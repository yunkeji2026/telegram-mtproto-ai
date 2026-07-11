"""平台会话健康登记表（进程级单例）——外部 worker 微服务的会话状态闭环（P0-2）。

背景：Messenger 网页会话（``services/messenger-web`` Node 微服务）掉线/cookie 失效/
崩溃循环放弃自愈时，此前只写 Node 自己的日志，Python 侧毫不知情——编排器要等健康
轮询才后知后觉，运营则完全看不见（「会话死了还在装在线」）。

本模块是接收 Node 主动 push 的 ``/api/internal/protocol/session-status`` 事件的落点：
- 记住每个 ``platform:account`` 的最新会话状态（authorized / needs_login / expired /
  logged_out / failed），供发送前快速判活与 ops 看板展示；
- ``record()`` 返回状态迁移语义（进入不健康 / 恢复），路由层据此发 EventBus 告警
  （``platform_session_alert``，订阅别名 ``platform_session``）；
- ``dump()`` → ``/api/workspace/metrics.platform_sessions``、``dump_prom()`` → Prometheus。

风格对齐 ``src/inbox/send_route_stats.py``：零依赖、线程安全、进程级单例、
distinct key 有上限（防脏数据撑爆内存）。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, Optional

# 视为「不健康」的会话状态（会话不可用于收发）
UNHEALTHY_STATUSES = frozenset({"expired", "needs_login", "logged_out", "failed"})
# 视为「健康」的状态
HEALTHY_STATUSES = frozenset({"authorized"})

_MAX_KEYS = 64
_SAN_RE = re.compile(r"[^a-zA-Z0-9_\-\.:@]")


def _san(value: str, limit: int = 48) -> str:
    v = _SAN_RE.sub("", str(value or "").strip())
    return v[:limit]


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class PlatformSessionHealth:
    """外部 worker 会话状态登记（线程安全，进程级）。"""

    __slots__ = ("_lock", "_started_at", "_sessions", "total_events", "_by_status")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        # key = "platform:account_id" → {status, detail, login_id, ts, changes}
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self.total_events = 0
        self._by_status: Dict[str, int] = {}

    @staticmethod
    def _key(platform: str, account_id: str) -> str:
        return f"{_san(platform, 24).lower()}:{_san(account_id)}"

    def record(self, platform: str, account_id: str, status: str,
               *, detail: str = "", login_id: str = "") -> Dict[str, Any]:
        """登记一次会话状态事件。

        返回迁移语义：``{"changed", "went_unhealthy", "recovered", "prev", "status"}``
        —— 路由层据此决定是否发告警（进入不健康）/恢复通知（回到健康），
        重复同态事件不重复告警。
        """
        st = _san(status, 24).lower() or "unknown"
        key = self._key(platform, account_id)
        with self._lock:
            self.total_events += 1
            self._by_status[st] = self._by_status.get(st, 0) + 1
            sess = self._sessions.get(key)
            if sess is None:
                if len(self._sessions) >= _MAX_KEYS:
                    # 超限：不再收新 key（防刷量），但事件计数仍累计
                    return {"changed": False, "went_unhealthy": False,
                            "recovered": False, "prev": "", "status": st}
                sess = {"status": "", "detail": "", "login_id": "", "ts": 0.0,
                        "changes": 0, "unhealthy_since": 0.0,
                        "last_remind_ts": 0.0}
                self._sessions[key] = sess
            prev = str(sess.get("status") or "")
            changed = prev != st
            if changed:
                sess["changes"] = int(sess.get("changes") or 0) + 1
            now = time.time()
            sess["status"] = st
            sess["detail"] = str(detail or "")[:300]
            sess["login_id"] = _san(login_id)
            sess["ts"] = now
            # 持续不健康起点：进入不健康时打点，期间同态重推（如放弃自愈的周期重报）
            # 不刷新 → 「已掉线多久」可信；恢复即清零（连带提醒节流点）。
            if st in UNHEALTHY_STATUSES:
                if not float(sess.get("unhealthy_since") or 0.0):
                    sess["unhealthy_since"] = now
            else:
                sess["unhealthy_since"] = 0.0
                sess["last_remind_ts"] = 0.0
            went_unhealthy = (st in UNHEALTHY_STATUSES
                              and prev not in UNHEALTHY_STATUSES)
            recovered = (st in HEALTHY_STATUSES
                         and prev in UNHEALTHY_STATUSES)
            return {"changed": changed, "went_unhealthy": went_unhealthy,
                    "recovered": recovered, "prev": prev, "status": st}

    def is_unhealthy(self, platform: str, account_id: str) -> bool:
        """该会话最近一次上报是否处于不健康态（未上报过 → False，不拦）。"""
        with self._lock:
            sess = self._sessions.get(self._key(platform, account_id))
            return bool(sess and str(sess.get("status")) in UNHEALTHY_STATUSES)

    def unhealthy_sessions(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._sessions.items()
                    if str(v.get("status")) in UNHEALTHY_STATUSES}

    def due_reminders(self, *, min_age_sec: float, interval_sec: float,
                      now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
        """待提醒的「持续不健康」会话（供 HealthWatchdog 周期复查）。

        语义（升级式）：掉线后 ``min_age_sec`` 仍未恢复 → 第一条提醒（「还没人修」），
        之后每 ``interval_sec`` 一条（防唠叨）。返回即视为「本轮要提醒」——原子标记
        ``last_remind_ts``，并发/下轮不重复。恢复时 ``record()`` 会清零两个时间戳。
        """
        ts = time.time() if now is None else float(now)
        due: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            for key, sess in self._sessions.items():
                if str(sess.get("status")) not in UNHEALTHY_STATUSES:
                    continue
                since = float(sess.get("unhealthy_since") or 0.0)
                if not since or (ts - since) < float(min_age_sec):
                    continue
                last = float(sess.get("last_remind_ts") or 0.0)
                if last and (ts - last) < float(interval_sec):
                    continue
                sess["last_remind_ts"] = ts
                out = dict(sess)
                out["down_sec"] = ts - since
                due[key] = out
        return due

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            sessions = {k: dict(v) for k, v in sorted(self._sessions.items())}
            unhealthy = [k for k, v in sessions.items()
                         if str(v.get("status")) in UNHEALTHY_STATUSES]
            return {
                "started_at": self._started_at,
                "total_events": self.total_events,
                "by_status": dict(sorted(self._by_status.items())),
                "sessions": sessions,
                "unhealthy": unhealthy,
                "unhealthy_count": len(unhealthy),
            }

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP platform_session_events_total Session status events "
                "pushed by external workers, by status",
                "# TYPE platform_session_events_total counter",
            ]
            for st, n in sorted(self._by_status.items()):
                lines.append(
                    f'platform_session_events_total{{status="{_esc(st)}"}} {int(n)}')
            lines += [
                "# HELP platform_session_unhealthy Whether the session's last "
                "reported status is unhealthy (1) or healthy (0)",
                "# TYPE platform_session_unhealthy gauge",
            ]
            for key, sess in sorted(self._sessions.items()):
                val = 1 if str(sess.get("status")) in UNHEALTHY_STATUSES else 0
                lines.append(
                    f'platform_session_unhealthy{{session="{_esc(key)}"}} {val}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self.total_events = 0
            self._by_status.clear()
            self._sessions.clear()


_SINGLETON: Optional[PlatformSessionHealth] = None
_LOCK = threading.Lock()


def get_platform_session_health() -> PlatformSessionHealth:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = PlatformSessionHealth()
    return _SINGLETON


__all__ = [
    "PlatformSessionHealth", "get_platform_session_health",
    "UNHEALTHY_STATUSES", "HEALTHY_STATUSES",
]
