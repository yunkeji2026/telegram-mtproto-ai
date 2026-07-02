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

from src.ai.voice_fitness import VoiceDecision

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CHARS = 200
_VALID_TRIGGERS = ("never", "always", "when_peer_voice", "smart")

# ── 全自动语音可观测性（进程内累计；供 /api/drafts/autosend-status 暴露）──────────
# 只在「策略已判定该发语音」之后计数：sent=真发出语音；fallback=合成/投递失败回落文本。
# 灰度时不必逐会话翻聊天记录即可监控自动语音是否在工作、回落原因与最近时长。
_METRICS: Dict[str, Any] = {
    "sent": 0, "fallback": 0, "last_reason": "",
    "last_ts": 0.0, "last_duration_ms": 0,
    # Stage4 决策观测：每次「该不该发语音」判定结果 + 原因分布（调阈值 / 看灰度命中率）。
    "voice_chosen": 0, "text_chosen": 0,
    "decision_reasons": {}, "last_decision": "",
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


def record_voice_decision(send_voice: bool, reason: str) -> None:
    """记一次「该不该发语音」判定（voice/text 计数 + 原因分布），供 autosend-status 观测。

    与 sent/fallback 不同：sent/fallback 是「已决定发语音」后的合成投递结果，本函数记的是
    更上游的**决策本身**——含「判文字」的占比与原因（如 low_fitness/too_long/unspeakable），
    用于灰度期看语音占比是否符合"克制"手感、按 reason 分布调阈值。
    """
    with _METRICS_LOCK:
        key = "voice_chosen" if send_voice else "text_chosen"
        _METRICS[key] = int(_METRICS.get(key, 0)) + 1
        r = str(reason or "")
        reasons = _METRICS.setdefault("decision_reasons", {})
        reasons[r] = int(reasons.get(r, 0)) + 1
        _METRICS["last_decision"] = ("voice:" if send_voice else "text:") + r


def metrics_snapshot() -> Dict[str, Any]:
    with _METRICS_LOCK:
        snap = dict(_METRICS)
        snap["decision_reasons"] = dict(_METRICS.get("decision_reasons") or {})
        return snap


def resolve_voice_autosend_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    """取 ``inbox.l2_autosend.voice`` 块（缺失返回空 dict → enabled 视为 false）。"""
    try:
        return dict(
            (((config or {}).get("inbox") or {}).get("l2_autosend") or {}).get("voice")
            or {}
        )
    except Exception:
        return {}


def decide_voice(
    voice_block: Dict[str, Any],
    text: str,
    *,
    peer_sent_voice: bool = False,
    recent_voice_ratio: float = 0.0,
    peer_emotion: str = "",
    peer_emotion_intensity: float = -1.0,
    intimacy: float = 0.0,
    crisis_block: bool = False,
) -> VoiceDecision:
    """决策本条回复发**语音**还是**文字**，返回带 ``reason`` 的 VoiceDecision（供观测）。

    统一 4 档 trigger 与 smart 评分的**单一入口**；``should_send_voice`` 是其布尔投影。
    - ``enabled=false`` / 空文本 / 长度越界 → 文字（reason: disabled/empty/too_short/too_long）。
    - ``trigger``：``never`` / ``always`` / ``when_peer_voice``（默认，对等）/ ``smart``。
    - ``smart``：委托 ``ai.voice_fitness.voice_fitness``——按**回复情绪 + 客户此刻情绪 +
      亲密度 + 频率**综合评分，``score ≥ threshold`` 才语音。调用方采集并传入
      ``recent_voice_ratio`` / ``peer_emotion*`` / ``intimacy`` / ``crisis_block``
      （缺省退化为"仅回复情绪 + 对等回应"，仍安全可用）。参数见 ``voice_block['smart']``。
    """
    vb = voice_block or {}
    if not bool(vb.get("enabled")):
        return VoiceDecision(False, 0.0, "disabled")
    t = (text or "").strip()
    if not t:
        return VoiceDecision(False, 0.0, "empty")
    n = len(t)
    try:
        min_chars = int(vb.get("min_chars", 1) or 1)
        max_chars = int(vb.get("max_chars", _DEFAULT_MAX_CHARS) or _DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        min_chars, max_chars = 1, _DEFAULT_MAX_CHARS
    if n < min_chars:
        return VoiceDecision(False, 0.0, "too_short")
    if n > max_chars:
        return VoiceDecision(False, 0.0, "too_long")
    trigger = str(vb.get("trigger", "when_peer_voice") or "when_peer_voice").lower()
    if trigger not in _VALID_TRIGGERS:
        trigger = "when_peer_voice"
    if trigger == "never":
        return VoiceDecision(False, 0.0, "trigger_never")
    if trigger == "always":
        return VoiceDecision(True, 1.0, "trigger_always")
    if trigger == "smart":
        from src.ai.voice_fitness import voice_fitness
        smart_cfg = vb.get("smart") if isinstance(vb.get("smart"), dict) else {}
        # voice_block 的 max_chars 已在上方护栏过；并进 smart cfg 保持同一长度口径。
        merged = {"max_chars": max_chars, **smart_cfg}
        return voice_fitness(
            t, peer_sent_voice=peer_sent_voice,
            recent_voice_ratio=recent_voice_ratio,
            peer_emotion=peer_emotion,
            peer_emotion_intensity=peer_emotion_intensity,
            intimacy=intimacy, crisis_block=crisis_block, cfg=merged)
    # when_peer_voice（默认）：你发语音我回语音
    if peer_sent_voice:
        return VoiceDecision(True, 1.0, "peer_voice")
    return VoiceDecision(False, 0.0, "no_peer_voice")


def should_send_voice(
    voice_block: Dict[str, Any],
    text: str,
    *,
    peer_sent_voice: bool = False,
    recent_voice_ratio: float = 0.0,
    peer_emotion: str = "",
    peer_emotion_intensity: float = -1.0,
    intimacy: float = 0.0,
    crisis_block: bool = False,
) -> bool:
    """``decide_voice`` 的布尔投影（向后兼容）。含 reason 的完整决策见 ``decide_voice``。"""
    return decide_voice(
        voice_block, text, peer_sent_voice=peer_sent_voice,
        recent_voice_ratio=recent_voice_ratio, peer_emotion=peer_emotion,
        peer_emotion_intensity=peer_emotion_intensity, intimacy=intimacy,
        crisis_block=crisis_block).send_voice


def persona_allowed_for_voice(
    voice_block: Dict[str, Any], persona_id: Optional[str]
) -> bool:
    """人设级灰度闸门：本人设是否获准发自动语音（与长度/trigger 决策正交）。

    ``persona_allowlist`` 缺省/空 → 不限制（True，向后兼容：所有人设按各自 voice_profile
    发声）。非空 → 仅名单内人设放行，名单外回落纯文本。灰度期用 ``[lin_xiaoyu]`` 把真声
    语音收敛到单一人设，放量时清空名单即可。``persona_id`` 应为**解析后**的真实人设 id
    （编排器号 meta 常无 persona_id → 调用方须先按会话绑定/默认解析再传入）。
    """
    vb = voice_block or {}
    allow = vb.get("persona_allowlist")
    if not allow:
        return True
    try:
        names = {str(x).strip() for x in allow if str(x).strip()}
    except TypeError:
        return True
    if not names:
        return True
    return bool(persona_id) and str(persona_id).strip() in names


async def _synth_ogg(config: Dict[str, Any], persona_id: str, text: str,
                     *, out_dir: str, contact_key: Optional[str] = None,
                     platform: str = "telegram",
                     account_id: Optional[str] = None) -> Optional[str]:
    """合成 TTS → 转 OGG/Opus，返回本地音频路径；任何失败返回 None。

    P3：传入端用户 ``contact_key`` 时，按会员档分层路由 TTS 后端（VIP→旗舰，
    免费→降级省成本）。monetization 未就绪 → tier=None → 不路由（零行为变更）。
    P4：``platform``/``account_id`` 用于解析关系阶段，喂给情感层（默认关）。
    """
    try:
        from src.ai.persona_voice import resolve_effective_voice_context
        voice_ctx = resolve_effective_voice_context(
            config or {}, persona_id=persona_id or None,
            chat_key=contact_key, contact_key=contact_key,
            platform=platform, account_id=account_id, text=text)
        voice_cfg = voice_ctx.get("voice_cfg") or {}
    except Exception:
        logger.debug("[voice_autosend] resolve_voice_cfg 失败", exc_info=True)
        return None
    voice_cfg["enabled"] = True
    voice_cfg["out_dir"] = out_dir
    # 会话语音防「同一段音频复读」：autosend 是对话式回复（非问候/FAQ），字节缓存
    # 命中价值低却会让「相同文字」发出逐字节相同的音频（听感=昨天那条又来了）。
    # 默认关掉字节缓存 → 相同文字也重新合成（克隆后端自带自然变化），绝不复用旧片段；
    # 运营若在 voice_profile 显式配了 tts_cache 则尊重其意愿。
    if "tts_cache" not in voice_cfg:
        voice_cfg["tts_cache"] = {"enabled": False}
    try:
        from src.ai.tts_pipeline import TTSPipeline
        tts = TTSPipeline(voice_cfg)
        result = await tts.synthesize(
            text, timeout_sec=45.0, emotion=voice_ctx.get("emotion"))
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
    contact_key: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """合成语音并落到出站媒体目录，返回 ``(本地路径, /static URL)``；失败返回 None。

    调用方据此 ``orch.send_media(media_path=local, media_url=url, media_type="voice")``。
    ``contact_key``（端用户身份）传入后按会员档分层路由 TTS 后端（默认 None=不路由）。
    """
    od = out_dir or str(Path(tempfile.gettempdir()) / "autosend_voice")
    audio_path = await _synth_ogg(
        config, persona_id, text, out_dir=od, contact_key=contact_key,
        platform=platform, account_id=account_id)
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
    "resolve_voice_autosend_cfg", "decide_voice", "should_send_voice",
    "persona_allowed_for_voice", "stage_voice_file",
    "record_voice_sent", "record_voice_fallback", "record_voice_decision",
    "metrics_snapshot",
]
