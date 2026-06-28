"""T：危机安全「总览」评测——把整条安全链合成单一入口回归。

线上一条危机消息会依次经过三道独立闸门，分别由三个评测覆盖：

  1) **主动抑制（L/O）** `evaluate_proactive_guard`——危机/低落期，AI 主动触达被
     ``block``/``soft`` 抑制（绝不在情绪低谷推剧情邀约；强度分级避免过度沉默）。
  2) **响应闭环（J）** `evaluate_crisis_response`——被动回复阶段：危机输入注入安全指令（预防），
     回复自身鼓励自伤则整段红线覆盖（事后兜底），终态绝不含鼓励自伤片段。
  3) **资源保障（Q）** `evaluate_resource_assurance`——severe 危机回复补一次求助资源、不重复。

三道闸门任一漏判都意味着"危机期安全链有缺口"。本模块**不引入新逻辑**，只把三个既有评测
聚合成一张总览 + 单一 ``passed``（全绿才绿），供 CI 一处回归整条安全链、运营一眼看缺口在哪。
"""

from __future__ import annotations

from typing import Any, Dict

from .crisis_resource_eval import evaluate_resource_assurance, format_resource_report
from .crisis_response_eval import (
    evaluate_crisis_response,
    format_crisis_response_report,
)
from .proactive_guard_eval import (
    evaluate_proactive_guard,
    format_proactive_guard_report,
)


def evaluate_crisis_safety_overview(*, hotline: str = "") -> Dict[str, Any]:
    """跑整条危机安全链（L/O + J + Q）；返回各环节报告 + 合并 passed。

    ``passed`` = 三道闸门全部 PASS（任一 FAIL → 总览 FAIL，安全链有缺口）。
    """
    proactive = evaluate_proactive_guard()
    response = evaluate_crisis_response(hotline=hotline)
    resource = evaluate_resource_assurance()
    links = {
        "proactive_guard": proactive,   # L/O：主动抑制
        "crisis_response": response,    # J：响应闭环（预防 + 红线兜底）
        "resource_assurance": resource,  # Q：资源保障
    }
    passed = all(bool(v.get("passed")) for v in links.values())
    return {
        "links": links,
        "summary": {
            "total_scenarios": sum(
                int(v.get("summary", {}).get("total", 0)) for v in links.values()),
            "links_passed": sum(1 for v in links.values() if v.get("passed")),
            "links_total": len(links),
        },
        "passed": passed,
    }


def format_crisis_safety_overview(report: Dict[str, Any]) -> str:
    s = report["summary"]
    head = "[PASS]" if report["passed"] else "[FAIL]"
    lines = [
        "############ 危机安全总览（整条安全链）############",
        f"环节通过: {s['links_passed']}/{s['links_total']}  "
        f"总场景: {s['total_scenarios']}  {head}",
        "",
    ]
    links = report["links"]
    lines.append(format_proactive_guard_report(links["proactive_guard"]))
    lines.append("")
    lines.append(format_crisis_response_report(links["crisis_response"]))
    lines.append("")
    lines.append(format_resource_report(links["resource_assurance"]))
    return "\n".join(lines)


__all__ = ["evaluate_crisis_safety_overview", "format_crisis_safety_overview"]
