"""HandoffReadinessScorer — 判断"现在该不该发 LINE 引流话术"。

MVP 3 信号：
  - turn_count      对方消息数（封顶 3 起 sat）
  - intimacy_score  从 IntimacyEngine 读的 0-100 分
  - goodbye_context 最近一条对方消息是告别语吗？

window_open 规则（最重要）：
  - score ≥ 70 **AND** 当前上下文是告别（goodbye_context=True）→ 开窗触发引流
  - 其他情况：即使 score=100 也不开窗，等出现告别场景再说

这样保证：关系到位 + 告别场景 + 自然时机三者齐备才引流。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from src.skills.intimacy_engine import IntimacyEngine

logger = logging.getLogger(__name__)


# ── 告别关键词（多语言，MVP 常见 20+） ───────────────────
_GOODBYE_KEYWORDS = [
    # 中文
    "晚安", "我去睡", "先睡", "改天聊", "明天聊", "明天再聊", "回头见",
    "今天先这样", "先下了", "先溜了", "不聊了", "有事", "下线",
    # English
    "good night", "goodnight", "gotta go", "gtg", "bye", "byebye",
    "talk later", "ttyl", "see you", "catch you later", "gn ",
    # 日本語
    "おやすみ", "またね", "また明日", "バイバイ",
    # 韓国語
    "잘자", "안녕",
]

# 归一化后快速匹配：统一小写，去首尾空格
_GOODBYE_SET = [k.strip().lower() for k in _GOODBYE_KEYWORDS if k.strip()]


# 权重（sum = 1.0）
_W_TURNS = 0.35
_W_INTIMACY = 0.50
_W_GOODBYE = 0.15

# 默认阈值 / 参数
_DEFAULT_TURN_SAT = 3        # 3 条 msg_in 即满
_DEFAULT_OPEN_THRESHOLD = 70.0
_INTIMACY_CACHE_TTL_S = 300  # 5 分钟内不重算 intimacy（复用 Journey 缓存值）


@dataclass
class ReadinessDecision:
    score: float                     # 0-100
    window_open: bool
    contributions: Dict[str, float]  # 加权贡献
    reasons: Dict[str, object]       # 诊断信号：turn_count / intimacy / goodbye_hit
    threshold: float

    def to_dict(self):
        return {
            "score": self.score,
            "window_open": self.window_open,
            "contributions": self.contributions,
            "reasons": self.reasons,
            "threshold": self.threshold,
        }


def is_goodbye_text(text: str) -> bool:
    """判断文本是否属于告别语境。简单关键词命中。"""
    if not text:
        return False
    t = text.strip().lower()
    if not t:
        return False
    for kw in _GOODBYE_SET:
        if kw in t:
            return True
    return False


class HandoffReadinessScorer:
    def __init__(
        self,
        store,
        intimacy_engine: IntimacyEngine,
        *,
        turn_saturation: int = _DEFAULT_TURN_SAT,
        open_threshold: float = _DEFAULT_OPEN_THRESHOLD,
    ) -> None:
        self._store = store
        self._intimacy = intimacy_engine
        self._turn_sat = max(1, int(turn_saturation))
        self._threshold = float(open_threshold)

    def evaluate(
        self,
        journey_id: str,
        *,
        latest_in_text: str = "",
    ) -> ReadinessDecision:
        # 1. turns（从 journey_events 读 msg_in）
        events = self._store.list_events(journey_id, limit=500)
        turn_in = sum(1 for e in events if e["event_type"] == "msg_in")
        s_turns = min(turn_in, self._turn_sat) / self._turn_sat

        # 2. intimacy：优先用 Journey 缓存值（5 分钟内新鲜），否则重算。
        # 好处：readiness 每轮触发时不再每次读 500 条事件算一遍。
        now_s = int(time.time())
        j = self._store.get_journey(journey_id)
        if (j and j.intimacy_updated_at
                and (now_s - j.intimacy_updated_at) < _INTIMACY_CACHE_TTL_S):
            intimacy_score = j.intimacy_score
        else:
            intimacy_bd = self._intimacy.compute_intimacy(journey_id)
            intimacy_score = intimacy_bd.score
        s_intimacy = intimacy_score / 100.0

        # 3. goodbye
        goodbye_hit = is_goodbye_text(latest_in_text)
        s_goodbye = 1.0 if goodbye_hit else 0.0

        contribs = {
            "turn_count":  round(_W_TURNS * s_turns, 3),
            "intimacy":    round(_W_INTIMACY * s_intimacy, 3),
            "goodbye":     round(_W_GOODBYE * s_goodbye, 3),
        }
        score_0_1 = sum(contribs.values())
        score_0_1 = max(0.0, min(1.0, score_0_1))
        score = round(score_0_1 * 100, 1)

        window_open = (score >= self._threshold) and goodbye_hit

        return ReadinessDecision(
            score=score,
            window_open=window_open,
            contributions=contribs,
            reasons={
                "turn_count": turn_in,
                "intimacy_score": intimacy_score,
                "goodbye_hit": goodbye_hit,
            },
            threshold=self._threshold,
        )
