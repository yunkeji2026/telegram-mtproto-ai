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

import threading
import time
from typing import Any, Dict, List, Optional


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


def _norm_selectors(raw: Any) -> Dict[str, bool]:
    out = {"bubble": False, "composer": False, "sendBtn": False, "peerTitle": False}
    if isinstance(raw, dict):
        for k in out:
            out[k] = bool(raw.get(k))
    return out


class InjectHealthStore:
    """线程安全的「每账号最新健康」内存存储（实时态，无需持久化）。"""

    def __init__(self, cap: int = 1000) -> None:
        self._cap = max(1, int(cap))
        self._lock = threading.Lock()
        self._latest: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _key(platform: str, account_id: str) -> str:
        return f"{platform}\t{account_id}"

    def record(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """归一并落最新一条；返回带 status/ts 的规范记录。"""
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
        with self._lock:
            self._latest[self._key(platform, account_id)] = norm
            # 软上限：超量则丢弃最旧（按 ts）
            if len(self._latest) > self._cap:
                oldest = min(self._latest, key=lambda k: self._latest[k]["ts"])
                self._latest.pop(oldest, None)
        return norm

    def latest(self, stale_after: Optional[float] = None) -> List[Dict[str, Any]]:
        """返回所有账号最新记录（按 ts 倒序）。stale_after 给定时为每条标注 stale。"""
        now = time.time()
        with self._lock:
            # 返回浅拷贝：避免调用方（或 stale 标注）污染存储中的原记录
            rows = [dict(r) for r in self._latest.values()]
        rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
        if stale_after:
            for r in rows:
                r["stale"] = (now - r.get("ts", 0)) > stale_after
        return rows

    def summary(self) -> Dict[str, int]:
        """状态计数概览（看板顶部徽标用）。"""
        counts: Dict[str, int] = {}
        with self._lock:
            for r in self._latest.values():
                s = r.get("status", "unknown")
                counts[s] = counts.get(s, 0) + 1
        counts["total"] = sum(v for k, v in counts.items() if k != "total")
        counts["mismatch"] = sum(counts.get(s, 0) for s in MISMATCH_STATUSES)
        return counts

    def clear(self) -> None:
        with self._lock:
            self._latest.clear()


_STORE: Optional[InjectHealthStore] = None


def get_inject_health_store() -> InjectHealthStore:
    """进程级单例（与 account_registry 等同模式）。"""
    global _STORE
    if _STORE is None:
        _STORE = InjectHealthStore()
    return _STORE
