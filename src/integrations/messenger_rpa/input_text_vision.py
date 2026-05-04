"""``inject_and_verify`` 的 Vision 兜底——读输入框真实文字。

dump-dead 设备（MIUI 把 uiautomator OOM-kill）上 ``inject_and_verify``
原来直接 ``ok=True reason='no_verify_dump_failed'``——盲信注入，键盘焦点
错位 / IME 没接管 / clipboard fallback 静默失败都不会被发现。

本模块提供 Vision 兜底：截屏 → 裁底栏（含输入框）→ GLM-4V-Flash 读
EditText 文本 → 上层做 expected/actual 对比。

成本：一次发送多 1 次 Vision 调用（≈5s）。仅在 dump 失败时触发。

设计选择 ── 为什么不裁更窄的"输入框 bbox"
    输入框的精确 bbox 需要 dump UI 才知道——但 dump 已经失败了。所以裁
    底部 30%（覆盖 keyboard-open 与 keyboard-closed 两种 layout）传给
    Vision，让它自己识别"消息输入框里有什么字"。多 100 多 KB 流量但准。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional

from src.integrations.line_rpa import adb_helpers as adb

logger = logging.getLogger(__name__)

# 底部 30% 在 720x1600 上 = y∈[1120, 1600]，覆盖：
#   - keyboard-open 时：输入框 + 键盘 + send 按钮
#   - keyboard-closed 时：输入框 + 一行 emoji 选项
# 不放宽到 50% 是为了避开 chat 气泡（防止 LLM 把"我刚发的消息"当成"输入框文字"）
_BOTTOM_STRIP_RATIO = 0.30
_MIN_CROP_HEIGHT = 200

# 任务名——见 vision_task_models.VISION_TASKS["input_verify"] 的实测笔记
_TASK_NAME = "input_verify"

_PROMPT = (
    "Look at the rounded input box near the bottom of this Messenger screen. "
    "If it shows only the gray word Message, output EMPTY. "
    "Otherwise output exactly what is typed in it. "
    "Reply with just the typed text, or the word EMPTY. No quotes, no labels."
)


@dataclass(frozen=True)
class VisionInputTextResult:
    text: Optional[str]
    debug: str = ""


def screencap_bottom_strip(
    serial: str,
    *,
    bottom_ratio: float = _BOTTOM_STRIP_RATIO,
    timeout: float = 22.0,
) -> Optional[Path]:
    """``adb exec-out screencap`` → 裁底部 → 临时 PNG。失败返 None。"""
    png_bytes, _err, code = adb.run_adb_binary(
        ["exec-out", "screencap", "-p"],
        serial=serial, timeout=timeout,
    )
    if code != 0 or not png_bytes or not png_bytes.startswith(b"\x89PNG"):
        logger.debug(
            "[input_text_vision] screencap rc=%s len=%s",
            code, len(png_bytes) if png_bytes else 0,
        )
        return None

    def _mktmp() -> Path:
        fd, name = tempfile.mkstemp(prefix="mrpa_input_", suffix=".png")
        try:
            os.close(fd)
        except OSError:
            pass
        return Path(name)

    try:
        from PIL import Image
    except ImportError:
        logger.debug("[input_text_vision] PIL 不可用，发整张")
        try:
            tmp = _mktmp()
            tmp.write_bytes(png_bytes)
            return tmp
        except OSError:
            return None

    try:
        img = Image.open(BytesIO(png_bytes))
        w, h = img.size
        crop_h = max(_MIN_CROP_HEIGHT, int(h * bottom_ratio))
        # 取底部 crop_h 像素
        cropped = img.crop((0, h - crop_h, w, h))
        tmp = _mktmp()
        cropped.save(tmp, format="PNG")
        return tmp
    except Exception as e:
        logger.debug(
            "[input_text_vision] crop 失败 %s；写整张原 PNG", e,
        )
        try:
            tmp = _mktmp()
            tmp.write_bytes(png_bytes)
            return tmp
        except OSError:
            return None


def parse_input_text_response(text: str) -> Optional[str]:
    """LLM 返回 → 输入框文本字符串。

    协议：
      - 'EMPTY'（任意大小写）→ ``""`` 表示输入框是 hint 状态
      - 单行裸文本 → 直接当文本（去 quote）
      - JSON ``{"text":"..."}`` → 取 text 字段（兼容旧版 prompt）
      - 解析不出 → ``None``
    """
    if not text:
        return None
    s = text.strip()

    # 去 markdown fence
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.MULTILINE)
    s = re.sub(r"\s*```\s*$", "", s, flags=re.MULTILINE)
    s = s.strip()

    if not s:
        return None

    # 协议关键字
    if s.upper().strip(' "\'`') == "EMPTY":
        return ""

    # JSON 兼容（嵌在 prose 里也能抽出）
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                t = obj.get("text")
                if isinstance(t, str):
                    return t
        except json.JSONDecodeError:
            pass
    # 任何位置的 "text":"..." 字段
    m = re.search(r'"text"\s*:\s*"([^"]*)"', s)
    if m:
        return m.group(1)

    # 单行裸文本：去前后引号、合并多余空白
    line = s.splitlines()[0].strip()
    line = line.strip('"\'`')
    if not line:
        return None
    # 排除明显的"模型在解释"——超长 / 含元描述词
    if len(line) > 200:
        return None
    meta_words = (
        "input box", "input field", "composer", "the field",
        "the box", "screenshot",
    )
    low = line.lower()
    if any(w in low for w in meta_words):
        return None
    return line


def read_input_text_via_vision(
    serial: str,
    vision_cfg: Dict[str, Any],
    global_vision: Optional[Dict[str, Any]] = None,
    *,
    bottom_ratio: float = _BOTTOM_STRIP_RATIO,
    cleanup: bool = True,
    task_name: str = _TASK_NAME,
) -> VisionInputTextResult:
    """主入口：截屏底部 → Vision 读输入框文本。

    返 ``text=None`` 时调用方应当走 ``no_verify_*`` 兜底逻辑（不放行 mismatch）。

    ``task_name`` 默认 ``"input_verify"`` → vision_task_models 表会强制选 plus
    （flash 在该任务上 100% false negative，详见任务表 notes）。
    """
    img_path = screencap_bottom_strip(serial, bottom_ratio=bottom_ratio)
    if img_path is None:
        return VisionInputTextResult(text=None, debug="screencap_failed")

    from src.integrations.messenger_rpa.vision_task_models import cfg_for_task
    from src.integrations.messenger_rpa import vision_metrics as _vm
    import time as _t
    title_cfg = cfg_for_task(task_name, base_cfg=vision_cfg)

    # P6: 度量起点。所有 return 路径必须 record（含失败 & 异常）。
    _t0 = _t.time()
    _used_model: Optional[str] = title_cfg.get("model")
    _used_provider: Optional[str] = title_cfg.get("provider")

    def _emit(ok_flag: bool, error_class: Optional[str]) -> None:
        try:
            _vm.record(
                task_name=task_name,
                model=_used_model,
                api_provider=_used_provider,
                duration_ms=int((_t.time() - _t0) * 1000),
                ok=ok_flag,
                error_class=error_class,
            )
        except Exception:
            pass

    try:
        from src.vision_client import VisionClient

        vc = VisionClient(title_cfg)
        ok = vc.initialize()
        if not ok:
            gv = global_vision or {}
            zk = (gv.get("zhipu_api_key") or "").strip()
            if zk and zk != "YOUR_ZHIPU_API_KEY":
                # 主端起不来 → 切 zhipu，沿用任务表的模型选择
                z_cfg = cfg_for_task(
                    task_name,
                    base_cfg={
                        **title_cfg,
                        "provider": "zhipu",
                        "api_key": zk,
                    },
                )
                z_cfg.pop("base_url", None)
                vc = VisionClient(z_cfg)
                _used_model = z_cfg.get("model")
                _used_provider = z_cfg.get("provider")
                if not vc.initialize():
                    _emit(False, "vision_init_fail")
                    return VisionInputTextResult(
                        text=None, debug="vision_init_fail",
                    )
            else:
                _emit(False, "vision_init_fail_no_zhipu")
                return VisionInputTextResult(
                    text=None, debug="vision_init_fail_no_zhipu",
                )

        text = vc.describe_image_sync(str(img_path), prompt=_PROMPT)
        if not text:
            _emit(False, "vision_empty")
            return VisionInputTextResult(text=None, debug="vision_empty")
        parsed = parse_input_text_response(text)
        if parsed is None:
            _emit(False, "parse_fail")
            return VisionInputTextResult(
                text=None, debug=f"parse_fail:{text[:60]!r}",
            )
        _emit(True, None)
        return VisionInputTextResult(text=parsed, debug="ok")
    except Exception as e:
        logger.debug("[input_text_vision] vision call exc", exc_info=True)
        _emit(False, f"exc:{type(e).__name__}")
        return VisionInputTextResult(text=None, debug=f"exc:{type(e).__name__}")
    finally:
        if cleanup and img_path is not None:
            try:
                img_path.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = [
    "VisionInputTextResult",
    "screencap_bottom_strip",
    "parse_input_text_response",
    "read_input_text_via_vision",
]
