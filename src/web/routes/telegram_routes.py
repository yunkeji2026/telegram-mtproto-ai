"""Telegram 帐号管理 Web/REST 路由。

挂载点：
    GET  /telegram                              — 设置页面
    GET  /api/telegram/settings                 — 读取当前配置
    PUT  /api/telegram/settings/voice-reply     — 保存语音回复配置（含 voice_profile）
    PUT  /api/telegram/settings/voice-asr       — 保存语音识别配置
    PUT  /api/telegram/settings/reply-logic     — 保存自动回复逻辑配置（支持热更新）
    GET  /api/telegram/account-info             — 账号在线状态 + 今日统计
"""
from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

_SENSITIVE_KEYS = {"api_key", "token", "secret", "password", "api_hash"}


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: "***" if str(k).lower() in _SENSITIVE_KEYS else _redact(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _tg_cfg(config_manager: Any) -> Dict[str, Any]:
    cfg = getattr(config_manager, "config", None) or {}
    tg = cfg.get("telegram") or {}
    return tg if isinstance(tg, dict) else {}


def _asr_cfg(config_manager: Any) -> Dict[str, Any]:
    cfg = getattr(config_manager, "config", None) or {}
    return dict(cfg.get("voice_recognition") or {})


def _save_cfg(config_manager: Any, request) -> None:
    ok = config_manager.save()
    if ok is False:
        raise HTTPException(500, tr(request, "err.tg.save_config_failed"))


def _check_wav_quality(path: str) -> Dict[str, Any]:
    """Check WAV file quality using stdlib wave module. No extra deps needed."""
    import wave as _wave
    checks: list = []
    info: Dict[str, Any] = {}
    try:
        with _wave.open(path, "r") as wf:
            channels = wf.getnchannels()
            sample_rate = wf.getframerate()
            nframes = wf.getnframes()
            duration = round(nframes / sample_rate, 1) if sample_rate else 0
            info = {"channels": channels, "sample_rate": sample_rate, "duration_sec": duration}
            # Duration
            if duration < 5:
                checks.append({"level": "warning", "msg": f"时长偏短（{duration}s），推荐 10–30 秒"})
            elif duration > 60:
                checks.append({"level": "warning", "msg": f"时长偏长（{duration}s），建议裁剪到 30 秒内"})
            else:
                checks.append({"level": "ok", "msg": f"时长 {duration}s ✓"})
            # Sample rate
            if sample_rate < 16000:
                checks.append({"level": "warning", "msg": f"采样率偏低（{sample_rate} Hz），推荐 ≥ 16000 Hz"})
            else:
                checks.append({"level": "ok", "msg": f"采样率 {sample_rate} Hz ✓"})
            # Channels
            if channels > 1:
                checks.append({"level": "warning", "msg": f"双声道，建议转为单声道（mono）"})
            else:
                checks.append({"level": "ok", "msg": "单声道 ✓"})
    except Exception as ex:
        checks.append({"level": "error", "msg": f"读取失败：{ex}"})
    has_error = any(c["level"] == "error" for c in checks)
    has_warn = any(c["level"] == "warning" for c in checks)
    grade = "error" if has_error else ("warning" if has_warn else "ok")
    return {"grade": grade, "info": info, "checks": checks}


_SNAPSHOT_DIR = Path("config/snapshots")


def _save_snapshot(config_manager: Any, label: str = "") -> Optional[str]:
    """Save current telegram config section as a timestamped JSON snapshot."""
    try:
        _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        raw = getattr(config_manager, "config", None) or {}
        tg_cfg = _redact_secrets({"telegram": raw.get("telegram") or {}})
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"tg_{ts}.json"
        data = {"_ts": ts, "_label": label, **tg_cfg}
        (_SNAPSHOT_DIR / filename).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # Prune: keep latest 20
        snaps = sorted(_SNAPSHOT_DIR.glob("tg_*.json"), key=lambda x: x.name)
        for old in snaps[:-20]:
            old.unlink(missing_ok=True)
        return filename
    except Exception as ex:
        logger.warning("[snapshot] save failed: %s", ex)
        return None


def _list_snapshots() -> list:
    """Return last 20 snapshots, newest first."""
    result = []
    try:
        for f in sorted(_SNAPSHOT_DIR.glob("tg_*.json"), key=lambda x: x.name, reverse=True)[:20]:
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
                result.append({
                    "filename": f.name,
                    "ts": meta.get("_ts", f.stem),
                    "label": meta.get("_label", ""),
                    "size_kb": round(f.stat().st_size / 1024, 1),
                })
            except Exception:
                pass
    except Exception:
        pass
    return result


def _today_stats_from_log(log_path: str = "logs/app.log") -> Dict[str, int]:
    """Count today's Telegram activity by scanning app.log (fast tail scan)."""
    stats = {"messages": 0, "voice_in": 0, "tts_sent": 0}
    try:
        today = datetime.date.today().strftime("%Y-%m-%d")
        p = Path(log_path)
        if not p.is_file():
            return stats
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-8000:]
        for line in lines:
            if today not in line:
                continue
            # voice received — matches: 收到语音消息 [私聊/群组
            if "收到语音消息 [" in line:
                stats["voice_in"] += 1
            # text message received
            elif "收到消息 [" in line:
                stats["messages"] += 1
            # TTS voice sent
            if "[voice_reply] voice sent" in line:
                stats["tts_sent"] += 1
    except Exception:
        pass
    return stats


