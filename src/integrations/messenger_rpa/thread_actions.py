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
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.integrations.line_rpa import adb_helpers as adb
from src.integrations.messenger_rpa import ui_scraper as uis

logger = logging.getLogger(__name__)

_DEFAULT_REMOTE = "/sdcard/_ui_scraper.xml"
_XML_HEAD = "<?xml"

# ── dump capability 缓存（per-serial）──────────────────────
# MIUI / 部分 ROM 上 ``uiautomator dump`` 进程被 lowmemkill 是结构性失败、
# 不会自愈。第一次失败重试 N 次都能成功才有意义；连续失败 K 次后我们就把
# 这个 serial 标"dump_dead"，跳过重试（saves 660ms × 后续每次调用）。
#
# **不**永久 dead——TTL 后会再试一次，让换机/重启 adb 后能恢复。
_DUMP_FAIL_THRESHOLD = 2          # 连失 2 次 → 标 dead
_DUMP_DEAD_TTL_SEC = 600          # 10 分钟后再试一次
_dump_fail_count: Dict[str, int] = {}
_dump_dead_until: Dict[str, float] = {}


# ── MemAvailable 预测：dump 启动前先判断会不会被 lowmemkill 杀掉 ─────
# Redmi A 系（3.8GB RAM）+ Messenger 跑久了，MemAvailable 经常掉到
# <800MB；此时 uiautomator 进程一启动就被 OOM kill，dump 退出码 0 但
# 文件根本没生成（实测 100% 复现）。提前读 /proc/meminfo 跳过这次
# dump 调用，直接走 Vision/calibrated fallback —— 省掉一次 6 秒
# adb timeout 等待。
#
# 阈值 800_000 = 800MB：MIUI 13/Redmi 13 实测 MemAvailable=862MB 时
# uiautomator 仍被 kill；500MB-1GB 都经历过 dump 失败。设 800MB 在
# "dump 偶尔成功的概率窗口"边界 —— 高于此阈值时仍尝试，dead-cache
# 兜底失败重试。
_MEM_AVAILABLE_KB_FLOOR = 800_000   # 800MB 以下视为危险
_MEM_CACHE_TTL_SEC = 12             # 缓存 12 秒避免每 cycle 多一次 ADB
_mem_cache: Dict[str, Tuple[float, int]] = {}   # serial -> (expire_ts, kb)


def _read_mem_available_kb(serial: str) -> Optional[int]:
    """读 /proc/meminfo 拿 MemAvailable（kB）。失败返 None。带 12s 缓存。"""
    now = time.time()
    cached = _mem_cache.get(serial)
    if cached and cached[0] > now:
        return cached[1]
    try:
        r = adb.run_adb(
            ["shell", "cat /proc/meminfo"],
            serial=serial, timeout=3.0,
        )
        if r.returncode != 0 or not r.stdout:
            return None
        for line in r.stdout.splitlines():
            if line.startswith("MemAvailable:"):
                # MemAvailable:     905020 kB
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    kb = int(parts[1])
                    _mem_cache[serial] = (now + _MEM_CACHE_TTL_SEC, kb)
                    return kb
        return None
    except Exception:
        return None


def _dump_likely_to_fail(serial: str) -> bool:
    """True 表示当前内存压力下 dump 大概率被 OOM kill，应直接跳过。"""
    kb = _read_mem_available_kb(serial)
    if kb is None:
        return False  # 读不到就别拦，让 dump 自己试
    return kb < _MEM_AVAILABLE_KB_FLOOR


def _dump_is_dead(serial: str) -> bool:
    until = _dump_dead_until.get(serial, 0.0)
    return until > time.time()


def _record_dump_fail(serial: str) -> None:
    n = _dump_fail_count.get(serial, 0) + 1
    _dump_fail_count[serial] = n
    if n >= _DUMP_FAIL_THRESHOLD:
        _dump_dead_until[serial] = time.time() + _DUMP_DEAD_TTL_SEC


def _record_dump_ok(serial: str) -> None:
    _dump_fail_count.pop(serial, None)
    _dump_dead_until.pop(serial, None)


