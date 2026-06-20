"""RPA 发送侧反封号守卫（Phase C：让 G1 全局 Kill-Switch 名副其实覆盖三端 RPA）。

A/B 线（sender.py / protocol_autoreply.py）已接 Kill-Switch；RPA（LINE/Messenger/
WhatsApp）此前不受 ``global`` 冻结约束 → 「全局」名不副实。本守卫是三端 runner 发送
入口共用的薄封装：发送前查账号级/平台级/全局停发，命中即跳过物理发送。

设计：
- 只读进程 Kill-Switch 单例（main.py 启动已按配置路径初始化）；走内存缓存，热路径零 DB。
- **绝不抛异常**：守卫故障不得阻断/掩盖正常 RPA 发送（best-effort，失败即视为放行）。
- 不依赖 root config（Kill-Switch 用单例即可）；canary 因 runner 仅持平台子配置，
  暂不在此判（见 DEVLOG Phase C 优化笔记，待 root cfg 接入后补）。
"""

from __future__ import annotations

from typing import Any, Tuple


def rpa_send_blocked(
    platform: str, account_id: str, *, kill_switch: Any = None,
) -> Tuple[bool, str]:
    """返回 ``(blocked, scope)``。任何异常 → ``(False, "")``（放行，不阻断 RPA）。"""
    try:
        ks = kill_switch
        if ks is None:
            from src.ops.kill_switch import get_kill_switch
            ks = get_kill_switch()
        blocked, scope, _reason = ks.is_blocked(str(platform or ""), str(account_id or "default"))
        return bool(blocked), str(scope or "")
    except Exception:
        return False, ""


__all__ = ["rpa_send_blocked"]
