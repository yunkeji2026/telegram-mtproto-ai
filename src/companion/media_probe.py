"""相册媒体探针 —— 视频时长/尺寸探测 + 缩略图抽帧 + 图片尺寸（全部软失败）。

给「后台上传图/视频」补护栏与预览：视频拿到时长（做时长上限护栏）、宽高（前端排版），
并抽一帧当封面（网格里视频不必自动播放也有缩略图）。**所有能力都软失败**——
ffmpeg/ffprobe/PIL 任一缺失只是拿不到该项元数据（返回 ``None``/``False``），
绝不阻塞上传（上传成功与否只取决于扩展名白名单 + 体积，见 ``persona_media_routes``）。

与 ``src/client/voice_sender.py`` 的 ffprobe 探测同型（``shutil.which`` 守卫 + subprocess 超时）。
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def probe_video(path: str) -> Optional[Dict[str, int]]:
    """探测视频 ``{duration_ms, width, height}``。ffprobe 缺失/失败/无效返回 ``None``。"""
    if not ffprobe_available():
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json", str(p),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout or "{}")
    except Exception:
        return None
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    w = h = 0
    if streams:
        try:
            w = int(streams[0].get("width") or 0)
            h = int(streams[0].get("height") or 0)
        except (TypeError, ValueError):
            w = h = 0
    dur_ms = 0
    try:
        secs = float(fmt.get("duration") or 0.0)
        if secs > 0:
            dur_ms = int(round(secs * 1000))
    except (TypeError, ValueError):
        dur_ms = 0
    return {"duration_ms": dur_ms, "width": w, "height": h}


def make_video_thumbnail(
    video_path: str, out_path: str, *,
    at_sec: float = 1.0, width: int = 320,
) -> bool:
    """从视频抽一帧当封面（缩到 ``width`` 宽保持宽高比）。ffmpeg 缺失/失败返回 ``False``。

    ``at_sec`` 落在视频时长之外时 ffmpeg 会抽不到帧 → 调用方宜按已探的时长夹取一个安全时间点。
    """
    if not ffmpeg_available():
        return False
    src = Path(video_path)
    if not src.is_file():
        return False
    try:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [
                "ffmpeg", "-y", "-ss", f"{max(0.0, float(at_sec)):.3f}",
                "-i", str(src), "-frames:v", "1",
                "-vf", f"scale={int(width)}:-1",
                "-q:v", "3", str(out_path),
            ],
            capture_output=True, text=True, timeout=60,
        )
        return r.returncode == 0 and Path(out_path).is_file() and Path(out_path).stat().st_size > 0
    except Exception:
        logger.debug("[media_probe] 抽帧失败（已忽略）", exc_info=True)
        return False


def probe_image(path: str) -> Optional[Dict[str, int]]:
    """探测图片 ``{width, height}``（用 PIL）。PIL 缺失/失败返回 ``None``。"""
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return None
    try:
        with Image.open(path) as im:
            w, h = im.size
        return {"width": int(w or 0), "height": int(h or 0)}
    except Exception:
        return None


__all__ = [
    "ffmpeg_available", "ffprobe_available",
    "probe_video", "make_video_thumbnail", "probe_image",
]
