"""M6③：协议栈联调自检（readiness 报告）的单元测试。"""

from __future__ import annotations

from src.integrations import protocol_bridge as pb
from src.integrations import protocol_diagnostics as pd
from src.integrations import telegram_protocol_login as tpl


def test_static_empty_config_not_ready():
    rep = pd.readiness_static({})
    assert rep["platform_login_enabled"] is False
    assert rep["telegram"]["ready"] is False
    assert rep["whatsapp"]["mode_enabled"] is False
    assert rep["overall_ready"] is False
    assert rep["inbox_ingest"]["sink_registered"] in (True, False)


def test_static_telegram_ready(monkeypatch):
    monkeypatch.setattr(tpl, "is_pyrogram_available", lambda: True)
    pb.register_inbox_sink(lambda m: None)
    try:
        cfg = {
            "platform_login": {"enabled": True,
                               "telegram": {"protocol_enabled": True}},
            "telegram": {"api_id": 123, "api_hash": "abc"},
        }
        rep = pd.readiness_static(cfg)
        assert rep["telegram"]["ready"] is True
        assert rep["telegram"]["credentials"] is True
        assert rep["telegram"]["pyrogram_available"] is True
        assert rep["inbox_ingest"]["sink_registered"] is True
        assert rep["overall_ready"] is True
    finally:
        pb.register_inbox_sink(None)


def test_static_telegram_missing_creds(monkeypatch):
    monkeypatch.setattr(tpl, "is_pyrogram_available", lambda: True)
    cfg = {"platform_login": {"enabled": True,
                              "telegram": {"protocol_enabled": True}}}
    rep = pd.readiness_static(cfg)
    assert rep["telegram"]["ready"] is False
    assert any("api_id" in h for h in rep["telegram"]["hints"])


def test_static_overall_requires_sink(monkeypatch):
    monkeypatch.setattr(tpl, "is_pyrogram_available", lambda: True)
    pb.register_inbox_sink(None)  # 无 sink
    cfg = {
        "platform_login": {"enabled": True,
                           "telegram": {"protocol_enabled": True}},
        "telegram": {"api_id": 1, "api_hash": "x"},
    }
    rep = pd.readiness_static(cfg)
    assert rep["telegram"]["ready"] is True
    assert rep["overall_ready"] is False  # sink 未注册 → 整体未就绪


async def test_readiness_whatsapp_reachable(monkeypatch):
    async def _ok(_cfg):
        return True
    monkeypatch.setattr(pd, "check_whatsapp_reachable", _ok)
    pb.register_inbox_sink(lambda m: None)
    try:
        cfg = {"platform_login": {"enabled": True,
                                  "whatsapp": {"protocol_enabled": True}}}
        rep = await pd.readiness(cfg)
        assert rep["whatsapp"]["service_reachable"] is True
        assert rep["whatsapp"]["ready"] is True
        assert rep["overall_ready"] is True
    finally:
        pb.register_inbox_sink(None)


async def test_readiness_whatsapp_unreachable(monkeypatch):
    async def _no(_cfg):
        return False
    monkeypatch.setattr(pd, "check_whatsapp_reachable", _no)
    pb.register_inbox_sink(lambda m: None)
    try:
        cfg = {"platform_login": {"enabled": True,
                                  "whatsapp": {"protocol_enabled": True}}}
        rep = await pd.readiness(cfg)
        assert rep["whatsapp"]["service_reachable"] is False
        assert rep["whatsapp"]["ready"] is False
        assert rep["overall_ready"] is False
        assert any("不可达" in h for h in rep["whatsapp"]["hints"])
    finally:
        pb.register_inbox_sink(None)


def test_format_report_renders():
    rep = pd.readiness_static({})
    text = pd.format_report(rep)
    assert "协议栈整体就绪" in text
    assert "Telegram protocol" in text
    assert "WhatsApp Baileys" in text
