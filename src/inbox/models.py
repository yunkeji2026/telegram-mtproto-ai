"""统一收件箱数据类。

字段刻意对齐既有 shape，落库零转换成本：
- InboxMessage 对齐 unified_inbox_routes._message_obj() 的字段
- MessageAnalysis 对齐 src/ai/chat_assistant_service.ChatAnalysis.to_dict()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class InboxConversation:
    """跨平台会话事实（ingest 写入，不含运营态配置）。

    运营态（automation_mode 等）单独放 conversation_settings 表，
    避免 ingest 覆盖人工设置。
    """

    conversation_id: str
    platform: str
    account_id: str = "default"
    chat_key: str = ""
    display_name: str = ""
    language: str = "unknown"
    last_text: str = ""
    last_ts: float = 0.0
    unread: int = 0
    contact_id: str = ""


@dataclass
class InboxMessage:
    """统一消息（原文/译文/方向/媒体/平台 message id）。"""

    conversation_id: str
    platform_msg_id: str = ""
    direction: str = "in"  # in / out
    text: str = ""
    original_text: str = ""
    translated_text: str = ""
    source_lang: str = "unknown"
    target_lang: str = ""
    media_type: str = ""
    media_ref: str = ""
    ts: float = 0.0


@dataclass
class MessageAnalysis:
    """意图/情绪/风险分析（Phase C 的 LLM 升级写入；A 先建表与读写口）。"""

    message_id: str
    conversation_id: str
    intent: str = ""
    emotion: str = ""
    risk_level: str = "low"
    risk_reasons: List[str] = field(default_factory=list)
    relationship_stage: str = ""
    summary: str = ""
    order_no: str = ""
    confidence: float = 0.0
    analyzer: str = "rule"  # rule / llm

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "intent": self.intent,
            "emotion": self.emotion,
            "risk_level": self.risk_level,
            "risk_reasons": list(self.risk_reasons),
            "relationship_stage": self.relationship_stage,
            "summary": self.summary,
            "order_no": self.order_no,
            "confidence": self.confidence,
            "analyzer": self.analyzer,
        }
