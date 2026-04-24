"""坐标自适应校准。

问题：
    coords.py 是基于 720×1600 物理像素硬标定的，对其他分辨率（1080×2400 等）
    虽然 Coord.at(w, h) 做了等比缩放，但 Messenger UI **不是按比例排版**——
    顶 status bar 高度由 system 决定，bottom nav 由 Bloks 决定，padding 在不同
    DPI 上行为不同。

    实测后必然存在偏差。换台机上：tap 第一行会落到第二行，输入框点到附件按钮等。

方案：
    1. 设备启动时 → wm size 拿真实 W×H
    2. 拿到 inbox 截图 → 让 vision 找：
       - 顶部标题底沿 Y（"Messenger" 文字下沿）
       - Stories 行底沿 Y（圆头像底沿）
       - 第一个 chat row 中心 Y
       - chat row 行高（第二行中心 - 第一行中心）
       - 底部 tab bar 中心 Y（"Chats" 文字所在）
    3. 与 BASE 比较算偏移因子，写到 coords/<serial>.json
    4. runner 启动时若有该文件，覆盖 cc 内部全局变量

调用：
    info = await calibrate_inbox(serial="...", vision_client=v, screenshot_png=...)
    save_calibration("...", info)
    apply_calibration("...")  # 修改 cc 模块的全局值
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.integrations.messenger_rpa import coords as cc

logger = logging.getLogger(__name__)


@dataclass
class InboxAnchors:
    """从 inbox 截图上识别出的关键 Y 坐标（物理像素）。"""

    width: int
    height: int
    top_title_bottom_y: Optional[int] = None
    stories_row_y: Optional[int] = None
    chat_row_first_y: Optional[int] = None
    chat_row_height: Optional[int] = None
    tab_bar_y: Optional[int] = None
    notes: str = ""


_CALIBRATE_PROMPT = """这是 Messenger Inbox 截图（W=<W> H=<H>）。请输出以下 Y 像素坐标的 JSON：

{
  "top_title_bottom_y": <"Messenger/Chats" 标题文字底沿 Y，无则 -1>,
  "stories_row_y":      <Stories 圆头像中心 Y，无则 -1>,
  "chat_row_first_y":   <第一个联系人姓名行的 Y 中心>,
  "chat_row_second_y":  <第二个联系人姓名行的 Y 中心>,
  "tab_bar_y":          <底部 Chats/Stories/Menu tab 文字的 Y 中心>
}

