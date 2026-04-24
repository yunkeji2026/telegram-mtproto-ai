"""Messenger Thread 页的"有断言"动作层。

职责：把"点一下 / 输一下"的裸 ADB 操作，升级为带 **前置条件 + 后置断言** 的
可组合动作。上层（runner / service）只需要调这里暴露的高层函数，不用再
管键盘弹没弹、输入框有没有字、发送键在哪儿这种细节。

设计动机（写在前面免得被 copy-paste）：

- ``uiautomator dump`` 在真机上 80-200ms，远低于 Vision（2-5s），所以可以
  在一次发送流程内调用 2-4 次而不伤响应时延。
- ``dump`` 的输出写 ``/sdcard/_ui.xml``，用 ``cat`` 读回来，避免在 Windows
  上 ``adb pull`` 引起的 cwd/路径歧义（曾经踩过坑）。
- ``stderr`` 里可能混有 MIUI 的 ThemeCompatibilityLoader 堆栈 —— 那是
  uiautomator dump 自身的 warning，**不要**当作失败信号；只看 stdout 是
  不是 ``<?xml`` 开头。

公开接口：

- :func:`dump_view_tree`：一次性拿到当前页 XML（字符串）
- :func:`verify_thread_title`：打开会话后确认是不是打开对了 (U1)
- :func:`wait_keyboard_open`：等键盘弹起 + 输入框可用
- :func:`inject_and_verify`：注入文字 + 读回 EditText.text 对比
- :func:`tap_send_when_ready`：断言键盘已弹 + SEND 键可见后再点
- :func:`assert_sent`：发送后检查最后一条气泡是不是我方刚发的 (U4 简版)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.integrations.line_rpa import adb_helpers as adb
from src.integrations.messenger_rpa import ui_scraper as uis

logger = logging.getLogger(__name__)

_DEFAULT_REMOTE = "/sdcard/_ui_scraper.xml"
_XML_HEAD = "<?xml"


# ── 基础：dump 当前页 view tree ────────────────────────────

def dump_view_tree(
    serial: str,
    *,
    remote_path: str = _DEFAULT_REMOTE,
    dump_timeout: float = 20.0,
    cat_timeout: float = 10.0,
    cleanup: bool = True,
) -> Optional[str]:
    """触发一次 uiautomator dump 并把 XML 拿回来。

    失败返回 ``None``。耗时典型 150-400ms。
    """
    t0 = time.time()
    r1 = adb.run_adb(
        ["shell", f"uiautomator dump {remote_path}"],
        serial=serial, timeout=dump_timeout,
    )
    if r1.returncode != 0:
        # MIUI 的 ThemeCompatibility 堆栈是走 stderr 的 warning，但 rc=0；
        # 只有在 rc!=0 时才当作失败。
        logger.debug(
            "[thread_actions] dump rc=%d stderr=%r",
            r1.returncode, (r1.stderr or "")[:120],
        )
        return None
    r2 = adb.run_adb(
        ["shell", f"cat {remote_path}"],
        serial=serial, timeout=cat_timeout,
    )
    out = r2.stdout or ""
    # 部分机型 / shell 会在 ``<?xml`` 前输出一行 MIUI/theme 的 log，导致
    # ``startswith('<?xml')`` 误判失败；从首个 ``<?xml`` 起截断即可。
    pos = out.find(_XML_HEAD)
    if pos > 0:
        out = out[pos:]
    if r2.returncode != 0 or _XML_HEAD not in (out[:200] if out else ""):
        logger.debug(
            "[thread_actions] cat xml failed rc=%d head=%r",
            r2.returncode, (out or "")[:80],
        )
        return None
    if cleanup:
        # best-effort；失败不影响调用方
        adb.run_adb(
            ["shell", f"rm -f {remote_path}"],
            serial=serial, timeout=5.0,
        )
    logger.debug(
        "[thread_actions] dump ok chars=%d dt=%.0fms",
        len(out), (time.time() - t0) * 1000,
    )
    return out


# ── U1: 顶栏联系人名二次校验 ───────────────────────────────

@dataclass
class VerifyResult:
    ok: bool
    actual: Optional[str] = None
    expected: Optional[str] = None
    reason: str = ""


def _normalize_peer_name(s: str) -> str:
    """比较 peer name 时忽略方向字符和首尾空格。

    Messenger 偶尔会把用户名包一层 LRE/PDF（``\\u202a...\\u202c``）、
    加 zero-width space 等。对比时需要剥掉。
    """
    if not s:
        return ""
    out = []
    for ch in s:
        # 移除 LRE(U+202A)/RLE/PDF/LRM 等控制字符
        if 0x200B <= ord(ch) <= 0x200F or 0x202A <= ord(ch) <= 0x202E:
            continue
        out.append(ch)
    return "".join(out).strip().casefold()


def peer_names_match(
    a: str,
    b: str,
    *,
    allow_substr: bool = True,
) -> bool:
    """Vision / 配置里的「对方显示名」是否与 ``chat_name`` 一致（规范化后）。

    用于 inbox 行匹配、与 :func:`verify_thread_title` 的判定口径一致。
    """
    na = _normalize_peer_name(a)
    nb = _normalize_peer_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if allow_substr and (na in nb or nb in na):
        return True
    return False


def peer_names_match_inbox_pick(row_name: str, target_name: str) -> bool:
    """Inbox 里**选行**用的名字匹配（比 :func:`peer_names_match` 更严）。

    ``peer_names_match`` 的 ``na in nb`` 会把列表里的 **短显示名** 误当成
    **长目标名** 的前缀（例：行名 ``Victor`` 错误命中目标 ``Victor Zan``），
    互发/直发时极易点进错会话。

    规则（``row_name``=Vision 给的该行名，``target_name``=配置/CLI 要找的人）：

    - 规范化后全等；
    - 或 **目标全名** 出现在 **行名** 内（行名更长：带 Active/省略号等）；
    - 或 **行名是目标的前缀** 且与目标长度差 ≤3（Vision 截断 ``Jane Do`` vs ``Jane Doe``）。
    """
    na = _normalize_peer_name(row_name)
    nb = _normalize_peer_name(target_name)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if nb in na:
        return True
    if na in nb and nb.startswith(na) and (len(nb) - len(na) <= 3):
        return True
    return False


def verify_thread_title(
    serial: str,
    expected_peer: str,
    *,
    allow_substr: bool = True,
) -> VerifyResult:
    """U1：当前是否处于"和 ``expected_peer`` 的会话"页。

    判定：
      1. 不在 Thread 页（无顶栏 title）→ ``ok=False reason=not_in_thread``
      2. 顶栏 title 命中 ``expected_peer`` → ``ok=True``
      3. 不命中但 ``allow_substr=True`` 且一方是另一方的子串（中文空格
         差、昵称后缀等）→ ``ok=True`` + reason 注明
      4. 完全不匹配 → ``ok=False`` + actual 字段
    """
    expected = (expected_peer or "").strip()
    if not expected:
        return VerifyResult(ok=False, reason="empty_expected")
    xml: Optional[str] = None
    for _attempt in range(3):
        xml = dump_view_tree(serial)
        if xml is not None:
            break
        time.sleep(0.22)
    if xml is None:
        return VerifyResult(
            ok=False, reason="dump_failed", expected=expected,
        )
    title = uis.find_thread_title(xml)
    if title is None:
        return VerifyResult(
            ok=False, reason="not_in_thread", expected=expected,
        )
    matched = (
        peer_names_match_inbox_pick(title, expected)
        if allow_substr
        else peer_names_match(title, expected, allow_substr=False)
    )
    if matched:
        nt = _normalize_peer_name(title)
        ne = _normalize_peer_name(expected)
        reason = "exact" if nt == ne else "substr"
        return VerifyResult(
            ok=True, actual=title, expected=expected, reason=reason,
        )
    return VerifyResult(
        ok=False, actual=title, expected=expected, reason="mismatch",
    )


# ── U2: 键盘 & 输入框状态等待 ──────────────────────────────

@dataclass
class KeyboardWaitResult:
    ok: bool
    input_box: Optional[uis.InputBoxState] = None
    xml: Optional[str] = None
    reason: str = ""
    tries: int = 0


async def wait_keyboard_open(
    serial: str,
    *,
    screen_h: int = 1600,
    timeout_sec: float = 3.0,
    poll_interval_sec: float = 0.4,
) -> KeyboardWaitResult:
    """等待键盘弹起（EditText.top 上移到屏高 75% 以上）。

    超时返回 ``ok=False reason=timeout``。
    """
    deadline = time.time() + timeout_sec
    tries = 0
    last_xml: Optional[str] = None
    last_ib: Optional[uis.InputBoxState] = None
    while time.time() < deadline:
        tries += 1
        xml = dump_view_tree(serial)
        if xml is None:
            await asyncio.sleep(poll_interval_sec)
            continue
        last_xml = xml
        ib = uis.find_input_box(xml, screen_h=screen_h)
        last_ib = ib
        if ib is None:
            await asyncio.sleep(poll_interval_sec)
            continue
        if ib.keyboard_open:
            return KeyboardWaitResult(
                ok=True, input_box=ib, xml=xml,
                reason="ok", tries=tries,
            )
        await asyncio.sleep(poll_interval_sec)
    return KeyboardWaitResult(
        ok=False, input_box=last_ib, xml=last_xml,
        reason="timeout", tries=tries,
    )


# ── U2: 注入文字 + 读回校验 ─────────────────────────────────

@dataclass
class InjectVerifyResult:
    ok: bool
    injected_via: str = ""
    actual_text: str = ""
    expected_text: str = ""
    reason: str = ""
    tries: int = 0


async def inject_and_verify(
    serial: str,
    text: str,
    *,
    inject_cfg: Optional[Dict[str, Any]] = None,
    screen_h: int = 1600,
    settle_sec: float = 0.8,
    tolerate_truncation_chars: int = 2,
    max_retries: int = 2,
) -> InjectVerifyResult:
    """注入 ``text`` 到当前输入框 → 等 ``settle_sec`` → dump → 读 EditText.text 对比。

    - 输入法/剪贴板/input text 偶尔丢几个末尾字符是已知行为；预留
      ``tolerate_truncation_chars`` 作为容差（前缀命中即可）。
    - 失败自动重试 ``max_retries`` 次（先 KEYCODE_DEL 清空再重注）。
    """
    from src.integrations.messenger_rpa.text_input import inject_text

    expected = (text or "")[:1500]
    if not expected:
        return InjectVerifyResult(
            ok=False, reason="empty_text", expected_text="",
        )
    cfg = dict(inject_cfg or {})

    tries = 0
    for attempt in range(max_retries + 1):
        tries += 1
        ir = inject_text(
            serial,
            expected,
            use_adb_keyboard=bool(cfg.get("use_adb_keyboard", True)),
            adb_keyboard_ime=str(cfg.get("adb_keyboard_ime") or
                                 "com.android.adbkeyboard/.AdbIME").strip(),
            adb_keyboard_package=str(cfg.get("adb_keyboard_package") or
                                     "com.android.adbkeyboard").strip(),
            allow_clipboard_fallback=bool(
                cfg.get("allow_clipboard_fallback", True)
            ),
            allow_input_text_fallback_for_ascii=bool(
                cfg.get("allow_input_text_fallback_for_ascii", True)
            ),
        )
        if not ir.ok:
            # 注入本身失败——重试没有意义（同路径会再失败）
            return InjectVerifyResult(
                ok=False, injected_via=ir.path,
                actual_text="", expected_text=expected,
                reason=f"inject_failed:{ir.error[:80]}",
                tries=tries,
            )
        # 等 IME 把字画进 EditText
        await asyncio.sleep(settle_sec)
        xml = dump_view_tree(serial)
        if xml is None:
            # dump 失败不视为致命；再给一次机会
            if attempt >= max_retries:
                return InjectVerifyResult(
                    ok=True, injected_via=ir.path,
                    actual_text="", expected_text=expected,
                    reason="no_verify_dump_failed",
                    tries=tries,
                )
            continue
        ib = uis.find_input_box(xml, screen_h=screen_h)
        actual = (ib.text if ib and not ib.is_hint else "") or ""
        # 命中判定：actual == expected，或 expected 是 actual 的前缀
        # （IME 偶尔把末尾 N 个字符吃掉但前面正确）
        if actual == expected:
            return InjectVerifyResult(
                ok=True, injected_via=ir.path,
                actual_text=actual, expected_text=expected,
                reason="exact", tries=tries,
            )
        min_acceptable = max(0, len(expected) - tolerate_truncation_chars)
        if (
            actual and expected.startswith(actual)
            and len(actual) >= min_acceptable
        ):
            return InjectVerifyResult(
                ok=True, injected_via=ir.path,
                actual_text=actual, expected_text=expected,
                reason=f"prefix_ok_delta={len(expected) - len(actual)}",
                tries=tries,
            )
        # Mismatch。清空 + 重试（尝试有效次数内）
        if attempt < max_retries:
            # 用 DEL 键清空：按 2x 长度保证清光（输入法可能合并音节等）
            clear_n = max(len(actual), len(expected)) * 2 + 4
            for _ in range(clear_n):
                adb.run_adb(
                    ["shell", "input keyevent KEYCODE_DEL"],
                    serial=serial, timeout=3.0,
                )
            await asyncio.sleep(0.3)
            continue
        return InjectVerifyResult(
            ok=False, injected_via=ir.path,
            actual_text=actual, expected_text=expected,
            reason="mismatch", tries=tries,
        )
    # 不应该走到这里
    return InjectVerifyResult(
        ok=False, reason="unknown", expected_text=expected, tries=tries,
    )


# ── U2 完成：精准点 SEND ───────────────────────────────────

@dataclass
class TapSendResult:
    ok: bool
    tapped_x: int = 0
    tapped_y: int = 0
    reason: str = ""


def tap_send_when_ready(
    serial: str,
    *,
    screen_h: int = 1600,
    fallback_xy: Optional[Tuple[int, int]] = None,
) -> TapSendResult:
    """发送前最后一次 dump，找到 ``Button cd=发送`` 的 bbox 中心再点。

    只要求**键盘已弹 + SEND 键可见**；若不满足且传入 ``fallback_xy``，则
    以硬编坐标兜底但 reason 标 ``fallback``，上层应记监控。
    """
    xml = dump_view_tree(serial)
    if xml is None:
        if fallback_xy:
            adb.run_adb(
                ["shell", f"input tap {fallback_xy[0]} {fallback_xy[1]}"],
                serial=serial, timeout=5.0,
            )
            return TapSendResult(
                ok=True, tapped_x=fallback_xy[0], tapped_y=fallback_xy[1],
                reason="fallback_dump_failed",
            )
        return TapSendResult(ok=False, reason="dump_failed")
    ib = uis.find_input_box(xml, screen_h=screen_h)
    if ib is None or not ib.keyboard_open:
        if fallback_xy:
            adb.run_adb(
                ["shell", f"input tap {fallback_xy[0]} {fallback_xy[1]}"],
                serial=serial, timeout=5.0,
            )
            return TapSendResult(
                ok=True, tapped_x=fallback_xy[0], tapped_y=fallback_xy[1],
                reason="fallback_no_keyboard",
            )
        return TapSendResult(ok=False, reason="keyboard_not_open")
    btn = uis.find_send_button(xml)
    if btn is None:
        if fallback_xy:
            adb.run_adb(
                ["shell", f"input tap {fallback_xy[0]} {fallback_xy[1]}"],
                serial=serial, timeout=5.0,
            )
            return TapSendResult(
                ok=True, tapped_x=fallback_xy[0], tapped_y=fallback_xy[1],
                reason="fallback_send_btn_missing",
            )
        return TapSendResult(ok=False, reason="send_btn_missing")
    cx, cy = btn.cx, btn.cy
    adb.run_adb(
        ["shell", f"input tap {cx} {cy}"],
        serial=serial, timeout=5.0,
    )
    return TapSendResult(ok=True, tapped_x=cx, tapped_y=cy, reason="precise")


# ── U4: 发送后端到端断言（view-tree 版） ──────────────────

@dataclass
class AssertSentResult:
    ok: bool
    reason: str = ""
    hint_bubble: Optional[str] = None
    seen_by: Optional[str] = None  # 对方已读标记（若出现）


async def assert_sent(
    serial: str,
    sent_text: str,
    *,
    screen_w: int = 720,
    screen_h: int = 1600,
    wait_sec: float = 1.2,
    prefix_chars: int = 8,
) -> AssertSentResult:
    """发送后 ``wait_sec`` dump；用 ``last_bubble_preview`` 粗读最下一条气泡。

    匹配策略（宽松 → 严格）：
      1. 若输入框在发完后变回 hint 态（空）→ 大概率发出去了；
      2. 若最后一条 bubble 命中 ``sent_text`` 前 ``prefix_chars`` 字符
         且位于"self"侧 → 确认发出；
      3. 如果有 ``find_peer_read_marker`` 命中 → 已读（最强信号）。

    Litho 气泡有时 ``text=`` 为空（只 content-desc 带文字），这种时候条件 2
    可能判不出；此时只要条件 1 成立就认为 ok，但 reason 标 ``input_cleared``
    提醒上层可做 Vision 抽样补校。
    """
    await asyncio.sleep(wait_sec)
    xml = dump_view_tree(serial)
    if xml is None:
        return AssertSentResult(ok=False, reason="dump_failed")
    ib = uis.find_input_box(xml, screen_h=screen_h)
    seen_by = uis.find_peer_read_marker(xml)
    bubble, _dbg = uis.last_bubble_preview(
        xml, screen_w=screen_w,
    )
    # 信号 1：已读标记（最强）
    if seen_by:
        return AssertSentResult(
            ok=True, reason="seen_by_peer",
            hint_bubble=bubble, seen_by=seen_by,
        )
    # 信号 2：最下一条气泡前 N 字命中 + 位于 self 侧
    if bubble and sent_text:
        probe = (sent_text or "")[: max(1, prefix_chars)]
        if probe and bubble.startswith(probe):
            return AssertSentResult(
                ok=True, reason="bubble_prefix_match",
                hint_bubble=bubble, seen_by=seen_by,
            )
    # 信号 3：输入框被清空（发送动作最常见的副作用）
    if ib is not None and ib.is_hint:
        return AssertSentResult(
            ok=True, reason="input_cleared",
            hint_bubble=bubble, seen_by=seen_by,
        )
    # 全都失败
    return AssertSentResult(
        ok=False, reason="no_signal",
        hint_bubble=bubble, seen_by=seen_by,
    )


__all__ = [
    "VerifyResult",
    "KeyboardWaitResult",
    "InjectVerifyResult",
    "TapSendResult",
    "AssertSentResult",
    "dump_view_tree",
    "verify_thread_title",
    "wait_keyboard_open",
    "inject_and_verify",
    "tap_send_when_ready",
    "assert_sent",
]
