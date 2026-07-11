"""每人设「相册/媒体」后台 API —— 上传/列表/改/删/试触发。

挂 ``/api/personas/{pid}/media*``。文件落 ``src/web/static/persona_albums/<pid>/``（经 /static
直服，供网格缩略图与前端预览），元数据（触发词/配文/权重/关系闸门/命中）落 DB
（``persona_media_store``）。回复链（image_autosend / skill_manager Stage 0）读同一份 store。

护栏：扩展名白名单（图 jpg/png/webp/gif、视频 mp4/mov/webm/m4v）、体积上限（图 10MB / 视频 50MB）、
视频时长上限（默认 3 分钟，仅当 ffprobe 可探时才拦；软失败不阻塞）、sha256 去重、
persona_id 目录消毒防穿越、写操作 viewer 只读拦截。文案经 ``tr`` 收口零 CJK。

视频上传附带元数据探测（ffprobe 拿时长/宽高）+ 抽帧生成封面缩略图（ffmpeg，落 ``*.thumb.jpg``）；
图片探宽高（PIL）。以上全部软失败——缺 ffmpeg/ffprobe/PIL 只是拿不到该项元数据，不影响上传落库。
"""

import hashlib
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, List

from fastapi import Depends, HTTPException, Request

from src.companion.media_probe import (
    make_video_thumbnail as _make_video_thumbnail,
    probe_image as _probe_image,
    probe_video as _probe_video,
)
from src.web.web_i18n import tr

logger = logging.getLogger("ai_chat_assistant.persona_media_routes")

_STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
_ALBUM_ROOT = _STATIC_DIR / "persona_albums"

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}
_MAX_PHOTO_BYTES = 10 * 1024 * 1024
_MAX_VIDEO_BYTES = 50 * 1024 * 1024
_MAX_VIDEO_DURATION_MS = 3 * 60 * 1000  # 视频时长上限（仅 ffprobe 可探时才拦）
_ROLE_VIEWER = "viewer"


def _safe_pid(pid: Any) -> str:
    """人设 id 收敛为安全目录名（防路径穿越）。"""
    s = re.sub(r"[^A-Za-z0-9_-]", "_", str(pid or ""))[:64]
    return s or "default"


def _media_type_for_ext(ext: str) -> str:
    e = (ext or "").lower()
    if e in _VIDEO_EXT:
        return "video"
    if e in _IMAGE_EXT:
        return "photo"
    return ""


