"""桌面壳注入健康信标（D1b）——汇聚各内嵌账号的「逐选择器命中」状态供运营看板。

注入脚本（``desktop/inject/tg-inject.js``）在状态变化或每 30s 心跳时上报一条健康记录；
本模块按 ``(platform, account_id)`` 保留**最新一条**，并把它分类成可读状态：

- ``ok``：注入正常（输入框/消息气泡都抓到）
- ``mismatch_composer`` / ``mismatch_bubble``：会话已开但抓不到输入框/气泡 → 官方改版选择器失配，
  运营可据此走 D1 覆写层热修（改 ``config/desktop_selector_profiles.json``）
- ``no_chat``：未登录或未进入会话（非故障）
- ``unsupported``：该平台无选择器档案

分类语义与渲染层 ``desktop/renderer/inject-status.js::deriveInjectState`` 对齐，使壳层状态条与
后端看板口径一致。``classify_inject_health`` 为纯函数，便于单测。
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Any, Deque, Dict, List, Optional


def classify_inject_health(rec: Dict[str, Any]) -> str:
    """把一条上报记录分类为状态串（与渲染层 deriveInjectState 同口径）。"""
    if rec is None or not rec.get("supported", True):
        return "unsupported"
    composer = bool(rec.get("composer"))
    bubbles = int(rec.get("bubbles") or 0)
    chat_open = bool(rec.get("chatOpen"))
    if not chat_open and not composer:
        return "no_chat"
    if not composer:
        return "mismatch_composer"
    if chat_open and bubbles <= 0:
        return "mismatch_bubble"
    return "ok"


# 失配类状态（运营需关注：很可能官方改版导致选择器失效）
MISMATCH_STATUSES = ("mismatch_composer", "mismatch_bubble")

# 逐选择器键的诊断顺序（输入框/发送按钮最关键 → 影响出站；置前便于运营优先校准）
SELECTOR_KEYS = ("composer", "sendBtn", "bubble", "peerTitle")


def selector_failure_breakdown(alerts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """聚合持续失配账号的「逐选择器缺失」计数（纯函数，便于单测）。

    输入 ``persistent_mismatches()`` 返回的告警列表（每条含 ``selectors{key:bool}``）。
    输出 ``[{key, missing}]``，仅含 ``missing>0`` 的键，按缺失账号数降序（同数按 SELECTOR_KEYS
    固定序稳定）。让运营从「N 个账号失配」精准下钻到「**哪个 selector key 在最多账号上抓空**」，
    优先热修该键，而非盲改整份覆写。

    仅统计**明确为 False**（抓空）的键；缺字段/非 dict 一律跳过，避免误报。
    """
    counts = {k: 0 for k in SELECTOR_KEYS}
    for a in (alerts or []):
        sel = (a or {}).get("selectors") if isinstance(a, dict) else None
        if not isinstance(sel, dict):
            continue
        for k in SELECTOR_KEYS:
            if sel.get(k) is False:
                counts[k] += 1
    out = [{"key": k, "missing": counts[k]} for k in SELECTOR_KEYS if counts[k] > 0]
    out.sort(key=lambda d: d["missing"], reverse=True)
    return out

# 「持续失配」默认阈值（秒）——失配连续超过此时长升级为持续告警（区别于一闪而过的抖动）
DEFAULT_PERSIST_SEC = 300.0


def _norm_selectors(raw: Any) -> Dict[str, bool]:
    out = {"bubble": False, "composer": False, "sendBtn": False, "peerTitle": False}
    if isinstance(raw, dict):
        for k in out:
            out[k] = bool(raw.get(k))
    return out


class InjectHealthStore:
    """线程安全的「每账号最新健康」内存存储（实时态，无需持久化）。"""

    def __init__(self, cap: int = 1000, event_cap: int = 200) -> None:
        self._cap = max(1, int(cap))
        self._lock = threading.Lock()
        self._latest: Dict[str, Dict[str, Any]] = {}
        # 状态跃迁历史环（趋势/告警流用）：status 变化时各记一条
        self._events: Deque[Dict[str, Any]] = collections.deque(
            maxlen=max(1, int(event_cap)))

    @staticmethod
    def _key(platform: str, account_id: str) -> str:
        return f"{platform}\t{account_id}"

    def record(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """归一并落最新一条；返回带 status/ts 的规范记录。

        附带**状态跃迁追踪**：维护 `mismatch_since`（进入失配的起始 ts，跨 composer↔bubble
        子状态连续保留；恢复非失配时清空），用于「失配持续 N 分钟」判定；状态变化时写跃迁历史环。
        """
        platform = str((rec or {}).get("platform") or "").lower()
        account_id = str((rec or {}).get("account_id") or "")
        norm = {
            "platform": platform,
            "account_id": account_id,
            "supported": bool((rec or {}).get("supported", True)),
            "generic": bool((rec or {}).get("generic")),
            "can_ingest": bool((rec or {}).get("can_ingest")),
            "composer": bool((rec or {}).get("composer")),
            "bubbles": int((rec or {}).get("bubbles") or 0),
            "chatOpen": bool((rec or {}).get("chatOpen")),
            "selectors": _norm_selectors((rec or {}).get("selectors")),
            "ts": float((rec or {}).get("ts") or time.time()),
        }
        norm["status"] = classify_inject_health(norm)
        if not platform and not account_id:
            return norm  # 无主键不入库（仍返回分类，便于探针自测）
        is_mismatch = norm["status"] in MISMATCH_STATUSES
        with self._lock:
            prev = self._latest.get(self._key(platform, account_id))
            prev_status = prev.get("status") if prev else None
            prev_mismatch_since = (prev or {}).get("mismatch_since")
            # mismatch_since：持续在失配态则沿用起点；刚进入失配记当前 ts；非失配清空
            if is_mismatch:
                norm["mismatch_since"] = (
                    prev_mismatch_since
                    if (prev_status in MISMATCH_STATUSES and prev_mismatch_since)
                    else norm["ts"]
                )
            else:
                norm["mismatch_since"] = None
            # 状态跃迁 → 记历史环（含首次出现）
            if prev_status != norm["status"]:
                self._events.append({
                    "platform": platform, "account_id": account_id,
                    "status": norm["status"], "from": prev_status or "",
                    "ts": norm["ts"],
                })
            self._latest[self._key(platform, account_id)] = norm
            # 软上限：超量则丢弃最旧（按 ts）
            if len(self._latest) > self._cap:
                oldest = min(self._latest, key=lambda k: self._latest[k]["ts"])
                self._latest.pop(oldest, None)
        return norm

    def latest(self, stale_after: Optional[float] = None,
               now: Optional[float] = None) -> List[Dict[str, Any]]:
        """返回所有账号最新记录（按 ts 倒序）。

        stale_after 给定时为每条标注 stale；并为失配账号附 `mismatch_secs`（已持续秒数）。
        """
        _now = float(now if now is not None else time.time())
        with self._lock:
            # 返回浅拷贝：避免调用方（或标注）污染存储中的原记录
            rows = [dict(r) for r in self._latest.values()]
        rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
        for r in rows:
            if stale_after:
                r["stale"] = (_now - r.get("ts", 0)) > stale_after
            ms = r.get("mismatch_since")
            r["mismatch_secs"] = (max(0.0, _now - ms) if ms else 0.0)
        return rows

    def persistent_mismatches(
        self, threshold_sec: float = DEFAULT_PERSIST_SEC,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """返回失配已**持续 ≥ threshold_sec** 的账号（告警流用，按持续时长倒序）。"""
        _now = float(now if now is not None else time.time())
        thr = max(0.0, float(threshold_sec))
        out: List[Dict[str, Any]] = []
        with self._lock:
            for r in self._latest.values():
                ms = r.get("mismatch_since")
                if r.get("status") in MISMATCH_STATUSES and ms:
                    dur = _now - ms
                    if dur >= thr:
                        d = dict(r)
                        d["mismatch_secs"] = max(0.0, dur)
                        out.append(d)
        out.sort(key=lambda r: r.get("mismatch_secs", 0), reverse=True)
        return out

    def recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        """状态跃迁历史（最新在前）——供趋势/告警流展示。"""
        with self._lock:
            evs = list(self._events)
        evs.reverse()
        return evs[:max(1, min(int(limit or 50), 500))]

    def summary(self, persist_sec: Optional[float] = None,
                now: Optional[float] = None) -> Dict[str, int]:
        """状态计数概览（看板顶部徽标用）。

        persist_sec 给定时附 `persistent_mismatch`（失配持续超阈值的账号数）。
        """
        counts: Dict[str, int] = {}
        with self._lock:
            for r in self._latest.values():
                s = r.get("status", "unknown")
                counts[s] = counts.get(s, 0) + 1
        counts["total"] = sum(v for k, v in counts.items() if k != "total")
        counts["mismatch"] = sum(counts.get(s, 0) for s in MISMATCH_STATUSES)
        if persist_sec is not None:
            counts["persistent_mismatch"] = len(
                self.persistent_mismatches(persist_sec, now=now))
        return counts

    def clear(self) -> None:
        with self._lock:
            self._latest.clear()
            self._events.clear()


_STORE: Optional[InjectHealthStore] = None


def get_inject_health_store() -> InjectHealthStore:
    """进程级单例（与 account_registry 等同模式）。"""
    global _STORE
    if _STORE is None:
        _STORE = InjectHealthStore()
    return _STORE
