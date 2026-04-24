"""SQLite-backed audit log with auto-migration from legacy JSONL."""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import List, Dict, Optional


class AuditStore:

    _DDL = """
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        user_id TEXT NOT NULL,
        action TEXT NOT NULL,
        target TEXT NOT NULL DEFAULT '',
        old_val TEXT NOT NULL DEFAULT '',
        new_val TEXT NOT NULL DEFAULT '',
        snapshot_id TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
    CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
    """

    def __init__(self, db_path: Path, legacy_jsonl_path: Optional[Path] = None,
                 webhook_notifier=None):
        self._db_path = db_path
        self._legacy_jsonl = legacy_jsonl_path
        self._webhook = webhook_notifier
        self._logger = logging.getLogger("AuditStore")
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self):
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(self._DDL)
        self._conn.commit()
        self._migrate_jsonl()

    def _migrate_jsonl(self):
        if not self._legacy_jsonl or not self._legacy_jsonl.exists():
            return
        count = self._conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        if count > 0:
            return
        migrated = 0
        try:
            with open(self._legacy_jsonl, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                        self._conn.execute(
                            "INSERT INTO audit_log (ts, user_id, action, target, old_val, new_val, snapshot_id) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (e.get("ts", ""), str(e.get("user", "")), e.get("action", ""),
                             e.get("target", ""), e.get("old", ""), e.get("new", ""),
                             e.get("snap", "")),
                        )
                        migrated += 1
                    except (json.JSONDecodeError, KeyError):
                        continue
            self._conn.commit()
            if migrated > 0:
                backup = self._legacy_jsonl.with_suffix(".jsonl.bak")
                self._legacy_jsonl.rename(backup)
                self._logger.info("已迁移 %d 条 JSONL 审计记录到 SQLite，原文件备份为 %s", migrated, backup.name)
        except Exception as e:
            self._logger.warning("JSONL 迁移失败: %s", e)

    def log(self, user_id: str, action: str, target: str = "",
            old_val: str = "", new_val: str = "", snapshot_id: str = ""):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            self._conn.execute(
                "INSERT INTO audit_log (ts, user_id, action, target, old_val, new_val, snapshot_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, str(user_id), action, target, old_val, new_val, snapshot_id),
            )
            self._conn.commit()
        except Exception as e:
            self._logger.warning("审计写入失败: %s", e)
        self._logger.info("[配置审计] %s %s by %s", action, target, user_id)
        if self._webhook and getattr(self._webhook, "enabled", False):
            self._webhook.notify("config_change", {
                "action": action, "target": target, "user_id": user_id,
                "old_val": old_val, "new_val": new_val,
            })

    def query(self, limit: int = 50, action: str = "", user_id: str = "",
              since: str = "", keyword: str = "") -> List[Dict]:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        params: list = []
        if action:
            sql += " AND action = ?"
            params.append(action)
        if user_id:
            sql += " AND user_id = ?"
            params.append(str(user_id))
        if since:
            sql += " AND ts >= ?"
            params.append(since)
        if keyword:
            sql += " AND (action LIKE ? OR target LIKE ? OR old_val LIKE ? OR new_val LIKE ?)"
            like = f"%{keyword}%"
            params.extend([like, like, like, like])
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        try:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in reversed(rows)]
        except Exception:
            return []

    def last_entry(self) -> Optional[Dict]:
        try:
            row = self._conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    def cleanup(self, keep_days: int = 90, max_rows: int = 50000):
        """归档清理：删除超期记录，并在总行数超限时进一步清理最早的记录"""
        try:
            cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(time.time() - keep_days * 86400))
            cur = self._conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
            deleted_age = cur.rowcount
            self._conn.commit()

            count = self._conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
            deleted_overflow = 0
            if count > max_rows:
                excess = count - max_rows
                self._conn.execute(
                    "DELETE FROM audit_log WHERE id IN "
                    "(SELECT id FROM audit_log ORDER BY id ASC LIMIT ?)",
                    (excess,),
                )
                deleted_overflow = excess
                self._conn.commit()

            total = deleted_age + deleted_overflow
            if total > 0:
                self._conn.execute("VACUUM")
                self._logger.info(
                    "审计清理: 过期删除 %d + 溢出删除 %d = %d 条 (保留 %d 天 / %d 行上限)",
                    deleted_age, deleted_overflow, total, keep_days, max_rows,
                )
            return total
        except Exception as e:
            self._logger.warning("审计清理失败: %s", e)
            return 0

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
