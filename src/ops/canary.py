"""G3 金丝雀放量（反封号护栏三件套之三）。

激进上量时**先只让一小批 cohort 真发**，绿灯稳定才逐步扩面，把「新策略/新号上线」
的爆炸半径限制在少数账号。

语义与 G1/G2 的关系
--------------------
- Kill-Switch（G1）= **默认放行 + 黑名单**（命中即停）。
- Canary（G3）   = **默认拦截 + 白名单**（不在 cohort 即 hold）。两者正交、可叠加。
- 同样**独立于** ``companion_send_gate.enabled``：金丝雀是放量闸，不依赖预热闸开关。

模式
----
- ``manual``（默认，首版推荐）：cohort = ``ops.canary.pinned_accounts``（运营手点扩面）。
- ``auto_health``：pinned ∪ 自动扩面集（``plan_expansion`` 纯函数按机群绿灯推进，
  由 watchdog 周期调用；扩面集持久化在 runtime_flags.db 的 ``canary_cohort`` 表）。

成员标识：``"<platform>:<account_id>"``（小写平台）。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

_DDL = """
CREATE TABLE IF NOT EXISTS canary_cohort (
    member    TEXT PRIMARY KEY,
    added_at  REAL NOT NULL DEFAULT 0
);
"""


def member_key(platform: str, account_id: str) -> str:
    return f"{str(platform or '').strip().lower()}:{str(account_id or '').strip()}"


def _cfg(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        return dict(((cfg or {}).get("ops") or {}).get("canary") or {})
    except Exception:
        return {}


def canary_enabled(cfg: Optional[Dict[str, Any]]) -> bool:
    return bool(_cfg(cfg).get("enabled", False))


def _pinned(cfg: Optional[Dict[str, Any]]) -> Set[str]:
    out: Set[str] = set()
    for m in _cfg(cfg).get("pinned_accounts") or []:
        s = str(m or "").strip()
        if not s:
            continue
        # 允许 "platform:id" 或裸 id（裸 id 视为 telegram）
        out.add(s.lower() if ":" in s else f"telegram:{s}")
    return out


class CanaryStore:
    """auto_health 模式下的自动扩面集（线程安全 SQLite，复用 runtime_flags.db）。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=10)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_DDL)
            self._conn.commit()

    def add(self, members: Set[str], *, now: Optional[float] = None) -> None:
        now = float(now if now is not None else time.time())
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO canary_cohort (member, added_at) VALUES (?,?)",
                [(m, now) for m in members],
            )
            self._conn.commit()

    def remove(self, member: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM canary_cohort WHERE member=?", (member,))
            self._conn.commit()

    def members(self) -> Set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT member FROM canary_cohort").fetchall()
        return {r[0] for r in rows}

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM canary_cohort")
            self._conn.commit()


_store: Optional[CanaryStore] = None
_store_lock = threading.Lock()


def get_canary_store(db_path: Optional[Path] = None) -> CanaryStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                path = Path(db_path) if db_path else Path("config/runtime_flags.db")
                _store = CanaryStore(path)
    return _store


def active_cohort(cfg: Optional[Dict[str, Any]], store: Optional[CanaryStore] = None) -> Set[str]:
    """当前可放行成员集 = pinned ∪（auto_health 模式下的持久扩面集）。"""
    cohort = _pinned(cfg)
    if str(_cfg(cfg).get("mode") or "manual") == "auto_health":
        st = store
        if st is None:
            try:
                st = get_canary_store()
            except Exception:
                st = None
        if st is not None:
            cohort |= st.members()
    return cohort


def is_held(
    platform: str, account_id: str, cfg: Optional[Dict[str, Any]],
    store: Optional[CanaryStore] = None,
) -> Tuple[bool, str]:
    """金丝雀放量判定：启用且账号不在 cohort → (True, 'canary_hold')。

    未启用 → 永不 hold（零破坏）。启用时 cohort 为空 → 全 hold（最保守，符合「先不放」）。
    """
    if not canary_enabled(cfg):
        return False, ""
    if member_key(platform, account_id) in active_cohort(cfg, store):
        return False, ""
    return True, "canary_hold"


def plan_expansion(
    current: Set[str], candidates: List[str], *, fleet_ok: bool, step: int = 5,
) -> Set[str]:
    """纯函数：绿灯稳定（fleet_ok）时，把 candidates 里尚未纳入的成员扩入，最多 step 个。

    fleet_ok=False（出现 paused/banned 或红率超标）→ 不推进，原样返回。
    """
    if not fleet_ok or step <= 0:
        return set(current)
    out = set(current)
    added = 0
    for m in candidates:
        mk = str(m or "").strip().lower()
        if not mk or mk in out:
            continue
        out.add(mk)
        added += 1
        if added >= step:
            break
    return out


__all__ = [
    "member_key", "canary_enabled", "active_cohort", "is_held",
    "plan_expansion", "CanaryStore", "get_canary_store",
]
