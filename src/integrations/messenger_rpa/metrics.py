"""Messenger RPA 进程级指标（Prometheus histogram / counter）。

单例模式，无新增依赖（自己实现 histogram bucket 聚合）。
runner 每次 run 结束调 observe_run(result)，/api/messenger-rpa/metrics 读 dump()。

暴露以下指标：
  - messenger_rpa_run_duration_seconds_bucket{le=...}     histogram
  - messenger_rpa_run_duration_seconds_count/sum
  - messenger_rpa_phase_duration_seconds_bucket{phase=inbox_vision|thread_vision|llm}
  - messenger_rpa_runs_total{outcome=ok|error|risk_blocked|no_peer}  counter
  - messenger_rpa_caption_outcome_total{source=prefetch|sync|timeout|error}  counter
  - messenger_rpa_guard_skips_total{name=...}  counter (P1-E1)
      记录各层安全守卫触发计数：
      step 维度: inbox_self_sent_skip, thread_self_skip_hard_gap,
                 self_message_skip, reply_cooldown_skip, runaway_paused,
                 sticky_idle, sticky_gate_skip, ...
      hint 维度: thread_xml_bubble_guard:self, runaway_circuit_tripped:*,
                 self_media_xml_guard, ...
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

# 秒 bucket — 适配 RPA 场景（通常 2~10s）
_RUN_BUCKETS: Tuple[float, ...] = (
    0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 45.0,
)
_PHASE_BUCKETS: Tuple[float, ...] = (
    0.1, 0.3, 0.5, 1.0, 2.0, 3.5, 5.0, 8.0, 15.0,
)


# ── P1-E1: 守卫 reason metrics 白名单 ──
# step 维度（result["step"]）— 只累加这些已知的安全相关 step，避免维度爆炸
_GUARD_TRACKED_STEPS: frozenset = frozenset({
    # P0-A inbox 阶段守卫
    "inbox_self_sent_skip",
    "inbox_self_sent_hard_skip",  # 兼容历史 label
    # P0-B thread 内时间窗硬守卫
    "thread_self_skip_hard_gap",
    # vision 误识 self → peer
    "self_message_skip",
    # P1-C / 旧 thread XML 守卫
    "self_latest_xml_prevision_skip",
    # cooldown / dedup
    "reply_cooldown_skip",
    "duplicate_skip",
    "all_unread_skipped",
    # send gate
    "send_gate_skip",
    "sticky_gate_skip",
    "send_failed",
    # runaway 熔断
    "runaway_paused",
    # sticky 路径
    "sticky_idle",
    "media_ack_duplicate_skip",
    # escalation
    "escalation_new",
    "escalation_cooldown",
    # vision 误进非粘性 chat
    "wrong_chat_misroute_skip",
    "vision_repeated_previous_peer_after_recent_self_reply",
})

# hint 维度：精确匹配
_GUARD_TRACKED_HINTS_EXACT: frozenset = frozenset({
    "thread_xml_bubble_guard:self",
    "self_media_xml_guard",
    "self_media_xml_guard_ignored_natural_peer",
    "thread_self_xml_guard:no_xml",
    "thread_self_xml_guard:no_snippet",
    "thread_self_xml_guard:not_thread_but_checking",
    "thread_self_xml_guard:error",
    "promoted_extra_peer_after_self_overlap",
    "current_thread_fast_path",
    # P16 反空转守卫
    "skipped_peer_text_short_circuit",      # D 层：内容指纹/相似度短路
    "chat_overlap_skip_cooldown",            # C 层：thread 内冷却覆盖期
    "chat_overlap_inbox_skip",               # IL 层：inbox 阶段提前跳过
    "bubble_self_confirms_overlap",          # B 层：bubble + overlap 双确认
    # P17 thread_combined 截屏 hash 缓存命中
    "thread_combined_cache_hit",
})

# hint 维度：前缀匹配（截断到前缀本身作 metrics key，避免维度爆炸）
_GUARD_TRACKED_HINT_PREFIXES: Tuple[str, ...] = (
    "runaway_circuit_tripped:",
    "runaway_hard_ceiling:",
    "vision_misroute_guard:",
    "row_resolve_ignored:",
    "current_thread_seen:",
    "current_thread_exit_to_inbox",
    "lang_mix_filter:",
    "skipped_chat_blacklist",
    # P16-C：长冷却 hint 形如 chat_overlap_long_cooldown:600s:streak=3
    "chat_overlap_long_cooldown:",
    # P16-D2：短路时携带 ratio 数值
    "self_overlap_strict_skip:",
    # P16-IL2：长冷却内 inbox preview 不同 → 解除冷却放行（带相似度数值）
    "chat_overlap_inbox_escape:",
)


class _Histogram:
    __slots__ = ("buckets", "counts", "sum", "count")

    def __init__(self, buckets: Tuple[float, ...]) -> None:
        self.buckets = buckets
        self.counts = [0] * (len(buckets) + 1)  # +1 for +Inf
        self.sum = 0.0
        self.count = 0

    def observe(self, value: float) -> None:
        self.sum += float(value)
        self.count += 1
        placed = False
        for i, b in enumerate(self.buckets):
            if value <= b:
                self.counts[i] += 1
                placed = True
                break
        if not placed:
            self.counts[-1] += 1

    def dump(self) -> Dict[str, Any]:
        # 累积 bucket（Prometheus convention — le=0.5 包含所有 <=0.5 的样本）
        cum: List[int] = []
        running = 0
        for c in self.counts:
            running += c
            cum.append(running)
        return {
            "buckets": list(self.buckets),
            "counts": list(self.counts),  # 非累积
            "cum_counts": cum,             # 累积（Prometheus 用）
            "sum": self.sum,
            "count": self.count,
        }


class MessengerRpaMetrics:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._run_hist = _Histogram(_RUN_BUCKETS)
        self._phase_hists: Dict[str, _Histogram] = {
            "inbox_vision": _Histogram(_PHASE_BUCKETS),
            "thread_vision": _Histogram(_PHASE_BUCKETS),
            "llm": _Histogram(_PHASE_BUCKETS),
        }
        # runs_total counter
        self._run_outcomes: Dict[str, int] = {
            "ok": 0, "error": 0, "risk_blocked": 0,
            "no_peer": 0, "duplicate": 0, "skipped": 0,
        }
        # caption source counter
        self._caption_sources: Dict[str, int] = {
            "prefetch": 0, "sync": 0, "timeout": 0, "error": 0,
        }
        # send success counter
        self._sends_total = 0
        # handoff 引流计数器
        self._handoff_injected_total = 0
        self._handoff_sent_total = 0
        self._handoff_by_script: Dict[str, int] = {}   # script_id 维度
        self._handoff_skipped: Dict[str, int] = {}     # skipped reason 分布
        # P1-E1: 守卫触发计数（reason 维度）
        # key 由白名单（_GUARD_TRACKED_STEPS / hints）筛选，避免维度爆炸
        self._guard_skips: Dict[str, int] = {}

    def observe_run(self, result: Dict[str, Any]) -> None:
        """每次 runner.run_once 结束调一次。"""
        with self._lock:
            try:
                total_ms = float(result.get("total_ms", 0) or 0)
                if total_ms > 0:
                    self._run_hist.observe(total_ms / 1000.0)
                phase = result.get("phase_ms") or {}
                for name, ms in phase.items():
                    h = self._phase_hists.get(name)
                    if h is not None and ms:
                        try:
                            h.observe(float(ms) / 1000.0)
                        except (TypeError, ValueError):
                            pass
                # 归类 outcome（互斥优先级）
                step = str(result.get("step") or "")
                err = str(result.get("error") or "")
                if "risk_blocked" in step:
                    self._run_outcomes["risk_blocked"] += 1
                elif result.get("ok"):
                    self._run_outcomes["ok"] += 1
                    if result.get("reply_text"):
                        # 有 reply_text 且 ok 且不是 ack 代表发送成功
                        self._sends_total += 1
                elif "duplicate" in step:
                    self._run_outcomes["duplicate"] += 1
                elif "no_peer" in step:
                    self._run_outcomes["no_peer"] += 1
                elif "skipped" in step or "approval" in step:
                    self._run_outcomes["skipped"] += 1
                elif err:
                    self._run_outcomes["error"] += 1
                else:
                    self._run_outcomes["skipped"] += 1
                # caption source
                cs = str(result.get("caption_source") or "")
                if cs in self._caption_sources:
                    self._caption_sources[cs] += 1
                # handoff 引流
                if result.get("handoff_injected"):
                    self._handoff_injected_total += 1
                    sid = str(result.get("handoff_script_id") or "_unknown")
                    self._handoff_by_script[sid] = (
                        self._handoff_by_script.get(sid, 0) + 1
                    )
                sk = str(result.get("handoff_skipped") or "")
                if sk:
                    self._handoff_skipped[sk] = (
                        self._handoff_skipped.get(sk, 0) + 1
                    )
                # handoff 已发送（有 token 且发送成功）
                if result.get("handoff_token") and result.get("step") == "sent":
                    self._handoff_sent_total += 1
                # ── P1-E1: 守卫触发计数 ──
                # step 维度（每次 run 仅一个 step，但白名单内才计）
                if step and step in _GUARD_TRACKED_STEPS:
                    self._guard_skips[step] = (
                        self._guard_skips.get(step, 0) + 1
                    )
                # hints 维度（list，每个独立计数，仅白名单或前缀匹配的）
                hints = result.get("hints") or []
                if isinstance(hints, list):
                    for h in hints:
                        if not isinstance(h, str):
                            continue
                        if h in _GUARD_TRACKED_HINTS_EXACT:
                            self._guard_skips[h] = (
                                self._guard_skips.get(h, 0) + 1
                            )
                            continue
                        for pfx in _GUARD_TRACKED_HINT_PREFIXES:
                            if h.startswith(pfx):
                                # 用前缀（去除变量部分）作 metrics key
                                key = pfx.rstrip(":") if pfx.endswith(":") else pfx
                                self._guard_skips[key] = (
                                    self._guard_skips.get(key, 0) + 1
                                )
                                break
            except Exception:
                pass  # metrics 不能反噬主流程

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "run_duration": self._run_hist.dump(),
                "phase_duration": {
                    name: h.dump() for name, h in self._phase_hists.items()
                },
                "run_outcomes": dict(self._run_outcomes),
                "caption_sources": dict(self._caption_sources),
                "sends_total": self._sends_total,
                "handoff_injected_total": self._handoff_injected_total,
                "handoff_sent_total": self._handoff_sent_total,
                "handoff_by_script": dict(self._handoff_by_script),
                "handoff_skipped": dict(self._handoff_skipped),
                "guard_skips": dict(self._guard_skips),  # P1-E1
            }

    def reset(self) -> None:
        """测试用；生产不建议调。"""
        with self._lock:
            self._run_hist = _Histogram(_RUN_BUCKETS)
            self._phase_hists = {
                name: _Histogram(_PHASE_BUCKETS)
                for name in self._phase_hists.keys()
            }
            for k in self._run_outcomes:
                self._run_outcomes[k] = 0
            for k in self._caption_sources:
                self._caption_sources[k] = 0
            self._sends_total = 0
            self._handoff_injected_total = 0
            self._handoff_sent_total = 0
            self._handoff_by_script = {}
            self._handoff_skipped = {}
            self._guard_skips = {}


# 进程级单例
_SINGLETON: Optional[MessengerRpaMetrics] = None
_SINGLETON_LOCK = threading.Lock()


def get_metrics() -> MessengerRpaMetrics:
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = MessengerRpaMetrics()
    return _SINGLETON