def _reset_dump_capability_cache() -> None:
    """测试用——重置全局缓存。"""
    _dump_fail_count.clear()
    _dump_dead_until.clear()
    _mem_cache.clear()
    _u2_clients.clear()


# ── uiautomator2 持久 service 客户端缓存 ─────────────────────
# 仓库已装 uiautomator2 3.5.0 但之前完全没用——还在 `adb shell uiautomator dump`
# 反复 spawn 短命进程，被 MIUI lowmemkill 100% 杀掉。uiautomator2 在设备
# 跑常驻 atx-agent + UiAutomator2 service，HTTP RPC 调用毫秒级响应，不
# 重 spawn 进程，OOM kill 不到。实测 Redmi 13: connect 214ms / dump 553ms /
# 59KB XML，对比 adb shell 100% 失败。
_u2_clients: Dict[str, Any] = {}


def _get_u2_client(serial: str):
    """Lazy-init + 缓存 uiautomator2 device 句柄（per-serial）。"""
    cli = _u2_clients.get(serial)
    if cli is not None:
        return cli
    try:
        import uiautomator2 as u2
        cli = u2.connect(serial)
        _u2_clients[serial] = cli
        return cli
    except Exception as e:
        logger.warning("[thread_actions] uiautomator2 connect 失败 %s: %s", serial, e)
        return None


def _dump_via_u2(serial: str, *, timeout_s: float = 6.0) -> Optional[str]:
    """通过 uiautomator2 持久 service 拿当前页 XML。失败返 None。"""
    cli = _get_u2_client(serial)
    if cli is None:
        return None
    try:
        # u2 的 dump_hierarchy 内部走 HTTP，参数 timeout 自带；这里顶层包
        # 一层 wall-clock 容错。
        xml = cli.dump_hierarchy()
        if xml and _XML_HEAD in xml[:200]:
            return xml
        return None
    except Exception as e:
        logger.debug("[thread_actions] u2.dump_hierarchy 失败: %s", e)
        # 句柄可能已坏（设备重连等），下次重 connect
        _u2_clients.pop(serial, None)
        return None


# ── 基础：dump 当前页 view tree ────────────────────────────

