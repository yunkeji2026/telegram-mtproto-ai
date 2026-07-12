"""auto-draft 富化回调抽取（Stage 2，从 main.py initialize() 原样迁出，行为不变）。

enrich_auto_draft(assistant, draft_svc, _ad_app, _ad_store, conv, text, draft_id, mode)：
异步用会话历史 + 人设产线富化自动草稿；_ad_app=web_app, _ad_store=inbox_store。
"""
from __future__ import annotations


async def enrich_auto_draft(assistant, draft_svc, _ad_app, _ad_store, conv: dict, text: str, draft_id: str, mode: str) -> None:
    """异步：拉历史 → 人设产线生成正文 → enrich_draft 收尾。

    失败任意环节都兜底 release（保留规则模板占位，降级旧行为），
    保证停泊草稿不会卡死在 enriching。"""
    try:
        from src.inbox.persona_reply import (
            generate_persona_reply, normalize_history,
        )
        cid = str(conv.get("conversation_id") or "")
        platform = str(conv.get("platform") or "")
        chat_key = str(conv.get("chat_key") or "")
        account_id = str(conv.get("account_id") or "default")
        msgs = []
        _peer_media_type = ""
        _peer_media_ref = ""
        _peer_media_desc = ""
        _peer_msg_id = ""  # 语音转录回写目标行
        try:
            for r in _ad_store.list_recent_messages(cid, limit=30):
                msgs.append({
                    "direction": r.get("direction") or "in",
                    "text": r.get("text") or "",
                    "media_type": r.get("media_type") or "",
                    "media_ref": r.get("media_ref") or "",
                    "message_id": r.get("message_id") or "",
                })
            for r in reversed(msgs):
                if str(r.get("direction") or "") == "in":
                    _peer_media_type = str(
                        r.get("media_type") or ""
                    )
                    _peer_media_ref = str(
                        r.get("media_ref") or ""
                    )
                    _peer_msg_id = str(
                        r.get("message_id") or ""
                    )
                    _lt = str(r.get("text") or "")
                    if _lt.startswith("[图片内容]"):
                        _peer_media_desc = _lt.replace(
                            "[图片内容]", "", 1
                        ).strip()
                    break
        except Exception:
            msgs = []
        history, last = normalize_history(msgs)
        if not last:
            last = str(text or "")
        _peer_audio_emotion = None  # 语音声学情绪（SER）
        if (
            _peer_media_type in ("image", "photo", "sticker")
            and _peer_media_ref
            and not _peer_media_desc
        ):
            try:
                from src.integrations.protocol_bridge import (
                    static_media_ref_to_path,
                )
                _img_path = static_media_ref_to_path(
                    _peer_media_ref
                )
                _tc = getattr(
                    getattr(_ad_app, "state", None),
                    "telegram_client",
                    None,
                )
                if _img_path and _tc is not None and hasattr(
                    _tc, "_get_image_content"
                ):
                    _desc = await _tc._get_image_content(
                        _img_path
                    )
                    if _desc:
                        _peer_media_desc = str(_desc).strip()
            except Exception:
                assistant.logger.debug(
                    "[AutoDraft] 图片识别补全失败",
                    exc_info=True,
                )
        elif (
            _peer_media_type in ("voice", "audio")
            and _peer_media_ref
            and not _peer_media_desc
        ):
            # 语音转录补全（与图片 Vision 描述对等）：入站语音
            # 转成文本喂人设产线。缺这步 AI 只见「[语音]」占位，
            # 会搪塞「我听不了语音」而非接住内容。
            try:
                from src.integrations.protocol_bridge import (
                    static_media_ref_to_path,
                )
                _voice_path = static_media_ref_to_path(
                    _peer_media_ref
                )
                _tc = getattr(
                    getattr(_ad_app, "state", None),
                    "telegram_client",
                    None,
                )
                _vtr = getattr(
                    _tc, "voice_transcriber", None,
                ) if _tc is not None else None
                if _voice_path and _vtr is not None:
                    _vlang = str(
                        (assistant.config.get(
                            "voice_recognition", {},
                        ) or {}).get("language", "auto")
                    ) or "auto"
                    _vtxt = await _vtr.transcribe_voice_message(
                        str(_voice_path), _vlang,
                    )
                    if _vtxt and str(_vtxt).strip():
                        _peer_media_desc = str(_vtxt).strip()
                        # 转录文本即「对方说的话」→ 直接作为待回复
                        # 文本（替换 [语音] 占位），让意图/语言/
                        # 回复都基于真实内容（含回对语言）。
                        if last.strip() in (
                            "[语音]", "[媒体]", "",
                        ):
                            last = _peer_media_desc
                        # 修复 history 与转录不一致：把历史末条用户
                        #「[语音]」占位补成转录文本，避免语言切换误判。
                        for _hm in reversed(history or []):
                            if isinstance(_hm, dict) and _hm.get(
                                "role") == "user":
                                if str(_hm.get("content") or "").strip() in (
                                    "[语音]", "[媒体]", ""):
                                    _hm["content"] = _peer_media_desc
                                break
                        assistant.logger.info(
                            "[AutoDraft] 语音转录补全: %s",
                            _peer_media_desc[:80],
                        )
                        # 音频情绪识别（SER）：从声学语气听情绪，
                        # 与原生 TG 路径对齐（best-effort，软降级）。
                        try:
                            _se_cfg = (assistant.config.get(
                                'speech_emotion', {}) or {})
                            if _se_cfg.get('enabled'):
                                from src.ai.speech_emotion import (
                                    get_speech_emotion_recognizer)
                                from src.ai.speech_emotion_stats import (
                                    get_speech_emotion_stats)
                                _ser = get_speech_emotion_recognizer(
                                    _se_cfg)
                                _sres = await _ser.recognize_async(
                                    str(_voice_path))
                                _mc = float(_se_cfg.get(
                                    'min_confidence', 0.5) or 0.5)
                                _peer_audio_emotion = (
                                    _sres.as_emotion_dict(
                                        min_confidence=_mc))
                                get_speech_emotion_stats().record(
                                    ok=_sres.ok,
                                    emotion=_sres.emotion,
                                    confident=bool(
                                        _peer_audio_emotion and
                                        _peer_audio_emotion.get(
                                            'confident')),
                                    remote=str(
                                        _sres.model or ''
                                    ).startswith('remote:'))
                                if _peer_audio_emotion and \
                                        _peer_audio_emotion.get(
                                            'confident'):
                                    assistant.logger.info(
                                        "[AutoDraft] 声学情绪: %s "
                                        "score=%.2f",
                                        _peer_audio_emotion.get(
                                            'raw_label'),
                                        _peer_audio_emotion.get(
                                            'score') or 0.0)
                        except Exception:
                            assistant.logger.debug(
                                "[AutoDraft] 音频情绪识别失败",
                                exc_info=True)
                        # 转录回写入站消息行：坐席台/时间线即时看到
                        # 「对方说了什么」而非空白/[语音]占位（转录已在
                        # 此异步路径完成，回写零额外成本、不阻塞主循环、
                        # 不重复转录）。only_if_empty 防踩已有内容。
                        try:
                            _ad_store.update_message_text(
                                cid,
                                message_id=_peer_msg_id,
                                media_ref=_peer_media_ref,
                                text=_peer_media_desc,
                                only_if_empty=True,
                            )
                        except Exception:
                            assistant.logger.debug(
                                "[AutoDraft] 转录回写消息失败",
                                exc_info=True,
                            )
                    else:
                        assistant.logger.warning(
                            "[AutoDraft] 语音转录空结果 ref=%s",
                            _peer_media_ref,
                        )
            except Exception:
                assistant.logger.warning(
                    "[AutoDraft] 语音转录补全失败 ref=%s",
                    _peer_media_ref,
                    exc_info=True,
                )
        # 账号级人设（单一事实源，与 autosend voice 同口径）：
        # meta.persona_id → meta.persona_ids[0] → config 默认，
        # 根治复数/单数不匹配导致的空 persona。
        _persona_id = ""
        try:
            from src.ai.persona_voice import (
                resolve_account_persona_id as _rapi2,
            )
            _persona_id = _rapi2(
                assistant.config.config or {},
                platform, account_id,
            )
        except Exception:
            _persona_id = ""
        # 风险分档（单一事实源）：草稿创建时已算好 risk_level，
        # 取出透传给统一引擎 → 低风险走快路省延迟、中/高风险吃满全栈。
        _risk_level = ""
        try:
            _drow = draft_svc.get_draft(draft_id) or {}
            _risk_level = str(_drow.get("risk_level") or "")
        except Exception:
            _risk_level = ""
        # 语言决策收敛到 generate_persona_reply（单一事实源，
        # 含短消息防误切）；这里不再各自重复检测，直接采信其
        # 返回的 reply_lang 落库 draft_lang。
        out = await generate_persona_reply(
            app=_ad_app, platform=platform, chat_key=chat_key,
            last_inbound=last, history=history,
            persona_id=_persona_id,
            risk_level=_risk_level,
            media_type=_peer_media_type,
            media_ref=_peer_media_ref,
            media_desc=_peer_media_desc,
            conversation_id=cid,
            peer_audio_emotion=_peer_audio_emotion,
        )
        if out.get("ok") and out.get("reply"):
            done = draft_svc.enrich_draft(
                draft_id, reply_text=out["reply"],
                reply_lang=str(out.get("reply_lang") or "zh"),
                automation_mode=mode,
            )
            if done:
                return
        # 生成失败/为空 → 兜底放行（规则模板占位）
        draft_svc.release_enriching_draft(draft_id)
    except Exception:
        assistant.logger.debug(
            "[AutoDraft] 人设补全失败，兜底放行 draft_id=%s",
            draft_id, exc_info=True)
        try:
            draft_svc.release_enriching_draft(draft_id)
        except Exception:
            pass
