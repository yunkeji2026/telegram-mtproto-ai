"""ADB 同步封装：dump、拉起前台、输入与点击。"""

from __future__ import annotations

import base64
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


@dataclass
class AdbResult:
    stdout: str
    stderr: str
    returncode: int


def adb_stderr_looks_transient(msg: str) -> bool:
    """ADB 常见「瞬断」错误：适合 wait-for-device / 重试，而非直接判死。"""
    s = (msg or "").lower()
    return any(
        k in s
        for k in (
            "not found",
            "closed",
            "timeout",
            "no devices",
            "offline",
            "unauthorized",
        )
    )


def run_adb_binary(
    args: Sequence[str],
    *,
    serial: Optional[str],
    timeout: float = 60.0,
) -> Tuple[bytes, str, int]:
    """
    执行 adb 子进程并返回原始字节 stdout（用于 exec-out screencap 等二进制流）。
    勿对 PNG 使用 text=True 的 run_adb，否则会损坏图像头。
    """
    cmd: List[str] = ["adb"]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
        )
        out = p.stdout if isinstance(p.stdout, (bytes, bytearray)) else bytes(p.stdout or b"")
        err_b = p.stderr if isinstance(p.stderr, (bytes, bytearray)) else bytes(p.stderr or b"")
        err = err_b.decode("utf-8", errors="replace")
        return bytes(out), err, p.returncode
    except subprocess.TimeoutExpired as e:
        return b"", str(e), 124
    except FileNotFoundError:
        return b"", "adb not found in PATH", 127


def run_adb(
    args: Sequence[str],
    *,
    serial: Optional[str],
    timeout: float = 60.0,
) -> AdbResult:
    cmd: List[str] = ["adb"]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return AdbResult(p.stdout or "", p.stderr or "", p.returncode)
    except subprocess.TimeoutExpired as e:
        return AdbResult("", str(e), 124)
    except FileNotFoundError:
        return AdbResult("", "adb not found in PATH", 127)


