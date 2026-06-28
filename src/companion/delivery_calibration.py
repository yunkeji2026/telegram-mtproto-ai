"""全自动「真发」开闸前校准（纯函数）。

服务于 capability 看板里风险最高的主开关 ``inbox.l2_autosend.deliver``：开它之前，运营
必须先看清——**真发是双重 opt-in**：``deliver=true`` 且会话档位=全自动(``auto_ai``) 才真发。
所以「有多少会话被设成 auto_ai」决定了"翻开 deliver 到底会不会、对多少人真发"。

本模块把这件事算成确定性判词：worker/deliver/send-gate 三开关 × auto_ai 会话分布 → verdict
（inactive 不会发 / effective 会真发 / misconfigured 开了但发不出或裸奔），并给行动建议。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Mapping, Optional

from .capability_status import _dig

# automation_mode 取值（与 InboxStore.AUTOMATION_MODES 对齐；此处只读不强依赖）
_KNOWN_MODES = ("manual", "review", "multi_choice", "auto_ai")


def summarize_automation_modes(modes: Optional[Mapping[str, str]]) -> Dict[str, Any]:
    """把 ``{conversation_id: automation_mode}`` 聚合为分布。

    注：``all_automation_modes()`` 只返回**显式设过档位**的会话；auto_ai 必为显式设置，
    故 auto_ai 计数准确（未设档会话回落全局 review，不在此 dict、也不会被 deliver 真发）。
    """
    counts = Counter()
    for m in (modes or {}).values():
        key = m if m in _KNOWN_MODES else "other"
        counts[key] += 1
    total = sum(counts.values())
    by_mode = {k: counts.get(k, 0) for k in _KNOWN_MODES}
    if counts.get("other"):
        by_mode["other"] = counts["other"]
    return {"total_with_setting": total, "by_mode": by_mode,
            "auto_ai": counts.get("auto_ai", 0)}


def delivery_calibration(
    config: Any,
    modes: Optional[Mapping[str, str]] = None,
    *,
    recent_autosend: Optional[int] = None,
    recent_autosend_failed: Optional[int] = None,
) -> Dict[str, Any]:
    """主开关真发就绪度校准（纯函数）。

    config: 原始 config dict。modes: 会话档位（缺省视为无 auto_ai 会话）。
    recent_*: 可选，近窗口审计的真发/失败计数（路由 best-effort 注入；None=未知）。
    """
    worker_on = bool(_dig(config, "inbox.l2_autosend.enabled", False))
    deliver_on = bool(_dig(config, "inbox.l2_autosend.deliver", False))
    gate_on = bool(_dig(config, "companion_send_gate.enabled", False))

    dist = summarize_automation_modes(modes)
    auto_ai = dist["auto_ai"]

    warnings = []
    if deliver_on and not worker_on:
        warnings.append("deliver=true 但 l2_autosend worker 未启用 → 不会处置草稿，发不出")
    if deliver_on and auto_ai == 0:
        warnings.append("deliver=true 但无 auto_ai 会话 → 不会对任何人真发（需把目标会话设「🚀全自动」）")
    if deliver_on and not gate_on:
        warnings.append("真发已开但出站安全闸 companion_send_gate 未开 → 内容/频率裸奔，建议同开")

    # verdict：会不会真发 + 配置是否自洽
    will_send = deliver_on and worker_on and auto_ai > 0
    if will_send:
        verdict = "effective"           # 此刻确实会对 auto_ai 会话真发
        recommendation = ("真发生效中：建议确认 send-gate 已开 + 持续盯 recent 失败数"
                          if gate_on else "真发生效但安全闸未开：尽快开 companion_send_gate")
    elif deliver_on:
        verdict = "misconfigured"       # 开了但发不出（worker 关 / 无 auto_ai）
        recommendation = "deliver 已开但当前不会真发——按 warnings 修正前置条件"
    else:
        verdict = "inactive"            # 主开关关，处于安全默认态
        recommendation = ("先把少量目标会话设 auto_ai 并开 send-gate，再灰度开 deliver"
                          if auto_ai == 0 else
                          f"已有 {auto_ai} 个 auto_ai 会话就绪；开 send-gate 后可灰度开 deliver")

    return {
        "switches": {"worker": worker_on, "deliver": deliver_on, "send_gate": gate_on},
        "automation_modes": dist,
        "recent": {"autosend": recent_autosend, "autosend_failed": recent_autosend_failed},
        "will_send_now": will_send,
        "verdict": verdict,
        "warnings": warnings,
        "recommendation": recommendation,
    }


__all__ = ["summarize_automation_modes", "delivery_calibration"]