def _as_str_list(raw: Any) -> List[str]:
    """触发词/标签解析：接受 JSON 数组 或 逗号/顿号/换行分隔的字符串。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw).strip()
    if not s:
        return []
    if s[0] == "[":
        try:
            v = json.loads(s)
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
        except Exception:
            pass
    return [t.strip() for t in re.split(r"[,\n，、;；]", s) if t.strip()]


def register_persona_media_routes(app, auth_dep, audit_store=None, config_manager=None):
    """挂载每人设相册后台 API。``auth_dep`` 为登录校验依赖；``audit_store`` 为操作审计（可选）。"""

    def _store():
        from src.companion.persona_media_store import get_persona_media_store
        return get_persona_media_store()

    def _actor(request: Request) -> str:
        try:
            return str(request.session.get("username") or "web_admin")
        except Exception:
            return "web_admin"

    def _audit(request: Request, action: str, target: str = "", detail: str = "") -> None:
        if audit_store is None:
            return
        try:
            audit_store.log(_actor(request), action, target, "", detail)
        except Exception:
            logger.debug("[pmedia] 审计写入失败（已忽略）", exc_info=True)

    def _require_store(request: Request):
        st = _store()
        if st is None:
            raise HTTPException(503, tr(request, "err.pmedia.store_unavailable"))
        return st

    def _require_write(request: Request):
        try:
            role = request.session.get("role", "")
        except Exception:
            role = ""
        if role == _ROLE_VIEWER:
            raise HTTPException(403, tr(request, "err.persona.readonly_no_edit"))

    def _require_persona(request: Request, pid: str):
        from src.utils.persona_manager import PersonaManager
        p = PersonaManager.get_instance().get_persona_by_id(str(pid))
        if p is None:
            raise HTTPException(404, tr(request, "err.pmedia.persona_not_found", name=pid))
        return p

    def _owned_row(request: Request, st, pid: str, mid: str):
        row = st.get(str(mid))
        if row is None or str(row.get("persona_id")) != str(pid):
            raise HTTPException(404, tr(request, "err.pmedia.not_found"))
        return row

    @app.get("/api/personas/{pid}/media")
    async def list_persona_media(pid: str, request: Request, _=Depends(auth_dep)):
        """列出该人设全部媒体条目 + 统计。"""
        st = _require_store(request)
        return {"items": st.list(str(pid)), "stats": st.stats(str(pid))}

    @app.post("/api/personas/{pid}/media")
    async def upload_persona_media(pid: str, request: Request, _=Depends(auth_dep)):
        """上传一张图/一段视频到该人设相册（multipart: file + 可选 triggers/caption/tags/...）。"""
        _require_write(request)
        st = _require_store(request)
        _require_persona(request, pid)
        form = await request.form()
        upload = form.get("file")
        if upload is None or not getattr(upload, "filename", ""):
            raise HTTPException(400, tr(request, "err.pmedia.file_required"))
        ext = os.path.splitext(str(upload.filename))[1].lower()
        mtype = _media_type_for_ext(ext)
        if not mtype:
            raise HTTPException(400, tr(request, "err.pmedia.ext_not_allowed", ext=ext or "?"))
        data = await upload.read()
        if not data:
            raise HTTPException(400, tr(request, "err.inbox.empty_file"))
        limit = _MAX_VIDEO_BYTES if mtype == "video" else _MAX_PHOTO_BYTES
        if len(data) > limit:
            raise HTTPException(
                413, tr(request, "err.pmedia.too_large", mb=limit // (1024 * 1024)))
        sha = hashlib.sha256(data).hexdigest()
        dup = st.find_by_sha(str(pid), sha)
        if dup is not None:
            return {"ok": True, "item": dup, "deduped": True}
        safe = _safe_pid(pid)
        d = _ALBUM_ROOT / safe
        try:
            d.mkdir(parents=True, exist_ok=True)
            name = f"{uuid.uuid4().hex}{ext}"
            fpath = (d / name).resolve()
            fpath.write_bytes(data)
        except Exception as ex:  # noqa: BLE001
            logger.warning("[pmedia] 保存文件失败: %s", ex, exc_info=True)
            raise HTTPException(500, tr(request, "err.pmedia.save_failed", err=str(ex)[:200]))
        url = f"/static/persona_albums/{safe}/{name}"
        # 元数据探测 + 视频护栏/封面（全部软失败，缺 ffmpeg/ffprobe/PIL 不阻塞上传）。
        width = height = duration_ms = 0
        thumb_url = ""
        if mtype == "video":
            meta = _probe_video(str(fpath)) or {}
            width = int(meta.get("width") or 0)
            height = int(meta.get("height") or 0)
            duration_ms = int(meta.get("duration_ms") or 0)
            if 0 < _MAX_VIDEO_DURATION_MS < duration_ms:
                try:
                    fpath.unlink()
                except Exception:
                    pass
                raise HTTPException(413, tr(
                    request, "err.pmedia.too_long",
                    sec=_MAX_VIDEO_DURATION_MS // 1000))
            thumb_name = f"{name}.thumb.jpg"
            at_sec = min(1.0, (duration_ms / 1000.0) / 2.0) if duration_ms > 0 else 0.0
            if _make_video_thumbnail(str(fpath), str(d / thumb_name), at_sec=at_sec):
                thumb_url = f"/static/persona_albums/{safe}/{thumb_name}"
        else:
            meta = _probe_image(str(fpath)) or {}
            width = int(meta.get("width") or 0)
            height = int(meta.get("height") or 0)
        try:
            weight = int(form.get("weight") or 1)
        except Exception:
            weight = 1
        try:
            min_bond = int(form.get("min_bond_level") or 0)
        except Exception:
            min_bond = 0
        enabled = str(form.get("enabled", "1")).strip().lower() not in ("0", "false", "no", "")
        try:
            actor = str(request.session.get("username") or "")
        except Exception:
            actor = ""
        row = st.add(
            str(pid), mtype, str(fpath), url, thumb_url=thumb_url,
            triggers=_as_str_list(form.get("triggers")),
            caption=str(form.get("caption") or "").strip(),
            tags=_as_str_list(form.get("tags")),
            weight=weight, enabled=enabled, min_bond_level=min_bond,
            bytes_=len(data), width=width, height=height,
            duration_ms=duration_ms, sha256=sha, created_by=actor)
        logger.info("[pmedia] 上传 pid=%s type=%s id=%s bytes=%d dur=%dms",
                    pid, mtype, row.get("id"), len(data), duration_ms)
        _audit(request, "pmedia_upload", f"pid={pid} id={row.get('id')}",
               f"type={mtype} bytes={len(data)}")
        return {"ok": True, "item": row}

    @app.patch("/api/personas/{pid}/media/{mid}")
    async def update_persona_media(pid: str, mid: str, request: Request, _=Depends(auth_dep)):
        """改条目元数据（触发词/配文/多语配文/标签/启停/权重/关系闸门）。"""
        _require_write(request)
        st = _require_store(request)
        _owned_row(request, st, pid, mid)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, tr(request, "err.pmedia.bad_body"))
        fields: dict = {}
        if "triggers" in body:
            fields["triggers"] = _as_str_list(body.get("triggers"))
        if "tags" in body:
            fields["tags"] = _as_str_list(body.get("tags"))
        if "caption" in body:
            fields["caption"] = str(body.get("caption") or "")
        if isinstance(body.get("caption_i18n"), dict):
            fields["caption_i18n"] = {
                str(k): str(v) for k, v in body["caption_i18n"].items()}
        if "enabled" in body:
            fields["enabled"] = bool(body.get("enabled"))
        if "weight" in body:
            try:
                fields["weight"] = int(body.get("weight") or 1)
            except Exception:
                pass
        if "min_bond_level" in body:
            try:
                fields["min_bond_level"] = int(body.get("min_bond_level") or 0)
            except Exception:
                pass
        item = st.update(str(mid), **fields)
        _audit(request, "pmedia_update", f"pid={pid} id={mid}",
               ",".join(sorted(fields.keys())))
        return {"ok": True, "item": item}

    @app.delete("/api/personas/{pid}/media/{mid}")
    async def delete_persona_media(pid: str, mid: str, request: Request, _=Depends(auth_dep)):
        """删条目（DB 行 + 磁盘文件；仅删相册根目录内的文件，防误删）。"""
        _require_write(request)
        st = _require_store(request)
        row = _owned_row(request, st, pid, mid)
        st.delete(str(mid))
        root = _ALBUM_ROOT.resolve()
        for cand in (str(row.get("file_path") or ""),
                     str(row.get("file_path") or "") + ".thumb.jpg"):
            if not cand:
                continue
            try:
                fp = Path(cand).resolve()
                if fp.is_file() and root in fp.parents:
                    fp.unlink()
            except Exception:
                logger.debug("[pmedia] 删除文件失败（已忽略）", exc_info=True)
        _audit(request, "pmedia_delete", f"pid={pid} id={mid}",
               str(row.get("media_type") or ""))
        return {"ok": True}

    @app.post("/api/personas/{pid}/media/test")
    async def test_persona_media_trigger(pid: str, request: Request, _=Depends(auth_dep)):
        """「试触发」：输入一句话，返回会命中的池（keyword/generic/none）+ 全部候选（不随机）。"""
        st = _require_store(request)
        body = await request.json()
        text = str((body or {}).get("text") or "")
        from src.ai.companion_selfie import detect_selfie_request
        from src.companion.persona_media import explain_match
        # 与真实链路同口径：通用池仅在「泛化要照片/自拍」请求时才作候选。
        generic_ok = bool(detect_selfie_request(text))
        rows = st.list(str(pid), enabled_only=True)
        out = explain_match(rows, text, generic_ok=generic_ok)
        out["generic_ok"] = generic_ok
        return out
