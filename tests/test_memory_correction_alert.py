"""R18 记忆校正趋势 + 低采纳告警：build_correction_stats 趋势聚合 + alert-status 接线。

覆盖 helper 的 trend/sample/adoption 计算，以及 alert-status 在采纳率偏低（样本足够）
时挂 memory_adoption 告警、样本不足/采纳率高时不误报。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml
from starlette.testclient import TestClient

from src.utils.audit_store import AuditStore
from src.utils.config_manager import ConfigManager
from src.web.admin import create_app
from src.web.routes.episodic_identity_routes import build_correction_stats


async def _load_cm(tmp_path: Path) -> ConfigManager:
    cfg = {
        "telegram": {"api_id": "111", "api_hash": "abc", "phone_number": "+1"},
        "ai": {"api_key": "test"},
        "skills": {"enabled": []},
        "domain": "payment",
        "domain_plugins": {"payment": {"enabled": True}},
        "web_admin": {
            "secret_key": "test-secret-very-long-key-for-testing",
            "auth_token": "test-token-123",
            "session_max_age": 3600,
        },
        "intent": {"keywords": {}, "patterns": {}},
        "reply": {},
        "context_store": {"ttl_days": 30},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text("greeting: hi\n", encoding="utf-8")
    (tmp_path / "reply_strategies.yaml").write_text(
        yaml.dump(
            {
                "strategies": {
                    "standard": {"temperature": 0.7, "max_tokens": 800,
                                 "context_rounds": 3, "enabled": True}
                },
                "intent_strategy_map": {"default": "standard"},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (tmp_path / "snapshots").mkdir(exist_ok=True)
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    await cm.load()
    return cm


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── build_correction_stats helper ───────────────────────────────────────

def _sm(pending=0, total=0):
    sm = SimpleNamespace()
    sm.episodic_inferred_counts = lambda: {"pending": pending, "total": total}
    return sm


def test_build_stats_empty():
    out = build_correction_stats(None, None, days=30)
    assert out["confirmed"] == 0
    assert out["adoption_rate"] == 0.0
    assert out["trend"] == []
    assert out["sample"] == 0


def test_build_stats_trend_and_rate(tmp_path):
    audit = AuditStore(db_path=tmp_path / "audit.db")
    # 3 条确认（同库；ts 同日，trend 聚到一天）
    for i in range(3):
        audit.log("alice", "episodic_confirm_inferred", target=str(i), new_val=f"f{i}")
    out = build_correction_stats(audit, _sm(pending=1, total=10), days=30)
    assert out["confirmed"] == 3
    assert out["pending_inferred"] == 1
    assert out["sample"] == 4
    assert abs(out["adoption_rate"] - 0.75) < 1e-6
    # 同日 3 条 → trend 一个桶 count=3
    assert sum(t["count"] for t in out["trend"]) == 3


def test_build_stats_no_trend_flag(tmp_path):
    audit = AuditStore(db_path=tmp_path / "audit.db")
    audit.log("a", "episodic_confirm_inferred", target="1", new_val="x")
    out = build_correction_stats(audit, _sm(), days=30, with_trend=False, recent_limit=0)
    assert "trend" not in out
    assert out["recent"] == []


# ── alert-status 低采纳告警 ─────────────────────────────────────────────

def _build_app(tmp_path, *, confirmed, pending, adoption_alert=None):
    cm = _run_async(_load_cm(tmp_path))
    if adoption_alert is not None:
        cm.config.setdefault("memory", {})["adoption_alert"] = adoption_alert
    audit = AuditStore(db_path=tmp_path / "audit.db")
    for i in range(confirmed):
        audit.log("alice", "episodic_confirm_inferred", target=str(i), new_val=f"f{i}")
    tc = MagicMock()
    sm = MagicMock()
    sm.episodic_inferred_counts = MagicMock(return_value={"pending": pending, "total": pending + confirmed})
    # 关闭其它告警源的干扰
    sm.crisis_count_for_admin = MagicMock(return_value=0)
    tc.skill_manager = sm
    app = create_app(cm, audit_store=audit, boot_ts=0, telegram_client=tc)
    client = TestClient(app, raise_server_exceptions=True)
    # /api/alert-status 走 page（session）鉴权——需真实登录建会话
    from src.utils.web_user_store import ROLE_MASTER, WebUserStore

    wstore = WebUserStore(tmp_path / "web_users.db")
    if wstore.user_count() == 0:
        wstore.create_user("admin", "test-token-123", ROLE_MASTER)
    client.get("/login")
    client.post(
        "/login",
        data={"username": "admin", "password": "test-token-123"},
        follow_redirects=True,
    )
    client.headers.update({"Authorization": "Bearer test-token-123"})
    return client


def _adoption_alerts(client):
    r = client.get("/api/alert-status")
    assert r.status_code == 200
    return [a for a in (r.json().get("alerts") or []) if a.get("type") == "memory_adoption"]


def test_alert_fires_on_low_adoption(tmp_path):
    # confirmed=3, pending=20 → sample=23≥10, rate≈0.13<0.3 → 告警
    client = _build_app(tmp_path, confirmed=3, pending=20)
    al = _adoption_alerts(client)
    assert len(al) == 1
    assert al[0]["level"] == "warn"
    assert "/episodic-memory" in al[0]["action_url"]


def test_alert_silent_on_small_sample(tmp_path):
    # confirmed=2, pending=3 → sample=5 <10 → 不报
    client = _build_app(tmp_path, confirmed=2, pending=3)
    assert _adoption_alerts(client) == []


def test_alert_silent_on_high_adoption(tmp_path):
    # confirmed=20, pending=2 → sample=22, rate≈0.91 → 不报
    client = _build_app(tmp_path, confirmed=20, pending=2)
    assert _adoption_alerts(client) == []


# ── R19：阈值可配 ───────────────────────────────────────────────────────

def test_alert_disabled_by_config(tmp_path):
    # 低采纳本应报，但 enabled=false → 静默
    client = _build_app(
        tmp_path, confirmed=3, pending=20, adoption_alert={"enabled": False},
    )
    assert _adoption_alerts(client) == []


def test_alert_custom_low_rate(tmp_path):
    # rate≈0.13；默认 0.30 会报，但配 low_rate=0.10 → 不报
    client = _build_app(
        tmp_path, confirmed=3, pending=20, adoption_alert={"low_rate": 0.10},
    )
    assert _adoption_alerts(client) == []


def test_alert_custom_min_sample(tmp_path):
    # sample=5 < 默认 10 不报；但配 min_sample=4 → 报（rate=2/5=0.4? 需 <low_rate）
    # 用 confirmed=1,pending=9 → sample=10? 改为显式小样本场景：
    client = _build_app(
        tmp_path, confirmed=1, pending=7,  # sample=8, rate=0.125
        adoption_alert={"min_sample": 5},
    )
    al = _adoption_alerts(client)
    assert len(al) == 1
    assert al[0]["level"] == "warn"
