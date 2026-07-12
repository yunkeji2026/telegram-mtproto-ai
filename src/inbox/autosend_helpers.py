"""autosend 语音回调抽取（Stage 2，从 main.py initialize() 原样迁出，行为不变）。

autosend_voice(assistant, platform, account_id, chat_key, text) -> bool：
全自动语音（gated, 默认关）；只依赖 assistant（config/inbox_store/logger/_web_loop）+ 参数。
"""
from __future__ import annotations

import asyncio
from typing import Any  # noqa: F401


async def autosend_voice(assistant, platform, account_id, chat_key, text) -> bool:
    """全自动语音（gated, 默认关）：按策略把本条回复转 TTS
    语音经 orch.send_media 发出。返回 True=已作为语音发出；
    False=未发（未启用/不满足策略/合成或投递失败）→ 调用方回落文本。
    一处生效全平台（telegram/whatsapp/messenger/line/ig）。"""
    _cfg = assistant.config.config or {}
    from src.inbox.voice_autosend import (
        resolve_voice_autosend_cfg,
        decide_voice, stage_voice_file,
        record_voice_sent, record_voice_fallback,
        record_voice_decision,
        persona_allowed_for_voice,
    )
    _vb = resolve_voice_autosend_cfg(_cfg)
    if not _vb.get("enabled"):
        return False
    # 反双发：仅对**编排器管理**的账号发语音。原生 standalone
    # Telegram（camille_test）不归编排器 → owns_media=False →
    # 这里早退，让原生 voice_reply 独占（无双发）；编排器协议/
    # 官方号用裸 client + reply-hook，无原生语音 → System Z 接手。
    from src.integrations.account_orchestrator import (
        get_orchestrator as _go,
    )
    _orch = _go(_cfg)
    if not _orch.owns_media(platform, account_id):
        return False
    # 上下文信号采集：when_peer_voice 用 peer_voice;
    # smart 档额外用「频率 + 客户此刻情绪 + 危机 + 亲密度」做情境评分。
    # 一次 list_recent_messages 复用算 peer_voice + 频率 + 客户末条文本。
    _peer_voice = False
    _peer_text = ""
    _voice_ratio = 0.0
    _peer_emo = ""
    _peer_emo_int = -1.0
    _intimacy = 0.0
    _crisis_block = False
    try:
        from src.inbox.normalizer import conv_id as _cidf
        _st = getattr(assistant, "inbox_store", None)
        if _st is not None:
            _cid = _cidf(platform, account_id, chat_key)
            try:
                _win = int(
                    ((_vb.get("smart") or {}).get("recent_window"))
                    or 6)
            except Exception:
                _win = 6
            _recent = _st.list_recent_messages(
                _cid, limit=max(_win, 6)) or []
            # peer_voice + 客户末条入站文本（危机判定用）
            for _m in reversed(_recent):
                if str(_m.get("direction") or "in") == "in":
                    _peer_voice = str(
                        _m.get("media_type") or ""
                    ).lower() in ("voice", "audio")
                    _peer_text = str(_m.get("text") or "")
                    break
            # recent_voice_ratio：近窗口 outbound 语音占比（频率刹车，保证"克制"）
            _outs = [
                _m for _m in _recent
                if str(_m.get("direction") or "") == "out"][-_win:]
            if _outs:
                _vc = sum(
                    1 for _m in _outs
                    if str(_m.get("media_type") or "").lower()
                    in ("voice", "audio"))
                _voice_ratio = _vc / float(len(_outs))
            # 客户此刻情绪 + 亲密度代理（conversation_meta 落库）
            try:
                _cm = _st.get_conv_meta(_cid) or {}
                _peer_emo = str(_cm.get("last_emotion") or "")
                _peer_emo_int = float(
                    _cm.get("last_emotion_intensity", -1.0))
                # 亲密度弱代理：聊得越多越熟（真 intimacy 在 contacts
                # 子系统/可能未启用 → msg_count 归一近似，0~1）。
                _mc = float(_cm.get("msg_count") or 0)
                _intimacy = max(0.0, min(1.0, _mc / 50.0))
            except Exception:
                pass
            # 危机：对客户末条入站文本跑权威 detect_crisis（severe/
            # elevated → 不机械发语音，走安全网；比 last_risk 落库更准）。
            try:
                if _peer_text:
                    from src.utils.wellbeing_guard import (
                        detect_crisis as _dc,
                    )
                    _crisis_block = str(
                        (_dc(_peer_text) or {}).get("level")
                        or "none").lower() in (
                        "severe", "elevated")
            except Exception:
                _crisis_block = False
    except Exception:
        _peer_voice = False
    _vdec = decide_voice(
        _vb, text, peer_sent_voice=_peer_voice,
        recent_voice_ratio=_voice_ratio,
        peer_emotion=_peer_emo,
        peer_emotion_intensity=_peer_emo_int,
        intimacy=_intimacy,
        crisis_block=_crisis_block,
    )
    record_voice_decision(
        _vdec.send_voice, _vdec.reason)
    if not _vdec.send_voice:
        return False
    # 账号级人设（声音克隆 voice_profile 来源）。编排器
    # Telegram 协议号 meta 常无 persona_id（_pid 空）→ 用
    # 共享解析器按 meta.persona_id → meta.persona_ids[0] →
    # config[platform].persona_ids[0] 统一回退（根治复数/单数
    # 命名不匹配：sync 写 persona_ids 而旧代码读 persona_id →
    # 空 _real_pid → 灰度白名单误拦真声、回落纯文本的根因）。
    from src.ai.persona_voice import (
        resolve_account_persona_id as _rapi,
    )
    _pid = _rapi(_cfg, platform, account_id)
    # 解析真实人设（_pid 空时按 chat_key 绑定/默认回退），与
    # stage_voice_file 内部同口径（同 chat_key/account）。
    _real_pid = _pid
    try:
        from src.ai.persona_voice import (
            resolve_effective_voice_context as _revc,
        )
        _ctx0 = _revc(
            _cfg, persona_id=_pid or None,
            account_persona_id=_pid or None,
            chat_key=str(chat_key),
            contact_key=str(chat_key),
            platform=platform, account_id=account_id)
        _real_pid = str(
            _ctx0.get("persona_id") or _pid or "")
    except Exception:
        _real_pid = _pid
    # Phase2 人设级灰度白名单：名单非空时仅放行名单内人设发
    # 语音，名单外回落纯文本（正常回落，不计 fallback——未合成）。
    if not persona_allowed_for_voice(_vb, _real_pid):
        assistant.logger.info(
            "[autosend voice] 人设 %s 不在灰度白名单 → 回落"
            "文本 platform=%s acct=%s", _real_pid or "?",
            platform, account_id)
        return False
    # 至此策略已判定「该发语音」：合成/投递的成败计入指标。
    # P3：传 chat_key（端用户身份）→ 按会员档分层路由 TTS
    # 后端（VIP→旗舰，免费→降级省成本）；monetization 未就绪
    # → tier=None → 不路由（零行为变更）。
    _staged = await stage_voice_file(
        _cfg, platform, account_id, _real_pid, text,
        contact_key=str(chat_key))
    if not _staged:
        record_voice_fallback("synth_failed")
        assistant.logger.info(
            "[autosend voice] 合成失败回落文本 platform=%s acct=%s",
            platform, account_id)
        return False
    _local, _url = _staged

    async def _vcoro():
        # caption="" → 客户收纯语音；inbox_text=text →
        # 坐席台会话里显示「自动语音念了什么」(转写)。
        return await _orch.send_media(
            platform, account_id, chat_key,
            media_path=_local, media_url=_url,
            media_type="voice", caption="",
            inbox_text=text)

    _wl = getattr(assistant, "_web_loop", None)
    if _wl is not None and _wl.is_running():
        _vf = asyncio.run_coroutine_threadsafe(_vcoro(), _wl)
        _vres = await asyncio.wrap_future(_vf)
    else:
        _vres = await _vcoro()
    _ok = bool(
        isinstance(_vres, dict) and _vres.get("delivered"))
    if _ok:
        _dur = 0
        try:
            from src.client.voice_sender import (
                probe_audio_duration_ms as _probe,
            )
            _dur = int(_probe(_local) or 0)
        except Exception:
            _dur = 0
        record_voice_sent(_dur)
        assistant.logger.info(
            "[autosend voice] 已发语音 platform=%s acct=%s dur=%sms",
            platform, account_id, _dur)
    else:
        record_voice_fallback("deliver_failed")
        assistant.logger.info(
            "[autosend voice] 投递失败回落文本 platform=%s acct=%s",
            platform, account_id)
    return _ok


