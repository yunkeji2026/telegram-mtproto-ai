"""实时共情语音通话网关（WebSocket）。

浏览器麦克风 ⇄ 本网关 ⇄ 语音主机（MiniCPM-o 4.5，全双工）。网关职责：
  1. 鉴权 + 取会话上下文（人设 / 音色参考音 / 长期记忆 / 语言）；
  2. 健康探测语音主机，不可用即优雅回错（不卡死浏览器）；
  3. 把人设画像 + 记忆拼成系统提示、参考音作「音频系统提示」下发 session.init；
  4. 双向中继：浏览器音频/打断 → 主机；主机转写/语音/turn.end → 浏览器。

默认 **关**（``realtime_voice.enabled``）。可测的装配逻辑抽成 ``build_call_init``
（纯函数，单测覆盖）；WS 中继是薄 IO，靠 mock host 做契约测试。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.ai.realtime_voice import (
    EV_ERROR,
    EV_INPUT_AUDIO,
    EV_INTERRUPT,
    EV_OUTPUT_AUDIO,
    EV_READY,
    EV_TRANSCRIPT_ASSISTANT,
    EV_TRANSCRIPT_TRANSLATION,
    EV_TURN_END,
    RealtimeVoiceConfig,
    build_call_system_prompt,
    build_session_init,
    dumps_event,
    parse_host_event,
    pick_language,
)
from src.ai.realtime_voice_client import RealtimeVoiceClient
from src.ai.realtime_voice_stats import get_realtime_voice_stats

try:  # 注解经 ``from __future__ import annotations`` 变字符串，需在模块全局可解析 Request
    from starlette.requests import Request
except Exception:  # pragma: no cover
    Request = Any  # type: ignore[assignment,misc]

try:  # 同理：WS 端点签名 ``ws: WebSocket`` 经 __future__ 注解变字符串，必须在模块全局可解析，
    # 否则 FastAPI 解析不出 WebSocket 参数→握手前即 close（症状：浏览器永远「未连接」）。
    from fastapi import WebSocket, WebSocketDisconnect
except Exception:  # pragma: no cover
    WebSocket = Any  # type: ignore[assignment,misc]
    WebSocketDisconnect = Exception  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


def _b64_file(path: str) -> str:
    """读参考音频→base64；失败返回空串（降级到内置音色）。"""
    try:
        p = Path(path)
        if p.is_file() and p.stat().st_size > 0:
            return base64.b64encode(p.read_bytes()).decode("ascii")
    except Exception:
        pass
    return ""


_REF_AUDIO_DIR = Path("config/voice_refs")
_REF_AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".ogg", ".flac")


def discover_reference_audio(persona_id: str, base_dir: Optional[Path] = None) -> str:
    """按人设 id 在参考音目录找 ``<id>.<ext>``（真人录音克隆素材）；无则返回 ""。

    约定优先于配置：运营把一段干净人声命名为 ``<persona_id>.wav`` 丢进
    ``config/voice_refs/``，通话即自动克隆该音色——无需改配置/重启；文件每通话现读，
    换音频立即生效。空文件 / 缺失一律忽略（降级到内置音色，绝不阻断通话）。
    """
    pid = str(persona_id or "").strip()
    if not pid:
        return ""
    d = Path(base_dir) if base_dir is not None else _REF_AUDIO_DIR
    for ext in _REF_AUDIO_EXTS:
        p = d / f"{pid}{ext}"
        try:
            if p.is_file() and p.stat().st_size > 0:
                return str(p)
        except OSError:
            continue
    return ""


def _ffmpeg_to_16k_mono_wav(path: Path) -> Optional[bytes]:
    """ffmpeg 兜底：任意格式（含 mp3/m4a/aac）→ 16k 单声道 16-bit PCM wav 字节。

    无 ffmpeg / 解码失败 → None（上层回落原始字节，绝不阻断）。
    """
    try:
        import shutil
        import subprocess
        exe = shutil.which("ffmpeg")
        if not exe:
            return None
        out = subprocess.run(
            [exe, "-v", "error", "-i", str(path), "-ac", "1", "-ar", "16000",
             "-t", "20", "-f", "wav", "-acodec", "pcm_s16le", "pipe:1"],
            capture_output=True, timeout=30)
        if out.returncode == 0 and out.stdout:
            return bytes(out.stdout)
    except Exception:
        logger.debug("[voice/ref] ffmpeg 归一化失败", exc_info=True)
    return None


def _normalize_ref_to_16k_mono_wav(path: Path) -> Optional[bytes]:
    """参考音 → **16kHz 单声道 16-bit PCM wav**（克隆引擎最通用的入参）。

    soundfile 读 + soxr 高质量重采样 + 单声道折叠 + 峰值归一 + 限长 20s；
    soundfile 解不了的格式（m4a/aac 等）回落 ffmpeg。任何失败 → None（上层回落原始字节）。
    根因：用户常给 48kHz 立体声录音，而克隆主机要 16k 单声道 → 直接发原始 wav 会合不出声。
    """
    try:
        import io
        import numpy as np
        import soundfile as sf
        try:
            data, sr = sf.read(str(path), dtype="float32", always_2d=False)
        except Exception:
            return _ffmpeg_to_16k_mono_wav(path)
        arr = np.asarray(data, dtype="float32")
        if arr.ndim == 2:                       # 立体声 → 单声道（均值折叠）
            arr = arr.mean(axis=1)
        arr = arr.flatten()
        if arr.size == 0:
            return None
        if int(sr) != 16000:                    # 高质量重采样到 16k
            try:
                import soxr
                arr = soxr.resample(arr, sr, 16000).astype("float32")
            except Exception:
                import librosa
                arr = librosa.resample(arr, orig_sr=int(sr), target_sr=16000).astype("float32")
        arr = arr[: 16000 * 20]                 # 限长 20s（防超长参考拖垮主机）
        peak = float(np.max(np.abs(arr))) if arr.size else 0.0
        if peak > 1e-4:                         # 峰值归一（统一响度，利于克隆稳定）
            arr = arr * (0.97 / peak)
        np.clip(arr, -1.0, 1.0, out=arr)
        buf = io.BytesIO()
        sf.write(buf, arr, 16000, subtype="PCM_16", format="WAV")
        return buf.getvalue()
    except Exception:
        logger.debug("[voice/ref] 归一化失败，回落原始字节", exc_info=True)
        return None


_REF_NORM_CACHE: Dict[str, tuple] = {}   # path → (mtime, size, b64)，避免每通话重复解码


def _b64_ref_audio(path: str) -> str:
    """读参考音 → 归一化 16k 单声道 wav → base64；归一化失败回落原始字节，再失败空串。

    按 (path, mtime, size) 缓存——同一文件只解码一次，换文件/改文件自动失效重算。
    """
    try:
        p = Path(path)
        if not p.is_file():
            return ""
        st = p.stat()
        if st.st_size <= 0:
            return ""
        key = str(p)
        c = _REF_NORM_CACHE.get(key)
        if c and c[0] == st.st_mtime and c[1] == st.st_size:
            return c[2]
        norm = _normalize_ref_to_16k_mono_wav(p)
        raw = norm if norm else p.read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")
        _REF_NORM_CACHE[key] = (st.st_mtime, st.st_size, b64)
        return b64
    except Exception:
        return ""


_REF_HEALTH_CACHE: Dict[str, tuple] = {}   # path → (mtime, size, health)，体检结果按文件签名缓存


def _decode_audio_mono(path: Path):
    """解码任意音频 → (单声道 float32 一维数组, 采样率)；失败 → (None, 0)。

    soundfile 直读（wav/mp3/flac/ogg）→ 失败回落 ffmpeg 归一化后读回（m4a/aac 等）。
    体检用的是**原始音**（未做峰值归一），故削波/电平测得准。
    """
    try:
        import io
        import numpy as np
        import soundfile as sf
        try:
            data, sr = sf.read(str(path), dtype="float32", always_2d=False)
        except Exception:
            nb = _normalize_ref_to_16k_mono_wav(path)
            if not nb:
                return None, 0
            data, sr = sf.read(io.BytesIO(nb), dtype="float32", always_2d=False)
        arr = np.asarray(data, dtype="float32")
        if arr.ndim == 2:
            arr = arr.mean(axis=1)
        return arr.reshape(-1), int(sr)
    except Exception:
        return None, 0


def analyze_reference_file(path: str) -> Dict[str, Any]:
    """对参考音文件做体检（按 path+mtime+size 缓存）。失败/缺依赖 → ``unknown`` 占位（不抛）。"""
    fallback = {"grade": "unknown", "score": 0, "summary": "", "issues": [], "hints": []}
    try:
        p = Path(path)
        if not p.is_file():
            return fallback
        st = p.stat()
        key = str(p)
        c = _REF_HEALTH_CACHE.get(key)
        if c and c[0] == st.st_mtime and c[1] == st.st_size:
            return c[2]
        arr, sr = _decode_audio_mono(p)
        if arr is None:
            health = fallback
        else:
            from src.ai.voice_ref_health import analyze_reference_audio
            health = analyze_reference_audio(arr, sr)
        _REF_HEALTH_CACHE[key] = (st.st_mtime, st.st_size, health)
        return health
    except Exception:
        return fallback


def reference_audio_meta(persona_id: str, base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """参考音状态（供人设卡「已克隆 / 默认音」状态灯）：是否存在 + 时长 + 体检（尽力而为）。"""
    path = discover_reference_audio(persona_id, base_dir)
    if not path:
        return {"has_reference": False, "duration_sec": 0.0, "ref_filename": "", "health": None}
    dur = 0.0
    try:
        import soundfile as sf
        info = sf.info(path)
        dur = round(float(info.frames) / float(info.samplerate or 1), 1)
    except Exception:
        try:
            import wave
            with wave.open(path, "rb") as w:
                dur = round(w.getnframes() / float(w.getframerate() or 1), 1)
        except Exception:
            dur = 0.0
    return {"has_reference": True, "duration_sec": dur, "ref_filename": Path(path).name,
            "health": analyze_reference_file(path)}


def _safe_pid(pid: str) -> bool:
    """人设 id 白名单字符（防路径穿越）：字母/数字/下划线/连字符，≤64。"""
    pid = str(pid or "")
    return bool(pid) and len(pid) <= 64 and all(c.isalnum() or c in "_-" for c in pid)


def build_clone_instructions(voice_ctx: Optional[Dict[str, Any]], text: str = "") -> str:
    """人设情绪 → 克隆主机的语气指令（MiniCPM ``instructions``，作系统侧风格、**绝不读出**）。

    优先用已派生情绪（``voice_ctx.emotion``，会话情绪开关开时有值）；为空/中性则按人设
    基线 + 文本线索**现派生**，保证试听不仅"像本人"还"对语气"。复用与消息渠道克隆同一套
    ``to_qwen_instructions`` 措辞（一致性）。任何异常 → ""（中性，绝不破坏合成）。
    """
    try:
        from src.ai.voice_emotion import coerce_emotion, derive_emotion, to_qwen_instructions
        vc = voice_ctx or {}
        # 运营在 voice_profile 里显式配的语气指令作 base（与消息渠道克隆同口径），情绪在其后叠加、不覆盖。
        vcfg = vc.get("voice_cfg")
        base = str((vcfg or {}).get("instructions") or "").strip() if isinstance(vcfg, dict) else ""
        spec = coerce_emotion(vc.get("emotion"))
        if spec.is_neutral():   # 情绪开关没开/未派生 → 用人设基线+文本现派生
            spec = derive_emotion(text=text, persona=vc.get("persona"))
        return to_qwen_instructions(spec, base=base)
    except Exception:
        logger.debug("[voice/preview] 情绪指令构建失败，按中性", exc_info=True)
        return ""


def build_call_tone_directive(voice_ctx: Optional[Dict[str, Any]],
                              persona: Optional[Dict[str, Any]] = None) -> str:
    """实时通话「首句情绪锚」：人设**语气基线** → 一句文本系统提示指令（绝不念出）。

    与试听克隆同源——让通话从开口第一句就对味（现状靠模型顺对话才慢慢上情绪）。基线取
    runtime ``voice_profile.emotion`` → 回落人设 dict 推断。**只锚人设性格、不掺会话情绪**：
    通话里对方情绪由模型听声实时自适应，故指令把"随对方情绪走（低落先共情、不强行活泼）"
    写进措辞内建安全，无需额外查会话状态。无明确基线 → ""（交给通用共情守则，不冗余）。
    """
    try:
        from src.ai.voice_emotion import EmotionSpec, emotion_tone_descriptor, persona_default_emotion
        emo = ""
        vcfg = (voice_ctx or {}).get("voice_cfg") if isinstance(voice_ctx, dict) else None
        if isinstance(vcfg, dict) and isinstance(vcfg.get("voice_profile"), dict):
            emo = str(vcfg["voice_profile"].get("emotion") or "").strip().lower()
        if not emo:
            emo = persona_default_emotion(persona if isinstance(persona, dict) else None) or ""
        if not emo:
            return ""
        desc = emotion_tone_descriptor(EmotionSpec(emo))
        if not desc:
            return ""
        first = desc.split("、")[0]
        return (f"你天然的语气基调偏【{desc}】，从开口第一句起就自然流露；"
                f"同时随对方情绪走：对方低落/难过/烦躁时先共情安抚、不强行{first}。")
    except Exception:
        logger.debug("[voice/live] 语气基调构建失败，跳过", exc_info=True)
        return ""


_OPENER_DEFAULT = {
    "zh": "在呢，听到你的声音真好，今天过得怎么样呀？",
    "en": "Hey, it's so good to hear your voice. How's your day going?",
}


def build_opener_text(persona: Optional[Dict[str, Any]], language: str = "zh") -> str:
    """通话「主动开场白」文本（纯函数）：人设 ``speaking.openers[0]`` → 清占位/空白 →
    **保证完整句** → 语言兜底。

    开场白是要「说出口」的第一句，不能像 chat opener 那样用省略号吊半句（"哇最近太忙了！你有没有…"
    念出来就像只说了一半）。清洗规则：
      - 去 ``xxxx`` 占位、压空白；
      - **仅省略号（…/.../。。。）视为「没说完」信号**——命中则裁到最后一个完整句末（。！？!?），
        裁不出完整句 → 回落一句完整默认开场；
      - 不动单个正常句末标点，也不误删波浪号/破折号等语气符（"在呢~" 原样保留）。
    无可用 opener → 按语言取一句温暖默认开场。
    """
    lang = language if language in _OPENER_DEFAULT else "zh"
    try:
        txt = ""
        if isinstance(persona, dict):
            openers = ((persona.get("speaking") or {}).get("openers")) or []
            if isinstance(openers, (list, tuple)) and openers:
                txt = str(openers[0] or "")
        txt = re.sub(r"x{2,}", "", txt, flags=re.IGNORECASE)
        txt = re.sub(r"\s+", " ", txt).strip()
        core = re.sub(r"(?:…+|\.{2,}|。{2,})\s*$", "", txt).strip()
        if core != txt and core and core[-1] not in "。！？!?":
            # 原文以省略号吊半句 → 只保留到最后一个完整句末，丢弃残句（无完整句 → 走下方兜底）
            cut = max((core.rfind(c) for c in "。！？!?"), default=-1)
            core = core[:cut + 1] if cut >= 0 else ""
        txt = core
        if len(txt) < 2:
            return _OPENER_DEFAULT[lang]
        return txt[:200]
    except Exception:
        return _OPENER_DEFAULT[lang]


def _wav_to_pcm16(wav_bytes: bytes) -> tuple:
    """WAV 字节 → (PCM16 单声道小端 bytes, sample_rate)；任何异常 → (b"", 0)。

    用 soundfile 读任意 WAV（PCM/float、单/双声道）→ 单声道 float → int16，供浏览器 ``playPcm16`` 直接播。
    """
    try:
        import io
        import numpy as np
        import soundfile as sf
        data, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32", always_2d=False)
        arr = np.asarray(data, dtype="float32")
        if arr.ndim > 1:
            arr = arr.mean(axis=1)
        pcm = (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        sr = int(sr)
        if not pcm or sr <= 0:
            return b"", 0
        return pcm, sr
    except Exception:
        logger.debug("[voice/live] WAV→PCM16 解码失败", exc_info=True)
        return b"", 0


async def _prepare_call_opener(client, ctx: Dict[str, Any],
                               init_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """通话接通前**预合成**开场白（克隆真声）→ {text, pcm, sr}；不满足条件/失败 → None。

    **在实时会话 init 之前合成**：合成走 ``/v1/tts/clone``（``model.chat`` 一次性），与实时会话共用
    同一模型；先合成、后由 ``session.init`` 重置并初始化 token2wav（参考音克隆缓存），保证实时音色
    状态**最后建立、不被一次性合成扰动**。仅在有参考音（能用通话同源音色）且模型已载入时触发。
    """
    try:
        voice_ctx = ctx.get("voice_ctx") if isinstance(ctx, dict) else None
        voice_ctx = voice_ctx if isinstance(voice_ctx, dict) else {}
        vcfg = voice_ctx.get("voice_cfg") if isinstance(voice_ctx.get("voice_cfg"), dict) else {}
        vp = vcfg.get("voice_profile") if isinstance(vcfg.get("voice_profile"), dict) else {}
        ref_path = str(vp.get("reference_audio_path") or "").strip()
        if not ref_path:
            return None    # 无参考音 → 无法用通话同源音色合成开场白，静默跳过
        lang = str(init_payload.get("language") or "zh")
        text = build_opener_text(ctx.get("persona"), lang)
        if not text:
            return None
        try:
            loaded = (await asyncio.to_thread(client.model_status) or {}).get("model_loaded")
        except Exception:
            loaded = False
        if not loaded:
            return None    # 模型未载入 → 跳过开场白（实时连接会另行回 model_not_loaded）
        ref_b64 = _b64_ref_audio(ref_path)
        if not ref_b64:
            return None
        instr = build_clone_instructions(voice_ctx, text)
        wav = await asyncio.wait_for(
            asyncio.to_thread(client.clone_oneshot, text, ref_b64,
                              language=lang, instructions=instr, timeout=18.0),
            timeout=20.0)
        if not wav:
            return None
        pcm, sr = _wav_to_pcm16(wav)
        if not pcm or sr <= 0:
            return None
        return {"text": text, "pcm": pcm, "sr": sr}
    except Exception:
        logger.debug("[voice/live] 开场白预合成失败，跳过", exc_info=True)
        return None


async def _emit_opener(ws, opener: Optional[Dict[str, Any]]) -> None:
    """把预合成的开场白下发给浏览器：transcript.assistant（显示文本）+ output_audio 分块 + turn.end。

    复用现有下行通道（浏览器 ``playPcm16`` 按 ``sample_rate`` 排队播），故前端零改动。任何异常静默吞。
    """
    try:
        if not isinstance(opener, dict):
            return
        text = str(opener.get("text") or "")
        pcm = opener.get("pcm") or b""
        sr = int(opener.get("sr") or 0)
        if not pcm or sr <= 0:
            return
        if text:
            await ws.send_text(dumps_event(
                {"type": EV_TRANSCRIPT_ASSISTANT, "text": text, "final": True,
                 "opener": True, "tid": 0}))
        step = max(1, sr) * 2          # ~1s/块（PCM16 = 2 字节/采样），分块利于尽早起播
        for i in range(0, len(pcm), step):
            chunk = pcm[i:i + step]
            await ws.send_text(dumps_event(
                {"type": EV_OUTPUT_AUDIO,
                 "audio_b64": base64.b64encode(chunk).decode("ascii"),
                 "sample_rate": sr}))
        await ws.send_text(dumps_event({"type": EV_TURN_END}))
    except Exception:
        logger.debug("[voice/live] 开场白下发失败", exc_info=True)


def _resolve_translation_service(app):
    """取 ``app.state.translation_service``；缺失则按 ai_client 现建一个并挂上（与 inbox 同口径）。"""
    try:
        st = getattr(app, "state", None)
        svc = getattr(st, "translation_service", None) if st is not None else None
        if svc is not None:
            return svc
        from src.ai.translation_service import TranslationService
        svc = TranslationService(ai_client=getattr(st, "ai_client", None) if st is not None else None)
        if st is not None:
            try:
                st.translation_service = svc
            except Exception:
                pass
        return svc
    except Exception:
        logger.debug("[voice/live] 取/建 translation_service 失败", exc_info=True)
        return None


async def _send_subtitle(ws, text: str, *, tid: Any, target_lang: str,
                         translation_service) -> None:
    """把一条助手转写译成运营阅读语言 → 下发 ``transcript.translation``（按 ``tid`` 关联气泡）。

    复用统一 ``TranslationService``（术语表 + TM + 语检 + 多引擎 failover + 缓存）：**同语言**
    （provider=identity）/ 空 / 失败 / 未真译 → 静默不发（同语言通话零字幕、零 API 成本）。
    **源语言交给服务自动检测**（不按通话语言硬塞）——这样即便某条文本与通话语言不一致
    （如 zh 人设的固定开场白被用在 en 通话），也能正确识别而非 garble。带 6s 超时；异常全吞。
    """
    try:
        text = str(text or "").strip()
        tgt = str(target_lang or "").strip().lower()
        if not text or not tgt or translation_service is None:
            return
        res = await asyncio.wait_for(
            translation_service.translate(
                text, target_lang=tgt, source_lang="", style="chat"),
            timeout=6.0)
        translated = str(getattr(res, "translated_text", "") or "")
        provider = str(getattr(res, "provider", "") or "")
        if (not bool(getattr(res, "ok", False)) or not translated
                or translated == text or provider in ("identity", "none")):
            return       # 同语言/失败/空/未真译 → 不发字幕
        await ws.send_text(dumps_event({
            "type": EV_TRANSCRIPT_TRANSLATION, "tid": tid,
            "text": translated, "lang": str(getattr(res, "target_lang", tgt) or tgt)}))
    except Exception:
        logger.debug("[voice/live] 字幕翻译失败，跳过", exc_info=True)


def build_call_init(
    *,
    voice_ctx: Dict[str, Any],
    persona: Optional[Dict[str, Any]],
    memory_bullets_text: str = "",
    customer_language: str = "",
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把「已解析的语音上下文 + 人设 + 记忆 + 语言」装配成 session.init 负载（纯函数）。

    - ``voice_ctx``：``resolve_effective_voice_context`` 的返回（含 ``voice_cfg``）。
    - 有参考音 → 作音频系统提示克隆该音色；否则用主机内置 ``voice``。
    - ``memory_bullets_text``：``get_bullets_for_prompt`` 的多行 bullets。
    """
    rvc = RealtimeVoiceConfig.from_config(cfg)
    voice_cfg = (voice_ctx or {}).get("voice_cfg") or {}
    vp = voice_cfg.get("voice_profile") if isinstance(voice_cfg.get("voice_profile"), dict) else {}
    ref_path = str((vp or {}).get("reference_audio_path") or "").strip()
    voice_ref_b64 = _b64_ref_audio(ref_path) if ref_path else ""
    voice = ""
    if not voice_ref_b64:
        voice = str(voice_cfg.get("voice") or (vp or {}).get("speaker_id") or rvc.default_voice or "")
    language = pick_language(None, default=(customer_language or rvc.default_language))
    bullets: List[str] = [b for b in str(memory_bullets_text or "").splitlines() if b.strip()]
    emotion_tone = build_call_tone_directive(voice_ctx, persona)
    system_prompt = build_call_system_prompt(
        persona=persona, memory_bullets=bullets, language=language,
        extra_guidance=rvc.guidance, emotion_tone=emotion_tone)
    return build_session_init(
        system_prompt=system_prompt, language=language,
        voice_ref_b64=voice_ref_b64 or None, voice=voice or None,
        sample_rate=rvc.sample_rate, model=rvc.model)


