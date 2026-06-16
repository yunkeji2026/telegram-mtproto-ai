"""P61：跨平台媒体引用 → 本地可读路径 统一解析层。

各平台 media_ref 形态不一（绝对路径 / 相对路径 / file:// / http(s) URL）。
本模块把「消息里的 media_ref」解析成**当前进程可直接打开的本地文件路径**，
供 OCR/ASR 翻译复用——避免在 4 个 runner 各写一遍解析逻辑。

设计：纯函数、不下载远程、不抛异常。解析不到（远程 URL / 文件不存在）→ None，
调用方回退到「上传」路径。远程拉取留待后续（需各平台凭证，不在本层）。
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote, urlparse

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_AUDIO_EXT = {".ogg", ".opus", ".mp3", ".m4a", ".wav", ".webm", ".amr", ".aac", ".mp4"}


def _is_remote(ref: str) -> bool:
    return ref.startswith(("http://", "https://"))


def _strip_file_scheme(ref: str) -> str:
    if ref.startswith("file://"):
        parsed = urlparse(ref)
        path = unquote(parsed.path or "")
        # Windows: file:///C:/x → /C:/x，去掉前导斜杠
        if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        return path
    return ref


def resolve_media_path(
    message: Dict[str, Any],
    *,
    base_dirs: Optional[list] = None,
) -> Optional[str]:
    """把消息的 media_ref 解析为存在的本地文件路径；解析不到返回 None。

    - 绝对路径且存在 → 直接返回
    - file:// → 去 scheme 后判断
    - 相对路径 → 在 base_dirs 下逐个拼接尝试
    - http(s):// 远程 → None（本层不下载）
    """
    if not isinstance(message, dict):
        return None
    ref = str(message.get("media_ref") or "").strip()
    if not ref:
        return None
    if _is_remote(ref):
        return None
    ref = _strip_file_scheme(ref)
    try:
        if os.path.isabs(ref) and os.path.isfile(ref):
            return ref
        for base in (base_dirs or []):
            cand = os.path.join(str(base), ref)
            if os.path.isfile(cand):
                return cand
        # 相对当前工作目录兜底
        if os.path.isfile(ref):
            return os.path.abspath(ref)
    except Exception:
        return None
    return None


def media_kind(message: Dict[str, Any]) -> str:
    """归一媒体大类：image | voice | other | ''（按 media_type 或 ref 扩展名推断）。"""
    if not isinstance(message, dict):
        return ""
    mt = str(message.get("media_type") or "").strip().lower()
    if mt in ("image", "photo"):
        return "image"
    if mt in ("voice", "audio"):
        return "voice"
    ref = str(message.get("media_ref") or "").strip().lower()
    ext = os.path.splitext(urlparse(ref).path or ref)[1]
    if ext in _IMAGE_EXT:
        return "image"
    if ext in _AUDIO_EXT:
        return "voice"
    return "other" if (mt or ref) else ""


def resolve_for_translate(
    message: Dict[str, Any],
    *,
    base_dirs: Optional[list] = None,
) -> Tuple[Optional[str], str, str]:
    """便捷封装：返回 (local_path|None, kind, reason)。

    reason ∈ ok | no_ref | remote_unsupported | not_found | unsupported_kind。
    """
    ref = str((message or {}).get("media_ref") or "").strip()
    if not ref:
        return None, "", "no_ref"
    kind = media_kind(message)
    if kind not in ("image", "voice"):
        return None, kind, "unsupported_kind"
    if _is_remote(ref):
        return None, kind, "remote_unsupported"
    path = resolve_media_path(message, base_dirs=base_dirs)
    if not path:
        return None, kind, "not_found"
    return path, kind, "ok"


__all__ = ["resolve_media_path", "media_kind", "resolve_for_translate"]
