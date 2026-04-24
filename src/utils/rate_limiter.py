"""令牌桶限流器 — user / chat / global 三层限流 + 自动封禁"""

import logging
import time
from typing import Dict, Tuple

logger = logging.getLogger("RateLimiter")


class TokenBucket:
    __slots__ = ("capacity", "rate", "_tokens", "_last_refill")

    def __init__(self, capacity: int, rate: float):
        self.capacity = capacity
        self.rate = rate
        self._tokens = float(capacity)
        self._last_refill = time.time()

    def consume(self, n: int = 1) -> bool:
        now = time.time()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last_refill = now
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False

    @property
    def tokens(self) -> float:
        now = time.time()
        elapsed = now - self._last_refill
        return min(self.capacity, self._tokens + elapsed * self.rate)


class RateLimiter:

    def __init__(self, config: dict = None):
        cfg = (config or {}).get("rate_limit", {})
        self._enabled = cfg.get("enabled", False)

        g = cfg.get("global", {})
        self._global_bucket = TokenBucket(
            capacity=int(g.get("capacity", 30)),
            rate=float(g.get("rate_per_sec", 2)),
        )

        self._user_cfg = cfg.get("per_user", {})
        self._user_capacity = int(self._user_cfg.get("capacity", 5))
        self._user_rate = float(self._user_cfg.get("rate_per_sec", 0.2))

        self._chat_cfg = cfg.get("per_chat", {})
        self._chat_capacity = int(self._chat_cfg.get("capacity", 20))
        self._chat_rate = float(self._chat_cfg.get("rate_per_sec", 1))

        self._user_buckets: Dict[str, TokenBucket] = {}
        self._chat_buckets: Dict[int, TokenBucket] = {}

        # ── 自动封禁 ──
        ban_cfg = cfg.get("auto_ban", {})
        self._ban_enabled = ban_cfg.get("enabled", True)
        self._ban_threshold = int(ban_cfg.get("threshold", 20))
        self._ban_window = int(ban_cfg.get("window_sec", 60))
        self._ban_duration = int(ban_cfg.get("ban_duration", 600))
        self._ban_hits: Dict[str, list] = {}
        self._banned: Dict[str, float] = {}

        self._stats = {
            "blocked_global": 0, "blocked_user": 0,
            "blocked_chat": 0, "blocked_ban": 0, "passed": 0,
        }
        self._max_buckets = 2000

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── 封禁相关 ──

    def is_banned(self, user_id: str) -> bool:
        """检查用户是否在封禁名单中"""
        if not self._ban_enabled or not user_id:
            return False
        expire = self._banned.get(user_id)
        if expire is None:
            return False
        if time.time() < expire:
            self._stats["blocked_ban"] = self._stats.get("blocked_ban", 0) + 1
            return True
        del self._banned[user_id]
        return False

    def check_auto_ban(self, user_id: str) -> bool:
        """记录一次限流命中，达到阈值时自动封禁；返回是否刚触发封禁"""
        if not self._ban_enabled or not user_id:
            return False
        now = time.time()
        hits = self._ban_hits.get(user_id)
        if hits is None:
            hits = []
            self._ban_hits[user_id] = hits
        hits.append(now)
        cutoff = now - self._ban_window
        self._ban_hits[user_id] = [t for t in hits if t > cutoff]
        if len(self._ban_hits[user_id]) >= self._ban_threshold:
            self._banned[user_id] = now + self._ban_duration
            self._ban_hits.pop(user_id, None)
            logger.warning("用户 %s 被自动封禁 %ds（%d 次限流/%ds）",
                           user_id, self._ban_duration,
                           self._ban_threshold, self._ban_window)
            self._evict_bans()
            return True
        return False

    def _evict_bans(self):
        """清理过期封禁条目"""
        now = time.time()
        expired = [k for k, v in self._banned.items() if v <= now]
        for k in expired:
            del self._banned[k]

    # ── 核心限流 ──

    def allow(self, user_id: str = "", chat_id: int = 0) -> Tuple[bool, str]:
        if not self._enabled:
            return True, ""

        if user_id and self.is_banned(user_id):
            return False, "banned"

        if not self._global_bucket.consume():
            self._stats["blocked_global"] += 1
            return False, "global"

        if chat_id:
            cb = self._chat_buckets.get(chat_id)
            if cb is None:
                cb = TokenBucket(self._chat_capacity, self._chat_rate)
                self._chat_buckets[chat_id] = cb
                self._evict(self._chat_buckets)
            if not cb.consume():
                self._stats["blocked_chat"] += 1
                return False, "chat"

        if user_id:
            ub = self._user_buckets.get(user_id)
            if ub is None:
                ub = TokenBucket(self._user_capacity, self._user_rate)
                self._user_buckets[user_id] = ub
                self._evict(self._user_buckets)
            if not ub.consume():
                self._stats["blocked_user"] += 1
                return False, "user"

        self._stats["passed"] += 1
        return True, ""

    def get_stats(self) -> Dict:
        s = dict(self._stats)
        s["active_bans"] = sum(1 for v in self._banned.values() if v > time.time())
        return s

    def _evict(self, buckets: dict):
        if len(buckets) > self._max_buckets:
            oldest_keys = sorted(buckets.keys(), key=lambda k: buckets[k]._last_refill)
            for k in oldest_keys[:len(buckets) - self._max_buckets // 2]:
                buckets.pop(k, None)
