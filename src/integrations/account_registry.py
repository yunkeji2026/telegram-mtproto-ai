"""平台账号注册表（M1）。

承载「多登录方式并存」所需的账号持久化：每个账号记住自己的 ``mode``
（protocol / web / device）、绑定的代理与指纹（防关联），供账号池编排器在重启后
用正确的 worker 类型把它拉起。

设计：独立 SQLite（默认 ``config/account_registry.db``），线程安全，幂等 migration
（``executescript(_DDL)`` + ALTER 列表，已存在即忽略），与 ``src/inbox/store.py`` 风格一致。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_DDL = """
CREATE TABLE IF NOT EXISTS platform_accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    account_id      TEXT NOT NULL,
    mode            TEXT NOT NULL DEFAULT 'device',
    label           TEXT NOT NULL DEFAULT '',
    proxy_id        TEXT NOT NULL DEFAULT '',
    fingerprint_id  TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    meta_json       TEXT NOT NULL DEFAULT '{}',
    created_at      REAL NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL DEFAULT 0,
    last_online_at  REAL NOT NULL DEFAULT 0,
    UNIQUE(platform, account_id)
);
CREATE INDEX IF NOT EXISTS idx_platform_accounts_plat
    ON platform_accounts(platform, status);
"""

# 预留 ALTER 迁移位（新增列集中于此，已存在即忽略）
_MIGRATIONS: List[str] = []

VALID_STATUS = ("pending", "online", "offline", "removed")


class AccountRegistry:
    """平台账号注册表（线程安全 SQLite 封装）。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False, timeout=10
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            for _sql in _MIGRATIONS:
                try:
                    self._conn.execute(_sql)
                except Exception:
                    pass
            self._conn.commit()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        try:
            meta = json.loads(d.pop("meta_json", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        # N3：读出时解密 meta 敏感字段（session_string 等）；旧明文行透传不破
        try:
            from src.integrations.registry_crypto import decrypt_meta
            meta = decrypt_meta(meta)
        except Exception:
            pass
        d["meta"] = meta
        return d

    @staticmethod
    def _meta_json(meta: Optional[Dict[str, Any]]) -> str:
        """N3：写盘前加密 meta 敏感字段再 json 序列化（best-effort）。"""
        m = meta or {}
        try:
            from src.integrations.registry_crypto import encrypt_meta
            m = encrypt_meta(m)
        except Exception:
            pass
        return json.dumps(m, ensure_ascii=False)

    def upsert(
        self,
        platform: str,
        account_id: str,
        *,
        mode: Optional[str] = None,
        label: Optional[str] = None,
        proxy_id: Optional[str] = None,
        fingerprint_id: Optional[str] = None,
        status: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """新增或更新账号。**仅覆盖显式传入（非 None）的字段**，其余沿用既有值，
        避免「只想改状态」的调用把 mode/label 等清掉。"""
        platform = str(platform or "").lower()
        account_id = str(account_id or "")
        now = time.time()
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM platform_accounts WHERE platform=? AND account_id=?",
                (platform, account_id),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """INSERT INTO platform_accounts
                       (platform, account_id, mode, label, proxy_id, fingerprint_id,
                        status, meta_json, created_at, updated_at, last_online_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (platform, account_id,
                     mode or "device", label or "", proxy_id or "",
                     fingerprint_id or "", status or "pending",
                     self._meta_json(meta),
                     now, now, now if status == "online" else 0),
                )
            else:
                cur = dict(existing)
                new_mode = mode if mode is not None else cur["mode"]
                new_label = label if label is not None else cur["label"]
                new_proxy = proxy_id if proxy_id is not None else cur["proxy_id"]
                new_fp = (fingerprint_id if fingerprint_id is not None
                          else cur["fingerprint_id"])
                new_status = status if status is not None else cur["status"]
                new_meta = (self._meta_json(meta)
                            if meta is not None else cur["meta_json"])
                last_online = (now if new_status == "online"
                               else cur["last_online_at"])
                self._conn.execute(
                    """UPDATE platform_accounts
                       SET mode=?, label=?, proxy_id=?, fingerprint_id=?, status=?,
                           meta_json=?, updated_at=?, last_online_at=?
                       WHERE platform=? AND account_id=?""",
                    (new_mode, new_label, new_proxy, new_fp, new_status,
                     new_meta, now, last_online, platform, account_id),
                )
            self._conn.commit()
        return self.get(platform, account_id) or {}

    def set_status(self, platform: str, account_id: str, status: str) -> None:
        if status not in VALID_STATUS:
            return
        now = time.time()
        with self._lock:
            self._conn.execute(
                """UPDATE platform_accounts SET status=?, updated_at=?,
                       last_online_at=CASE WHEN ?='online' THEN ? ELSE last_online_at END
                   WHERE platform=? AND account_id=?""",
                (status, now, status, now, str(platform or "").lower(),
                 str(account_id or "")),
            )
            self._conn.commit()

    def get(self, platform: str, account_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM platform_accounts WHERE platform=? AND account_id=?",
                (str(platform or "").lower(), str(account_id or "")),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list(
        self, platform: Optional[str] = None, *, include_removed: bool = False
    ) -> List[Dict[str, Any]]:
        q = "SELECT * FROM platform_accounts WHERE 1=1"
        args: List[Any] = []
        if platform:
            q += " AND platform=?"
            args.append(str(platform).lower())
        if not include_removed:
            q += " AND status != 'removed'"
        q += " ORDER BY platform, created_at"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def remove(self, platform: str, account_id: str) -> None:
        self.set_status(platform, account_id, "removed")


_registry: Optional[AccountRegistry] = None
_registry_lock = threading.Lock()


def get_account_registry(db_path: Optional[Path] = None) -> AccountRegistry:
    """进程内单例。首次调用可指定路径，默认 ``config/account_registry.db``。"""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                path = Path(db_path) if db_path else Path("config/account_registry.db")
                _registry = AccountRegistry(path)
    return _registry
