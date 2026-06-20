"""Phase K2：C 端变现持久层（SQLite）。

存端用户（``contact_key``）的**订阅 / 解锁 / 交易流水**，是变现闭环的真相源：
- ``subscriptions``：每端用户当前会员档（tier + 有效期），upsert（一人一条当前订阅）。
- ``unlocks``：端用户已买断的一次性内容项（contact+item 幂等，不重复）。
- ``tx_ledger``：所有金钱事件（subscribe/unlock/gift），``ref`` 唯一 → 支付回调幂等。

镜像 ``care_schedule`` / ``crisis_event_store`` 约定：单连接 ``check_same_thread=False`` +
写操作 ``threading.Lock`` + **绝不抛**（变现不可用不能拖垮陪伴主流程）。支持 ``:memory:``
（测试零落盘）与文件路径（生产落 config 目录）双模式。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils.monetization import (
    DEFAULT_CATALOG,
    effective_tier,
    merge_catalog,
    quote,
    tier_grants,
)

logger = logging.getLogger("EntitlementStore")

_TX_STATUSES = ("paid", "refunded", "failed")


class EntitlementStore:
    _DDL = """
    CREATE TABLE IF NOT EXISTS subscriptions (
        contact_key  TEXT PRIMARY KEY,
        tier         TEXT NOT NULL DEFAULT 'free',
        active_until REAL NOT NULL DEFAULT 0,
        status       TEXT NOT NULL DEFAULT 'active',
        source       TEXT NOT NULL DEFAULT '',
        started_at   REAL NOT NULL,
        updated_at   REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS unlocks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_key TEXT NOT NULL,
        item_id     TEXT NOT NULL,
        source      TEXT NOT NULL DEFAULT '',
        ref         TEXT NOT NULL DEFAULT '',
        ts          REAL NOT NULL,
        UNIQUE(contact_key, item_id)
    );
    CREATE TABLE IF NOT EXISTS tx_ledger (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_key TEXT NOT NULL,
        kind        TEXT NOT NULL,
        item_id     TEXT NOT NULL DEFAULT '',
        amount      REAL NOT NULL DEFAULT 0,
        currency    TEXT NOT NULL DEFAULT 'USD',
        status      TEXT NOT NULL DEFAULT 'paid',
        source      TEXT NOT NULL DEFAULT '',
        ref         TEXT NOT NULL DEFAULT '',
        note        TEXT NOT NULL DEFAULT '',
        ts          REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tx_ts ON tx_ledger(ts);
    CREATE INDEX IF NOT EXISTS idx_tx_contact ON tx_ledger(contact_key);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_tx_ref ON tx_ledger(ref) WHERE ref != '';
    CREATE INDEX IF NOT EXISTS idx_unlock_contact ON unlocks(contact_key);
    """

    def __init__(self, db_path, *, catalog: Optional[Dict[str, Any]] = None):
        self._db_path = db_path if db_path == ":memory:" else Path(db_path)
        self._catalog = catalog or DEFAULT_CATALOG
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

    @property
    def catalog(self) -> Dict[str, Any]:
        return self._catalog

    def set_catalog(self, catalog: Dict[str, Any]) -> None:
        self._catalog = catalog or DEFAULT_CATALOG

    # ── 写入 ────────────────────────────────────────────────────────────
    def record_tx(
        self,
        *,
        contact_key: str,
        kind: str,
        item_id: str = "",
        amount: float = 0.0,
        currency: str = "",
        status: str = "paid",
        source: str = "",
        ref: str = "",
        note: str = "",
        now: Optional[float] = None,
    ) -> Optional[int]:
        """落一条交易流水。``ref`` 非空且重复 → 幂等跳过返回 None（支付回调重投安全）。"""
        n = float(now if now is not None else time.time())
        cur_code = str(currency or self._catalog.get("currency") or "USD")
        st = str(status or "paid").lower()
        if st not in _TX_STATUSES:
            st = "paid"
        try:
            with self._lock:
                if ref:
                    dup = self._conn.execute(
                        "SELECT id FROM tx_ledger WHERE ref = ? LIMIT 1", (str(ref),)
                    ).fetchone()
                    if dup:
                        return None
                c = self._conn.execute(
                    "INSERT INTO tx_ledger (contact_key, kind, item_id, amount, currency,"
                    " status, source, ref, note, ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (str(contact_key), str(kind), str(item_id), float(amount or 0),
                     cur_code, st, str(source)[:64], str(ref)[:128],
                     str(note)[:200], n),
                )
                self._conn.commit()
                return int(c.lastrowid) if c.lastrowid else None
        except Exception as e:  # noqa: BLE001
            logger.debug("record_tx failed: %s", e)
            return None

    def grant_subscription(
        self,
        contact_key: str,
        tier: str,
        active_until: float,
        *,
        source: str = "",
        ref: str = "",
        amount: Optional[float] = None,
        now: Optional[float] = None,
        record_ledger: bool = True,
    ) -> bool:
        """开通/续费会员（upsert 当前订阅）。``record_ledger`` 时按 catalog 月费入账。

        幂等：若提供 ``ref`` 且流水已存在，则**不重复入账也不改订阅**（返回 False）。
        """
        n = float(now if now is not None else time.time())
        ck = str(contact_key or "").strip()
        if not ck:
            return False
        tx_id = None
        if record_ledger:
            q = quote("subscribe", tier, self._catalog) or {}
            amt = float(amount if amount is not None else q.get("amount", 0))
            tx_id = self.record_tx(
                contact_key=ck, kind="subscribe", item_id=str(tier),
                amount=amt, source=source, ref=ref, now=n,
                note=f"subscribe {tier}",
            )
            if ref and tx_id is None:
                return False  # 幂等：该 ref 已处理过
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO subscriptions (contact_key, tier, active_until, status,"
                    " source, started_at, updated_at) VALUES (?,?,?, 'active', ?, ?, ?)"
                    " ON CONFLICT(contact_key) DO UPDATE SET tier=excluded.tier,"
                    " active_until=excluded.active_until, status='active',"
                    " source=excluded.source, updated_at=excluded.updated_at",
                    (ck, str(tier), float(active_until), str(source)[:64], n, n),
                )
                self._conn.commit()
                return True
        except Exception as e:  # noqa: BLE001
            logger.debug("grant_subscription failed: %s", e)
            return False

    def record_unlock(
        self,
        contact_key: str,
        item_id: str,
        *,
        source: str = "",
        ref: str = "",
        amount: Optional[float] = None,
        now: Optional[float] = None,
        record_ledger: bool = True,
    ) -> bool:
        """买断一次性内容项（contact+item 幂等）。返回是否新解锁（已持有 → False）。"""
        n = float(now if now is not None else time.time())
        ck = str(contact_key or "").strip()
        iid = str(item_id or "").strip()
        if not ck or not iid:
            return False
        try:
            with self._lock:
                exists = self._conn.execute(
                    "SELECT 1 FROM unlocks WHERE contact_key = ? AND item_id = ? LIMIT 1",
                    (ck, iid),
                ).fetchone()
                if exists:
                    return False
                self._conn.execute(
                    "INSERT INTO unlocks (contact_key, item_id, source, ref, ts)"
                    " VALUES (?,?,?,?,?)",
                    (ck, iid, str(source)[:64], str(ref)[:128], n),
                )
                self._conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("record_unlock failed: %s", e)
            return False
        if record_ledger:
            q = quote("unlock", iid, self._catalog) or {}
            amt = float(amount if amount is not None else q.get("amount", 0))
            self.record_tx(
                contact_key=ck, kind="unlock", item_id=iid, amount=amt,
                source=source, ref=ref, now=n, note=f"unlock {iid}",
            )
        return True

    def record_gift(
        self,
        contact_key: str,
        item_id: str,
        *,
        amount: Optional[float] = None,
        source: str = "",
        ref: str = "",
        now: Optional[float] = None,
    ) -> Optional[int]:
        """打赏/虚拟礼物：纯入账（不授予功能位）。返回流水 id（幂等重投 → None）。"""
        n = float(now if now is not None else time.time())
        q = quote("gift", str(item_id), self._catalog) or {}
        amt = float(amount if amount is not None else q.get("amount", 0))
        return self.record_tx(
            contact_key=str(contact_key), kind="gift", item_id=str(item_id),
            amount=amt, source=source, ref=ref, now=n, note=f"gift {item_id}",
        )

    # ── 查询 ────────────────────────────────────────────────────────────
    def _subscription_row(self, contact_key: str) -> Optional[Dict[str, Any]]:
        try:
            r = self._conn.execute(
                "SELECT contact_key, tier, active_until, status, source, started_at,"
                " updated_at FROM subscriptions WHERE contact_key = ?",
                (str(contact_key),),
            ).fetchone()
        except Exception:
            return None
        if not r:
            return None
        return {
            "contact_key": r[0], "tier": r[1], "active_until": r[2],
            "status": r[3], "source": r[4], "started_at": r[5], "updated_at": r[6],
        }

    def unlocked_items(self, contact_key: str) -> List[str]:
        try:
            rows = self._conn.execute(
                "SELECT item_id FROM unlocks WHERE contact_key = ? ORDER BY ts ASC",
                (str(contact_key),),
            ).fetchall()
            return [str(r[0]) for r in rows]
        except Exception:
            return []

    def is_unlocked(self, contact_key: str, item_id: str) -> bool:
        try:
            r = self._conn.execute(
                "SELECT 1 FROM unlocks WHERE contact_key = ? AND item_id = ? LIMIT 1",
                (str(contact_key), str(item_id)),
            ).fetchone()
            return bool(r)
        except Exception:
            return False

    def get_entitlement(self, contact_key: str, *, now: Optional[float] = None) -> Dict[str, Any]:
        """端用户当前权益快照：有效 tier + grants + 已解锁项。绝不抛（缺则 free）。"""
        n = float(now if now is not None else time.time())
        sub = self._subscription_row(contact_key) or {}
        raw_tier = str(sub.get("tier") or "free")
        active_until = float(sub.get("active_until") or 0)
        eff = effective_tier(raw_tier, active_until, n)
        unlocked = self.unlocked_items(contact_key)
        return {
            "contact_key": str(contact_key),
            "tier": eff,
            "tier_raw": raw_tier,
            "active": eff != "free",
            "active_until": active_until,
            "grants": sorted(tier_grants(eff, self._catalog)),
            "unlocked": unlocked,
        }

    def recent_tx(self, *, limit: int = 50, before_ts: Optional[float] = None) -> List[Dict[str, Any]]:
        lim = max(1, min(int(limit or 50), 500))
        try:
            if before_ts and before_ts > 0:
                rows = self._conn.execute(
                    "SELECT id, contact_key, kind, item_id, amount, currency, status,"
                    " source, ref, note, ts FROM tx_ledger WHERE ts < ?"
                    " ORDER BY ts DESC LIMIT ?",
                    (float(before_ts), lim),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, contact_key, kind, item_id, amount, currency, status,"
                    " source, ref, note, ts FROM tx_ledger ORDER BY ts DESC LIMIT ?",
                    (lim,),
                ).fetchall()
        except Exception:
            return []
        cols = ["id", "contact_key", "kind", "item_id", "amount", "currency",
                "status", "source", "ref", "note", "ts"]
        return [dict(zip(cols, r)) for r in rows]

    def revenue_summary(self, *, since: float = 0, until: Optional[float] = None) -> Dict[str, Any]:
        """营收聚合（单次 SQL）：总额 + 按 kind 分组 + 计数，仅计 status='paid'。"""
        lo = float(since or 0)
        hi = float(until) if until is not None else time.time() + 1
        out = {"total": 0.0, "count": 0, "by_kind": {},
               "currency": str(self._catalog.get("currency") or "USD"),
               "since": lo, "until": hi}
        try:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*), COALESCE(SUM(amount),0) FROM tx_ledger"
                " WHERE status='paid' AND ts >= ? AND ts < ? GROUP BY kind",
                (lo, hi),
            ).fetchall()
        except Exception:
            return out
        total = 0.0
        cnt = 0
        for kind, c, amt in rows:
            a = round(float(amt or 0), 2)
            out["by_kind"][str(kind)] = {"amount": a, "count": int(c)}
            total += a
            cnt += int(c)
        out["total"] = round(total, 2)
        out["count"] = cnt
        return out

    def active_subscription_count(self, *, now: Optional[float] = None) -> int:
        n = float(now if now is not None else time.time())
        try:
            r = self._conn.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE status='active'"
                " AND tier != 'free' AND active_until > ?",
                (n,),
            ).fetchone()
            return int(r[0]) if r else 0
        except Exception:
            return 0

    def active_subscriptions(self, *, now: Optional[float] = None,
                             limit: int = 200) -> List[Dict[str, Any]]:
        n = float(now if now is not None else time.time())
        lim = max(1, min(int(limit or 200), 1000))
        try:
            rows = self._conn.execute(
                "SELECT contact_key, tier, active_until, source, updated_at"
                " FROM subscriptions WHERE status='active' AND tier != 'free'"
                " AND active_until > ? ORDER BY active_until ASC LIMIT ?",
                (n, lim),
            ).fetchall()
        except Exception:
            return []
        cols = ["contact_key", "tier", "active_until", "source", "updated_at"]
        return [dict(zip(cols, r)) for r in rows]

    def spend_by_contacts(self, contact_keys, *, since: float = 0,
                          until: Optional[float] = None) -> Dict[str, float]:
        """批量取多个端用户的累计已付金额（LTV）。避免健康榜 N+1。"""
        keys = [str(k) for k in (contact_keys or []) if str(k)]
        if not keys:
            return {}
        lo = float(since or 0)
        hi = float(until) if until is not None else time.time() + 1
        out: Dict[str, float] = {}
        try:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                ph = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT contact_key, COALESCE(SUM(amount),0) FROM tx_ledger"
                    f" WHERE status='paid' AND ts >= ? AND ts < ?"
                    f" AND contact_key IN ({ph}) GROUP BY contact_key",
                    (lo, hi, *chunk),
                ).fetchall()
                for r in rows:
                    out[str(r[0])] = round(float(r[1] or 0), 2)
        except Exception as e:  # noqa: BLE001
            logger.debug("spend_by_contacts failed: %s", e)
        return out

    def tiers_by_contacts(self, contact_keys, *, now: Optional[float] = None) -> Dict[str, str]:
        """批量取多个端用户当前**有效**会员档（非 free 且未过期）。"""
        n = float(now if now is not None else time.time())
        keys = [str(k) for k in (contact_keys or []) if str(k)]
        if not keys:
            return {}
        out: Dict[str, str] = {}
        try:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                ph = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT contact_key, tier FROM subscriptions"
                    f" WHERE status='active' AND tier != 'free' AND active_until > ?"
                    f" AND contact_key IN ({ph})",
                    (n, *chunk),
                ).fetchall()
                for r in rows:
                    out[str(r[0])] = str(r[1])
        except Exception as e:  # noqa: BLE001
            logger.debug("tiers_by_contacts failed: %s", e)
        return out

    def top_spenders(self, *, since: float = 0, until: Optional[float] = None,
                     limit: int = 10) -> List[Dict[str, Any]]:
        lo = float(since or 0)
        hi = float(until) if until is not None else time.time() + 1
        lim = max(1, min(int(limit or 10), 100))
        try:
            rows = self._conn.execute(
                "SELECT contact_key, COALESCE(SUM(amount),0) AS spent, COUNT(*) AS txc"
                " FROM tx_ledger WHERE status='paid' AND ts >= ? AND ts < ?"
                " GROUP BY contact_key ORDER BY spent DESC LIMIT ?",
                (lo, hi, lim),
            ).fetchall()
        except Exception:
            return []
        return [{"contact_key": str(r[0]), "spent": round(float(r[1] or 0), 2),
                 "tx_count": int(r[2])} for r in rows]

    def expire_subscriptions(self, *, now: Optional[float] = None) -> int:
        """把已过期仍 active 的订阅标 expired（仅状态收敛；有效判定本就看 active_until）。"""
        n = float(now if now is not None else time.time())
        try:
            with self._lock:
                c = self._conn.execute(
                    "UPDATE subscriptions SET status='expired', updated_at=?"
                    " WHERE status='active' AND tier != 'free' AND active_until <= ?",
                    (n, n),
                )
                self._conn.commit()
                return int(c.rowcount)
        except Exception as e:  # noqa: BLE001
            logger.debug("expire_subscriptions failed: %s", e)
            return 0

    def cancel_subscription(self, contact_key: str, *, now: Optional[float] = None) -> bool:
        """⑦ 退订：立即把某端用户订阅标 expired 且 active_until=now（Stripe 退订事件触发）。"""
        n = float(now if now is not None else time.time())
        ck = str(contact_key or "").strip()
        if not ck:
            return False
        try:
            with self._lock:
                c = self._conn.execute(
                    "UPDATE subscriptions SET status='expired', active_until=?,"
                    " updated_at=? WHERE contact_key=?",
                    (n, n, ck),
                )
                self._conn.commit()
                return int(c.rowcount) > 0
        except Exception as e:  # noqa: BLE001
            logger.debug("cancel_subscription failed: %s", e)
            return False

    def lapsed_payers(self, *, recent_days: float = 30,
                      now: Optional[float] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """⑧ 流失付费挽回榜：有历史已付但近 N 天 0 付费的端用户，按累计 LTV 降序。

        每行含 ``ltv / last_paid_ts / days_since_paid / tier``（当前有效档，多已 free）。
        纯读、绝不抛。运营据此主动挽回（高 LTV 优先）。
        """
        n = float(now if now is not None else time.time())
        since = n - max(1.0, float(recent_days)) * 86400.0
        lim = max(1, min(int(limit or 50), 500))
        try:
            rows = self._conn.execute(
                "SELECT contact_key, COALESCE(SUM(amount),0) AS total, MAX(ts) AS last_ts,"
                " COALESCE(SUM(CASE WHEN ts >= ? THEN amount ELSE 0 END),0) AS recent"
                " FROM tx_ledger WHERE status='paid' GROUP BY contact_key"
                " HAVING total > 0 AND recent <= 0 ORDER BY total DESC LIMIT ?",
                (since, lim),
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug("lapsed_payers failed: %s", e)
            return []
        keys = [str(r[0]) for r in rows]
        # 取最后已知会员档（含已过期/退订；挽回时知道对方原来是什么档）
        raw_tiers: Dict[str, str] = {}
        try:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                ph = ",".join("?" * len(chunk))
                trows = self._conn.execute(
                    f"SELECT contact_key, tier FROM subscriptions"
                    f" WHERE contact_key IN ({ph})", tuple(chunk)).fetchall()
                for tr in trows:
                    raw_tiers[str(tr[0])] = str(tr[1])
        except Exception as e:  # noqa: BLE001
            logger.debug("lapsed_payers tier lookup failed: %s", e)
        out: List[Dict[str, Any]] = []
        for r in rows:
            ck = str(r[0])
            last_ts = float(r[2] or 0)
            out.append({
                "contact_key": ck,
                "ltv": round(float(r[1] or 0), 2),
                "last_paid_ts": last_ts,
                "days_since_paid": (round((n - last_ts) / 86400.0, 1)
                                    if last_ts else None),
                "tier": raw_tiers.get(ck, "free"),
                "recent_days": int(recent_days),
            })
        return out

    def count_tx(self) -> int:
        try:
            r = self._conn.execute("SELECT COUNT(*) FROM tx_ledger").fetchone()
            return int(r[0]) if r else 0
        except Exception:
            return 0


_singleton: Optional["EntitlementStore"] = None
_singleton_lock = threading.Lock()


def get_entitlement_store(db_path=None, *, catalog: Optional[Dict[str, Any]] = None) -> "EntitlementStore":
    """进程内单例。首次调用传入 db_path/catalog；之后返回同一实例（catalog 可再 set）。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = EntitlementStore(db_path or ":memory:", catalog=catalog)
    elif catalog is not None:
        _singleton.set_catalog(catalog)
    return _singleton


def reset_entitlement_store() -> None:
    """测试辅助：清空单例。"""
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            _singleton.close()
        _singleton = None


__all__ = ["EntitlementStore", "get_entitlement_store", "reset_entitlement_store"]
