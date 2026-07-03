"""人设化智能回复——单一事实源（Phase 1：生成产线收敛）。

历史上「给客户拟一条人设化回复」这件事有多处实现：手动「生成草稿」按钮走
``/api/desktop/smart-reply``、协议自动回复走 ``SkillManager.process_message``、
收件箱全自动草稿走规则模板 ``_suggestions``——后者既无人设、又无上下文、又不查 KB，
导致「全自动回复」与「手动生成草稿」质量割裂。

本模块把 ``/api/desktop/smart-reply`` 的产线逻辑（SkillManager 意图→策略→KB→
``AIClient.generate_reply_with_intent``，PersonaManager 注入后台人设、禁机器措辞、
可选译文）抽成**唯一**的异步函数 ``generate_persona_reply``，供以下三处复用：

  1. ``/api/desktop/smart-reply``（手动按钮，薄壳调用）
  2. ``DraftService.auto_generate_draft``（收件箱全自动草稿，Phase 2 接入）
  3. （后续）协议自动回复，统一上下文装配

层级：仅依赖 ``src.ai``（AIClient/TranslationService）、``src.utils.persona_manager``，
不反向依赖 ``src.web``，故可被 inbox/web 双向复用、可纯单测。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def normalize_history(
    messages: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, str]], str]:
    """把渠道消息列表归一为 OpenAI 风格 history，并取最后一条入站文本。

    入参 ``messages`` 每项形如 ``{"direction": "in"|"out"|..., "text": "..."}``
    （收件箱 store 行 / 桌面壳 DOM 抓取 / smart-reply 请求体三种来源同构）。

    返回 ``(history, last_inbound)``：
      - history: ``[{"role": "user"|"assistant", "content": str}]``，已滤空。
      - last_inbound: 最后一条 ``direction in {in, inbound}`` 的文本；
        若无入站则回落 history 末条内容（兜底，保证至少有「待回复」锚点）。
    """
    history: List[Dict[str, str]] = []
    last_inbound = ""
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        t = str(m.get("text") or "").strip()
        if not t and (m.get("media_type") or m.get("media_ref")):
            try:
                from src.integrations.protocol_bridge import media_placeholder
                t = media_placeholder(str(m.get("media_type") or ""))
            except Exception:
                t = "[媒体]"
        if not t:
            continue
        is_in = m.get("direction") in ("in", "inbound")
        history.append({"role": "user" if is_in else "assistant", "content": t})
        if is_in:
            last_inbound = t
    if not last_inbound and history:
        last_inbound = history[-1]["content"]
    return history, last_inbound


def resolve_reply_language(
    last_inbound: str,
    history: List[Dict[str, str]] | None = None,
    *,
    explicit: str = "",
    default: str = "zh",
) -> str:
    """决定「回复正文该用哪种语言」——单一事实源（含防误切护栏）。

    优先级：
      1. ``explicit`` 非空 → 直接采用（手动 UI 选定的目标语，最高优先，不二次猜）。
      2. 否则按**最新一条入站**检测语言（贴合「客户切语言要跟上」的诉求）。
      3. 防过度切换：最新一条过短（如 ``ok`` / ``哈哈`` / 纯 emoji，``detect_language``
         极易误判）时，回落到**近窗口入站消息的主导语言**——长消息仍信任最新一条，
         兼顾切换灵敏度与短 token 噪声稳定性。

    纯函数、零副作用、可单测；``detect_language`` 惰性导入（与本模块既有
    ``TranslationService`` 同属 ``src.ai``，不抬升导入期成本）。
    """
    explicit = str(explicit or "").strip()
    if explicit:
        return explicit
    from src.ai.translation_service import detect_language

    last = str(last_inbound or "").strip()
    lang = (detect_language(last) if last else "") or default
    if len(last) < 4 and history:
        window = " ".join(
            str(m.get("content") or "")
            for m in history[-6:]
            if m.get("role") == "user"
        ).strip()
        if len(window) >= 4:
            _win_lang = detect_language(window) or lang
            if _win_lang != lang:
                logger.debug(
                    "[persona_reply] 短消息防误切：last=%r(%s) → 回落窗口主导=%s",
                    last[:12], lang, _win_lang,
                )
            lang = _win_lang
    return lang or default


def _resolve_persona_badge(
    chat_key: str, persona_id: str, used_persona_default: str
) -> Tuple[str, str]:
    """让人设徽标说真话：返回（实际生效的 persona 标识, tier）。

    按 chat_key 解析会话绑定/账号画像/domain 三级；解析失败回落传入默认值。
    """
    try:
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        resolved, tier = pm.get_persona_with_tier(chat_key or "", persona_id)
        rid = str((resolved or {}).get("id") or "")
        if tier == "account_profile":
            return (rid or persona_id or "domain", tier)
        if tier == "chat_binding":
            return (rid or "domain", tier)
        return ("domain", tier)
    except Exception:
        logger.debug("[persona_reply] persona tier 解析失败", exc_info=True)
        return (used_persona_default, "")


async def _translate_reply(app: Any, reply: str, target_lang: str) -> str:
    """把回复译成 target_lang（chat 风格）。失败/无服务返回空串。"""
    if not (reply and target_lang):
        return ""
    try:
        from src.ai.translation_service import TranslationService
        svc = getattr(getattr(app, "state", None), "translation_service", None)
        if not isinstance(svc, TranslationService):
            ai_client = getattr(getattr(app, "state", None), "ai_client", None)
            svc = TranslationService(ai_client=ai_client)
        res = await svc.translate(reply, target_lang=target_lang, style="chat")
        if res.ok:
            rd = res.to_dict()
            return rd.get("translated_text") or rd.get("text") or ""
    except Exception:
        logger.debug("[persona_reply] 译文失败", exc_info=True)
    return ""


async def generate_persona_reply(
    *,
    app: Any,
    platform: str,
    chat_key: str,
    last_inbound: str,
    history: List[Dict[str, str]],
    persona_id: str = "",
    target_lang: str = "",
    reply_lang: str = "",
    risk_level: str = "",
    media_type: str = "",
    media_ref: str = "",
    media_desc: str = "",
    conversation_id: str = "",
    peer_audio_emotion: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """人设化智能回复（单一事实源）。

    走与 ``/api/chat/test`` 同一条产线：SkillManager 识别意图 → 取回复策略 →
    KB 检索 → ``AIClient.generate_reply_with_intent``（PersonaManager 注入后台人设、
    禁「作为AI」机器措辞、融合知识库）。SkillManager 不可用时回落通用提示词，
    保证至少有草稿。

    入参：
      - app: 暴露 ``app.state``（skill_manager / ai_client / kb_store / translation_service）
      - history: ``normalize_history`` 产物（``[{role, content}]``）
      - last_inbound: 待回复的最后一条客户文本（空则直接返回 ok=False）
      - reply_lang: 生成正文语言。显式传入=最高优先；为空则回落 target_lang；
        再为空则由 ``resolve_reply_language`` 按 last_inbound/history 自动决策
        （含短消息防误切护栏）。
      - target_lang: 坐席手动选定的目标语；既作正文语言回落，又触发**额外译文**
        （``translated`` 字段）。

    返回：``{ok, reply, reply_lang, persona, persona_tier, intent, translated?}``
      - reply_lang: 本次实际采用的正文语言（决策结果，供调用方落库 draft_lang，
        无需再各自重复检测——语言决策在此收敛为单一事实源）。
    """
    last_inbound = str(last_inbound or "").strip()
    if not last_inbound:
        return {"ok": False, "detail": "无可用对话上下文", "reply": ""}

    # 语言决策单一事实源：显式 reply_lang > 坐席 target_lang > 自动决策(含短消息防误切)。
    resolved_lang = (
        str(reply_lang or "").strip()
        or str(target_lang or "").strip()
        or resolve_reply_language(last_inbound, history, default="zh")
    )

    state = getattr(app, "state", None)
    sm = getattr(state, "skill_manager", None)
    if sm is None:
        tc = getattr(state, "telegram_client", None)
        sm = getattr(tc, "skill_manager", None) if tc is not None else None
    ai = getattr(state, "ai_client", None)

    reply = None
    used_persona = ""
    used_intent = ""
    used_unified = False  # 统一引擎已自带记忆写回 → 避免文末重复写

    # ★ 统一规则引擎（单一事实源·彻底对齐）：优先走 SkillManager.generate_inbox_draft，
    # 与原生 bot/RPA 同享情感引擎/陪伴阶段/慢思考/人设守卫/危机兜底/记忆读写全栈规则。
    # 可经 config inbox.auto_draft.unified_pipeline=false 秒级回退到下方直连路径。
    if (
        sm is not None
        and hasattr(sm, "generate_inbox_draft")
        and getattr(sm, "ai_client", None) is not None
    ):
        _unified_on = True
        try:
            _cfg = getattr(getattr(sm, "config", None), "config", None) or {}
            _unified_on = bool(
                ((_cfg.get("inbox") or {}).get("auto_draft") or {})
                .get("unified_pipeline", True)
            )
        except Exception:
            _unified_on = True
        if _unified_on:
            try:
                _res = await sm.generate_inbox_draft(
                    text=last_inbound,
                    chat_key=chat_key,
                    platform=platform,
                    history=history,
                    persona_id=persona_id,
                    reply_lang=resolved_lang,
                    risk_level=risk_level,
                    media_type=media_type,
                    media_ref=media_ref,
                    media_desc=media_desc,
                    conversation_id=conversation_id,
                    peer_audio_emotion=peer_audio_emotion,
                )
                if _res and (_res.get("reply") or "").strip():
                    reply = _res["reply"]
                    used_intent = _res.get("intent") or ""
                    used_persona = persona_id or "domain"
                    used_unified = True
            except Exception:
                logger.debug("[persona_reply] 统一引擎失败，回落直连", exc_info=True)
                reply = None

    # 主路径（回落）：人设 + KB + 策略（与 /api/chat/test 一致）
    if not reply and sm is not None and getattr(sm, "ai_client", None) is not None:
        try:
            user_id = f"desktop:{platform}:{chat_key}" or "__desktop__"
            intent = sm._recognize_intent(last_inbound)
            used_intent = intent
            try:
                strategy, _sid = sm.get_strategy_for_intent(intent, user_id)
            except Exception:
                strategy = {}
            kb_context = ""
            kb = getattr(state, "kb_store", None)
            if kb is not None:
                try:
                    _res = kb.search(last_inbound, top_k=3, lang="zh")
                    kb_context = kb.build_ai_context_from_result(_res, lang="zh")
                except Exception:
                    kb_context = ""
            ctx: Dict[str, Any] = {
                "user_id": user_id,
                "chat_id": chat_key or user_id,
                "channel": "desktop",
                "platform": platform,
                "intent": intent,
                "current_intent": intent,
                "_reply_strategy": strategy or {},
                "reply_lang": resolved_lang,
            }
            if persona_id:
                ctx["account_persona_id"] = persona_id
            if len(history) > 1:
                hist = history[:-1] if history[-1]["role"] == "user" else history
                ctx["_conversation_history"] = hist[-20:]
            if kb_context:
                ctx["kb_context"] = kb_context
            # ★ 情景记忆注入（单一事实源补全）：全自动/手动产线此前不读长期记忆，导致
            # 「跨会话记不住（如名字）」。复用 SkillManager 既有读取逻辑，按 chat_key 命中
            # 该联系人的长期事实，写入 ctx["_episodic_memory_text"]——generate_reply 会把它
            # 作为「用户长期记忆要点」注入系统提示。读/写用同一 key，保证闭环一致。
            if chat_key and hasattr(sm, "_inject_episodic_into_context"):
                try:
                    sm._inject_episodic_into_context(
                        ctx, str(chat_key), "",
                        current_user_text=last_inbound,
                        platform=platform,
                    )
                except Exception:
                    logger.debug("[persona_reply] 情景记忆注入跳过", exc_info=True)
            so: Dict[str, Any] = {}
            for _sk in ("temperature", "max_tokens", "context_rounds", "model", "thinking_budget"):
                if _sk in (strategy or {}):
                    so[_sk] = strategy[_sk]
            reply = await sm.ai_client.generate_reply_with_intent(
                user_message=last_inbound,
                intent=intent,
                user_context=ctx,
                strategy_overrides=so or None,
            )
            used_persona = persona_id or "domain"
        except Exception:
            logger.debug("[persona_reply] 人设主路径失败，回落通用", exc_info=True)
            reply = None

    # 兜底：SkillManager 不可用时退回通用提示词（保证至少有草稿）
    if not reply and ai is not None:
        lines = []
        for m in history[-12:]:
            who = "客户：" if m.get("role") == "user" else "我："
            lines.append(who + str(m.get("content") or ""))
        # 兜底走 ai.chat（不过 generate_reply_with_intent，故无 LANGUAGE RULE 守卫），
        # 这里直接把语言要求写进提示，避免兜底路径回错语言。
        _lang_hint = (
            f"必须完全使用客户的语言（{resolved_lang}）回复，不要夹杂其它语言。"
            if resolved_lang and resolved_lang != "zh" else ""
        )
        prompt = (
            "你是温暖、自然、像真人一样的线上陪伴/客服。基于以下对话，草拟我的下一条回复。"
            f"{_lang_hint}"
            "口吻自然口语化，禁止出现「作为AI/作为一个AI/有什么可以帮您」等机器措辞，"
            "只输出回复正文。\n\n对话：\n" + "\n".join(lines) + "\n\n我的回复："
        )
        try:
            reply = await ai.chat(prompt)
        except Exception:
            logger.debug("[persona_reply] 兜底失败", exc_info=True)
            reply = None

    reply = (reply or "").strip()
    # ★ 情景记忆写回（闭环）：本轮成功生成回复后，按与读取相同的 key 抽取并落库事实，
    # 让全自动/手动产线像 native bot 一样「越聊越记得」。fire-and-forget——绝不阻塞、
    # 失败也不影响回复发送；记忆开关/抽取意图门控仍由 SkillManager 内部既有逻辑把关。
    if reply and not used_unified and sm is not None and chat_key and hasattr(sm, "_episodic_memory_extract_async"):
        try:
            import asyncio as _aio
            _aio.create_task(sm._episodic_memory_extract_async(
                str(chat_key), last_inbound, reply, used_intent or "", "", platform,
            ))
        except Exception:
            logger.debug("[persona_reply] 情景记忆写回调度跳过", exc_info=True)
    persona_tier = ""
    if reply:
        used_persona, persona_tier = _resolve_persona_badge(
            chat_key, persona_id, used_persona
        )

    out: Dict[str, Any] = {
        "ok": bool(reply),
        "reply": reply,
        "reply_lang": resolved_lang,
        "persona": used_persona,
        "persona_tier": persona_tier,
        "intent": used_intent,
    }
    translated = await _translate_reply(app, reply, target_lang)
    if translated:
        out["translated"] = translated
    return out