def list_device_serials() -> List[str]:
    r = run_adb(["devices"], serial=None, timeout=15.0)
    out: List[str] = []
    for ln in (r.stdout or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("List of devices"):
            continue
        parts = ln.split()
        if len(parts) >= 2 and parts[1] == "device":
            out.append(parts[0])
    return out


def list_adb_device_rows() -> List[Tuple[str, str]]:
    """``adb devices`` 原始行，返回 ``(serial, state)``。

    state 常见值：``device`` / ``unauthorized`` / ``offline``；无设备时返回空表。
    """
    r = run_adb(["devices"], serial=None, timeout=15.0)
    out: List[Tuple[str, str]] = []
    for ln in (r.stdout or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("List of devices"):
            continue
        parts = ln.split()
        if len(parts) >= 2:
            out.append((parts[0], parts[1]))
    return out


def pick_serial(preferred: str, *, prefer_line_installed: bool, line_pkg: str) -> Optional[str]:
    """优先已安装 LINE 的 device；否则第一个非模拟器常用前缀。"""
    serials = list_device_serials()
    if preferred and preferred in serials:
        return preferred

    def has_line(sid: str) -> bool:
        rr = run_adb(["shell", f"pm path {line_pkg}"], serial=sid, timeout=20.0)
        return rr.returncode == 0 and line_pkg in (rr.stdout or "")

    if prefer_line_installed:
        for sid in serials:
            if sid.startswith("127.0.0.1") or sid.startswith("emulator-"):
                continue
            if has_line(sid):
                return sid
        for sid in serials:
            if has_line(sid):
                return sid

    for sid in serials:
        if sid.startswith("127.0.0.1") or sid.startswith("emulator-"):
            continue
        return sid
    return serials[0] if serials else None


def ensure_line_foreground(serial: str, line_pkg: str, splash_activity: str) -> AdbResult:
    """合并为单条 shell，减少部分设备上 adb shell 会话过早断开。"""
    script = (
        f"am start -n {line_pkg}/{splash_activity} >/dev/null 2>&1; "
        f"echo OK"
    )
    return run_adb(["shell", script], serial=serial, timeout=30.0)


def uiautomator_dump(serial: str, remote_path: str) -> AdbResult:
    run_adb(["shell", f"rm -f {remote_path}"], serial=serial, timeout=15.0)
    r = run_adb(
        ["shell", f"uiautomator dump {remote_path}"],
        serial=serial,
        timeout=45.0,
    )
    if r.returncode != 0 and "error" in (r.stderr or "").lower():
        logger.warning("uiautomator dump: %s", r.stderr[:300])
    return r


def cat_remote_file(serial: str, remote_path: str) -> AdbResult:
    return run_adb(["exec-out", "cat", remote_path], serial=serial, timeout=30.0)


def dump_ui_hierarchy_xml(serial: str, remote_path: str) -> AdbResult:
    """
    单条 shell 内 dump + cat，避免部分设备上分两次 adb 失败；
    输出为 XML 字符串（stdout）。
    """
    script = f"uiautomator dump {remote_path} 2>/dev/null; cat {remote_path} 2>/dev/null"
    return run_adb(["shell", script], serial=serial, timeout=60.0)


def dump_ui_hierarchy_xml_as_root(serial: str, remote_path: str) -> AdbResult:
    """部分 ROM 会 kill uiautomator；root 下重试。"""
    script = (
        f"su -c 'uiautomator dump {remote_path}' 2>/dev/null; "
        f"su -c 'cat {remote_path}' 2>/dev/null"
    )
    return run_adb(["shell", script], serial=serial, timeout=90.0)


def input_tap(serial: str, x: int, y: int) -> AdbResult:
    return run_adb(["shell", "input", "tap", str(x), str(y)], serial=serial, timeout=15.0)


def input_swipe(
    serial: str,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    duration_ms: int = 380,
) -> AdbResult:
    """通用 swipe：从 (x1,y1) 划到 (x2,y2)，duration_ms 控制滑动时长。"""
    return run_adb(
        [
            "shell", "input", "swipe",
            str(int(x1)), str(int(y1)),
            str(int(x2)), str(int(y2)),
            str(int(max(80, duration_ms))),
        ],
        serial=serial,
        timeout=20.0,
    )


def screen_size(serial: str) -> Optional[Tuple[int, int]]:
    """读取屏幕分辨率；返回 (w, h) 或 None。"""
    r = run_adb(["shell", "wm", "size"], serial=serial, timeout=10.0)
    m = re.search(r"Physical size:\s*(\d+)x(\d+)", r.stdout or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    m2 = re.search(r"Override size:\s*(\d+)x(\d+)", r.stdout or "")
    if m2:
        return int(m2.group(1)), int(m2.group(2))
    return None


def input_keyevent(serial: str, keycode: str) -> AdbResult:
    return run_adb(["shell", "input", "keyevent", keycode], serial=serial, timeout=15.0)


def _escape_input_text_ascii(s: str) -> str:
    """adb shell input text：空格用 %s，部分符号需转义。"""
    s = s.replace("\\", "\\\\").replace("%", "\\%")
    s = s.replace(" ", "%s")
    return s


def input_text_ascii(serial: str, text: str) -> AdbResult:
    """仅适合 ASCII；中文需 ADB Keyboard。"""
    esc = _escape_input_text_ascii(text)
    return run_adb(["shell", "input", "text", esc], serial=serial, timeout=30.0)


def ime_set_adb_keyboard(serial: str, ime_component: str) -> AdbResult:
    """ime_component 例: com.android.adbkeyboard/.AdbIME"""
    run_adb(["shell", "ime", "enable", ime_component], serial=serial, timeout=15.0)
    return run_adb(["shell", "ime", "set", ime_component], serial=serial, timeout=15.0)


def adb_keyboard_input_text(
    serial: str,
    text: str,
    *,
    use_base64: bool,
    package: Optional[str] = None,
) -> AdbResult:
    """
    依赖第三方 ADB Keyboard（常见包名 com.android.adbkeyboard）。
    优先 ADB_INPUT_B64（UTF-8），否则 ADB_INPUT_TEXT。
    指定 package 时增加 -p，部分 ROM 上更可靠。
    """
    def _broadcast(action: str, msg: str) -> AdbResult:
        args: List[str] = ["shell", "am", "broadcast"]
        if package and package.strip():
            args.extend(["-p", package.strip()])
        args.extend(["-a", action, "--es", "msg", msg])
        return run_adb(args, serial=serial, timeout=30.0)

    if use_base64 or not text.isascii():
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        return _broadcast("ADB_INPUT_B64", b64)
    return _broadcast("ADB_INPUT_TEXT", text)


def is_adbkeyboard_installed(
    serial: str, package: str = "com.android.adbkeyboard"
) -> bool:
    """快速检查 ADB Keyboard 是否安装。"""
    r = run_adb(
        ["shell", "pm", "path", package], serial=serial, timeout=8.0
    )
    return r.returncode == 0 and bool((r.stdout or "").strip())


def ensure_adbkeyboard_installed(
    serial: str,
    *,
    package: str = "com.android.adbkeyboard",
    ime_component: str = "com.android.adbkeyboard/.AdbIME",
    apk_path: Optional[str] = None,
    auto_enable: bool = True,
) -> dict:
    """确保目标设备已装 AdbKeyboard 并且 IME 可用。幂等、允许重复调。

    返回 dict::

        {"installed": bool, "enabled": bool, "path": str, "error": str,
         "steps": [...]}

    - 如果 package 已安装则直接返回 installed=True
    - 否则 `adb install` apk_path（默认 tools/ADBKeyboard.apk）
    - auto_enable=True 时再做一次 `ime enable`（不会主动切换，避免打扰用户）
    """
    import os
    steps: list = []
    info = {
        "installed": False,
        "enabled": False,
        "path": "",
        "error": "",
        "steps": steps,
    }

    if is_adbkeyboard_installed(serial, package=package):
        info["installed"] = True
        steps.append("already_installed")
    else:
        if not apk_path:
            here = os.path.dirname(os.path.abspath(__file__))
            workspace = os.path.abspath(os.path.join(here, "..", "..", ".."))
            candidate = os.path.join(workspace, "tools", "ADBKeyboard.apk")
            apk_path = candidate
        if not apk_path or not os.path.exists(apk_path):
            info["error"] = f"apk_not_found:{apk_path}"
            steps.append("apk_missing")
            return info
        steps.append(f"installing_from:{apk_path}")
        r = run_adb(
            ["install", "-r", "-g", apk_path],
            serial=serial, timeout=60.0,
        )
        if r.returncode != 0 or "Success" not in (r.stdout or ""):
            info["error"] = (r.stderr or r.stdout or "install_failed")[:300]
            steps.append("install_failed")
            return info
        steps.append("install_success")
        if not is_adbkeyboard_installed(serial, package=package):
            info["error"] = "installed_but_not_visible"
            steps.append("post_install_missing")
            return info
        info["installed"] = True

    info["path"] = ime_component
    if auto_enable:
        r = run_adb(
            ["shell", "ime", "enable", ime_component],
            serial=serial, timeout=10.0,
        )
        enabled = (r.returncode == 0)
        info["enabled"] = enabled
        steps.append(f"ime_enable:{enabled}")
    return info


def clipboard_paste(serial: str, text: str) -> AdbResult:
    """通过 setprimaryclip + KEYCODE_PASTE 注入任意 Unicode 文本（含中文/emoji）。

    工作原理：
        1. `cmd statusbar collapse` 关掉通知栏（避免被覆盖）
        2. `cmd clipboard set-primary-clip` 写入剪贴板（UTF-8 base64 编码避免 shell 转义）
        3. `input keyevent KEYCODE_PASTE` 在当前焦点输入框粘贴

    兼容性：
        - Android 7+ 全部支持 cmd clipboard
        - MIUI/HyperOS 默认禁止后台 app 读取剪贴板，但 PASTE 是用户手势 → 不受限
        - 部分设备需要"无障碍服务"权限读取剪贴板，但 PASTE 路径不需要

    优势 vs ADB Keyboard：
        - 零安装依赖
        - 不需要切换 IME（不会破坏用户 IME 偏好）
        - 单次粘贴 = 完整文本，不会被自动补全/纠正打断

    限制：
        - 必须先 tap 输入框唤起键盘 + 焦点
        - 不能保证粘贴后光标位置（默认追加到末尾）
        - 部分 ROM 的 cmd clipboard 需要 shell uid（adb shell 默认有）

    返回：最后一步 KEYCODE_PASTE 的 AdbResult。
    """
    import shlex
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    # 用 base64 + base64 -d 管道写入剪贴板，避免 shell 转义中文/emoji
    cmd_str = (
        f"echo {shlex.quote(b64)} | base64 -d | "
        f"cmd clipboard set-primary-clip --user 0 --source clipboard"
    )
    r1 = run_adb(["shell", cmd_str], serial=serial, timeout=10.0)
    if r1.returncode != 0:
        # 备选：写入 SD 文件再读
        try:
            tmp = f"/sdcard/_rpa_clip_{int(time.time())}.txt"
            run_adb(
                ["shell", f"echo {shlex.quote(b64)} | base64 -d > {tmp}"],
                serial=serial, timeout=10.0,
            )
            r1b = run_adb(
                ["shell", f"cat {tmp} | cmd clipboard set-primary-clip --user 0"],
                serial=serial, timeout=10.0,
            )
            run_adb(["shell", f"rm -f {tmp}"], serial=serial, timeout=5.0)
            if r1b.returncode != 0:
                return r1b
        except Exception:
            return r1
    # 触发粘贴
    return run_adb(
        ["shell", "input", "keyevent", "KEYCODE_PASTE"],
        serial=serial, timeout=10.0,
    )


def shlex_single_quote(s: str) -> str:
    """Android shell 单引号包裹。"""
    return "'" + s.replace("'", "'\\''") + "'"


def wait_for_adb_keyboard_ready(serial: str, timeout_sec: float = 3.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        r = run_adb(["shell", "settings", "get", "secure", "default_input_method"], serial=serial)
        out = (r.stdout or "").strip()
        if "adbkeyboard" in out.lower():
            return
        time.sleep(0.2)


def parse_bounds(bounds: str) -> Optional[Tuple[int, int, int, int]]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