async def autosend_image(assistant, platform, account_id, chat_key, text) -> bool:
    """全自动「按需发图」（gated, 默认关）：客户最近一条在要图/命中关键词时，
    优先发人设注册相册(关键词/通用池, 图或视频, 秒发)，否则回落生成
    (自拍相册/openai、物体图 text2img)，经 orch.send_media 发出。返回
    True=已作为图/视频发出（跳过语音/文本）；False=未发→回落。
    一处生效全平台（telegram/whatsapp/messenger/line/ig）。"""
    _cfg = assistant.config.config or {}
    from src.inbox.image_autosend import (
        resolve_image_autosend_cfg, run_autosend_image,
    )
    _scfg = resolve_image_autosend_cfg(_cfg)
    if not _scfg.get("enabled", False):
        return False
    # 反双发：仅对**编排器管理**且支持发媒体的账号发图（与语音同口径）。
    from src.integrations.account_orchestrator import (
        get_orchestrator as _go,
    )
    _orch = _go(_cfg)
    if not _orch.owns_media(platform, account_id):
        return False
    # 客户最近一条入站文本（判要图）+ 近窗口历史（上下文抽主体）。
    _peer_text = ""
    _history: list = []
    try:
        from src.inbox.normalizer import conv_id as _cidf
        _st = getattr(assistant, "inbox_store", None)
        if _st is None:
            return False
        _cid = _cidf(platform, account_id, chat_key)
        _recent = _st.list_recent_messages(
            _cid, limit=12) or []
        for _m in _recent:
            _t = str(_m.get("text") or "")
            if _t:
                _history.append({
                    "role": "user" if str(
                        _m.get("direction") or "in") == "in"
                    else "assistant",
                    "content": _t})
        for _m in reversed(_recent):
            if (str(_m.get("direction") or "in") == "in"
                    and str(_m.get("text") or "")):
                _peer_text = str(_m.get("text"))
                break
    except Exception:
        return False
    if not _peer_text:
        return False
    # 账号级人设（相册分册 / 出图 prompt 来源），与语音同口径解析。
    from src.ai.persona_voice import (
        resolve_account_persona_id as _rapi,
    )
    _pid = _rapi(_cfg, platform, account_id)
    _real_pid = _pid
    try:
        from src.ai.persona_voice import (
            resolve_effective_voice_context as _revc,
        )
        _ctx0 = _revc(
            _cfg, persona_id=_pid or None,
            account_persona_id=_pid or None,
            chat_key=str(chat_key),
            contact_key=str(chat_key),
            platform=platform, account_id=account_id)
        _real_pid = str(
            _ctx0.get("persona_id") or _pid or "")
    except Exception:
        _real_pid = _pid
    # 物体图可选 LLM 精炼 prompt（heuristic 抽主体不稳时；仅生成回落用到）。
    _refine = None
    _ai = getattr(assistant, "ai_client", None)
    if (_scfg.get("contextual_images_llm_prompt", False)
            and _ai is not None):
        async def _refine():
            from src.ai.contextual_image import (
                build_llm_prompt_refine_instruction as _bi,
            )
            return await _ai.chat(
                _bi(_peer_text, _history))

    # 发送 marshalling：把 orch.send_media 投到 web loop（与语音同口径）。
    async def _send_fn(_mp, _mu, _mt, _cap, _inbox):
        async def _coro():
            return await _orch.send_media(
                platform, account_id, chat_key,
                media_path=_mp, media_url=_mu,
                media_type=_mt, caption=_cap,
                inbox_text=_inbox)
        _wl = getattr(assistant, "_web_loop", None)
        if _wl is not None and _wl.is_running():
            _f = asyncio.run_coroutine_threadsafe(
                _coro(), _wl)
            _res = await asyncio.wrap_future(_f)
        else:
            _res = await _coro()
        return bool(
            isinstance(_res, dict)
            and _res.get("delivered"))

    return await run_autosend_image(
        _cfg, platform, account_id, chat_key,
        _real_pid, _peer_text, _history,
        send_fn=_send_fn, ai_text=text,
        llm_refine=_refine)

