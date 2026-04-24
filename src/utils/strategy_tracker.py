"""策略 A/B 效果追踪器 — SQLite 记录每次策略回复的效果指标

核心设计:
  - 每次机器人回复时写入一条 strategy_event
  - 当同一用户在同一对话中于 FOLLOW_UP_WINDOW 内再次发送消息时,
    回填 (backfill) 上一条事件的 follow_up / follow_up_intent 字段
  - 提供聚合查询: 各策略的回复数、平均响应时间、追问率、同意图追问率
  - 会话追踪: 同一 user+chat 在 SESSION_GAP 内的连续交互归为一个 session
"""

import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("StrategyTracker")

FOLLOW_UP_WINDOW_SEC = 300  # 5 min
SESSION_GAP_SEC = 1800      # 30 min — 超过此间隔视为新会话


class StrategyTracker:

    _DDL = """
    CREATE TABLE IF NOT EXISTS strategy_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        ts_epoch REAL NOT NULL,
        strategy_id TEXT NOT NULL,
        intent TEXT NOT NULL,
        user_id TEXT NOT NULL,
        chat_id INTEGER NOT NULL DEFAULT 0,
        response_ms INTEGER NOT NULL DEFAULT 0,
        used_ai INTEGER NOT NULL DEFAULT 1,
        follow_up INTEGER DEFAULT NULL,
        follow_up_intent TEXT DEFAULT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_se_ts ON strategy_events(ts_epoch);
    CREATE INDEX IF NOT EXISTS idx_se_strategy ON strategy_events(strategy_id);
    CREATE INDEX IF NOT EXISTS idx_se_user_chat ON strategy_events(user_id, chat_id);
    """

    _MIGRATION_SESSION = (
        "ALTER TABLE strategy_events ADD COLUMN session_id TEXT NOT NULL DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS idx_se_session ON strategy_events(session_id)",
    )
    _MIGRATION_MODEL = (
        "ALTER TABLE strategy_events ADD COLUMN model_id TEXT NOT NULL DEFAULT ''",
    )

    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self._DDL)
        self._conn.commit()
        self._migrate()

    def _migrate(self):
        """增量 schema 迁移"""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(strategy_events)").fetchall()}
        if "session_id" not in cols:
            for sql in self._MIGRATION_SESSION:
                try:
                    self._conn.execute(sql)
                except sqlite3.OperationalError:
                    pass
            self._conn.commit()
            logger.info("strategy_events 表已迁移: +session_id")
        if "model_id" not in cols:
            for sql in self._MIGRATION_MODEL:
                try:
                    self._conn.execute(sql)
                except sqlite3.OperationalError:
                    pass
            self._conn.commit()
            logger.info("strategy_events 表已迁移: +model_id")

    def _resolve_session_id(self, user_id: str, chat_id: int) -> str:
        """查找同一 user+chat 最近一条事件，若在 SESSION_GAP 内则复用其 session_id"""
        cutoff = time.time() - SESSION_GAP_SEC
        try:
            row = self._conn.execute(
                "SELECT session_id, ts_epoch FROM strategy_events "
                "WHERE user_id = ? AND chat_id = ? AND ts_epoch >= ? "
                "ORDER BY id DESC LIMIT 1",
                (str(user_id), chat_id, cutoff),
            ).fetchone()
            if row and row["session_id"]:
                return row["session_id"]
        except Exception:
            pass
        return uuid.uuid4().hex[:12]

    # ── 写入 ──────────────────────────────────────────

    def record(self, strategy_id: str, intent: str, user_id: str,
               chat_id: int = 0, response_ms: int = 0, used_ai: bool = True,
               model_id: str = "") -> int:
        """记录一次策略回复事件，返回事件 id"""
        now = time.time()
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        session_id = self._resolve_session_id(user_id, chat_id)
        try:
            cur = self._conn.execute(
                "INSERT INTO strategy_events "
                "(ts, ts_epoch, strategy_id, intent, user_id, chat_id, "
                "response_ms, used_ai, session_id, model_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, now, strategy_id, intent, str(user_id), chat_id,
                 response_ms, 1 if used_ai else 0, session_id, model_id),
            )
            self._conn.commit()
            return cur.lastrowid
        except Exception as e:
            logger.debug("策略事件写入失败: %s", e)
            return 0

    def backfill_follow_up(self, user_id: str, chat_id: int, current_intent: str) -> None:
        """回填追问标记: 查找同一用户+对话最近一条 follow_up IS NULL 的事件,
        若在 FOLLOW_UP_WINDOW 内则标记为已追问"""
        now = time.time()
        cutoff = now - FOLLOW_UP_WINDOW_SEC
        try:
            row = self._conn.execute(
                "SELECT id, intent FROM strategy_events "
                "WHERE user_id = ? AND chat_id = ? AND ts_epoch >= ? AND follow_up IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (str(user_id), chat_id, cutoff),
            ).fetchone()
            if not row:
                return
            same = 1 if row["intent"] == current_intent else 0
            self._conn.execute(
                "UPDATE strategy_events SET follow_up = 1, follow_up_intent = ? WHERE id = ?",
                (current_intent, row["id"]),
            )
            self._conn.commit()
        except Exception as e:
            logger.debug("回填追问失败: %s", e)

    def mark_no_follow_up(self, older_than_sec: int = None) -> int:
        """将超过追问窗口仍为 NULL 的事件标记为 follow_up=0（定期调用）"""
        window = older_than_sec or FOLLOW_UP_WINDOW_SEC
        cutoff = time.time() - window
        try:
            cur = self._conn.execute(
                "UPDATE strategy_events SET follow_up = 0 "
                "WHERE follow_up IS NULL AND ts_epoch < ?",
                (cutoff,),
            )
            self._conn.commit()
            return cur.rowcount
        except Exception as e:
            logger.debug("批量关闭追问标记失败: %s", e)
            return 0

    # ── 聚合查询 ──────────────────────────────────────

    def strategy_summary(self, hours: int = 24) -> List[Dict]:
        """各策略的核心指标汇总"""
        cutoff = time.time() - hours * 3600
        rows = self._conn.execute("""
            SELECT
                strategy_id,
                COUNT(*) AS total,
                ROUND(AVG(response_ms)) AS avg_ms,
                SUM(CASE WHEN follow_up = 1 THEN 1 ELSE 0 END) AS follow_ups,
                SUM(CASE WHEN follow_up = 0 THEN 1 ELSE 0 END) AS no_follow_ups,
                SUM(CASE WHEN follow_up IS NULL THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN follow_up = 1 AND follow_up_intent = intent THEN 1 ELSE 0 END) AS same_intent_follow_ups,
                SUM(CASE WHEN used_ai = 0 THEN 1 ELSE 0 END) AS template_hits
            FROM strategy_events
            WHERE ts_epoch >= ?
            GROUP BY strategy_id
            ORDER BY total DESC
        """, (cutoff,)).fetchall()
        result = []
        for r in rows:
            total = r["total"] or 1
            resolved = (r["follow_ups"] or 0) + (r["no_follow_ups"] or 0)
            follow_ups = r["follow_ups"] or 0
            result.append({
                "strategy_id": r["strategy_id"],
                "total": total,
                "avg_ms": int(r["avg_ms"] or 0),
                "follow_up_rate": round(follow_ups / max(resolved, 1) * 100, 1),
                "same_intent_rate": round((r["same_intent_follow_ups"] or 0) / max(follow_ups, 1) * 100, 1),
                "silence_rate": round((r["no_follow_ups"] or 0) / max(resolved, 1) * 100, 1),
                "template_hit_rate": round((r["template_hits"] or 0) / total * 100, 1),
                "pending": r["pending"] or 0,
            })
        return result

    def strategy_hourly(self, strategy_id: str, hours: int = 24) -> List[Dict]:
        """某策略按小时的事件数和平均响应时间"""
        cutoff = time.time() - hours * 3600
        rows = self._conn.execute(
            "SELECT substr(ts, 1, 13) AS hour, COUNT(*) AS cnt, "
            "ROUND(AVG(response_ms)) AS avg_ms "
            "FROM strategy_events "
            "WHERE strategy_id = ? AND ts_epoch >= ? "
            "GROUP BY hour ORDER BY hour",
            (strategy_id, cutoff),
        ).fetchall()
        return [{"hour": r["hour"], "count": r["cnt"],
                 "avg_ms": int(r["avg_ms"] or 0)} for r in rows]

    def intent_strategy_matrix(self, hours: int = 24) -> List[Dict]:
        """意图 × 策略 的交叉统计（用于 A/B 对比）"""
        cutoff = time.time() - hours * 3600
        rows = self._conn.execute(
            "SELECT intent, strategy_id, COUNT(*) AS cnt, "
            "ROUND(AVG(response_ms)) AS avg_ms, "
            "SUM(CASE WHEN follow_up = 1 THEN 1 ELSE 0 END) AS fu "
            "FROM strategy_events WHERE ts_epoch >= ? "
            "GROUP BY intent, strategy_id ORDER BY cnt DESC",
            (cutoff,),
        ).fetchall()
        return [{"intent": r["intent"], "strategy_id": r["strategy_id"],
                 "count": r["cnt"], "avg_ms": int(r["avg_ms"] or 0),
                 "follow_ups": r["fu"] or 0} for r in rows]

    def recent_events(self, limit: int = 50) -> List[Dict]:
        """最近 N 条事件（调试用）"""
        rows = self._conn.execute(
            "SELECT * FROM strategy_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def total_events(self, hours: int = 24) -> int:
        cutoff = time.time() - hours * 3600
        return self._conn.execute(
            "SELECT COUNT(*) FROM strategy_events WHERE ts_epoch >= ?",
            (cutoff,),
        ).fetchone()[0]

    # ── 模型级聚合 ─────────────────────────────────────

    def model_summary(self, hours: int = 24) -> List[Dict]:
        """各模型的核心指标汇总"""
        cutoff = time.time() - hours * 3600
        rows = self._conn.execute("""
            SELECT
                model_id,
                COUNT(*) AS total,
                ROUND(AVG(response_ms)) AS avg_ms,
                SUM(CASE WHEN follow_up = 1 THEN 1 ELSE 0 END) AS follow_ups,
                SUM(CASE WHEN follow_up = 0 THEN 1 ELSE 0 END) AS no_follow_ups,
                SUM(CASE WHEN follow_up = 1 AND follow_up_intent = intent THEN 1 ELSE 0 END) AS same_fu,
                COUNT(DISTINCT strategy_id) AS strategies_count
            FROM strategy_events
            WHERE ts_epoch >= ? AND model_id != ''
            GROUP BY model_id ORDER BY total DESC
        """, (cutoff,)).fetchall()
        result = []
        for r in rows:
            total = r["total"] or 1
            resolved = (r["follow_ups"] or 0) + (r["no_follow_ups"] or 0)
            fu = r["follow_ups"] or 0
            result.append({
                "model_id": r["model_id"],
                "total": total,
                "avg_ms": int(r["avg_ms"] or 0),
                "follow_up_rate": round(fu / max(resolved, 1) * 100, 1),
                "same_intent_rate": round((r["same_fu"] or 0) / max(fu, 1) * 100, 1),
                "silence_rate": round((r["no_follow_ups"] or 0) / max(resolved, 1) * 100, 1),
                "strategies_count": r["strategies_count"],
            })
        return result

    def model_strategy_matrix(self, hours: int = 24) -> List[Dict]:
        """模型 × 策略 交叉统计"""
        cutoff = time.time() - hours * 3600
        rows = self._conn.execute(
            "SELECT model_id, strategy_id, COUNT(*) AS cnt, "
            "ROUND(AVG(response_ms)) AS avg_ms, "
            "SUM(CASE WHEN follow_up = 1 THEN 1 ELSE 0 END) AS fu, "
            "SUM(CASE WHEN follow_up = 0 THEN 1 ELSE 0 END) AS nfu "
            "FROM strategy_events WHERE ts_epoch >= ? AND model_id != '' "
            "GROUP BY model_id, strategy_id ORDER BY cnt DESC",
            (cutoff,),
        ).fetchall()
        result = []
        for r in rows:
            resolved = (r["fu"] or 0) + (r["nfu"] or 0)
            result.append({
                "model_id": r["model_id"], "strategy_id": r["strategy_id"],
                "count": r["cnt"], "avg_ms": int(r["avg_ms"] or 0),
                "follow_up_rate": round((r["fu"] or 0) / max(resolved, 1) * 100, 1),
                "silence_rate": round((r["nfu"] or 0) / max(resolved, 1) * 100, 1),
            })
        return result

    # ── 用户分群分析 ─────────────────────────────────

    def user_segment_analysis(self, hours: int = 24) -> Dict:
        """按用户活跃度分群，分析各群体对不同策略的效果差异。

        分群规则 (基于分析窗口内):
          - heavy: >= 10 条事件
          - moderate: 3-9 条
          - light: 1-2 条
        """
        cutoff = time.time() - hours * 3600
        rows = self._conn.execute("""
            SELECT user_id, COUNT(*) AS cnt,
                   GROUP_CONCAT(strategy_id) AS sids,
                   ROUND(AVG(response_ms)) AS avg_ms,
                   SUM(CASE WHEN follow_up = 1 THEN 1 ELSE 0 END) AS fu,
                   SUM(CASE WHEN follow_up = 0 THEN 1 ELSE 0 END) AS nfu
            FROM strategy_events WHERE ts_epoch >= ?
            GROUP BY user_id
        """, (cutoff,)).fetchall()

        segments = {"heavy": [], "moderate": [], "light": []}
        for r in rows:
            cnt = r["cnt"]
            if cnt >= 10:
                seg = "heavy"
            elif cnt >= 3:
                seg = "moderate"
            else:
                seg = "light"
            resolved = (r["fu"] or 0) + (r["nfu"] or 0)
            segments[seg].append({
                "user_id": r["user_id"], "events": cnt,
                "avg_ms": int(r["avg_ms"] or 0),
                "follow_up_rate": round((r["fu"] or 0) / max(resolved, 1) * 100, 1),
                "strategies": r["sids"] or "",
            })

        result = {}
        for seg, users in segments.items():
            if not users:
                result[seg] = {"users": 0, "events": 0, "avg_ms": 0,
                               "follow_up_rate": 0, "top_strategies": {}}
                continue
            total_events = sum(u["events"] for u in users)
            avg_ms_all = round(sum(u["avg_ms"] * u["events"] for u in users)
                               / max(total_events, 1))
            weighted_fu = sum(u["follow_up_rate"] * u["events"] for u in users)
            strat_counts: Dict[str, int] = {}
            for u in users:
                for s in u["strategies"].split(","):
                    s = s.strip()
                    if s:
                        strat_counts[s] = strat_counts.get(s, 0) + 1
            top = dict(sorted(strat_counts.items(), key=lambda x: -x[1])[:5])
            result[seg] = {
                "users": len(users),
                "events": total_events,
                "avg_ms": avg_ms_all,
                "follow_up_rate": round(weighted_fu / max(total_events, 1), 1),
                "top_strategies": top,
            }
        return result

    # ── 会话级聚合 ─────────────────────────────────────

    def session_summary(self, hours: int = 24) -> List[Dict]:
        """会话级指标汇总: 会话数、平均轮次、首次意图分布、各策略会话解决率"""
        cutoff = time.time() - hours * 3600
        rows = self._conn.execute("""
            SELECT
                session_id,
                COUNT(*) AS turns,
                GROUP_CONCAT(DISTINCT strategy_id) AS strategies,
                MIN(intent) AS first_intent,
                ROUND(AVG(response_ms)) AS avg_ms,
                MAX(CASE WHEN follow_up = 0 THEN 1 ELSE 0 END) AS resolved
            FROM strategy_events
            WHERE ts_epoch >= ? AND session_id != ''
            GROUP BY session_id
            HAVING turns >= 1
            ORDER BY MAX(ts_epoch) DESC
        """, (cutoff,)).fetchall()
        return [{"session_id": r["session_id"], "turns": r["turns"],
                 "strategies": r["strategies"] or "", "first_intent": r["first_intent"],
                 "avg_ms": int(r["avg_ms"] or 0),
                 "resolved": bool(r["resolved"])} for r in rows]

    def session_stats(self, hours: int = 24) -> Dict:
        """会话级汇总统计"""
        sessions = self.session_summary(hours)
        if not sessions:
            return {"total_sessions": 0, "avg_turns": 0, "resolve_rate": 0,
                    "by_strategy": {}, "by_first_intent": {}}
        total = len(sessions)
        resolved = sum(1 for s in sessions if s["resolved"])
        avg_turns = round(sum(s["turns"] for s in sessions) / total, 1)

        by_strategy: Dict[str, Dict] = {}
        for s in sessions:
            for sid in s["strategies"].split(","):
                sid = sid.strip()
                if not sid:
                    continue
                if sid not in by_strategy:
                    by_strategy[sid] = {"sessions": 0, "resolved": 0, "total_turns": 0}
                by_strategy[sid]["sessions"] += 1
                by_strategy[sid]["total_turns"] += s["turns"]
                if s["resolved"]:
                    by_strategy[sid]["resolved"] += 1
        for sid, d in by_strategy.items():
            d["resolve_rate"] = round(d["resolved"] / max(d["sessions"], 1) * 100, 1)
            d["avg_turns"] = round(d["total_turns"] / max(d["sessions"], 1), 1)

        by_intent: Dict[str, int] = {}
        for s in sessions:
            fi = s["first_intent"]
            by_intent[fi] = by_intent.get(fi, 0) + 1

        return {
            "total_sessions": total,
            "avg_turns": avg_turns,
            "resolve_rate": round(resolved / total * 100, 1),
            "by_strategy": by_strategy,
            "by_first_intent": dict(sorted(by_intent.items(), key=lambda x: -x[1])),
        }

    def purge(self, keep_days: int = 30) -> int:
        """删除超过 keep_days 天的旧事件并回收磁盘空间"""
        cutoff = time.time() - keep_days * 86400
        try:
            cur = self._conn.execute(
                "DELETE FROM strategy_events WHERE ts_epoch < ?", (cutoff,))
            deleted = cur.rowcount
            if deleted > 0:
                self._conn.execute("VACUUM")
            self._conn.commit()
            logger.info("策略事件清理: 删除 %d 条 (保留 %d 天)", deleted, keep_days)
            return deleted
        except Exception as e:
            logger.warning("策略事件清理失败: %s", e)
            return 0

    def close(self):
        if self._conn:
            self._conn.close()
