"""
进程内指标存储，供监控 API 读取，由业务侧写入。
单例、线程安全（简单场景下 GIL 足够）。
"""

import time
import threading
from typing import Optional, List, Dict
from collections import deque, defaultdict

# 保留最近 N 次响应时间用于算 P99
RESPONSE_TIMES_MAX = 500


class MetricsStore:
    _instance: Optional["MetricsStore"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._start_time = time.time()
        self._messages_received = 0
        self._messages_replied = 0
        self._api_calls = 0
        self._errors_count = 0
        self._response_times: deque = deque(maxlen=RESPONSE_TIMES_MAX)
        self._last_message_at: Optional[float] = None
        # 由业务侧定期或按事件更新
        self._queue_size = 0
        self._telegram_connected = False
        self._assistant_ref = None
        self._skill_hits: Dict[str, int] = defaultdict(int)
        self._trigger_layers: Dict[str, int] = defaultdict(int)
        self._cb_state: str = "closed"
        self._cb_open_until: float = 0
        self._rate_limited_count: int = 0
        self._auto_ban_count: int = 0
        self._fallback_count: int = 0
        self._truncated_count: int = 0
        self._reply_lengths: deque = deque(maxlen=200)
        self._ai_last_success_at: float = 0
        self._ai_last_error_at: float = 0
        self._ai_consecutive_errors: int = 0
        self._queue_drops: int = 0
        self._active_tasks: int = 0
        self._concurrency_limit: int = 0
        self._lang_mismatch_count: int = 0
        # 情景记忆 / 慢思考（可选观测）
        self._slow_think_count: int = 0
        self._episodic_inject_count: int = 0
        self._embed_fail_count: int = 0
        self._episodic_backfill_count: int = 0
        # 启动时配置基线建议（与 config_advisories 一致，供 /api/bot-metrics 与 Prometheus）
        self._startup_advisory_total: int = 0
        self._startup_advisory_warnings: int = 0
        self._startup_advisory_audit_logged: Optional[int] = None
        # LINE RPA（ADB 个人号）轮次统计
        self._line_rpa_runs: int = 0
        self._line_rpa_ok: int = 0
        self._line_rpa_step_hits: Dict[str, int] = defaultdict(int)
        self._line_rpa_total_ms: deque = deque(maxlen=200)

    @classmethod
    def get_instance(cls) -> "MetricsStore":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def set_assistant_ref(self, ref):
        """主进程传入 AIChatAssistant 引用，用于 health 等"""
        self._assistant_ref = ref

    def set_telegram_connected(self, connected: bool):
        self._telegram_connected = connected

    def set_queue_size(self, size: int):
        self._queue_size = size

    def record_message_received(self):
        self._messages_received += 1
        self._last_message_at = time.time()

    def record_reply(self):
        self._messages_replied += 1

    def record_response_time_ms(self, ms: float):
        self._response_times.append(ms)

    def record_api_call(self, duration_ms: Optional[float] = None):
        self._api_calls += 1
        if duration_ms is not None:
            self._response_times.append(duration_ms)

    def record_error(self):
        self._errors_count += 1

    def record_skill_hit(self, intent: str):
        """按意图/技能命中计数，供排错与大盘"""
        if intent:
            with self._lock:
                self._skill_hits[intent] = self._skill_hits.get(intent, 0) + 1

    # ── 四层触发统计 ────────────────────────────────────────
    def record_trigger_layer(self, layer: str):
        """layer: 'l1', 'l2', 'l3_filtered', 'l4_silenced', 'skipped'"""
        with self._lock:
            self._trigger_layers[layer] = self._trigger_layers.get(layer, 0) + 1

    # ── 熔断器状态 ──────────────────────────────────────────
    def set_circuit_breaker_state(self, state: str, open_until: float = 0):
        """state: 'closed' | 'open'"""
        self._cb_state = state
        self._cb_open_until = open_until

    # ── 限流计数 ────────────────────────────────────────────
    def record_rate_limited(self):
        with self._lock:
            self._rate_limited_count += 1

    def record_auto_ban(self):
        with self._lock:
            self._auto_ban_count += 1

    def record_fallback_reply(self):
        with self._lock:
            self._fallback_count += 1

    def record_truncated_reply(self):
        with self._lock:
            self._truncated_count += 1

    def record_reply_length(self, length: int):
        self._reply_lengths.append(length)

    def record_ai_success(self):
        self._ai_last_success_at = time.time()
        self._ai_consecutive_errors = 0

    def record_ai_error(self):
        self._ai_last_error_at = time.time()
        self._ai_consecutive_errors += 1

    def record_lang_mismatch(self):
        with self._lock:
            self._lang_mismatch_count += 1

    def record_slow_think(self):
        with self._lock:
            self._slow_think_count += 1

    def record_episodic_inject(self):
        with self._lock:
            self._episodic_inject_count += 1

    def record_embed_fail(self):
        with self._lock:
            self._embed_fail_count += 1

    def record_episodic_backfill(self, n: int = 1):
        with self._lock:
            self._episodic_backfill_count += int(max(0, n))

    def set_startup_advisory_counts(self, total: int, warning_events: int) -> None:
        """启动 collect_production_advisories 之后调用。"""
        with self._lock:
            self._startup_advisory_total = max(0, int(total))
            self._startup_advisory_warnings = max(0, int(warning_events))

    def set_startup_advisory_audit_logged(self, n: int) -> None:
        """Web 启用且 AuditStore 写入 warning 条数之后调用；n 为写入审计的条数。"""
        with self._lock:
            self._startup_advisory_audit_logged = max(0, int(n))

    def record_line_rpa_run(self, *, step: str, ok: bool, total_ms: float) -> None:
        """记录一次 line_rpa run_once 完成（供 /api 与排障）。"""
        with self._lock:
            self._line_rpa_runs += 1
            if ok:
                self._line_rpa_ok += 1
            sk = (step or "").strip() or "unknown"
            self._line_rpa_step_hits[sk] = self._line_rpa_step_hits.get(sk, 0) + 1
            self._line_rpa_total_ms.append(max(0.0, float(total_ms)))

    def record_queue_drop(self):
        with self._lock:
            self._queue_drops += 1

    def set_active_tasks(self, count: int, limit: int):
        self._active_tasks = count
        self._concurrency_limit = limit

    def snapshot(self) -> dict:
        with self._lock:
            skill_hits = dict(self._skill_hits)
            trigger_layers = dict(self._trigger_layers)
            rate_limited = self._rate_limited_count
            auto_bans = self._auto_ban_count
            fallbacks = self._fallback_count
            truncated = self._truncated_count
            slow_think = self._slow_think_count
            episodic_inject = self._episodic_inject_count
            embed_fail = self._embed_fail_count
            epi_backfill = self._episodic_backfill_count
            adv_total = self._startup_advisory_total
            adv_warn = self._startup_advisory_warnings
            adv_audit = self._startup_advisory_audit_logged
            lr_runs = self._line_rpa_runs
            lr_ok = self._line_rpa_ok
            lr_steps = dict(self._line_rpa_step_hits)
        lr_ms = list(self._line_rpa_total_ms)
        lr_avg = round(sum(lr_ms) / len(lr_ms), 2) if lr_ms else 0.0
        times = list(self._response_times)
        n = len(times)
        avg_ms = sum(times) / n if n else 0
        p99_ms = sorted(times)[int(n * 0.99)] if n else 0
        reply_lens = list(self._reply_lengths)
        avg_reply_len = round(sum(reply_lens) / len(reply_lens)) if reply_lens else 0
        replied = max(self._messages_replied, 1)
        return {
            "messages_received": self._messages_received,
            "messages_replied": self._messages_replied,
            "api_calls": self._api_calls,
            "response_time_avg_ms": round(avg_ms, 0),
            "response_time_p99_ms": round(p99_ms, 0),
            "errors_count": self._errors_count,
            "skill_hits": skill_hits,
            "trigger_layers": trigger_layers,
            "circuit_breaker": {
                "state": self._cb_state,
                "open_until": self._cb_open_until,
            },
            "rate_limit": {
                "rejected": rate_limited,
                "auto_bans": auto_bans,
            },
            "reply_quality": {
                "avg_reply_length": avg_reply_len,
                "fallback_count": fallbacks,
                "fallback_rate_pct": round(fallbacks / replied * 100, 1),
                "truncated_count": truncated,
                "truncated_rate_pct": round(truncated / replied * 100, 1),
                "lang_mismatch_count": self._lang_mismatch_count,
            },
            "memory": {
                "slow_think": slow_think,
                "episodic_inject": episodic_inject,
                "embed_fail": embed_fail,
                "episodic_backfill_rows": epi_backfill,
            },
            "startup_advisories": {
                "total": adv_total,
                "warnings": adv_warn,
                "audit_logged_warnings": adv_audit,
            },
            "line_rpa": {
                "runs": lr_runs,
                "ok": lr_ok,
                "success_rate_pct": round(lr_ok / lr_runs * 100, 1) if lr_runs else 0.0,
                "by_step": lr_steps,
                "run_total_ms_avg": lr_avg,
                "run_total_ms_samples": len(lr_ms),
            },
            "queue_size": self._queue_size,
            "queue_drops": self._queue_drops,
            "active_tasks": self._active_tasks,
            "concurrency_limit": self._concurrency_limit,
            "last_message_at": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._last_message_at))
                if self._last_message_at
                else None
            ),
        }

    def uptime_seconds(self) -> float:
        return time.time() - self._start_time

    def telegram_connected(self) -> bool:
        if self._assistant_ref is not None:
            try:
                tc = getattr(self._assistant_ref, "telegram_client", None)
                if tc is not None:
                    return getattr(tc, "running", False) and getattr(tc, "client", None) is not None
            except Exception:
                pass
        return self._telegram_connected

    def ai_healthy(self) -> bool:
        if self._ai_consecutive_errors >= 5:
            return False
        if self._cb_state == "open":
            return False
        return True

    def status(self) -> str:
        tg = self.telegram_connected()
        ai = self.ai_healthy()
        if tg and ai:
            return "ok"
        if tg or ai:
            return "degraded"
        return "down"


def get_metrics_store() -> MetricsStore:
    return MetricsStore.get_instance()
