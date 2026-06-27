"""桌面内嵌账号「受控出站队列」（D4：双向收件箱桥 + 受控 autopilot）。

桌面壳 / 浏览器扩展内嵌的官方网页账号（registry ``mode="desktop"``）**没有服务端
worker**，编排器不接管它们——所以统一收件箱的全自动 autopilot 此前无法把回复发给这些
账号（``send_via_adapters`` 找不到 adapter）。本模块补上这条出站路径：

  autopilot 决定要发 → ``enqueue()`` **先过 send-gate / kill-switch 闸门** → 通过才落库为
  pending 命令 → 桌面壳 / 扩展轮询 ``pull()`` 取走 → 在官方网页 DOM 填入并发送 → ``ack()``。

**「受控」的关键不变式**：闸门检查写在 ``enqueue()`` **内部**——任何命令进队列前都必过
Kill-Switch（恒查）+ 反封号闸门（``companion_send_gate.enabled`` 时），**没有旁路**。被拦截的
命令**根本不入队**（返回 ``{"enqueued": False, "blocked": <reason>}``），autopilot 据此记
``autosend_failed`` 而非误判已送达。

durable（SQLite）而非纯内存：autopilot 入队后即便后端重启，桌面壳重连仍能 ``pull`` 到未发命令，
不丢主动回复（但 resolve-先于-deliver 的语义由上层 worker 保证，这里只负责命令暂存与去并发）。

默认不参与任何流程：仅当 ``inbox.l2_autosend.desktop_bridge.enabled=true`` 且会话账号
``mode="desktop"`` 时，main.py 的 autosend 投递回调才会路由到这里（双重 opt-in，零行为变更）。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 默认库路径（与 kill_switch 的 runtime_flags.db 同目录约定，可由 getter 覆盖）
_DEFAULT_DB = os.path.join("config", "desktop_outbound.db")

# claimed 但迟迟未 ack（桌面壳崩溃/页面被关）→ 超过此秒数自动回收为 pending 可重取
_RECLAIM_AFTER_SEC = 180.0
# 已终态（sent/failed）保留天数，enqueue 时顺手清理，防表无限增长
_RETENTION_SEC = 7 * 86400.0

# 闸门类型：(platform, account_id, *, config, registry) -> (blocked, reason)
GuardFn = Callable[..., Tuple[bool, str]]


def _default_guard(platform: str, account_id: str, *, config=None, registry=None):
    """默认闸门 = 编排器统一守卫（Kill-Switch + 反封号闸门）。

    延迟 import 避免模块级循环依赖；任何异常 fail-open（与 send_blocked 同语义）。
    """
    try:
        from src.integrations.shared.send_guard import send_blocked
        return send_blocked(platform, account_id, config=config, registry=registry)
    except Exception:  # noqa: BLE001
        return False, ""


class DesktopOutboundQueue:
    """线程安全、SQLite 持久化的桌面出站命令队列（闸门内置）。"""

    def __init__(self, db_path: str = _DEFAULT_DB) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        if db_path != ":memory:":
            try:
                os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
            except Exception:
                pass
        # 单连接 + 锁（与 kill_switch 同模式）：:memory: 必须共用同一连接才不丢表
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS desktop_outbound (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    chat_key TEXT NOT NULL,
                    conversation_id TEXT DEFAULT '',
                    text TEXT NOT NULL,
                    kind TEXT DEFAULT 'text',
                    draft_id TEXT DEFAULT '',
                    status TEXT DEFAULT 'pending',
                    reason TEXT DEFAULT '',
                    attempts INTEGER DEFAULT 0,
                    created_at REAL,
                    claimed_at REAL,
                    acked_at REAL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dob_claim "
                "ON desktop_outbound(platform, account_id, status, id)"
            )

    # ── 写入：受控入队 ────────────────────────────────────────────────
    def enqueue(
        self,
        platform: str,
        account_id: str,
        chat_key: str,
        text: str,
        *,
        conversation_id: str = "",
        kind: str = "text",
        draft_id: str = "",
        config: Optional[Dict[str, Any]] = None,
        registry: Any = None,
        guard: Optional[GuardFn] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """受控入队：先过闸门，通过才落 pending。

        返回 ``{"enqueued": True, "id": <int>, "status": "pending"}``，或被拦截时
        ``{"enqueued": False, "blocked": "kill_switch:.../send_gate:..."}``。
        text 为空直接拒（``{"enqueued": False, "blocked": "empty_text"}``）。
        """
        p = str(platform or "").lower()
        a = str(account_id or "")
        ck = str(chat_key or "")
        body = str(text or "").strip()
        if not p or not a or not ck:
            return {"enqueued": False, "blocked": "missing_key"}
        if not body:
            return {"enqueued": False, "blocked": "empty_text"}
        # ★ 受控不变式：入队前必过闸门（Kill-Switch 恒查 + 反封号闸门按开关）
        g = guard or _default_guard
        try:
            blocked, reason = g(p, a, config=config, registry=registry)
        except Exception:  # noqa: BLE001
            blocked, reason = False, ""  # broken guard 不得卡死出站
        if blocked:
            logger.info(
                "[desktop_outbound] 出站被闸门拦截 %s:%s (%s)", p, a, reason)
            return {"enqueued": False, "blocked": reason or "blocked"}
        ts = float(now if now is not None else time.time())
        self._prune(ts)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO desktop_outbound "
                "(platform, account_id, chat_key, conversation_id, text, kind, "
                " draft_id, status, attempts, created_at) "
                "VALUES (?,?,?,?,?,?,?, 'pending', 0, ?)",
                (p, a, ck, str(conversation_id or ""), body, str(kind or "text"),
                 str(draft_id or ""), ts),
            )
            rid = int(cur.lastrowid or 0)
        return {"enqueued": True, "id": rid, "status": "pending"}

    # ── 读取：认领待发命令（桌面壳/扩展轮询）────────────────────────────
    def pull(
        self,
        platform: str,
        account_id: str,
        *,
        limit: int = 20,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """认领某账号的 pending 命令（pending→claimed，attempts+1），按 id 升序。

        认领前先回收**超时未 ack** 的 claimed（桌面壳崩溃/页面关闭），避免命令卡死。
        """
        p = str(platform or "").lower()
        a = str(account_id or "")
        lim = max(1, min(int(limit or 20), 100))
        ts = float(now if now is not None else time.time())
        with self._lock:
            # 回收超时 claimed
            self._conn.execute(
                "UPDATE desktop_outbound SET status='pending' "
                "WHERE platform=? AND account_id=? AND status='claimed' "
                "AND claimed_at IS NOT NULL AND (? - claimed_at) > ?",
                (p, a, ts, _RECLAIM_AFTER_SEC),
            )
            rows = self._conn.execute(
                "SELECT * FROM desktop_outbound "
                "WHERE platform=? AND account_id=? AND status='pending' "
                "ORDER BY id ASC LIMIT ?",
                (p, a, lim),
            ).fetchall()
            items: List[Dict[str, Any]] = []
            for r in rows:
                rid = int(r["id"])
                self._conn.execute(
                    "UPDATE desktop_outbound "
                    "SET status='claimed', claimed_at=?, attempts=attempts+1 "
                    "WHERE id=?",
                    (ts, rid),
                )
                it = self._row_to_item(r)
                # rows 是 UPDATE 前快照：返回认领后的真实态（claimed + attempts 已 +1）
                it["status"] = "claimed"
                it["attempts"] = int(r["attempts"] or 0) + 1
                items.append(it)
        return items

    # ── 回执：客户端发完确认 ──────────────────────────────────────────
    def ack(
        self,
        item_id: int,
        *,
        ok: bool = True,
        error: str = "",
        now: Optional[float] = None,
    ) -> bool:
        """客户端发送后回执：claimed→sent / failed。返回是否命中一条记录。"""
        ts = float(now if now is not None else time.time())
        status = "sent" if ok else "failed"
        with self._lock:
            cur = self._conn.execute(
                "UPDATE desktop_outbound SET status=?, reason=?, acked_at=? "
                "WHERE id=? AND status IN ('claimed','pending')",
                (status, str(error or ""), ts, int(item_id)),
            )
            return int(cur.rowcount or 0) > 0

    # ── 可观测 ───────────────────────────────────────────────────────
    def pending_count(
        self, platform: Optional[str] = None, account_id: Optional[str] = None,
    ) -> int:
        sql = ("SELECT COUNT(*) AS n FROM desktop_outbound "
               "WHERE status IN ('pending','claimed')")
        args: List[Any] = []
        if platform:
            sql += " AND platform=?"
            args.append(str(platform).lower())
        if account_id:
            sql += " AND account_id=?"
            args.append(str(account_id))
        with self._lock:
            row = self._conn.execute(sql, tuple(args)).fetchone()
        return int(row["n"] if row else 0)

    def summary(self) -> Dict[str, int]:
        """按状态计数（看板/健康探针用）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM desktop_outbound GROUP BY status"
            ).fetchall()
        out: Dict[str, int] = {}
        for r in rows:
            out[str(r["status"])] = int(r["n"])
        out["total"] = sum(out.values())
        return out

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM desktop_outbound ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit or 50), 500)),),
            ).fetchall()
        return [self._row_to_item(r) for r in rows]

    # ── 内部 ─────────────────────────────────────────────────────────
    @staticmethod
    def _row_to_item(r: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": int(r["id"]),
            "platform": r["platform"],
            "account_id": r["account_id"],
            "chat_key": r["chat_key"],
            "conversation_id": r["conversation_id"] or "",
            "text": r["text"],
            "kind": r["kind"] or "text",
            "draft_id": r["draft_id"] or "",
            "status": r["status"],
            "attempts": int(r["attempts"] or 0),
            "created_at": r["created_at"],
        }

    def _prune(self, now: float) -> None:
        """清理超龄终态记录（best-effort，调用方已持/未持锁均安全：内部自锁）。"""
        try:
            with self._lock:
                self._conn.execute(
                    "DELETE FROM desktop_outbound "
                    "WHERE status IN ('sent','failed') AND acked_at IS NOT NULL "
                    "AND (? - acked_at) > ?",
                    (now, _RETENTION_SEC),
                )
        except Exception:
            logger.debug("[desktop_outbound] prune 失败（已忽略）", exc_info=True)

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM desktop_outbound")

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


_QUEUE: Optional[DesktopOutboundQueue] = None
_QUEUE_LOCK = threading.Lock()


def get_desktop_outbound_queue(db_path: Optional[str] = None) -> DesktopOutboundQueue:
    """进程级单例（与 inject_health / kill_switch 同模式）。

    首次调用决定 db_path（默认 ``config/desktop_outbound.db``，可被环境变量
    ``DESKTOP_OUTBOUND_DB`` 覆盖）。后续调用忽略 db_path（已初始化）。
    """
    global _QUEUE
    if _QUEUE is None:
        with _QUEUE_LOCK:
            if _QUEUE is None:
                path = (db_path or os.environ.get("DESKTOP_OUTBOUND_DB")
                        or _DEFAULT_DB)
                _QUEUE = DesktopOutboundQueue(path)
    return _QUEUE


def reset_desktop_outbound_queue(queue: Optional[DesktopOutboundQueue] = None) -> None:
    """测试用：替换/清空进程单例。"""
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is not None and queue is not _QUEUE:
            _QUEUE.close()
        _QUEUE = queue


__all__ = [
    "DesktopOutboundQueue",
    "get_desktop_outbound_queue",
    "reset_desktop_outbound_queue",
]
