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
    chat_type: str = "private"  # private / group / channel（群组不进升级告警）
    # 身份画像（真实昵称/头像/资料面板用）：跨平台采集 peer 真实身份，
    # 落库后列表/头部/客户信息面板统一读出，替代「一排数字 id」。
    # 均为纯加法可选字段，缺省空——空值绝不覆盖已存的非空值（见 store.ingest_batch）。
    username: str = ""      # @handle（Telegram username / 平台公开标识）
    phone: str = ""         # 电话号（若平台可得；展示时脱敏）
    avatar_url: str = ""    # 已落地头像的可加载 URL（/static/... 或平台代理端点）
    first_seen: float = 0.0  # 首次接触时间戳（0=未知，入库时取 created_at）


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
    # P4-2 引用回复：被引用消息的平台 id / 文本摘要 / 发言人（群内），用于气泡上方渲染引用条
    reply_to_id: str = ""
    reply_to_text: str = ""
    reply_to_sender: str = ""


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