def dump_view_tree(
    serial: str,
    *,
    remote_path: str = _DEFAULT_REMOTE,
    dump_timeout: float = 20.0,
    cat_timeout: float = 10.0,
    cleanup: bool = True,
    bypass_dead_cache: bool = False,
) -> Optional[str]:
    """触发一次 uiautomator dump 并把 XML 拿回来。

    失败返回 ``None``。耗时典型 150-400ms（u2 path）/ 600-2000ms（legacy fallback）。

    若该 serial 已被标 ``dump_dead``（连失 ≥2 次，TTL 内）→ 直接返 None。

    优先级：
      1. uiautomator2 持久 service（HTTP RPC，几百 ms，对 lowmemkill 免疫）
      2. legacy `adb shell uiautomator dump`（短命进程，MIUI lowmemkill 易杀）
    """
    if not bypass_dead_cache and _dump_is_dead(serial):
        return None
    t0 = time.time()
    # ── 优先 uiautomator2（持久 service，不被 OOM kill）─────
    u2_xml = _dump_via_u2(serial)
    if u2_xml:
        _record_dump_ok(serial)
        logger.debug(
            "[thread_actions] dump via u2 ok chars=%d dt=%.0fms",
            len(u2_xml), (time.time() - t0) * 1000,
        )
        return u2_xml
    # ── Legacy fallback：`adb shell uiautomator dump` ─────
    # 内存压力守卫只对 legacy path 适用——u2 path 已经免疫
    if not bypass_dead_cache and _dump_likely_to_fail(serial):
        logger.debug(
            "[thread_actions] skip legacy dump: MemAvailable<%dkB",
            _MEM_AVAILABLE_KB_FLOOR,
        )
        return None
    r1 = adb.run_adb(
        ["shell", f"uiautomator dump {remote_path}"],
        serial=serial, timeout=dump_timeout,
    )
    if r1.returncode != 0:
        # MIUI 的 ThemeCompatibility 堆栈是走 stderr 的 warning，但 rc=0；
        # 只有在 rc!=0 时才当作失败。
        # 但部分 ROM（如 Samsung）uiautomator 进程在写完 XML 后被 OOM Kill，
        # 导致 rc!=0 但文件已完整写入——这时不应直接放弃，继续 cat 尝试。
        logger.debug(
            "[thread_actions] dump rc=%d stderr=%r; will still try cat",
            r1.returncode, (r1.stderr or "")[:120],
        )
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
        # 部分设备（如 Samsung）uiautomator 先写 /sdcard/window_dump.xml 再复制
        # 到指定路径；被 OOM Kill 后自定义路径不存在，但默认路径已写好。
        _fallback = "/sdcard/window_dump.xml"
        if remote_path != _fallback:
            r_fb = adb.run_adb(
                ["shell", f"cat {_fallback}"],
                serial=serial, timeout=cat_timeout,
            )
            fb_out = r_fb.stdout or ""
            fb_pos = fb_out.find(_XML_HEAD)
            if fb_pos >= 0:
                fb_out = fb_out[fb_pos:]
            if _XML_HEAD in (fb_out[:200] if fb_out else ""):
                logger.debug(
                    "[thread_actions] dump fallback ok chars=%d dt=%.0fms",
                    len(fb_out), (time.time() - t0) * 1000,
                )
                _record_dump_ok(serial)
                return fb_out
        logger.debug(
            "[thread_actions] cat xml failed rc=%d head=%r",
            r2.returncode, (out or "")[:80],
        )
        _record_dump_fail(serial)
        return None
    if cleanup:
        # best-effort；失败不影响调用方
        adb.run_adb(
            ["shell", f"rm -f {remote_path}"],
            serial=serial, timeout=5.0,
        )
    _record_dump_ok(serial)
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
    # 目标全名出现在行名内——要求词边界：匹配后下一字符不能是字母/数字
    # （避免 "victor zan" 错误命中 "victor zanches"）
    if nb in na:
        idx = na.index(nb)
        end = idx + len(nb)
        if end >= len(na) or not na[end].isalnum():
            return True
    if na in nb and nb.startswith(na) and (len(nb) - len(na) <= 3):
        return True
    return False


