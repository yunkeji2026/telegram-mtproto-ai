"""E1/E3 运营总览纯函数测试：灯聚合 / 计费异常 / 总览装配。"""

from src.utils.ops_overview import (
    assemble_ops_overview,
    billing_anomalies,
    companion_config_light,
    worst_light,
)


def test_worst_light_picks_most_severe():
    assert worst_light("green", "yellow") == "yellow"
    assert worst_light("green", "red", "yellow") == "red"
    assert worst_light("green", "green") == "green"


def test_worst_light_ignores_empty_and_unknown():
    assert worst_light("", "green") == "green"
    assert worst_light("", "") == ""
    assert worst_light("bogus", "yellow") == "yellow"


def test_billing_anomalies_over_seats_is_fail():
    billing = {
        "available": True,
        "reconcile": {"seats": 5, "active_agents": 8, "over_seats": 3},
        "charges": {},
    }
    issues = billing_anomalies(billing)
    codes = {i["code"]: i for i in issues}
    assert "over_seats" in codes
    assert codes["over_seats"]["severity"] == "fail"
    assert codes["over_seats"]["detail"]["over_seats"] == 3


def test_billing_anomalies_message_overage_is_warn():
    billing = {
        "available": True,
        "reconcile": {"over_seats": 0},
        "charges": {
            "message_overage_qty": 120,
            "message_overage_amount": 12.0,
            "currency": "USD",
        },
    }
    issues = billing_anomalies(billing)
    assert len(issues) == 1
    assert issues[0]["code"] == "message_overage"
    assert issues[0]["severity"] == "warn"


def test_billing_anomalies_empty_when_unavailable():
    assert billing_anomalies({"available": False, "reconcile": {"over_seats": 9}}) == []
    assert billing_anomalies(None) == []


def test_assemble_overall_light_is_worst_of_health_and_reliability():
    ov = assemble_ops_overview(
        health={"light": "green"},
        reliability={"light": "yellow", "score": 80},
    )
    assert ov["ok"] is True
    assert ov["overall_light"] == "yellow"
    assert ov["kpis"]["reliability_score"] == 80


def test_assemble_over_seats_escalates_overall_to_red():
    ov = assemble_ops_overview(
        health={"light": "green"},
        reliability={"light": "green"},
        billing={
            "available": True,
            "plan": "pro",
            "reconcile": {"over_seats": 2},
            "charges": {"total": 99.0, "currency": "USD"},
        },
    )
    assert ov["overall_light"] == "red"
    assert ov["kpis"]["over_seats"] == 2
    assert ov["kpis"]["billing_anomaly_count"] == 1
    assert ov["kpis"]["billing_total"] == 99.0


def test_assemble_surfaces_roi_kpis_and_sections():
    roi = {
        "business": {"leads": 10, "conversions": 3, "conversion_rate": 0.3},
        "automation": {"ai_share_pct": 65, "saved_hours": 4.0, "saved_money": 80.0},
    }
    ov = assemble_ops_overview(
        roi=roi,
        health={"light": "green"},
        reliability={"light": "green", "alert_count": 2},
        open_incidents=1,
    )
    k = ov["kpis"]
    assert k["leads"] == 10
    assert k["conversions"] == 3
    assert k["ai_share_pct"] == 65
    assert k["saved_money"] == 80.0
    assert k["open_alerts"] == 2
    assert k["open_incidents"] == 1
    assert ov["sections"]["roi"] is roi


def test_assemble_handles_all_empty():
    ov = assemble_ops_overview()
    assert ov["ok"] is True
    assert ov["overall_light"] == ""
    assert ov["billing_anomalies"] == []


# ── 陪伴能力配置健康接入总览 ───────────────────────────────────────────────

def test_companion_config_light_levels():
    assert companion_config_light(None) == ""
    assert companion_config_light({"summary": {"errors": 0, "warnings": 0}}) == "green"
    assert companion_config_light({"summary": {"errors": 0, "warnings": 2}}) == "yellow"
    assert companion_config_light({"summary": {"errors": 1, "warnings": 0}}) == "red"


def test_assemble_companion_errors_escalate_overall_to_red():
    ov = assemble_ops_overview(
        health={"light": "green"}, reliability={"light": "green"},
        companion={"summary": {"errors": 1, "warnings": 0},
                   "consistency": [{"severity": "error", "message": "真发裸奔"}]},
    )
    assert ov["overall_light"] == "red"
    assert ov["kpis"]["companion_config_light"] == "red"
    assert ov["kpis"]["companion_config_errors"] == 1
    assert ov["sections"]["companion"]["summary"]["errors"] == 1


def test_assemble_companion_warnings_escalate_to_yellow_only():
    ov = assemble_ops_overview(
        health={"light": "green"}, reliability={"light": "green"},
        companion={"summary": {"errors": 0, "warnings": 3}},
    )
    assert ov["overall_light"] == "yellow"
    assert ov["kpis"]["companion_config_warnings"] == 3


def test_assemble_companion_none_does_not_affect_overall():
    ov = assemble_ops_overview(health={"light": "green"}, reliability={"light": "green"})
    assert ov["overall_light"] == "green"
    assert ov["kpis"]["companion_config_light"] == ""


# ── P0-3/B9：入站自动译量接入总览（成本护栏观测） ───────────────────────────

def test_assemble_surfaces_inbound_translation_volume():
    it = {"translated": 42, "failed": 3, "by_source_lang": {"en": 30, "ja": 12},
          "trend": [{"day": "07-10", "translated": 42}]}
    ov = assemble_ops_overview(
        health={"light": "green"}, reliability={"light": "green"},
        inbound_translation=it,
    )
    assert ov["kpis"]["inbound_xlate_translated"] == 42
    assert ov["kpis"]["inbound_xlate_failed"] == 3
    assert ov["sections"]["inbound_translation"] is it
    # 纯观测：不影响总览灯
    assert ov["overall_light"] == "green"


def test_assemble_inbound_translation_absent_defaults_zero():
    ov = assemble_ops_overview()
    assert ov["kpis"]["inbound_xlate_translated"] == 0
    assert ov["kpis"]["inbound_xlate_failed"] == 0
    assert ov["sections"]["inbound_translation"] == {}
