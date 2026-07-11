"""E1/E3 运营总览纯函数测试：灯聚合 / 计费异常 / 总览装配。"""

from src.utils.ops_overview import (
    assemble_ops_overview,
    billing_anomalies,
    companion_config_light,
    orchestrator_worker_light,
    orchestrator_worker_problems,
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


# ── 编排器 worker 健康接入总览（出站路由真信号；P2-b） ─────────────────────

def test_orchestrator_worker_light_levels():
    # 无 status / 无受管账号 → 不参与
    assert orchestrator_worker_light(None) == ""
    assert orchestrator_worker_light({}) == ""
    assert orchestrator_worker_light({"total": 0, "by_state": {}}) == ""
    # 全 running → 绿
    assert orchestrator_worker_light(
        {"total": 2, "by_state": {"running": 2}}) == "green"
    # 有 error 态 worker → 黄（降级，非全崩）
    assert orchestrator_worker_light(
        {"total": 2, "by_state": {"running": 1, "error": 1}}) == "yellow"


def test_orchestrator_worker_light_ignores_raw_fallback_rate():
    """关键不变量：回落率本身不上灯——RPA-only 部署 100% 回落是正常的，不应误报。"""
    # 只有回落率高、但编排器无 error worker → 不因回落率抬灯
    ov = assemble_ops_overview(
        health={"light": "green"}, reliability={"light": "green"},
        orchestrator={"total": 1, "by_state": {"running": 1}},
        send_routes={"total": 100, "fallback_rate": 1.0,
                     "adapter_total": 100, "orchestrator_total": 0},
    )
    assert ov["overall_light"] == "green"
    assert ov["kpis"]["send_fallback_rate"] == 1.0  # 数值仍暴露（信息量）
    assert ov["kpis"]["orchestrator_worker_light"] == "green"


def test_assemble_orchestrator_error_escalates_to_yellow():
    ov = assemble_ops_overview(
        health={"light": "green"}, reliability={"light": "green"},
        orchestrator={"total": 3, "by_state": {"running": 2, "error": 1}},
    )
    assert ov["overall_light"] == "yellow"
    assert ov["kpis"]["orchestrator_worker_light"] == "yellow"
    assert ov["kpis"]["orchestrator_workers_error"] == 1
    assert ov["kpis"]["orchestrator_workers_running"] == 2
    assert ov["sections"]["orchestrator"]["by_state"]["error"] == 1


def test_assemble_orchestrator_none_does_not_affect_overall():
    ov = assemble_ops_overview(health={"light": "green"}, reliability={"light": "green"})
    assert ov["overall_light"] == "green"
    assert ov["kpis"]["orchestrator_worker_light"] == ""
    assert ov["kpis"]["send_fallback_rate"] == 0.0
    assert ov["kpis"]["send_total"] == 0


# ── P6：编排器 worker 崩溃问题项（告警外发 + 总览灯升级共用口径） ──────────────

def test_orchestrator_worker_problems_severity_by_restarts():
    status = {
        "total": 3,
        "accounts": [
            {"platform": "telegram", "account_id": "a1", "state": "running"},
            {"platform": "line", "account_id": "a2", "state": "error", "restarts": 1,
             "last_error": "adb lost"},
            {"platform": "whatsapp", "account_id": "a3", "state": "error", "restarts": 5,
             "last_error": "session dead"},
        ],
    }
    probs = orchestrator_worker_problems(status)
    by_id = {p["id"]: p for p in probs}
    assert set(by_id) == {"line:a2", "whatsapp:a3"}  # running 不计入
    assert by_id["line:a2"]["status"] == "warn"       # restarts<3 → 瞬时抖动
    assert by_id["whatsapp:a3"]["status"] == "fail"   # restarts>=3 → 真实掉线
    assert by_id["whatsapp:a3"]["detail"] == "session dead"


def test_orchestrator_worker_problems_empty_cases():
    assert orchestrator_worker_problems(None) == []
    assert orchestrator_worker_problems({}) == []
    assert orchestrator_worker_problems({"accounts": []}) == []
    # 仅 by_state（无 accounts 明细）→ 无法逐条判定 → 空
    assert orchestrator_worker_problems({"total": 1, "by_state": {"error": 1}}) == []


def test_orchestrator_worker_light_red_on_persistent_crash():
    """accounts 里有持续崩溃（restarts>=3）→ 总览灯升级到 red（与 P6 告警同口径）。"""
    status = {
        "total": 2,
        "by_state": {"running": 1, "error": 1},
        "accounts": [
            {"platform": "telegram", "account_id": "a1", "state": "running"},
            {"platform": "line", "account_id": "a2", "state": "error", "restarts": 4},
        ],
    }
    assert orchestrator_worker_light(status) == "red"
    ov = assemble_ops_overview(
        health={"light": "green"}, reliability={"light": "green"},
        orchestrator=status,
    )
    assert ov["overall_light"] == "red"
    assert ov["kpis"]["orchestrator_worker_light"] == "red"


def test_orchestrator_worker_light_yellow_on_transient_crash():
    status = {
        "total": 2,
        "by_state": {"running": 1, "error": 1},
        "accounts": [
            {"platform": "line", "account_id": "a2", "state": "error", "restarts": 1},
        ],
    }
    assert orchestrator_worker_light(status) == "yellow"