def _health_check(config_manager: Any) -> Dict[str, Any]:
    """Run configuration health checks and return issues list."""
    issues = []
    tg = _tg_cfg(config_manager)
    asr = _asr_cfg(config_manager)
    vr = tg.get("voice_reply") or {}

    # ── voice reply checks ──────────────────────────────────────
    if vr.get("enabled"):
        backend = vr.get("backend", "")
        if backend == "voice_clone_command":
            vp = vr.get("voice_profile") or {}
            ref = vp.get("reference_audio_path", "")
            if not ref:
                issues.append({
                    "level": "error",
                    "code": "ref_audio_missing",
                    "msg": "克隆声音模式需要设置参考音频路径（voice_profile.reference_audio_path）",
                })
            elif not Path(ref).is_file():
                issues.append({
                    "level": "warning",
                    "code": "ref_audio_not_found",
                    "msg": f"参考音频文件不存在：{ref}",
                })
        elif backend == "openai":
            oai = vr.get("openai_tts") or {}
            if not oai.get("api_key"):
                issues.append({
                    "level": "error",
                    "code": "oai_tts_no_key",
                    "msg": "OpenAI TTS 未配置 API Key",
                })

    # ── ASR checks ──────────────────────────────────────────────
    if asr.get("enabled", True):
        provider = asr.get("provider", "faster_whisper")
        if provider == "openai":
            oai = asr.get("openai") or {}
            if not oai.get("api_key"):
                issues.append({
                    "level": "error",
                    "code": "asr_no_key",
                    "msg": "云端 ASR 未配置 API Key",
                })

    return {
        "ok": not any(i["level"] == "error" for i in issues),
        "issues": issues,
        "voice_reply_enabled": bool(vr.get("enabled")),
        "asr_enabled": asr.get("enabled", True),
    }


def _tail_log(log_path: str = "logs/app.log", n: int = 30) -> list:
    """Return last N Telegram-relevant log lines as parsed dicts."""
    result = []
    _TG_KEYWORDS = (
        "TelegramClient", "voice_reply", "收到消息", "收到语音",
        "[voice/tts", "语音识别", "语音转录",
    )
    try:
        p = Path(log_path)
        if not p.is_file():
            return result
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-3000:]
        for raw in lines:
            raw = raw.rstrip()
            if not any(kw in raw for kw in _TG_KEYWORDS):
                continue
            # Parse: [2026-05-17 00:18:05] [INFO] logger: message
            level = "INFO"
            ts = ""
            msg = raw
            import re as _re
            m = _re.match(
                r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+\[(\w+)\]\s+[^:]+:\s+(.*)",
                raw,
            )
            if m:
                ts, level, msg = m.group(1)[11:], m.group(2), m.group(3)
            result.append({"ts": ts, "level": level, "msg": msg[:180]})
        return result[-n:]
    except Exception:
        return result


_VOICE_SAMPLE_DIRS = ["voice_samples", "voices", "audio_samples"]


