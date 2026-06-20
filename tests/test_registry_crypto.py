"""Phase N3：account_registry meta 敏感字段静态加密单测。

覆盖：加解密往返 / 非敏感字段不动 / 旧明文透传 / 幂等不重复加密 / 换钥解密失败置空 /
密钥文件自动生成 / registry 端到端（密文落盘、读出透明解密）。
"""
import sqlite3

import pytest
from cryptography.fernet import Fernet

from src.integrations import registry_crypto as rc


@pytest.fixture
def key_env(monkeypatch):
    monkeypatch.setenv("ACCOUNT_REGISTRY_KEY", Fernet.generate_key().decode())
    rc.reset_cache()
    yield
    rc.reset_cache()


def test_encrypt_then_decrypt_roundtrip(key_env):
    meta = {"session_string": "SECRET", "phone": "138", "session_name": "x"}
    enc = rc.encrypt_meta(meta)
    assert enc["session_string"].startswith("enc:v1:")
    assert enc["phone"] == "138"          # 非敏感字段原样
    assert enc["session_name"] == "x"
    assert rc.decrypt_meta(enc)["session_string"] == "SECRET"


def test_decrypt_plaintext_passthrough(key_env):
    # N2 已写的旧明文（无前缀）原样读出，不破
    assert rc.decrypt_meta({"session_string": "OLD"})["session_string"] == "OLD"


def test_encrypt_idempotent(key_env):
    once = rc.encrypt_meta({"session_string": "S"})
    twice = rc.encrypt_meta(once)
    assert once["session_string"] == twice["session_string"]  # 已加密不重复套娃


def test_decrypt_wrong_key_blanks(monkeypatch):
    monkeypatch.setenv("ACCOUNT_REGISTRY_KEY", Fernet.generate_key().decode())
    rc.reset_cache()
    enc = rc.encrypt_meta({"session_string": "S"})
    # 换一把钥 → 解不开 → 置空（回落文件 session/重新扫码，不喂 garbage）
    monkeypatch.setenv("ACCOUNT_REGISTRY_KEY", Fernet.generate_key().decode())
    rc.reset_cache()
    assert rc.decrypt_meta(enc)["session_string"] == ""
    rc.reset_cache()


def test_key_file_autogen(monkeypatch, tmp_path):
    monkeypatch.delenv("ACCOUNT_REGISTRY_KEY", raising=False)
    monkeypatch.setenv("ACCOUNT_REGISTRY_KEY_FILE", str(tmp_path / "reg.key"))
    rc.reset_cache()
    enc = rc.encrypt_meta({"session_string": "S"})
    assert enc["session_string"].startswith("enc:v1:")
    assert (tmp_path / "reg.key").exists()
    rc.reset_cache()


def test_empty_meta_safe(key_env):
    assert rc.encrypt_meta(None) == {}
    assert rc.decrypt_meta({}) == {}


def test_registry_roundtrip_encrypted(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCOUNT_REGISTRY_KEY", Fernet.generate_key().decode())
    rc.reset_cache()
    from src.integrations.account_registry import AccountRegistry
    db = tmp_path / "acc.db"
    reg = AccountRegistry(db)
    reg.upsert("telegram", "123", mode="protocol",
               meta={"session_string": "SS_SECRET", "session_name": "f"})
    # 读出透明解密
    row = reg.get("telegram", "123")
    assert row["meta"]["session_string"] == "SS_SECRET"
    assert row["meta"]["session_name"] == "f"
    # 底层确认密文落盘（明文不出现）
    raw = sqlite3.connect(str(db)).execute(
        "SELECT meta_json FROM platform_accounts").fetchone()[0]
    assert "SS_SECRET" not in raw and "enc:v1:" in raw
    rc.reset_cache()


def test_registry_update_preserves_encryption(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCOUNT_REGISTRY_KEY", Fernet.generate_key().decode())
    rc.reset_cache()
    from src.integrations.account_registry import AccountRegistry
    reg = AccountRegistry(tmp_path / "acc.db")
    reg.upsert("telegram", "9", mode="protocol", meta={"session_string": "A"})
    # 只改状态（meta=None）→ 不动既有密文，读出仍能解
    reg.set_status("telegram", "9", "online")
    assert reg.get("telegram", "9")["meta"]["session_string"] == "A"
    rc.reset_cache()
