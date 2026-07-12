"""主机弹窗告警（Windows）——云端 Key 失效等关键运维事件提醒机主。

设计原则：
- 绝不影响主流程：所有函数吞掉自身异常，永不抛出。
- 去抖防刷屏：同一 key 在冷却窗内只弹一次。
- 非阻塞：Windows 弹窗在守护线程里弹（MessageBoxW），不卡调用方。
- 可静默：设环境变量 ``HOST_ALERT_SILENT=1``（测试/CI/无桌面）时只记日志、不弹窗。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any

_logger = logging.getLogger("host_alert")
_last_alert: dict[str, float] = {}
_lock = threading.Lock()

# key 失效/不可用的特征串（覆盖中英文与常见云厂商措辞）
_KEY_FAIL_MARKERS = (
    "unauthorized", "invalid api key", "invalid_api_key", "incorrect api key",
    "invalid authentication", "authentication", "permission denied", "permissiondenied",
    "api key", "api_key", "apikey", "access denied", "forbidden",
    "quota", "insufficient", "billing", "arrears", "arrearage", "expired",
    "欠费", "余额不足", "密钥", "鉴权", "无权", "认证失败", "配额", "过期", "已失效",
)


def looks_like_key_failure(err: Any) -> bool:
    """粗判一个异常/消息是否像「云端 Key 不可用/出问题」。"""
    try:
        # 优先看 HTTP 状态码（401 未授权 / 403 禁止）
        code = getattr(err, "status_code", None)
        if code is None:
            resp = getattr(err, "response", None)
            code = getattr(resp, "status_code", None)
        try:
            if int(code) in (401, 403):
                return True
        except (TypeError, ValueError):
            pass
        s = str(err).lower()
        if " 401" in s or " 403" in s or "http 401" in s or "http 403" in s:
            return True
        return any(m in s for m in _KEY_FAIL_MARKERS)
    except Exception:
        return False


def notify_host(title: str, message: str, *, key: str = "", cooldown_sec: float = 1800.0) -> bool:
    """非阻塞主机弹窗 + 日志；同 key 冷却窗内只提醒一次。返回是否本次实际提醒。

    绝不抛异常。``HOST_ALERT_SILENT=1`` 时只记日志、不弹窗。
    """
    try:
        k = (key or title or "").strip() or "host_alert"
        now = time.time()
        with _lock:
            if now - _last_alert.get(k, 0.0) < max(0.0, cooldown_sec):
                return False
            _last_alert[k] = now
        _logger.warning("[HOST ALERT] %s | %s", title, message)
        if os.environ.get("HOST_ALERT_SILENT", "").strip().lower() in ("1", "true", "yes", "on"):
            return True
        if sys.platform == "win32":
            def _popup():
                try:
                    import ctypes
                    # MB_ICONWARNING(0x30) | MB_SETFOREGROUND(0x10000) | MB_TOPMOST(0x40000)
                    ctypes.windll.user32.MessageBoxW(0, str(message), str(title), 0x30 | 0x10000 | 0x40000)
                except Exception:
                    pass
            threading.Thread(target=_popup, name="host_alert_popup", daemon=True).start()
        return True
    except Exception:
        return False


def notify_key_failure(provider: str, detail: str = "", *, cooldown_sec: float = 1800.0) -> bool:
    """便捷入口：云端 Key 失效提醒（按 provider 去抖）。"""
    title = "云端 Key 异常"
    msg = f"{provider} 的 API Key 不可用或出现问题，请检查/更换。\n详情: {detail}".strip()
    return notify_host(title, msg, key=f"keyfail:{provider}", cooldown_sec=cooldown_sec)
