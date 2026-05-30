"""线程安全的每日发送上限跟踪器

4 个 RPA service 都有"每日发送上限"逻辑，但各自实现略有差异：
- LINE: 用 state_store DB 的 run_stats 直接统计当日 sent
- WhatsApp: 用 state_store DB 同样统计
- Messenger: 内存中维护 send_stats + DB 保底
- Telegram: 不强制每日上限（MTProto 限制由 Telegram 服务器决定）

本模块提供一个**纯内存**的可选工具，作为快速判断或缓存层，不替代 DB。

使用方式：

    cap = DailyCapTracker(daily_cap=300)
    if cap.would_exceed():
        return  # 跳过本次发送
    # ...发送成功后
    cap.record_sent()

每日 0 点（按 timezone）自动 reset。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class DailyCapSnapshot:
    """对外暴露的快照（避免外部修改内部状态）。"""

    daily_cap: int  # 0 = 不限
    daily_sent: int
    reset_at_ts: float  # 下次自动归零的 wallclock ts
    remaining: int  # daily_cap - daily_sent，cap=0 时为 None


class DailyCapTracker:
    """线程安全的每日上限跟踪器。

    Args:
        daily_cap: 每日发送上限。0 = 不限。
        tz_offset_hours: 时区偏移（小时）。日界以此 tz 0 点为准。
        initial_sent: 初始已发送计数（可从 DB 恢复时传入）。
        initial_day: 初始的"日"标识 (YYYY-MM-DD 字符串)；不传则用今日。
    """

    def __init__(
        self,
        daily_cap: int = 0,
        *,
        tz_offset_hours: float = 8.0,  # 默认 UTC+8（亚洲市场）
        initial_sent: int = 0,
        initial_day: Optional[str] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._daily_cap = max(0, int(daily_cap))
        self._tz_offset = float(tz_offset_hours)
        self._sent = max(0, int(initial_sent))
        self._day_key = initial_day or self._today_key()

    # ── 私有：日界判断 ────────────────────────────────────────────────────

    def _today_key(self) -> str:
        """当前 tz 下的 YYYY-MM-DD。"""
        offset_sec = self._tz_offset * 3600
        local_ts = time.time() + offset_sec
        return datetime.utcfromtimestamp(local_ts).strftime("%Y-%m-%d")

    def _maybe_reset(self) -> None:
        """若跨日则重置 sent 计数。必须在持锁状态下调用。"""
        today = self._today_key()
        if today != self._day_key:
            self._day_key = today
            self._sent = 0

    def _next_reset_ts(self) -> float:
        """下一次 0 点的 wallclock ts。"""
        offset_sec = self._tz_offset * 3600
        now = time.time() + offset_sec
        # 当前 tz 当日 0 点 ts
        today_midnight = datetime.utcfromtimestamp(now).replace(
            hour=0, minute=0, second=0, microsecond=0,
            tzinfo=timezone.utc,
        ).timestamp()
        # 转回原始 wallclock 系
        today_midnight_wallclock = today_midnight - offset_sec
        # 加 24h 得到下一个 0 点
        return today_midnight_wallclock + 86400.0

    # ── 公开 API ──────────────────────────────────────────────────────────

    def set_cap(self, daily_cap: int) -> None:
        """运行时调整 cap（cfg.put）。"""
        with self._lock:
            self._daily_cap = max(0, int(daily_cap))

    def would_exceed(self, n: int = 1) -> bool:
        """判断再发 n 条是否会超限。cap=0 时永远返回 False。"""
        if n <= 0:
            return False
        with self._lock:
            self._maybe_reset()
            if self._daily_cap <= 0:
                return False
            return self._sent + n > self._daily_cap

    def remaining(self) -> int:
        """剩余可发条数。cap=0 时返回 -1 表示不限。"""
        with self._lock:
            self._maybe_reset()
            if self._daily_cap <= 0:
                return -1
            return max(0, self._daily_cap - self._sent)

    def record_sent(self, n: int = 1) -> int:
        """记录已发送 n 条。返回更新后的 sent 计数。"""
        if n <= 0:
            return self._sent
        with self._lock:
            self._maybe_reset()
            self._sent += int(n)
            return self._sent

    def reset(self) -> None:
        """手动重置（运维操作）。"""
        with self._lock:
            self._sent = 0
            self._day_key = self._today_key()

    def snapshot(self) -> DailyCapSnapshot:
        """获取当前状态快照。"""
        with self._lock:
            self._maybe_reset()
            cap = self._daily_cap
            sent = self._sent
            reset_at = self._next_reset_ts()
        return DailyCapSnapshot(
            daily_cap=cap,
            daily_sent=sent,
            reset_at_ts=reset_at,
            remaining=(cap - sent) if cap > 0 else -1,
        )

    @property
    def daily_cap(self) -> int:
        with self._lock:
            return self._daily_cap

    @property
    def daily_sent(self) -> int:
        with self._lock:
            self._maybe_reset()
            return self._sent
