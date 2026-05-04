# -*- coding: utf-8 -*-
"""MobileBridgeService — 双向 bridge：mobile-auto0423 ↔ telegram-mtproto-ai。

Mobile → Telegram:
  - 轮询 openclaw.db lead_handoffs（WAL 只读打开）
  - 幂等写入 contacts.db: ensure contact/channel_identity/journey + append event
  - 用 mobile_bridge_sync_state 表保存 watermark，支持 crash-safe 重启

Telegram → Mobile:
  - 提供 writeback_acknowledge / complete / reject
  - 通过 mobile 的 HTTP API 触发状态转移（不直接写 openclaw.db）
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# channel/account 用于在 contacts.db 里标识"来自 mobile-auto0423"的身份
MOBILE_CHANNEL = "mobile"
MOBILE_ACCOUNT = "openclaw"

# openclaw.db handoff state → contacts.db journey funnel_stage
_STATE_STAGE_MAP: Dict[str, str] = {
    "pending": "HANDOFF_SENT",
    "acknowledged": "HANDOFF_SENT",
    "completed": "LINE_ENGAGED",
    "rejected": "LOST_HANDOFF",
    "expired": "LOST_HANDOFF",
    "duplicate_blocked": "LOST_HANDOFF",
}

# watermark key
_WM_KEY = "handoff_watermark"
_WM_INIT = "1970-01-01T00:00:00Z"

# writeback 重试参数
_MAX_ATTEMPTS = 5
_BACKOFF_SECS = [30, 120, 600, 3600, 7200]   # 30s / 2min / 10min / 1h / 2h

# bridge 状态表 DDL（加到 contacts.db，完全独立）
_BRIDGE_STATE_DDL = """
CREATE TABLE IF NOT EXISTS mobile_bridge_sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

