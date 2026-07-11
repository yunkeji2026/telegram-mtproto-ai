"""把 unified_inbox 现有聚合结果旁路写入 InboxStore。

这是 Phase A 增量 1 的「写入桥」：复用 unified_inbox_routes 已经归一好的
chat dict（_normalize_chat 产物）与 message dict（_message_obj 产物），
映射成 InboxConversation / InboxMessage 落库。

刻意不引入 ChannelAdapter：增量 1 只做旁路持久化（零行为变化），适配器留到
读路径切换（增量 2）时再引入并真正被使用，避免现在写无人调用的死代码。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .models import InboxConversation, InboxMessage
from .normalizer import extract_platform_msg_id

logger = logging.getLogger(__name__)

# 群/频道类型集合（非 1:1 私聊）。命中即视为群会话。
_GROUP_CHAT_TYPES = {
    "group", "supergroup", "megagroup", "gigagroup", "channel", "broadcast",
}


def _telegram_peer_segment(conv: Dict[str, Any]) -> Optional[int]:
    """从 chat_key / conversation_id 取 Telegram peer 段并解析成 int（取不到→None）。"""
    for key in ("chat_key", "conversation_id"):
        raw = str(conv.get(key) or "")
        if not raw:
            continue
        seg = raw.split(":")[-1].strip()
        try:
            return int(seg)
        except (ValueError, TypeError):
            continue
    return None


def is_group_conversation(conv: Dict[str, Any]) -> bool:
    """判定会话是否为群/频道（非 1:1 私聊）——纯函数，供 auto-draft 源头过滤复用。

    双重判据（任一命中即群）：
      1) ``chat_type`` ∈ 群类型集合（最可靠，来自平台元数据）；
      2) Telegram 兜底启发式：peer 段为负整数（``-100…`` 超级群/频道、``-…`` 旧群），
         私聊 peer 为正。其他平台无负 id 语义，仅凭 chat_type 判定。

    仅做识别、不做策略；是否因此跳过由调用方按配置开关决定。
    """
    ct = str(conv.get("chat_type") or "").lower()
    if ct in _GROUP_CHAT_TYPES:
        return True
    platform = str(conv.get("platform") or "").lower()
    if platform.startswith("telegram"):
        peer = _telegram_peer_segment(conv)
        if peer is not None and peer < 0:
            return True
    return False


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
    # P4-2 引用回复：从 source.reply_to 抽被引用消息摘要（上游 Node/RPA 带则有）
    rt = src.get("reply_to") if isinstance(src.get("reply_to"), dict) else {}
    # P4-11D 群提及明细：source.mentions=[{jid,number,name}] → 持久成 JSON 串（缺省 ''）
    _ml = src.get("mentions") if isinstance(src.get("mentions"), list) else None
    mentions_json = ""
    if _ml:
        import json as _json
        try:
            mentions_json = _json.dumps(_ml, ensure_ascii=False)
        except Exception:
            mentions_json = ""
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
        reply_to_id=str(rt.get("id") or ""),
        reply_to_text=str(rt.get("text") or ""),
        reply_to_sender=str(rt.get("sender") or ""),
        mentions_json=mentions_json,
        # P4-11E 群发言人（source.sender_id/sender_name，缺则空）
        sender_id=str(src.get("sender_id") or ""),
        sender_name=str(src.get("sender_name") or ""),
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
        chat_type=str(chat.get("chat_type") or "private"),
        # peer 身份画像（normalize_chat 已归一；空值不覆盖已存真名/头像，见 store.ingest_batch）
        username=str(chat.get("username") or ""),
        phone=str(chat.get("phone") or ""),
        avatar_url=str(chat.get("avatar_url") or ""),
    )


def _resolve_contact_id(store, conv: InboxConversation) -> str:
    """Q 延伸：best-effort 反查 contact_id（需 store.register_contact_resolver）。"""
    resolver = getattr(store, "_contact_resolver", None)
    if resolver is None or not conv.platform or not conv.chat_key:
        return ""
    try:
        return str(
            resolver(conv.platform, conv.account_id, conv.chat_key) or "",
        ).strip()
    except Exception:
        logger.debug("contact_resolver 失败", exc_info=True)
        return ""


def _apply_contact_id(store, conv: InboxConversation) -> str:
    """解析并写入 conv.contact_id（供 conversations + conv_meta 共用）。"""
    if conv.contact_id:
        return conv.contact_id
    cid = _resolve_contact_id(store, conv)
    if cid:
        conv.contact_id = cid
    return cid


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
            "chat_type": conv.chat_type or "private",
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
        # store-backed 会话（ProtocolInboxAdapter/WebInboxAdapter 经 store_row_to_chat 读出，
        # 带 from_store=True）已是事实源里的行，再写回毫无意义且有害：其 last_message 由
        # last_text 重建、**不带 platform_msg_id**，会落到 hash(text|ts) 兜底键，与权威路径
        # （protocol worker push 经 ingest_incoming 带真 msg_id）产出的 ``:<pmid>`` 行不同键，
        # 凭空多出一条 ``:h:<hash>`` 重复行；更糟的是该“新插入”会再次触发 new_inbound 回调
        # （auto-draft 等），造成重复草稿/重复处理。故 store 读出的会话一律跳过 re-ingest。
        if chat.get("from_store"):
            continue
        conv = _conv_from_chat(chat)
        if not conv.conversation_id or not conv.platform:
            continue
        contact_id = _apply_contact_id(store, conv)
        msgs: List[InboxMessage] = []
        lm = chat.get("last_message") or {}
        # 媒体消息可能无文本（如无 caption 的图片/语音/贴纸）——有 media_ref/type 也应落库
        if isinstance(lm, dict) and (
            lm.get("text") or lm.get("media_ref") or lm.get("media_type")
        ):
            msgs.append(_msg_from_obj(conv.conversation_id, lm, platform=conv.platform))
        n = store.ingest_batch(conv, msgs)
        inserted += n
        if n > 0 and isinstance(lm, dict) \
                and str(lm.get("direction") or "in") == "in":
            # P0-companion：客户再次来消息 → 立即取消搁置，让会话重回「待接管」队列。
            # clear_snooze 自带「未搁置即 no-op、不建 meta 行」护栏，故每条入站直调即可。
            try:
                store.clear_snooze(conv.conversation_id)
            except Exception:
                logger.debug("clear_snooze 失败（已忽略）", exc_info=True)
            if publish_events:
                _publish_inbox_message(conv)
            # E2：通知已注册的入站新消息回调（auto-draft 生成等），best-effort
            msg_text = str(lm.get("text") or "").strip()
            if not msg_text:
                media_type = str(lm.get("media_type") or "")
                if media_type or lm.get("media_ref"):
                    from src.integrations.protocol_bridge import media_placeholder
                    msg_text = media_placeholder(media_type)
            if msg_text:
                conv_dict = {
                    "conversation_id": conv.conversation_id,
                    "platform": conv.platform,
                    "account_id": conv.account_id,
                    "chat_key": conv.chat_key,
                    "display_name": conv.display_name,
                    "chat_type": conv.chat_type or "private",
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
                    # O：用 analyze_emotion 的强度量级补齐情绪「有多强」（标签来自规则分类器，
                    # 强度来自情感引擎，二者正交）；供主动护栏强度分级。失败回落 -1（未知）。
                    _emo_intensity = -1.0
                    try:
                        from src.utils.emotional_context import analyze_emotion
                        _emo = analyze_emotion(msg_text)
                        if str(_emo.get("dimension") or "") != "neutral":
                            _emo_intensity = float(_emo.get("primary_intensity") or -1.0)
                    except Exception:
                        logger.debug("analyze_emotion 强度计算失败", exc_info=True)
                    # 音频声学情绪融合：若该条语音带 SER 结果（lm["audio_emotion"]），
                    # 与文字情绪按 fuse_emotion 规则合成（言不由衷时以声学为准），
                    # 写更准的 last_emotion / last_emotion_intensity。无音频 → 原样文字。
                    _emo_label = str(_analysis.get("emotion") or "")
                    try:
                        _audio_emo = lm.get("audio_emotion") if isinstance(lm, dict) else None
                        if _audio_emo:
                            from src.ai.emotion_fusion import fuse_emotion
                            _fused = fuse_emotion(
                                text_label=_emo_label,
                                text_intensity=_emo_intensity,
                                audio_emo=_audio_emo,
                            )
                            if _fused.get("label"):
                                _emo_label = _fused["label"]
                            _fi = _fused.get("intensity")
                            if isinstance(_fi, (int, float)) and _fi >= 0:
                                _emo_intensity = float(_fi)
                    except Exception:
                        logger.debug("音频情绪融合失败（回落文字情绪）", exc_info=True)
                    store.update_conv_meta(
                        conv.conversation_id,
                        platform=conv.platform,
                        intent=str(_analysis.get("intent") or ""),
                        emotion=_emo_label,
                        risk=str(_analysis.get("risk_level") or "low"),
                        contact_id=contact_id,
                        emotion_intensity=_emo_intensity,
                    )
                except Exception:
                    logger.debug("update_conv_meta 失败", exc_info=True)

                # 深度人设 · 运行期累积（L4 经历式记忆 + L2 未收尾话题）。best-effort，
                # 仅当 deep_persona store 已初始化（=master flag 开、ai_client 已建库）才写；
                # 关则 store=None 直接跳过（零开销、零行为变更）。只存消毒后的短摘要。
                try:
                    from src.companion.deep_persona_store import get_deep_persona_store
                    _dp_store = get_deep_persona_store()
                    if _dp_store is not None and msg_text:
                        from src.companion.deep_persona_stats import get_deep_persona_stats
                        _dp_stats = get_deep_persona_stats()
                        _cid = conv.conversation_id
                        _txt = msg_text.strip()
                        _snip = _txt[:40]
                        _lbl = str(locals().get("_emo_label") or "")
                        _inten = float(locals().get("_emo_intensity") or -1.0)
                        # L4：情绪浓的实质消息 → 记成"带情感的经历"（记故事而非事实）
                        if _inten >= 0.5 and len(_txt) >= 8:
                            # E1：semantic_recall 开且 embedder 就绪 → 顺手缓存事件向量（off 回复热路）
                            _e_emb = None
                            try:
                                from src.companion.deep_persona_runtime import (
                                    get_embedder, semantic_recall_enabled)
                                if semantic_recall_enabled():
                                    _emb_fn = get_embedder()
                                    if _emb_fn is not None:
                                        _e_emb = _emb_fn(_snip)
                            except Exception:
                                _e_emb = None
                            _dp_store.add_experiential(
                                _cid, _snip, emotion=_lbl, salience=_inten, emb=_e_emb)
                            _dp_stats.incr("experiential_added")
                        # L2：提到计划/将来/悬念 → open loop，供日后"不问就回指"
                        import re as _re_dp
                        if len(_txt) >= 6 and _re_dp.search(
                            r"(打算|准备|下周|下个月|明天|计划|考虑|想去|等我|回头|之后|以后|面试|复查|结果出来)",
                            _txt,
                        ):
                            _dp_store.add_open_loop(
                                _cid, _snip, salience=max(_inten, 0.3))
                            _dp_stats.incr("open_loops_added")
                        # E3（默认关）：记人设自身跨会话「见闻」话题（去标识聚合，只存话题词+计数）
                        try:
                            from src.companion.deep_persona_runtime import self_memory_enabled
                            if self_memory_enabled() and len(_txt) >= 4:
                                from src.companion.persona_self_memory import (
                                    get_persona_self_memory, extract_self_topic)
                                from src.utils.persona_manager import PersonaManager
                                _psm = get_persona_self_memory("config/deep_persona.db")
                                _p2, _ = PersonaManager.get_instance().get_persona_with_tier(
                                    _cid, "")
                                _pid2 = str((_p2 or {}).get("id") or "") if _p2 else ""
                                _tp = extract_self_topic(_txt)
                                if _psm is not None and _pid2 and _tp:
                                    _psm.record_topic(_pid2, _tp)
                        except Exception:
                            logger.debug("deep_persona self_memory 记录失败（忽略）", exc_info=True)
                        # L2：后续消息回应了旧话题 → 自动收尾，避免反复追问同一件事
                        try:
                            from src.companion.deep_persona import find_resolved_loops
                            _loops = _dp_store.get_open_loops(_cid)
                            for _rt in find_resolved_loops(_loops, _txt):
                                _dp_store.resolve_open_loop(_cid, _rt)
                                _dp_stats.incr("loops_resolved")
                        except Exception:
                            pass
                        # 机会式巩固（节流 15min/会话）：关系画像 L5 + 内部梗
                        try:
                            if _dp_store.due_for_consolidation(_cid):
                                from src.companion.deep_persona import (
                                    run_deep_persona_consolidation)
                                # C4 漂移守卫：解析当前人设传入，画像命中硬禁项则拒写
                                _persona = None
                                try:
                                    from src.utils.persona_manager import PersonaManager
                                    _persona, _ = PersonaManager.get_instance(
                                    ).get_persona_with_tier(_cid, "")
                                except Exception:
                                    _persona = None
                                # E2：llm_refine 开且 LLM 就绪 → 传 llm_fn 精修画像（off 热路）
                                _llm_fn = None
                                _emb_fn2 = None
                                try:
                                    from src.companion.deep_persona_runtime import (
                                        get_llm, llm_refine_enabled,
                                        get_embedder, semantic_recall_enabled)
                                    if llm_refine_enabled():
                                        _llm_fn = get_llm()
                                    # G2：语义开时传 embedder 批量回填历史事件向量（off 热路）
                                    if semantic_recall_enabled():
                                        _emb_fn2 = get_embedder()
                                except Exception:
                                    _llm_fn = None
                                _r = run_deep_persona_consolidation(
                                    store, _dp_store, _cid, persona=_persona,
                                    llm_fn=_llm_fn, embedder=_emb_fn2)
                                _dp_stats.incr("consolidations")
                                if _r.get("profile"):
                                    _dp_stats.incr("profiles_built")
                                if _r.get("jokes"):
                                    _dp_stats.incr("jokes_detected", int(_r["jokes"]))
                                if _r.get("drift"):
                                    _dp_stats.incr("drift_blocked")
                        except Exception:
                            logger.debug("deep_persona 巩固失败（忽略）", exc_info=True)
                except Exception:
                    logger.debug("deep_persona 运行期累积失败（忽略）", exc_info=True)

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
    _apply_contact_id(store, conv)
    msgs = [
        _msg_from_obj(conv.conversation_id, m, platform=conv.platform)
        for m in (messages or [])
        if isinstance(m, dict) and m.get("text")
    ]
    return store.ingest_batch(conv, msgs)