def _is_desktop_account(platform, account_id) -> bool:
    """会话账号是否为内嵌「桌面/扩展」模式（无服务端 worker）。"""
    try:
        from src.integrations.account_registry import (
            get_account_registry as _gar,
        )
        _row = _gar().get(platform, account_id) or {}
        return str(_row.get("mode") or "") == "desktop"
    except Exception:
        return False


def build_autosend_translate_cb(assistant, web_app):
    """构造 AutosendWorker 出站自动翻译回调（投递前把 AI 回复译成客户语言）。

    从 main.py initialize() 原样抽出（行为不变）：未启用/装配失败返回 None。
    translation_service 懒取（worker 真正投递时早已挂到 web_app.state）。
    """
    try:
        from src.inbox.outbound_translate import (
            parse_outbound_translate_cfg as _parse_otx_cfg,
        )
        _otx_cfg = _parse_otx_cfg(assistant.config.config or {})
        if not _otx_cfg.get("enabled"):
            return None
        _otx_src = _otx_cfg.get("source_lang") or "zh"
        _otx_style = _otx_cfg.get("style") or "chat"

        async def _autosend_translate(item, _src=_otx_src, _style=_otx_style):
            from src.inbox.outbound_translate import (
                translate_outbound_text as _tot,
            )
            _ts = getattr(web_app.state, "translation_service", None)
            if _ts is None:
                return str(item.get("text", ""))
            return await _tot(
                item, translation_service=_ts,
                store=assistant.inbox_store,
                source_lang=_src, style=_style)

        assistant.logger.info(
            "AutosendWorker 出站自动翻译已启用（src=%s）", _otx_src)
        return _autosend_translate
    except Exception:
        assistant.logger.debug("出站自动翻译装配跳过", exc_info=True)
        return None


