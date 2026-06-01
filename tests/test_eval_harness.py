"""评测框架单测（Phase 质量横切）。"""

from __future__ import annotations

from src.eval.metrics import multiclass_metrics, resolve_rate
from src.eval.dataset import IntentSample, load_intent_samples
from src.eval.intent_eval import evaluate_intent, format_report
from src.eval.predictors import rule_intent_predictor


# ── metrics ──────────────────────────────────────────────────────────────

def test_multiclass_metrics_perfect():
    pairs = [("a", "a"), ("b", "b"), ("a", "a")]
    m = multiclass_metrics(pairs)
    assert m["accuracy"] == 1.0
    assert m["correct"] == 3 and m["total"] == 3
    assert m["per_label"]["a"]["f1"] == 1.0


def test_multiclass_metrics_with_errors():
    pairs = [("a", "a"), ("a", "b"), ("b", "b"), ("a", "b")]
    m = multiclass_metrics(pairs)
    assert m["accuracy"] == 0.5
    # a: tp=1 fp=2 -> precision 1/3; b: tp=1 fn=2 -> recall 1/3
    assert m["per_label"]["a"]["precision"] == round(1 / 3, 4)
    assert m["per_label"]["b"]["recall"] == round(1 / 3, 4)
    assert m["confusion"]["b->a"] == 2


def test_multiclass_metrics_empty():
    m = multiclass_metrics([])
    assert m["total"] == 0 and m["accuracy"] == 0.0


def test_resolve_rate():
    r = resolve_rate([True, False, True, True])
    assert r["total"] == 4 and r["resolved"] == 3
    assert r["resolve_rate"] == 0.75
    assert resolve_rate([])["resolve_rate"] == 0.0


# ── dataset ───────────────────────────────────────────────────────────────

def test_seed_dataset_loads():
    samples = load_intent_samples()
    assert len(samples) > 0
    assert all(isinstance(s, IntentSample) for s in samples)
    assert all(s.intent for s in samples if s.text.strip())


def test_load_jsonl(tmp_path):
    p = tmp_path / "d.jsonl"
    p.write_text('{"text":"hi","intent":"打招呼"}\n{"text":"","intent":"空消息"}\n',
                 encoding="utf-8")
    rows = load_intent_samples(str(p))
    assert len(rows) == 2 and rows[0].intent == "打招呼"


def test_load_yaml(tmp_path):
    p = tmp_path / "d.yaml"
    p.write_text('- {text: "在吗", intent: "打招呼"}\n', encoding="utf-8")
    rows = load_intent_samples(str(p))
    assert len(rows) == 1 and rows[0].text == "在吗"


# ── intent eval（规则基线）────────────────────────────────────────────────

def test_rule_predictor_basic():
    predict = rule_intent_predictor()
    assert predict("在吗") == "打招呼"
    assert predict("") == "空消息"


def test_evaluate_intent_on_seed_runs_and_reports():
    report = evaluate_intent(rule_intent_predictor())
    m = report["metrics"]
    # 规则基线在种子集上应是合理但非满分（harness 能暴露误判）
    assert 0.7 <= m["accuracy"] <= 1.0
    assert isinstance(report["passed"], bool)
    assert m["total"] == len(load_intent_samples())
    # 基线低于满分 → 应有误判明细
    assert len(report["errors"]) == m["total"] - m["correct"]


def test_evaluate_intent_threshold_gating():
    # 阈值设 0 必过；设 1.01 必不过
    assert evaluate_intent(rule_intent_predictor(), threshold=0.0)["passed"] is True
    assert evaluate_intent(rule_intent_predictor(), threshold=1.01)["passed"] is False


def test_format_report_ascii_safe():
    report = evaluate_intent(rule_intent_predictor())
    text = format_report(report)
    assert "意图评测报告" in text
    # 不含会让 Windows GBK 控制台崩溃的字符
    for bad in ("✓", "✗", "═"):
        assert bad not in text


def test_evaluate_intent_perfect_predictor():
    samples = load_intent_samples()
    gold = {s.text: s.intent for s in samples}
    report = evaluate_intent(lambda t: gold[t], samples, threshold=0.85)
    assert report["passed"] is True
    assert report["metrics"]["accuracy"] == 1.0
    assert report["errors"] == []