只输出 JSON。"""


async def detect_anchors(
    *,
    vision_client: Any,
    screenshot_png_path: str,
    width: int,
    height: int,
    timeout_sec: float = 30.0,
) -> InboxAnchors:
    """识别 Inbox 关键 Y 锚点。

    v2 双通道策略：
      1) 先跑 **像素级** `auto_calibrate.calibrate_inbox_rows`
         （扫左侧头像列的灰度密度峰值）—— 快（<1s）、稳、免 vision token。
      2) 若像素级失败（屏上头像不够、非 Inbox 页等），再 fallback 到 vision
         调用，让 LLM 估 Y（慢 + 偶尔离谱，但覆盖 edge case）。
    """
    # ── 通道 1：像素级（主力） ────────────────────────
    try:
        from src.integrations.messenger_rpa.auto_calibrate import (
            calibrate_inbox_rows,
        )
        calib = calibrate_inbox_rows(screenshot_png_path)
        if calib.ok:
            # 输出坐标已被 calibrate_inbox_rows 归一到 720×1600；
            # 这里要把它**再缩回**当前 width/height 物理像素
            ry = float(height) / 1600.0
            rx = float(width) / 720.0
            first_phy = int(round(calib.first_y * ry))
            h_phy = int(round(calib.row_height * ry))
            logger.info(
                "[calibrator] 像素级成功 first_y(norm)=%d h(norm)=%d "
                "→ 物理 first_y=%d h=%d visible_rows=%d",
                calib.first_y, calib.row_height, first_phy, h_phy,
                calib.visible_rows,
            )
            return InboxAnchors(
                width=width,
                height=height,
                chat_row_first_y=first_phy,
                chat_row_height=h_phy,
                notes=f"pixel_ok:rows={calib.visible_rows}",
            )
        else:
            logger.info(
                "[calibrator] 像素级失败 reason=%s raw_peaks=%s，fallback vision",
                calib.reason, calib.peaks_raw,
            )
    except Exception as ex:
        logger.warning("[calibrator] 像素级异常，fallback vision err=%s", ex)

    # ── 通道 2：Vision 兜底 ───────────────────────────
    prompt = (
        _CALIBRATE_PROMPT.replace("<W>", str(width)).replace("<H>", str(height))
    )

    # 异步走 describe_image，否则 fallback 到 describe_image_sync
    raw_text: str
    if hasattr(vision_client, "describe_image"):
        try:
            raw_text = await asyncio.wait_for(
                vision_client.describe_image(screenshot_png_path, prompt),
                timeout=timeout_sec,
            )
        except (asyncio.TimeoutError, Exception):
            raw_text = ""
    else:
        loop = asyncio.get_running_loop()
        raw_text = await loop.run_in_executor(
            None,
            vision_client.describe_image_sync,
            screenshot_png_path,
            prompt,
        )
    raw_text = raw_text or ""

    cleaned = (raw_text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        # 尝试找第一个 { 到最后一个 }
        s = cleaned.find("{")
        e = cleaned.rfind("}")
        if s >= 0 and e > s:
            try:
                obj = json.loads(cleaned[s : e + 1])
            except Exception:
                logger.warning("[calibrator] vision 输出非 JSON: %r", cleaned[:200])
                return InboxAnchors(
                    width=width, height=height, notes=f"parse_failed: {cleaned[:80]}"
                )
        else:
            return InboxAnchors(
                width=width, height=height, notes=f"parse_failed: {cleaned[:80]}"
            )

    def _g(k: str) -> Optional[int]:
        v = obj.get(k)
        if v is None or v == -1 or v == "-1":
            return None
        try:
            iv = int(v)
            return iv if 0 <= iv <= height else None
        except (ValueError, TypeError):
            return None

    first = _g("chat_row_first_y")
    second = _g("chat_row_second_y")
    height_per_row = (
        (second - first) if (first is not None and second is not None and second > first) else None
    )

    return InboxAnchors(
        width=width,
        height=height,
        top_title_bottom_y=_g("top_title_bottom_y"),
        stories_row_y=_g("stories_row_y"),
        chat_row_first_y=first,
        chat_row_height=height_per_row,
        tab_bar_y=_g("tab_bar_y"),
        notes="",
    )


def _calib_dir(workspace: Path) -> Path:
    p = workspace / "data" / "messenger_rpa_calibration"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _calib_file(workspace: Path, serial: str) -> Path:
    safe = serial.replace(":", "_").replace(".", "_")
    return _calib_dir(workspace) / f"{safe}.json"


def save_calibration(
    workspace: Path, serial: str, anchors: InboxAnchors
) -> Path:
    fp = _calib_file(workspace, serial)
    fp.write_text(
        json.dumps(asdict(anchors), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("[calibrator] 已保存 %s", fp)
    return fp


def load_calibration(workspace: Path, serial: str) -> Optional[InboxAnchors]:
    fp = _calib_file(workspace, serial)
    if not fp.exists():
        return None
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
        return InboxAnchors(**{k: d.get(k) for k in InboxAnchors.__dataclass_fields__})
    except Exception:
        logger.exception("[calibrator] 读取 %s 失败", fp)
        return None


@dataclass
class CalibratedCoords:
    """一组校准过的「等比 + 偏移修正后」的常用坐标（物理像素，特定设备）。"""

    chat_row_first_y: int
    chat_row_height: int
    chat_row_text_x: int
    tab_chats_y: int
    tab_chats_x: int
    width: int
    height: int


def calibrated_for(
    serial: str, width: int, height: int, anchors: InboxAnchors
) -> CalibratedCoords:
    """把 anchors（实测）和 cc 默认值（720×1600 等比缩放）合并出一组实战坐标。

    设计取舍（v2）：
    - chat_row_first_y / row_height：优先用 anchors（通常由像素级 auto_calibrate 算出，
      范围内即信；范围外回退到 BASE 等比缩放）。像素扫描比 vision 估 Y 稳定得多。
    - tab_bar_y：vision 校准（位置突出，估算误差 <20px）；不在合法范围就等比缩放。
    """
    rx = float(width) / cc.BASE_WIDTH
    ry = float(height) / cc.BASE_HEIGHT

    def fb(default_base_y: int) -> int:
        return int(round(default_base_y * ry))

    # ★ chat_row：anchors 合法 → 直接用；非法 → 退回等比缩放
    if (
        anchors.chat_row_first_y is not None
        and 400 * ry <= anchors.chat_row_first_y <= 900 * ry
    ):
        chat_first_y = int(anchors.chat_row_first_y)
    else:
        chat_first_y = fb(cc.CHAT_ROW_FIRST_Y)

    if (
        anchors.chat_row_height is not None
        and 80 * ry <= anchors.chat_row_height <= 220 * ry
    ):
        chat_h = int(anchors.chat_row_height)
    else:
        chat_h = fb(cc.CHAT_ROW_HEIGHT)

    # tab_bar_y 让 vision 校准（误差小，且位置关键）
    tab_y = (
        anchors.tab_bar_y
        if anchors.tab_bar_y is not None and 1200 <= anchors.tab_bar_y <= height
        else fb(cc.TAB_CHATS.y)
    )

    chat_text_x = int(round(cc.CHAT_ROW_TEXT_X * rx))
    tab_x = int(round(cc.TAB_CHATS.x * rx))

    return CalibratedCoords(
        chat_row_first_y=chat_first_y,
        chat_row_height=chat_h,
        chat_row_text_x=chat_text_x,
        tab_chats_y=tab_y,
        tab_chats_x=tab_x,
        width=width,
        height=height,
    )


__all__ = [
    "InboxAnchors",
    "CalibratedCoords",
    "detect_anchors",
    "save_calibration",
    "load_calibration",
    "calibrated_for",
]
