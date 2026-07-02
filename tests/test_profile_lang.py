"""profile.lang —— 坐席 UI 语言「跟人走」的存储层门禁。

覆盖：
- 新用户默认 lang='' （无偏好→登录不强制语言，沿用 cookie/默认）
- set_lang 仅接受 zh/en，非法值拒绝且不污染已存值
- 既有库二次打开（模拟重启）走 ALTER 迁移幂等、且保留已写入的 lang
"""
import sqlite3

from src.utils.web_user_store import WebUserStore, ROLE_AGENT


def test_new_user_lang_defaults_empty(tmp_path):
    store = WebUserStore(tmp_path / "u.db")
    store.create_user("amy", "secret123", ROLE_AGENT, display_name="Amy")
    u = store.get_user("amy")
    assert u is not None
    assert u.get("lang", "") == ""  # 无偏好


def test_set_lang_roundtrip_and_validation(tmp_path):
    store = WebUserStore(tmp_path / "u.db")
    store.create_user("amy", "secret123", ROLE_AGENT)

    assert store.set_lang("amy", "en") is True
    assert store.get_user("amy")["lang"] == "en"

    # 非法值拒绝，且不覆盖已存的 en
    assert store.set_lang("amy", "fr") is False
    assert store.set_lang("amy", "") is False
    assert store.get_user("amy")["lang"] == "en"

    # 可切回 zh
    assert store.set_lang("amy", "zh") is True
    assert store.get_user("amy")["lang"] == "zh"


def test_migration_idempotent_on_reopen(tmp_path):
    """二次实例化同一 DB（模拟进程重启）：ALTER 迁移幂等、不抛、保留数据。"""
    db = tmp_path / "u.db"
    s1 = WebUserStore(db)
    s1.create_user("amy", "secret123", ROLE_AGENT)
    s1.set_lang("amy", "en")

    s2 = WebUserStore(db)  # 不应因 duplicate column 抛 OperationalError
    assert s2.get_user("amy")["lang"] == "en"


def test_legacy_db_without_lang_column_gets_migrated(tmp_path):
    """模拟「升级前」的旧库（web_users 无 lang 列）：打开 store 应自动补列，不报错。"""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE web_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            pw_salt BLOB NOT NULL,
            pw_hash BLOB NOT NULL,
            role TEXT NOT NULL DEFAULT 'viewer',
            display_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            last_login TEXT,
            enabled INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    conn.execute(
        "INSERT INTO web_users(username,pw_salt,pw_hash,role,display_name,created_at) "
        "VALUES('legacy',x'00',x'00','agent','Legacy','2026-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()

    store = WebUserStore(db)  # 触发迁移
    assert store.set_lang("legacy", "en") is True
    assert store.get_user("legacy")["lang"] == "en"
