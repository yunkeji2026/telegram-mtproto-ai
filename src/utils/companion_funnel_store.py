"""Stage 3：付费解锁预告「转化漏斗」持久层（SQLite）。

记录沉默期发出的**付费解锁预告**（``story_teaser``）事件，配合变现库 ``tx_ledger`` 的
已付流水做归因，回答运营最关心的问题：**这些预告真的把人推向付费了吗？**

- ``teaser_events``：每发出一条付费预告记一行（contact_key + scenario_id + feature + ts）。
- 转化由 ``funnel_stats`` **注入** ``paid_lookup``（``(contact_keys) -> {ck: [{item_id, kind, ts}]}``）
  在查询期归因：预告后 ``attribution_days`` 内该端用户有已付事件 → 记一次转化；item_id 命中
  预告 feature → 记一次「精确转化」（更强信号）。store 本身不耦合变现内部 → 可纯单测。

镜像 ``entitlement_store`` / ``crisis_event_store`` 约定：单连接 ``check_same_thread=False`` +
写操作 ``threading.Lock`` + **绝不抛**（埋点失败不能拖垮主动发送主流程）。支持 ``:memory:``
（测试零落盘）与文件路径（生产落 config 目录）双模式。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("CompanionFunnelStore")

# 注入式已付查询器：给定 contact_key 列表 → {ck: [{item_id, kind, ts}, ...]}（仅已付事件）。
PaidLookup = Callable[[List[str]], Dict[str, List[Dict[str, Any]]]]

_DAY = 86400.0


class CompanionFunnelStore:
    _DDL = """
    CREATE TABLE IF NOT EXISTS teaser_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_key TEXT NOT NULL,
        scenario_id TEXT NOT NULL DEFAULT '',
        feature     TEXT NOT NULL DEFAULT '',
        ts          REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_teaser_ts ON teaser_events(ts);
    CREATE INDEX IF NOT EXISTS idx_teaser_contact ON teaser_events(contact_key);
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
    def record_teaser(
        self,
        contact_key: str,
        scenario_id: str,
        feature: str,
        *,
        now: Optional[float] = None,
    ) -> Optional[int]:
        """记一条付费预告发出事件。绝不抛（埋点失败不影响发送）。"""
        ck = str(contact_key or "").strip()
        if not ck:
            return None
        n = float(now if now is not None else time.time())
        try:
            with self._lock:
                c = self._conn.execute(
                    "INSERT INTO teaser_events (contact_key, scenario_id, feature, ts)"
                    " VALUES (?,?,?,?)",
                    (ck, str(scenario_id or ""), str(feature or ""), n),
                )
                self._conn.commit()
                return int(c.lastrowid) if c.lastrowid else None
        except Exception as e:  # noqa: BLE001
            logger.debug("record_teaser failed: %s", e)
            return None

    # ── 查询 ────────────────────────────────────────────────────────────
    def _events_since(self, since: float) -> List[Dict[str, Any]]:
        try:
            rows = self._conn.execute(
                "SELECT contact_key, scenario_id, feature, ts FROM teaser_events"
                " WHERE ts >= ? ORDER BY ts ASC",
                (float(since),),
            ).fetchall()
        except Exception:
            return []
        return [{"contact_key": str(r[0]), "scenario_id": str(r[1]),
                 "feature": str(r[2]), "ts": float(r[3])} for r in rows]

    def recent(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        lim = max(1, min(int(limit or 50), 500))
        try:
            rows = self._conn.execute(
                "SELECT id, contact_key, scenario_id, feature, ts FROM teaser_events"
                " ORDER BY ts DESC LIMIT ?",
                (lim,),
            ).fetchall()
        except Exception:
            return []
        cols = ["id", "contact_key", "scenario_id", "feature", "ts"]
        return [dict(zip(cols, r)) for r in rows]

    def count(self) -> int:
        try:
            r = self._conn.execute("SELECT COUNT(*) FROM teaser_events").fetchone()
            return int(r[0]) if r else 0
        except Exception:
            return 0

    def funnel_stats(
        self,
        *,
        paid_lookup: Optional[PaidLookup] = None,
        now: Optional[float] = None,
        window_days: float = 30.0,
        attribution_days: float = 14.0,
    ) -> Dict[str, Any]:
        """转化漏斗：近 ``window_days`` 内发出的预告 → 归因到 ``attribution_days`` 内的已付转化。

        归因（注入 ``paid_lookup`` 时）：某端用户**最早一条预告之后** attribution 窗口内有任意
        已付事件 → 记该用户「转化」；已付 item_id 命中其被预告的 feature → 另记「精确转化」（更强）。
        ``paid_lookup`` 缺省（变现未接）→ 转化恒 0，仅看「发了多少 / 触达多少人 / 按场景分布」。

        返回：``{window_days, attribution_days, teasers, contacts_teased, conversions,
        feature_conversions, conversion_rate, feature_conversion_rate, by_scenario:[...]}``。
        """
        n = float(now if now is not None else time.time())
        since = n - max(1.0, float(window_days)) * _DAY
        attr = max(0.0, float(attribution_days)) * _DAY
        rows = self._events_since(since)
        out: Dict[str, Any] = {
            "window_days": float(window_days),
            "attribution_days": float(attribution_days),
            "teasers": len(rows),
            "contacts_teased": 0,
            "conversions": 0,
            "feature_conversions": 0,
            "conversion_rate": 0.0,
            "feature_conversion_rate": 0.0,
            "by_scenario": [],
        }
        if not rows:
            return out

        # 按端用户聚合：最早预告 ts、被预告的 features、被预告的 scenarios 及各事件。
        by_contact: Dict[str, Dict[str, Any]] = {}
        by_scenario: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            ck = r["contact_key"]
            sid = r["scenario_id"]
            bc = by_contact.setdefault(
                ck, {"first_ts": r["ts"], "features": set(),
                     "events": []})
            bc["first_ts"] = min(bc["first_ts"], r["ts"])
            if r["feature"]:
                bc["features"].add(r["feature"])
            bc["events"].append(r)
            bs = by_scenario.setdefault(
                sid, {"teasers": 0, "contacts": set(), "conversions": set()})
            bs["teasers"] += 1
            bs["contacts"].add(ck)

        out["contacts_teased"] = len(by_contact)
        paid_map: Dict[str, List[Dict[str, Any]]] = {}
        if paid_lookup is not None:
            try:
                paid_map = paid_lookup(list(by_contact.keys())) or {}
            except Exception:
                logger.debug("paid_lookup failed", exc_info=True)
                paid_map = {}

        converted = set()
        feature_converted = set()
        for ck, bc in by_contact.items():
            first_ts = float(bc["first_ts"])
            hi = first_ts + attr
            for p in paid_map.get(ck) or []:
                try:
                    p_ts = float(p.get("ts") or 0)
                except (TypeError, ValueError):
                    continue
                if not (first_ts <= p_ts <= hi):
                    continue
                converted.add(ck)
                item = str(p.get("item_id") or "")
                if item and item in bc["features"]:
                    feature_converted.add(ck)
                # 场景归因：把转化记给该用户在此次付费之前被预告过的所有场景。
                for ev in bc["events"]:
                    if ev["ts"] <= p_ts:
                        by_scenario[ev["scenario_id"]]["conversions"].add(ck)

        out["conversions"] = len(converted)
        out["feature_conversions"] = len(feature_converted)
        ct = out["contacts_teased"]
        out["conversion_rate"] = round(len(converted) / ct, 4) if ct else 0.0
        out["feature_conversion_rate"] = (
            round(len(feature_converted) / ct, 4) if ct else 0.0)
        out["by_scenario"] = sorted(
            ({"scenario_id": sid, "teasers": v["teasers"],
              "contacts": len(v["contacts"]), "conversions": len(v["conversions"]),
              "conversion_rate": (round(len(v["conversions"]) / len(v["contacts"]), 4)
                                  if v["contacts"] else 0.0)}
             for sid, v in by_scenario.items()),
            key=lambda x: (-x["teasers"], x["scenario_id"]),
        )
        return out


_singleton: Optional["CompanionFunnelStore"] = None
_singleton_lock = threading.Lock()


def get_companion_funnel_store(db_path=None) -> "CompanionFunnelStore":
    """进程内单例。首次调用传入 db_path；之后返回同一实例。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = CompanionFunnelStore(db_path or ":memory:")
    return _singleton


def reset_companion_funnel_store() -> None:
    """测试辅助：清空单例。"""
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            _singleton.close()
        _singleton = None


__all__ = [
    "CompanionFunnelStore",
    "get_companion_funnel_store",
    "reset_companion_funnel_store",
]
