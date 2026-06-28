"""危机资源保障评测（Q）——severe 危机回复「补求助资源、且不重复」。

`SkillManager._apply_crisis_safety_net` 在 ``crisis_resource_assurance`` 开启时，对 severe
危机、配了热线、且回复**未提及**求助资源的情况，温柔补一句资源。本模块把该分支当回归：

  - severe + 开 + 有热线 + 回复无资源 → **补一次**（且最终只出现一次热线）；
  - 回复已含「热线/求助/咨询/热线串」→ **不重复附加**（避免啰嗦/双附）；
  - 非 severe / 保障关 / 无热线 → **不补**；
  - 红线优先：若回复本身鼓励自伤 → 先被 `safe_fallback_reply` 整段覆盖（资源分支不触达）。

``simulate_crisis_safety_net`` 逐行复刻线上确定性核心（含「红线覆盖 → 资源保障」次序），
纯函数、离线可复现 → CI 常驻安全门禁。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .dataset import CrisisResourceScenario, load_crisis_resource_scenarios

_RESOURCE_MARKERS = ("热线", "求助", "咨询")


def simulate_crisis_safety_net(
    reply: str, *, level: str = "", hotline: str = "",
    wb_enabled: bool = True, assurance: bool = False,
) -> str:
    """复刻 `_apply_crisis_safety_net` 确定性核心（红线覆盖优先 → severe 资源保障）。"""
    from src.utils.wellbeing_guard import detect_harmful_reply, safe_fallback_reply

    if not reply:
        return reply
    if not wb_enabled:
        return reply
    if detect_harmful_reply(reply):
        return safe_fallback_reply(level or "severe", hotline=hotline)
    if (level == "severe" and assurance and hotline
            and not any(k in reply for k in (*_RESOURCE_MARKERS, hotline))):
        return reply.rstrip() + f"\n如果你愿意，也可以找人聊聊：{hotline}。"
    return reply


def evaluate_resource_assurance(
    scenarios: Optional[List[CrisisResourceScenario]] = None,
) -> Dict[str, Any]:
    """跑资源保障评测；返回逐场景明细 + 指标 + passed。

    passed = 该补的全补(append_recall=1.0) 且零误补(false_append=0) 且
             任何 final 里热线最多出现一次(no_duplicate)。
    """
    rows = scenarios if scenarios is not None else load_crisis_resource_scenarios()
    need = caught = false_append = duplicate = 0
    results: List[Dict[str, Any]] = []
    for sc in rows:
        final = simulate_crisis_safety_net(
            sc.reply, level=sc.level, hotline=sc.hotline, assurance=sc.assurance)
        appended = final != sc.reply and "找人聊聊" in final
        if sc.expect_appended:
            need += 1
            if appended:
                caught += 1
        elif appended:
            false_append += 1
        # 资源行附加后，热线串不得重复出现
        if sc.hotline and final.count(sc.hotline) > 1:
            duplicate += 1
        results.append({"reply": sc.reply, "level": sc.level,
                        "assurance": sc.assurance, "expect_appended": sc.expect_appended,
                        "appended": appended, "note": sc.note})

    recall = round(caught / need, 3) if need else 1.0
    return {
        "results": results,
        "summary": {
            "total": len(rows),
            "append_recall": recall,
            "false_append": false_append,
            "duplicate": duplicate,
        },
        "passed": recall >= 1.0 and false_append == 0 and duplicate == 0,
    }


def format_resource_report(report: Dict[str, Any]) -> str:
    m = report["summary"]
    lines = [
        "=== 危机资源保障报告 ===",
        f"场景: {m['total']}  补附召回: {m['append_recall']:.0%}  "
        f"误补: {m['false_append']}  重复附加: {m['duplicate']}  "
        f"{'[PASS]' if report['passed'] else '[FAIL]'}",
    ]
    bad = [r for r in report["results"]
           if r["expect_appended"] != r["appended"]]
    if bad:
        lines.append(f"不符 {len(bad)} 例:")
        for r in bad[:20]:
            lines.append(
                f"  - level={r['level']} 保障={r['assurance']} "
                f"期望补={r['expect_appended']} 实际={r['appended']} 「{r['reply'][:18]}」")
    return "\n".join(lines)


__all__ = [
    "simulate_crisis_safety_net", "evaluate_resource_assurance",
    "format_resource_report",
]
