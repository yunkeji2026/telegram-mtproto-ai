"""统一收件箱消息/会话归一器（Phase A3）。

把此前内联在 ``src/web/routes/unified_inbox_routes.py`` 的 ``_message_obj`` /
``_normalize_chat`` / ``_candidate_messages_from_source`` / ``_conv_id`` 提为
**纯函数**（无 request/IO 依赖，仅依赖语言检测），便于：
- 跨层复用（Channel Adapter 各平台共用同一归一逻辑）；
- 单元测试（不必起 FastAPI app）。

行为与抽取前完全一致；路由层保留同名薄委托别名，调用点零改动。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.ai.translation_service import detect_language

# 统一收件箱草稿/审批的 4 档自动化模式（与 unified_inbox 前端一致）
SEND_MODES = ["manual", "review", "multi_choice", "auto_ai"]


def conv_id(platform: str, account_id: str, chat_key: str) -> str:
    """会话唯一 id：platform:account_id:chat_key。"""
    return f"{platform}:{account_id}:{chat_key}"


def message_obj(
    *,
    text: str,
    ts: Any = 0,
    direction: str = "in",
    message_id: str = "",
    source: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把一条原始消息归一为统一 message dict（含语言检测与占位翻译态）。"""
    raw = str(text or "")
    lang = detect_language(raw)
    return {
        "message_id": str(message_id or ""),
        "direction": direction if direction in {"in", "out"} else "in",
        "text": raw,
        "original_text": raw,
        "translated_text": raw,
        "language": lang,
        "translation": {
            "source_lang": lang,
            "target_lang": "zh",
            "ok": lang in {"zh", "unknown"} or not raw.strip(),
            "provider": "identity" if lang == "zh" else "none",
            "error": "" if lang in {"zh", "unknown"} else "not_requested",
        },
        "ts": ts or 0,
        "source": source or {},
    }


def normalize_chat(
    *,
    platform: str,
    platform_name: str,
    account_id: str,
    account_label: str,
    chat_key: str,
    name: str,
    last_msg: str,
    last_ts: Any = 0,
    unread: Any = 0,
    source: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把一条平台会话归一为统一 chat dict。"""
    msg = message_obj(text=last_msg, ts=last_ts, direction="in", source=source)
    return {
        "platform": platform,
        "platform_name": platform_name,
        "account_id": account_id,
        "account_label": account_label,
        "chat_key": chat_key,
        "conversation_id": conv_id(platform, account_id, chat_key),
        "name": name,
        "last_msg": last_msg,
        "last_ts": last_ts or 0,
        "unread": unread or 0,
        "language": msg["language"],
        "last_message": msg,
        "messages": [msg] if last_msg else [],
        "can_send": True,
        "send_modes": list(SEND_MODES),
        "automation_mode": "review",
        "risk": {"level": "unknown", "reasons": []},
        "relationship": {"stage": "", "intimacy_score": None},
        "source": source or {},
    }


def candidate_messages_from_source(source: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从平台 source 里尽力抽取历史消息列表并归一（取最近 50 条，过滤空文本）。"""
    for key in ("messages", "history", "recent_messages", "conversation"):
        rows = source.get(key)
        if isinstance(rows, list):
            out: List[Dict[str, Any]] = []
            for idx, row in enumerate(rows[-50:]):
                if isinstance(row, dict):
                    text = (row.get("text") or row.get("raw")
                            or row.get("peer_text") or row.get("message") or "")
                    direction = row.get("direction") or ("out" if row.get("is_self") else "in")
                    out.append(message_obj(
                        text=str(text or ""),
                        ts=row.get("ts") or row.get("timestamp") or 0,
                        direction=str(direction),
                        message_id=str(row.get("id") or row.get("message_id") or idx),
                        source=row,
                    ))
            return [m for m in out if m.get("text")]
    return []
