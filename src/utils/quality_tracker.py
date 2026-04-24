"""AI 对话质量监控 — token 用量、异常回复检测、满意度代理指标"""

import collections
import logging
import re
import time
from typing import Dict, List, Optional

logger = logging.getLogger("QualityTracker")


class QualityTracker:

    def __init__(self, config: dict = None):
        cfg = (config or {}).get("ai_quality", {})
        self._enabled = cfg.get("enabled", True)
        self._min_reply_len = int(cfg.get("min_reply_length", 5))
        self._max_reply_len = int(cfg.get("max_reply_length", 2000))
        self._repeat_window = int(cfg.get("repeat_window", 10))

        self._calls: collections.deque = collections.deque(maxlen=1000)
        self._anomalies: collections.deque = collections.deque(maxlen=200)
        self._recent_replies: collections.deque = collections.deque(maxlen=self._repeat_window)

        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._total_calls = 0
        self._total_anomalies = 0

    def record_call(self, prompt_tokens: int = 0, completion_tokens: int = 0,
                    elapsed_ms: int = 0, reply: str = "", request_id: str = ""):
        if not self._enabled:
            return
        now = time.time()
        self._total_calls += 1
        self._total_prompt_tokens += prompt_tokens
        self._total_completion_tokens += completion_tokens

        entry = {
            "ts": now,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "elapsed_ms": elapsed_ms,
            "reply_len": len(reply),
            "request_id": request_id,
        }
        self._calls.append(entry)

        anomalies = self._detect_anomalies(reply, elapsed_ms)
        if anomalies:
            self._total_anomalies += 1
            for a in anomalies:
                self._anomalies.append({
                    "ts": now, "type": a, "request_id": request_id,
                    "reply_preview": reply[:80],
                })
            logger.warning("[质量] 异常回复 (%s): %s...", ", ".join(anomalies), reply[:60])

        self._recent_replies.append(reply.strip()[:200])

    def _detect_anomalies(self, reply: str, elapsed_ms: int) -> List[str]:
        issues = []
        r = reply.strip()
        if len(r) < self._min_reply_len:
            issues.append("too_short")
        if len(r) > self._max_reply_len:
            issues.append("too_long")
        if r and r in self._recent_replies:
            issues.append("repeated")
        if r and re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]{3,}", r):
            issues.append("garbled")
        if elapsed_ms > 30000:
            issues.append("slow_response")
        if r and re.search(r"(作为|我是)(一个)?(AI|人工智能|语言模型)", r):
            issues.append("identity_leak")
        return issues

    def get_summary(self) -> Dict:
        calls = list(self._calls)
        if not calls:
            return {"total_calls": 0}
        recent_ms = [c["elapsed_ms"] for c in calls if c["elapsed_ms"] > 0]
        avg_ms = round(sum(recent_ms) / len(recent_ms)) if recent_ms else 0
        recent_tokens = [c["total_tokens"] for c in calls[-100:]]
        avg_tokens = round(sum(recent_tokens) / len(recent_tokens)) if recent_tokens else 0
        return {
            "total_calls": self._total_calls,
            "total_prompt_tokens": self._total_prompt_tokens,
            "total_completion_tokens": self._total_completion_tokens,
            "total_tokens": self._total_prompt_tokens + self._total_completion_tokens,
            "avg_response_ms": avg_ms,
            "avg_tokens_per_call": avg_tokens,
            "total_anomalies": self._total_anomalies,
            "anomaly_rate": round(self._total_anomalies / max(self._total_calls, 1) * 100, 1),
        }

    def get_recent_anomalies(self, limit: int = 20) -> List[Dict]:
        items = list(self._anomalies)
        result = items[-limit:]
        for r in result:
            r["ts_str"] = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
        return result

    def get_token_trend(self, last_n: int = 50) -> List[Dict]:
        calls = list(self._calls)[-last_n:]
        return [{"ts": time.strftime("%H:%M", time.localtime(c["ts"])),
                 "tokens": c["total_tokens"], "ms": c["elapsed_ms"]}
                for c in calls]
