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
# 人审纠正样本（AI 失误数据资产）保留更久——供 prompt/KB 离线调优，但仍设上限防无限增长
_CORRECTION_RETENTION_SEC = 90 * 86400.0

# 闸门类型：(platform, account_id, *, config, registry) -> (blocked, reason)
GuardFn = Callable[..., Tuple[bool, str]]


def corrections_to_export(
    items: List[Dict[str, Any]], *, dedup: bool = True,
) -> List[Dict[str, Any]]:
    """纠正样本 → 偏好对导出形（纯函数，可单测）。

    每条映射为 ``{kind, source, platform, rejected, chosen, ai_suggestion, reason, ts}``：
    ``rejected``=原草稿(orig_text)、``chosen``=人定稿(new_text)——直接喂 DPO/偏好微调或 eval。
    cancel 样本 chosen 为空（仅负例 + reason）。``dedup`` 时按 (kind,rejected,chosen,reason) 去重。
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    for it in items or []:
        rec = {
            "kind": str(it.get("kind") or ""),
            "source": str(it.get("source") or ""),
            "platform": str(it.get("platform") or ""),
            "rejected": str(it.get("orig_text") or ""),
            "chosen": str(it.get("new_text") or ""),
            "ai_suggestion": str(it.get("ai_suggestion") or ""),
            "reason": str(it.get("reason") or ""),
            "ts": it.get("created_at"),
        }
        if dedup:
            key = (rec["kind"], rec["rejected"], rec["chosen"], rec["reason"])
            if key in seen:
                continue
            seen.add(key)
        out.append(rec)
    return out


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
            # 人审纠正留痕（P4.2）：append-only，每次「改写」记 before/after，「拦截+理由」记 reason。
            # 这是「AI 失误样本集」——供离线 prompt/KB 调优，故与命令表分离、保留更久。
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS desktop_corrections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    chat_key TEXT DEFAULT '',
                    kind TEXT NOT NULL,
                    orig_text TEXT DEFAULT '',
                    new_text TEXT DEFAULT '',
                    reason TEXT DEFAULT '',
                    ai_suggestion TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    created_at REAL
                )
                """
            )
            # P4.4：旧库（P4.2 schema）幂等补列——完整三元组(原草稿/AI候选/人定稿) + source 标注
            for _col in ("ai_suggestion", "source"):
                try:
                    self._conn.execute(
                        "ALTER TABLE desktop_corrections ADD COLUMN "
                        + _col + " TEXT DEFAULT ''")
                except Exception:
                    pass  # 已存在 → 忽略

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
        hold: bool = False,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """受控入队：先过闸门，通过才落库。

        ``hold=False``（默认）→ 落 ``pending``（可被 pull 自动发）；
        ``hold=True``（人审模式 review_mode）→ 落 ``held``（pull 不认领，等运营「放行」才转 pending）。
        **闸门恒在入队前执行**（即便 hold）——held 命令也是已过 Kill-Switch/反封号的，放行=发送已审命令。

        返回 ``{"enqueued": True, "id": <int>, "status": "pending"|"held"}``，或被拦截时
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
        status = "held" if hold else "pending"
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO desktop_outbound "
                "(platform, account_id, chat_key, conversation_id, text, kind, "
                " draft_id, status, attempts, created_at) "
                "VALUES (?,?,?,?,?,?,?, ?, 0, ?)",
                (p, a, ck, str(conversation_id or ""), body, str(kind or "text"),
                 str(draft_id or ""), status, ts),
            )
            rid = int(cur.lastrowid or 0)
        return {"enqueued": True, "id": rid, "status": status}

    # ── 读取：认领待发命令（桌面壳/扩展轮询）────────────────────────────
    def pull(
        self,
        platform: str,
        account_id: str,
        *,
        chat_key: Optional[str] = None,
        limit: int = 20,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """认领某账号的 pending 命令（pending→claimed，attempts+1），按 id 升序。

        ``chat_key`` 给定时**仅认领该会话**的命令——注入 ``fill-composer`` 只填当前打开的
        会话、不会按 chat_key 导航；客户端按「当前打开会话」拉取，其余命令留队列等会话打开，
        既不丢、也**绝不发错聊天**（防封号/防串话的关键安全闸）。

        认领前先回收**超时未 ack** 的 claimed（桌面壳崩溃/页面关闭），避免命令卡死。
        """
        p = str(platform or "").lower()
        a = str(account_id or "")
        ck = None if chat_key is None else str(chat_key)
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
            sql = ("SELECT * FROM desktop_outbound "
                   "WHERE platform=? AND account_id=? AND status='pending'")
            args: List[Any] = [p, a]
            if ck is not None:
                sql += " AND chat_key=?"
                args.append(ck)
            sql += " ORDER BY id ASC LIMIT ?"
            args.append(lim)
            rows = self._conn.execute(sql, tuple(args)).fetchall()
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

    # ── 人审介入（P2：拦截 / 改写 / 放行 / 暂停 / 重试）───────────────────
    def _transition(
        self, item_id: int, new_status: str, from_statuses: Tuple[str, ...],
        *, set_acked: Optional[float] = None, clear_reason: bool = False,
    ) -> bool:
        """受控状态迁移：仅当当前状态 ∈ from_statuses 才改为 new_status；返回是否命中。"""
        sets = "status=?"
        args: List[Any] = [new_status]
        if clear_reason:
            sets += ", reason=''"
        if set_acked is not None:
            sets += ", acked_at=?"
            args.append(float(set_acked))
        args.append(int(item_id))
        args.extend(from_statuses)
        placeholders = ",".join("?" for _ in from_statuses)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE desktop_outbound SET " + sets
                + " WHERE id=? AND status IN (" + placeholders + ")",
                tuple(args),
            )
            return int(cur.rowcount or 0) > 0

    def hold(self, item_id: int) -> bool:
        """暂停：pending→held（不再被 pull 自动发，等放行）。"""
        return self._transition(item_id, "held", ("pending",))

    def release(self, item_id: int) -> bool:
        """放行：held→pending（重新进入自动发送）。"""
        return self._transition(item_id, "pending", ("held",))

    def cancel(
        self, item_id: int, *, reason: str = "", now: Optional[float] = None,
    ) -> bool:
        """拦截：pending/held→cancelled（永不发送）。记 acked_at 供超龄清理。

        给定 ``reason`` 时留一条 cancel 纠正样本（「这条 AI 回复因…被拦」）；批量无理由拦截
        **不留样本**，避免用大量无理由 dismiss 污染失误数据集。
        """
        ts = float(now if now is not None else time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT text, platform, account_id, chat_key FROM desktop_outbound "
                "WHERE id=? AND status IN ('pending','held')",
                (int(item_id),),
            ).fetchone()
            if not row:
                return False
            self._conn.execute(
                "UPDATE desktop_outbound SET status='cancelled', acked_at=? WHERE id=?",
                (ts, int(item_id)),
            )
            rsn = str(reason or "").strip()
            if rsn:
                self._record_correction_locked(
                    "cancel", row, orig=str(row["text"] or ""), new="", reason=rsn)
        return True

    def retry(self, item_id: int) -> bool:
        """重试：failed→pending（清 reason，重新排队等 pull）。"""
        return self._transition(
            item_id, "pending", ("failed",), clear_reason=True)

    def edit(
        self, item_id: int, text: str,
        *, ai_suggestion: str = "", source: str = "",
    ) -> bool:
        """改写：仅 pending/held 可改文本（claimed/已终态不可改，防改飞行中/已发）。空文本拒。

        文本确有变化时自动留一条 edit 纠正样本——最强「AI 失误」标注信号，零额外 UI。
        P4.4：``ai_suggestion`` 给定时记下 AI 候选，``source`` 标注定稿来源
        （human / ai_adopted / ai_edited），凑成「原草稿→AI候选→人定稿」黄金三元组。
        """
        body = str(text or "").strip()
        if not body:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT text, platform, account_id, chat_key FROM desktop_outbound "
                "WHERE id=? AND status IN ('pending','held')",
                (int(item_id),),
            ).fetchone()
            if not row:
                return False
            old = str(row["text"] or "")
            self._conn.execute(
                "UPDATE desktop_outbound SET text=? WHERE id=?", (body, int(item_id)))
            if old != body:
                self._record_correction_locked(
                    "edit", row, orig=old, new=body, reason="",
                    ai_suggestion=str(ai_suggestion or ""),
                    source=str(source or "human"))
        return True

    def _record_correction_locked(
        self, kind: str, row: sqlite3.Row, *, orig: str, new: str, reason: str,
        ai_suggestion: str = "", source: str = "",
    ) -> None:
        """记一条纠正样本（调用方已持锁）。best-effort，不得因留痕失败影响主操作。"""
        try:
            self._conn.execute(
                "INSERT INTO desktop_corrections "
                "(platform, account_id, chat_key, kind, orig_text, new_text, reason, "
                " ai_suggestion, source, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (row["platform"], row["account_id"], row["chat_key"] or "",
                 str(kind), str(orig or ""), str(new or ""), str(reason or ""),
                 str(ai_suggestion or ""), str(source or ""), time.time()),
            )
        except Exception:
            logger.debug("[desktop_outbound] 纠正留痕失败（已忽略）", exc_info=True)

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

    def get(self, item_id: int) -> Optional[Dict[str, Any]]:
        """取单条命令（含 platform/account_id/chat_key/text），找不到返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM desktop_outbound WHERE id=?", (int(item_id),)
            ).fetchone()
        return self._row_to_item(row) if row else None

    def review_list(self, limit: int = 50) -> List[Dict[str, Any]]:
        """待审(held)命令，FIFO（id 升序）——人审队列按到达顺序处理。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM desktop_outbound WHERE status='held' "
                "ORDER BY id ASC LIMIT ?",
                (max(1, min(int(limit or 50), 200)),),
            ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def review_oldest_age(self, now: Optional[float] = None) -> float:
        """最久待审(held)命令已等待秒数（无待审→0）。用于人审 SLA 超时告警。"""
        ts = float(now if now is not None else time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(created_at) AS m FROM desktop_outbound WHERE status='held'"
            ).fetchone()
        m = row["m"] if row else None
        if m is None:
            return 0.0
        return max(0.0, ts - float(m))

    def intercept_rate(self) -> Tuple[float, int]:
        """近期拦截率：cancelled / (sent+failed+cancelled)。返回 (rate, sample)。

        分母为「已终结」命令数；held（待审）不计入。终态超 7 天会被 prune，故为**滚动近 7 日**窗口。
        """
        s = self.summary()
        sent = int(s.get("sent", 0))
        failed = int(s.get("failed", 0))
        cancelled = int(s.get("cancelled", 0))
        denom = sent + failed + cancelled
        return ((cancelled / denom) if denom else 0.0), denom

    def corrections(
        self, limit: int = 100,
        *, kind: Optional[str] = None, source: Optional[str] = None,
        since: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """近期人审纠正样本（最新在前）——AI 失误数据集，供离线调优/导出。

        可按 ``kind``（edit/cancel）、``source``（human/ai_adopted/ai_edited）、
        ``since``（created_at ≥ 秒级时间戳）过滤，用于增量/分类导出。
        """
        sql = "SELECT * FROM desktop_corrections"
        where: List[str] = []
        args: List[Any] = []
        if kind:
            where.append("kind=?")
            args.append(str(kind))
        if source:
            where.append("source=?")
            args.append(str(source))
        if since is not None:
            where.append("created_at >= ?")
            args.append(float(since))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit or 100), 5000)))
        with self._lock:
            rows = self._conn.execute(sql, tuple(args)).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "id": int(r["id"]),
                "platform": r["platform"],
                "account_id": r["account_id"],
                "chat_key": r["chat_key"] or "",
                "kind": r["kind"],
                "orig_text": r["orig_text"] or "",
                "new_text": r["new_text"] or "",
                "reason": r["reason"] or "",
                "ai_suggestion": (r["ai_suggestion"] if "ai_suggestion" in r.keys() else "") or "",
                "source": (r["source"] if "source" in r.keys() else "") or "",
                "created_at": r["created_at"],
            })
        return out

    def corrections_summary(self) -> Dict[str, int]:
        """纠正样本计数（看板读数用）：{edit, cancel, total, ai_assisted}。

        ``ai_assisted`` = source ∈ {ai_adopted, ai_edited} 的条数（AI 候选被采纳/微调）。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, source, COUNT(*) AS n FROM desktop_corrections "
                "GROUP BY kind, source"
            ).fetchall()
        out: Dict[str, int] = {}
        ai_assisted = 0
        for r in rows:
            n = int(r["n"])
            out[str(r["kind"])] = out.get(str(r["kind"]), 0) + n
            if str(r["source"] or "") in ("ai_adopted", "ai_edited"):
                ai_assisted += n
        out["total"] = sum(v for k, v in out.items())
        out["ai_assisted"] = ai_assisted
        return out

    def corrections_reason_breakdown(self) -> Dict[str, int]:
        """拦截理由聚类（P7）：按 reason 分组计 cancel 样本数（仅非空 reason）。

        reason 由前端传结构化 code（off_topic/tone/factual/...）；这里 label-agnostic，
        只按存储值分组——展示层负责 code→中文映射。供「AI 在哪类问题最常错」洞察。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT reason, COUNT(*) AS n FROM desktop_corrections "
                "WHERE kind='cancel' AND reason != '' GROUP BY reason"
            ).fetchall()
        return {str(r["reason"]): int(r["n"]) for r in rows}

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
                    "WHERE status IN ('sent','failed','cancelled') "
                    "AND acked_at IS NOT NULL AND (? - acked_at) > ?",
                    (now, _RETENTION_SEC),
                )
                self._conn.execute(
                    "DELETE FROM desktop_corrections "
                    "WHERE created_at IS NOT NULL AND (? - created_at) > ?",
                    (now, _CORRECTION_RETENTION_SEC),
                )
        except Exception:
            logger.debug("[desktop_outbound] prune 失败（已忽略）", exc_info=True)

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM desktop_outbound")
            self._conn.execute("DELETE FROM desktop_corrections")

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
    "corrections_to_export",
]
