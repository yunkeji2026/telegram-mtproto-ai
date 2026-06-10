"""代理池（M4）。

「一号一代理」是多账号防关联的核心之一。本模块提供一个**用户自填**的代理池：
代理资源来源留空（不内置任何代理），由运营在「账号管理 → 新增账号 → 代理配置」里
逐条录入（或导入），登录新账号时绑定一条代理，持久化到账号注册表的 ``proxy_id``。

存储：独立 SQLite（默认 ``config/proxy_pool.db``），线程安全，与 ``account_registry`` 同风格。
"""

from __future__ import annotations

import asyncio
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_DDL = """
CREATE TABLE IF NOT EXISTS proxies (
    proxy_id        TEXT PRIMARY KEY,
    label           TEXT NOT NULL DEFAULT '',
    scheme          TEXT NOT NULL DEFAULT 'socks5',
    host            TEXT NOT NULL DEFAULT '',
    port            INTEGER NOT NULL DEFAULT 0,
    username        TEXT NOT NULL DEFAULT '',
    password        TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'unknown',
    assigned_account TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL DEFAULT 0,
    last_checked_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_proxies_status ON proxies(status);
"""

VALID_SCHEMES = ("http", "https", "socks4", "socks5")


def _proxy_url(scheme: str, host: str, port: int, username: str, password: str) -> str:
    auth = ""
    if username:
        auth = username + (f":{password}" if password else "") + "@"
    return f"{scheme}://{auth}{host}:{port}"


class ProxyPool:
    """用户自填的代理池（线程安全 SQLite 封装）。"""

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
            self._conn.commit()

    @staticmethod
    def _row(row: sqlite3.Row, *, mask: bool = True) -> Dict[str, Any]:
        d = dict(row)
        d["url"] = _proxy_url(
            d["scheme"], d["host"], d["port"], d["username"], d["password"])
        if mask:
            d["password"] = "******" if d.get("password") else ""
            if d.get("username"):
                d["url"] = _proxy_url(
                    d["scheme"], d["host"], d["port"], d["username"], "******")
        return d

    def add(
        self,
        *,
        scheme: str = "socks5",
        host: str = "",
        port: int = 0,
        username: str = "",
        password: str = "",
        label: str = "",
    ) -> Dict[str, Any]:
        scheme = str(scheme or "socks5").lower()
        if scheme not in VALID_SCHEMES:
            raise ValueError(f"不支持的代理协议: {scheme}")
        if not host or not port:
            raise ValueError("host 与 port 必填")
        pid = "px_" + secrets.token_hex(5)
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO proxies
                   (proxy_id, label, scheme, host, port, username, password,
                    status, assigned_account, created_at, updated_at, last_checked_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, label, scheme, host, int(port), username, password,
                 "unknown", "", now, now, 0),
            )
            self._conn.commit()
        return self.get(pid, mask=True) or {}

    def get(self, proxy_id: str, *, mask: bool = True) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM proxies WHERE proxy_id=?", (proxy_id,)
            ).fetchone()
        return self._row(row, mask=mask) if row else None

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM proxies ORDER BY created_at"
            ).fetchall()
        return [self._row(r, mask=True) for r in rows]

    def remove(self, proxy_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM proxies WHERE proxy_id=?", (proxy_id,))
            self._conn.commit()

    def assign(self, proxy_id: str, account_key: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE proxies SET assigned_account=?, updated_at=? WHERE proxy_id=?",
                (account_key, time.time(), proxy_id),
            )
            self._conn.commit()

    def set_status(self, proxy_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE proxies SET status=?, last_checked_at=?, updated_at=? WHERE proxy_id=?",
                (status, time.time(), time.time(), proxy_id),
            )
            self._conn.commit()

    async def test(self, proxy_id: str, timeout: float = 6.0) -> bool:
        """基础可达性探测：对 host:port 做 TCP 连接（不校验代理协议握手）。"""
        entry = self.get(proxy_id, mask=False)
        if not entry:
            return False
        ok = False
        try:
            fut = asyncio.open_connection(entry["host"], int(entry["port"]))
            reader, writer = await asyncio.wait_for(fut, timeout=timeout)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            ok = True
        except Exception:
            ok = False
        self.set_status(proxy_id, "ok" if ok else "fail")
        return ok


_pool: Optional[ProxyPool] = None
_pool_lock = threading.Lock()


def get_proxy_pool(db_path: Optional[Path] = None) -> ProxyPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                path = Path(db_path) if db_path else Path("config/proxy_pool.db")
                _pool = ProxyPool(path)
    return _pool
