"""多平台 deferred 发送队列（平台无关）。

背景与定位
----------
Messenger 的 deferred 队列与 RPA runner（`messenger_rpa_approvals` 表 + 浏览器池 +
截图新鲜度）深度耦合，是为「人审/safe_skip 类回复 + 浏览器真发」量身定制的，不适合
直接套到 LINE/WhatsApp/Telegram 等可编程发送的平台。

本模块提供一个**平台无关**的轻量 deferred outbox：
- 自己的 SQLite 表（与 messenger 路径解耦，互不影响）。
- 通用护栏：staleness 过期 / kill-switch（复用 `src.ops.kill_switch`）/ quiet_hours 顺延 /
  per-(platform,account) pacing 最小间隔 / max_per_tick 慢清。
- **sender 注册表**：每个平台注册一个 `async send(account_id, chat_key, text) -> bool`，
  drain loop 到点查注册表真发；未注册的平台 → 留 pending（不丢，等接线后自动发）。

设计与 care_dispatcher / reactivation_loop 同范式（可注入、可单测、默认关）。
messenger 仍走既有 runner deferred 路径；本队列服务**非 messenger**平台。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

class DeferredSenderNotReady(Exception):
    """sender 抛此异常表示「此刻无法投递（如 worker 未就绪）」→ 不算失败，
    dispatcher 把该条推后重试（区别于「投递失败」的 mark_failed）。"""


# sender：(account_id, chat_key, text) -> 是否送达成功
# 抛 DeferredSenderNotReady → 推后重试；返回 False → 标记失败。
SenderFn = Callable[[str, str, str], Awaitable[bool]]
# kill-switch 检查：(platform, account_id) -> (blocked, scope, reason)
KillSwitchCheck = Callable[[str, str], Tuple[bool, str, str]]

_DDL = """
CREATE TABLE IF NOT EXISTS deferred_outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT NOT NULL,
    account_id    TEXT NOT NULL DEFAULT 'default',
    chat_key      TEXT NOT NULL,
    reply_text    TEXT NOT NULL,
    defer_until   REAL NOT NULL DEFAULT 0,
    reason        TEXT NOT NULL DEFAULT '',
    staleness_sec REAL NOT NULL DEFAULT 86400,
    status        TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    extra         TEXT NOT NULL DEFAULT '{}',
    created_at    REAL NOT NULL DEFAULT 0,
    sent_at       REAL NOT NULL DEFAULT 0,
    error         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_outbox_due
    ON deferred_outbox (status, defer_until);
