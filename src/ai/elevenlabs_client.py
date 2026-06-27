"""ElevenLabs v3 TTS 客户端 — 付费档「真人情感」语音（含克隆音色）。

与 ``voice_clone_client.py`` 同源：薄 HTTP 封装 + 纯函数（可单测，无网络/IO）。
ElevenLabs v3（``eleven_v3``）是当前最具表现力的 TTS：通过**内联音频标签**
（``[excited]`` / ``[laughs]`` / ``[whispers]``）+ **voice_settings**（stability 调低更听
情绪、style 放大个性）做情感控制；非实时，正适合聊天语音条。

接口契约（核实于 2026-06 官方 docs）：
    POST {base_url}/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128
        Headers: xi-api-key, Content-Type: application/json
        Body:    {"text", "model_id":"eleven_v3",
                  "voice_settings":{"stability","similarity_boost","style",
                                    "use_speaker_boost","speed"}}
        成功 → **裸音频字节**（按 output_format，mp3/ogg-opus）；
        失败 → JSON {"detail": ...}（如 401 鉴权 / 配额耗尽）。

voice_id 为该人设的 ElevenLabs 克隆音色 ID（见 voice_profile.speaker_id / voice）。
v3 单次 ≤ 5000 字符；聊天回复远低于此。

可单测的纯函数：
  - ``output_format_for`` — 把内部 format（mp3/ogg）映射成 EL output_format
  - ``build_tts_body``    — 合成请求体（JSON bytes）
  - ``parse_tts_response``— 解析响应为音频字节（区分裸音频 vs 错误 JSON）
"""
from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "eleven_v3"
DEFAULT_BASE_URL = "https://api.elevenlabs.io"

# 内部 format → ElevenLabs output_format。ogg/opus → ogg-opus 容器（与 .ogg 后缀对齐）。
_OUTPUT_FORMAT_MAP = {
    "mp3": "mp3_44100_128",
    "ogg": "opus_48000_128",
    "opus": "opus_48000_128",
}


def output_format_for(fmt: str) -> str:
    """内部 format → ElevenLabs output_format（未知 → mp3_44100_128）。"""
    return _OUTPUT_FORMAT_MAP.get(str(fmt or "").strip().lower(), "mp3_44100_128")


def build_tts_body(
    text: str, *, model_id: str = DEFAULT_MODEL,
    voice_settings: Optional[Dict[str, Any]] = None,
) -> bytes:
    """ElevenLabs /v1/text-to-speech 请求体（JSON bytes）。"""
    body: Dict[str, Any] = {"text": str(text or ""), "model_id": model_id or DEFAULT_MODEL}
    if voice_settings:
        body["voice_settings"] = dict(voice_settings)
    return json.dumps(body).encode()


def parse_tts_response(body: bytes, content_type: str = "") -> bytes:
    """解析响应为音频字节。

    成功 → 裸音频字节；若是错误 JSON（``{"detail":...}``）→ 抛 RuntimeError。
    判定：content-type 含 json 或 body 以 ``{`` 开头时按 JSON 错误处理。
    """
    if not body:
        raise RuntimeError("elevenlabs: empty response")
    ct = str(content_type or "").lower()
    looks_json = "application/json" in ct or body[:1] == b"{"
    if looks_json:
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return body  # 不是合法 JSON → 当作音频字节
        detail = data.get("detail") if isinstance(data, dict) else None
        msg = detail if isinstance(detail, str) else json.dumps(detail or data, ensure_ascii=False)
        raise RuntimeError(f"elevenlabs_api_error: {str(msg)[:300]}")
    return body


class ElevenLabsClient:
    """ElevenLabs v3 TTS HTTP 客户端（薄封装）。"""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        cfg = cfg or {}
        self.api_key: str = str(cfg.get("api_key") or "").strip()
        self.base_url: str = str(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self.model_id: str = str(cfg.get("model_id") or DEFAULT_MODEL).strip()
        self.timeout_sec: float = float(cfg.get("timeout_sec") or 120)
        self.similarity_boost: float = float(cfg.get("similarity_boost") or 0.75)

    def synthesize(
        self, text: str, voice_id: str, out: Path, *, emotion: Any = None,
        output_format: str = "mp3_44100_128",
    ) -> None:
        """文本 + voice_id → 合成（按 output_format 的音频）写入 out。失败抛异常。"""
        if not self.api_key:
            raise RuntimeError("elevenlabs_missing_api_key")
        if not voice_id:
            raise RuntimeError("elevenlabs_missing_voice_id")

        # 情绪 → 内联音频标签 + voice_settings（两条情感杠杆都上）
        from src.ai.voice_emotion import (
            coerce_emotion, elevenlabs_voice_settings, to_elevenlabs_text,
        )
        spec = coerce_emotion(emotion)
        text_tagged = to_elevenlabs_text(text, spec)
        settings = elevenlabs_voice_settings(spec, similarity_boost=self.similarity_boost)

        payload = build_tts_body(
            text_tagged, model_id=self.model_id, voice_settings=settings)
        url = f"{self.base_url}/v1/text-to-speech/{voice_id}?output_format={output_format}"
        req = urllib.request.Request(
            url, data=payload,
            headers={
                "xi-api-key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
            ct = resp.headers.get("Content-Type", "") if hasattr(resp, "headers") else ""
            body = resp.read()
        audio = parse_tts_response(body, ct)
        if not audio:
            raise RuntimeError("elevenlabs: decoded empty audio")
        Path(out).write_bytes(audio)


__all__ = [
    "ElevenLabsClient", "build_tts_body", "parse_tts_response",
    "output_format_for", "DEFAULT_MODEL", "DEFAULT_BASE_URL",
]
