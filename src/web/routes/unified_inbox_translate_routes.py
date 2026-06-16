"""统一收件箱——翻译路由域（巨石拆分 slice 31 + slice 35，slice 40 合并）。

``register_translate_routes(app, *, api_auth)`` 挂载全部翻译端点：

- ``unified-inbox/translate``：通用文本翻译（含 ``target_lang:"auto"``）
- ``unified-inbox/translation-engines``：目标语引擎能力矩阵
- ``unified-inbox/translate-image`` / ``translate-voice`` / ``translate-message-media``

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫）。
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import Depends, Request

from src.ai.translation_service import normalize_lang
from src.web.routes.unified_inbox_services import (
    _get_translation_service,
    _inbox_store,
    _resolve_conv_language,
)

logger = logging.getLogger(__name__)


def _media_base_dirs(request: Request) -> list:
    """媒体解析白名单根目录（config.media.base_dirs）。仅在白名单内的文件可被读取。"""
    cm = getattr(request.app.state, "config_manager", None)
    try:
        full = getattr(cm, "config", None) or {}
        dirs = list((full.get("media") or {}).get("base_dirs") or [])
    except Exception:
        dirs = []
    return [str(d) for d in dirs if str(d or "").strip()]


def _remote_fetch_cfg(request: Request) -> dict:
    """config.media.remote_fetch（受控远程媒体下载，默认关）。"""
    cm = getattr(request.app.state, "config_manager", None)
    try:
        full = getattr(cm, "config", None) or {}
        return dict((full.get("media") or {}).get("remote_fetch") or {})
    except Exception:
        return {}


def _within_base_dirs(path: str, base_dirs: list) -> bool:
    """容纳检查：resolved 真实路径必须落在某个白名单根内（防路径穿越）。
    未配置白名单时放行（media_ref 来自我方 store/平台，非终端用户输入）。"""
    if not base_dirs:
        return True
    try:
        rp = os.path.realpath(path)
        for b in base_dirs:
            br = os.path.realpath(str(b))
            if rp == br or rp.startswith(br + os.sep):
                return True
    except Exception:
        return False
    return False


def _lookup_stored_media(request: Request, conversation_id: str, message_id: str):
    """从 store 按 message_id 取该消息的 (media_type, media_ref)。取不到返回 ('','')。"""
    store = _inbox_store(request)
    if store is None or not conversation_id:
        return "", ""
    try:
        rows = store.list_messages(conversation_id, limit=500)
    except Exception:
        return "", ""
    mid = str(message_id or "")
    for r in rows:
        if mid and str(r.get("platform_msg_id") or "") == mid:
            return str(r.get("media_type") or ""), str(r.get("media_ref") or "")
    return "", ""


def register_translate_routes(app, *, api_auth) -> None:
    """挂载全部翻译端点（文本 + 媒体集群）。"""

    @app.post("/api/unified-inbox/translate")
    async def api_unified_inbox_translate(request: Request, _=Depends(api_auth)):
        """通用翻译。

        P1-2（翻译单一真相源）：``target_lang`` 支持 ``"auto"``，由服务端用与 ``/send``
        完全相同的 ``_resolve_conv_language`` + ``normalize_lang`` 解析客户语言，
        消除「预览在前端解析 vs 一击在后端解析」的 drift。需随 body 传 platform/
        account_id/chat_key 以定位会话。返回 ``resolved_target`` 告知实际目标语；
        ``"auto"`` 无法解析（客户语言 unknown）时返回 resolved_target="" 且不翻译，
        前端据此回落「按原文发送」。
        """
        body = await request.json()
        text = str(body.get("text") or "")
        target_lang = str(body.get("target_lang") or "zh").strip()
        source_lang = normalize_lang(str(body.get("source_lang") or ""))
        style = str(body.get("style") or "chat")

        if target_lang.lower() == "auto":
            target_lang = _resolve_conv_language(
                request,
                str(body.get("platform") or "").lower(),
                str(body.get("account_id") or "default"),
                str(body.get("chat_key") or ""),
            )
        else:
            target_lang = normalize_lang(target_lang)

        if not target_lang:
            return {
                "ok": False,
                "resolved_target": "",
                "translation": {
                    "ok": False, "translated_text": text, "original_text": text,
                    "target_lang": "", "source_lang": source_lang or "",
                    "provider": "none", "error": "auto_unresolved",
                },
            }

        svc = _get_translation_service(request)
        result = await svc.translate(
            text,
            target_lang=target_lang,
            source_lang=source_lang,
            style=style,
        )
        return {"ok": result.ok, "resolved_target": target_lang, "translation": result.to_dict()}

    @app.get("/api/unified-inbox/translation-engines")
    async def api_unified_inbox_translation_engines(
        request: Request, target_lang: str = "zh", _=Depends(api_auth)
    ):
        """指定目标语的引擎能力矩阵：让坐席在切换目标语时即知主引擎是否兜底。"""
        svc = _get_translation_service(request)
        return {"ok": True, "matrix": svc.engine_matrix(target_lang)}

    @app.post("/api/unified-inbox/translate-image")
    async def api_unified_inbox_translate_image(request: Request, _=Depends(api_auth)):
        """P58：图片 OCR → 翻译。前端传 base64 图片，返回逐字 OCR 文本 + 译文。"""
        import os as _os

        from src.ai.image_translate import (
            ImageTranslateService,
            build_vision_ocr_fn,
            decode_image_to_temp,
        )

        body = await request.json()
        image_b64 = str(body.get("image_b64") or "")
        target_lang = str(body.get("target_lang") or "zh")
        source_lang = str(body.get("source_lang") or "")
        style = str(body.get("style") or "chat")

        cm = getattr(request.app.state, "config_manager", None)
        vision_cfg = {}
        try:
            full = getattr(cm, "config", None) or {}
            vision_cfg = dict(full.get("vision") or {})
        except Exception:
            vision_cfg = {}
        if not vision_cfg.get("enabled", False):
            return {"ok": False, "reason": "vision_disabled",
                    "message": "图像识别未启用（config.vision.enabled）"}

        try:
            from src.vision_client import has_any_vision_backend
            if not has_any_vision_backend(vision_cfg, vision_cfg):
                return {"ok": False, "reason": "no_vision_backend",
                        "message": "未配置可用的图像识别后端（Ollama base_url 或智谱 api_key）"}
        except Exception:
            pass

        path, reason = decode_image_to_temp(image_b64)
        if path is None:
            return {"ok": False, "reason": reason, "message": f"图片无效：{reason}"}
        try:
            svc = ImageTranslateService(
                _get_translation_service(request),
                build_vision_ocr_fn(vision_cfg, vision_cfg),
            )
            return await svc.translate_image(
                path, target_lang=target_lang, source_lang=source_lang, style=style,
            )
        finally:
            try:
                _os.remove(path)
            except Exception:
                pass

    @app.post("/api/unified-inbox/translate-voice")
    async def api_unified_inbox_translate_voice(request: Request, _=Depends(api_auth)):
        """P58-2：语音转写(ASR) → 翻译。前端传 base64 音频，返回转写文本 + 译文。"""
        import os as _os

        from src.ai.voice_translate import (
            VoiceTranslateService,
            build_audio_transcribe_fn,
            decode_audio_to_temp,
        )

        body = await request.json()
        audio_b64 = str(body.get("audio_b64") or "")
        target_lang = str(body.get("target_lang") or "zh")
        source_lang = str(body.get("source_lang") or "")
        style = str(body.get("style") or "chat")

        cm = getattr(request.app.state, "config_manager", None)
        audio_cfg = {}
        try:
            full = getattr(cm, "config", None) or {}
            audio_cfg = dict(full.get("audio_pipeline") or {})
        except Exception:
            audio_cfg = {}
        if not audio_cfg.get("enabled", False):
            return {"ok": False, "reason": "asr_disabled",
                    "message": "语音转写未启用（config.audio_pipeline.enabled）"}

        path, reason = decode_audio_to_temp(audio_b64)
        if path is None:
            return {"ok": False, "reason": reason, "message": f"音频无效：{reason}"}
        try:
            svc = VoiceTranslateService(
                _get_translation_service(request),
                build_audio_transcribe_fn(audio_cfg),
            )
            return await svc.translate_voice(
                path, target_lang=target_lang, source_lang=source_lang, style=style,
            )
        finally:
            try:
                _os.remove(path)
            except Exception:
                pass

    @app.post("/api/unified-inbox/translate-message-media")
    async def api_unified_inbox_translate_message_media(request: Request, _=Depends(api_auth)):
        """P61-2：会话内媒体一键翻译（可解析则免上传）。"""
        from src.inbox.media_resolver import resolve_for_translate

        body = await request.json()
        conversation_id = str(body.get("conversation_id") or "")
        message_id = str(body.get("message_id") or "")
        target_lang = str(body.get("target_lang") or "zh")
        source_lang = str(body.get("source_lang") or "")
        style = str(body.get("style") or "chat")

        media_type, media_ref = _lookup_stored_media(request, conversation_id, message_id)
        if not media_ref:
            media_ref = str(body.get("media_ref") or "")
            media_type = media_type or str(body.get("media_type") or "")

        base_dirs = _media_base_dirs(request)
        try:
            from src.integrations.protocol_bridge import (
                protocol_media_root, static_media_ref_to_path,
            )
            _local = static_media_ref_to_path(media_ref)
            if _local:
                media_ref = _local
                base_dirs = base_dirs + [str(protocol_media_root())]
        except Exception:
            logger.debug("protocol 媒体路径映射失败", exc_info=True)

        message = {"media_type": media_type, "media_ref": media_ref}
        path, kind, reason = resolve_for_translate(message, base_dirs=base_dirs)

        _tmp_download: Optional[str] = None
        if reason == "remote_unsupported":
            _rf = _remote_fetch_cfg(request)
            if _rf.get("enabled", False):
                from src.inbox.media_fetch import fetch_remote_media
                _dl_path, _dl_reason = await fetch_remote_media(
                    media_ref,
                    kind=kind,
                    max_bytes=int(_rf.get("max_mb", 10) or 10) * 1024 * 1024,
                    timeout_sec=float(_rf.get("timeout_sec", 8) or 8),
                    allow_domains=list(_rf.get("allow_domains") or []),
                )
                if _dl_path:
                    path, reason, _tmp_download = _dl_path, "ok", _dl_path
                else:
                    return {"ok": False, "reason": _dl_reason, "fallback": "upload",
                            "message": "远程媒体下载失败，请上传文件"}

        if reason != "ok":
            msg = {
                "no_ref": "该消息无媒体引用",
                "remote_unsupported": "媒体为远程链接，暂不支持免上传翻译，请上传文件",
                "not_found": "未找到本地媒体文件，请上传文件",
                "unsupported_kind": "暂不支持该媒体类型翻译",
            }.get(reason, reason)
            return {"ok": False, "reason": reason, "fallback": "upload", "message": msg}

        if _tmp_download is None and not _within_base_dirs(path, base_dirs):
            return {"ok": False, "reason": "outside_base_dirs", "fallback": "upload",
                    "message": "媒体文件不在允许目录内"}

        try:
            if kind == "image":
                from src.ai.image_translate import ImageTranslateService, build_vision_ocr_fn
                cm = getattr(request.app.state, "config_manager", None)
                try:
                    vision_cfg = dict((getattr(cm, "config", None) or {}).get("vision") or {})
                except Exception:
                    vision_cfg = {}
                if not vision_cfg.get("enabled", False):
                    return {"ok": False, "reason": "vision_disabled",
                            "message": "图像识别未启用（config.vision.enabled）"}
                try:
                    from src.vision_client import has_any_vision_backend
                    if not has_any_vision_backend(vision_cfg, vision_cfg):
                        return {"ok": False, "reason": "no_vision_backend",
                                "message": "未配置可用的图像识别后端"}
                except Exception:
                    pass
                svc = ImageTranslateService(
                    _get_translation_service(request),
                    build_vision_ocr_fn(vision_cfg, vision_cfg),
                )
                out = await svc.translate_image(
                    path, target_lang=target_lang, source_lang=source_lang, style=style,
                )
                out["media_kind"] = "image"
                out["from_upload"] = False
                out["from_remote"] = _tmp_download is not None
                return out

            from src.ai.voice_translate import VoiceTranslateService, build_audio_transcribe_fn
            cm = getattr(request.app.state, "config_manager", None)
            try:
                audio_cfg = dict((getattr(cm, "config", None) or {}).get("audio_pipeline") or {})
            except Exception:
                audio_cfg = {}
            if not audio_cfg.get("enabled", False):
                return {"ok": False, "reason": "asr_disabled",
                        "message": "语音转写未启用（config.audio_pipeline.enabled）"}
            svc = VoiceTranslateService(
                _get_translation_service(request),
                build_audio_transcribe_fn(audio_cfg),
            )
            out = await svc.translate_voice(
                path, target_lang=target_lang, source_lang=source_lang, style=style,
            )
            out["media_kind"] = "voice"
            out["from_upload"] = False
            out["from_remote"] = _tmp_download is not None
            return out
        finally:
            if _tmp_download:
                try:
                    os.unlink(_tmp_download)
                except Exception:
                    pass
