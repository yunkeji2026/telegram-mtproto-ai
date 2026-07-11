"""协议自动回复限速 + 熔断（Phase 5）。

放量挂大量号前的最后一道保险，**按账号**维度（区别于去重/冷却的按会话维度）：

- 限速/配额：每账号每小时 / 每天自动发上限，超限 → 转人工（不自动发，防封号）。
- 熔断：同账号连续 N 次基础设施失败（发送/生成失败）→ 打开断路器，冷却期内
  全部转人工；冷却后半开放行一次试探，成功即闭合，失败再开（标准断路器，避免永久锁死）。

进程内内存态（与 ``_last_reply`` 一致，多进程部署需挪共享存储——已知限制）。
单例 ``get_autoreply_limiter`` 供 hook 与 ``/api/accounts`` 共享同一份计数。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Deque, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_HOUR = 3600.0
_DAY = 86400.0

_SEND_DDL = """
CREATE TABLE IF NOT EXISTS account_sends (
    account_key TEXT NOT NULL,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_sends_key_ts
    ON account_sends(account_key, ts);
"""


class SendCountStore:
    """按账号的发送时间戳持久化（让 send-gate 日/时配额**跨进程重启存活**）。

    背景：``AutoReplyLimiter`` 原本纯内存计数，服务一重启 ``sends_today`` 即归零 →
    「日配额」实为「本进程生命周期配额」，频繁重启时形同虚设（真号安全洞）。本存储把
    每次实际发送落 SQLite，日/时窗口计数改为查库，重启后仍反映真实 24h 量。

    线程安全（check_same_thread=False + 自带锁，与仓内其它 store 一致）；启动与周期
    清理 >2 天的陈旧行（配额只看 24h，留 48h 冗余即可，表恒小）。绝不抛给调用方——
    任何 IO 异常由上层 limiter 捕获后降级回内存，**永不阻断发送**。
    """

    def __init__(self, db_path: Any) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=10)
        self._lock = threading.Lock()
        self._writes = 0
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SEND_DDL)
            self._conn.commit()
            self._prune_locked(time.time() - 2 * _DAY)

    def record(self, account_key: str, ts: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO account_sends (account_key, ts) VALUES (?, ?)",
                (str(account_key), float(ts)),
            )
            self._conn.commit()
            self._writes += 1
            if self._writes % 100 == 0:      # 摊还清理，热路径零额外开销
                self._prune_locked(time.time() - 2 * _DAY)

    def count_since(self, account_key: str, since_ts: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM account_sends WHERE account_key=? AND ts>=?",
                (str(account_key), float(since_ts)),
            ).fetchone()
        return int((row[0] if row else 0) or 0)

    def _prune_locked(self, before_ts: float) -> None:
        try:
            self._conn.execute("DELETE FROM account_sends WHERE ts<?", (float(before_ts),))
            self._conn.commit()
        except Exception:
            logger.debug("[send-count] prune 失败（忽略）", exc_info=True)


class AutoReplyLimiter:
    """按账号的限速 + 断路器（线程安全）。"""

    def __init__(
        self, *, hourly: int = 30, daily: int = 200,
        breaker_threshold: int = 5, breaker_cooldown: float = 300.0,
        store: Optional["SendCountStore"] = None,
    ) -> None:
        self.hourly = int(hourly or 0)
        self.daily = int(daily or 0)
        self.breaker_threshold = int(breaker_threshold or 0)
        self.breaker_cooldown = float(breaker_cooldown or 0)
        self._sends: Dict[str, Deque[float]] = defaultdict(deque)
        self._fails: Dict[str, int] = defaultdict(int)
        self._open_until: Dict[str, float] = {}
        self._lock = threading.Lock()
        # 可选持久化：跨重启存活的日/时计数（None=纯内存，保持旧行为，零破坏）。
        self._store = store

    def _counts(self, account_key: str, now: float) -> Tuple[int, int]:
        """返回 (hour_used, day_used)。有持久化 store → 查库（跨重启真值）；
        否则用内存 deque（旧行为）。store 查询异常 → 降级内存（守卫绝不因 IO 卡死发送）。"""
        if self._store is not None:
            try:
                hour = self._store.count_since(account_key, now - _HOUR)
                day = self._store.count_since(account_key, now - _DAY)
                return hour, day
            except Exception:
                logger.debug("[limiter] store 计数失败，降级内存", exc_info=True)
        dq = self._sends.get(account_key)
        if dq is None:
            return 0, 0
        self._prune(dq, now)
        return sum(1 for t in dq if t >= now - _HOUR), len(dq)

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
            hour, day = self._counts(account_key, now)
            if h_limit and hour >= h_limit:
                return False, "quota_hour"
            if d_limit and day >= d_limit:
                return False, "quota_day"
            return True, "ok"

    def record_sent(self, account_key: str, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            self._sends[account_key].append(now)   # 内存态仍维护（store 缺失时的兜底）
        # 持久化在锁外（避免 DB IO 占内存锁）；失败不影响内存计数
        if self._store is not None:
            try:
                self._store.record(account_key, now)
            except Exception:
                logger.debug("[limiter] store.record 失败（内存已记）", exc_info=True)

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
            hour, day = self._counts(account_key, now)
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
    """进程内单例。首次调用按 ``config.protocol_autoreply.{rate,breaker}`` 取阈值。

    默认接持久化 ``SendCountStore``（``config/account_sends.db``）→ send-gate 日/时配额
    跨重启存活；建库失败自动降级纯内存（旧行为，绝不阻断启动）。``protocol_autoreply.rate.
    persist=false`` 可显式关闭持久化。
    """
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                pa = (cfg or {}).get("protocol_autoreply") or {}
                rate = pa.get("rate") or {}
                brk = pa.get("breaker") or {}
                store = None
                if rate.get("persist", True):
                    try:
                        store = SendCountStore(
                            str(rate.get("db_path") or "config/account_sends.db"))
                    except Exception:
                        logger.warning(
                            "[limiter] SendCountStore 建库失败，降级纯内存计数", exc_info=True)
                        store = None
                _limiter = AutoReplyLimiter(
                    hourly=rate.get("hourly", 30),
                    daily=rate.get("daily", 200),
                    breaker_threshold=brk.get("threshold", 5),
                    breaker_cooldown=brk.get("cooldown_sec", 300),
                    store=store,
                )
    return _limiter


def reset_autoreply_limiter() -> None:
    """测试辅助：清空单例。"""
    global _limiter
    with _limiter_lock:
        _limiter = None
