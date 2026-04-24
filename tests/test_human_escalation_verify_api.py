"""人工转接：/api/human-escalation/verify 只读快照"""


def test_human_escalation_verify_api(auth_client, config_manager):
    config_manager.config["human_escalation"] = {
        "enabled": True,
        "repeat_threshold": 3,
        "timezone": "Asia/Shanghai",
        "agents": [{"user_id": 1, "username": "", "display_name": "A"}],
        "agent_teams": [],
    }
    r = auth_client.get("/api/human-escalation/verify")
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True
    assert d.get("helper_loaded") is False  # telegram_client=None in test app
    assert d.get("store_loaded") is False
    eff = d.get("effective") or {}
    assert eff.get("enabled") is True
    assert eff.get("agents_count") == 1
    assert eff.get("agent_teams_count") == 0
    assert eff.get("timezone") == "Asia/Shanghai"
    assert eff.get("escalation_cooldown_scope") == "per_normalized_question"
