"""LINE 导航器：在"锁屏/主页/其他 App/聊天列表/某会话"之间移动。

设计重点：
  - 每个动作后**都重新 dump + detect_screen_state** 确认状态，而不是盲打 sleep
  - 每一步都有超时和最大重试；失败时返回详细 reason 而不是 raise
  - 依赖注入一个 dump_func（异步 → bytes），这样 runner/测试都能复用

依赖：
  - adb_helpers（input_tap / input_keyevent / ensure_line_foreground）
  - screen_state.detect_screen_state
  - chat_list_scanner.parse_unread_rows
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Optional, Tuple

from src.integrations.line_rpa import adb_helpers as adb
from src.integrations.line_rpa import screen_ocr
from src.integrations.line_rpa import screen_state as ss
from src.integrations.line_rpa.chat_list_scanner import UnreadRow, parse_unread_rows

logger = logging.getLogger(__name__)

DumpFunc = Callable[[], Awaitable[Tuple[Optional[bytes], str]]]
# P6-B2: vision 列表扫描函数签名 (png_bytes, max_rows) → (List[UnreadRow], str)
VisionScanFunc = Callable[[bytes, int], Awaitable[Tuple[List[UnreadRow], str]]]


@dataclass
class NavResult:
    ok: bool
    state: str
    reason: str
    attempts: int = 0
    xml: Optional[bytes] = None


class Navigator:
    def __init__(
        self,
        *,
        serial: str,
        line_pkg: str,
        splash_activity: str,
        dump_func: DumpFunc,
        after_tap_sleep_sec: float = 0.8,
        after_launch_sleep_sec: float = 1.2,
        chat_list_tab_tap: Optional[Tuple[int, int]] = None,
        red_dot_cfg: Optional[dict] = None,
        vision_scan_func: Optional["VisionScanFunc"] = None,
        vision_scan_budget_sec: float = 30.0,
    ) -> None:
        self._serial = serial
        self._pkg = line_pkg
        self._splash = splash_activity
        self._dump = dump_func
        self._after_tap = float(after_tap_sleep_sec)
        self._after_launch = float(after_launch_sleep_sec)
        self._chat_list_tab_tap = chat_list_tab_tap  # (x, y) 人工标定 "聊天" tab 坐标
        self._red_dot_cfg = red_dot_cfg if isinstance(red_dot_cfg, dict) else None
        # P6-B2: vision 驱动列表扫描（OOM 机型回退）
        self._vision_scan_func = vision_scan_func
        self._vision_scan_budget_sec = max(5.0, float(vision_scan_budget_sec))
        self._last_vision_scan_ts: float = 0.0

    # ── 低层：等待某一屏幕状态 ──────────────────────────
    async def wait_for_state(
        self,
        targets: List[str],
        *,
        timeout_sec: float = 8.0,
        poll_sec: float = 0.8,
    ) -> NavResult:
        t0 = time.time()
        last_state = ss.UNKNOWN
        last_reason = ""
        last_xml: Optional[bytes] = None
        attempts = 0
        while time.time() - t0 < timeout_sec:
            attempts += 1
            xml, _ = await self._dump()
            last_xml = xml
            state, reason = ss.detect_screen_state(xml, line_pkg=self._pkg)
            last_state, last_reason = state, reason
            if state in targets:
                return NavResult(True, state, reason, attempts, xml)
            await asyncio.sleep(poll_sec)
        return NavResult(False, last_state, f"timeout;{last_reason}", attempts, last_xml)

    # ── 基本动作 ────────────────────────────────────────
    async def press_back(self) -> None:
        await asyncio.to_thread(adb.input_keyevent, self._serial, "4")  # BACK
        await asyncio.sleep(self._after_tap)

    async def press_home(self) -> None:
        await asyncio.to_thread(adb.input_keyevent, self._serial, "3")  # HOME
        await asyncio.sleep(self._after_tap)

    async def tap(self, x: int, y: int) -> None:
        await asyncio.to_thread(adb.input_tap, self._serial, int(x), int(y))
        await asyncio.sleep(self._after_tap)

    async def launch_line(self) -> None:
        await asyncio.to_thread(
            adb.ensure_line_foreground, self._serial, self._pkg, self._splash
        )
        await asyncio.sleep(self._after_launch)

    async def swipe_chat_list_down(
        self,
        *,
        ratio: float = 0.55,
        duration_ms: int = 380,
    ) -> bool:
        """在聊天列表页向上拖内容 ≈ ratio*屏高（揭示下方旧的会话）。

        返回 True 表示滑动指令已发出。不负责检测是否真的滚动生效（那由下一次 scan 验证）。
        """
        size = await asyncio.to_thread(adb.screen_size, self._serial)
        if size is None:
            size = (1080, 2340)
        w, h = size
        x = int(w * 0.5)
        y1 = int(h * 0.75)
        y2 = int(h * (0.75 - max(0.1, min(0.85, ratio))))
        await asyncio.to_thread(
            adb.input_swipe, self._serial, x, y1, x, y2, duration_ms,
        )
        await asyncio.sleep(self._after_tap)
        return True

    # P4-2：向下拖内容（揭示上方新的会话 / 刷新列表头）
    async def swipe_chat_list_up(
        self,
        *,
        ratio: float = 0.55,
        duration_ms: int = 380,
    ) -> bool:
        size = await asyncio.to_thread(adb.screen_size, self._serial)
        if size is None:
            size = (1080, 2340)
        w, h = size
        x = int(w * 0.5)
        y1 = int(h * 0.25)
        y2 = int(h * (0.25 + max(0.1, min(0.85, ratio))))
        await asyncio.to_thread(
            adb.input_swipe, self._serial, x, y1, x, y2, duration_ms,
        )
        await asyncio.sleep(self._after_tap)
        return True

    # P4-2：确保滚到顶（循环向下拖直到签名不变或达到 max_attempts）
    async def scroll_chat_list_to_top(
        self,
        *,
        max_attempts: int = 4,
        sig_fn=None,
    ) -> int:
        """尽力把聊天列表滚到顶部。

        sig_fn 可选：(xml_bytes) -> hashable，用于"签名未变即停"。默认用前 120 字符哈希兜底。
        返回实际尝试滑动次数。
        """
        last_sig = None
        attempts = 0
        for _ in range(max(0, int(max_attempts))):
            xml, _ = await self._dump()
            try:
                if sig_fn and xml:
                    sig = sig_fn(xml)
                elif xml:
                    sig = hash(xml[:200])
                else:
                    sig = None
            except Exception:
                sig = None
            if sig is not None and last_sig is not None and sig == last_sig:
                break  # 已到顶或滑动未生效
            last_sig = sig
            await self.swipe_chat_list_up(ratio=0.7, duration_ms=280)
            attempts += 1
        return attempts

    # ── 高层：回到聊天列表 ──────────────────────────────
    async def goto_chat_list(self, *, max_steps: int = 6) -> NavResult:
        """从任意状态回到聊天列表页。

        策略：多段小步走，每步观察状态。
        """
        attempts = 0
        last_xml: Optional[bytes] = None
        for step in range(max_steps):
            attempts += 1
            xml, _ = await self._dump()
            last_xml = xml
            state, reason = ss.detect_screen_state(xml, line_pkg=self._pkg)

            if state == ss.CHAT_LIST:
                return NavResult(True, state, f"reached after {step} steps", attempts, xml)

            if state == ss.LOCK_SCREEN:
                # 无法解锁 → 交上层决定（返回失败供告警）
                return NavResult(
                    False, state, "lock_screen;need_manual_unlock", attempts, xml,
                )

            if state == ss.OTHER_APP or state == ss.UNKNOWN:
                # 拉 LINE 到前台
                await self.launch_line()
                continue

            if state == ss.CHAT_ROOM:
                # 按 BACK 退出会话
                await self.press_back()
                continue

            if state == ss.OTHER_LINE:
                # 处于 LINE 但不在列表/会话：
                # 1) 若配置了聊天 Tab 坐标，点它
                if self._chat_list_tab_tap:
                    x, y = self._chat_list_tab_tap
                    await self.tap(x, y)
                    continue
                # 2) 否则按一下 BACK（如 VOOM/设置等子页多会回列表）
                await self.press_back()
                continue

        # 最后再探一次
        xml, _ = await self._dump()
        last_xml = xml
        state, reason = ss.detect_screen_state(xml, line_pkg=self._pkg)
        return NavResult(
            state == ss.CHAT_LIST, state,
            f"exhausted;{reason}", attempts, last_xml,
        )

    # ── 高层：从列表打开某会话 ──────────────────────────
    async def open_unread_chat(
        self,
        row: UnreadRow,
        *,
        timeout_sec: float = 6.0,
    ) -> NavResult:
        await self.tap(row.tap_x, row.tap_y)
        r = await self.wait_for_state([ss.CHAT_ROOM], timeout_sec=timeout_sec)
        if not r.ok:
            return NavResult(
                False, r.state,
                f"open_chat_fail tap=({row.tap_x},{row.tap_y}) name={row.name!r};{r.reason}",
                r.attempts, r.xml,
            )
        return r

    async def back_to_chat_list(self, *, timeout_sec: float = 6.0) -> NavResult:
        await self.press_back()
        r = await self.wait_for_state([ss.CHAT_LIST], timeout_sec=timeout_sec)
        if not r.ok:
            # 可能 LINE 的子页结构多了一层，再 BACK 一次
            await self.press_back()
            r = await self.wait_for_state([ss.CHAT_LIST], timeout_sec=timeout_sec)
        return r

    # ── 一次扫未读 ──────────────────────────────────────
    async def scan_unread_rows(
        self,
        *,
        max_rows: int = 10,
    ) -> Tuple[List[UnreadRow], str, Optional[bytes]]:
        xml, _ = await self._dump()

        # P6-B2: XML 失败 → vision 驱动回退（OOM 机型）
        if not xml:
            if self._vision_scan_func is not None:
                now = time.time()
                if (now - self._last_vision_scan_ts) < self._vision_scan_budget_sec:
                    wait_s = self._vision_scan_budget_sec - (now - self._last_vision_scan_ts)
                    logger.debug("vision scan budget: wait %.1fs", wait_s)
                    return [], f"vision_scan_budget_wait:{wait_s:.0f}s", None
                png = None
                try:
                    png = await asyncio.to_thread(
                        screen_ocr.capture_screen_png, self._serial, adb
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug("vision scan 截图失败: %s", e)
                if png:
                    self._last_vision_scan_ts = time.time()
                    try:
                        rows, dbg = await self._vision_scan_func(png, max_rows)
                        return rows, f"vision_fallback:{dbg}", None
                    except Exception as e:  # noqa: BLE001
                        return [], f"vision_scan_error:{e}", None
            return [], "no_xml_for_scan", None

        state, reason = ss.detect_screen_state(xml, line_pkg=self._pkg)
        if state != ss.CHAT_LIST:
            # 若 XML 状态非列表但 vision_scan 可用，也尝试截图扫
            if self._vision_scan_func is not None:
                now = time.time()
                if (now - self._last_vision_scan_ts) >= self._vision_scan_budget_sec:
                    png2 = None
                    try:
                        png2 = await asyncio.to_thread(
                            screen_ocr.capture_screen_png, self._serial, adb
                        )
                    except Exception:
                        pass
                    if png2:
                        self._last_vision_scan_ts = time.time()
                        try:
                            rows2, dbg2 = await self._vision_scan_func(png2, max_rows)
                            if rows2:
                                return rows2, f"vision_state_fallback:{dbg2}", None
                        except Exception:
                            pass
            return [], f"not_chat_list;state={state};{reason}", xml

        png: Optional[bytes] = None
        if self._red_dot_cfg and self._red_dot_cfg.get("enabled"):
            try:
                png = await asyncio.to_thread(
                    screen_ocr.capture_screen_png, self._serial, adb
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("red_dot 截图失败: %s", e)
                png = None

        rows, dbg = parse_unread_rows(
            xml,
            max_rows=max_rows,
            png_bytes=png,
            red_dot_cfg=self._red_dot_cfg,
        )
        return rows, dbg, xml
