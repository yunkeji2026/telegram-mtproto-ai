"""统一收件箱——消息/媒体/语音发送写路径路由域（巨石拆分 slice 30）。

把 ``register_unified_inbox_routes`` 巨型闭包中的核心**写路径**子域整体外移为
``register_send_routes(app, *, api_auth, page_auth)``，由主 register 在**原位置**调用：

- ``unified-inbox/send``：文本发送（含发送前 outbound 翻译闭环 + 漏斗埋点 + 坐席首响归属）
- ``unified-inbox/send-media``：multipart 媒体发送（protocol 多开账号）
- ``unified-inbox/send-voice``：文本→TTS（可声音克隆）→OGG/Opus 语音消息发送
- ``unified-inbox/send-caps``：媒体/语音直发能力探测（供前端按钮置灰）

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 30 端点契约断言）。

闭包级依赖经子注册显式入参：``page_auth``（send/media/voice）+ ``api_auth``（caps）。
依赖全部朝下：services.(_inbox_store/_get_translation_service/_resolve_conv_language)、
auth._session_agent、context._record_copilot_adopt_from_send、channel_adapters、
aggregate._INBOX_ADAPTERS；翻译/编排器/媒体落盘/TTS/转码均为 handler 内或顶部 import。
send 内的 ``_mark_send`` 为请求级嵌套闭包（坐席首响归属打点），随 send handler 保留。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request

from src.ai.translation_service import normalize_lang
from src.inbox.channel_adapters import ChannelSendError, send_via_adapters
from src.inbox.normalizer import conv_id as _conv_id
from src.web.routes.unified_inbox_aggregate import _INBOX_ADAPTERS
from src.web.routes.unified_inbox_auth import _session_agent
from src.web.routes.unified_inbox_context import _record_copilot_adopt_from_send
from src.web.routes.unified_inbox_services import (
    _get_translation_service,
    _inbox_store,
    _resolve_conv_engine,
    _resolve_conv_language,
)

logger = logging.getLogger(__name__)


def _account_removed(platform: str, account_id: str) -> bool:
    """该 (platform, account_id) 是否为注册表里 status=removed 的账号。

    已移除账号在收件箱里只读展示历史（见 ProtocolInboxAdapter）；其会话禁止发送。
    注意：实时 Telegram A 线用 account_id='default'（不在注册表），故不会被误拦；
    仅明确指向 removed 账号 id（如 8118214990/tg-desktop）的发送被拒。查不到一律放行。
    """
    if not platform or not account_id or account_id == "default":
        return False
    try:
        from src.integrations.account_registry import get_account_registry
        row = get_account_registry().get(platform, account_id)
        return bool(row and row.get("status") == "removed")
    except Exception:
        return False


def register_send_routes(app, *, api_auth, page_auth) -> None:
    """挂载文本/媒体/语音发送 + 直发能力探测端点。"""

    @app.post("/api/unified-inbox/send")
    async def api_unified_inbox_send(request: Request, _=Depends(page_auth)):
        """向指定平台/账号发送消息（可选发送前自动翻译成客户语言）。

        Body: { platform, account_id, chat_key, text,
                target_lang?, source_lang?, skip_translate?, copilot_meta? }

        发送前翻译（outbound 闭环）：
        - 不传 target_lang（或 skip_translate=true）→ 行为不变，按原文发送（向后兼容）。
        - target_lang="auto" → 用会话持久化的客户语言（conversations.language）。
        - target_lang 具体语种 → 译成该语言。
        翻译失败 / 目标==源 / 目标为空 → best-effort 回落原文，绝不阻断发送。
        返回额外字段 original_text / sent_text / translation 供前端展示「发出的实际译文」。
        """
        body = await request.json()
        platform = str(body.get("platform") or "").lower()
        account_id = str(body.get("account_id") or "default")
        chat_key = str(body.get("chat_key") or "")
        text = str(body.get("text") or "").strip()
        if not chat_key or not text:
            raise HTTPException(400, "chat_key 和 text 不能为空")
        if _account_removed(platform, account_id):
            raise HTTPException(409, "该账号已从系统移除，仅可查看历史记录，无法发送")

        # —— 发送前翻译（outbound 闭环）：默认关闭，显式 target_lang / "auto" 才触发 ——
        original_text = text
        translation_info: Optional[Dict[str, Any]] = None
        target_lang = str(body.get("target_lang") or "").strip()
        source_lang = str(body.get("source_lang") or "").strip()
        skip_translate = bool(body.get("skip_translate"))
        # P1-4：出向翻译漏斗埋点用——原始请求语言/auto 标志/失败标志（best-effort）
        _raw_target = target_lang
        _is_auto = target_lang.lower() == "auto"
        _xlate_failed = False
        if _is_auto:
            target_lang = _resolve_conv_language(request, platform, account_id, chat_key)
        else:
            # 显式语种也归一（zh-cn→zh 等），避免前端/集成方传入非规范码导致守卫误判。
            target_lang = normalize_lang(target_lang)
        source_lang = normalize_lang(source_lang)
        if (
            target_lang
            and not skip_translate
            and target_lang.lower() not in ("unknown", source_lang.lower())
        ):
            try:
                svc = _get_translation_service(request)
                # F+：会话首选引擎（多线路对照择优后记住的）优先；无 → 现有 failover
                _pref_engine = _resolve_conv_engine(request, platform, account_id, chat_key)
                res = await svc.translate(
                    text, target_lang=target_lang, source_lang=source_lang,
                    style="chat", engine=_pref_engine,
                )
                if res.ok and (res.translated_text or "").strip():
                    text = res.translated_text.strip()
                    translation_info = res.to_dict()
                elif not res.ok:
                    _xlate_failed = True
            except Exception:
                _xlate_failed = True
                logger.debug("[send] 发送前翻译失败，按原文发送", exc_info=True)

        _send_agent = _session_agent(request)

        def _mark_send(cid: str) -> None:
            """发送成功后打坐席首响归属点（best-effort，失败不影响发送）。"""
            ibx = _inbox_store(request)
            if ibx is None or not cid:
                return
            try:
                ibx.record_agent_send(
                    cid, _send_agent["agent_id"],
                    agent_name=_send_agent.get("display_name", ""))
            except Exception:
                logger.debug("record_agent_send 失败（已忽略）", exc_info=True)
            # Phase 6：坐席接管发出消息 → 清除自动回复打的「需人工」标签（闭环收口）
            try:
                from src.integrations.protocol_autoreply import clear_needs_human
                clear_needs_human(ibx, cid)
            except Exception:
                logger.debug("清除 needs-human 标签失败（已忽略）", exc_info=True)

        # A2 写路径收尾：发送收敛到各渠道适配器（与 collect/status 对称）。
        # 跨切面（坐席首响归属打点）统一留在路由，按 result.conversation_id 归属。
        try:
            result = await send_via_adapters(
                request, platform, account_id, chat_key, text, _INBOX_ADAPTERS,
            )
        except ChannelSendError as ex:
            raise HTTPException(ex.status_code, ex.detail)
        cid = (result.get("conversation_id") if isinstance(result, dict) else None) \
            or _conv_id(platform, account_id, chat_key)
        _mark_send(cid)
        # P1：发生发送前翻译时，旁路记录「实发译文 → 中文原文/质量」，供 /thread 富集出向双行
        # （跨刷新/重启/设备持久；不触碰 messages 去重）。best-effort，失败不影响发送。
        if translation_info and cid:
            _ibx_xl = _inbox_store(request)
            if _ibx_xl is not None:
                try:
                    _ibx_xl.record_outbound_translation(
                        cid, sent_text=text, original_text=original_text,
                        source_lang=source_lang, target_lang=target_lang,
                        provider=str(translation_info.get("provider") or ""),
                        error=str(translation_info.get("error") or ""),
                    )
                except Exception:
                    logger.debug("record_outbound_translation 失败（已忽略）", exc_info=True)
        copilot_meta = body.get("copilot_meta")
        if copilot_meta and cid:
            ibx = _inbox_store(request)
            # copilot 采纳记录用坐席原始输入（而非译文），保持「坐席选了哪条草稿」语义。
            _record_copilot_adopt_from_send(
                ibx, cid, _send_agent["agent_id"], original_text, copilot_meta,
            )
        # P1-4：出向翻译漏斗埋点（覆盖率/auto 解析失败率/降级率；best-effort，绝不影响发送）
        try:
            from src.ai.outbound_translation_stats import get_outbound_translation_stats
            _translated = translation_info is not None
            _degraded = bool(
                _translated and (
                    str(translation_info.get("provider") or "").lower() in ("none", "identity")
                    or str(translation_info.get("error") or "")
                )
            )
            _funnel_kw = dict(
                requested=(bool(_raw_target) and not skip_translate),
                is_auto=_is_auto,
                auto_resolved=(bool(target_lang) if _is_auto else None),
                translated=_translated,
                target_lang=target_lang,
                degraded=_degraded,
                failed=_xlate_failed,
            )
            get_outbound_translation_stats().record_send(**_funnel_kw)
            # P3：同口径持久化进按日表，供经理看板按 7/30 日窗读取（跨重启 + 趋势）
            _ibx_fn = _inbox_store(request)
            if _ibx_fn is not None and hasattr(_ibx_fn, "record_outbound_xlate"):
                _ibx_fn.record_outbound_xlate(**_funnel_kw)
        except Exception:
            logger.debug("出向翻译漏斗埋点失败（已忽略）", exc_info=True)
        return {
            "ok": True,
            "result": result,
            "original_text": original_text,
            "sent_text": text,
            "translation": translation_info,
        }

    @app.post("/api/unified-inbox/send-media")
    async def api_unified_inbox_send_media(request: Request, _=Depends(page_auth)):
        """M6⑥：坐席从收件箱发送媒体（图片/语音/视频/文件）。

        multipart: file + platform/account_id/chat_key/caption。
        仅 protocol 账号（编排器接管、在线）支持；其它平台返回 501（走各自 RPA 发送）。
        发送成功后媒体以 /static URL 回写线程，坐席侧立即可见。
        """
        form = await request.form()
        platform = str(form.get("platform") or "").lower()
        account_id = str(form.get("account_id") or "default")
        chat_key = str(form.get("chat_key") or "")
        caption = str(form.get("caption") or "")
        upload = form.get("file")
        if not chat_key or upload is None or not getattr(upload, "filename", ""):
            raise HTTPException(400, "file 和 chat_key 不能为空")
        if _account_removed(platform, account_id):
            raise HTTPException(409, "该账号已从系统移除，仅可查看历史记录，无法发送")

        from src.integrations.account_orchestrator import get_orchestrator
        orch = get_orchestrator()
        if not orch.owns_media(platform, account_id):
            raise HTTPException(501, "该账号不支持从收件箱发送媒体（需 protocol 多开且在线）")

        data = await upload.read()
        if not data:
            raise HTTPException(400, "空文件")
        if len(data) > 25 * 1024 * 1024:
            raise HTTPException(413, "文件过大（上限 25MB）")

        from src.integrations.protocol_bridge import save_outbound_media
        local, url, mtype = save_outbound_media(
            platform, account_id, upload.filename, data)
        _send_agent = _session_agent(request)
        try:
            res = await orch.send_media(
                platform, account_id, chat_key,
                media_path=local, media_url=url, media_type=mtype, caption=caption)
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(502, f"媒体发送失败: {ex}")
        cid = _conv_id(platform, account_id, chat_key)
        try:
            ibx = _inbox_store(request)
            if ibx is not None:
                ibx.record_agent_send(
                    cid, _send_agent["agent_id"],
                    agent_name=_send_agent.get("display_name", ""))
        except Exception:
            logger.debug("record_agent_send(media) 失败", exc_info=True)
        return {"ok": True, "result": res, "media_ref": url, "media_type": mtype}

    @app.post("/api/unified-inbox/send-voice")
    async def api_unified_inbox_send_voice(request: Request, _=Depends(page_auth)):
        """坐席发送语音回复：回复文本 → （可声音克隆）TTS 合成 → 作为语音消息发送。

        Body: { platform, account_id, chat_key, text, persona_id?, caption?, voice_cfg_override? }
        声音克隆复用 voice_profile（telegram.voice_reply / personas.*.voice_profile，
        backend=voice_clone_command/coqui_http 等）；合成后转 OGG/Opus 以"语音消息"形态发出。
        仅 protocol 账号（编排器接管、在线）支持；其它平台返回 501（走各自 RPA voice_output）。
        """
        body = await request.json()
        platform = str(body.get("platform") or "").lower()
        account_id = str(body.get("account_id") or "default")
        chat_key = str(body.get("chat_key") or "")
        text = str(body.get("text") or "").strip()
        persona_id = body.get("persona_id") or None
        caption = str(body.get("caption") or "")
        cfg_override = body.get("voice_cfg_override")
        if not chat_key or not text:
            raise HTTPException(400, "chat_key 和 text 不能为空")
        if len(text) > 1000:
            raise HTTPException(400, "文本过长（语音上限 1000 字）")
        if _account_removed(platform, account_id):
            raise HTTPException(409, "该账号已从系统移除，仅可查看历史记录，无法发送")

        from src.integrations.account_orchestrator import get_orchestrator
        orch = get_orchestrator()
        if not orch.owns_media(platform, account_id):
            raise HTTPException(501, "该账号不支持从收件箱发送语音（需 protocol 多开且在线）")

        # 解析语音配置（含声音克隆 voice_profile），允许调用方临时覆盖
        cm = getattr(request.app.state, "config_manager", None)
        raw_cfg = (getattr(cm, "config", None) or {}) if cm else {}
        from src.ai.persona_voice import resolve_voice_cfg_for_contact
        voice_cfg = resolve_voice_cfg_for_contact(
            persona_id, raw_cfg, contact_key=chat_key or None)
        if isinstance(cfg_override, dict):
            voice_cfg.update({k: v for k, v in cfg_override.items() if v not in (None, "")})
        voice_cfg["enabled"] = True

        # 合成到临时目录
        import tempfile
        from pathlib import Path as _Path
        out_dir = _Path(tempfile.gettempdir()) / "unified_voice_send"
        voice_cfg["out_dir"] = str(out_dir)
        from src.ai.tts_pipeline import TTSPipeline
        from src.ai.persona_voice import resolve_emotion_for_send
        try:
            tts = TTSPipeline(voice_cfg)
            # P4：情感层（默认关）。开启后按关系阶段/文本线索派生情绪。
            _emotion = resolve_emotion_for_send(
                voice_cfg, text, platform=platform,
                account_id=account_id, chat_key=chat_key or None)
            result = await tts.synthesize(
                text, timeout_sec=45.0, emotion=_emotion)
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(502, f"语音合成失败: {ex}")
        if not result.ok or not result.audio_path:
            return {"ok": False, "reason": result.error or "tts_failed",
                    "message": f"语音合成失败：{result.error or 'unknown'}"}

        # 转 OGG/Opus，使其在 Telegram/WhatsApp 呈现为"语音消息"（ffmpeg 缺失则原样发）
        audio_path = result.audio_path
        try:
            from src.client.voice_sender import convert_to_ogg_opus
            converted = await asyncio.to_thread(convert_to_ogg_opus, audio_path, delete_src=True)
            if converted:
                audio_path = converted
        except Exception:
            logger.debug("OGG 转码失败，按原格式发送", exc_info=True)

        # 落到出站媒体目录（线程回写可见）+ 发送（强制 media_type=voice）
        try:
            with open(audio_path, "rb") as fh:
                data = fh.read()
            from src.integrations.protocol_bridge import save_outbound_media
            local, url, _mt = save_outbound_media(
                platform, account_id, os.path.basename(audio_path), data)
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(502, f"语音落盘失败: {ex}")
        finally:
            try:
                os.remove(audio_path)
            except Exception:
                pass

        _send_agent = _session_agent(request)
        try:
            res = await orch.send_media(
                platform, account_id, chat_key,
                media_path=local, media_url=url, media_type="voice", caption=caption,
                inbox_text=text)
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(502, f"语音发送失败: {ex}")
        cid = _conv_id(platform, account_id, chat_key)
        try:
            ibx = _inbox_store(request)
            if ibx is not None:
                ibx.record_agent_send(
                    cid, _send_agent["agent_id"],
                    agent_name=_send_agent.get("display_name", ""))
        except Exception:
            logger.debug("record_agent_send(voice) 失败", exc_info=True)
        return {
            "ok": True, "result": res, "media_ref": url, "media_type": "voice",
            "duration_sec": getattr(result, "duration_sec", -1.0),
            "provider": getattr(result, "provider", ""),
            "voice": getattr(result, "voice", ""),
        }

    @app.get("/api/unified-inbox/send-caps")
    async def api_unified_inbox_send_caps(
        request: Request, platform: str = "", account_id: str = "default",
    ):
        """返回指定平台/账号是否支持从收件箱直发媒体/语音（protocol 多开且在线）。

        供前端事先置灰媒体/语音按钮 + tooltip 说明，把「点了才撞 501」改为「事先可知」。
        探测失败一律按「不支持」处理（保守，点击仍有 501 兜底）。
        """
        api_auth(request)
        plat = str(platform or "").lower()
        acc = str(account_id or "default")
        can = False
        try:
            from src.integrations.account_orchestrator import get_orchestrator
            can = bool(get_orchestrator().owns_media(plat, acc))
        except Exception:
            logger.debug("send-caps 探测失败（按不支持处理）", exc_info=True)
            can = False
        return {
            "ok": True, "platform": plat, "account_id": acc,
            "can_media": can, "can_voice": can,
        }
