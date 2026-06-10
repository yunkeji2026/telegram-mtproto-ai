"""协议自动回复限速 + 熔断（Phase 5）。

放量挂大量号前的最后一道保险，**按账号**维度（区别于去重/冷却的按会话维度）：

- 限速/配额：每账号每小时 / 每天自动发上限，超限 → 转人工（不自动发，防封号）。
- 熔断：同账号连续 N 次基础设施失败（发送/生成失败）→ 打开断路器，冷却期内
  全部转人工；冷却后半开放行一次试探，成功即闭合，失败再开（标准断路器，避免永久锁死）。

进程内内存态（与 ``_last_reply`` 一致，多进程部署需挪共享存储——已知限制）。
单例 ``get_autoreply_limiter`` 供 hook 与 ``/api/accounts`` 共享同一份计数。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Optional, Tuple

_HOUR = 3600.0
_DAY = 86400.0


class AutoReplyLimiter:
    """按账号的限速 + 断路器（线程安全）。"""

    def __init__(
        self, *, hourly: int = 30, daily: int = 200,
        breaker_threshold: int = 5, breaker_cooldown: float = 300.0,
    ) -> None:
        self.hourly = int(hourly or 0)
        self.daily = int(daily or 0)
        self.breaker_threshold = int(breaker_threshold or 0)
        self.breaker_cooldown = float(breaker_cooldown or 0)
        self._sends: Dict[str, Deque[float]] = defaultdict(deque)
        self._fails: Dict[str, int] = defaultdict(int)
        self._open_until: Dict[str, float] = {}
        self._lock = threading.Lock()

    def configure(
        self, *, hourly: Optional[int] = None, daily: Optional[int] = None,
        breaker_threshold: Optional[int] = None,
        breaker_cooldown: Optional[float] = None,
    ) -> None:
        """运行时改阈值（设置面板保存时调用，无需重启）。仅改非 None 项。"""
        with self._lock:
            if hourly is not None:
                self.hourly = int(hourly or 0)
            if daily is not None:
                self.daily = int(daily or 0)
            if breaker_threshold is not None:
                self.breaker_threshold = int(breaker_threshold or 0)
            if breaker_cooldown is not None:
                self.breaker_cooldown = float(breaker_cooldown or 0)

    @staticmethod
    def _prune(dq: Deque[float], now: float) -> None:
        cutoff = now - _DAY
        while dq and dq[0] < cutoff:
            dq.popleft()

    def allow(
        self, account_key: str, now: Optional[float] = None,
        *, hourly: Optional[int] = None, daily: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """是否允许自动发。返回 (allowed, reason)。
        reason ∈ ok | circuit_open | quota_hour | quota_day。
        ``hourly``/``daily`` 可按账号覆盖全局上限（None=用全局）。"""
        now = now if now is not None else time.time()
        h_limit = self.hourly if hourly is None else int(hourly or 0)
        d_limit = self.daily if daily is None else int(daily or 0)
        with self._lock:
            ou = self._open_until.get(account_key, 0.0)
            if ou and now < ou:
                return False, "circuit_open"
            dq = self._sends[account_key]
            self._prune(dq, now)
            day = len(dq)
            hour = sum(1 for t in dq if t >= now - _HOUR)
            if h_limit and hour >= h_limit:
                return False, "quota_hour"
            if d_limit and day >= d_limit:
                return False, "quota_day"
            return True, "ok"

    def record_sent(self, account_key: str, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            self._sends[account_key].append(now)

    def record_success(self, account_key: str) -> None:
        """成功 → 清失败计数 + 闭合断路器。"""
        with self._lock:
            self._fails[account_key] = 0
            self._open_until.pop(account_key, None)

    def record_failure(self, account_key: str, now: Optional[float] = None) -> bool:
        """基础设施失败 → 累加；达到阈值则打开断路器。返回「本次是否刚触发熔断」。"""
        now = now if now is not None else time.time()
        with self._lock:
            self._fails[account_key] += 1
            if (self.breaker_threshold
                    and self._fails[account_key] >= self.breaker_threshold):
                self._open_until[account_key] = now + self.breaker_cooldown
                self._fails[account_key] = 0
                return True
            return False

    def snapshot(
        self, account_key: str, now: Optional[float] = None,
        *, hourly: Optional[int] = None, daily: Optional[int] = None,
    ) -> Dict[str, Any]:
        """给 UI / API 的配额与熔断快照（hourly/daily 可按账号覆盖显示上限）。"""
        now = now if now is not None else time.time()
        h_limit = self.hourly if hourly is None else int(hourly or 0)
        d_limit = self.daily if daily is None else int(daily or 0)
        with self._lock:
            dq = self._sends.get(account_key)
            if dq is not None:
                self._prune(dq, now)
                day = len(dq)
                hour = sum(1 for t in dq if t >= now - _HOUR)
            else:
                day = hour = 0
            ou = self._open_until.get(account_key, 0.0)
            open_now = bool(ou and now < ou)
            return {
                "hour_used": hour, "hour_limit": h_limit,
                "day_used": day, "day_limit": d_limit,
                "circuit_open": open_now,
                "circuit_until": ou if open_now else 0,
            }


_limiter: Optional[AutoReplyLimiter] = None
_limiter_lock = threading.Lock()


def get_autoreply_limiter(cfg: Optional[Dict[str, Any]] = None) -> AutoReplyLimiter:
    """进程内单例。首次调用按 ``config.protocol_autoreply.{rate,breaker}`` 取阈值。"""
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                pa = (cfg or {}).get("protocol_autoreply") or {}
                rate = pa.get("rate") or {}
                brk = pa.get("breaker") or {}
                _limiter = AutoReplyLimiter(
                    hourly=rate.get("hourly", 30),
                    daily=rate.get("daily", 200),
                    breaker_threshold=brk.get("threshold", 5),
                    breaker_cooldown=brk.get("cooldown_sec", 300),
                )
    return _limiter


def reset_autoreply_limiter() -> None:
    """测试辅助：清空单例。"""
    global _limiter
    with _limiter_lock:
        _limiter = None