def _scan_voice_files(ref_path: str = "") -> list:
    """Scan standard voice sample dirs + ref_path dir for .wav files."""
    dirs = list(_VOICE_SAMPLE_DIRS)
    if ref_path:
        parent = str(Path(ref_path).parent)
        if parent not in dirs:
            dirs.append(parent)
    files: list = []
    seen: set = set()
    for d in dirs:
        p = Path(d)
        if not p.is_dir():
            continue
        for f in sorted(p.glob("*.wav"), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.name in seen:
                continue
            seen.add(f.name)
            files.append({
                "name": f.name,
                "path": str(f.resolve()).replace("\\", "/"),
                "size_kb": round(f.stat().st_size / 1024, 1),
                "dir": str(p),
                "url": f"/api/telegram/voice-sample/{f.name}",
            })
    return files


def _recent_senders_from_log(log_path: str = "logs/app.log", n: int = 30) -> list:
    """Extract unique recent private-chat senders from app.log."""
    import re as _re
    seen: dict = {}
    try:
        p = Path(log_path)
        if not p.is_file():
            return []
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-5000:]
        for raw in reversed(lines):  # newest first
            m = _re.search(
                r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*收到(?:语音)?消息 \[私聊/([^\]]+)\]",
                raw,
            )
            if m:
                ts, uname = m.group(1), m.group(2).strip()
                if uname and uname not in seen:
                    seen[uname] = {"username": uname, "last_ts": ts[11:]}
        return list(seen.values())[:n]
    except Exception:
        return []


def _redact_secrets(obj: Any, _keys=("api_key", "password", "secret", "token")) -> Any:
    """Deep-copy obj with sensitive string values masked."""
    import copy
    obj = copy.deepcopy(obj)
    def _walk(o: Any) -> None:
        if isinstance(o, dict):
            for k in list(o):
                if any(s in k.lower() for s in _keys) and isinstance(o[k], str) and o[k]:
                    o[k] = "***"
                else:
                    _walk(o[k])
        elif isinstance(o, list):
            for item in o:
                _walk(item)
    _walk(obj)
    return obj


