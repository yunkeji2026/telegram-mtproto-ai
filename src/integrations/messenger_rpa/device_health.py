"""设备健康守护：自动重连 + 唤醒 + 解锁。

无人值守 RPA 最常见的失败：
1. WiFi/USB 节电休眠 → adb 报 "device not found"
2. 屏幕熄灭 → screencap 黑屏
3. 锁屏 → input tap 在锁屏上
4. ADB daemon 崩了 → 全部命令报 "no devices/emulators found"

本模块统一兜底，外层只关心 ensure_device_ready(serial) 一个函数：

    healthy, info = ensure_device_ready(
        "192.168.0.113:5555",
        try_reconnect=True,       # 设备掉线时 adb connect
        try_wake=True,            # 屏熄时 KEYCODE_WAKEUP
        try_unlock_swipe=True,    # 锁屏时上滑（无密码场景）
        max_attempts=3,
    )

返回 info 字典记录每一次尝试的耗时和结果，方便上层 metric 上报。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

from src.integrations.line_rpa import adb_helpers as adb

logger = logging.getLogger(__name__)


def _is_device_present(serial: str) -> bool:
    serials = adb.list_device_serials()
    return serial in serials


def _is_screen_on(serial: str) -> Optional[bool]:
    """通过 dumpsys power 看屏幕是否亮。返回 None 代表无法判定。"""
    r = adb.run_adb(
        ["shell", "dumpsys", "power"], serial=serial, timeout=8.0
    )
    if r.returncode != 0:
        return None
    out = r.stdout or ""
    # MIUI/HyperOS：mWakefulness=Awake / Asleep
    if "mWakefulness=Awake" in out:
        return True
    if "mWakefulness=Asleep" in out or "mWakefulness=Dozing" in out:
        return False
    # AOSP：Display Power: state=ON / OFF
    if "Display Power: state=ON" in out:
        return True
    if "Display Power: state=OFF" in out:
        return False
    return None


def _is_locked(serial: str) -> Optional[bool]:
    """KeyguardServiceDelegate / mShowingDream / mDreamingLockscreen 综合判断。"""
    r = adb.run_adb(
        ["shell", "dumpsys", "window"], serial=serial, timeout=8.0
    )
    if r.returncode != 0:
        return None
    out = r.stdout or ""
    if "mDreamingLockscreen=true" in out or "isStatusBarKeyguard=true" in out:
        return True
    if "mDreamingLockscreen=false" in out or "isStatusBarKeyguard=false" in out:
        return False
    return None


def _adb_connect(
    serial: str, *, force_disconnect_first: bool = True
) -> Tuple[bool, str]:
    """对网络 ADB（host:port）尝试 adb connect。本地 USB 直接返 True。

    v2：先 `adb disconnect` 甩掉 TCP 半死状态；很多时候 Windows 端 ADB
    daemon 的连接缓存了一个不响应的 socket，单纯 connect 永远报超时，
    显式 disconnect 后再 connect 就活了。
    """
    if ":" not in serial:
        return True, "usb"
    if force_disconnect_first:
        try:
            adb.run_adb(["disconnect", serial], serial=None, timeout=5.0)
        except Exception:
            pass
        time.sleep(0.2)
    r = adb.run_adb(["connect", serial], serial=None, timeout=10.0)
    out = (r.stdout or "") + " " + (r.stderr or "")
    low = out.lower()
    if "connected" in low or "already" in low:
        return True, out.strip()
    logger.warning(
        "[device_health] adb connect %s 失败: rc=%s out=%s",
        serial, r.returncode, out.strip()[:200],
    )
    return False, out.strip()[:200]


def probe_devices(serials: list) -> Dict[str, Dict[str, Any]]:
    """给 Web/运维用的批量探测：返回每台设备是否在线 + 连接状态。
    不触发唤醒/解锁，只做快速 ping，5 秒内返回。

    ``present`` 为真仅当 ``adb devices`` 中该 serial 的 state 为 *device*。
    另返回 ``adb_state``（*device* / *unauthorized* / *offline* / *not_listed*），
    便于区分未授权与完全未连接。
    """
    online = set(adb.list_device_serials())
    row_map = {s: st for s, st in adb.list_adb_device_rows()}
    out: Dict[str, Dict[str, Any]] = {}
    for s in serials:
        st = row_map.get(s)
        present = st == "device"
        info: Dict[str, Any] = {
            "serial": s,
            "present": present,
            "adb_state": st if st is not None else "not_listed",
        }
        if present:
            sr = _is_screen_on(s)
            info["screen_on"] = sr
            info["locked"] = _is_locked(s)
        out[s] = info
    return out


def _wake(serial: str) -> bool:
    r = adb.run_adb(
        ["shell", "input", "keyevent", "KEYCODE_WAKEUP"],
        serial=serial, timeout=5.0,
    )
    return r.returncode == 0


def _unlock_swipe(serial: str) -> bool:
    r = adb.run_adb(
        ["shell", "input", "swipe", "360", "1300", "360", "500", "200"],
        serial=serial, timeout=5.0,
    )
    return r.returncode == 0


# ── P4-2：输入法预检 + 硬重启 ────────────────
def _get_current_ime(serial: str) -> Optional[str]:
    """读 settings secure default_input_method；返回 None 代表无法读。"""
    r = adb.run_adb(
        ["shell", "settings", "get", "secure", "default_input_method"],
        serial=serial, timeout=5.0,
    )
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    if out.lower() in ("null", ""):
        return None
    return out


def _list_installed_imes(serial: str) -> list:
    """列出已启用的 IME（ime list -s），供 fallback 选择。"""
    r = adb.run_adb(
        ["shell", "ime", "list", "-s"], serial=serial, timeout=5.0,
    )
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]


def _set_ime(serial: str, ime_id: str) -> bool:
    """切换默认输入法。需要 IME 已 enabled。"""
    r = adb.run_adb(
        ["shell", "ime", "set", ime_id], serial=serial, timeout=8.0,
    )
    out = (r.stdout or "") + " " + (r.stderr or "")
    return r.returncode == 0 and "selected" in out.lower()


def _hard_restart_adb_server() -> bool:
    """最后兜底：kill-server + start-server。对其他 adb 进程有影响，只在多次
    reconnect 失败后才用。"""
    try:
        adb.run_adb(["kill-server"], serial=None, timeout=8.0)
        time.sleep(1.0)
        r = adb.run_adb(["start-server"], serial=None, timeout=10.0)
        time.sleep(1.5)
        return r.returncode == 0
    except Exception as ex:
        logger.warning("[device_health] hard_restart_adb_server 异常: %s", ex)
        return False


def ensure_device_ready(
    serial: str,
    *,
    try_reconnect: bool = True,
    try_wake: bool = True,
    try_unlock_swipe: bool = True,
    max_attempts: int = 3,
    backoff_sec: float = 2.0,
    preferred_ime: Optional[str] = None,
    hard_restart_on_fail: bool = True,
) -> Tuple[bool, Dict[str, Any]]:
    """阻塞地把 device 弄到 RPA 可用状态。

    依次执行：
      1) 不在线 → adb connect
      2) 屏熄 → KEYCODE_WAKEUP
      3) 锁屏 → 上滑（仅无密码可用，要密码请人工处理）

    成功返回 (True, info)；info 包含每个步骤的成败和耗时。
    """
    info: Dict[str, Any] = {
        "serial": serial,
        "attempts": [],
        "ok": False,
        "total_ms": 0,
    }
    t0 = time.time()

    for attempt in range(1, max_attempts + 1):
        rec: Dict[str, Any] = {"attempt": attempt}
        try:
            present = _is_device_present(serial)
            rec["present_before"] = present
            if not present:
                if not try_reconnect:
                    rec["error"] = "device not present and reconnect disabled"
                    info["attempts"].append(rec)
                    break
                # ★ P4-2：第 >=2 次重连还失败，先 kill-server + start-server
                # （代价大，对其他 adb 进程有影响，但能解决 daemon 僵死）
                if attempt >= 2 and hard_restart_on_fail:
                    hs_ok = _hard_restart_adb_server()
                    rec["hard_restart_adb"] = hs_ok
                    logger.info(
                        "[device_health] %s hard_restart_adb_server=%s", serial, hs_ok,
                    )
                logger.info(
                    "[device_health] %s 不在线，第 %d 次重连…", serial, attempt
                )
                # 第一次 connect：正常连
                # 第二次+：先 disconnect 再 connect，破 TCP 半死状态
                ok, msg = _adb_connect(
                    serial, force_disconnect_first=(attempt >= 2),
                )
                rec["adb_connect"] = ok
                rec["adb_connect_msg"] = msg
                # connect 后通常需要 1-2s 才能 list 到
                time.sleep(1.5 + 0.5 * attempt)
                if not _is_device_present(serial):
                    rec["error"] = (
                        f"still not present after connect: {msg}"
                    )
                    info["attempts"].append(rec)
                    time.sleep(backoff_sec * attempt)
                    continue

            screen_on = _is_screen_on(serial)
            rec["screen_on"] = screen_on
            if screen_on is False and try_wake:
                logger.info("[device_health] %s 屏熄，KEYCODE_WAKEUP", serial)
                rec["wake_ok"] = _wake(serial)
                time.sleep(0.5)

            locked = _is_locked(serial)
            rec["locked"] = locked
            if locked is True and try_unlock_swipe:
                logger.info("[device_health] %s 锁屏，上滑解锁", serial)
                rec["unlock_swipe_ok"] = _unlock_swipe(serial)
                time.sleep(0.6)
                # 重新检查锁屏：仍锁说明是密码锁，需要人工
                locked_after = _is_locked(serial)
                rec["locked_after_swipe"] = locked_after
                if locked_after is True:
                    rec["error"] = "still locked (PIN/password 设备需要人工)"
                    info["attempts"].append(rec)
                    info["ok"] = False
                    info["total_ms"] = int((time.time() - t0) * 1000)
                    return False, info

            # ★ P4-2：IME 预检（最后一步，不阻塞主流程）
            # 只当 preferred_ime 配置了、当前 IME 和它不匹配时才切换
            if preferred_ime:
                current = _get_current_ime(serial)
                rec["ime_current"] = current or ""
                rec["ime_preferred"] = preferred_ime
                if current and current != preferred_ime:
                    installed = _list_installed_imes(serial)
                    if preferred_ime in installed:
                        set_ok = _set_ime(serial, preferred_ime)
                        rec["ime_set"] = set_ok
                        if set_ok:
                            logger.info(
                                "[device_health] %s IME %s → %s",
                                serial, current, preferred_ime,
                            )
                    else:
                        rec["ime_set"] = False
                        rec["ime_warn"] = (
                            f"preferred_ime={preferred_ime} not in enabled list"
                        )

            rec["ok"] = True
            info["attempts"].append(rec)
            info["ok"] = True
            info["total_ms"] = int((time.time() - t0) * 1000)
            return True, info

        except Exception as ex:
            rec["exception"] = f"{type(ex).__name__}: {ex}"
            info["attempts"].append(rec)
            time.sleep(backoff_sec * attempt)

    info["total_ms"] = int((time.time() - t0) * 1000)
    return False, info


__all__ = ["ensure_device_ready", "probe_devices"]
