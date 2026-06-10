"""Phase 7 自动回复全局设置（JSON 覆盖 + 校验 + 合并）单测。"""

from __future__ import annotations

import pytest

from src.integrations import protocol_autoreply_settings as s


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path):
    s.set_store_path(tmp_path / "pa.json")
    yield
    s.set_store_path(tmp_path / "pa.json")  # reset cache


def test_diff_settings_flatten_and_changes():
    before = {"enabled": False, "rate": {"hourly": 30, "daily": 200}}
    after = {"enabled": True, "rate": {"hourly": 50, "daily": 200}, "delay": {"min_sec": 1}}
    ch = s.diff_settings(before, after)
    keys = {c["key"]: (c["old"], c["new"]) for c in ch}
    assert keys["enabled"] == (False, True)
    assert keys["rate.hourly"] == (30, 50)
    assert "rate.daily" not in keys  # 未变化不列出
    assert keys["delay.min_sec"] == (None, 1)  # 新增 → old=None


def test_config_audit_store_record_and_recent(tmp_path):
    from src.integrations.protocol_autoreply_audit import AutoReplyAudit
    store = AutoReplyAudit(tmp_path / "audit.db")
    store.record_config_change(
        actor="boss", scope="global",
        changes=[{"key": "rate.hourly", "old": 30, "new": 50}])
    store.record_config_change(
        actor="agent7", scope="account",
        platform="telegram", account_id="a1",
        changes=[{"key": "rate.daily", "old": None, "new": 100}])
    rows = store.recent_config_changes(limit=10)
    assert len(rows) == 2
    assert rows[0]["actor"] == "agent7"  # 最新在前
    assert rows[0]["scope"] == "account"
    assert rows[0]["changes"][0]["new"] == 100


def test_sanitize_whitelist_and_types():
    out = s.sanitize({
        "enabled": "true",
        "rate": {"hourly": "50", "daily": 999, "bogus": 1},
        "breaker": {"threshold": 3},
        "hours": {"enabled": 1, "start": "9:5", "end": "25:99", "tz_offset": 8},
        "delay": {"min_sec": 1, "max_sec": 5},
        "evil_key": "x",
    })
    assert out["enabled"] is True
    assert out["rate"] == {"hourly": 50, "daily": 999}  # bogus 丢弃
    assert out["breaker"] == {"threshold": 3}
    assert out["hours"]["enabled"] is True
    assert out["hours"]["start"] == "09:05"   # 归一补零
    assert out["hours"]["end"] == "23:59"     # 越界钳制
    assert out["delay"] == {"min_sec": 1, "max_sec": 5}
    assert "evil_key" not in out


def test_save_and_load_roundtrip():
    s.save({"enabled": True, "rate": {"hourly": 10}})
    assert s.load()["enabled"] is True
    assert s.load()["rate"]["hourly"] == 10


def test_save_deep_merges():
    s.save({"rate": {"hourly": 10, "daily": 100}})
    s.save({"rate": {"hourly": 20}})  # 只改 hourly，daily 应保留
    loaded = s.load()
    assert loaded["rate"]["hourly"] == 20
    assert loaded["rate"]["daily"] == 100


def test_effective_overlays_yaml_base():
    base = {"protocol_autoreply": {"enabled": False, "rate": {"hourly": 5, "daily": 50}}}
    s.save({"enabled": True, "rate": {"hourly": 99}})
    eff = s.effective_settings(base)
    assert eff["enabled"] is True          # JSON 覆盖 YAML
    assert eff["rate"]["hourly"] == 99     # JSON 覆盖
    assert eff["rate"]["daily"] == 50      # YAML 基底保留


def test_cfg_with_settings_shape():
    base = {"foo": "bar", "protocol_autoreply": {"enabled": False}}
    s.save({"enabled": True})
    cfg = s.cfg_with_settings(base)
    assert cfg["foo"] == "bar"
    assert cfg["protocol_autoreply"]["enabled"] is True


def test_load_empty_when_no_file():
    assert s.load() == {}