def register_telegram_routes(
    app,
    *,
    page_auth,
    api_auth,
    templates,
    config_manager,
    telegram_client=None,
    audit_store=None,
) -> None:

    # ── 页面 ─────────────────────────────────────────────────
    @app.get("/telegram", response_class=HTMLResponse)
    async def telegram_page(request: Request):
        page_auth(request)
        return templates.TemplateResponse(
            request, "telegram.html", {"active": "telegram"},
        )

    # ── 读取全量设置 ─────────────────────────────────────────
    @app.get("/api/telegram/settings")
    async def api_tg_settings_get(request: Request, _=Depends(api_auth)):
        tg = _tg_cfg(config_manager)
        asr = _asr_cfg(config_manager)
        vr = tg.get("voice_reply") or {}
        return {
            "ok": True,
            "voice_reply": _redact(vr),
            "voice_recognition": _redact(asr),
            "reply_logic": tg.get("reply_logic") or {},
            "process_private": tg.get("process_private", True),
            "process_groups": tg.get("process_groups", False),
            "no_reply_sender_usernames": tg.get("no_reply_sender_usernames") or [],
        }

    # ── 保存语音回复配置 ─────────────────────────────────────
    @app.put("/api/telegram/settings/voice-reply")
    async def api_tg_voice_reply_put(request: Request, _=Depends(api_auth)):
        body: Dict[str, Any] = await request.json()
        root = getattr(config_manager, "config", None) or {}
        tg = root.setdefault("telegram", {})
        vr = tg.setdefault("voice_reply", {})

        top_allowed = {
            "enabled", "trigger", "probability", "backend",
            "max_text_chars", "max_seconds", "timeout_sec",
            "send_text_summary", "out_dir", "voice",
        }
        updated = []
        for k, v in body.items():
            if k in top_allowed:
                vr[k] = v
                updated.append(k)

        # voice_profile — includes the critical reference_audio_path
        vp_body = body.get("voice_profile")
        if isinstance(vp_body, dict):
            vp = vr.setdefault("voice_profile", {})
            vp_allowed = {
                "enabled", "owner_consent", "backend",
                "reference_audio_path", "voice_profile_path",
                "speaker_id", "command_timeout_sec",
            }
            for k, v in vp_body.items():
                if k in vp_allowed:
                    vp[k] = v
                    updated.append(f"voice_profile.{k}")

        # openai_tts sub-fields
        oai_body = body.get("openai_tts")
        if isinstance(oai_body, dict):
            oai = vr.setdefault("openai_tts", {})
            for k, v in oai_body.items():
                if k in {"model", "voice"}:
                    oai[k] = v
                    updated.append(f"openai_tts.{k}")
                elif k == "api_key" and str(v).strip() not in ("", "***"):
                    oai[k] = v
                    updated.append("openai_tts.api_key")

        _save_cfg(config_manager, request)
        _save_snapshot(config_manager, label="voice_reply")
        logger.info("[telegram_routes] voice_reply saved: %s", updated)
        return {"ok": True, "updated": updated}

    # ── 保存语音识别配置 ─────────────────────────────────────
    @app.put("/api/telegram/settings/voice-asr")
    async def api_tg_voice_asr_put(request: Request, _=Depends(api_auth)):
        body: Dict[str, Any] = await request.json()
        root = getattr(config_manager, "config", None) or {}
        asr = root.setdefault("voice_recognition", {})

        top_allowed = {"enabled", "provider", "language", "timeout", "max_file_size"}
        updated = []
        for k, v in body.items():
            if k in top_allowed:
                asr[k] = v
                updated.append(k)

        if isinstance(body.get("openai"), dict):
            oai = asr.setdefault("openai", {})
            for k, v in body["openai"].items():
                if k in {"model", "base_url"}:
                    oai[k] = v
                    updated.append(f"openai.{k}")
                elif k == "api_key" and str(v).strip() not in ("", "***"):
                    oai[k] = v
                    updated.append("openai.api_key")

        if isinstance(body.get("whisper"), dict):
            wh = asr.setdefault("whisper", {})
            for k, v in body["whisper"].items():
                if k in {"model_size", "device", "compute_type"}:
                    wh[k] = v
                    updated.append(f"whisper.{k}")

        if isinstance(body.get("faster_whisper"), dict):
            fw = asr.setdefault("faster_whisper", {})
            for k, v in body["faster_whisper"].items():
                if k in {"model_size", "device", "compute_type"}:
                    fw[k] = v
                    updated.append(f"faster_whisper.{k}")

        _save_cfg(config_manager, request)
        _save_snapshot(config_manager, label="voice_asr")
        logger.info("[telegram_routes] voice_asr saved: %s", updated)
        return {"ok": True, "updated": updated}

    # ── 保存自动回复逻辑（支持热更新） ──────────────────────
    @app.put("/api/telegram/settings/reply-logic")
    async def api_tg_reply_logic_put(request: Request, _=Depends(api_auth)):
        body: Dict[str, Any] = await request.json()
        root = getattr(config_manager, "config", None) or {}
        tg = root.setdefault("telegram", {})

        top_allowed = {"process_private", "process_groups", "no_reply_sender_usernames"}
        rl_allowed = {
            "enabled", "cooldown_seconds", "max_consecutive_replies",
            "reply_to_user_message", "ignore_edited",
        }
        updated = []
        for k, v in body.items():
            if k in top_allowed:
                tg[k] = v
                updated.append(k)

        if isinstance(body.get("reply_logic"), dict):
            rl = tg.setdefault("reply_logic", {})
            for k, v in body["reply_logic"].items():
                if k in rl_allowed:
                    rl[k] = v
                    updated.append(f"reply_logic.{k}")

        _save_cfg(config_manager, request)
        _save_snapshot(config_manager, label="reply_logic")

        # 热更新运行时 TelegramClient
        if telegram_client is not None:
            try:
                if "process_private" in body:
                    telegram_client.process_private = bool(body["process_private"])
                if "process_groups" in body:
                    telegram_client.process_groups = bool(body["process_groups"])
                if "no_reply_sender_usernames" in body:
                    telegram_client.no_reply_sender_usernames = list(
                        body["no_reply_sender_usernames"]
                    )
            except Exception as e:
                logger.warning("[telegram_routes] hot-reload partial: %s", e)

        logger.info("[telegram_routes] reply_logic saved: %s", updated)
        return {"ok": True, "updated": updated}

    # ── 账号在线状态 + 今日统计 ──────────────────────────────
    @app.get("/api/telegram/account-info")
    async def api_tg_account_info(request: Request, _=Depends(api_auth)):
        tg_cfg = _tg_cfg(config_manager)
        info: Dict[str, Any] = {
            "phone": tg_cfg.get("phone_number", ""),
            "session_name": tg_cfg.get("session_name", "default"),
            "online": False,
            "username": "",
            "display_name": "",
            "stats": {"messages": 0, "voice_in": 0, "tts_sent": 0},
        }

        if telegram_client is not None:
            info["online"] = bool(getattr(telegram_client, "running", False))
            try:
                me = getattr(telegram_client, "_me", None)
                if me:
                    info["username"] = (
                        f"@{me.username}" if getattr(me, "username", None) else ""
                    )
                    info["display_name"] = " ".join(
                        filter(None, [
                            getattr(me, "first_name", ""),
                            getattr(me, "last_name", ""),
                        ])
                    )
            except Exception:
                pass

        # Today's stats from log
        try:
            import asyncio as _aio
            info["stats"] = await _aio.to_thread(_today_stats_from_log)
        except Exception:
            pass

        return {"ok": True, **info}

    # ── 配置健康检查 ─────────────────────────────────────────────
    @app.get("/api/telegram/health")
    async def api_tg_health(request: Request, _=Depends(api_auth)):
        import asyncio as _aio
        result = await _aio.to_thread(_health_check, config_manager)
        return {"ok": True, **result}

    # ── 日志尾流 ─────────────────────────────────────────────────
    @app.get("/api/telegram/log-tail")
    async def api_tg_log_tail(
        request: Request, n: int = 30, _=Depends(api_auth)
    ):
        import asyncio as _aio
        lines = await _aio.to_thread(_tail_log, "logs/app.log", min(n, 100))
        return {"ok": True, "lines": lines}

    # ── 语音文件扫描 ────────────────────────────────────────────
    @app.get("/api/telegram/voice-files")
    async def api_tg_voice_files(request: Request, _=Depends(api_auth)):
        import asyncio as _aio
        tg = _tg_cfg(config_manager)
        ref = (tg.get("voice_reply") or {}).get("voice_profile", {}).get(
            "reference_audio_path", ""
        )
        files = await _aio.to_thread(_scan_voice_files, ref)
        return {"ok": True, "files": files}

    # ── 语音文件上传 + 质量检测 ─────────────────────────────────
    @app.post("/api/telegram/upload-voice")
    async def api_tg_upload_voice(
        request: Request, file: UploadFile = File(...), _=Depends(api_auth)
    ):
        if not file.filename or not file.filename.lower().endswith(".wav"):
            raise HTTPException(400, tr(request, "tg_js_021"))
        content = await file.read()
        if len(content) > 20 * 1024 * 1024:  # 20 MB
            raise HTTPException(400, tr(request, "err.tg.file_too_large_20mb"))
        dest_dir = Path("voice_samples")
        dest_dir.mkdir(exist_ok=True)
        dest = dest_dir / file.filename
        dest.write_bytes(content)
        resolved = str(dest.resolve()).replace("\\", "/")
        import asyncio as _aio
        quality = await _aio.to_thread(_check_wav_quality, str(dest))
        return {
            "ok": True,
            "name": file.filename,
            "path": resolved,
            "size_kb": round(len(content) / 1024, 1),
            "url": f"/api/telegram/voice-sample/{file.filename}",
            "quality": quality,
        }

    # ── WAV 质量检测（已存文件） ──────────────────────────────
    @app.get("/api/telegram/voice-quality")
    async def api_tg_voice_quality(
        request: Request, filename: str = "", _=Depends(api_auth)
    ):
        if not filename or "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(400, "invalid filename")
        if not filename.lower().endswith(".wav"):
            raise HTTPException(400, "only .wav")
        for d in _VOICE_SAMPLE_DIRS:
            f = Path(d) / filename
            if f.is_file():
                import asyncio as _aio
                q = await _aio.to_thread(_check_wav_quality, str(f))
                return {"ok": True, "quality": q}
        raise HTTPException(404, "file not found")

    # ── 语音样本文件服务 ────────────────────────────────────────
    @app.get("/api/telegram/voice-sample/{filename}")
    async def api_tg_voice_sample(
        filename: str, request: Request, _=Depends(api_auth)
    ):
        if "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(400, "invalid filename")
        if not filename.lower().endswith(".wav"):
            raise HTTPException(400, "only .wav allowed")
        # Search in all standard dirs
        for d in _VOICE_SAMPLE_DIRS:
            f = Path(d) / filename
            if f.is_file():
                return Response(content=f.read_bytes(), media_type="audio/wav")
        raise HTTPException(404, "file not found")

    # ── 配置导出 ──────────────────────────────────────────────
    @app.get("/api/telegram/config-export")
    async def api_tg_config_export(request: Request, _=Depends(api_auth)):
        raw = getattr(config_manager, "config", None) or {}
        tg_only = {"telegram": raw.get("telegram") or {}}
        sanitized = _redact_secrets(tg_only)
        date_str = datetime.date.today().strftime("%Y%m%d")
        return Response(
            content=json.dumps(sanitized, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition":
                    f'attachment; filename="telegram_config_{date_str}.json"'
            },
        )

    # ── 最近联系人（从日志解析） ───────────────────────────────
    @app.get("/api/telegram/recent-contacts")
    async def api_tg_recent_contacts(request: Request, _=Depends(api_auth)):
        import asyncio as _aio
        contacts = await _aio.to_thread(_recent_senders_from_log)
        # Mark which ones are already in blocklist
        tg = _tg_cfg(config_manager)
        blocked = [
            u.strip().lower().lstrip("@")
            for u in (tg.get("no_reply_sender_usernames") or [])
        ]
        for c in contacts:
            c["blocked"] = c["username"].lower().lstrip("@") in blocked
        return {"ok": True, "contacts": contacts}

    # ── SSE 实时日志流 ───────────────────────────────────────────
    @app.get("/api/telegram/log-stream")
    async def api_tg_log_stream(request: Request, _=Depends(api_auth)):
        import asyncio as _aio
        import time as _time
        import re as _re
        from fastapi.responses import StreamingResponse as _SR

        _KW = (
            "TelegramClient", "voice_reply", "收到消息", "收到语音",
            "voice/tts", "语音识别", "语音转录",
        )
        _PAT = _re.compile(
            r"\[(\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}))\]\s+\[(\w+)\]\s+[^:]+:\s+(.*)"
        )

        log_path = Path("logs/app.log")

        async def _stream():
            pos = log_path.stat().st_size if log_path.is_file() else 0
            last_ping = _time.monotonic()
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    await _aio.sleep(0.5)
                    # Keepalive comment every 20 s
                    if _time.monotonic() - last_ping > 20:
                        yield ": ping\n\n"
                        last_ping = _time.monotonic()
                    if not log_path.is_file():
                        continue
                    curr = log_path.stat().st_size
                    if curr <= pos:
                        continue
                    with log_path.open("r", encoding="utf-8", errors="ignore") as f:
                        f.seek(pos)
                        chunk = f.read()
                    pos = curr
                    for raw in chunk.splitlines():
                        raw = raw.strip()
                        if not raw or not any(kw in raw for kw in _KW):
                            continue
                        ts, level, msg = "", "INFO", raw
                        m = _PAT.match(raw)
                        if m:
                            ts, level, msg = m.group(2), m.group(3), m.group(4)
                        data = json.dumps(
                            {"ts": ts, "level": level, "msg": msg[:200]},
                            ensure_ascii=False,
                        )
                        yield f"data: {data}\n\n"
            except Exception:
                pass

        return _SR(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── 配置快照列表 ────────────────────────────────────────────
    @app.get("/api/telegram/config-snapshots")
    async def api_tg_config_snapshots(request: Request, _=Depends(api_auth)):
        import asyncio as _aio
        snaps = await _aio.to_thread(_list_snapshots)
        return {"ok": True, "snapshots": snaps}

    # ── 配置快照恢复 ──────────────────────────────────────────
    @app.post("/api/telegram/config-restore")
    async def api_tg_config_restore(request: Request, _=Depends(api_auth)):
        body: Dict[str, Any] = await request.json()
        filename = (body.get("filename") or "").strip()
        if not filename or not filename.startswith("tg_") or not filename.endswith(".json"):
            raise HTTPException(400, "invalid filename")
        snap_path = _SNAPSHOT_DIR / filename
        if not snap_path.is_file():
            raise HTTPException(404, "snapshot not found")
        try:
            data = json.loads(snap_path.read_text(encoding="utf-8"))
        except Exception as ex:
            raise HTTPException(500, f"read error: {ex}")
        restored_tg = data.get("telegram")
        if not isinstance(restored_tg, dict):
            raise HTTPException(400, "snapshot missing telegram section")
        # Save pre-restore snapshot
        _save_snapshot(config_manager, label="pre_restore")
        root = getattr(config_manager, "config", None) or {}
        root["telegram"] = restored_tg
        _save_cfg(config_manager, request)
        logger.info("[telegram_routes] config restored from snapshot: %s", filename)
        return {"ok": True, "restored": filename}
