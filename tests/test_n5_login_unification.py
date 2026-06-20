"""N5 登录注册统一：A 线 config 账号并入 B 线持久注册表（与 QR 共用一张表）。

覆盖：新账号写入（mode=protocol/pending/meta）、幂等重入、**不破坏 QR 既有登录态**
（session_string/online/mode 保留）、config 静态属性（label/proxy）刷新、default 取舍、
registry 缺失兜底。
"""

from __future__ import annotations

import pytest

from src.client.telegram_account_registry import TelegramAccountRegistry
from src.integrations.account_registry import AccountRegistry


@pytest.fixture
def registry(tmp_path):
    return AccountRegistry(tmp_path / "acc.db")


def _tg_cfg_two():
    return {
        "accounts": [
            {"id": "acc_a", "label": "号A", "api_id": 1, "api_hash": "h",
             "phone_number": "+8613800000000", "session_name": "cam_a",
             "persona_ids": ["warm"], "proxy_id": "p1", "enabled": True},
            {"id": "acc_b", "label": "号B", "api_id": 2, "api_hash": "h2",
             "phone_number": "+8613811111111", "session_name": "cam_b",
             "enabled": True},
        ]
    }


# ── 新账号写入 ──────────────────────────────────────────────────────────

def test_sync_new_accounts_written(registry):
    tg = TelegramAccountRegistry.from_config(_tg_cfg_two())
    synced = tg.sync_to_account_registry(registry)
    assert set(synced) == {"acc_a", "acc_b"}
    a = registry.get("telegram", "acc_a")
    assert a["mode"] == "protocol"
    assert a["status"] == "pending"
    assert a["label"] == "号A"
    assert a["proxy_id"] == "p1"
    assert a["meta"]["session_name"] == "cam_a"
    assert a["meta"]["phone_number"] == "+8613800000000"
    assert a["meta"]["persona_ids"] == ["warm"]
    assert a["meta"]["config_synced"] is True


def test_sync_idempotent(registry):
    tg = TelegramAccountRegistry.from_config(_tg_cfg_two())
    tg.sync_to_account_registry(registry)
    tg.sync_to_account_registry(registry)  # 再来一次
    rows = registry.list("telegram")
    assert len(rows) == 2  # 不重复


# ── 不破坏 QR 既有登录态（核心安全保证）─────────────────────────────────

def test_sync_preserves_qr_session_and_online(registry):
    # 模拟 acc_a 已通过 QR 登录：有 session_string、online、protocol
    registry.upsert(
        "telegram", "acc_a", mode="protocol", status="online",
        meta={"session_string": "SECRET_SESSION", "source": "qr"},
    )
    tg = TelegramAccountRegistry.from_config(_tg_cfg_two())
    tg.sync_to_account_registry(registry)
    a = registry.get("telegram", "acc_a")
    # 会话凭据与在线态绝不能被同步打翻
    assert a["meta"]["session_string"] == "SECRET_SESSION"
    assert a["status"] == "online"
    assert a["mode"] == "protocol"
    # config 静态身份叠加进去、原有 meta 不丢
    assert a["meta"]["session_name"] == "cam_a"
    assert a["meta"]["source"] == "qr"
    assert a["meta"]["config_synced"] is True
    assert a["label"] == "号A"


def test_sync_refreshes_proxy_from_config(registry):
    registry.upsert("telegram", "acc_a", proxy_id="old_proxy",
                    meta={"session_string": "S"})
    tg = TelegramAccountRegistry.from_config(_tg_cfg_two())
    tg.sync_to_account_registry(registry)
    a = registry.get("telegram", "acc_a")
    assert a["proxy_id"] == "p1"  # 以 config 为准
    assert a["meta"]["session_string"] == "S"  # 仍不丢凭据


def test_sync_clears_proxy_when_config_drops_it(registry):
    registry.upsert("telegram", "acc_b", proxy_id="had_proxy")
    tg = TelegramAccountRegistry.from_config(_tg_cfg_two())  # acc_b 无 proxy_id
    tg.sync_to_account_registry(registry)
    assert registry.get("telegram", "acc_b")["proxy_id"] == ""


# ── default 取舍 + 兜底 ─────────────────────────────────────────────────

def test_sync_include_default_toggle(registry):
    tg = TelegramAccountRegistry.from_config(
        {"api_id": 1, "api_hash": "h", "phone_number": "+1", "session_name": "s"}
    )  # 单账号回退 → default
    assert tg.sync_to_account_registry(registry, include_default=False) == []
    assert registry.list("telegram") == []
    synced = tg.sync_to_account_registry(registry, include_default=True)
    assert synced == ["default"]


def test_sync_none_registry_safe():
    tg = TelegramAccountRegistry.from_config(_tg_cfg_two())
    assert tg.sync_to_account_registry(None) == []
