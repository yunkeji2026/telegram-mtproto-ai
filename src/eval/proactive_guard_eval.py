"""主动护栏闭环评测（L）——危机/低落期 AI 主动触达的抑制决策。

`proactive_emotion_gate`（纯函数）是所有主动路径（开场/早晚安/纪念日/槽位采集）共用的
安全闸门：severe 近期危机 → ``block``（完全不主动，改派 care 兜底）；elevated/末条负面 →
``soft``（仅温和问候，禁剧情邀约）；否则 ``""``。本模块把它当**安全不变量**回归：

  - **severe 窗口内必 block**（severe_block_recall 必须 = 1.0，漏一个 = 在最脆弱时还推剧情）；
  - 窗口外正确退化（只看末条情绪）；
  - 不过度沉默（正面/中性末条 → 不抑制，否则伤害正常陪伴粘性）。

确定性、离线可复现 → CI 常驻安全门禁。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .dataset import ProactiveGuardScenario, load_proactive_guard_scenarios

_NOW = 1_700_000_000.0  # 固定 now，使 created_at 推导可复现


def _gate_of(sc: ProactiveGuardScenario) -> str:
    from src.utils.wellbeing_guard import proactive_emotion_gate

    latest: Optional[Dict[str, Any]] = None
    if sc.crisis_level:
        latest = {"level": sc.crisis_level,
                  "created_at": _NOW - sc.crisis_age_days * 86400.0}
    ei = sc.last_emotion_intensity if sc.last_emotion_intensity >= 0 else None
    return proactive_emotion_gate(
        latest, now=_NOW, window_days=14.0, last_emotion=sc.last_emotion,
        last_emotion_intensity=ei)


def evaluate_proactive_guard(
    scenarios: Optional[List[ProactiveGuardScenario]] = None,
) -> Dict[str, Any]:
    """跑主动护栏；返回逐场景明细 + 安全指标 + passed。

    passed = severe 窗口内 block 召回 1.0 且整体准确率 1.0 且零「漏抑制负面」。
    （over_suppression 仅作观测，不计入 FAIL——宁可偶尔少打扰也不在情绪低谷硬推。）
    """
    rows = scenarios if scenarios is not None else load_proactive_guard_scenarios()
    correct = 0
    severe_need = severe_block = 0
    under_suppress = 0   # 期望 soft/block 却得 ""（安全/共情漏判，严重）
    over_suppress = 0    # 期望 "" 却被抑制（过度沉默，观测）
    results: List[Dict[str, Any]] = []
    for sc in rows:
        got = _gate_of(sc)
        ok = got == sc.expect
        if ok:
            correct += 1
        # severe 窗口内（age≤14）必 block
        if sc.crisis_level == "severe" and sc.crisis_age_days <= 14:
            severe_need += 1
            if got == "block":
                severe_block += 1
        if sc.expect in ("soft", "block") and got == "":
            under_suppress += 1
        if sc.expect == "" and got != "":
            over_suppress += 1
        results.append({"expect": sc.expect, "got": got, "ok": ok,
                        "crisis_level": sc.crisis_level,
                        "crisis_age_days": sc.crisis_age_days,
                        "last_emotion": sc.last_emotion, "note": sc.note})

    n = len(rows)
    accuracy = round(correct / n, 3) if n else 0.0
    severe_recall = round(severe_block / severe_need, 3) if severe_need else 1.0
    return {
        "results": results,
        "summary": {
            "total": n,
            "accuracy": accuracy,
            "severe_block_recall": severe_recall,
            "under_suppress": under_suppress,
            "over_suppress": over_suppress,
        },
        "passed": (severe_recall >= 1.0 and accuracy >= 1.0 and under_suppress == 0),
    }


def format_proactive_guard_report(report: Dict[str, Any]) -> str:
    m = report["summary"]
    lines = [
        "=== 主动护栏闭环报告（情绪安全闸门）===",
        f"场景: {m['total']}  准确率: {m['accuracy']:.0%}  "
        f"severe窗口内block召回: {m['severe_block_recall']:.0%}  "
        f"漏抑制: {m['under_suppress']}  过度沉默: {m['over_suppress']}  "
        f"{'[PASS]' if report['passed'] else '[FAIL]'}",
    ]
    bad = [r for r in report["results"] if not r["ok"]]
    if bad:
        lines.append(f"不符 {len(bad)} 例:")
        for r in bad[:20]:
            lines.append(
                f"  - 危机={r['crisis_level'] or '无'}/{r['crisis_age_days']}d "
                f"末条={r['last_emotion'] or '空'} 期望={r['expect'] or '不抑制'} "
                f"实际={r['got'] or '不抑制'}")
    return "\n".join(lines)


__all__ = ["evaluate_proactive_guard", "format_proactive_guard_report"]
