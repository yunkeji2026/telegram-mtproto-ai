"""协议自动回复审计流（Phase 4 ①）。

把每一次「自动回复决策」落一条可观测记录：入站文本 / 生成文本 / 风险 / 决策
（sent | skipped）/ 原因。Web 后台与桌面壳用它渲染「自动回复实时流」面板——
**没有可观测就不敢放量**。

设计与 ``account_registry`` 一致：独立 SQLite（默认 ``config/autoreply_audit.db``），
线程安全，幂等 migration，进程内单例。本模块不依赖 FastAPI，可纯单测。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_DDL = """
CREATE TABLE IF NOT EXISTS autoreply_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL DEFAULT 0,
    platform        TEXT NOT NULL DEFAULT '',
    account_id      TEXT NOT NULL DEFAULT '',
    chat_key        TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL DEFAULT '',
    inbound         TEXT NOT NULL DEFAULT '',
    reply           TEXT NOT NULL DEFAULT '',
    risk            TEXT NOT NULL DEFAULT '',
    decision        TEXT NOT NULL DEFAULT '',
    reason          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ara_ts ON autoreply_audit(ts);
CREATE INDEX IF NOT EXISTS idx_ara_acct ON autoreply_audit(platform, account_id, ts);
CREATE TABLE IF NOT EXISTS autoreply_config_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL DEFAULT 0,
    actor       TEXT NOT NULL DEFAULT '',
    scope       TEXT NOT NULL DEFAULT '',
    platform    TEXT NOT NULL DEFAULT '',
    account_id  TEXT NOT NULL DEFAULT '',
    changes     TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_aca_ts ON autoreply_config_audit(ts);
"""

_MIGRATIONS: List[str] = []

# 防止表无限增长：record 时偶发裁剪到最近 N 条
_MAX_ROWS = 5000


class AutoReplyAudit:
    """自动回复审计存储（线程安全 SQLite 封装）。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False, timeout=10
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._writes = 0
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

    def record(
        self,
        *,
        platform: str,
        account_id: str,
        chat_key: str = "",
        conversation_id: str = "",
        inbound: str = "",
        reply: str = "",
        risk: str = "",
        decision: str = "",
        reason: str = "",
        ts: Optional[float] = None,
    ) -> int:
        now = ts if ts is not None else time.time()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO autoreply_audit
                   (ts, platform, account_id, chat_key, conversation_id,
                    inbound, reply, risk, decision, reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (now, str(platform or "").lower(), str(account_id or ""),
                 str(chat_key or ""), str(conversation_id or ""),
                 str(inbound or "")[:2000], str(reply or "")[:2000],
                 str(risk or ""), str(decision or ""), str(reason or "")),
            )
            self._conn.commit()
            self._writes += 1
            if self._writes % 200 == 0:
                self._trim_locked()
            rid = int(cur.lastrowid or 0)
        _publish({
            "id": rid, "ts": now,
            "platform": str(platform or "").lower(),
            "account_id": str(account_id or ""),
            "chat_key": str(chat_key or ""),
            "conversation_id": str(conversation_id or ""),
            "inbound": str(inbound or "")[:2000],
            "reply": str(reply or "")[:2000],
            "risk": str(risk or ""), "decision": str(decision or ""),
            "reason": str(reason or ""),
        })
        return rid

    def _trim_locked(self) -> None:
        try:
            self._conn.execute(
                """DELETE FROM autoreply_audit WHERE id NOT IN
                   (SELECT id FROM autoreply_audit ORDER BY id DESC LIMIT ?)""",
                (_MAX_ROWS,),
            )
            self._conn.commit()
        except Exception:
            pass

    def recent(
        self, *, limit: int = 50,
        platform: Optional[str] = None, account_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        q = "SELECT * FROM autoreply_audit WHERE 1=1"
        args: List[Any] = []
        if platform:
            q += " AND platform=?"
            args.append(str(platform).lower())
        if account_id:
            q += " AND account_id=?"
            args.append(str(account_id))
        q += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit or 50), 500)))
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def stats(self, *, since_ts: float = 0) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT decision, reason, COUNT(*) AS n FROM autoreply_audit
                   WHERE ts >= ? GROUP BY decision, reason""",
                (float(since_ts or 0),),
            ).fetchall()
        sent = 0
        skipped = 0
        by_reason: Dict[str, int] = {}
        for r in rows:
            n = int(r["n"])
            if r["decision"] == "sent":
                sent += n
            else:
                skipped += n
            by_reason[str(r["reason"] or "")] = by_reason.get(
                str(r["reason"] or ""), 0) + n
        return {"sent": sent, "skipped": skipped, "by_reason": by_reason}


    # ── 配置变更审计（Phase 9）──────────────────────────────────────────
    def record_config_change(
        self, *, actor: str, scope: str,
        platform: str = "", account_id: str = "",
        changes: Optional[List[Dict[str, Any]]] = None,
        ts: Optional[float] = None,
    ) -> int:
        now = ts if ts is not None else time.time()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO autoreply_config_audit
                   (ts, actor, scope, platform, account_id, changes)
                   VALUES (?,?,?,?,?,?)""",
                (now, str(actor or ""), str(scope or ""),
                 str(platform or ""), str(account_id or ""),
                 json.dumps(changes or [], ensure_ascii=False)),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def recent_config_changes(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM autoreply_config_audit ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit or 50), 500)),),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["changes"] = json.loads(d.get("changes") or "[]")
            except Exception:
                d["changes"] = []
            out.append(d)
        return out


# ── 进程内事件总线（Phase 10：record() → SSE 零延迟推送）────────────────
_subscribers: "set" = set()  # 元素：(loop, asyncio.Queue)
_sub_lock = threading.Lock()


def subscribe() -> "asyncio.Queue":
    """SSE 端点订阅新决策事件。返回的队列由调用方在结束时 unsubscribe。"""
    loop = asyncio.get_event_loop()
    q: "asyncio.Queue" = asyncio.Queue(maxsize=2000)
    with _sub_lock:
        _subscribers.add((loop, q))
    return q


def unsubscribe(q: "asyncio.Queue") -> None:
    with _sub_lock:
        for item in list(_subscribers):
            if item[1] is q:
                _subscribers.discard(item)


def subscriber_count() -> int:
    with _sub_lock:
        return len(_subscribers)


def _safe_put(q: "asyncio.Queue", row: Dict[str, Any]) -> None:
    try:
        q.put_nowait(row)
    except asyncio.QueueFull:
        try:  # 满则丢最旧，保最新（SSE 实时性优先）
            q.get_nowait()
            q.put_nowait(row)
        except Exception:
            pass


def _publish(row: Dict[str, Any]) -> None:
    """把一条审计事件投递给所有订阅者（跨线程安全）。"""
    with _sub_lock:
        subs = list(_subscribers)
    for loop, q in subs:
        try:
            loop.call_soon_threadsafe(_safe_put, q, row)
        except Exception:
            pass


_audit: Optional[AutoReplyAudit] = None
_audit_lock = threading.Lock()


def get_autoreply_audit(db_path: Optional[Path] = None) -> AutoReplyAudit:
    """进程内单例。默认 ``config/autoreply_audit.db``。"""
    global _audit
    if _audit is None:
        with _audit_lock:
            if _audit is None:
                path = Path(db_path) if db_path else Path("config/autoreply_audit.db")
                _audit = AutoReplyAudit(path)
    return _audit
