"""编排器发送侧统一反封号守卫（Stage M：让旁路发送入口也受护栏约束）。

A 线 mixin（``sender.py``）与三端 RPA（``rpa_send_guard``）已各自接 G1 Kill-Switch；
但**编排器受管 worker** 的发送（``AccountOrchestrator.send/send_media`` → 协议号 B 线 /
WhatsApp / 官方 API LINE·Messenger·WhatsApp Cloud）此前**直发裸 client**，绕过了
Kill-Switch + 反封号闸门——主动问候 / 唤醒 / 关怀 / 接管 等所有经编排器的外发都从这里走。

本守卫是编排器发送入口的薄封装：发送前查 ``global/platform/account`` 三级停发 +（开启时）
反封号闸门（预热爬坡 / 健康红灯 / 配额）。命中即不发，让编排器返回
``{delivered: False, blocked: ...}``（调用方据此不记冷却、择机重试——冻结是暂态）。

设计：
- **Kill-Switch 恒查**（紧急急停，绝不可被任何发送路径绕过；模块级只读单例、零 DB、永不抛）。
- **反封号闸门按 ``companion_send_gate.enabled`` 才查**（默认关 → 行为零变更）；
  复用 A/B 线同一份 ``build_account_signals``（registry 天龄/代理/封禁 + limiter 今日量），口径统一。
- **绝不抛异常**：守卫自身故障一律视为放行（broken guard 不得反过来把全部发送卡死）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def send_blocked(
    platform: str,
    account_id: str,
    *,
    config: Optional[Dict[str, Any]] = None,
    registry: Any = None,
) -> Tuple[bool, str]:
    """编排器发送前统一护栏。返回 ``(blocked, reason)``；任何异常 → ``(False, "")`` 放行。

    reason 形如 ``kill_switch:<scope>`` / ``send_gate:<reason>``，供日志/审计区分拦因。
    """
    p = str(platform or "")
    a = str(account_id or "default")
    # 1) Kill-Switch（恒查，紧急急停）
    try:
        from src.ops.kill_switch import is_blocked as _ks_blocked
        on, scope, _reason = _ks_blocked(p, a)
        if on:
            return True, f"kill_switch:{scope or 'global'}"
    except Exception:
        pass
    # 2) 反封号闸门（仅 enabled 时；默认关 → 零破坏）
    try:
        from src.skills.companion_send_gate import evaluate, gate_enabled
        if gate_enabled(config):
            from src.skills.account_signals import build_account_signals
            limiter = None
            try:
                from src.integrations.protocol_autoreply_limits import (
                    get_autoreply_limiter,
                )
                limiter = get_autoreply_limiter(config or {})
            except Exception:
                limiter = None
            sig = build_account_signals(p, a, registry=registry, limiter=limiter)
            dec = evaluate(sig, config)
            if not dec.get("allowed", True):
                return True, f"send_gate:{dec.get('reason') or 'blocked'}"
    except Exception:
        pass
    return False, ""


__all__ = ["send_blocked"]
