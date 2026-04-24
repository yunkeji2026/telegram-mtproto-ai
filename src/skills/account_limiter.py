"""AccountLimiter — 控制每账号 / 全域的每日 handoff 发送配额。

为什么需要：Meta 风控会关注"这个账号一天把多少人往外 App 引"。
daily_cap=15 表示每个 Messenger 账号每天最多发 15 次引流话术，
超过后 `check_and_reserve` 会拒绝——让 runner 走普通回复。

global_cap 是全域当日总数软上限（所有账号合计），防止一整个话术池被 Meta
聚合识别。超出后所有账号都会被拒绝。

存储：account_handoff_counters 表，按 UTC 日期分桶，PK 原子递增。
跨进程安全，因为 SQLite 文件锁保护了 UPDATE。

注意：**不是 rate-limiter（秒/分钟），是 quota（日）**。频率类反封号
（"一分钟连发 5 条"）由 rpa runner 自己做 pacing，这里不管。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class LimitDecision:
    ok: bool
    reason: str = ""
    remaining_today: int = 0
    account_count_today: int = 0
    global_count_today: int = 0


class AccountLimiter:
    """每账号 / 全域的日配额；按 UTC 日期切桶。"""

    def __init__(
        self,
        store,
        *,
        daily_cap: int = 15,
        global_cap: int = 0,   # 0 = 不启用全域限额
        alert_thresholds_pct: Optional[list] = None,   # 如 [80, 100]；None=不告警
        on_threshold_crossed: Optional[Callable[[str, int, int, int], None]] = None,
    ) -> None:
        self._store = store
        self._daily_cap = max(1, int(daily_cap))
        self._global_cap = max(0, int(global_cap))
        # W4-Cap-Alert：跨过任意 pct 阈值时触发回调（stateless：基于 old→new 区间）
        self._thresholds = sorted(set(
            int(p) for p in (alert_thresholds_pct or []) if 0 < int(p) <= 100
        ))
        self._on_threshold = on_threshold_crossed

    # ── W4-Cap-Alert：late-binding 设置回调（main.py 在 webhook 就绪后调） ──
    def set_on_threshold_crossed(
        self,
        callback: Optional[Callable[[str, int, int, int], None]],
    ) -> None:
        self._on_threshold = callback

    # ── 查 ────────────────────────────────────────────
    @staticmethod
    def _utc_day(ts: Optional[int] = None) -> str:
        ts = ts if ts is not None else int(time.time())
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    def remaining_for(self, account_id: str, *, now: Optional[int] = None) -> int:
        day = self._utc_day(now)
        used = self._store.get_account_handoff_counter(account_id, day)
        return max(0, self._daily_cap - used)

    def global_remaining(self, *, now: Optional[int] = None) -> int:
        if self._global_cap <= 0:
            return 10 ** 9    # 相当于无限
        day = self._utc_day(now)
        used = self._store.sum_account_handoff_counters(day)
        return max(0, self._global_cap - used)

    def get_counts(self, account_id: str, *, now: Optional[int] = None) -> dict:
        day = self._utc_day(now)
        return {
            "day": day,
            "account_count": self._store.get_account_handoff_counter(account_id, day),
            "account_remaining": self.remaining_for(account_id, now=now),
            "global_count": self._store.sum_account_handoff_counters(day),
            "global_cap": self._global_cap,
            "daily_cap": self._daily_cap,
        }

    # ── 预判 + 扣减（原子一对）──────────────────────────
    def check_and_reserve(
        self,
        account_id: str,
        *,
        now: Optional[int] = None,
    ) -> LimitDecision:
        """只有返回 ok=True 时才扣成功。失败不扣。

        调用方约定：拿到 ok=True 就当这次引流已占坑，后续无论发送成功与否
        都不退还；发送失败应走 runner 的 retry/降级，不通过还额度。
        """
        day = self._utc_day(now)
        # 全域优先判
        if self._global_cap > 0:
            global_used = self._store.sum_account_handoff_counters(day)
            if global_used >= self._global_cap:
                return LimitDecision(
                    ok=False, reason="global_cap_exceeded",
                    remaining_today=0,
                    global_count_today=global_used,
                )
        # 账号级
        acct_used = self._store.get_account_handoff_counter(account_id, day)
        if acct_used >= self._daily_cap:
            return LimitDecision(
                ok=False, reason="account_cap_exceeded",
                remaining_today=0,
                account_count_today=acct_used,
            )
        # 扣
        new_count = self._store.incr_account_handoff_counter(account_id, day)
        global_used_after = self._store.sum_account_handoff_counters(day) \
            if self._global_cap > 0 else 0
        logger.info("AccountLimiter reserved: acc=%s day=%s count=%d/%d",
                    account_id, day, new_count, self._daily_cap)
        # W4-Cap-Alert：阈值跨越检测（stateless——仅看 old→new 区间）
        self._emit_threshold_crossings(account_id, new_count)
        return LimitDecision(
            ok=True, reason="reserved",
            remaining_today=max(0, self._daily_cap - new_count),
            account_count_today=new_count,
            global_count_today=global_used_after,
        )

    # ── W4-Cap-Alert ──────────────────────────────────────
    def _emit_threshold_crossings(self, account_id: str, new_count: int) -> None:
        """new_count 刚 +1。若上一步的 pct < 某阈值 <= 新 pct，触发回调。

        stateless：基于 (old_count, new_count) 计算——每次跨越只触发一次，
        当天后续扣减不会重复触发同一阈值（因为 pct 单调递增）。
        """
        if not self._thresholds or self._on_threshold is None:
            return
        old_count = new_count - 1
        cap = self._daily_cap
        old_pct = old_count * 100.0 / cap
        new_pct = new_count * 100.0 / cap
        for pct in self._thresholds:
            if old_pct < pct <= new_pct:
                try:
                    self._on_threshold(account_id, pct, new_count, cap)
                except Exception:
                    logger.warning(
                        "AccountLimiter threshold callback failed "
                        "(acc=%s pct=%s)", account_id, pct, exc_info=True)

    # ── 退款（业务层拒绝后释放配额） ─────────────────
    def refund(self, account_id: str, *, now: Optional[int] = None) -> bool:
        """预扣后的业务层失败（合规拒/渲染失败）时调用，释放一个配额。

        保守：计数不会低于 0；返回是否确实减了 1。
        """
        day = self._utc_day(now)
        current = self._store.get_account_handoff_counter(account_id, day)
        if current <= 0:
            return False
        # sqlite 没直接的条件减，借一层：写一个"手动 -1"的便利方法
        return self._store.decr_account_handoff_counter(account_id, day) > 0

    # ── 手动工具 ──────────────────────────────────────
    def reset(self, account_id: str, *, now: Optional[int] = None) -> None:
        """运营/测试手动清零今日计数。"""
        day = self._utc_day(now)
        self._store.reset_account_handoff_counter(account_id, day)
