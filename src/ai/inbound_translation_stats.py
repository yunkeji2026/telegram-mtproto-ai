"""入站自动翻译（/thread 打开会话懒翻译）运行观测（进程级单例）。

2026-07 性能重构（同步预算 + 后台补译）配套：入站翻译从「响应路径同步整批」改为
「同步只译最新 2 条、其余后台任务写库」后，**后台侧的健康状况**（补译量、失败率、
noop 打标数、负缓存规模、当前 in-flight）此前不可见——积压/引擎宕机只能等坐席抱怨。

本模块把两侧都变成可观测计数：

- ``sync_*``：/thread 响应路径同步侧（ok=有效译文 / noop=产出==原文打标 / fail）；
- ``deferred_total``：转交后台的候选条数；``bg_spawned``：后台任务数；
- ``bg_*``：后台任务侧同三态；
- ``skipped_cooldown``：因失败负缓存/在译中被跳过的候选（重试风暴被挡住的证据）。

区别于 ``inbound_xlate_daily``（store 按日漏斗，跨重启、供 dashboard 趋势）：本单例是
**进程级工程观测**（含后台/负缓存内部态），经 ``dump()``→``/api/workspace/metrics
.inbound_translation``、``dump_prom()``→Prometheus，供 ops「🌐 翻译引擎」卡与告警。

风格对齐 ``src/ai/outbound_translation_stats.py``：无新增依赖，线程安全，只存计数。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


class InboundTranslationStats:
    """入站翻译（同步预算 + 后台补译）计数（线程安全，进程级）。"""

    __slots__ = (
        "_lock", "_started_at", "_last_ts",
        "sync_ok", "sync_noop", "sync_fail",
        "deferred_total", "bg_spawned",
        "bg_ok", "bg_noop", "bg_fail",
        "skipped_cooldown",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._last_ts = 0.0
        self.sync_ok = 0           # 同步侧：有效译文
        self.sync_noop = 0         # 同步侧：产出==原文（打「已处理」标）
        self.sync_fail = 0         # 同步侧：异常/超时/引擎全败
        self.deferred_total = 0    # 转交后台的候选条数
        self.bg_spawned = 0        # 后台任务数（会话级）
        self.bg_ok = 0
        self.bg_noop = 0
        self.bg_fail = 0
        self.skipped_cooldown = 0  # 负缓存/在译中跳过（重试风暴被挡）

    def record_sync(self, outcome: str) -> None:
        with self._lock:
            self._last_ts = time.time()
            if outcome == "ok":
                self.sync_ok += 1
            elif outcome == "noop":
                self.sync_noop += 1
            else:
                self.sync_fail += 1

    def record_bg(self, outcome: str) -> None:
        with self._lock:
            self._last_ts = time.time()
            if outcome == "ok":
                self.bg_ok += 1
            elif outcome == "noop":
                self.bg_noop += 1
            else:
                self.bg_fail += 1

    def record_deferred(self, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self._last_ts = time.time()
            self.deferred_total += int(n)
            self.bg_spawned += 1

    def record_skipped_cooldown(self, n: int = 1) -> None:
        if n <= 0:
            return
        with self._lock:
            self.skipped_cooldown += int(n)

    def dump(self, runtime: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """``runtime``：调用方注入的瞬时态（in-flight 会话/消息、负缓存大小），
        由 ``src.workspace.inbound_translate.runtime_snapshot()`` 产出。"""
        with self._lock:
            bg_total = self.bg_ok + self.bg_noop + self.bg_fail
            out: Dict[str, Any] = {
                "started_at": self._started_at,
                "last_record_ts": self._last_ts,
                "sync_ok": self.sync_ok,
                "sync_noop": self.sync_noop,
                "sync_fail": self.sync_fail,
                "deferred_total": self.deferred_total,
                "bg_spawned": self.bg_spawned,
                "bg_ok": self.bg_ok,
                "bg_noop": self.bg_noop,
                "bg_fail": self.bg_fail,
                "bg_fail_rate": round(self.bg_fail / bg_total, 4) if bg_total else 0,
                "skipped_cooldown": self.skipped_cooldown,
            }
        if runtime:
            out["runtime"] = dict(runtime)
        return out

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP inbound_xlate_sync_total Inbound auto-translate sync-path results",
                "# TYPE inbound_xlate_sync_total counter",
                f'inbound_xlate_sync_total{{outcome="ok"}} {self.sync_ok}',
                f'inbound_xlate_sync_total{{outcome="noop"}} {self.sync_noop}',
                f'inbound_xlate_sync_total{{outcome="fail"}} {self.sync_fail}',
                "# HELP inbound_xlate_bg_total Inbound auto-translate background-task results",
                "# TYPE inbound_xlate_bg_total counter",
                f'inbound_xlate_bg_total{{outcome="ok"}} {self.bg_ok}',
                f'inbound_xlate_bg_total{{outcome="noop"}} {self.bg_noop}',
                f'inbound_xlate_bg_total{{outcome="fail"}} {self.bg_fail}',
                "# HELP inbound_xlate_deferred_total Candidates handed to background translate",
                "# TYPE inbound_xlate_deferred_total counter",
                f"inbound_xlate_deferred_total {self.deferred_total}",
                "# HELP inbound_xlate_skipped_cooldown_total Candidates skipped by fail-cooldown/in-flight",
                "# TYPE inbound_xlate_skipped_cooldown_total counter",
                f"inbound_xlate_skipped_cooldown_total {self.skipped_cooldown}",
            ]
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self.sync_ok = self.sync_noop = self.sync_fail = 0
            self.deferred_total = self.bg_spawned = 0
            self.bg_ok = self.bg_noop = self.bg_fail = 0
            self.skipped_cooldown = 0
            self._last_ts = 0.0


_SINGLETON: Optional[InboundTranslationStats] = None
_LOCK = threading.Lock()


def get_inbound_translation_stats() -> InboundTranslationStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = InboundTranslationStats()
    return _SINGLETON


__all__ = ["InboundTranslationStats", "get_inbound_translation_stats"]
