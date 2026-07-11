"""旧文件相册 → ``persona_media`` 注册表 的导入器（纯逻辑，供 CLI 与测试复用）。

旧机制（``companion_selfie`` backend=album）从 ``config/persona_albums/<persona_key>/``
随机挑图，无触发词/配文/命中统计。本模块把这些静态媒体导入新 DB 注册表（元数据可后续
在后台补），文件复制进 ``static/persona_albums/<pid>/`` 供 /static 直服 + 前端预览。

**幂等**：按 ``(persona_id, sha256)`` 去重，可重复跑（已导入的算 ``dup`` 跳过）。
探测（视频时长/宽高 + 封面、图片宽高）经 ``media_probe`` 软失败——缺 ffmpeg/ffprobe/PIL
只是拿不到该项元数据，绝不阻塞导入。
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}
_MAX_VIDEO_DURATION_MS = 3 * 60 * 1000


def _media_type(ext: str) -> str:
    e = (ext or "").lower()
    if e in VIDEO_EXT:
        return "video"
    if e in IMAGE_EXT:
        return "photo"
    return ""


def _safe_pid(pid: Any) -> str:
    import re
    s = re.sub(r"[^A-Za-z0-9_-]", "_", str(pid or ""))[:64]
    return s or "default"


def discover_albums(
    src: Any, *, only_persona: Optional[str] = None,
) -> List[Tuple[str, List[Path]]]:
    """扫描旧相册根：每个子目录＝一个人设的相册（persona_id=目录名）。

    根目录直接摆放的文件语义含糊（不知归谁）——仅当显式 ``only_persona`` 时才收进那个人设，
    否则跳过（不臆测归属）。返回 ``[(persona_id, [files...]), ...]``（稳定排序）。
    """
    root = Path(src)
    if not root.is_dir():
        return []
    ext = IMAGE_EXT | VIDEO_EXT
    acc: "Dict[str, List[Path]]" = {}
    for d in sorted(root.iterdir(), key=lambda p: p.name):
        if not d.is_dir():
            continue
        pid = d.name
        if only_persona and pid != only_persona:
            continue
        files = [f for f in sorted(d.iterdir(), key=lambda p: p.name)
                 if f.is_file() and f.suffix.lower() in ext]
        if files:
            acc.setdefault(pid, []).extend(files)
    if only_persona:
        root_files = [f for f in sorted(root.iterdir(), key=lambda p: p.name)
                      if f.is_file() and f.suffix.lower() in ext]
        if root_files:
            acc.setdefault(only_persona, []).extend(root_files)
    return list(acc.items())


def import_file(
    store, album_root: Any, persona_id: str, path: Path, *,
    triggers: Optional[List[str]] = None, apply: bool = True,
    created_by: str = "import",
) -> str:
    """导入单个媒体文件到 store。返回 ``imported`` | ``dup`` | ``skip`` | ``error``。

    dry-run（``apply=False``）只做去重判定与分类，不落盘不写库。
    """
    try:
        ext = path.suffix.lower()
        mtype = _media_type(ext)
        if not mtype:
            return "skip"
        data = path.read_bytes()
        if not data:
            return "skip"
        sha = hashlib.sha256(data).hexdigest()
        if store.find_by_sha(str(persona_id), sha) is not None:
            return "dup"
        if not apply:
            return "imported"  # dry-run：将会导入
        safe = _safe_pid(persona_id)
        d = Path(album_root) / safe
        d.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}{ext}"
        fpath = (d / name).resolve()
        fpath.write_bytes(data)
        url = f"/static/persona_albums/{safe}/{name}"
        width = height = duration_ms = 0
        thumb_url = ""
        if mtype == "video":
            from src.companion.media_probe import make_video_thumbnail, probe_video
            meta = probe_video(str(fpath)) or {}
            width = int(meta.get("width") or 0)
            height = int(meta.get("height") or 0)
            duration_ms = int(meta.get("duration_ms") or 0)
            thumb_name = f"{name}.thumb.jpg"
            at_sec = min(1.0, (duration_ms / 1000.0) / 2.0) if duration_ms > 0 else 0.0
            if make_video_thumbnail(str(fpath), str(d / thumb_name), at_sec=at_sec):
                thumb_url = f"/static/persona_albums/{safe}/{thumb_name}"
        else:
            from src.companion.media_probe import probe_image
            meta = probe_image(str(fpath)) or {}
            width = int(meta.get("width") or 0)
            height = int(meta.get("height") or 0)
        store.add(
            str(persona_id), mtype, str(fpath), url, thumb_url=thumb_url,
            triggers=list(triggers or []), width=width, height=height,
            duration_ms=duration_ms, bytes_=len(data), sha256=sha,
            created_by=created_by)
        return "imported"
    except Exception:
        logger.warning("[pmedia_import] 导入失败 %s", path, exc_info=True)
        return "error"


def import_albums(
    store, src: Any, album_root: Any, *,
    only_persona: Optional[str] = None,
    triggers: Optional[List[str]] = None,
    apply: bool = True,
) -> Dict[str, Any]:
    """批量导入旧相册。返回汇总 ``{personas:{pid:{imported,dup,skip,error,files}}, totals, apply}``。"""
    summary: Dict[str, Any] = {
        "apply": bool(apply), "personas": {},
        "total_imported": 0, "total_dup": 0, "total_skip": 0, "total_error": 0,
    }
    for pid, files in discover_albums(src, only_persona=only_persona):
        pstat = {"imported": 0, "dup": 0, "skip": 0, "error": 0, "files": len(files)}
        for f in files:
            res = import_file(store, album_root, pid, f,
                              triggers=triggers, apply=apply)
            pstat[res] = pstat.get(res, 0) + 1
            summary[f"total_{res}"] = summary.get(f"total_{res}", 0) + 1
        summary["personas"][pid] = pstat
    return summary


__all__ = [
    "IMAGE_EXT", "VIDEO_EXT", "discover_albums", "import_file", "import_albums",
]
