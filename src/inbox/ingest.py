"""把 unified_inbox 现有聚合结果旁路写入 InboxStore。

这是 Phase A 增量 1 的「写入桥」：复用 unified_inbox_routes 已经归一好的
chat dict（_normalize_chat 产物）与 message dict（_message_obj 产物），
映射成 InboxConversation / InboxMessage 落库。

刻意不引入 ChannelAdapter：增量 1 只做旁路持久化（零行为变化），适配器留到
读路径切换（增量 2）时再引入并真正被使用，避免现在写无人调用的死代码。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from .models import InboxConversation, InboxMessage
from .normalizer import extract_platform_msg_id

logger = logging.getLogger(__name__)


def _msg_from_obj(
    conversation_id: str, m: Dict[str, Any], *, direction: str = "", platform: str = "",
) -> InboxMessage:
    text = str(m.get("text") or "")
    # 稳定 message id：从平台原始 source 按白名单提取可信 id（MTProto message.id /
    # WhatsApp wamid 等），让 collect / thread 两条路径对同一条消息产出同一去重键。
    # 取不到（多数 RPA 源）→ 留空，store 回落 hash(text|ts) 内容去重（跨路径仍一致，
    # 因为两路径同文本同 ts）。LINE 不取裸 id（房间 id），见 normalizer 白名单。
    src = m.get("source") if isinstance(m.get("source"), dict) else {}
    pid = extract_platform_msg_id(src, platform)
    # P61：携带媒体字段（message_obj 已从 source 抽取；无则回落直接读 source）
    media_type = str(m.get("media_type") or "")
    media_ref = str(m.get("media_ref") or "")
    if not media_type and not media_ref:
        from .normalizer import extract_media
        media_type, media_ref = extract_media(src)
    return InboxMessage(
        conversation_id=conversation_id,
        platform_msg_id=pid,
        direction=direction or str(m.get("direction") or "in"),
        text=text,
        original_text=str(m.get("original_text") or text),
        translated_text=str(m.get("translated_text") or ""),
        source_lang=str(m.get("language") or "unknown"),
        media_type=media_type,
        media_ref=media_ref,
        ts=float(m.get("ts") or 0),
    )


def _conv_from_chat(chat: Dict[str, Any]) -> InboxConversation:
    return InboxConversation(
        conversation_id=str(chat.get("conversation_id") or ""),
        platform=str(chat.get("platform") or ""),
        account_id=str(chat.get("account_id") or "default"),
        chat_key=str(chat.get("chat_key") or ""),
        display_name=str(chat.get("name") or ""),
        language=str(chat.get("language") or "unknown"),
        last_text=str(chat.get("last_msg") or ""),
        last_ts=float(chat.get("last_ts") or 0),
        unread=int(chat.get("unread") or 0),
    )


def _publish_inbox_message(conv: InboxConversation) -> None:
    """有新入站消息时，向全局事件总线发 inbox_message（SSE 实时推送给工作台）。"""
    try:
        from src.integrations.shared.event_bus import get_event_bus
        get_event_bus().publish("inbox_message", {
            "conversation_id": conv.conversation_id,
            "platform": conv.platform,
            "account_id": conv.account_id,
            "chat_key": conv.chat_key,
            "name": conv.display_name,
            "preview": (conv.last_text or "")[:80],
            "unread": int(conv.unread or 0),
            "ts": conv.last_ts,
        })
    except Exception:
        logger.debug("inbox_message 事件发布失败", exc_info=True)


def ingest_collected_chats(
    store, chats: List[Dict[str, Any]], *, publish_events: bool = False
) -> int:
    """旁路写入聚合到的对话列表。返回新插入的消息条数。best-effort，调用方包 try。

    publish_events=True 时，对**新插入的入站消息**所属会话发 inbox_message 事件
    （供坐席工作台 SSE 实时刷新）；冷启动首轮应传 False 以免事件洪泛。
    """
    inserted = 0
    for chat in chats or []:
        conv = _conv_from_chat(chat)
        if not conv.conversation_id or not conv.platform:
            continue
        msgs: List[InboxMessage] = []
        lm = chat.get("last_message") or {}
        # 媒体消息可能无文本（如无 caption 的图片/语音）——有 media_ref 也应落库
        if isinstance(lm, dict) and (lm.get("text") or lm.get("media_ref")):
            msgs.append(_msg_from_obj(conv.conversation_id, lm, platform=conv.platform))
        n = store.ingest_batch(conv, msgs)
        inserted += n
        if n > 0 and isinstance(lm, dict) \
                and str(lm.get("direction") or "in") == "in":
            if publish_events:
                _publish_inbox_message(conv)
            # E2：通知已注册的入站新消息回调（auto-draft 生成等），best-effort
            msg_text = str(lm.get("text") or "").strip()
            if msg_text:
                conv_dict = {
                    "conversation_id": conv.conversation_id,
                    "platform": conv.platform,
                    "account_id": conv.account_id,
                    "chat_key": conv.chat_key,
                    "display_name": conv.display_name,
                }
                for _cb in getattr(store, "_new_inbound_cbs", []):
                    try:
                        _cb(conv_dict, msg_text)
                    except Exception:
                        logger.debug("new_inbound_cb 调用失败", exc_info=True)
                # I1：异步更新对话智能元数据（best-effort，不阻断 ingest）
                try:
                    from src.ai.chat_assistant_service import quick_analyze
                    _analysis = quick_analyze(msg_text)
                    store.update_conv_meta(
                        conv.conversation_id,
                        platform=conv.platform,
                        intent=str(_analysis.get("intent") or ""),
                        emotion=str(_analysis.get("emotion") or ""),
                        risk=str(_analysis.get("risk_level") or "low"),
                    )
                except Exception:
                    logger.debug("update_conv_meta 失败", exc_info=True)

                # R3：检测 CSAT 问卷回复（1-5 纯数字，会话正在等待问卷）
                try:
                    _t_stripped = msg_text.strip()
                    if (
                        _t_stripped.isdigit()
                        and 1 <= int(_t_stripped) <= 5
                        and store.is_survey_awaiting(conv.conversation_id)
                    ):
                        _score = int(_t_stripped)
                        matched = store.record_survey_response(conv.conversation_id, _score)
                        if matched:
                            store.set_conv_survey_awaiting(conv.conversation_id, False)
                            logger.info(
                                "R3 CSAT survey response conv=%s score=%d",
                                conv.conversation_id, _score,
                            )
                except Exception:
                    logger.debug("R3 survey_response 检测失败", exc_info=True)
    return inserted


def ingest_thread(store, chat: Dict[str, Any], messages: List[Dict[str, Any]]) -> int:
    """操作员打开会话时，把该会话较完整的历史落库。返回新插入条数。"""
    if not chat:
        return 0
    conv = _conv_from_chat(chat)
    if not conv.conversation_id or not conv.platform:
        return 0
    msgs = [
        _msg_from_obj(conv.conversation_id, m, platform=conv.platform)
        for m in (messages or [])
        if isinstance(m, dict) and m.get("text")
    ]
    return store.ingest_batch(conv, msgs)
