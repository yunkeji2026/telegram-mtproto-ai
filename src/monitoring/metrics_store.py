"""
进程内指标存储，供监控 API 读取，由业务侧写入。
单例、线程安全（简单场景下 GIL 足够）。
"""

import time
import threading
from typing import Optional, List, Dict, Any, Tuple
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
        # ★ companion v3 安全网：safe_skip 计数 + 时间序列（最近 1 小时）
        # 目的：当 companion_mode 把消息全部 safe_skip 掉时，dashboard 能立即看到
        self._companion_safe_skip_total: int = 0
        self._companion_safe_skip_by_reason: Dict[str, int] = defaultdict(int)
        self._companion_safe_skip_recent: deque = deque(maxlen=200)  # (ts, reason)
        # ★ W2-D2.7 v7：deferred 队列健康观测（drain loop 写入）
        # 由 service._deferred_drain_loop 每 tick 调用 set_deferred_queue_size 更新
        self._deferred_queue_total: int = 0
        self._deferred_queue_by_account: Dict[str, int] = {}
        self._deferred_drain_sent_1h: int = 0   # 最近 1h drain 真发条数
        self._deferred_drain_sent_recent: deque = deque(maxlen=500)  # (ts,)
        self._deferred_drain_failed_recent: deque = deque(maxlen=200)  # (ts, reason)
        # ★ W2-D4.7：reactivation 主动唤醒观测
        self._reactivation_scheduled_recent: deque = deque(maxlen=500)  # (ts, contact_id_short)
        self._reactivation_failed_recent: deque = deque(maxlen=200)     # (ts, reason)
        self._reactivation_skipped_recent: deque = deque(maxlen=500)    # (ts, reason)
        self._reactivation_dry_run_recent: deque = deque(maxlen=500)    # (ts,)
        self._reactivation_last_run_ts: float = 0.0
        self._reactivation_last_candidates: int = 0
        # ★ W2-D5.1 + W3-D3.5：dry_run 话术样本（容量 200 覆盖 24h+ 灰度）
        self._reactivation_dry_samples: deque = deque(maxlen=200)
        # 元素结构：{ts, contact_id, chat_name, reply_text, silent_days, account_id, ...}
        # ★ Phase O 质量闭环：care dispatcher dry_run 话术样本（与 reactivation 同范式）
        self._care_dry_samples: deque = deque(maxlen=200)
        # ★ O·P 联动质量看板：care 派发 skip 原因 + 人工反馈（与 reactivation 同范式）
        self._care_skipped_recent: deque = deque(maxlen=500)    # (ts, reason)
        self._care_feedback_recent: deque = deque(maxlen=200)   # (ts, verdict)
        # ★ W2-D6.3：pacing 延迟分布观测（最近 200 次 _maybe_pacing_defer 决策的 delay_sec）
        self._pacing_delays_recent: deque = deque(maxlen=200)
        # ★ W2-D6.1：reactivation 24h 回复率归因（loop tick 末尾写）
        # {sent_24h, responded_24h, response_rate_pct, computed_at}
        self._reactivation_response_stats: Dict[str, Any] = {}
        # ★ W2-D6.2：dry_run feedback 计数
        self._reactivation_feedback_recent: deque = deque(maxlen=200)  # (ts, verdict, sample_ts)
        # ★ W2-D7.5：dislike 反向输入 — 内存 deque 存最近 20 条被否决的 reply_text
        # 故意不持久化：dislike 是主观判断，重启后让运营在新会话重新审核更健康
        self._reactivation_disliked_replies: deque = deque(maxlen=20)
        # ★ W3-D3.4：peer_typing prefetch 观测
        # (already_done: bool, wait_ms: float) — already_done True = 与 LLM 真并发完成，节省了等待
        self._peer_typing_prefetch_recent: deque = deque(maxlen=200)
        # ★ P3-C：_search_chat_by_name 指标
        # outcome: 'ok' | 'fail' | 'skip'（skip = 熔断短路，未真正搜索）
        self._search_chat_total: int = 0
        self._search_chat_ok: int = 0
        self._search_chat_fail: int = 0
        self._search_chat_skip: int = 0
        self._search_chat_recent: deque = deque(maxlen=500)  # (ts, outcome)
        # ★ P11 (2026-05-04)：messenger_rpa 通用计数器（self_overlap, cache_hit,
        # xml_fallback 等关键事件）。运营 dashboard 可看趋势防退化。
        # event name 例：
        #   self_overlap_promote / self_overlap_skip
        #   thread_title_cache_hit / pre_foreground_cache_hit
        #   xml_inbox_fallback / xml_inbox_supplement
        #   cycle_entry_thread_recovered / sticky_force_full_screen_reset
        #   inject_verify_emoji_normalized / send_failed_inject_mismatch
        self._messenger_rpa_metrics: Dict[str, int] = defaultdict(int)
        # P11.7 时间序列：(ts, name) 最近 2000 条 → 支持任意窗口聚合
        self._messenger_rpa_metrics_recent: deque = deque(maxlen=2000)
        # ★ 统一草稿引擎（generate_inbox_draft）规则栈生效观测：累计 + 时间序列。
        # event name 例：
        #   generated / empty
        #   memory_hit / emotional_active / companion_active
        #   slow_think / retry_applied / persona_guard_intercept / crisis_override
        # 暴露于 /api/drafts/autosend-status 的 draft_pipeline 段，让「规则是否真生效」可监控。
        self._inbox_draft_metrics: Dict[str, int] = defaultdict(int)
        self._inbox_draft_recent: deque = deque(maxlen=2000)  # (ts, name)
        # 草稿生成延迟（端到端 ms）：算 p50/p95，做延迟预算告警
        self._inbox_draft_latency_ms: deque = deque(maxlen=500)
        # ★ 防复读观测：字符层 vs 语义层触发占比 + 重写采纳率 + 嵌入缓存命中率。
        # 让运营量化「语义层值不值 / 换角度重生是否真降重 / 嵌入缓存省了多少调用」。
        self._ar_checks: int = 0            # 主判定次数（不含重试后的复评）
        self._ar_char_trig: int = 0         # 由字符 Jaccard 触发的复读
        self._ar_sem_trig: int = 0          # 由语义嵌入触发（字符没抓到）的复读
        self._ar_rewrite_attempt: int = 0   # 触发复读后发起换角度重生的次数
        self._ar_rewrite_adopted: int = 0   # 重生结果更优被采纳的次数
        self._embed_cache_hit: int = 0      # 防复读嵌入缓存命中（复用向量）
        self._embed_cache_miss: int = 0     # 未命中（真正发嵌入请求）

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

    def record_messenger_rpa_metric(self, name: str, count: int = 1) -> None:
        """P11: messenger_rpa 通用事件计数（self_overlap/cache_hit/xml_fallback…）。

        P11.7：累计计数 + 时间序列两路同时写入。
        """
        if not name:
            return
        c = int(max(0, count))
        if c <= 0:
            return
        now = time.time()
        with self._lock:
            self._messenger_rpa_metrics[name] += c
            for _ in range(c):
                self._messenger_rpa_metrics_recent.append((now, name))

    def get_messenger_rpa_metrics(
        self, window_sec: Optional[float] = None,
    ) -> Dict[str, int]:
        """P11: dashboard / Prometheus 拉取 messenger_rpa 通用计数快照。

        ``window_sec=None`` 返回累计；指定时返回最近 N 秒内的窗口聚合。
        """
        with self._lock:
            if not window_sec or window_sec <= 0:
                return dict(self._messenger_rpa_metrics)
            cutoff = time.time() - float(window_sec)
            agg: Dict[str, int] = defaultdict(int)
            for ts, name in self._messenger_rpa_metrics_recent:
                if ts >= cutoff:
                    agg[name] += 1
            return dict(agg)

    def record_inbox_draft_event(self, name: str, count: int = 1) -> None:
        """统一草稿引擎规则事件计数（累计 + 时间序列）。

        让运营在 dashboard 上看到「全自动/手动草稿到底有没有命中记忆/情感/陪伴/守卫」，
        防止规则被悄悄关掉或退化而无人察觉。
        """
        if not name:
            return
        c = int(max(0, count))
        if c <= 0:
            return
        now = time.time()
        with self._lock:
            self._inbox_draft_metrics[name] += c
            for _ in range(min(c, 50)):  # 时序仅记前 50，避免单次爆量挤掉历史
                self._inbox_draft_recent.append((now, name))

    def record_anti_repeat_check(self, layer: str = "none") -> None:
        """记录一次防复读主判定；layer ∈ {'none','char','semantic'}。

        只在 5b/8b 的**首判**调用（重试后的复评传 record=False，不计入），
        避免把「候选 + 重生候选」重复计数导致触发率虚高。
        """
        with self._lock:
            self._ar_checks += 1
            if layer == "char":
                self._ar_char_trig += 1
            elif layer == "semantic":
                self._ar_sem_trig += 1

    def record_anti_repeat_rewrite_attempt(self) -> None:
        """记录一次「换角度重生」尝试（进入重试即计一次）。"""
        with self._lock:
            self._ar_rewrite_attempt += 1

    def record_anti_repeat_rewrite_adopted(self) -> None:
        """记录一次重生结果更优被采纳（attempt 的子集）。"""
        with self._lock:
            self._ar_rewrite_adopted += 1

    def record_embed_cache(self, hits: int = 0, misses: int = 0) -> None:
        """记录防复读嵌入缓存一批的命中/未命中（稳态命中率应 ≈ N/(N+1)）。"""
        h, m = max(0, int(hits)), max(0, int(misses))
        if h == 0 and m == 0:
            return
        with self._lock:
            self._embed_cache_hit += h
            self._embed_cache_miss += m

    def record_inbox_draft_latency(self, ms: float) -> None:
        """记录单条草稿端到端生成延迟（ms）。"""
        try:
            v = float(ms)
        except (TypeError, ValueError):
            return
        if v < 0:
            return
        with self._lock:
            self._inbox_draft_latency_ms.append(v)

    def get_inbox_draft_metrics(self, window_sec: float = 3600.0) -> Dict[str, Any]:
        """统一草稿引擎规则栈快照：``total`` 累计 + ``window`` 最近窗口聚合 + 延迟分位。"""
        with self._lock:
            total = dict(self._inbox_draft_metrics)
            cutoff = time.time() - float(window_sec or 3600.0)
            window: Dict[str, int] = defaultdict(int)
            for ts, name in self._inbox_draft_recent:
                if ts >= cutoff:
                    window[name] += 1
            _lat = sorted(self._inbox_draft_latency_ms)
        latency: Dict[str, Any] = {"count": len(_lat)}
        if _lat:
            def _pct(p: float) -> int:
                idx = min(len(_lat) - 1, max(0, int(round(p * (len(_lat) - 1)))))
                return int(_lat[idx])
            latency.update({
                "p50_ms": _pct(0.50),
                "p95_ms": _pct(0.95),
                "max_ms": int(_lat[-1]),
                "avg_ms": int(sum(_lat) / len(_lat)),
            })
        out_total = dict(total)
        gen = int(out_total.get("generated", 0)) or 0
        # 便于一眼看比例：命中率（基于累计 generated）
        rates: Dict[str, float] = {}
        if gen > 0:
            # fast_path/empty 也纳入比率：前者是「低风险快路占比」（风险分类是否过宽的
            # 单一事实源，watchdog 用窗口率、本字段给累计率，校准脚本/仪表盘据此推阈值），
            # 后者是「空草稿率」（生成失败信号）。漏列会让下游读到 None 误判为 0。
            for _k in ("memory_hit", "emotional_active", "companion_active",
                       "slow_think", "retry_applied", "persona_guard_intercept",
                       "crisis_override", "fast_path", "empty"):
                rates[_k] = round(int(out_total.get(_k, 0)) / gen, 4)
        return {
            "total": out_total,
            "window": dict(window),
            "window_sec": int(window_sec or 3600.0),
            "rates_vs_generated": rates,
            "latency": latency,
        }

    def set_startup_advisory_counts(self, total: int, warning_events: int) -> None:
        """启动 collect_production_advisories 之后调用。"""
        with self._lock:
            self._startup_advisory_total = max(0, int(total))
            self._startup_advisory_warnings = max(0, int(warning_events))

    def set_startup_advisory_audit_logged(self, n: int) -> None:
        """Web 启用且 AuditStore 写入 warning 条数之后调用；n 为写入审计的条数。"""
        with self._lock:
            self._startup_advisory_audit_logged = max(0, int(n))

    def record_companion_safe_skip(self, reason: str = "") -> None:
        """陪护模式 safe_skip 计数（pre_send_gate / credit_low / ascii_guard / 其他）。"""
        with self._lock:
            self._companion_safe_skip_total += 1
            r = (reason or "").strip().split(":", 1)[0] or "unknown"
            self._companion_safe_skip_by_reason[r] = self._companion_safe_skip_by_reason.get(r, 0) + 1
            self._companion_safe_skip_recent.append((time.time(), r))

    def companion_safe_skip_rate_1h(self) -> float:
        """最近 1h safe_skip 速率（条/小时）；用于告警。"""
        with self._lock:
            cutoff = time.time() - 3600
            return float(sum(1 for ts, _ in self._companion_safe_skip_recent if ts >= cutoff))

    def set_deferred_queue_size(self, total: int, by_account: Optional[Dict[str, int]] = None) -> None:
        with self._lock:
            self._deferred_queue_total = max(0, int(total))
            if by_account is not None:
                self._deferred_queue_by_account = dict(by_account)

    def record_deferred_drain_sent(self) -> None:
        with self._lock:
            self._deferred_drain_sent_recent.append(time.time())

    def record_deferred_drain_failed(self, reason: str = "") -> None:
        with self._lock:
            self._deferred_drain_failed_recent.append((time.time(), (reason or "")[:60]))

    def record_reactivation_scheduled(self, contact_id: str = "") -> None:
        with self._lock:
            self._reactivation_scheduled_recent.append((time.time(), (contact_id or "")[:12]))

    def record_reactivation_failed(self, reason: str = "") -> None:
        with self._lock:
            self._reactivation_failed_recent.append((time.time(), (reason or "")[:60]))

    def record_reactivation_skipped(self, reason: str = "") -> None:
        with self._lock:
            self._reactivation_skipped_recent.append((time.time(), (reason or "")[:60]))

    def record_reactivation_dry_run(self, sample: Optional[Dict[str, Any]] = None) -> None:
        """W2-D5.1：dry_run 计数 + 可选保存样本（供运营审核）。"""
        with self._lock:
            self._reactivation_dry_run_recent.append(time.time())
            if sample:
                rec = dict(sample)
                rec["ts"] = time.time()
                rec["ts_iso"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(rec["ts"]),
                )
                self._reactivation_dry_samples.append(rec)

    def reactivation_dry_samples(
        self, limit: int = 50, *, before_ts: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """读最近 N 条 dry_run 生成话术（最新在前）。

        ★ W3-D3.5：``before_ts`` 用于增量加载 — 只返 ts < before_ts 的，
        前端"加载更多"传上次最早一条的 ts，避免每次重拉前 N 条。
        """
        with self._lock:
            samples = list(self._reactivation_dry_samples)
        ordered = list(reversed(samples))  # 最新在前
        if before_ts is not None:
            try:
                cut = float(before_ts)
                ordered = [s for s in ordered if float(s.get("ts") or 0) < cut]
            except Exception:
                pass
        return ordered[:max(1, int(limit))]

    def record_care_dry_run(self, sample: Optional[Dict[str, Any]] = None) -> None:
        """Phase O 质量闭环：care dispatcher dry_run 样本（供运营审核）。"""
        if not sample:
            return
        with self._lock:
            rec = dict(sample)
            rec["ts"] = time.time()
            rec["ts_iso"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(rec["ts"]),
            )
            self._care_dry_samples.append(rec)

    def care_dry_samples(
        self, limit: int = 50, *, before_ts: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """读最近 N 条 care dry_run 话术（最新在前，支持 before_ts 增量加载）。"""
        with self._lock:
            samples = list(self._care_dry_samples)
        ordered = list(reversed(samples))
        if before_ts is not None:
            try:
                cut = float(before_ts)
                ordered = [s for s in ordered if float(s.get("ts") or 0) < cut]
            except Exception:
                pass
        return ordered[:max(1, int(limit))]

    def record_care_skipped(self, reason: str = "") -> None:
        """O·P：care 派发 skip 原因（no_context/already_discussed/identity_leak/…）。"""
        with self._lock:
            self._care_skipped_recent.append((time.time(), (reason or "")[:60]))

    def record_care_feedback(self, verdict: str = "") -> None:
        """O·P：care dry_run 样本的人工 like/dislike 计数。"""
        v = (verdict or "").strip().lower()
        if v not in ("like", "dislike"):
            return
        with self._lock:
            self._care_feedback_recent.append((time.time(), v))

    @staticmethod
    def _reason_hist(items, since: float) -> Dict[str, int]:
        """把 (ts, reason) 序列在 since 之后的部分按 reason 计数。"""
        hist: Dict[str, int] = {}
        for ts, reason in items:
            if ts >= since:
                key = reason or "(none)"
                hist[key] = hist.get(key, 0) + 1
        return hist

    def companion_quality_overview(self, *, window_sec: float = 86400) -> Dict[str, Any]:
        """O·P 联动质量看板：care + reactivation 的发送质量统一视图。

        含两条主动线的 skip 原因分布 + 人工 like/dislike 反馈 + dry_run 计数，
        以及共享 dislike 黑名单规模。供运营一眼看「质量在变好还是变差、卡在哪」。
        """
        now = time.time()
        since = now - max(60.0, float(window_sec))
        with self._lock:
            re_skip = list(self._reactivation_skipped_recent)
            re_fb = list(self._reactivation_feedback_recent)
            re_sched = list(self._reactivation_scheduled_recent)
            re_dry = list(self._reactivation_dry_run_recent)
            ca_skip = list(self._care_skipped_recent)
            ca_fb = list(self._care_feedback_recent)
            ca_dry = list(self._care_dry_samples)
            blacklist_n = len(self._reactivation_disliked_replies)

        def _fb_counts(items_with_verdict):
            like = sum(1 for t, v in items_with_verdict if t >= since and v == "like")
            dislike = sum(1 for t, v in items_with_verdict if t >= since and v == "dislike")
            total = like + dislike
            return {
                "like": like, "dislike": dislike,
                "like_rate_pct": round(like / total * 100, 1) if total else None,
            }

        re_fb2 = [(t, v) for t, v, _ in re_fb]
        return {
            "window_sec": int(window_sec),
            "reactivation": {
                "scheduled": sum(1 for t, _ in re_sched if t >= since),
                "skipped": sum(1 for t, _ in re_skip if t >= since),
                "skip_reasons": self._reason_hist(re_skip, since),
                "dry_run": sum(1 for t in re_dry if t >= since),
                "feedback": _fb_counts(re_fb2),
            },
            "care": {
                "skipped": sum(1 for t, _ in ca_skip if t >= since),
                "skip_reasons": self._reason_hist(ca_skip, since),
                "dry_run": sum(1 for s in ca_dry if float(s.get("ts") or 0) >= since),
                "feedback": _fb_counts(ca_fb),
            },
            "disliked_blacklist_size": blacklist_n,
        }

    @staticmethod
    def _bucket_counts(items, *, n_buckets: int = 12, bucket_sec: int = 300) -> List[int]:
        """W2-D6.5：把 ts 序列分成 N 个 bucket_sec 秒的桶，最近一桶在最右。"""
        now = time.time()
        cutoff = now - n_buckets * bucket_sec
        buckets = [0] * n_buckets
        for item in items:
            ts = item if isinstance(item, (int, float)) else (item[0] if item else 0)
            if not ts or ts < cutoff or ts > now:
                continue
            idx = int((ts - cutoff) / bucket_sec)
            if 0 <= idx < n_buckets:
                buckets[idx] += 1
        return buckets

    # ★ W2-D6.3：pacing 延迟分布
    def record_pacing_delay(self, delay_sec: float) -> None:
        try:
            d = max(0.0, float(delay_sec))
        except Exception:
            return
        with self._lock:
            self._pacing_delays_recent.append(d)

    def _pacing_delay_stats(self) -> Dict[str, Any]:
        with self._lock:
            arr = list(self._pacing_delays_recent)
        if not arr:
            return {
                "count": 0, "avg_sec": 0.0, "p50_sec": 0.0,
                "p95_sec": 0.0, "max_sec": 0.0,
                "buckets": {"<5s": 0, "5-15s": 0, "15-30s": 0, "30-60s": 0, "60s+": 0},
            }
        arr.sort()
        n = len(arr)
        avg = sum(arr) / n
        p50 = arr[int(n * 0.5)]
        p95 = arr[min(n - 1, int(n * 0.95))]
        # ★ W2-D7.3：分布桶（暴露双峰）
        buckets = {"<5s": 0, "5-15s": 0, "15-30s": 0, "30-60s": 0, "60s+": 0}
        for d in arr:
            if d < 5:
                buckets["<5s"] += 1
            elif d < 15:
                buckets["5-15s"] += 1
            elif d < 30:
                buckets["15-30s"] += 1
            elif d < 60:
                buckets["30-60s"] += 1
            else:
                buckets["60s+"] += 1
        return {
            "count": n,
            "avg_sec": round(avg, 1),
            "p50_sec": round(p50, 1),
            "p95_sec": round(p95, 1),
            "max_sec": round(arr[-1], 1),
            "buckets": buckets,
        }

    # ★ W2-D6.1：reactivation 回复率（reactivation_loop tick 末尾计算后写入）
    def set_reactivation_response_stats(self, stats: Dict[str, Any]) -> None:
        with self._lock:
            self._reactivation_response_stats = dict(stats)

    # ★ W2-D6.2：dry_run feedback
    def record_reactivation_feedback(self, verdict: str, sample_ts: float = 0.0) -> None:
        with self._lock:
            self._reactivation_feedback_recent.append(
                (time.time(), (verdict or "")[:20], float(sample_ts or 0)),
            )

    # ★ W2-D7.5：dislike → 黑名单
    def add_disliked_reply(self, reply_text: str) -> None:
        text = (reply_text or "").strip()
        if not text:
            return
        with self._lock:
            # 避免重复：同样的 reply 只保留一份
            if text not in self._reactivation_disliked_replies:
                self._reactivation_disliked_replies.append(text)

    # ★ P3-C：_search_chat_by_name 指标
    def record_search_chat(self, *, ok: bool = False, skipped: bool = False) -> None:
        """记录一次 _search_chat_by_name 结果。
        skipped=True 表示熔断器短路（计入 skip，不计 ok/fail）。
        """
        outcome = "skip" if skipped else ("ok" if ok else "fail")
        with self._lock:
            self._search_chat_total += 1
            if skipped:
                self._search_chat_skip += 1
            elif ok:
                self._search_chat_ok += 1
            else:
                self._search_chat_fail += 1
            self._search_chat_recent.append((time.time(), outcome))

    # ★ W3-D3.4：peer_typing prefetch 观测
    def record_peer_typing_prefetch(self, already_done: bool, wait_ms: float) -> None:
        with self._lock:
            self._peer_typing_prefetch_recent.append(
                (bool(already_done), max(0.0, float(wait_ms))),
            )

    def _peer_typing_prefetch_stats(self) -> Dict[str, Any]:
        with self._lock:
            arr = list(self._peer_typing_prefetch_recent)
        if not arr:
            return {"count": 0, "concurrent_pct": 0.0, "avg_wait_ms": 0.0}
        n = len(arr)
        n_done = sum(1 for done, _ in arr if done)
        avg_wait = sum(w for _, w in arr) / n
        return {
            "count": n,
            "concurrent_pct": round(n_done / n * 100.0, 1),
            "avg_wait_ms": round(avg_wait, 1),
        }

    def is_similar_to_disliked(self, reply_text: str,
                               threshold: float = 0.7) -> Tuple[bool, str]:
        """W2-D7.5：检查 reply 是否和最近 dislike 的 reply 相似。

        返回 (是否相似, 最相似的那条 dislike reply)。
        threshold 默认 0.7（SequenceMatcher.ratio() 0..1，0.7 已经很像）。
        """
        text = (reply_text or "").strip()
        if not text:
            return (False, "")
        with self._lock:
            disliked = list(self._reactivation_disliked_replies)
        if not disliked:
            return (False, "")
        from difflib import SequenceMatcher
        best_ratio = 0.0
        best_match = ""
        for d in disliked:
            r = SequenceMatcher(None, text, d).ratio()
            if r > best_ratio:
                best_ratio = r
                best_match = d
        return (best_ratio >= threshold, best_match if best_ratio >= threshold else "")

    def set_reactivation_run(self, candidates_seen: int) -> None:
        with self._lock:
            self._reactivation_last_run_ts = time.time()
            self._reactivation_last_candidates = max(0, int(candidates_seen))

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
            css_total = self._companion_safe_skip_total
            css_by_reason = dict(self._companion_safe_skip_by_reason)
            css_recent = list(self._companion_safe_skip_recent)
            dq_total = self._deferred_queue_total
            dq_by_acc = dict(self._deferred_queue_by_account)
            dq_sent_recent = list(self._deferred_drain_sent_recent)
            dq_fail_recent = list(self._deferred_drain_failed_recent)
            re_sched = list(self._reactivation_scheduled_recent)
            re_fail = list(self._reactivation_failed_recent)
            re_skip = list(self._reactivation_skipped_recent)
            re_dry = list(self._reactivation_dry_run_recent)
            re_last_ts = self._reactivation_last_run_ts
            re_last_cands = self._reactivation_last_candidates
            ar_checks = self._ar_checks
            ar_char = self._ar_char_trig
            ar_sem = self._ar_sem_trig
            ar_rw_att = self._ar_rewrite_attempt
            ar_rw_ad = self._ar_rewrite_adopted
            ec_hit = self._embed_cache_hit
            ec_miss = self._embed_cache_miss
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
            "companion_safe_skip": {
                "total": css_total,
                "by_reason": css_by_reason,
                "rate_1h": float(sum(1 for ts, _ in css_recent if ts >= time.time() - 3600)),
                # W2-D6.5：1h × 12 桶（每 5min 一个）的 sparkline
                "sparkline_1h": self._bucket_counts(css_recent),
            },
            "deferred_queue": {
                "total": dq_total,
                "by_account": dq_by_acc,
                "drained_1h": float(sum(1 for ts in dq_sent_recent if ts >= time.time() - 3600)),
                "failed_1h": float(sum(1 for ts, _ in dq_fail_recent if ts >= time.time() - 3600)),
                "drained_sparkline_1h": self._bucket_counts(dq_sent_recent),
            },
            "reactivation": {
                "scheduled_1h": float(sum(1 for ts, _ in re_sched if ts >= time.time() - 3600)),
                "scheduled_24h": float(sum(1 for ts, _ in re_sched if ts >= time.time() - 86400)),
                "failed_1h": float(sum(1 for ts, _ in re_fail if ts >= time.time() - 3600)),
                "skipped_1h": float(sum(1 for ts, _ in re_skip if ts >= time.time() - 3600)),
                "dry_run_1h": float(sum(1 for ts in re_dry if ts >= time.time() - 3600)),
                "last_run_ts": re_last_ts,
                "last_run_iso": (
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(re_last_ts))
                    if re_last_ts else None
                ),
                "last_candidates": re_last_cands,
                "response_stats": dict(self._reactivation_response_stats),
                "feedback_1h": {
                    "like": sum(1 for ts, v, _ in self._reactivation_feedback_recent
                                if ts >= time.time() - 3600 and v == "like"),
                    "dislike": sum(1 for ts, v, _ in self._reactivation_feedback_recent
                                   if ts >= time.time() - 3600 and v == "dislike"),
                },
                "sparkline_1h": self._bucket_counts(re_sched),
            },
            # ★ P3-C：messenger_rpa search_chat 指标
            "messenger_rpa_search": {
                "total": self._search_chat_total,
                "ok": self._search_chat_ok,
                "fail": self._search_chat_fail,
                "skip": self._search_chat_skip,
                "success_rate_pct": round(
                    self._search_chat_ok / max(1, self._search_chat_ok + self._search_chat_fail) * 100, 1
                ),
                "ok_1h": sum(
                    1 for ts, o in list(self._search_chat_recent) if o == "ok" and ts >= time.time() - 3600
                ),
                "fail_1h": sum(
                    1 for ts, o in list(self._search_chat_recent) if o == "fail" and ts >= time.time() - 3600
                ),
                "skip_1h": sum(
                    1 for ts, o in list(self._search_chat_recent) if o == "skip" and ts >= time.time() - 3600
                ),
                "sparkline_1h": self._bucket_counts(
                    [(ts, o) for ts, o in list(self._search_chat_recent) if o != "skip"]
                ),
            },
            # ★ 防复读观测：触发分层 + 重写采纳 + 嵌入缓存命中
            "anti_repeat": {
                "checks": ar_checks,
                "char_triggered": ar_char,
                "semantic_triggered": ar_sem,
                "trigger_rate_pct": round((ar_char + ar_sem) / ar_checks * 100, 1) if ar_checks else 0.0,
                "semantic_share_pct": round(ar_sem / (ar_char + ar_sem) * 100, 1) if (ar_char + ar_sem) else 0.0,
                "rewrite_attempted": ar_rw_att,
                "rewrite_adopted": ar_rw_ad,
                "rewrite_adopt_rate_pct": round(ar_rw_ad / ar_rw_att * 100, 1) if ar_rw_att else 0.0,
                "embed_cache": {
                    "hit": ec_hit,
                    "miss": ec_miss,
                    "hit_rate_pct": round(ec_hit / (ec_hit + ec_miss) * 100, 1) if (ec_hit + ec_miss) else 0.0,
                },
            },
            # ★ W2-D6.3：pacing 延迟分布
            "pacing": self._pacing_delay_stats(),
            # ★ W3-D3.4：peer_typing prefetch 观测
            "peer_typing_prefetch": self._peer_typing_prefetch_stats(),
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
