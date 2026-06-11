"""Unified voice / TTS API routes.

Endpoints
---------
POST /api/voice/tts-test
    Generate a TTS preview for a given persona + text.
    Body: {text, persona_id?, format?}
    Returns: {ok, url, duration_sec, provider, voice, error?}

GET  /api/voice/tts-test/{filename}
    Serve the generated preview audio file (short-lived).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

logger = logging.getLogger(__name__)

_TTS_PREVIEW_DIR = Path("tmp_tts_preview")
_TTS_PREVIEW_TTL_SEC = 600  # files older than 10 min are cleaned up


_TTS_PREVIEW_PREFIXES = ("ttspreview-", "line-tts-")
# P9-D: per-prefix TTL — approval queue files survive longer than quick UI tests
_TTS_PREVIEW_TTL_BY_PREFIX: Dict[str, float] = {
    "ttspreview-": _TTS_PREVIEW_TTL_SEC,   # 10 min: UI test previews
    "line-tts-":   7200.0,                  # 2 h: LINE approval queue
}


def _cleanup_old_previews() -> None:
    """Remove preview files older than per-prefix TTL (best-effort)."""
    try:
        now = time.time()
        for f in _TTS_PREVIEW_DIR.iterdir():
            if not f.is_file():
                continue
            ttl = next(
                (v for k, v in _TTS_PREVIEW_TTL_BY_PREFIX.items() if f.name.startswith(k)),
                None,
            )
            if ttl is None:
                continue
            try:
                st = f.stat()
                # P11-B: 任何 < 512B 的孤儿文件（截断/静默失败）直接清除，不受 TTL 约束
                if st.st_size < 512 or st.st_mtime < now - ttl:
                    f.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


def register_voice_routes(app, api_auth, config_manager=None):
    """Register /api/voice/* endpoints on *app*."""

    @app.post("/api/voice/tts-test")
    async def api_voice_tts_test(request: Request, _=Depends(api_auth)):
        """Generate a TTS audio preview.

        Request body (JSON):
            text        — text to synthesise (required)
            persona_id  — persona ID for voice config lookup (optional)
            format      — output format override: mp3|ogg (optional)
        """
        try:
            body: Dict[str, Any] = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON body")

        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text is required")
        if len(text) > 400:
            raise HTTPException(400, "text too long (max 400 chars)")

        persona_id: Optional[str] = body.get("persona_id") or None
        fmt_override: Optional[str] = body.get("format") or None
        # Optional: caller passes current UI settings to override saved config
        cfg_override: Optional[Dict[str, Any]] = body.get("voice_cfg_override") or None

        # Resolve voice config
        raw_cfg: Dict[str, Any] = {}
        if config_manager and hasattr(config_manager, "config"):
            raw_cfg = config_manager.config or {}

        try:
            from src.ai.persona_voice import resolve_voice_cfg
            voice_cfg = resolve_voice_cfg(persona_id, raw_cfg)
        except Exception as ex:
            logger.warning("[voice/tts-test] resolve_voice_cfg failed: %s", ex)
            voice_cfg = {}

        # Apply caller override (allows previewing unsaved UI settings)
        if isinstance(cfg_override, dict):
            voice_cfg.update({k: v for k, v in cfg_override.items() if v not in (None, "")})
            logger.debug("[voice/tts-test] override keys: %s", list(cfg_override))

        voice_cfg["enabled"] = True
        if fmt_override:
            voice_cfg["format"] = fmt_override.strip().lower()

        # Redirect output to preview dir
        _TTS_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        uid = uuid.uuid4().hex[:10]
        suffix = voice_cfg.get("format", "mp3")
        preview_path = _TTS_PREVIEW_DIR / f"ttspreview-{uid}.{suffix}"
        voice_cfg["out_dir"] = str(_TTS_PREVIEW_DIR)

        try:
            from src.ai.tts_pipeline import TTSPipeline
            tts = TTSPipeline(voice_cfg)
            # Override out path so we control filename
            import asyncio as _aio
            result = await _aio.wait_for(
                tts.synthesize(text, timeout_sec=45.0),
                timeout=50.0,
            )
        except Exception as ex:
            logger.error("[voice/tts-test] TTS error: %s", ex)
            return {"ok": False, "error": str(ex)[:200]}

        if not result.ok:
            return {"ok": False, "error": result.error}

        # Rename to our deterministic preview path
        try:
            Path(result.audio_path).rename(preview_path)
        except Exception:
            preview_path = Path(result.audio_path)

        # Async cleanup of old files (non-blocking)
        try:
            _cleanup_old_previews()
        except Exception:
            pass

        file_url = f"/api/voice/tts-test/{preview_path.name}"
        return {
            "ok": True,
            "url": file_url,
            "filename": preview_path.name,
            "duration_sec": result.duration_sec,
            "provider": result.provider,
            "voice": result.voice,
            "format": result.format,
            "bytes": preview_path.stat().st_size if preview_path.is_file() else 0,
        }

    @app.get("/api/voice/profiles")
    async def api_voice_profiles(request: Request, _=Depends(api_auth)):
        """列出可用「语音音色」供收件箱按人设选择：全局默认 + 各人设的克隆/音色。

        返回：{ok, default:{backend,voice,is_clone,ready}, profiles:[{persona_id,name,backend,voice,is_clone,ready}]}
        - is_clone：voice_profile 启用且后端为 voice_clone_command/coqui_http（声音克隆）
        - ready：克隆音色但缺 owner_consent/参考音频时为 False（前端可标灰/提示）
        """
        raw_cfg: Dict[str, Any] = {}
        if config_manager and hasattr(config_manager, "config"):
            raw_cfg = config_manager.config or {}
        from src.ai.persona_voice import resolve_voice_cfg

        clone_backends = {"voice_clone_command", "coqui_http"}

        def _describe(cfg: Dict[str, Any]) -> Dict[str, Any]:
            vp = cfg.get("voice_profile") if isinstance(cfg.get("voice_profile"), dict) else {}
            vp_on = bool(vp.get("enabled"))
            backend = str((vp.get("backend") if vp_on else None) or cfg.get("backend") or "edge_tts").lower()
            voice = str((vp.get("speaker_id") if vp_on else None) or cfg.get("voice") or "")
            is_clone = vp_on and backend in clone_backends
            ready = True
            if is_clone:
                ref = str(vp.get("reference_audio_path") or "").strip()
                ready = bool(vp.get("owner_consent")) and bool(ref)
            return {"backend": backend, "voice": voice, "is_clone": is_clone, "ready": ready}

        default_desc = _describe(resolve_voice_cfg(None, raw_cfg))
        profiles = []
        try:
            from src.utils.persona_manager import PersonaManager
            pm = PersonaManager.get_instance()
            for s in pm.list_profiles_summary():
                if not s.get("has_voice"):
                    continue
                pid = s.get("id")
                desc = _describe(resolve_voice_cfg(pid, raw_cfg))
                profiles.append({"persona_id": pid, "name": s.get("name") or pid, **desc})
        except Exception as ex:  # noqa: BLE001
            logger.warning("[voice/profiles] persona enumerate failed: %s", ex)
        return {"ok": True, "default": default_desc, "profiles": profiles}

    def _dashscope_creds() -> tuple:
        """从 config 取 DashScope 凭据（messenger_rpa.voice_output 优先）；空则由 enroll 走 env/secret。"""
        raw_cfg = (getattr(config_manager, "config", None) or {}) if config_manager else {}
        vo = (raw_cfg.get("messenger_rpa") or {}).get("voice_output") or {}
        return str(vo.get("dashscope_api_key") or "").strip(), str(vo.get("dashscope_region") or "").strip()

    def _audit(request: Request, action: str, detail: str) -> None:
        """声纹运营留痕（登记/解绑/改绑/删除）。app.state.audit_store 缺失时静默跳过。"""
        try:
            st = getattr(request.app.state, "audit_store", None)
            if not st:
                return
            try:
                actor = request.session.get("username", "api")
            except Exception:
                actor = "api"
            st.log(actor, action, detail)
        except Exception:
            logger.debug("[voice] audit log failed", exc_info=True)

    @app.get("/api/voice/cloned")
    async def api_voice_cloned(request: Request, _=Depends(api_auth)):
        """列出 DashScope 已登记的克隆声纹（无 key 时优雅返回 reason，不报错）。"""
        api_key, region = _dashscope_creds()
        try:
            from src.ai.voice_enroll import list_cloned_voices
            d = await asyncio.to_thread(
                list_cloned_voices, api_key=api_key, region=region or "intl")
        except RuntimeError as ex:
            if "DASHSCOPE_API_KEY" in str(ex):
                return {"ok": False, "reason": "no_api_key",
                        "message": "未配置 DASHSCOPE_API_KEY（messenger_rpa.voice_output.dashscope_api_key 或环境变量）"}
            return {"ok": False, "reason": "list_failed", "message": str(ex)[:300]}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "reason": "list_failed", "message": str(ex)[:300]}
        voices = (d.get("output") or {}).get("voice_list") or (d.get("output") or {}).get("voices") or []
        return {"ok": True, "voices": voices, "raw": d}

    @app.post("/api/voice/enroll")
    async def api_voice_enroll(request: Request, _=Depends(api_auth)):
        """声纹自助登记闭环：上传参考音频 → DashScope 登记克隆声纹 →
        写回指定人设的 voice_profile（voice_clone_command/qwen）→ 收件箱音色下拉即可选。

        multipart: file（参考音频）+ persona_id + preferred_name + region? + language_type?
        需 owner_consent 已在登记语义内（仅允许本人/已授权声音）。无 DASHSCOPE_API_KEY 时优雅返回。
        """
        form = await request.form()
        upload = form.get("file")
        persona_id = str(form.get("persona_id") or "").strip()
        preferred_name = str(form.get("preferred_name") or "").strip()
        region_in = str(form.get("region") or "").strip()
        language_type = str(form.get("language_type") or "Japanese").strip() or "Japanese"
        reference_text = str(form.get("reference_text") or "").strip()
        if upload is None or not getattr(upload, "filename", ""):
            raise HTTPException(400, "file（参考音频）必填")
        if not persona_id:
            raise HTTPException(400, "persona_id 必填")
        if not preferred_name:
            raise HTTPException(400, "preferred_name 必填")

        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        persona = pm.get_persona_by_id(persona_id)
        if persona is None:
            raise HTTPException(404, f"人设 {persona_id} 不存在")

        data = await upload.read()
        if not data:
            raise HTTPException(400, "空文件")
        if len(data) > 15 * 1024 * 1024:
            raise HTTPException(413, "参考音频过大（上限 15MB）")

        api_key, cfg_region = _dashscope_creds()
        region = region_in or cfg_region or "intl"

        # 保存参考音频到 voice_samples/<safe>.<ext>
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", preferred_name)[:40] or "voice"
        samples = Path("voice_samples")
        samples.mkdir(parents=True, exist_ok=True)
        ext = (os.path.splitext(upload.filename)[1] or ".wav").lower()
        audio_path = (samples / f"{safe}{ext}").resolve()
        try:
            audio_path.write_bytes(data)
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(500, f"参考音频落盘失败: {ex}")

        # ── 局域网优先：LAN 克隆主机在线则零样本登记（不烧云端配额）──
        raw_full_cfg: Dict[str, Any] = {}
        if config_manager and hasattr(config_manager, "config"):
            raw_full_cfg = config_manager.config or {}
        lan_cfg = raw_full_cfg.get("voice_clone_lan") or {}
        if isinstance(lan_cfg, dict) and lan_cfg.get("enabled"):
            try:
                from src.ai.voice_clone_client import VoiceCloneClient
                lan = VoiceCloneClient(lan_cfg)
                lan_up = await asyncio.to_thread(lan.health_ok)
            except Exception as ex:  # noqa: BLE001
                logger.debug("[voice/enroll] LAN health probe error: %s", ex)
                lan_up = False
            if lan_up:
                from src.ai.voice_enroll import build_lan_voice_profile
                from src.utils.persona_manager import PersonaManager as _PM
                vp_lan = build_lan_voice_profile(
                    reference_audio_path=str(audio_path), speaker_id=safe,
                    base_url=lan.base_url, language=str(lan_cfg.get("language") or "zh"),
                    reference_text=reference_text,
                    clone_path=str(lan_cfg.get("clone_path") or "/v1/tts/clone"))
                new_persona = dict(persona)
                new_persona["voice_profile"] = vp_lan
                try:
                    _pm = _PM.get_instance()
                    _pm.upsert_profile(persona_id, new_persona)
                    cm = getattr(request.app.state, "config_manager", None) or config_manager
                    _pm.persist_profiles(cm)
                except Exception as ex:  # noqa: BLE001
                    logger.warning("[voice/enroll] LAN persist failed: %s", ex)
                    return {"ok": False, "reason": "persist_failed", "message": str(ex)[:300]}
                _audit(request, "voice_enroll",
                       f"persona={persona_id} mode=lan_zeroshot name={preferred_name}")
                return {"ok": True, "mode": "lan_zeroshot", "persona_id": persona_id,
                        "reference_audio_path": str(audio_path), "lan_base_url": lan.base_url}
            logger.info("[voice/enroll] voice_clone_lan 不可用 → 回落云端 Qwen 登记")

        from src.ai.voice_enroll import (
            build_qwen_voice_profile, enroll_voice, qwen_profile_json_dict)
        try:
            res = await asyncio.to_thread(
                enroll_voice, audio_path=str(audio_path),
                preferred_name=preferred_name, api_key=api_key, region=region)
        except RuntimeError as ex:
            if "DASHSCOPE_API_KEY" in str(ex):
                return {"ok": False, "reason": "no_api_key",
                        "message": "未配置 DASHSCOPE_API_KEY（messenger_rpa.voice_output.dashscope_api_key 或环境变量）"}
            return {"ok": False, "reason": "enroll_failed", "message": str(ex)[:300]}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "reason": "enroll_failed", "message": str(ex)[:300]}

        voice = res["voice"]
        target_model = res.get("target_model")
        # 写 qwen_tts_wrapper 消费的 voice-profile JSON
        json_path = (samples / f"qwen_{safe}.json").resolve()
        try:
            json_path.write_text(
                json.dumps(qwen_profile_json_dict(
                    voice=voice, target_model=target_model,
                    reference_audio_path=str(audio_path), region=region,
                    preferred_name=preferred_name), ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:
            logger.debug("[voice/enroll] 写 voice-profile JSON 失败", exc_info=True)

        # 写回人设 voice_profile 并持久化（重启后续用）
        vp = build_qwen_voice_profile(
            voice=voice, reference_audio_path=str(audio_path),
            voice_profile_json_path=str(json_path), speaker_id=safe,
            region=region, target_model=target_model, language_type=language_type)
        new_persona = dict(persona)
        new_persona["voice_profile"] = vp
        try:
            pm.upsert_profile(persona_id, new_persona)
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            pm.persist_profiles(cm)
        except Exception as ex:  # noqa: BLE001
            logger.warning("[voice/enroll] persist persona failed: %s", ex)
            return {"ok": False, "reason": "persist_failed", "message": str(ex)[:300],
                    "voice": voice}
        _audit(request, "voice_enroll", f"persona={persona_id} voice={voice} name={preferred_name}")
        return {"ok": True, "voice": voice, "persona_id": persona_id,
                "reference_audio_path": str(audio_path)}

    @app.delete("/api/voice/profiles/{persona_id}")
    async def api_voice_unbind(persona_id: str, request: Request,
                               purge_cloud: bool = False, _=Depends(api_auth)):
        """解绑：移除某人设的 voice_profile（音色回落默认）。

        默认仅断开绑定、保留云端声纹（可随时改绑复用）。
        purge_cloud=1 时额外永久删除云端 Qwen 声纹（不可恢复，best-effort）。
        """
        from src.ai.voice_enroll import without_voice_profile
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        persona = pm.get_persona_by_id(persona_id)
        if persona is None:
            raise HTTPException(404, f"人设 {persona_id} 不存在")
        vp = persona.get("voice_profile")
        if not isinstance(vp, dict):
            return {"ok": True, "persona_id": persona_id, "changed": False}
        voice = str(vp.get("voice") or "").strip()
        purged = None
        if purge_cloud and voice:
            api_key, region = _dashscope_creds()
            try:
                from src.ai.voice_enroll import delete_cloned_voice
                await asyncio.to_thread(
                    delete_cloned_voice, voice=voice, api_key=api_key, region=region or "intl")
                purged = True
            except RuntimeError as ex:
                purged = False
                if "DASHSCOPE_API_KEY" in str(ex):
                    logger.info("[voice/unbind] purge skipped: no api key")
            except Exception as ex:  # noqa: BLE001
                purged = False
                logger.warning("[voice/unbind] cloud purge failed: %s", ex)
        try:
            pm.upsert_profile(persona_id, without_voice_profile(persona))
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            pm.persist_profiles(cm)
        except Exception as ex:  # noqa: BLE001
            logger.warning("[voice/unbind] persist failed: %s", ex)
            return {"ok": False, "reason": "persist_failed", "message": str(ex)[:300]}
        _audit(request, "voice_unbind",
               f"persona={persona_id} voice={voice or '-'} purge_cloud={bool(purge_cloud)} purged={purged}")
        return {"ok": True, "persona_id": persona_id, "changed": True, "purged": purged}

    @app.post("/api/voice/rebind")
    async def api_voice_rebind(request: Request, _=Depends(api_auth)):
        """改绑/复用：把已登记音色从 from_persona_id 复制到 to_persona_id（免重复上传/登记）。"""
        body = await request.json()
        src_id = str((body or {}).get("from_persona_id") or "").strip()
        dst_id = str((body or {}).get("to_persona_id") or "").strip()
        if not src_id or not dst_id:
            raise HTTPException(400, "from_persona_id 与 to_persona_id 均必填")
        if src_id == dst_id:
            raise HTTPException(400, "源与目标人设相同")
        from src.ai.voice_enroll import copy_voice_profile
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        src = pm.get_persona_by_id(src_id)
        dst = pm.get_persona_by_id(dst_id)
        if src is None:
            raise HTTPException(404, f"源人设 {src_id} 不存在")
        if dst is None:
            raise HTTPException(404, f"目标人设 {dst_id} 不存在")
        if not isinstance(src.get("voice_profile"), dict):
            return {"ok": False, "reason": "source_has_no_voice",
                    "message": f"源人设 {src_id} 未绑定音色"}
        try:
            pm.upsert_profile(dst_id, copy_voice_profile(src, dst))
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            pm.persist_profiles(cm)
        except Exception as ex:  # noqa: BLE001
            logger.warning("[voice/rebind] persist failed: %s", ex)
            return {"ok": False, "reason": "persist_failed", "message": str(ex)[:300]}
        _audit(request, "voice_rebind", f"from={src_id} to={dst_id}")
        return {"ok": True, "from_persona_id": src_id, "to_persona_id": dst_id}

    def _local_persona_voice_rows():
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        rows = []
        for s in pm.list_profiles_summary():
            pid = s.get("id")
            if not pid:
                continue
            p = pm.get_persona_by_id(pid)
            if not isinstance(p, dict):
                continue
            rows.append({"persona_id": pid, "name": s.get("name") or pid, "persona": p})
        return rows

    @app.get("/api/voice/reconcile")
    async def api_voice_reconcile(request: Request, _=Depends(api_auth)):
        """声纹资产对账：云端 list × 本地人设 voice_profile 交叉比对。

        返回 orphans（可回收）/ shared（多人设共用）/ linked / dangling（本地引用但云端未见）。
        """
        from src.ai.voice_enroll import (
            collect_local_voice_refs, list_all_cloned_voices, reconcile_voice_assets)
        local_refs = collect_local_voice_refs(_local_persona_voice_rows())
        api_key, region = _dashscope_creds()
        try:
            cloud_entries = await asyncio.to_thread(
                list_all_cloned_voices, api_key=api_key, region=region or "intl")
        except RuntimeError as ex:
            if "DASHSCOPE_API_KEY" in str(ex):
                report = reconcile_voice_assets([], local_refs)
                return {
                    "ok": False, "reason": "no_api_key",
                    "message": "未配置 DASHSCOPE_API_KEY，仅返回本地引用侧对账",
                    "local_refs": local_refs, **report,
                }
            return {"ok": False, "reason": "list_failed", "message": str(ex)[:300],
                    "local_refs": local_refs}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "reason": "list_failed", "message": str(ex)[:300],
                    "local_refs": local_refs}
        report = reconcile_voice_assets(cloud_entries, local_refs)
        return {"ok": True, "cloud_entries": cloud_entries, "local_refs": local_refs, **report}

    @app.post("/api/voice/purge")
    async def api_voice_purge(request: Request, _=Depends(api_auth)):
        """永久删除单个云端声纹。有本地引用时需 force=true（前端二次确认）。"""
        body = await request.json()
        voice = str((body or {}).get("voice") or "").strip()
        force = bool((body or {}).get("force"))
        if not voice:
            raise HTTPException(400, "voice 必填")
        from src.ai.voice_enroll import collect_local_voice_refs, delete_cloned_voice, purge_guard
        local_refs = collect_local_voice_refs(_local_persona_voice_rows())
        guard = purge_guard(voice, local_refs, force=force)
        if not guard.get("allowed"):
            return {"ok": False, **guard}
        api_key, region = _dashscope_creds()
        try:
            await asyncio.to_thread(
                delete_cloned_voice, voice=voice, api_key=api_key, region=region or "intl")
        except RuntimeError as ex:
            if "DASHSCOPE_API_KEY" in str(ex):
                return {"ok": False, "reason": "no_api_key",
                        "message": "未配置 DASHSCOPE_API_KEY"}
            return {"ok": False, "reason": "purge_failed", "message": str(ex)[:300]}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "reason": "purge_failed", "message": str(ex)[:300]}
        _audit(request, "voice_purge",
               f"voice={voice} force={force} ref_count={guard.get('ref_count', 0)}")
        return {"ok": True, "voice": voice, "purged": True, "ref_count": guard.get("ref_count", 0)}

    @app.post("/api/voice/purge-orphans")
    async def api_voice_purge_orphans(request: Request, _=Depends(api_auth)):
        """一键回收孤儿声纹：仅删除 ref_count=0 的云端声纹（安全，不碰仍被引用者）。"""
        from src.ai.voice_enroll import (
            collect_local_voice_refs, delete_cloned_voice,
            list_all_cloned_voices, reconcile_voice_assets)
        api_key, region = _dashscope_creds()
        local_refs = collect_local_voice_refs(_local_persona_voice_rows())
        try:
            cloud_entries = await asyncio.to_thread(
                list_all_cloned_voices, api_key=api_key, region=region or "intl")
        except RuntimeError as ex:
            if "DASHSCOPE_API_KEY" in str(ex):
                return {"ok": False, "reason": "no_api_key",
                        "message": "未配置 DASHSCOPE_API_KEY"}
            return {"ok": False, "reason": "list_failed", "message": str(ex)[:300]}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "reason": "list_failed", "message": str(ex)[:300]}
        report = reconcile_voice_assets(cloud_entries, local_refs)
        orphans = report.get("orphans") or []
        deleted, failed = [], []
        for item in orphans:
            vid = item.get("voice")
            if not vid:
                continue
            try:
                await asyncio.to_thread(
                    delete_cloned_voice, voice=vid, api_key=api_key, region=region or "intl")
                deleted.append(vid)
            except Exception as ex:  # noqa: BLE001
                failed.append({"voice": vid, "message": str(ex)[:200]})
        _audit(request, "voice_purge_orphans",
               f"deleted={len(deleted)} failed={len(failed)} voices={','.join(deleted[:20])}")
        return {
            "ok": True, "deleted": deleted, "failed": failed,
            "orphan_count": len(orphans), "summary": report.get("summary"),
        }

    def _serve_tts_file(filename: str):
        """Shared helper: validate + serve any TTS preview file."""
        if "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(400, "invalid filename")
        if not any(filename.startswith(p) for p in _TTS_PREVIEW_PREFIXES):
            raise HTTPException(404, "not found")
        fpath = _TTS_PREVIEW_DIR / filename
        if not fpath.is_file():
            raise HTTPException(404, "preview expired or not found")
        ext = fpath.suffix.lower().lstrip(".")
        mime_map = {"mp3": "audio/mpeg", "ogg": "audio/ogg",
                   "wav": "audio/wav", "opus": "audio/ogg"}
        return FileResponse(str(fpath), media_type=mime_map.get(ext, "application/octet-stream"))

    @app.get("/api/voice/tts-test/{filename}")
    async def api_voice_tts_file(filename: str, request: Request, _=Depends(api_auth)):
        """Serve a previously generated TTS preview file."""
        return _serve_tts_file(filename)

    @app.get("/api/voice/tts-file/{filename}")
    async def api_voice_tts_file_alt(filename: str, request: Request, _=Depends(api_auth)):
        """P8-D: Unified TTS file serving endpoint (line-tts-* + ttspreview-*)."""
        return _serve_tts_file(filename)

    @app.post("/api/admin/tts-cleanup")
    async def api_admin_tts_cleanup(request: Request, max_age_sec: int = 86400, _=Depends(api_auth)):
        """P16-C: Admin endpoint to trigger manual cleanup of old TTS preview files.

        Query params:
            max_age_sec — files older than this are deleted (default 24h)
        Returns:
            {ok: true, removed: N, max_age_sec: N}
        """
        from src.integrations.shared.tts_preview import cleanup_tts_previews
        removed = cleanup_tts_previews(max_age_sec=float(max_age_sec))
        return {"ok": True, "removed": removed, "max_age_sec": max_age_sec}

    @app.get("/api/admin/tts-stats")
    async def api_admin_tts_stats(request: Request, _=Depends(api_auth)):
        """P17-C: Return statistics about tmp_tts_preview directory.

        Returns:
            {ok: true, files: N, total_bytes: N, oldest_sec: N|null, newest_sec: N|null, by_prefix: {...}}
        """
        from src.integrations.shared.tts_preview import _TTS_DIR as tts_dir
        import time
        stats = {"files": 0, "total_bytes": 0, "by_prefix": {}, "oldest_sec": None, "newest_sec": None}
        now = time.time()
        prefixes = ("tts-", "line-tts-", "wa-tts-")
        try:
            if tts_dir.exists():
                for f in tts_dir.iterdir():
                    if not f.is_file():
                        continue
                    st = f.stat()
                    age = now - st.st_mtime
                    stats["files"] += 1
                    stats["total_bytes"] += st.st_size
                    if stats["oldest_sec"] is None or age > stats["oldest_sec"]:
                        stats["oldest_sec"] = age
                    if stats["newest_sec"] is None or age < stats["newest_sec"]:
                        stats["newest_sec"] = age
                    pref = next((p for p in prefixes if f.name.startswith(p)), "other")
                    stats["by_prefix"][pref] = stats["by_prefix"].get(pref, 0) + 1
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}
        return {"ok": True, **stats}

    @app.get("/admin/tts-dashboard")
    async def admin_tts_dashboard(request: Request):
        """P20-B: TTS stats dashboard page."""
        try:
            api_auth(request)
        except HTTPException as exc:
            if exc.status_code == 401:
                return HTMLResponse(
                    '<!doctype html><meta http-equiv="refresh" content="0; url=/login">'
                    '<a href="/login">请先登录</a>',
                    status_code=200,
                )
            raise
        from fastapi.templating import Jinja2Templates
        templates = Jinja2Templates(directory="src/web/templates")
        return templates.TemplateResponse("admin_tts_dashboard.html", {"request": request})
