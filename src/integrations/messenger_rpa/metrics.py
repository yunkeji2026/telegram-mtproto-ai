"""Messenger RPA 进程级指标（Prometheus histogram / counter）。

单例模式，无新增依赖（自己实现 histogram bucket 聚合）。
runner 每次 run 结束调 observe_run(result)，/api/messenger-rpa/metrics 读 dump()。

暴露以下指标：
  - messenger_rpa_run_duration_seconds_bucket{le=...}     histogram
  - messenger_rpa_run_duration_seconds_count/sum
  - messenger_rpa_phase_duration_seconds_bucket{phase=inbox_vision|thread_vision|llm}
  - messenger_rpa_runs_total{outcome=ok|error|risk_blocked|no_peer}  counter
  - messenger_rpa_caption_outcome_total{source=prefetch|sync|timeout|error}  counter
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
