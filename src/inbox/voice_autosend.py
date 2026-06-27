"""全自动语音回复（System Z autosend 的 TTS 出站，Phase 全自动语音）。

把「全自动聊天 + 翻译 + 语音」凑齐成闭环：之前统一收件箱 autosend 只发**文本**，
auto-voice 仅存在于原生 TG 客户端（``telegram.voice_reply``）与 RPA 设备号
（``voice_output.auto_voice``）。本模块给 **System Z 全自动 autosend** 补上「按策略把
回复转 TTS 语音」的能力，一处生效、全平台共用（经 ``orch.send_media(media_type="voice")``，
Telegram/WhatsApp/Messenger/LINE/Instagram 均可，见 official_api_worker.send_media）。

设计（复用既有件，避免重复造轮子）：
- 语音配置：``persona_voice.resolve_voice_cfg``（人设 voice_profile → 声音克隆/后端）。
- 合成：``ai.tts_pipeline.TTSPipeline``；格式：``client.voice_sender.convert_to_ogg_opus``。
- 落盘：``protocol_bridge.save_outbound_media``（与坐席「发送语音」同一出站媒体目录）。
- 触发护栏：仿 ``client.sender._maybe_send_voice_reply``（trigger / 长度上限 / 失败回落文本）。

**默认关**（``inbox.l2_autosend.voice.enabled=false``）→ 全自动仍纯文本，零行为变更。
任何环节失败都返回「不发语音」让调用方回落文本，绝不卡住全自动主流程。
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CHARS = 200
_VALID_TRIGGERS = ("never", "always", "when_peer_voice")

# ── 全自动语音可观测性（进程内累计；供 /api/drafts/autosend-status 暴露）──────────
# 只在「策略已判定该发语音」之后计数：sent=真发出语音；fallback=合成/投递失败回落文本。
# 灰度时不必逐会话翻聊天记录即可监控自动语音是否在工作、回落原因与最近时长。
_METRICS: Dict[str, Any] = {
    "sent": 0, "fallback": 0, "last_reason": "",
    "last_ts": 0.0, "last_duration_ms": 0,
}
_METRICS_LOCK = threading.Lock()


def record_voice_sent(duration_ms: int = 0) -> None:
    with _METRICS_LOCK:
        _METRICS["sent"] = int(_METRICS["sent"]) + 1
        _METRICS["last_ts"] = time.time()
        if duration_ms and duration_ms > 0:
            _METRICS["last_duration_ms"] = int(duration_ms)


def record_voice_fallback(reason: str) -> None:
    with _METRICS_LOCK:
        _METRICS["fallback"] = int(_METRICS["fallback"]) + 1
        _METRICS["last_reason"] = str(reason or "")
        _METRICS["last_ts"] = time.time()


def metrics_snapshot() -> Dict[str, Any]:
    with _METRICS_LOCK:
        return dict(_METRICS)


def resolve_voice_autosend_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    """取 ``inbox.l2_autosend.voice`` 块（缺失返回空 dict → enabled 视为 false）。"""
    try:
        return dict(
            (((config or {}).get("inbox") or {}).get("l2_autosend") or {}).get("voice")
            or {}
        )
    except Exception:
        return {}


def should_send_voice(
    voice_block: Dict[str, Any],
    text: str,
    *,
    peer_sent_voice: bool = False,
) -> bool:
    """纯决策：本条全自动回复是否应以**语音**发出。

    - ``enabled=false`` / 空文本 → False。
    - 长度护栏：``min_chars``..``max_chars`` 之外 → False（长文本念语音体验差，回落文本）。
    - ``trigger``：``never`` 永不；``always`` 总是；``when_peer_voice`` 仅当客户上一条入站
      是语音时（"你发语音我回语音"，最拟人且最克制，默认值）。
    """
    vb = voice_block or {}
    if not bool(vb.get("enabled")):
        return False
    t = (text or "").strip()
    if not t:
        return False
    n = len(t)
    try:
        min_chars = int(vb.get("min_chars", 1) or 1)
        max_chars = int(vb.get("max_chars", _DEFAULT_MAX_CHARS) or _DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        min_chars, max_chars = 1, _DEFAULT_MAX_CHARS
    if n < min_chars or n > max_chars:
        return False
    trigger = str(vb.get("trigger", "when_peer_voice") or "when_peer_voice").lower()
    if trigger not in _VALID_TRIGGERS:
        trigger = "when_peer_voice"
    if trigger == "never":
        return False
    if trigger == "always":
        return True
    return bool(peer_sent_voice)  # when_peer_voice


async def _synth_ogg(config: Dict[str, Any], persona_id: str, text: str,
                     *, out_dir: str) -> Optional[str]:
    """合成 TTS → 转 OGG/Opus，返回本地音频路径；任何失败返回 None。"""
    try:
        from src.ai.persona_voice import resolve_voice_cfg
        voice_cfg = resolve_voice_cfg(persona_id or None, config or {})
    except Exception:
        logger.debug("[voice_autosend] resolve_voice_cfg 失败", exc_info=True)
        return None
    voice_cfg["enabled"] = True
    voice_cfg["out_dir"] = out_dir
    try:
        from src.ai.tts_pipeline import TTSPipeline
        tts = TTSPipeline(voice_cfg)
        result = await tts.synthesize(text, timeout_sec=45.0)
    except Exception:
        logger.debug("[voice_autosend] TTS 合成异常", exc_info=True)
        return None
    if not getattr(result, "ok", False) or not getattr(result, "audio_path", ""):
        return None
    audio_path = result.audio_path
    try:
        from src.client.voice_sender import convert_to_ogg_opus
        converted = await asyncio.to_thread(convert_to_ogg_opus, audio_path, delete_src=True)
        if converted:
            return converted
    except Exception:
        logger.debug("[voice_autosend] OGG 转码失败，按原格式", exc_info=True)
    return audio_path


async def stage_voice_file(
    config: Dict[str, Any],
    platform: str,
    account_id: str,
    persona_id: str,
    text: str,
    *,
    out_dir: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """合成语音并落到出站媒体目录，返回 ``(本地路径, /static URL)``；失败返回 None。

    调用方据此 ``orch.send_media(media_path=local, media_url=url, media_type="voice")``。
    """
    od = out_dir or str(Path(tempfile.gettempdir()) / "autosend_voice")
    audio_path = await _synth_ogg(config, persona_id, text, out_dir=od)
    if not audio_path:
        return None
    try:
        with open(audio_path, "rb") as fh:
            data = fh.read()
    except Exception:
        logger.debug("[voice_autosend] 读取合成音频失败", exc_info=True)
        return None
    finally:
        try:
            os.remove(audio_path)
        except Exception:
            pass
    if not data:
        return None
    try:
        from src.integrations.protocol_bridge import save_outbound_media
        local, url, _mt = save_outbound_media(
            platform, account_id, os.path.basename(audio_path), data)
        return (local, url)
    except Exception:
        logger.debug("[voice_autosend] 落出站媒体失败", exc_info=True)
        return None


__all__ = [
    "resolve_voice_autosend_cfg", "should_send_voice", "stage_voice_file",
    "record_voice_sent", "record_voice_fallback", "metrics_snapshot",
]
