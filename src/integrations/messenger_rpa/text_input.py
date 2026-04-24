"""文本注入策略与降级链。

按优先级尝试以下注入方式，第一次成功就返回：

    1. ADB Keyboard (com.android.adbkeyboard) — 支持中文/emoji，最稳
    2. clipboard set-primary-clip + KEYCODE_PASTE — 全 Android 支持，但 MIUI 阉割
    3. input text + Unicode 转义 — 仅 ASCII，中文会乱码
    4. 拒绝发送（保护：avoid 发出乱码）

每条路径都是幂等的；外层 RPA 只调用 inject_text() 即可。
"""
from __future__ import annotations

import base64
import logging
import shlex
import time
from dataclasses import dataclass
from typing import Optional

from src.integrations.line_rpa import adb_helpers as adb

logger = logging.getLogger(__name__)


@dataclass
class InjectResult:
    ok: bool
    path: str = ""  # adbkeyboard | clipboard_paste | input_text_ascii | rejected
    error: str = ""


def _has_non_ascii(text: str) -> bool:
    return not text.isascii()


def _inject_text_once(
    serial: str,
    text: str,
    *,
    use_adb_keyboard: bool,
    adb_keyboard_ime: str,
    adb_keyboard_package: str,
    allow_clipboard_fallback: bool,
    allow_input_text_fallback_for_ascii: bool,
) -> InjectResult:
    """单次尝试注入（无设备重试）。"""
    has_unicode = _has_non_ascii(text)

    if use_adb_keyboard and adb_keyboard_ime:
        installed = adb.is_adbkeyboard_installed(
            serial, package=adb_keyboard_package
        )
        if installed:
            try:
                adb.ime_set_adb_keyboard(serial, adb_keyboard_ime)
                adb.wait_for_adb_keyboard_ready(serial, timeout_sec=2.0)
                r = adb.adb_keyboard_input_text(
                    serial,
                    text,
                    use_base64=True,
                    package=adb_keyboard_package,
                )
                if r.returncode == 0:
                    return InjectResult(ok=True, path="adbkeyboard")
                logger.warning(
                    "[text_input] adbkeyboard 失败 rc=%d stderr=%r",
                    r.returncode, (r.stderr or "")[:120],
                )
            except Exception as ex:
                logger.warning("[text_input] adbkeyboard 异常: %s", ex)
        else:
            logger.info(
                "[text_input] %s 未安装 ADB Keyboard，进入 fallback 链",
                serial,
            )

    if allow_clipboard_fallback:
        try:
            r = adb.clipboard_paste(serial, text)
            if r.returncode == 0:
                return InjectResult(ok=True, path="clipboard_paste")
            err_tail = ((r.stderr or "") + (r.stdout or ""))[:240]
            logger.info(
                "[text_input] clipboard_paste rc=%d stderr=%r",
                r.returncode, err_tail[:120],
            )
            if adb.adb_stderr_looks_transient(err_tail):
                return InjectResult(
                    ok=False, path="clipboard_paste", error=err_tail,
                )
        except Exception as ex:
            es = f"{type(ex).__name__}: {ex}"
            logger.info("[text_input] clipboard_paste 异常: %s", es)
            if adb.adb_stderr_looks_transient(es):
                return InjectResult(ok=False, path="clipboard_paste", error=es)

    if not has_unicode and allow_input_text_fallback_for_ascii:
        try:
            r = adb.input_text_ascii(serial, text)
            if r.returncode == 0:
                return InjectResult(ok=True, path="input_text_ascii")
            err_tail = ((r.stderr or "") + (r.stdout or ""))[:240]
            return InjectResult(
                ok=False, path="input_text_ascii",
                error=f"rc={r.returncode} stderr={err_tail[:120]}",
            )
        except Exception as ex:
            return InjectResult(
                ok=False, path="input_text_ascii",
                error=f"{type(ex).__name__}: {ex}",
            )

    return InjectResult(
        ok=False,
        path="rejected",
        error=(
            "non_ascii reply but neither ADB Keyboard nor clipboard works on "
            "this device — install 'tools/ADBKeyboard.apk' (开启 USB 安装) 或 "
            "在设备 设置→开发者选项 启用 USB 调试（安全设置）"
        ),
    )


