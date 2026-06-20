"""Phase O2：主动关怀待办持久层（SQLite）。

O1 抽取出「到期关怀约定」后落这张 ``care_schedule`` 表，O3 到点消费、O4 后台可见化。
把 O1 的「宁滥」在入库层收敛为「宁缺」：**置信度阈值 + 同主题去重 + 过期清理**。

状态机：``pending → sent | skipped | expired | cancelled``。
隐私：只存短摘要（≤160 字）。纯存储、平台无关、可单测（``:memory:``）。
默认关：是否喂入抽取由上层 ``companion.proactive_care.enabled`` 控（O3 接线时引）。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.contacts.care_commitment import CareCommitment, extract_commitments

logger = logging.getLogger("CareScheduleStore")

_DEFAULT_MIN_CONFIDENCE = 0.6   # 挡住 O1 无主题兜底（0.5）等低质项
_DEFAULT_DEDUP_WINDOW_DAYS = 3.0
_DAY = 86400.0

_STATUSES = ("pending", "sent", "skipped", "expired", "cancelled")


def _topic_norm(topic: str) -> str:
    return (topic or "").strip().lower()[:32]


class CareScheduleStore:
    _DDL = """
    CREATE TABLE IF NOT EXISTS care_schedule (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_key TEXT NOT NULL,
        platform TEXT NOT NULL DEFAULT '',
        account_id TEXT NOT NULL DEFAULT '',
        chat_key TEXT NOT NULL DEFAULT '',
        due_at REAL NOT NULL,
        event_at REAL NOT NULL DEFAULT 0,
        topic TEXT NOT NULL DEFAULT '',
        topic_norm TEXT NOT NULL DEFAULT '',
        sentiment TEXT NOT NULL DEFAULT 'neutral',
        source_text TEXT NOT NULL DEFAULT '',
        confidence REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        sent_at REAL,
        note TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_care_due ON care_schedule(status, due_at);
    CREATE INDEX IF NOT EXISTS idx_care_contact ON care_schedule(contact_key, status);
    """

    def __init__(self, db_path):
        self._db_path = db_path if db_path == ":memory:" else Path(db_path)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self) -> None:
        if self._db_path != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path) if self._db_path != ":memory:" else ":memory:",
            check_same_thread=False,
        )
        self._conn.executescript(self._DDL)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    # ── 写入 ────────────────────────────────────────────────────────────
    def add_commitment(
        self,
        commitment: CareCommitment,
        *,
        contact_key: str,
        platform: str = "",
        account_id: str = "",
        chat_key: str = "",
        min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
        dedup_window_days: float = _DEFAULT_DEDUP_WINDOW_DAYS,
    ) -> Optional[int]:
        """落一条关怀待办；置信度不足或近窗同主题已有 pending → 跳过返回 None（绝不抛）。"""
        if commitment.confidence < float(min_confidence):
            return None
        tnorm = _topic_norm(commitment.topic)
        win = float(dedup_window_days) * _DAY
        now = time.time()
        try:
            with self._lock:
                # 去重：同 contact + 同主题 + due 邻近窗口内已有 pending → 不重复
                dup = self._conn.execute(
                    "SELECT id FROM care_schedule WHERE contact_key = ? AND topic_norm = ?"
                    " AND status = 'pending' AND ABS(due_at - ?) <= ? LIMIT 1",
                    (str(contact_key), tnorm, float(commitment.due_at), win),
                ).fetchone()
                if dup:
                    return None
                cur = self._conn.execute(
                    "INSERT INTO care_schedule (contact_key, platform, account_id, chat_key,"
                    " due_at, event_at, topic, topic_norm, sentiment, source_text, confidence,"
                    " status, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (
                        str(contact_key), str(platform), str(account_id), str(chat_key),
                        float(commitment.due_at), float(commitment.event_at),
                        str(commitment.topic)[:80], tnorm, str(commitment.sentiment)[:16],
                        str(commitment.source_text or "")[:160],
                        float(commitment.confidence), now, now,
                    ),
                )
                self._conn.commit()
                return int(cur.lastrowid) if cur.lastrowid else None
        except Exception as e:  # noqa: BLE001
            logger.debug("care_schedule add failed: %s", e)
            return None

    def add_from_text(
        self,
        text: str,
        *,
        contact_key: str,
        platform: str = "",
        account_id: str = "",
        chat_key: str = "",
        now: Optional[float] = None,
        min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
        dedup_window_days: float = _DEFAULT_DEDUP_WINDOW_DAYS,
    ) -> List[int]:
        """便捷接线：抽取一条消息 → 入库。返回新增的 id 列表（去重/低分项不计）。"""
        ids: List[int] = []
        for c in extract_commitments(text, now=now):
            rid = self.add_commitment(
                c, contact_key=contact_key, platform=platform,
                account_id=account_id, chat_key=chat_key,
                min_confidence=min_confidence, dedup_window_days=dedup_window_days)
            if rid:
                ids.append(rid)
        return ids

    # ── 查询 ────────────────────────────────────────────────────────────
    _COLS = [
        "id", "contact_key", "platform", "account_id", "chat_key", "due_at", "event_at",
        "topic", "topic_norm", "sentiment", "source_text", "confidence", "status",
        "created_at", "updated_at", "sent_at", "note",
    ]

    def _rows(self, where: str, params: list, limit: int) -> List[Dict[str, Any]]:
        try:
            rows = self._conn.execute(
                f"SELECT {', '.join(self._COLS)} FROM care_schedule{where}"
                " ORDER BY due_at ASC LIMIT ?",
                (*params, int(limit)),
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug("care_schedule query failed: %s", e)
            return []
        return [dict(zip(self._COLS, r)) for r in rows]

    def list_due(self, now: Optional[float] = None, *, limit: int = 100) -> List[Dict[str, Any]]:
        """到期且仍 pending 的待办（due_at <= now），按 due_at 升序。"""
        n = float(now if now is not None else time.time())
        return self._rows(" WHERE status = 'pending' AND due_at <= ?", [n],
                          max(1, min(int(limit), 500)))

    def list_pending(self, *, limit: int = 200) -> List[Dict[str, Any]]:
        return self._rows(" WHERE status = 'pending'", [], max(1, min(int(limit), 500)))

    def list_recent(self, *, status: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        if status:
            return self._rows(" WHERE status = ?", [str(status)], max(1, min(int(limit), 500)))
        return self._rows("", [], max(1, min(int(limit), 500)))

    def list_by_contact(self, contact_key: str, *, status: str = "",
                        limit: int = 100) -> List[Dict[str, Any]]:
        """某联系人的关怀待办（P 线健康卡用）。status 空=全部状态。"""
        if status:
            return self._rows(" WHERE contact_key = ? AND status = ?",
                            [str(contact_key), str(status)], max(1, min(int(limit), 500)))
        return self._rows(" WHERE contact_key = ?", [str(contact_key)],
                        max(1, min(int(limit), 500)))

    def count_pending_by_contact(self, contact_key: str) -> int:
        """某联系人当前 pending 关怀数（健康卡 pending_care 信号）。"""
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM care_schedule WHERE contact_key = ? AND status = 'pending'",
                (str(contact_key),),
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def pending_counts_by_contacts(self, contact_keys) -> Dict[str, int]:
        """批量取多个联系人的 pending 关怀数（避免健康榜 N+1 查询）。"""
        keys = [str(k) for k in (contact_keys or []) if str(k)]
        if not keys:
            return {}
        out: Dict[str, int] = {}
        try:
            # 分块 IN 查询（SQLite 变量上限保守取 500）
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                ph = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT contact_key, COUNT(*) FROM care_schedule"
                    f" WHERE status = 'pending' AND contact_key IN ({ph})"
                    f" GROUP BY contact_key",
                    chunk,
                ).fetchall()
                for r in rows:
                    out[str(r[0])] = int(r[1])
        except Exception as e:  # noqa: BLE001
            logger.debug("pending_counts_by_contacts failed: %s", e)
        return out

    # ── 状态流转 ────────────────────────────────────────────────────────
    def _set_status(self, sid: int, status: str, *, note: str = "",
                    set_sent_at: bool = False) -> bool:
        if status not in _STATUSES:
            return False
        try:
            with self._lock:
                if set_sent_at:
                    cur = self._conn.execute(
                        "UPDATE care_schedule SET status = ?, note = ?, updated_at = ?,"
                        " sent_at = ? WHERE id = ? AND status = 'pending'",
                        (status, str(note)[:300], time.time(), time.time(), int(sid)),
                    )
                else:
                    cur = self._conn.execute(
                        "UPDATE care_schedule SET status = ?, note = ?, updated_at = ?"
                        " WHERE id = ? AND status = 'pending'",
                        (status, str(note)[:300], time.time(), int(sid)),
                    )
                self._conn.commit()
                return bool(cur.rowcount)
        except Exception as e:  # noqa: BLE001
            logger.debug("care_schedule set_status failed: %s", e)
            return False

    def mark_sent(self, sid: int, *, note: str = "") -> bool:
        return self._set_status(sid, "sent", note=note, set_sent_at=True)

    def mark_skipped(self, sid: int, *, note: str = "") -> bool:
        return self._set_status(sid, "skipped", note=note)

    def cancel(self, sid: int, *, note: str = "") -> bool:
        return self._set_status(sid, "cancelled", note=note)

    def bring_forward(self, sid: int, *, now: Optional[float] = None) -> bool:
        """把一条 pending 待办的 due_at 提前到 now（运营「立即发」）→ 下个派发 tick 即到期。"""
        n = float(now if now is not None else time.time())
        try:
            with self._lock:
                cur = self._conn.execute(
                    "UPDATE care_schedule SET due_at = ?, updated_at = ?"
                    " WHERE id = ? AND status = 'pending'",
                    (n, n, int(sid)),
                )
                self._conn.commit()
                return bool(cur.rowcount)
        except Exception as e:  # noqa: BLE001
            logger.debug("care_schedule bring_forward failed: %s", e)
            return False

    def expire_overdue(self, now: Optional[float] = None, *, grace_days: float = 1.0) -> int:
        """把逾期太久仍 pending 的待办标 expired（错过关怀时机，不再补发）。返回数量。"""
        n = float(now if now is not None else time.time())
        cutoff = n - float(grace_days) * _DAY
        try:
            with self._lock:
                cur = self._conn.execute(
                    "UPDATE care_schedule SET status = 'expired', updated_at = ?"
                    " WHERE status = 'pending' AND due_at < ?",
                    (time.time(), cutoff),
                )
                self._conn.commit()
                return int(cur.rowcount)
        except Exception as e:  # noqa: BLE001
            logger.debug("care_schedule expire failed: %s", e)
            return 0

    def count(self, *, status: str = "") -> int:
        try:
            if status:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM care_schedule WHERE status = ?", (str(status),)
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) FROM care_schedule").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0


_singleton: Optional["CareScheduleStore"] = None
_singleton_lock = threading.Lock()


def get_care_schedule_store(db_path=None) -> "CareScheduleStore":
    """进程内单例。首次调用传入 db_path 落库位置；之后忽略入参返回同一实例。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = CareScheduleStore(db_path or ":memory:")
    return _singleton


__all__ = ["CareScheduleStore", "get_care_schedule_store", "_topic_norm"]
