"""编排器发送侧统一反封号守卫（Stage M：让旁路发送入口也受护栏约束）。

A 线 mixin（``sender.py``）与三端 RPA（``rpa_send_guard``）已各自接 G1 Kill-Switch；
但**编排器受管 worker** 的发送（``AccountOrchestrator.send/send_media`` → 协议号 B 线 /
WhatsApp / 官方 API LINE·Messenger·WhatsApp Cloud）此前**直发裸 client**，绕过了
Kill-Switch + 反封号闸门——主动问候 / 唤醒 / 关怀 / 接管 等所有经编排器的外发都从这里走。

本守卫是编排器发送入口的薄封装：发送前查 ``global/platform/account`` 三级停发 +（开启时）
金丝雀放量白名单 + 反封号闸门（预热爬坡 / 健康红灯 / 配额）。命中即不发，让编排器返回
``{delivered: False, blocked: ...}``（调用方据此不记冷却、择机重试——冻结是暂态）。

设计：
- **Kill-Switch 恒查**（紧急急停，绝不可被任何发送路径绕过；模块级只读单例、零 DB、永不抛）。
- **金丝雀放量按 ``ops.canary.enabled`` 才查**（默认关 → 行为零变更）：启用时不在 cohort
  的账号一律 hold。此前 canary 只接在 B 线 ``protocol_autoreply`` 与官方 API/webhook 入站链，
  **编排器受管发送路径（L2 autosend deliver / 主动问候 / 唤醒 / 关怀经 orchestrator.send）
  绕过了它** → 放量爆炸半径控制对最主要的自动外发失效。此处补齐，使 canary 与 Kill-Switch/
  send-gate 一样成为编排器发送入口的统一护栏（一处接，覆盖 send/send_media + 适配器回落全路径）。
- **反封号闸门按 ``companion_send_gate.enabled`` 才查**（默认关 → 行为零变更）；
  复用 A/B 线同一份 ``build_account_signals``（registry 天龄/代理/封禁 + limiter 今日量），口径统一。
- **绝不抛异常**：守卫自身故障一律视为放行（broken guard 不得反过来把全部发送卡死）。
- 查序＝急停 → 金丝雀 → 反封号（最硬的先判；canary 是「放量圈」，send-gate 是「圈内节奏」）。
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

    reason 形如 ``kill_switch:<scope>`` / ``canary_hold`` / ``send_gate:<reason>``，
    供日志/审计区分拦因。
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
    # 2) 金丝雀放量（仅 ops.canary.enabled 时；默认关 → 零破坏）：不在 cohort → hold
    try:
        from src.ops.canary import is_held as _canary_held
        held, _c_reason = _canary_held(p, a, config)
        if held:
            return True, _c_reason or "canary_hold"
    except Exception:
        pass
    # 3) 反封号闸门（仅 enabled 时；默认关 → 零破坏）
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