# writeback 重试队列 DDL
_WRITEBACK_QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS mobile_bridge_writeback_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id   TEXT NOT NULL,
    action       TEXT NOT NULL,
    by_user      TEXT NOT NULL DEFAULT '',
    notes        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT DEFAULT '',
    next_retry_at REAL NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL DEFAULT (unixepoch()),
    delivered_at REAL
)
"""


class MobileBridgeService:
    """双向 bridge，负责 openclaw.db ↔ contacts.db 的状态同步与回写。"""

    def __init__(
        self,
        *,
        contacts_store: Any,
        openclaw_db_path: str,
        mobile_api_base: str = "http://127.0.0.1:18080",
        poll_interval_sec: float = 15.0,
    ) -> None:
        self._store = contacts_store
        self._openclaw_path = str(openclaw_db_path)
        self._mobile_api = mobile_api_base.rstrip("/")
        self._poll_interval = poll_interval_sec
        self._stop_evt: asyncio.Event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

        # stats（GIL 保护下简单类型赋值是原子的）
        self._sync_ok: int = 0
        self._sync_fail: int = 0
        self._writeback_ok: int = 0
        self._writeback_fail: int = 0
        self._last_sync_ts: float = 0.0
        self._last_error: str = ""

        self._ensure_bridge_table()

    # ── 初始化 bridge 表 ────────────────────────────────────
    def _ensure_bridge_table(self) -> None:
        try:
            with self._store._lock:  # noqa: SLF001
                self._store._conn.execute(_BRIDGE_STATE_DDL)  # noqa: SLF001
                self._store._conn.execute(_WRITEBACK_QUEUE_DDL)  # noqa: SLF001
                self._store._conn.commit()  # noqa: SLF001
        except Exception as exc:
            logger.warning("[mobile_bridge] bridge 表创建失败: %s", exc)

    # ── watermark ─────────────────────────────────────────────
    def _get_watermark(self) -> str:
        try:
            with self._store._lock:  # noqa: SLF001
                row = self._store._conn.execute(  # noqa: SLF001
                    "SELECT value FROM mobile_bridge_sync_state WHERE key=?", (_WM_KEY,)
                ).fetchone()
            return row[0] if row else _WM_INIT
        except Exception:
            return _WM_INIT

    def _set_watermark(self, value: str) -> None:
        try:
            with self._store._lock:  # noqa: SLF001
                self._store._conn.execute(  # noqa: SLF001
                    "INSERT OR REPLACE INTO mobile_bridge_sync_state(key, value, updated_at)"
                    " VALUES (?, ?, datetime('now'))",
                    (_WM_KEY, value),
                )
                self._store._conn.commit()  # noqa: SLF001
        except Exception as exc:
            logger.warning("[mobile_bridge] watermark 写入失败: %s", exc)

    # ── 读 openclaw.db ─────────────────────────────────────────────
    def _poll_openclaw(self, since: str, limit: int = 200) -> List[Dict[str, Any]]:
        """只读打开 openclaw.db，取 state_updated_at > since 的 handoffs。"""
        if not Path(self._openclaw_path).exists():
            return []
        try:
            conn = sqlite3.connect(
                f"file:{self._openclaw_path}?mode=ro", uri=True, timeout=15
            )
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM lead_handoffs"
                " WHERE state_updated_at > ?"
                " ORDER BY state_updated_at ASC"
                " LIMIT ?",
                (since, limit),
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("[mobile_bridge] poll openclaw 失败: %s", exc)
            return []

    # ── 同步单条 handoff ────────────────────────────────────────
    def _sync_one(self, row: Dict[str, Any]) -> bool:
        """把一条 lead_handoffs 行同步到 contacts.db（幂等）。"""
        canonical_id = row.get("canonical_id") or ""
        handoff_id = row.get("handoff_id") or ""
        state = row.get("state") or "pending"
        channel = row.get("channel") or ""
        source_agent = row.get("source_agent") or ""
        source_device = row.get("source_device") or ""
        target_agent = row.get("target_agent") or ""
        snippet_sent = (row.get("snippet_sent") or "")[:300]
        state_notes = (row.get("state_notes") or "")[:200]
        state_updated_at = row.get("state_updated_at") or ""
        created_at = row.get("created_at") or ""

        if not canonical_id or not handoff_id:
            return False

        # trace_id 用于幂等：同一 (handoff_id, state) 只写一次 journey_event
        trace_id = f"mobile_bridge:{handoff_id}:{state}"

        try:
            # 确保 contact + channel_identity（幂等）
            contact, ci, _ = self._store.ensure_channel_identity(
                channel=MOBILE_CHANNEL,
                account_id=MOBILE_ACCOUNT,
                external_id=canonical_id,
                display_name=canonical_id,
            )
            journey = self._store.get_journey_by_contact(contact.contact_id)
            if journey is None:
                return False

            # 幂等检查：若同 trace_id 已有记录则跳过写入
            already_synced = False
            try:
                with self._store._lock:  # noqa: SLF001
                    row_check = self._store._conn.execute(  # noqa: SLF001
                        "SELECT 1 FROM journey_events WHERE trace_id=? LIMIT 1",
                        (trace_id,),
                    ).fetchone()
                    already_synced = row_check is not None
            except Exception:
                pass

            if already_synced:
                return True  # 视为成功，跳过重复写入

            # 追加 journey_event（每次状态变化都记一条；event_type 带 state 便于过滤）
            event_type = f"mobile_handoff_{state}"
            payload: Dict[str, Any] = {
                "source": "mobile-auto0423",
                "handoff_id": handoff_id,
                "canonical_id": canonical_id,
                "state": state,
                "channel": channel,
                "source_agent": source_agent,
                "source_device": source_device,
                "target_agent": target_agent,
                "snippet_sent": snippet_sent,
                "state_notes": state_notes,
                "state_updated_at": state_updated_at,
                "created_at": created_at,
            }
            self._store.append_event(
                journey_id=journey.journey_id,
                event_type=event_type,
                payload=payload,
                trace_id=trace_id,
            )

            # 尝试推进 funnel_stage（FSM guard 会自动拒绝非法转移，静默处理）
            target_stage = _STATE_STAGE_MAP.get(state)
            if target_stage:
                try:
                    from src.contacts.journey_fsm import transit as _fsm_transit
                    _fsm_transit(
                        self._store,
                        journey_id=journey.journey_id,
                        to_stage=target_stage,
                        payload={"reason": f"mobile_bridge:{state}", "handoff_id": handoff_id},
                    )
                except Exception as fsm_exc:
                    logger.debug("[mobile_bridge] stage transit 跳过: %s", fsm_exc)

            return True

        except Exception as exc:
            logger.warning(
                "[mobile_bridge] sync_one 失败 handoff=%s: %s",
                handoff_id[:12], exc,
            )
            return False

    # ── 同步批次（同步函数，由 _poll_loop 通过 asyncio.to_thread 调用）──────
    def _sync_batch(self) -> Dict[str, int]:
        """poll + sync + drain writeback queue 全在此同步方法内执行，
        避免直接在异步事件循环里做阻塞操作。"""
        # ─ Mobile → Telegram 同步
        watermark = self._get_watermark()
        rows = self._poll_openclaw(since=watermark)
        ok_count = 0
        new_wm = watermark
        for row in rows:
            if self._sync_one(row):
                self._sync_ok += 1
                ok_count += 1
                ts = row.get("state_updated_at") or ""
                if ts > new_wm:
                    new_wm = ts
            else:
                self._sync_fail += 1
        if new_wm > watermark:
            self._set_watermark(new_wm)
        if ok_count:
            logger.info("[mobile_bridge] 同步 %d 条 (ok=%d fail=%d)",
                        ok_count, self._sync_ok, self._sync_fail)

        # ─ 排空 writeback 重试队列
        rb_ok, rb_fail = self._drain_writeback_queue()
        if rb_ok or rb_fail:
            logger.info("[mobile_bridge] writeback重试 ok=%d fail=%d", rb_ok, rb_fail)

        self._last_sync_ts = time.time()
        self._last_error = ""
        return {"sync_rows": len(rows), "sync_ok": ok_count, "rb_ok": rb_ok, "rb_fail": rb_fail}

    # ── 轮询主循环 ─────────────────────────────────────────────
    async def _poll_loop(self) -> None:
        logger.info(
            "[mobile_bridge] 循环启动 (openclaw=%s interval=%.0fs)",
            self._openclaw_path, self._poll_interval,
        )
        while not self._stop_evt.is_set():
            try:
                # 全部阻塞操作放入线程，避免卡主 event loop
                await asyncio.to_thread(self._sync_batch)
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("[mobile_bridge] 轮询异常: %s", exc)
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

    # ── 生命周期 ──────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_evt.clear()
        self._task = asyncio.create_task(self._poll_loop(), name="mobile_bridge_poll")
        logger.info("[mobile_bridge] 已启动")

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        logger.info("[mobile_bridge] 已停止")

    # ── status（供 health endpoint 使用） ─────────────────────────────
    def status(self) -> Dict[str, Any]:
        # 单次 GROUP BY 拿全部 writeback 状态计数
        wb_counts: Dict[str, int] = {}
        try:
            with self._store._lock:  # noqa: SLF001
                for r in self._store._conn.execute(  # noqa: SLF001
                    "SELECT status, COUNT(*) FROM mobile_bridge_writeback_queue GROUP BY status"
                ).fetchall():
                    wb_counts[r[0]] = r[1]
        except Exception:
            pass
        dead_letter = wb_counts.get("dead_letter", 0)
        return {
            "ok": dead_letter == 0,
            "sync_ok": self._sync_ok,
            "sync_fail": self._sync_fail,
            "writeback_ok": self._writeback_ok,
            "writeback_fail": self._writeback_fail,
            "writeback_pending": wb_counts.get("pending", 0),
            "writeback_dead_letter": dead_letter,
            "last_sync_ts": self._last_sync_ts,
            "last_error": self._last_error,
            "watermark": self._get_watermark(),
            "openclaw_db_exists": Path(self._openclaw_path).exists(),
            "mobile_api_base": self._mobile_api,
        }

    # ── Telegram → Mobile 回写 ─────────────────────────────────────────
    def _call_mobile_api(
        self, path: str, body: Dict[str, Any], timeout: int = 10
    ) -> Dict[str, Any]:
        """向 mobile FastAPI 发 POST 请求。
        - 200: 正常返回
        - 409: 幂等成功（状态已是目标状态），返回空 dict
        - 其他: 抛 RuntimeError
        """
        url = f"{self._mobile_api}{path}"
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                # 已是目标状态，视为幂等成功
                return {"ok": True, "idempotent": True, "code": 409}
            detail = exc.read().decode(errors="replace")[:300]
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc

    def writeback_acknowledge(
        self, handoff_id: str, *, by: str = "telegram_admin", notes: str = ""
    ) -> Dict[str, Any]:
        return self._call_mobile_api(
            f"/lead-mesh/handoffs/{handoff_id}/acknowledge",
            {"by": by, "notes": notes},
        )

    def writeback_complete(
        self, handoff_id: str, *, by: str = "telegram_admin", notes: str = ""
    ) -> Dict[str, Any]:
        return self._call_mobile_api(
            f"/lead-mesh/handoffs/{handoff_id}/complete",
            {"by": by, "notes": notes},
        )

    def writeback_reject(
        self, handoff_id: str, *, by: str = "telegram_admin", notes: str = ""
    ) -> Dict[str, Any]:
        return self._call_mobile_api(
            f"/lead-mesh/handoffs/{handoff_id}/reject",
            {"by": by, "notes": notes},
        )

    # ── writeback 入队与重试 ───────────────────────────────────
    def enqueue_writeback(
        self, handoff_id: str, action: str, *, by: str = "", notes: str = ""
    ) -> int:
        """writeback 失败时入队持久化重试。返回队列 row id。"""
        try:
            with self._store._lock:  # noqa: SLF001
                cur = self._store._conn.execute(  # noqa: SLF001
                    "INSERT INTO mobile_bridge_writeback_queue"
                    "(handoff_id, action, by_user, notes, next_retry_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (handoff_id, action, by, notes, time.time()),
                )
                self._store._conn.commit()  # noqa: SLF001
            return cur.lastrowid or 0
        except Exception as exc:
            logger.warning("[mobile_bridge] enqueue_writeback 失败: %s", exc)
            return 0

    def _drain_writeback_queue(self) -> tuple:
        """处理 writeback 队列中待重试的条目。返回 (ok, fail)。"""
        now = time.time()
        try:
            with self._store._lock:  # noqa: SLF001
                pending = self._store._conn.execute(  # noqa: SLF001
                    "SELECT id, handoff_id, action, by_user, notes, attempt_count"
                    " FROM mobile_bridge_writeback_queue"
                    " WHERE status='pending' AND next_retry_at <= ?"
                    " ORDER BY id LIMIT 10",
                    (now,),
                ).fetchall()
        except Exception:
            return 0, 0

        ok = fail = 0
        for row in pending:
            row_id, handoff_id, action, by_user, notes, attempt = (
                row[0], row[1], row[2], row[3], row[4], row[5]
            )
            try:
                self._call_mobile_api(
                    f"/lead-mesh/handoffs/{handoff_id}/{action}",
                    {"by": by_user, "notes": notes},
                )
                with self._store._lock:  # noqa: SLF001
                    self._store._conn.execute(  # noqa: SLF001
                        "UPDATE mobile_bridge_writeback_queue"
                        " SET status='delivered', delivered_at=? WHERE id=?",
                        (time.time(), row_id),
                    )
                    self._store._conn.commit()  # noqa: SLF001
                self._writeback_ok += 1
                ok += 1
            except Exception as exc:
                attempt += 1
                new_status = "dead_letter" if attempt >= _MAX_ATTEMPTS else "pending"
                delay = _BACKOFF_SECS[min(attempt - 1, len(_BACKOFF_SECS) - 1)]
                next_retry = time.time() + delay if new_status == "pending" else 0
                try:
                    with self._store._lock:  # noqa: SLF001
                        self._store._conn.execute(  # noqa: SLF001
                            "UPDATE mobile_bridge_writeback_queue"
                            " SET status=?, attempt_count=?, last_error=?, next_retry_at=?"
                            " WHERE id=?",
                            (new_status, attempt, str(exc)[:300], next_retry, row_id),
                        )
                        self._store._conn.commit()  # noqa: SLF001
                except Exception:
                    pass
                self._writeback_fail += 1
                fail += 1
        return ok, fail

    def list_writeback_queue(
        self, status: str = "dead_letter", limit: int = 50
    ) -> List[Dict[str, Any]]:
        """列出 writeback 队列中特定状态的条目（默认 dead_letter）。"""
        try:
            with self._store._lock:  # noqa: SLF001
                self._store._conn.row_factory = sqlite3.Row  # noqa: SLF001
                rows = self._store._conn.execute(  # noqa: SLF001
                    "SELECT * FROM mobile_bridge_writeback_queue"
                    " WHERE status=? ORDER BY id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("[mobile_bridge] list_writeback_queue 失败: %s", exc)
            return []

    def retry_dead_letter(self, item_id: int) -> bool:
        """将 dead_letter 条目重置为 pending（attempt_count 清零）。返回是否更新成功。"""
        try:
            with self._store._lock:  # noqa: SLF001
                cur = self._store._conn.execute(  # noqa: SLF001
                    "UPDATE mobile_bridge_writeback_queue"
                    " SET status='pending', attempt_count=0, next_retry_at=?, last_error=''"
                    " WHERE id=? AND status='dead_letter'",
                    (time.time(), item_id),
                )
                self._store._conn.commit()  # noqa: SLF001
            return cur.rowcount > 0
        except Exception as exc:
            logger.warning("[mobile_bridge] retry_dead_letter 失败: %s", exc)
            return False

    def count_by_state(self) -> Dict[str, int]:
        """统计 openclaw.db 中各 state 的 handoff 数量。"""
        if not Path(self._openclaw_path).exists():
            return {}
        try:
            conn = sqlite3.connect(
                f"file:{self._openclaw_path}?mode=ro", uri=True, timeout=15
            )
            rows = conn.execute(
                "SELECT state, COUNT(*) AS cnt FROM lead_handoffs GROUP BY state"
            ).fetchall()
            conn.close()
            return {r[0]: r[1] for r in rows}
        except Exception as exc:
            logger.warning("[mobile_bridge] count_by_state 失败: %s", exc)
            return {}

    def list_mobile_handoffs(
        self,
        *,
        state: str = "",
        canonical_id: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """从 openclaw.db 查询 handoff 列表（只读）。"""
        if not Path(self._openclaw_path).exists():
            return []
        try:
            conn = sqlite3.connect(
                f"file:{self._openclaw_path}?mode=ro", uri=True, timeout=15
            )
            conn.row_factory = sqlite3.Row
            clauses, params = [], []
            if state:
                clauses.append("state=?")
                params.append(state)
            if canonical_id:
                clauses.append("canonical_id=?")
                params.append(canonical_id)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM lead_handoffs {where}"
                f" ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("[mobile_bridge] list_mobile_handoffs 失败: %s", exc)
            return []

    def get_mobile_handoff(self, handoff_id: str) -> Optional[Dict[str, Any]]:
        """从 openclaw.db 查单条 handoff。"""
        if not Path(self._openclaw_path).exists():
            return None
        try:
            conn = sqlite3.connect(
                f"file:{self._openclaw_path}?mode=ro", uri=True, timeout=15
            )
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM lead_handoffs WHERE handoff_id=?", (handoff_id,)
            ).fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception as exc:
            logger.warning("[mobile_bridge] get_mobile_handoff 失败: %s", exc)
            return None
