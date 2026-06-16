"""GET /api/accounts 统一账号清单聚合（Phase 2）。

合并 registry + config.yaml + 运行时健康，桌面端/web 后台共用。
"""


def test_accounts_endpoint_ok(auth_client):
    r = auth_client.get("/api/accounts")
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True
    assert isinstance(d.get("accounts"), list)
    assert "count" in d


def test_accounts_merges_registry_entry(auth_client):
    """注册表里 upsert 的账号应出现在聚合结果，且带 mode/sources。"""
    from src.integrations.account_registry import get_account_registry

    get_account_registry().upsert(
        "telegram", "acct_agg_test", mode="web", label="聚合测试号", status="pending"
    )
    r = auth_client.get("/api/accounts")
    d = r.json()
    hit = [
        a for a in d["accounts"]
        if a["platform"] == "telegram" and a["account_id"] == "acct_agg_test"
    ]
    assert hit, "注册表账号未出现在 /api/accounts"
    rec = hit[0]
    assert rec["mode"] == "web"
    assert rec["label"] == "聚合测试号"
    assert "registry" in rec["sources"]


def test_auto_reply_audit_endpoint(auth_client):
    r = auth_client.get("/api/accounts/auto-reply/audit")
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True
    assert isinstance(d.get("items"), list)
    assert "stats" in d and "global_enabled" in d


def test_auto_reply_config_get_and_set(auth_client, tmp_path):
    from src.integrations import protocol_autoreply_settings as s
    s.set_store_path(tmp_path / "pa.json")
    try:
        r = auth_client.post("/api/accounts/auto-reply/config", json={
            "enabled": True, "rate": {"hourly": 7}, "delay": {"min_sec": 1, "max_sec": 3},
        })
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["settings"]["enabled"] is True
        assert d["settings"]["rate"]["hourly"] == 7
        # GET 应反映刚保存的设置
        g = auth_client.get("/api/accounts/auto-reply/config").json()
        assert g["settings"]["enabled"] is True
        assert g["settings"]["delay"]["max_sec"] == 3
    finally:
        s.set_store_path(tmp_path / "pa.json")


def test_auto_reply_health_endpoint(auth_client):
    r = auth_client.get("/api/accounts/auto-reply/health")
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True
    assert "healthy" in d
    assert "global_enabled" in d
    assert "skill_manager_ready" in d
    assert isinstance(d.get("warnings"), list)
    assert isinstance(d.get("recent_changes"), list)
    assert "limits" in d and "stats_24h" in d


def test_webhooks_get_set_and_masking(auth_client, tmp_path):
    from src.integrations import notify_webhooks_store as ws
    ws.set_store_path(tmp_path / "wh_api.json")
    try:
        r = auth_client.post("/api/accounts/auto-reply/webhooks", json={
            "webhooks": [{
                "name": "tg-ops", "format": "telegram", "token": "real-token",
                "target": "-1001", "events": ["autoreply_alert"],
            }],
        })
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True and d["count"] == 1
        # 返回脱敏
        assert d["webhooks"][0]["token"].endswith("***")
        # GET 反映保存结果
        g = auth_client.get("/api/accounts/auto-reply/webhooks").json()
        assert g["webhooks"][0]["name"] == "tg-ops"
        assert g["webhooks"][0]["target"] == "-1001"
        # 二次保存 token 留空 → 不覆盖真实 token（按 name 保留）
        auth_client.post("/api/accounts/auto-reply/webhooks", json={
            "webhooks": [{
                "name": "tg-ops", "format": "telegram", "token": "",
                "target": "-1002", "events": ["autoreply_alert"],
            }],
        })
        eff = ws.effective_webhooks({})
        assert eff[0]["token"] == "real-token"  # 真实 token 保住
        assert eff[0]["target"] == "-1002"      # 其它字段更新
    finally:
        ws.set_store_path(tmp_path / "wh_api.json")


def test_webhook_test_endpoint_reports_failure(auth_client):
    """对一个不可达 url 的渠道测试 → 结构化返回 ok:false，不抛 500。"""
    r = auth_client.post("/api/accounts/auto-reply/webhooks/test", json={
        "webhook": {
            "name": "t", "format": "telegram",
            "url": "http://127.0.0.1:1/x", "target": "1",
            "events": ["autoreply_alert"],
        },
    })
    assert r.status_code == 200
    assert r.json().get("ok") is False


def test_config_set_records_change_in_audit(auth_client, tmp_path):
    """改全局配置应在配置审计表留痕，并经 health.recent_changes 可见。"""
    from src.integrations import protocol_autoreply_settings as s
    s.set_store_path(tmp_path / "pa2.json")
    try:
        auth_client.post("/api/accounts/auto-reply/config",
                         json={"rate": {"hourly": 11}})
        d = auth_client.get("/api/accounts/auto-reply/health").json()
        flat = [
            ch for c in d["recent_changes"] for ch in (c.get("changes") or [])
        ]
        assert any(x.get("key", "").endswith("rate.hourly") for x in flat)
    finally:
        s.set_store_path(tmp_path / "pa2.json")
