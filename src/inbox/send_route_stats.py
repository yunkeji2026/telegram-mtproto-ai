"""收件箱出站「路由去向」观测（进程级单例）。

背景：``send_via_adapters`` 发送时有两条路——编排器（``AccountOrchestrator``）拥有该
``(platform, account_id)`` 的运行中 worker → 走 ``orch.send``（happy path，自带出站回写）；
否则**回落**到平台适配器（RPA / 单连接 / 网页微服务）。回落本是兜底，但此前**完全静默**：
无从得知「编排器多常没接管、哪个平台在长期走回落」。上一轮 Messenger 出站崩溃正是因为这条
回落路径无人看守，才拖到线上才被发现。

本模块把每次发送的路由去向变成可观测计数：按 ``(platform, route)`` 累计，``route`` ∈
``{orchestrator, adapter}``。经 ``dump()`` → ``/api/workspace/metrics.send_routes``、
``dump_prom()`` → Prometheus 暴露**回落率**，让「编排器漏接」在看板可见，而非只在崩了才知道。

风格对齐 ``src/web/frontend_error_stats.py``：无新增依赖，线程安全，进程级单例。
distinct platform 有上限（防脏数据把内存撑爆），超限归入 ``__other__``。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, Optional

_ROUTES = ("orchestrator", "adapter")
_MAX_PLATFORMS = 32
_PLAT_RE = re.compile(r"[^a-z0-9_\-]")


def _san_platform(platform: str) -> str:
    p = _PLAT_RE.sub("", str(platform or "").strip().lower())
    return p[:24] if p else "unknown"


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class SendRouteStats:
    """出站路由去向计数（线程安全，进程级）。"""

    __slots__ = ("_lock", "_started_at", "_last_ts", "total", "_by")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._last_ts = 0.0
        self.total = 0
        self._by: Dict[str, Dict[str, int]] = {}  # platform -> {route: count}

    def record(self, platform: str, route: str) -> None:
        """记一次发送的路由去向。``route`` 非法值一律归为 ``adapter``（保守：回落才是风险面）。"""
        p = _san_platform(platform)
        r = route if route in _ROUTES else "adapter"
        with self._lock:
            if p not in self._by and len(self._by) >= _MAX_PLATFORMS:
                p = "__other__"
            slot = self._by.setdefault(p, {})
            slot[r] = slot.get(r, 0) + 1
            self.total += 1
            self._last_ts = time.time()

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            by_platform: Dict[str, Dict[str, Any]] = {}
            orch_total = 0
            adapter_total = 0
            for p, slot in self._by.items():
                o = int(slot.get("orchestrator", 0))
                a = int(slot.get("adapter", 0))
                orch_total += o
                adapter_total += a
                tot = o + a
                by_platform[p] = {
                    "orchestrator": o,
                    "adapter": a,
                    "total": tot,
                    # 回落率：该平台有多少比例的发送没被编排器接管（越高越该查 worker ownership）
                    "fallback_rate": round(a / tot, 4) if tot else 0.0,
                }
            tot_all = orch_total + adapter_total
            return {
                "started_at": self._started_at,
                "last_record_ts": self._last_ts,
                "total": self.total,
                "orchestrator_total": orch_total,
                "adapter_total": adapter_total,
                "fallback_rate": round(adapter_total / tot_all, 4) if tot_all else 0.0,
                "by_platform": dict(sorted(
                    by_platform.items(),
                    key=lambda kv: (-kv[1]["total"], kv[0]))),
            }

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP inbox_send_routed_total Inbox outbound sends by route "
                "(orchestrator vs adapter fallback)",
                "# TYPE inbox_send_routed_total counter",
            ]
            for p, slot in sorted(self._by.items()):
                for r in _ROUTES:
                    lines.append(
                        f'inbox_send_routed_total{{platform="{_esc(p)}",route="{r}"}} '
                        f'{int(slot.get(r, 0))}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self.total = 0
            self._last_ts = 0.0
            self._by.clear()


_SINGLETON: Optional[SendRouteStats] = None
_LOCK = threading.Lock()


def get_send_route_stats() -> SendRouteStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = SendRouteStats()
    return _SINGLETON


__all__ = ["SendRouteStats", "get_send_route_stats"]
