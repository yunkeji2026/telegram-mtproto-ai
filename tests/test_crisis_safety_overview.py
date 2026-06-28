"""T：危机安全总览 eval——单一入口回归整条安全链（L/O + J + Q）。"""

from __future__ import annotations

from src.eval.crisis_safety_overview import (
    evaluate_crisis_safety_overview,
    format_crisis_safety_overview,
)


def test_overview_aggregates_three_links():
    rep = evaluate_crisis_safety_overview()
    assert set(rep["links"]) == {
        "proactive_guard", "crisis_response", "resource_assurance"}
    # 每个环节都带自己的 passed + summary
    for v in rep["links"].values():
        assert "passed" in v and "summary" in v
    assert rep["summary"]["links_total"] == 3


def test_overview_passed_iff_all_links_pass():
    rep = evaluate_crisis_safety_overview()
    expect = all(v["passed"] for v in rep["links"].values())
    assert rep["passed"] is expect
    # links_passed 计数与各环节 passed 一致
    assert rep["summary"]["links_passed"] == sum(
        1 for v in rep["links"].values() if v["passed"])


def test_crisis_safety_overview_gate():
    # 整条安全链必须全绿——任一环节漏判都意味着危机期有安全缺口（硬门禁）。
    rep = evaluate_crisis_safety_overview()
    assert rep["passed"], "\n危机安全链有缺口：\n" + format_crisis_safety_overview(rep)


def test_format_lists_all_links():
    rep = evaluate_crisis_safety_overview()
    txt = format_crisis_safety_overview(rep)
    assert "危机安全总览" in txt
    assert "主动护栏" in txt
    assert "危机响应闭环" in txt
    assert "危机资源保障" in txt
