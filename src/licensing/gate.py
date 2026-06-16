"""C0-3 套餐 gating 纯原语：只读拦截 / 功能位 / 渠道 / 席位。

全部为纯函数，便于单测，不依赖 FastAPI。强制层（middleware / 端点）调用这些原语，
是否真正生效由 ``LicenseStatus.read_only``（即 enforce + 失效）决定——故 ``enforce``
默认关时这些原语对现网恒为「放行」，零破坏。
"""

from __future__ import annotations

from typing import Any

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 只读模式下仍放行的写路径前缀：认证流转 + 授权自身 + 心跳/状态（避免 UI 报错刷屏）。
# 这些不改业务数据，且锁死它们会让用户连「看到只读提示 / 登出 / 重载授权」都做不到。
READONLY_ALLOW_PREFIXES = (
    "/login",
    "/logout",
    "/api/login",
    "/api/logout",
    "/api/auth",
    "/api/admin/license",            # 授权状态读取 / 重载
    "/api/workspace/presence",       # 坐席在线状态
    "/api/workspace/heartbeat",
    "/api/workspace/notifications/read",
    "/set_lang",
    "/set_ui_mode",
)


def is_write_blocked(path: str, method: str, status: Any) -> bool:
    """判断某请求是否应在只读模式下被拦截。

    仅当 ``status.read_only`` 为真、方法为写、且路径不在放行白名单时返回 True。
    """
    if not getattr(status, "read_only", False):
        return False
    if (method or "").upper() not in WRITE_METHODS:
        return False
    p = path or ""
    for prefix in READONLY_ALLOW_PREFIXES:
        if p == prefix or p.startswith(prefix):
            return False
    return True


def feature_allowed(status: Any, name: str) -> bool:
    """功能位是否放行：enforce 关 → 恒放行；enforce 开 → 看授权功能位。"""
    if not getattr(status, "enforce", False):
        return True
    return bool(status.feature_enabled(name))


def channel_allowed(status: Any, channel: str) -> bool:
    """渠道是否放行：enforce 关 → 恒放行；enforce 开 → 看授权渠道列表。"""
    if not getattr(status, "enforce", False):
        return True
    return bool(status.channel_allowed(channel))


def seat_exceeded(status: Any, active_agents: int) -> bool:
    """活跃坐席是否超授权席位：enforce 关、seats=0（不限）→ 恒 False。"""
    if not getattr(status, "enforce", False):
        return False
    seats = int(getattr(status, "seats", 0) or 0)
    if seats <= 0:
        return False
    return int(active_agents or 0) > seats