def build_autosend_callbacks(assistant, web_app, deliver_enabled):
    """构造 AutosendWorker 的 (send_callback, translate_callback)。

    从 main.py initialize() 原样抽出（行为不变）。deliver 编排「按需发图→语音→
    文本/桌面受控出站」三级投递；deliver_enabled=False 时 send_cb=None（仅 DB
    标记+审计，不发客户）。translate_cb 见 build_autosend_translate_cb。
    """
    send_cb = None
    if deliver_enabled:
        from types import SimpleNamespace as _SNS
        from src.inbox.channel_adapters import (
            send_via_adapters as _send_via,
            default_inbox_adapters as _dia,
        )
        _send_adapters = _dia()
        _send_shim = _SNS(app=web_app)
        _assistant_ref = assistant

        async def _try_autosend_voice(platform, account_id, chat_key, text):
            return await autosend_voice(_assistant_ref, platform, account_id, chat_key, text)

        async def _try_autosend_image(platform, account_id, chat_key, text):
            return await autosend_image(_assistant_ref, platform, account_id, chat_key, text)

        async def _autosend_deliver(
            platform, account_id, chat_key, text
        ):
            # 全自动「按需发图」优先（gated）：对方在要照片时先出图发出，
            # 成功即作为图片发出、跳过语音/文本；未启用/不满足/失败 → 继续走语音/文本。
            try:
                if await _try_autosend_image(
                    platform, account_id, chat_key, text
                ):
                    return {"ok": True, "delivered_as": "image"}
            except Exception:
                _assistant_ref.logger.debug(
                    "[autosend image] 失败，回落语音/文本", exc_info=True)
            # 全自动语音优先（gated）：成功即作为语音发出；
            # 未启用/不满足/失败 → 回落到下面的文本投递（零行为变更）。
            try:
                if await _try_autosend_voice(
                    platform, account_id, chat_key, text
                ):
                    return {"ok": True, "delivered_as": "voice"}
            except Exception:
                _assistant_ref.logger.debug(
                    "[autosend voice] 失败，回落文本", exc_info=True)
            # D4：桌面内嵌账号无服务端 worker，send_via_adapters 发不出去。
            # desktop_bridge 开启时把回复路由到「受控出站队列」——enqueue 内部
            # 先过 send-gate/kill-switch 闸门，通过才落队列，由桌面壳/扩展轮询
            # DOM 发送。被闸门拦截则返回 ok=False，让 worker 记 autosend_failed。
            try:
                _cfg = _assistant_ref.config.config or {}
                _br = ((_cfg.get("inbox", {}) or {}).get(
                    "l2_autosend", {}) or {}).get(
                    "desktop_bridge", {}) or {}
                if (_br.get("enabled", False)
                        and _is_desktop_account(platform, account_id)):
                    from src.inbox.desktop_outbound import (
                        get_desktop_outbound_queue as _gdoq,
                    )
                    from src.integrations.account_registry import (
                        get_account_registry as _gar2,
                    )
                    # 人审模式（review_mode）：命令落 held 等运营放行，
                    # 而非直接 pending 自动发。仍先过闸门（受控不变式）。
                    _review = bool(_br.get("review_mode", False))
                    _res = _gdoq().enqueue(
                        platform, account_id, chat_key, text,
                        config=_cfg, registry=_gar2(),
                        hold=_review,
                    )
                    if _res.get("enqueued"):
                        return {"ok": True,
                                "delivered_as": (
                                    "desktop_review"
                                    if _res.get("status") == "held"
                                    else "desktop_queued"),
                                "id": _res.get("id")}
                    return {"ok": False,
                            "error": "blocked:" + str(
                                _res.get("blocked") or "")}
            except Exception:
                _assistant_ref.logger.debug(
                    "[autosend desktop_bridge] 路由失败", exc_info=True)
            # AutosendWorker 跑在主 loop；而协议号(telegram/whatsapp
            # pyrogram/Baileys)的 worker 由编排器经 FastAPI startup
            # 钩子启动，活在「web 线程的 web_loop」上。直接在主 loop
            # await orch.send → client.send_message 会触发
            # "Future attached to a different loop"。故：编排器拥有
            # 该账号时，把整次投递调度到 web_loop 执行再跨线程取回。
            def _make_coro():
                return _send_via(
                    _send_shim, platform, account_id,
                    chat_key, text, _send_adapters,
                )
            _wl = getattr(_assistant_ref, "_web_loop", None)
            _orch_owns = False
            try:
                from src.integrations.account_orchestrator import (
                    get_orchestrator as _get_orch,
                )
                _orch_owns = _get_orch(
                    _assistant_ref.config.config or {}
                ).owns(platform, account_id)
            except Exception:
                _orch_owns = False
            if (_orch_owns and _wl is not None
                    and _wl.is_running()):
                _fut = asyncio.run_coroutine_threadsafe(
                    _make_coro(), _wl
                )
                return await asyncio.wrap_future(_fut)
            return await _make_coro()

        send_cb = _autosend_deliver
    translate_cb = build_autosend_translate_cb(assistant, web_app)
    return send_cb, translate_cb
