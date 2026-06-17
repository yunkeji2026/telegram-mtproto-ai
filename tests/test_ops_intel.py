"""G 线运营智能化纯函数测试：根因建议 / 趋势异动 / 周报装配。"""

from src.utils.ops_intel import (
    build_ops_report,
    detect_trend_anomaly,
    incident_advice,
)


# ── incident_advice ──────────────────────────────────────────────────────

def test_advice_maps_known_ids():
    out = incident_advice([
        {"id": "db", "name": "数据库连通"},
        {"id": "over_seats", "name": "计费"},
    ])
    by = {a["id"]: a for a in out}
    assert "持久层" in by["db"]["cause"]
    assert by["db"]["action"]
    assert "席位" in by["over_seats"]["cause"]


def test_advice_worker_prefix():
    out = incident_advice([{"id": "worker_autosend", "name": "L2 自动发送"}])
    assert "worker" in out[0]["cause"] or "熔断" in out[0]["cause"]
    assert out[0]["action"]


def test_advice_unknown_falls_back():
    out = incident_advice([{"id": "mystery", "name": "X"}])
    assert out[0]["cause"] == "未归类异常"


def test_advice_empty():
    assert incident_advice(None) == []
    assert incident_advice([]) == []


# ── detect_trend_anomaly ─────────────────────────────────────────────────

def test_anomaly_none_when_stable():
    assert detect_trend_anomaly([10, 10, 11, 9, 10]) is None


def test_anomaly_spike_up():
    a = detect_trend_anomaly([10, 10, 10, 10, 30])
    assert a is not None
    assert a["direction"] == "up"
    assert a["delta_pct"] >= 50


def test_anomaly_drop_down():
    a = detect_trend_anomaly([20, 20, 20, 20, 5])
    assert a is not None
    assert a["direction"] == "down"
    assert a["delta_pct"] <= -50


def test_anomaly_baseline_zero_then_positive():
    a = detect_trend_anomaly([0, 0, 0, 7])
    assert a is not None
    assert a["direction"] == "up"
    assert a["delta_pct"] is None


def test_anomaly_too_few_points():
    assert detect_trend_anomaly([1, 100]) is None


def test_anomaly_drop_last_ignores_partial_bucket():
    # 末桶半截(=2)会误报↓；drop_last 后以最后已完结桶(=10)判定→无异动。
    assert detect_trend_anomaly([10, 10, 10, 10, 2], drop_last=True) is None
    # drop_last 后倒数第二桶若本身突增，仍能抓到。
    a = detect_trend_anomaly([10, 10, 10, 40, 2], drop_last=True)
    assert a is not None and a["direction"] == "up"


# ── build_ops_report ─────────────────────────────────────────────────────

def test_report_computes_mttr_hours_and_headline():
    rep = build_ops_report(
        days=7,
        incident_stats={"total": 4, "resolved": 3, "open": 1,
                        "by_kind": {"health": 3, "billing": 1}, "mttr_sec": 7200},
        roi={"business": {"conversions": 5, "conversion_rate": 0.25, "leads": 20},
             "automation": {"ai_share_pct": 60, "saved_hours": 8.0, "saved_money": 160.0}},
        reliability={"score": 92, "light": "green"},
        billing={"charges": {"total": 99.0, "currency": "USD"}, "reconcile": {"over_seats": 0}},
    )
    assert rep["ok"] is True
    assert rep["incidents"]["mttr_hours"] == 2.0
    assert rep["incidents"]["by_kind"]["billing"] == 1
    assert rep["automation"]["saved_hours"] == 8.0
    assert rep["business"]["conversions"] == 5
    assert rep["reliability"]["score"] == 92
    assert any("运维事件" in h for h in rep["headline"])
    assert any("可靠性评分" in h for h in rep["headline"])


def test_report_handles_empty_inputs():
    rep = build_ops_report()
    assert rep["ok"] is True
    assert rep["incidents"]["total"] == 0
    assert rep["incidents"]["mttr_hours"] is None
