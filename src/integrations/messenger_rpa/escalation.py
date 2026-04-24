"""Messenger RPA 人工转接（escalation）触发器。

设计原则：
- 纯函数，容易单元测试
- 只判定是否触发 + 给出理由；动作由 runner 执行
- 触发器可扩展（KeyWord / MoneyPattern / RepeatedTopic / NegativeSentiment）
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence


@dataclass(frozen=True)
class EscalationDecision:
    should_escalate: bool
    reason: str                # 机读短标签，如 "keyword:human_request"
    human_message: str         # 给运营看的说明，日志/审批展示用

    @classmethod
    def none(cls) -> "EscalationDecision":
        return cls(False, "", "")


# 显式索要真人（多语言）
_HUMAN_REQUEST_KEYWORDS = [
    # 英文
    "real person", "talk to a human", "talk to human", "speak to human",
    "human please", "real human", "customer service", "support agent",
    "live agent", "real agent", "not a bot", "stop the bot",
    # 中文简繁
    "真人", "人工", "转人工", "找个真人", "不要机器人", "客服",
    # 日文
    "人と話", "オペレーター",
    # 其他
    "operator please", "no bot",
]

# 投诉 / 退款 / 取消 / 合同 — 触发升级
_COMPLAINT_KEYWORDS = [
    "complaint", "complain", "refund", "return", "chargeback",
    "cancel order", "cancel subscription", "dispute",
    "投诉", "退款", "退货", "取消订单", "退钱", "不满意",
]

# 金额（粗粒度）：$123 ¥ 500 RMB/USD/EUR + 3+ 位数
_MONEY_PATTERN = re.compile(
    r"(?:[$¥€￥]\s?\d[\d,.]*)|"           # 带货币符
    r"(?:\d+(?:\.\d+)?\s?(?:USD|CNY|RMB|EUR|JPY|HKD|元|美元|人民币))",
    re.IGNORECASE,
)

# 合同 / 合约 / 协议
_CONTRACT_KEYWORDS = [
    "contract", "agreement", "legal", "lawyer", "attorney",
    "合同", "合约", "协议", "律师", "起诉",
]


def _contains_any(text: str, keywords: Sequence[str]) -> Optional[str]:
    t = text.lower()
    for kw in keywords:
        if kw.lower() in t:
            return kw
    return None


def evaluate(
    *,
    peer_text: str,
    recent_peer_texts: Optional[List[str]] = None,
    recent_assistant_texts: Optional[List[str]] = None,
    config: Optional[dict] = None,
) -> EscalationDecision:
    """根据当前 peer 消息 + 历史上下文决定是否升级。

    参数
    ------
    peer_text : 最新的 peer 消息文本（已 strip）
    recent_peer_texts : 该 chat 近 N 条 peer 消息（最新在前）
    recent_assistant_texts : 该 chat 近 N 条 AI 回复（最新在前）
    config : messenger_rpa 配置段（读 escalation.* 开关）
    """
    cfg = config or {}
    esc_cfg = cfg.get("escalation") or {}
    if not esc_cfg.get("enabled", True):
        return EscalationDecision.none()

    text = (peer_text or "").strip()
    if not text:
        return EscalationDecision.none()

    # 显式"要真人"
    if esc_cfg.get("keyword_human_request", True):
        hit = _contains_any(text, _HUMAN_REQUEST_KEYWORDS)
        if hit:
            return EscalationDecision(
                True,
                f"keyword:human_request:{hit}",
                f"Peer explicitly asked for a human ('{hit}')",
            )

    # 投诉 / 退款
    if esc_cfg.get("keyword_complaint", True):
        hit = _contains_any(text, _COMPLAINT_KEYWORDS)
        if hit:
            return EscalationDecision(
                True,
                f"keyword:complaint:{hit}",
                f"Peer raised a complaint / refund request ('{hit}')",
            )

    # 合同/法律
    if esc_cfg.get("keyword_contract", True):
        hit = _contains_any(text, _CONTRACT_KEYWORDS)
        if hit:
            return EscalationDecision(
                True,
                f"keyword:contract:{hit}",
                f"Peer mentioned contract/legal topic ('{hit}')",
            )

    # 金额提及（仅在 peer 主动报价/讨论金钱时）
    if esc_cfg.get("money_mention", True):
        m = _MONEY_PATTERN.search(text)
        if m:
            return EscalationDecision(
                True,
                f"money_mention:{m.group(0)[:30]}",
                f"Peer mentioned money ('{m.group(0)[:30]}')",
            )

    # 重复追问：peer 最近 N 条与当前消息高度相似
    repeat_threshold = int(esc_cfg.get("repeat_threshold", 3))
    if repeat_threshold > 0 and recent_peer_texts:
        # 简化：统计最近 N 条中有多少条与当前 text 有 50%+ 重叠
        recent = [t for t in (recent_peer_texts or []) if t][:repeat_threshold]
        if len(recent) >= repeat_threshold - 1:
            similar_cnt = 0
            for prev in recent:
                if _rough_similarity(text, prev) >= 0.5:
                    similar_cnt += 1
            if similar_cnt >= repeat_threshold - 1:
                return EscalationDecision(
                    True,
                    "repeat:unresolved",
                    f"Peer asked similar question {similar_cnt + 1} times; "
                    "AI replies did not resolve",
                )

    return EscalationDecision.none()


def _rough_similarity(a: str, b: str) -> float:
    """粗粒度相似度：字符集合 Jaccard。适合短句。"""
    if not a or not b:
        return 0.0
    sa = set(a.lower())
    sb = set(b.lower())
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0