def register_voice_live_routes(app, *, api_auth=None, config_manager=None) -> None:
    """挂 /api/voice/live（WS 全双工）+ /ops/voice-call（试拨页）。默认关时不挂载。"""
    try:
        from fastapi import WebSocket, WebSocketDisconnect
        from fastapi.responses import HTMLResponse, JSONResponse
    except Exception:  # pragma: no cover
        logger.debug("[voice/live] FastAPI WebSocket 不可用，跳过注册")
        return

    def _full_cfg() -> Dict[str, Any]:
        try:
            if config_manager and hasattr(config_manager, "config"):
                return config_manager.config or {}
        except Exception:
            pass
        return {}

    def _episodic_store(request_app):
        """情景记忆库：优先 ``app.state.episodic_memory``，回落 ``skill_manager._episodic_store``
        （主程序把记忆库挂在 SkillManager 上、未单独挂 app.state，故必须回落才取得到）。"""
        st = getattr(request_app, "state", None)
        return (getattr(st, "episodic_memory", None)
                or getattr(getattr(st, "skill_manager", None), "_episodic_store", None))

    def _resolve_context(request_app, persona_id: str, chat_key: str,
                         memory_key: str = "") -> Dict[str, Any]:
        """取语音上下文 + 记忆。重依赖（PersonaManager/记忆库）全 try/except 降级。

        ``memory_key`` 为平台限定记忆键（bot 管线按 ``platform:chat_key`` 落库），
        语音页只拿到裸 ``chat_key``——故记忆查询优先用 ``memory_key``、回落裸键；
        ``chat_key`` 仍用于语音/情绪上下文（funnel stage 等按裸键），互不干扰。
        """
        cfg = _full_cfg()
        ctx: Dict[str, Any] = {}
        persona: Dict[str, Any] = {}
        try:
            from src.ai.persona_voice import resolve_effective_voice_context
            ctx = resolve_effective_voice_context(
                cfg, persona_id=persona_id or None, chat_key=chat_key or None) or {}
        except Exception:
            logger.debug("[voice/live] resolve_effective_voice_context 失败", exc_info=True)
        try:
            from src.utils.persona_manager import PersonaManager
            pid = persona_id or str(ctx.get("persona_id") or "")
            if pid:
                p = PersonaManager.get_instance().get_persona_by_id(pid)
                if isinstance(p, dict):
                    persona = p
        except Exception:
            logger.debug("[voice/live] persona 取用失败", exc_info=True)
        # 真人参考音自动发现：约定 config/voice_refs/<persona_id>.<ext> → 通话克隆该音色。
        # 显式 voice_profile.reference_audio_path 优先；文件每通话现读，换音立即生效。
        try:
            pid_eff = persona_id or str(ctx.get("persona_id") or "")
            vcfg = ctx.get("voice_cfg") if isinstance(ctx, dict) else None
            if isinstance(vcfg, dict):
                vp = dict(vcfg.get("voice_profile") or {})
                if not str(vp.get("reference_audio_path") or "").strip():
                    ref = discover_reference_audio(pid_eff)
                    if ref:
                        vp["reference_audio_path"] = ref
                        vcfg["voice_profile"] = vp
                        ctx["voice_cfg"] = vcfg
        except Exception:
            logger.debug("[voice/live] 参考音自动发现失败", exc_info=True)
        bullets = ""
        try:
            store = _episodic_store(request_app)
            mem_key = str(memory_key or chat_key or "")
            if store is not None and mem_key:
                bullets = store.get_bullets_for_prompt(mem_key, max_items=8) or ""
                if not bullets and memory_key and str(chat_key) and str(chat_key) != mem_key:
                    bullets = store.get_bullets_for_prompt(str(chat_key), max_items=8) or ""
        except Exception:
            logger.debug("[voice/live] 记忆 bullets 取用失败", exc_info=True)
        return {"voice_ctx": ctx, "persona": persona, "bullets": bullets, "cfg": cfg}

    @app.websocket("/api/voice/live")
    async def voice_live(ws: "WebSocket"):  # noqa: ANN001
        cfg = _full_cfg()
        rvc = RealtimeVoiceConfig.from_config(cfg)
        await ws.accept()
        if not rvc.enabled:
            await ws.send_text(dumps_event({"type": EV_ERROR, "error": "realtime_voice_disabled"}))
            await ws.close()
            return

        stats = get_realtime_voice_stats()
        stats.attempt()

        # 1) 开场握手：{type:"hello", token?, persona_id?, chat_key?, language?}
        try:
            hello = parse_client_hello(await ws.receive_text())
        except Exception:
            stats.ended("hello_error")
            await ws.close()
            return
        access_token = str((cfg.get("realtime_voice") or {}).get("access_token") or "")
        if access_token and hello.get("token") != access_token:
            stats.ended("unauthorized")
            await ws.send_text(dumps_event({"type": EV_ERROR, "error": "unauthorized"}))
            await ws.close()
            return

        ctx = _resolve_context(ws.app, hello.get("persona_id", ""), hello.get("chat_key", ""),
                               hello.get("memory_key", ""))
        init_payload = build_call_init(
            voice_ctx=ctx["voice_ctx"], persona=ctx["persona"],
            memory_bullets_text=ctx["bullets"],
            customer_language=hello.get("language", ""), cfg=ctx["cfg"])

        # 2) 健康探测主机
        client = RealtimeVoiceClient(rvc)
        _host_ok = await asyncio.to_thread(client.health_ok)
        stats.health_probe(bool(_host_ok))
        if not _host_ok:
            stats.ended("host_unreachable")
            await ws.send_text(dumps_event({"type": EV_ERROR, "error": "voice_host_unreachable"}))
            await ws.close()
            return

        # 2.5) 预合成「主动开场白」——**必须在 session.init 之前**，让实时会话的音色克隆缓存
        #      最后建立、不被一次性合成（model.chat）扰动。失败/无参考音/未载入 → None，零阻断。
        opener = await _prepare_call_opener(client, ctx, init_payload) if rvc.opener_enabled else None

        # 3) 连主机 + 下发 session.init
        try:
            session = await client.connect()
        except Exception as ex:  # noqa: BLE001
            stats.ended("connect_failed")
            logger.warning("[voice/live] 连主机失败: %s", ex)
            await ws.send_text(dumps_event({"type": EV_ERROR, "error": f"connect_failed:{str(ex)[:120]}"}))
            await ws.close()
            return

        await session.send_session_init(init_payload)
        _mem_bul = memory_bullets(ctx.get("bullets", ""))
        await ws.send_text(dumps_event({"type": EV_READY, "persona_id": hello.get("persona_id", ""),
                                        "language": init_payload.get("language"),
                                        "memory_count": len(_mem_bul),
                                        "memory_preview": [b[:80] for b in _mem_bul[:6]]}))
        stats.connected()
        _call_t0 = time.time()
        _end_reason = "normal"

        # 3.4) 双语字幕基建：把助手转写译成运营阅读语言叠显（同语言自动跳过、零 API 成本）
        sub_lang = rvc.subtitle_lang or "zh"
        _ts = _resolve_translation_service(ws.app) if rvc.subtitle_enabled else None
        sub_on = bool(rvc.subtitle_enabled) and _ts is not None
        _subtasks: set = set()

        def _spawn_sub(text: str, tid: Any) -> None:
            if not sub_on or not str(text or "").strip():
                return
            task = asyncio.ensure_future(
                _send_subtitle(ws, text, tid=tid, target_lang=sub_lang,
                               translation_service=_ts))
            _subtasks.add(task)
            task.add_done_callback(_subtasks.discard)

        # 4) 双向中继
        async def browser_to_host():
            try:
                while True:
                    msg = await ws.receive_text()
                    ev = parse_client_event(msg)
                    et = ev.get("type")
                    if et == EV_INPUT_AUDIO:
                        await session.send_audio(ev.get("audio_b64", ""), seq=ev.get("seq"))
                    elif et == EV_INTERRUPT:
                        await session.interrupt()
                    elif et:
                        await session.send_event(ev)
            except WebSocketDisconnect:
                raise
            except Exception:
                raise

        async def host_to_browser():
            turn_id = 0
            buf: List[str] = []
            turn_open = False
            while True:
                ev = await session.recv()
                if sub_on and ev.get("type") == EV_TRANSCRIPT_ASSISTANT:
                    if not turn_open:        # 一轮的第一片 → 新 tid（与 opener 的 0 不撞）
                        turn_id += 1
                        turn_open = True
                        buf = []
                    ev = {**ev, "tid": turn_id}
                    if ev.get("final"):
                        full = "".join(buf).strip() or str(ev.get("text") or "").strip()
                        turn_open = False
                        buf = []
                        await ws.send_text(dumps_event(ev))   # 先转发（不阻塞音频流）
                        _spawn_sub(full, turn_id)             # 译文后台补发，按 tid 关联
                        continue
                    txt = str(ev.get("text") or "")
                    if txt:
                        buf.append(txt)
                await ws.send_text(dumps_event(ev))

        try:
            # 3.5) 人设主动先开口（克隆真声开场白），消除接通后的尴尬沉默；无则跳过。
            #      放进 try 内，任何 opener/中继异常都会走 finally 收口（记结束 + 关会话），
            #      避免 connected() 已计但 ended() 漏计导致 active 泄漏 / 会话不释放。
            if opener:
                await _emit_opener(ws, opener)
                _spawn_sub(str(opener.get("text") or ""), 0)   # 开场白也配字幕（tid=0）
            await asyncio.gather(browser_to_host(), host_to_browser())
        except WebSocketDisconnect:
            pass
        except Exception as ex:  # noqa: BLE001
            _end_reason = "relay_error"
            logger.debug("[voice/live] 中继结束: %s", ex)
            try:
                await ws.send_text(dumps_event({"type": EV_ERROR, "error": "relay_ended"}))
            except Exception:
                pass
        finally:
            stats.ended(_end_reason, was_connected=True,
                        duration_sec=max(0.0, time.time() - _call_t0))
            for _t in list(_subtasks):     # 取消未决字幕任务，避免 loop 关停告警
                _t.cancel()
            await session.close()
            try:
                await ws.close()
            except Exception:
                pass

    # ── 试拨页（运营自测；公网暴露请置于同等鉴权/代理后）──
    @app.get("/ops/voice-call")
    async def voice_call_page(request: Request):  # noqa: ANN001
        from src.web.admin import templates
        return templates.TemplateResponse(request, "voice_call.html", {})

    # ── 语音引擎开关（按需占用 / 释放显存；与别的 AI 服务共用同卡时手动控制）──
    def _engine_authed(token: str) -> bool:
        access_token = str((_full_cfg().get("realtime_voice") or {}).get("access_token") or "")
        return (not access_token) or token == access_token

    @app.get("/api/voice/engine/status")
    async def voice_engine_status():  # noqa: ANN001
        rvc = RealtimeVoiceConfig.from_config(_full_cfg())
        if not rvc.enabled:
            return JSONResponse({"enabled": False, "model_loaded": False,
                                 "error": "realtime_voice_disabled"})
        st = await asyncio.to_thread(RealtimeVoiceClient(rvc).model_status)
        st["enabled"] = True
        access_token = str((_full_cfg().get("realtime_voice") or {}).get("access_token") or "")
        st["auth_required"] = bool(access_token)
        return JSONResponse(st)

    @app.post("/api/voice/engine/load")
    async def voice_engine_load(token: str = ""):  # noqa: ANN001
        rvc = RealtimeVoiceConfig.from_config(_full_cfg())
        if not rvc.enabled:
            return JSONResponse({"error": "realtime_voice_disabled"}, status_code=409)
        if not _engine_authed(token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        out = await asyncio.to_thread(RealtimeVoiceClient(rvc).load_model)
        get_realtime_voice_stats().engine_action("load")
        return JSONResponse(out)

    @app.post("/api/voice/engine/unload")
    async def voice_engine_unload(token: str = ""):  # noqa: ANN001
        rvc = RealtimeVoiceConfig.from_config(_full_cfg())
        if not rvc.enabled:
            return JSONResponse({"error": "realtime_voice_disabled"}, status_code=409)
        if not _engine_authed(token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        out = await asyncio.to_thread(RealtimeVoiceClient(rvc).unload_model)
        get_realtime_voice_stats().engine_action("unload")
        return JSONResponse(out)

    @app.get("/api/voice/live/readiness")
    async def voice_live_readiness(request: Request):  # noqa: ANN001
        """试拨前校准：config + 参考音体检 + 功能链(opener/字幕/记忆) + 引擎载入态。"""
        from src.companion.realtime_voice_calibration import realtime_voice_calibration

        cfg = _full_cfg()
        rvc = RealtimeVoiceConfig.from_config(cfg)
        ref_summary = collect_voice_ref_readiness_summary()
        engine_loaded = None
        if rvc.enabled:
            try:
                st = await asyncio.to_thread(RealtimeVoiceClient(rvc).model_status)
                engine_loaded = bool(st.get("model_loaded"))
            except Exception:
                engine_loaded = False
        mem = _episodic_store(request.app) is not None
        cal = realtime_voice_calibration(
            cfg, ref_summary=ref_summary, engine_loaded=engine_loaded, memory_store=mem)
        return JSONResponse({"ok": True, "enabled": rvc.enabled, **cal})

    @app.get("/api/voice/personas")
    async def voice_personas():  # noqa: ANN001
        """列出可选人设（供试拨页人设选择器）。只读、容错，失败降级为空列表。

        返回 {personas:[{id,name,role,tags,emotion,opener,has_voice}]}。``opener``
        取自人设 speaking.openers[0]，为后续「试听」预留（当前前端未用即忽略）。
        """
        items: List[Dict[str, Any]] = []
        try:
            from src.utils.persona_manager import PersonaManager
            pm = PersonaManager.get_instance()
            for s in pm.list_profiles_summary():
                pid = s.get("id")
                emotion, opener = "", ""
                try:
                    full = pm.get_persona_by_id(pid) or {}
                    vp = full.get("voice_profile") or {}
                    emotion = str(vp.get("emotion") or "")
                    # 与通话开场白同口径清洗（保证完整句，不吊半句）——前端展示/试听都拿到能直接说出口的整句
                    opener = build_opener_text(full, "zh")
                except Exception:
                    pass
                avatar_url = ""
                try:
                    if pid and Path(f"src/web/static/persona_avatars/{pid}.png").is_file():
                        avatar_url = f"/static/persona_avatars/{pid}.png"
                except Exception:
                    pass
                items.append({
                    "id": pid,
                    "name": s.get("name") or pid,
                    "role": s.get("role") or "",
                    "tags": list(s.get("tags") or [])[:4],
                    "emotion": emotion,
                    "opener": opener,
                    "has_voice": bool(s.get("has_voice")),
                    "avatar_url": avatar_url,
                    **reference_audio_meta(pid),
                })
        except Exception:
            logger.debug("[voice/live] 人设列表获取失败", exc_info=True)
        return JSONResponse({"personas": items})

    @app.post("/api/voice/persona-voice")
    async def upload_persona_voice(request: Request, persona_id: str = "", token: str = ""):  # noqa: ANN001
        """上传人设参考音（真人录音）→ 归一化 16k 单声道 wav → 存 ``config/voice_refs/<id>.wav``。

        浏览器直传文件原始字节（body=File），免 multipart 依赖；同源 → 过 CSRF。
        解码不了的文件拒收（``decode_failed``）；成功后通话即自动克隆该音色。
        """
        if not _engine_authed(token):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        pid = str(persona_id or "").strip()
        if not _safe_pid(pid):
            return JSONResponse({"ok": False, "error": "bad_persona_id"}, status_code=400)
        try:
            body = await request.body()
        except Exception:
            body = b""
        if not body:
            return JSONResponse({"ok": False, "error": "empty_body"}, status_code=400)
        if len(body) > 20 * 1024 * 1024:
            return JSONResponse({"ok": False, "error": "too_large"}, status_code=413)
        try:
            _REF_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        tmp = _REF_AUDIO_DIR / f".upload_{pid}.tmp"
        health = {"grade": "unknown", "score": 0, "summary": "", "issues": [], "hints": []}
        try:
            tmp.write_bytes(body)
            norm = _normalize_ref_to_16k_mono_wav(tmp)
            if not norm:
                return JSONResponse({"ok": False, "error": "decode_failed"}, status_code=400)
            # 体检用**原始上传音**（未做峰值归一）→ 削波/电平测得准；失败不阻断保存
            try:
                _arr, _sr = _decode_audio_mono(tmp)
                if _arr is not None:
                    from src.ai.voice_ref_health import analyze_reference_audio
                    health = analyze_reference_audio(_arr, _sr)
            except Exception:
                logger.debug("[voice/persona-voice] 体检失败（不阻断）", exc_info=True)
            for ext in _REF_AUDIO_EXTS:   # 清掉旧的其它扩展名变体，保证发现唯一
                (_REF_AUDIO_DIR / f"{pid}{ext}").unlink(missing_ok=True)
                _REF_NORM_CACHE.pop(str(_REF_AUDIO_DIR / f"{pid}{ext}"), None)
                _REF_HEALTH_CACHE.pop(str(_REF_AUDIO_DIR / f"{pid}{ext}"), None)
            dest = _REF_AUDIO_DIR / f"{pid}.wav"
            dest.write_bytes(norm)
        except Exception as ex:  # noqa: BLE001
            logger.warning("[voice/persona-voice] 保存失败: %s", ex)
            return JSONResponse({"ok": False, "error": f"save_failed:{str(ex)[:80]}"}, status_code=500)
        finally:
            tmp.unlink(missing_ok=True)
        _REF_NORM_CACHE.pop(str(dest), None)
        # 用原始音体检结果填 dest 缓存（比事后读归一化文件更准），供后续列表/选择复用
        try:
            _st = dest.stat()
            _REF_HEALTH_CACHE[str(dest)] = (_st.st_mtime, _st.st_size, health)
        except Exception:
            pass
        meta = reference_audio_meta(pid)
        meta["health"] = health
        return JSONResponse({"ok": True, "persona_id": pid, **meta})

    @app.delete("/api/voice/persona-voice")
    async def delete_persona_voice(persona_id: str = "", token: str = ""):  # noqa: ANN001
        """删除人设参考音 → 回落主机内置音色。容错：无文件也返回 ok。"""
        if not _engine_authed(token):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        pid = str(persona_id or "").strip()
        if not _safe_pid(pid):
            return JSONResponse({"ok": False, "error": "bad_persona_id"}, status_code=400)
        removed: List[str] = []
        for ext in _REF_AUDIO_EXTS:
            f = _REF_AUDIO_DIR / f"{pid}{ext}"
            try:
                if f.is_file():
                    f.unlink(missing_ok=True)
                    removed.append(f.name)
            except Exception:
                pass
            _REF_NORM_CACHE.pop(str(f), None)
            _REF_HEALTH_CACHE.pop(str(f), None)
        return JSONResponse({"ok": True, "persona_id": pid, "removed": removed,
                             "has_reference": False, "health": None})

    @app.get("/api/voice/conversations")
    async def voice_conversations(token: str = "", limit: int = 30):  # noqa: ANN001
        """列出最近会话供「会话标识」下拉（带入某客户长期记忆，免手填 chat_key）。

        只读、容错：无 inbox_store / 异常一律降级空列表。仅私聊（chat_type='private'
        或空）且 chat_key 非空者入选，按 chat_key 去重、最近优先。含 PII（客户名 + 末条
        预览），故与试听/开关同走 access_token 闸（未配置 token → 开放，与本机一致）。
        chat_key 取会话表原值——与 ``resolve_effective_voice_context(chat_key=...)`` 同口径。
        """
        if not _engine_authed(str(token or "")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        items: List[Dict[str, Any]] = []
        epi = _episodic_store(app)

        def _has_mem(key: str) -> bool:
            if epi is None or not key:
                return False
            try:
                return bool((epi.get_bullets_for_prompt(str(key), max_items=1) or "").strip())
            except Exception:
                return False

        try:
            store = getattr(getattr(app, "state", None), "inbox_store", None)
            if store is not None and hasattr(store, "list_conversations"):
                n = max(1, min(100, int(limit or 30)))
                seen = set()
                for c in store.list_conversations(limit=n * 2):
                    ck = str(c.get("chat_key") or "").strip()
                    if not ck or ck in seen:
                        continue
                    if str(c.get("chat_type") or "private") not in ("private", ""):
                        continue  # 群组/频道 chat_key 不适合 1:1 陪伴通话
                    seen.add(ck)
                    plat = str(c.get("platform") or "")
                    # 记忆键：bot 管线按 platform:chat_key 落库；探测两种候选取命中者
                    cand = f"{plat}:{ck}" if plat else ck
                    mem_key, has_mem = cand, _has_mem(cand)
                    if not has_mem and cand != ck and _has_mem(ck):
                        mem_key, has_mem = ck, True
                    name = str(c.get("display_name") or "").strip() or ck
                    last = str(c.get("last_text") or "").strip().replace("\n", " ")
                    if len(last) > 40:
                        last = last[:40] + "…"
                    items.append({
                        "chat_key": ck,
                        "memory_key": mem_key,
                        "has_memory": has_mem,
                        "name": name,
                        "platform": plat,
                        "subtitle": last,
                        "last_ts": float(c.get("last_ts") or 0.0),
                    })
                    if len(items) >= n:
                        break
        except Exception:
            logger.debug("[voice/live] 最近会话列表获取失败", exc_info=True)
        return JSONResponse({"conversations": items})

    @app.post("/api/voice/preview")
    async def voice_preview(request: Request):  # noqa: ANN001
        """试听：用人设音色把一句样句合成成内联音频（base64）返回。

        与真实发送共用 ``resolve_effective_voice_context`` + ``TTSPipeline``（音色一致），
        但走本页 access_token 鉴权 + 内联返回，与 admin-session 的 /api/voice/tts-test 解耦。
        注意：试听用 voice_profile（如 edge_tts）作**音色参考**；实时通话音色由语音主机
        （MiniCPM-o）决定，二者可能不同。任何失败优雅返回 {ok:false,error}，不抛 500。
        """
        cfg = _full_cfg()
        rvc = RealtimeVoiceConfig.from_config(cfg)
        if not rvc.enabled:
            return JSONResponse({"ok": False, "error": "realtime_voice_disabled"}, status_code=409)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if not _engine_authed(str(body.get("token") or "")):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        persona_id = str(body.get("persona_id") or "").strip() or None
        text = str(body.get("text") or "").strip()[:200]
        if not text:
            return JSONResponse({"ok": False, "error": "empty_text"}, status_code=400)
        # 先解析语音上下文（人设 + voice_cfg + 情绪）——克隆与 edge 两条试听路共用同一份，
        # 既省一次重复解析，也让克隆路拿得到人设情绪去构建语气指令。
        try:
            from src.ai.persona_voice import resolve_effective_voice_context
            voice_ctx = resolve_effective_voice_context(cfg, persona_id=persona_id, text=text)
            voice_cfg = dict(voice_ctx.get("voice_cfg") or {})
        except Exception:
            logger.debug("[voice/preview] voice ctx 解析失败", exc_info=True)
            voice_ctx, voice_cfg = {"emotion": None, "persona": {}}, {}
        # 优先「克隆真声」：引擎已载 + 人设有真人参考音 → 走 MiniCPM /v1/tts/clone，
        # 试听音色与真实通话同源，并把人设情绪作 instructions（系统侧语气，绝不读出）一并带上，
        # 使试听不仅"像本人"还"对语气"。否则（无参考音 / 引擎未载 / 异常）回落 edge_tts 参考音色。
        if persona_id:
            try:
                ref_path = discover_reference_audio(persona_id)
                if ref_path:
                    client = RealtimeVoiceClient(rvc)
                    if (await asyncio.to_thread(client.model_status)).get("model_loaded"):
                        ref_b64 = _b64_ref_audio(ref_path)
                        lang = pick_language(text, default=rvc.default_language)
                        instr = build_clone_instructions(voice_ctx, text)
                        wav = await asyncio.wait_for(
                            asyncio.to_thread(client.clone_oneshot, text, ref_b64,
                                              language=lang, instructions=instr, timeout=45.0),
                            timeout=48.0)
                        if wav:
                            return JSONResponse({
                                "ok": True,
                                "audio_b64": base64.b64encode(wav).decode("ascii"),
                                "mime": "audio/wav", "format": "wav",
                                "engine": "clone", "voice": "cloned",
                                "instructions": instr, "bytes": len(wav),
                            })
            except Exception:
                logger.debug("[voice/preview] 克隆试听失败，回落 edge", exc_info=True)
        voice_cfg["enabled"] = True
        preview_dir = Path("tmp_tts_preview")
        try:
            preview_dir.mkdir(parents=True, exist_ok=True)
            voice_cfg["out_dir"] = str(preview_dir)
        except Exception:
            pass
        try:
            from src.ai.tts_pipeline import TTSPipeline
            tts = TTSPipeline(voice_cfg)
            result = await asyncio.wait_for(
                tts.synthesize(text, timeout_sec=45.0, emotion=voice_ctx.get("emotion")),
                timeout=50.0)
        except Exception as ex:  # noqa: BLE001
            logger.warning("[voice/preview] 合成失败: %s", ex)
            return JSONResponse({"ok": False, "error": f"synth_failed:{str(ex)[:120]}"})
        if not getattr(result, "ok", False):
            return JSONResponse({"ok": False, "error": getattr(result, "error", "synth_failed")})
        try:
            audio_path = Path(result.audio_path)
            data = audio_path.read_bytes()
            audio_path.unlink(missing_ok=True)  # 试听不留盘，立即清理
        except Exception as ex:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"read_failed:{str(ex)[:120]}"})
        fmt = str(getattr(result, "format", "") or "mp3").lower()
        mime = {"mp3": "audio/mpeg", "ogg": "audio/ogg", "wav": "audio/wav",
                "opus": "audio/ogg"}.get(fmt, "audio/mpeg")
        return JSONResponse({
            "ok": True,
            "audio_b64": base64.b64encode(data).decode("ascii"),
            "mime": mime, "format": fmt,
            "engine": "edge",
            "voice": getattr(result, "voice", ""),
            "provider": getattr(result, "provider", ""),
            "duration_sec": getattr(result, "duration_sec", None),
            "bytes": len(data),
        })


def parse_client_hello(raw: Any) -> Dict[str, Any]:
    """解析浏览器开场握手（容错）。返回 {token,persona_id,chat_key,memory_key,language}。"""
    import json
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "token": str(data.get("token") or ""),
        "persona_id": str(data.get("persona_id") or ""),
        "chat_key": str(data.get("chat_key") or ""),
        "memory_key": str(data.get("memory_key") or ""),
        "language": str(data.get("language") or ""),
    }