def inject_text(
    serial: str,
    text: str,
    *,
    use_adb_keyboard: bool = True,
    adb_keyboard_ime: str = "com.android.adbkeyboard/.AdbIME",
    adb_keyboard_package: str = "com.android.adbkeyboard",
    allow_clipboard_fallback: bool = True,
    allow_input_text_fallback_for_ascii: bool = True,
    device_transient_retries: int = 4,
) -> InjectResult:
    """注入文本到当前焦点输入框。**调用前必须已经 tap 输入框 + 键盘已弹**。

    返回 InjectResult，其中 path 标记哪条路径成功；ok=False 时 error 说明原因。

    ``device_transient_retries``：ADB 偶发 ``device not found`` 时，在两次尝试之间
    ``wait-for-device`` 并重试整条注入链（对 MIUI USB 抖动有效）。
    """
    text = text or ""
    if not text:
        return InjectResult(ok=False, path="empty", error="empty text")

    n = max(1, min(8, int(device_transient_retries or 4)))
    last = InjectResult(ok=False, path="", error="")
    for attempt in range(n):
        last = _inject_text_once(
            serial,
            text,
            use_adb_keyboard=use_adb_keyboard,
            adb_keyboard_ime=adb_keyboard_ime,
            adb_keyboard_package=adb_keyboard_package,
            allow_clipboard_fallback=allow_clipboard_fallback,
            allow_input_text_fallback_for_ascii=allow_input_text_fallback_for_ascii,
        )
        if last.ok:
            if attempt > 0:
                logger.info(
                    "[text_input] 注入成功 path=%s attempt=%d/%d",
                    last.path, attempt + 1, n,
                )
            return last
        if last.path in ("rejected", "empty"):
            return last
        if attempt < n - 1 and adb.adb_stderr_looks_transient(last.error):
            delay = 0.18 + 0.28 * attempt
            time.sleep(delay)
            wfd = adb.run_adb(
                ["wait-for-device"], serial=serial, timeout=20.0,
            )
            if wfd.returncode != 0:
                logger.debug(
                    "[text_input] wait-for-device rc=%s err=%r",
                    wfd.returncode, (wfd.stderr or "")[:100],
                )
            continue
        return last
    return last


def precheck_text_input(
    serial: str,
    *,
    adb_keyboard_package: str = "com.android.adbkeyboard",
    auto_install_adbkeyboard: bool = True,
    adb_keyboard_ime: str = "com.android.adbkeyboard/.AdbIME",
) -> dict:
    """启动时跑一次健康检查，告知运维这台设备能用哪些 path。

    v2：auto_install_adbkeyboard=True 时（默认），若检测到 AdbKeyboard 未装、
    且 `tools/ADBKeyboard.apk` 存在，就**自动安装**——彻底解决"设备只能
    发 ASCII"的问题。安装失败不会抛异常，只在 info 里附 auto_install 字段。
    """
    info: dict = {"serial": serial}

    info["adbkeyboard_installed"] = adb.is_adbkeyboard_installed(
        serial, package=adb_keyboard_package
    )

    # ★ 自动修复分支：未装 → 尝试 adb install
    if auto_install_adbkeyboard and not info["adbkeyboard_installed"]:
        try:
            auto = adb.ensure_adbkeyboard_installed(
                serial,
                package=adb_keyboard_package,
                ime_component=adb_keyboard_ime,
                auto_enable=True,
            )
            info["auto_install"] = auto
            info["adbkeyboard_installed"] = bool(auto.get("installed"))
        except Exception as ex:
            info["auto_install"] = {"error": f"{type(ex).__name__}:{ex}"}

    # cmd clipboard 是否可用
    r = adb.run_adb(
        ["shell", "cmd", "clipboard", "help"], serial=serial, timeout=8.0
    )
    out = (r.stdout or "") + (r.stderr or "")
    info["clipboard_cmd_available"] = (
        r.returncode == 0
        and "no shell command implementation" not in out.lower()
    )

    # input text 总是可用的（adb shell input 是 framework 内置）
    info["input_text_available"] = True

    paths: list = []
    if info["adbkeyboard_installed"]:
        paths.append("adbkeyboard")
    if info["clipboard_cmd_available"]:
        paths.append("clipboard_paste")
    paths.append("input_text_ascii (only)")
    info["available_paths"] = paths

    can_unicode = (
        info["adbkeyboard_installed"] or info["clipboard_cmd_available"]
    )
    info["unicode_ok"] = can_unicode
    if not can_unicode:
        info["warning"] = (
            "此设备目前只能发送 ASCII。要发中文/emoji，请：\n"
            " 1) 安装 tools/ADBKeyboard.apk（在设备上开启 USB 安装），或\n"
            " 2) 在 开发者选项 启用 USB 调试（安全设置）后再次尝试 install"
        )
    return info


__all__ = ["inject_text", "precheck_text_input", "InjectResult"]
