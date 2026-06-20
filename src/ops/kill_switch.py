"""G1 全局 Kill-Switch（反封号护栏三件套之一）。

一键（毫秒级）冻结自动发送，三级作用域、重启不丢、可选 TTL 自动恢复。

设计要点
--------
- **作用域链**：``global`` → ``platform:<p>`` → ``account:<p>:<id>``，任一命中即停。
- **热路径只读内存**：``is_blocked`` 只读进程内 ``_state`` 字典（加锁），不碰 DB；
  DB 仅用于「置位时写穿 + 启动时回填」，避免每条发送都打库（与 send 热路径解耦）。
- **重启不丢**：状态落 SQLite（默认 ``config/runtime_flags.db``），与
  ``account_registry`` 同款线程安全封装。
- **TTL 自动恢复**：可选 ``expires_at``（0=永久）；``is_blocked`` 惰性过期清理，
  避免「停了忘了开」长期误伤（对 doc 方案的增强）。
- **跨线程**：Web API 线程置位 / asyncio 主循环读取，同进程共享单例 + 锁即可。

为什么不放进 ``companion_send_gate.evaluate()``：两条发送路径只在
``gate_enabled(cfg)=True`` 时才调用 ``evaluate``，若 kill-switch 藏在里面，预热闸门关闭
时它就永不生效——违背「独立急刹」的本意。故 kill-switch 由发送路径**直接**调用本模块，
与预热闸门正交，且保持 ``evaluate`` 为纯函数（零 IO，易测）。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

GLOBAL_SCOPE = "global"

_DDL = """
CREATE TABLE IF NOT EXISTS kill_switch (
    scope       TEXT PRIMARY KEY,
    on_flag     INTEGER NOT NULL DEFAULT 0,
    reason      TEXT NOT NULL DEFAULT '',
    actor       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL DEFAULT 0,
    expires_at  REAL NOT NULL DEFAULT 0
);
"""


def normalize_scope(scope: str) -> str:
    """规整并校验作用域字符串。非法 → 抛 ValueError。

    合法形态：``global`` / ``platform:<p>`` / ``account:<p>:<id>``。
    """
    s = str(scope or "").strip()
    if s == GLOBAL_SCOPE:
        return GLOBAL_SCOPE
    if s.startswith("platform:"):
        p = s[len("platform:"):].strip().lower()
        if p:
            return f"platform:{p}"
    if s.startswith("account:"):
        rest = s[len("account:"):]
        parts = rest.split(":", 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return f"account:{parts[0].strip().lower()}:{parts[1].strip()}"
    raise ValueError(
        f"非法 kill-switch scope: {scope!r}（需 global / platform:<p> / account:<p>:<id>）"
    )


def scope_chain(platform: str, account_id: str) -> List[str]:
    """从 (platform, account_id) 推出由粗到细的作用域链（任一命中即停）。"""
    chain = [GLOBAL_SCOPE]
    p = str(platform or "").strip().lower()
    a = str(account_id or "").strip()
    if p:
        chain.append(f"platform:{p}")
        if a:
            chain.append(f"account:{p}:{a}")
    return chain


class KillSwitch:
    """三级紧急停发开关（线程安全 SQLite + 内存缓存）。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False, timeout=10
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        # 内存镜像：scope -> {reason, actor, created_at, expires_at}
        self._state: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            self._conn.commit()
            self._reload_locked()

    def _reload_locked(self) -> None:
        rows = self._conn.execute(
            "SELECT scope, reason, actor, created_at, expires_at "
            "FROM kill_switch WHERE on_flag=1"
        ).fetchall()
        self._state = {
            r["scope"]: {
                "reason": r["reason"], "actor": r["actor"],
                "created_at": r["created_at"], "expires_at": r["expires_at"],
            }
            for r in rows
        }

    def _delete_locked(self, scope: str) -> None:
        self._state.pop(scope, None)
        self._conn.execute("DELETE FROM kill_switch WHERE scope=?", (scope,))
        self._conn.commit()

    def set(
        self,
        scope: str,
        *,
        reason: str = "",
        actor: str = "",
        ttl_sec: float = 0,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """置位某作用域（写穿 DB + 内存）。ttl_sec>0 时到点自动恢复。"""
        scope = normalize_scope(scope)
        now = float(now if now is not None else time.time())
        expires_at = now + float(ttl_sec) if ttl_sec and ttl_sec > 0 else 0.0
        rec = {
            "reason": str(reason or "")[:500], "actor": str(actor or "")[:120],
            "created_at": now, "expires_at": expires_at,
        }
        with self._lock:
            self._conn.execute(
                "INSERT INTO kill_switch (scope, on_flag, reason, actor, created_at, expires_at) "
                "VALUES (?,1,?,?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET on_flag=1, reason=excluded.reason, "
                "actor=excluded.actor, created_at=excluded.created_at, "
                "expires_at=excluded.expires_at",
                (scope, rec["reason"], rec["actor"], rec["created_at"], rec["expires_at"]),
            )
            self._conn.commit()
            self._state[scope] = rec
        return {"scope": scope, **rec}

    def clear(self, scope: str) -> bool:
        """解除某作用域。返回是否原本生效。"""
        scope = normalize_scope(scope)
        with self._lock:
            existed = scope in self._state
            self._delete_locked(scope)
        return existed

    def is_blocked(
        self, platform: str, account_id: str, *, now: Optional[float] = None
    ) -> Tuple[bool, str, str]:
        """热路径只读：返回 (blocked, 命中 scope, reason)。惰性过期清理。"""
        now = float(now if now is not None else time.time())
        with self._lock:
            for scope in scope_chain(platform, account_id):
                rec = self._state.get(scope)
                if not rec:
                    continue
                exp = float(rec.get("expires_at") or 0)
                if exp and exp <= now:
                    self._delete_locked(scope)  # TTL 到点，惰性恢复
                    continue
                return True, scope, str(rec.get("reason") or "")
        return False, "", ""

    def status(self, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """当前生效的作用域列表（已过期的惰性剔除）。"""
        now = float(now if now is not None else time.time())
        out: List[Dict[str, Any]] = []
        with self._lock:
            for scope, rec in list(self._state.items()):
                exp = float(rec.get("expires_at") or 0)
                if exp and exp <= now:
                    self._delete_locked(scope)
                    continue
                out.append({"scope": scope, **rec})
        out.sort(key=lambda d: (d["scope"] != GLOBAL_SCOPE, d["scope"]))
        return out


_singleton: Optional[KillSwitch] = None
_singleton_lock = threading.Lock()


def get_kill_switch(db_path: Optional[Path] = None) -> KillSwitch:
    """进程内单例。首次调用可指定路径，默认 ``config/runtime_flags.db``。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                path = Path(db_path) if db_path else Path("config/runtime_flags.db")
                _singleton = KillSwitch(path)
    return _singleton


def is_blocked(
    platform: str, account_id: str, *, now: Optional[float] = None
) -> Tuple[bool, str, str]:
    """模块级便捷入口（热路径）：单例未初始化 → 永不拦截（零破坏）。

    绝不抛异常：单例缺失或任何意外都返回「不拦截」，让发送路径失败开放
    （broken kill-switch 不应反过来把全部发送卡死）。
    """
    ks = _singleton
    if ks is None:
        return False, "", ""
    try:
        return ks.is_blocked(platform, account_id, now=now)
    except Exception:
        return False, "", ""


__all__ = [
    "KillSwitch", "get_kill_switch", "is_blocked",
    "normalize_scope", "scope_chain", "GLOBAL_SCOPE",
]