def parse_client_event(raw: Any) -> Dict[str, Any]:
    """解析浏览器→网关的事件（容错），未知/坏包返回 {}（中继层忽略）。"""
    import json
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def memory_bullets(text: str) -> List[str]:
    """记忆 bullets 文本（换行分隔）→ 去空白的条目列表；供计数 + 预览。"""
    return [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]


def count_memory_bullets(text: str) -> int:
    """非空记忆条目数；供前端「已带入 N 条记忆」提示。"""
    return len(memory_bullets(text))


def collect_voice_ref_readiness_summary(base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """聚合各 persona 参考音体检（供 readiness / 校准 / ops 卡）。"""
    from src.companion.realtime_voice_ref_readiness import summarize_voice_ref_rows
    rows: List[Dict[str, Any]] = []
    try:
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        for s in pm.list_profiles_summary():
            pid = str(s.get("id") or "").strip()
            if not pid:
                continue
            meta = reference_audio_meta(pid, base_dir)
            rows.append({"persona_id": pid, "name": s.get("name") or pid, **meta})
    except Exception:
        logger.debug("[voice/live] ref summary 失败", exc_info=True)
    return summarize_voice_ref_rows(rows)


__all__ = [
    "register_voice_live_routes",
    "build_call_init",
    "parse_client_hello",
    "parse_client_event",
    "count_memory_bullets",
    "memory_bullets",
    "discover_reference_audio",
    "reference_audio_meta",
    "analyze_reference_file",
    "collect_voice_ref_readiness_summary",
    "build_clone_instructions",
    "build_call_tone_directive",
    "build_opener_text",
    "_send_subtitle",
]
