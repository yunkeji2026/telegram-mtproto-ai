"""跨 send 的 ``verify_thread_title`` 结果缓存。

P0+P1 实现了 **同次 send 内** verify 去重（``just_verified_ts`` 局部变量）。
本模块把它扩展到 **跨 send**：burst / reactivation 重发同一人时，30-60s 内
不再重复 vision verify，省 5-10s/send。

核心机制：
  - **TTL 缓存**：``mark_verified`` 写入 ``(serial, peer_normalized) → ts``
  - **心跳刷新**：``send_succeeded`` 把 cache TS 推到现在——发送成功证明
    "我们仍在该 chat 内"，TTL 顺势续期
  - **best-effort invalidate**：``invalidate`` 在 BACK/exit_thread/inbox
    tap 等"我们主动离开 chat 或换会话"的地方调用

为什么 60s 而非更长：
  即使有心跳，也不能 100% 排除用户手动操作（实机 RPA 跑用户机器并不
  独占）。短 TTL 限制错误窗口，心跳让正常 burst 仍能复用。

为什么 module-level 而非实例属性：
  thread_actions 不知道 runner——它需要个独立位置存。runner 持有该模块
  的引用即可。tests 用 ``_reset()`` 清。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# (serial, peer_normalized) -> verified_ok_ts
_cache: Dict[Tuple[str, str], float] = {}
# 写一把粗粒度锁。verify_thread_title 是异步上下文中的同步调用，多 worker
# 时可能并发写——dict 操作单步原子但 read-then-write 不是。
_lock = threading.Lock()

# 默认 TTL；调用方可覆盖
# 60s → 300s（2026-05-04）：实测真实 cycle 间距 3–5 min（120s post-send
# cooldown + peer 思考），60s TTL 命中率 0%。300s 覆盖典型间距，且 cache
# 在 _exit_thread/BACK/inbox-tap 都会主动失效，所以延长不增加错聊风险。
_DEFAULT_TTL_SEC = 300.0


def _normalize_peer(peer: str) -> str:
    """与 thread_actions._normalize_peer_name 对齐——casefold + strip。

    复用而不 import 是为了避开 thread_actions ↔ recent_verify_cache 互引。
    """
    if not peer:
        return ""
    out = []
    for ch in peer:
        if 0x200B <= ord(ch) <= 0x200F or 0x202A <= ord(ch) <= 0x202E:
            continue
        out.append(ch)
    return "".join(out).strip().casefold()


def _key(serial: str, peer: str) -> Optional[Tuple[str, str]]:
    s = (serial or "").strip()
    p = _normalize_peer(peer)
    if not s or not p:
        return None
    return (s, p)


def is_recently_verified(
    serial: str, peer: str, *, ttl_sec: float = _DEFAULT_TTL_SEC,
) -> bool:
    """该 (serial, peer) 在 TTL 内是否被 verify ok 过。"""
    k = _key(serial, peer)
    if k is None:
        return False
    with _lock:
        ts = _cache.get(k, 0.0)
    return ts > 0 and (time.time() - ts) < ttl_sec


def mark_verified(serial: str, peer: str) -> None:
    """verify_thread_title 返 ok=True 时调。"""
    k = _key(serial, peer)
    if k is None:
        return
    with _lock:
        _cache[k] = time.time()


def send_succeeded(serial: str, peer: str) -> None:
    """送达成功——证明仍在 chat 内，刷新 cache TS（心跳）。

    与 ``mark_verified`` 等价；语义上分开是为了让上游 grep 时一眼看出
    意图（"哦这是心跳点，不是 verify 点"）。
    """
    mark_verified(serial, peer)


def invalidate(serial: str, peer: Optional[str] = None) -> None:
    """显式失效。``peer=None`` 失效该 serial 全部条目。

    调用点：``_exit_thread`` / KEYCODE_BACK 之后 / inbox row tap 之前
    （tap 后 chat 一定切换，旧 cache 必脏）。
    """
    if not serial:
        return
    s = serial.strip()
    if not s:
        return
    with _lock:
        if peer is None:
            keys_to_drop = [k for k in _cache if k[0] == s]
            for k in keys_to_drop:
                _cache.pop(k, None)
        else:
            p = _normalize_peer(peer)
            _cache.pop((s, p), None)


def invalidate_all() -> None:
    """全清——通常用在测试或全局重启。"""
    with _lock:
        _cache.clear()


# ── 测试 hook ──────────────────────────────────────────────
def _reset() -> None:
    invalidate_all()


def _peek_cache() -> Dict[Tuple[str, str], float]:
    """diagnostic 用——返回当前 cache 副本。"""
    with _lock:
        return dict(_cache)


__all__ = [
    "is_recently_verified",
    "mark_verified",
    "send_succeeded",
    "invalidate",
    "invalidate_all",
]