"""


def shift_out_of_quiet_hours(ts: float, *, start_hour: float, end_hour: float) -> float:
    """命中安静时段则顺延到其结束时刻；否则原样返回。start==end 表示无安静窗。

    与 care_dispatcher 同语义（独立实现，避免跨子系统硬依赖）。
    """
    from datetime import datetime, timedelta

    if start_hour == end_hour:
        return ts
    dt = datetime.fromtimestamp(ts)
    h = dt.hour + dt.minute / 60.0
    overnight = start_hour > end_hour
    in_quiet = (
        (not overnight and start_hour <= h < end_hour)
        or (overnight and (h >= start_hour or h < end_hour))
    )
    if not in_quiet:
        return ts
    target = dt.replace(hour=int(end_hour) % 24, minute=0, second=0, microsecond=0)
    if overnight and h >= start_hour:
        target = target + timedelta(days=1)
    if target.timestamp() <= ts:
        target = target + timedelta(days=1)
    return target.timestamp()


class DeferredOutboxStore:
    """平台无关 deferred 队列的持久化（线程安全 SQLite）。"""

    def __init__(self, db_path: Any = ":memory:") -> None:
        self._is_mem = str(db_path) == ":memory:"
        if not self._is_mem:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, timeout=10,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if not self._is_mem:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            self._conn.commit()

    def enqueue(
        self,
        *,
        platform: str,
        account_id: str,
        chat_key: str,
        reply_text: str,
        defer_until: float,
        reason: str = "",
        staleness_sec: float = 86400.0,
        extra: Optional[Dict[str, Any]] = None,
        now: Optional[float] = None,
    ) -> int:
        """入队一条待发消息。返回 row id（>0 成功，0 失败/缺参）。"""
        plat = str(platform or "").strip()
        chat = str(chat_key or "").strip()
        text = str(reply_text or "").strip()
        if not plat or not chat or not text:
            return 0
        n = float(now if now is not None else time.time())
        try:
            extra_json = json.dumps(extra or {}, ensure_ascii=False)
        except Exception:
            extra_json = "{}"
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO deferred_outbox "
                "(platform, account_id, chat_key, reply_text, defer_until, reason, "
                " staleness_sec, status, attempts, extra, created_at) "
                "VALUES (?,?,?,?,?,?,?,'pending',0,?,?)",
                (plat, str(account_id or "default") or "default", chat, text,
                 float(defer_until), str(reason or "")[:160],
                 float(staleness_sec), extra_json, n),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def drain_due(
        self, *, now: Optional[float] = None, limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """取到期（defer_until<=now）的 pending 行，最早到期优先。"""
        n = float(now if now is not None else time.time())
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM deferred_outbox "
                "WHERE status='pending' AND defer_until<=? "
                "ORDER BY defer_until ASC LIMIT ?",
                (n, max(1, int(limit))),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["extra"] = json.loads(d.get("extra") or "{}")
            except Exception:
                d["extra"] = {}
            out.append(d)
        return out

    def mark_sent(self, row_id: int, *, now: Optional[float] = None) -> None:
        n = float(now if now is not None else time.time())
        with self._lock:
            self._conn.execute(
                "UPDATE deferred_outbox SET status='sent', sent_at=?, "
                "attempts=attempts+1 WHERE id=?",
                (n, int(row_id)),
            )
            self._conn.commit()

    def mark_failed(self, row_id: int, err: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE deferred_outbox SET status='failed', attempts=attempts+1, "
                "error=? WHERE id=?",
                (str(err or "")[:200], int(row_id)),
            )
            self._conn.commit()

    def mark_expired(self, row_id: int, err: str = "stale") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE deferred_outbox SET status='expired', error=? WHERE id=?",
                (str(err or "")[:200], int(row_id)),
            )
            self._conn.commit()

    def push_until(self, row_id: int, new_until: float, note: str = "") -> bool:
        """drain 前护栏未通过 → 把 defer_until 推后（不丢消息）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE deferred_outbox SET defer_until=?, reason=? "
                "WHERE id=? AND status='pending'",
                (float(new_until), str(note or "")[:160], int(row_id)),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def count(self, *, status: str = "") -> int:
        with self._lock:
            if status:
                row = self._conn.execute(
                    "SELECT COUNT(*) c FROM deferred_outbox WHERE status=?",
                    (status,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) c FROM deferred_outbox").fetchone()
        return int(row["c"] if row else 0)

    def stats(self) -> Dict[str, Any]:
        """一次性聚合：各 status 计数 + pending 按 platform/reason 分组。

        `pending_by_reason` 是运营可观测性的核心——直接看出 pending 卡在哪道护栏
        （quiet_hours / pacing_min_gap / no_sender / kill_switch:* / sender_not_ready）。
        """
        with self._lock:
            by_status = {
                str(r["status"]): int(r["c"]) for r in self._conn.execute(
                    "SELECT status, COUNT(*) c FROM deferred_outbox GROUP BY status"
                ).fetchall()
            }
            by_platform = {
                str(r["platform"]): int(r["c"]) for r in self._conn.execute(
                    "SELECT platform, COUNT(*) c FROM deferred_outbox "
                    "WHERE status='pending' GROUP BY platform"
                ).fetchall()
            }
            by_reason = {
                (str(r["reason"]) or "(none)"): int(r["c"])
                for r in self._conn.execute(
                    "SELECT reason, COUNT(*) c FROM deferred_outbox "
                    "WHERE status='pending' GROUP BY reason"
                ).fetchall()
            }
        return {
            "by_status": by_status,
            "pending_by_platform": by_platform,
            "pending_by_reason": by_reason,
            "total": sum(by_status.values()),
        }

    # ── 运营动作（mutate）：重试 / 取消 / 清理 ──────────────────────
    _TERMINAL = ("failed", "expired", "cancelled")

    def requeue(self, row_id: int, *, now: Optional[float] = None) -> bool:
        """把单条终态行（failed/expired/cancelled）重新入队 pending（立即到期）。"""
        n = float(now if now is not None else time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE deferred_outbox SET status='pending', defer_until=?, "
                "error='', reason='requeued' "
                "WHERE id=? AND status IN ('failed','expired','cancelled')",
                (n, int(row_id)),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def requeue_status(
        self, status: str, *, now: Optional[float] = None, limit: int = 500,
    ) -> int:
        """批量把某终态（failed/expired/cancelled）的行重入队。返回受影响条数。"""
        st = str(status or "").strip()
        if st not in self._TERMINAL:
            return 0
        n = float(now if now is not None else time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE deferred_outbox SET status='pending', defer_until=?, "
                "error='', reason='requeued' WHERE id IN ("
                " SELECT id FROM deferred_outbox WHERE status=? ORDER BY id LIMIT ?)",
                (n, st, max(1, int(limit))),
            )
            self._conn.commit()
            return int(cur.rowcount)

    def cancel_pending(self, *, reason: str = "", platform: str = "") -> int:
        """把 pending 行按 reason/平台软取消（status='cancelled'）。返回条数。

        必须至少给一个过滤条件（reason 或 platform），避免误清空整个队列。
        典型用法：清掉卡在某道护栏（如 no_sender / quiet_hours）的积压。
        """
        reason = str(reason or "").strip()
        platform = str(platform or "").strip()
        if not reason and not platform:
            return 0
        clauses = ["status='pending'"]
        params: List[Any] = []
        if reason:
            clauses.append("reason=?")
            params.append(reason)
        if platform:
            clauses.append("platform=?")
            params.append(platform)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE deferred_outbox SET status='cancelled', "
                "error='cancelled_by_op' WHERE " + " AND ".join(clauses),
                params,
            )
            self._conn.commit()
            return int(cur.rowcount)

    def purge_terminal(
        self, *, older_than_sec: float = 604800.0, now: Optional[float] = None,
    ) -> int:
        """删除超过保留期的终态行（sent/expired/failed/cancelled）瘦身。返回条数。"""
        n = float(now if now is not None else time.time())
        cut = n - max(0.0, float(older_than_sec))
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM deferred_outbox WHERE status IN "
                "('sent','expired','failed','cancelled') AND "
                "COALESCE(NULLIF(sent_at,0), created_at) < ?",
                (cut,),
            )
            self._conn.commit()
            return int(cur.rowcount)

    def list_recent(
        self, *, status: str = "", limit: int = 100,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM deferred_outbox WHERE status=? "
                    "ORDER BY id DESC LIMIT ?",
                    (status, max(1, int(limit))),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM deferred_outbox ORDER BY id DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
        return [dict(r) for r in rows]


class DeferredDispatcher:
    """平台无关 deferred 队列的 drain loop（通用护栏 + sender 注册表）。

    护栏顺序（任一不通过 → push_until 推后，不丢消息）：
      1. staleness：created_at + staleness_sec < now → mark_expired（错过时机不补发）
      2. kill-switch：is_blocked(platform, account_id) → push +ks_backoff
      3. quiet_hours：命中安静窗 → 顺延到窗口结束
      4. pacing：per-(platform,account) 距上次发送 < min_gap → push +min_gap
      5. sender 查注册表：无注册 → 留 pending（push +no_sender_backoff，等接线）
    """

    def __init__(
        self,
        *,
        store: DeferredOutboxStore,
        senders: Optional[Dict[str, SenderFn]] = None,
        kill_switch_check: Optional[KillSwitchCheck] = None,
        quiet_start_hour: float = 23.0,
        quiet_end_hour: float = 8.0,
        min_gap_sec: float = 45.0,
        max_per_tick: int = 3,
        interval_sec: float = 120.0,
        ks_backoff_sec: float = 1800.0,
        no_sender_backoff_sec: float = 600.0,
    ) -> None:
        self._store = store
        self._senders: Dict[str, SenderFn] = dict(senders or {})
        if kill_switch_check is None:
            from src.ops.kill_switch import is_blocked as _ks
            kill_switch_check = _ks
        self._ks = kill_switch_check
        self._quiet_start = float(quiet_start_hour)
        self._quiet_end = float(quiet_end_hour)
        self._min_gap = max(0.0, float(min_gap_sec))
        self._max_per_tick = max(1, int(max_per_tick))
        self._interval = max(30.0, float(interval_sec))
        self._ks_backoff = max(60.0, float(ks_backoff_sec))
        self._no_sender_backoff = max(60.0, float(no_sender_backoff_sec))
        # per-(platform,account) 最近发送时刻（内存，pacing 用）
        self._last_sent: Dict[Tuple[str, str], float] = {}
        # 运营手动暂停的平台（内存；重启复位为全部活跃）
        self._paused: set = set()
        self._stop_evt: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None

    def register_sender(self, platform: str, fn: SenderFn) -> None:
        p = str(platform or "").strip()
        if p and fn is not None:
            self._senders[p] = fn

    def has_sender(self, platform: str) -> bool:
        return str(platform or "").strip() in self._senders

    def registered_platforms(self) -> List[str]:
        """已注册 sender 的平台列表（供运营可观测性展示）。"""
        return sorted(self._senders.keys())

    def pause(self, platform: str) -> None:
        """运营手动暂停某平台投递（pending 不丢，逐 tick 推后等 resume）。"""
        p = str(platform or "").strip()
        if p:
            self._paused.add(p)

    def resume(self, platform: str) -> None:
        self._paused.discard(str(platform or "").strip())

    def is_paused(self, platform: str) -> bool:
        return str(platform or "").strip() in self._paused

    def paused_platforms(self) -> List[str]:
        return sorted(self._paused)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_evt = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="deferred_outbox")

    async def stop(self) -> None:
        if self._stop_evt:
            self._stop_evt.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                pass

    async def _loop(self) -> None:
        try:
            while not (self._stop_evt and self._stop_evt.is_set()):
                try:
                    n = await self.run_once()
                    if n:
                        logger.info("[deferred_outbox] tick: sent %d msgs", n)
                except Exception:
                    logger.exception("deferred_outbox run_once 异常")
                try:
                    if self._stop_evt:
                        await asyncio.wait_for(
                            self._stop_evt.wait(), timeout=self._interval)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("deferred_outbox 退出")

    async def run_once(self, *, now: Optional[float] = None) -> int:
        """一次 drain：返回真实送达条数。"""
        n = float(now if now is not None else time.time())
        due = self._store.drain_due(now=n, limit=self._max_per_tick * 4)
        if not due:
            return 0
        sent = 0
        for row in due:
            if sent >= self._max_per_tick:
                break
            try:
                if await self._dispatch_one(row, n):
                    sent += 1
            except Exception:
                logger.debug("deferred dispatch_one 异常 id=%s",
                             row.get("id"), exc_info=True)
        return sent

    async def _dispatch_one(self, row: Dict[str, Any], now: float) -> bool:
        row_id = int(row.get("id") or 0)
        platform = str(row.get("platform") or "")
        account_id = str(row.get("account_id") or "default") or "default"
        chat_key = str(row.get("chat_key") or "")
        text = str(row.get("reply_text") or "")
        if row_id <= 0 or not platform or not chat_key or not text:
            if row_id > 0:
                self._store.mark_failed(row_id, "invalid_row")
            return False

        # 1. staleness：错过时机不补发
        created_at = float(row.get("created_at") or 0)
        staleness = float(row.get("staleness_sec") or 0)
        if staleness > 0 and created_at > 0 and (now - created_at) > staleness:
            self._store.mark_expired(row_id, "stale")
            return False

        # 1.5 运营暂停：该平台被手动暂停 → 推后到下一 tick 再看（不丢）
        if platform in self._paused:
            self._store.push_until(
                row_id, now + self._interval, note="paused")
            return False

        # 2. kill-switch
        try:
            blocked, scope, ks_reason = self._ks(platform, account_id)
        except Exception:
            blocked, scope, ks_reason = (False, "", "")
        if blocked:
            self._store.push_until(
                row_id, now + self._ks_backoff,
                note=f"kill_switch:{scope}")
            logger.info("[deferred_outbox] kill-switch 阻断 id=%d scope=%s → 推后",
                        row_id, scope)
            return False

        # 3. quiet_hours：顺延到安静窗结束
        shifted = shift_out_of_quiet_hours(
            now, start_hour=self._quiet_start, end_hour=self._quiet_end)
        if shifted > now:
            self._store.push_until(row_id, shifted, note="quiet_hours")
            return False

        # 4. pacing：per-(platform,account) 最小间隔
        key = (platform, account_id)
        last = self._last_sent.get(key, 0.0)
        if self._min_gap > 0 and last > 0 and (now - last) < self._min_gap:
            self._store.push_until(
                row_id, last + self._min_gap, note="pacing_min_gap")
            return False

        # 5. sender 查注册表
        fn = self._senders.get(platform)
        if fn is None:
            # 未接线 → 不丢，推后等注册（避免一直 drain 同一条）
            self._store.push_until(
                row_id, now + self._no_sender_backoff, note="no_sender")
            return False

        try:
            ok = bool(await fn(account_id, chat_key, text))
        except DeferredSenderNotReady:
            # worker 未就绪等暂态 → 推后重试，不丢、不标失败
            self._store.push_until(
                row_id, now + self._no_sender_backoff, note="sender_not_ready")
            return False
        except Exception as ex:
            self._store.mark_failed(row_id, f"{type(ex).__name__}:{ex}")
            logger.debug("deferred sender 异常 id=%d platform=%s",
                         row_id, platform, exc_info=True)
            return False
        if not ok:
            self._store.mark_failed(row_id, "sender_returned_false")
            return False
        self._store.mark_sent(row_id, now=now)
        self._last_sent[key] = now
        logger.info("[deferred_outbox] sent id=%d platform=%s chat=%s",
                    row_id, platform, chat_key)
        return True


__all__ = [
    "DeferredOutboxStore",
    "DeferredDispatcher",
    "DeferredSenderNotReady",
    "shift_out_of_quiet_hours",
    "SenderFn",
]