def verify_thread_title(
    serial: str,
    expected_peer: str,
    *,
    allow_substr: bool = True,
    vision_cfg: Optional[Dict[str, Any]] = None,
    global_vision_cfg: Optional[Dict[str, Any]] = None,
    use_recent_cache: bool = True,
    recent_cache_ttl_sec: float = 300.0,
) -> VerifyResult:
    """U1：当前是否处于"和 ``expected_peer`` 的会话"页。

    判定：
      1. 不在 Thread 页（无顶栏 title）→ ``ok=False reason=not_in_thread``
      2. 顶栏 title 命中 ``expected_peer`` → ``ok=True``
      3. 不命中但 ``allow_substr=True`` 且一方是另一方的子串（中文空格
         差、昵称后缀等）→ ``ok=True`` + reason 注明
      4. 完全不匹配 → ``ok=False`` + actual 字段

    Vision 兜底（``vision_cfg`` 提供时）：``uiautomator dump`` 在 MIUI
    一类 ROM 会被静默 OOM kill，导致永远拿不到 XML。此时退到 GLM-4V 读
    顶栏 peer 名走同一套匹配；reason 会带 ``_via_vision`` 后缀以便审计。
    """
    expected = (expected_peer or "").strip()
    if not expected:
        return VerifyResult(ok=False, reason="empty_expected")

    # ★ Recent verify cache：跨 send 命中即返。同 chat 60s 内重发跳过 vision。
    # 风险窗口由 TTL 限定 + send 心跳续期。
    if use_recent_cache:
        try:
            from src.integrations.messenger_rpa import recent_verify_cache as _rvc
            if _rvc.is_recently_verified(
                serial, expected, ttl_sec=recent_cache_ttl_sec,
            ):
                return VerifyResult(
                    ok=True, actual=expected, expected=expected,
                    reason="recent_cache_hit",
                )
        except Exception:
            logger.debug("[thread_actions] recent_verify_cache 查询异常", exc_info=True)

    # dump_view_tree 自身管 dump-dead 缓存：dead 时立即返 None（不调 adb），
    # ok 时重置计数器；这里只做 ≤3 次的瞬态重试，耗时由缓存自动收敛。
    xml: Optional[str] = None
    for _attempt in range(3):
        xml = dump_view_tree(serial)
        if xml is not None:
            break
        # dead 缓存命中后下一次也会立即返 None，没必要再 sleep
        if _dump_is_dead(serial):
            break
        time.sleep(0.22)

    title: Optional[str] = None
    title_source = "xml"
    _xml_non_messenger = False  # True when xml is non-None but not Messenger content
    if xml is not None:
        title = uis.find_thread_title(xml)
        if title is None:
            # ★ Guard: distinguish genuine "not in thread" (Messenger XML, no title)
            # from notification-shade XML returned by MIUI uiautomator2 bug.
            # Notification-shade XML never contains "com.facebook.orca".
            _xml_str = xml if isinstance(xml, str) else (
                xml.decode("utf-8", "replace") if isinstance(xml, bytes) else str(xml)
            )
            if "com.facebook.orca" in _xml_str:
                return VerifyResult(
                    ok=False, reason="not_in_thread", expected=expected,
                )
            # Non-Messenger XML (notification shade / system UI garbage) → Vision
            _xml_non_messenger = True
            logger.debug(
                "[thread_actions] xml has no Messenger package (notification shade?) "
                "serial=%s → Vision fallback", serial,
            )
    elif vision_cfg:
        try:
            from src.integrations.messenger_rpa.thread_title_vision import (
                read_thread_title_via_vision,
            )
            vr = read_thread_title_via_vision(
                serial, vision_cfg, global_vision_cfg,
            )
            title = vr.title
            title_source = f"vision({vr.debug})"
            if title is None:
                return VerifyResult(
                    ok=False,
                    reason=f"dump_failed_vision_{vr.debug}",
                    expected=expected,
                )
        except Exception as e:
            logger.debug(
                "[thread_actions] vision fallback 异常 %s", e, exc_info=True,
            )
            return VerifyResult(
                ok=False,
                reason=f"dump_failed_vision_exc:{type(e).__name__}",
                expected=expected,
            )
    else:
        return VerifyResult(
            ok=False, reason="dump_failed", expected=expected,
        )

    # ★ Non-Messenger XML fallback: xml was present but is notification-shade
    # garbage (MIUI uiautomator2 bug) → Vision is the only reliable source.
    if _xml_non_messenger and title is None:
        if vision_cfg:
            try:
                from src.integrations.messenger_rpa.thread_title_vision import (
                    read_thread_title_via_vision,
                )
                vr = read_thread_title_via_vision(serial, vision_cfg, global_vision_cfg)
                title = vr.title
                title_source = f"vision_xml_garbage({vr.debug})"
                if title is None:
                    return VerifyResult(
                        ok=False,
                        reason=f"xml_garbage_vision_{vr.debug}",
                        expected=expected,
                    )
            except Exception as e:
                logger.debug(
                    "[thread_actions] Vision (xml_garbage) fallback exc %s", e,
                    exc_info=True,
                )
                return VerifyResult(
                    ok=False,
                    reason=f"xml_garbage_vision_exc:{type(e).__name__}",
                    expected=expected,
                )
        else:
            return VerifyResult(
                ok=False, reason="not_in_thread_xml_garbage", expected=expected,
            )

    matched = (
        peer_names_match_inbox_pick(title, expected)
        if allow_substr
        else peer_names_match(title, expected, allow_substr=False)
    )
    if matched:
        nt = _normalize_peer_name(title)
        ne = _normalize_peer_name(expected)
        base = "exact" if nt == ne else "substr"
        reason = base if title_source == "xml" else f"{base}_via_vision"
        # 写 cache 让后续 send 享受 fast-path。mismatch 路径不写——实际就是
        # "现在不在该 chat"的强信号，写了反而误导。
        if use_recent_cache:
            try:
                from src.integrations.messenger_rpa import recent_verify_cache as _rvc
                _rvc.mark_verified(serial, expected)
            except Exception:
                logger.debug("[thread_actions] recent_verify_cache 写异常", exc_info=True)
        return VerifyResult(
            ok=True, actual=title, expected=expected, reason=reason,
        )
    return VerifyResult(
        ok=False, actual=title, expected=expected,
        reason="mismatch" if title_source == "xml" else "mismatch_via_vision",
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


def clear_focused_input(serial: str, *, adb_keyboard_package: str = "com.android.adbkeyboard") -> bool:
    """Clear the currently focused input field, preferring ADB Keyboard's own API."""
    package = (adb_keyboard_package or "com.android.adbkeyboard").strip()
    try:
        args = ["shell", "am", "broadcast"]
        if package:
            args.extend(["-p", package])
        args.extend(["-a", "ADB_CLEAR_TEXT"])
        r = adb.run_adb(args, serial=serial, timeout=5.0)
        if r.returncode == 0:
            return True
    except Exception:
        logger.debug("[thread_actions] ADB_CLEAR_TEXT failed", exc_info=True)

    try:
        adb.run_adb(
            ["shell", "input", "keyevent", "KEYCODE_CTRL_A"],
            serial=serial, timeout=3.0,
        )
        time.sleep(0.15)
        adb.run_adb(
            ["shell", "input", "keyevent", "KEYCODE_DEL"],
            serial=serial, timeout=3.0,
        )
        return True
    except Exception:
        logger.debug("[thread_actions] fallback input clear failed", exc_info=True)
        return False


async def inject_and_verify(
    serial: str,
    text: str,
    *,
    inject_cfg: Optional[Dict[str, Any]] = None,
    screen_h: int = 1600,
    settle_sec: float = 1.5,
    tolerate_truncation_chars: int = 5,
    max_retries: int = 2,
    vision_cfg: Optional[Dict[str, Any]] = None,
    global_vision_cfg: Optional[Dict[str, Any]] = None,
) -> InjectVerifyResult:
    """注入 ``text`` 到当前输入框 → 等 ``settle_sec`` → 读回真实文本对比。

    校验源（按优先级）：
      1. ``uiautomator dump`` → ``find_input_box`` → ``EditText.text``（首选）
      2. ``vision_cfg`` 提供时：截屏底栏 → GLM-4V 读输入框文本（**dump-dead
         设备的关键安全网**）
      3. 都失败 → ``ok=True reason='no_verify_*'``（兜底放行；曾经的"安全
         默认"）

    历史 bug（2026-04 修）：原实现在 dump 失败时 ``continue`` 让外层 for 再
    跑一次 ``inject_text``——而**没清空已注入的字**——导致
    ``inject('hi')→dump fail→inject('hi')→...`` 实际发出 ``"hihihi"``。
    现 dump 失败 → vision 兜底 / 直接 break，**永不重复注入**。

    重试语义：仅在 mismatch（dump/vision 都返回了真实 actual 但和 expected
    不符）才重试——清空 + 重注入。dump_failed 不重试（无法验证清空成功，
    重试一定双重注入）。
    """
    from src.integrations.messenger_rpa.text_input import inject_text

    expected = (text or "")[:1500]
    if not expected:
        return InjectVerifyResult(
            ok=False, reason="empty_text", expected_text="",
        )
    cfg = dict(inject_cfg or {})

    # 首次注入前先清空输入框（防止搜索/旧 draft 残留文字混入）。
    clear_focused_input(
        serial,
        adb_keyboard_package=str(
            cfg.get("adb_keyboard_package") or "com.android.adbkeyboard"
        ),
    )
    time.sleep(0.10)

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

        # ── 校验：优先 XML；XML 读空/失效时退到 vision，再不行才放行 ──
        actual: Optional[str] = None
        verify_source = ""
        xml = dump_view_tree(serial)
        if xml is not None:
            ib = uis.find_input_box(xml, screen_h=screen_h)
            actual = (ib.text if ib and not ib.is_hint else "") or ""
            verify_source = "xml"
        if (xml is None or actual == "") and vision_cfg:
            try:
                from src.integrations.messenger_rpa.input_text_vision import (
                    read_input_text_via_vision,
                )
                vr = read_input_text_via_vision(
                    serial, vision_cfg, global_vision_cfg,
                )
                if vr.text is not None:
                    actual = vr.text
                    verify_source = f"vision({vr.debug})"
                else:
                    # vision 也读不出 → 兜底放行（不重试避免双重注入）。
                    # ADB Keyboard 的 broadcast 返回成功但 Messenger XML 经常读空；
                    # 失败时外层会清草稿，因此这里比盲目清空重试更稳。
                    return InjectVerifyResult(
                        ok=True, injected_via=ir.path,
                        actual_text="", expected_text=expected,
                        reason=f"no_verify_vision_{vr.debug}",
                        tries=tries,
                    )
            except Exception as e:
                logger.debug(
                    "[thread_actions] vision input-text fallback 异常 %s",
                    e, exc_info=True,
                )
                return InjectVerifyResult(
                    ok=True, injected_via=ir.path,
                    actual_text="", expected_text=expected,
                    reason=f"no_verify_vision_exc:{type(e).__name__}",
                    tries=tries,
                )
        elif xml is None:
            # 完全没法验证——保留旧"信任注入"行为，但**绝不重试**
            return InjectVerifyResult(
                ok=True, injected_via=ir.path,
                actual_text="", expected_text=expected,
                reason="no_verify_dump_failed",
                tries=tries,
            )

        # 命中判定：actual == expected，或 expected 是 actual 的前缀
        # （IME 偶尔把末尾 N 个字符吃掉但前面正确）
        if actual == expected:
            base = "exact"
            return InjectVerifyResult(
                ok=True, injected_via=ir.path,
                actual_text=actual, expected_text=expected,
                reason=base if verify_source == "xml" else f"{base}_via_{verify_source}",
                tries=tries,
            )
        # P-A (2026-05-04): u2 dump 拿到的 EditText.text 把 emoji 渲染成 ".."
        # （expected="...？😊" len=27 vs actual="...？.." len=28）。这是
        # **真注入正确，只是 verify 字段语义降级**。剥掉 emoji + 末尾省略号
        # 后再比较——容忍 u2 的 emoji 字符串替换。
        _emoji_re = re.compile(
            r"[\U0001F000-\U0001FFFF\U00002600-\U000027BF\U00002B00-\U00002BFF]+"
        )
        # u2 dump 把 emoji 替成 ".."（2 个 dot），可能在文本中间或尾部。
        # 把所有连续的 .. 都剥掉，emoji 也剥掉，再 strip 多余空格。
        _dots_re = re.compile(r"\.{2,}")
        def _strip_emoji_and_dots(s: str) -> str:
            s2 = _emoji_re.sub("", s or "")
            s2 = _dots_re.sub("", s2)
            # 多余空格规整（emoji 周围的空格残留）
            return re.sub(r"\s+", " ", s2).strip()
        _exp_norm = _strip_emoji_and_dots(expected)
        _act_norm = _strip_emoji_and_dots(actual or "")
        if _exp_norm and _act_norm and _exp_norm == _act_norm:
            base = "exact_emoji_normalized"
            return InjectVerifyResult(
                ok=True, injected_via=ir.path,
                actual_text=actual, expected_text=expected,
                reason=base if verify_source == "xml" else f"{base}_via_{verify_source}",
                tries=tries,
            )
        min_acceptable = max(0, len(expected) - tolerate_truncation_chars)
        if (
            actual and expected.startswith(actual)
            and len(actual) >= min_acceptable
        ):
            base = f"prefix_ok_delta={len(expected) - len(actual)}"
            return InjectVerifyResult(
                ok=True, injected_via=ir.path,
                actual_text=actual, expected_text=expected,
                reason=base if verify_source == "xml" else f"{base}_via_{verify_source}",
                tries=tries,
            )
        # emoji-normalized prefix 也接受
        _min_acc_norm = max(0, len(_exp_norm) - tolerate_truncation_chars)
        if (
            _act_norm and _exp_norm.startswith(_act_norm)
            and len(_act_norm) >= _min_acc_norm
        ):
            base = f"prefix_ok_emoji_normalized_delta={len(_exp_norm) - len(_act_norm)}"
            return InjectVerifyResult(
                ok=True, injected_via=ir.path,
                actual_text=actual, expected_text=expected,
                reason=base if verify_source == "xml" else f"{base}_via_{verify_source}",
                tries=tries,
            )
        if not actual and ir.path == "adbkeyboard":
            return InjectVerifyResult(
                ok=True, injected_via=ir.path,
                actual_text=actual, expected_text=expected,
                reason=(
                    "trusted_adbkeyboard_empty_verify"
                    if verify_source == "xml"
                    else f"trusted_adbkeyboard_empty_verify_via_{verify_source}"
                ),
                tries=tries,
            )
        # P1-A：ADBKeyboard ok=True + vision 部分识别（emoji / 末尾字符 OCR 不
        # 准导致 mismatch_via_vision(ok)）→ 信任已注入。expected 含 emoji 时
        # vision 看输入框常缺失 emoji，actual 长度通常 >= expected_no_emoji 长度
        # 的 50% 即认为 ADBKeyboard 真发了。避免 send_failed 误报浪费 40-50s 重试。
        if (
            ir.path == "adbkeyboard" and actual
            and verify_source.startswith("vision")
            and len(actual) >= max(2, len(expected) * 0.5)
        ):
            base = f"trusted_adbkeyboard_partial_via_{verify_source}_actual_len={len(actual)}"
            return InjectVerifyResult(
                ok=True, injected_via=ir.path,
                actual_text=actual, expected_text=expected,
                reason=base,
                tries=tries,
            )
        # Mismatch。清空 + 重试（仅在 actual 实测可见时，避免盲目清空双发）
        # 加详细日志便于诊断：actual 实际长度 + 头 60 字符
        logger.warning(
            "[thread_actions] inject_verify mismatch attempt=%d "
            "expected_len=%d actual_len=%d source=%s actual=%r expected=%r",
            attempt + 1, len(expected), len(actual or ""), verify_source,
            (actual or "")[:60], expected[:60],
        )
        if attempt < max_retries:
            clear_focused_input(
                serial,
                adb_keyboard_package=str(
                    cfg.get("adb_keyboard_package") or "com.android.adbkeyboard"
                ),
            )
            await asyncio.sleep(0.3)
            continue
        base = "mismatch"
        return InjectVerifyResult(
            ok=False, injected_via=ir.path,
            actual_text=actual, expected_text=expected,
            reason=base if verify_source == "xml" else f"{base}_via_{verify_source}",
            tries=tries,
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


# ── 安全护盾：检测是否仍在 Messenger ──────────────────────────

def check_in_messenger(serial: str) -> bool:
    """检查当前前台是否还是 Messenger (com.facebook.orca)。

    用于发送前安全护盾：tap 输入框后若误触相机/图库等导致离开 Messenger，
    本函数会快速检测并告警，让调用方可以主动 BACK 恢复。

    判定依据：``dumpsys activity activities`` 中 ``mResumedActivity`` /
    ``topResumedActivity`` 行是否含 ``com.facebook.orca``。

    无法判定时保守返回 True（不误报）。
    """
    try:
        r = adb.run_adb(
            ["shell", "dumpsys", "activity", "activities"],
            serial=serial, timeout=6.0,
        )
        if r.returncode != 0:
            return True
        out = r.stdout or ""
        for line in out.splitlines():
            low = line.lower()
            if "mresumedactivity" not in low and "topresumedactivity" not in low:
                continue
            # 找到了 resumed activity 行
            if "com.facebook.orca" in line:
                return True
            # 有 ActivityRecord 标记说明确实在别的 app
            if "activityrecord{" in low or ("/" in line and "}" in line):
                return False
        # 找不到 resumed activity 行 → 保守认为还在
        return True
    except Exception:
        logger.debug("[thread_actions] check_in_messenger 异常", exc_info=True)
        return True


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
    "check_in_messenger",
]
