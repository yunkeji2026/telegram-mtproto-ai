"""Text-to-speech pipeline for Messenger voice replies.

The pipeline is deliberately lazy and soft-failing, matching audio_pipeline:
local providers are cheap for testing, online providers are better for voice
quality, and failures return structured errors so Messenger can fall back to
text/approval without blocking the RPA loop.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shlex
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# ── 不可达主机短路缓存 ───────────────────────────────────────────────────────
# coqui_http / voice_clone 等局域网主机离线时，若每次合成都做 3s TCP 预检，会白等
# 3s + 刷一条 WARNING。这里缓存「最近探测不可达」的 host:port，冷却期内直接秒回落，
# 不再触网；主机恢复后（冷却到期再探一次成功）自动清缓存复用。进程级共享、线程安全。
_TTS_UNREACHABLE_TTL_SEC = 60.0
_tts_unreachable_lock = threading.Lock()
_tts_unreachable_until: Dict[str, float] = {}  # "host:port" -> monotonic 解禁时刻


# 这些错误源于配置/授权问题（非传输故障），不应被「兜底合成」掩盖——
# 否则会用通用音色悄悄绕过 owner_consent 等门禁，或藏住缺文件/缺命令的配置错误。
_NON_FALLBACK_ERROR_MARKERS = (
    "voice_profile_requires_owner_consent",
    "voice_profile_missing_reference_audio_path",
    "voice_profile_reference_audio_missing",
    "voice_profile_missing_command",
    "backend disabled",      # 用户显式关闭 TTS（backend: disabled）→ 不得兜底
    "unknown backend",       # 配置写错后端名 → 暴露而非掩盖
    "empty_text",
    "pipeline_disabled",
    # ElevenLabs 本地配置错误（缺 key/voice_id）：本地即可判定的 misconfig，
    # 应暴露给运营修正，而非用通用音色静默掩盖（API 侧 401/配额错误仍走兜底出声）。
    "elevenlabs_missing_api_key",
    "elevenlabs_missing_voice_id",
)


def _is_non_fallback_error(err: Optional[str]) -> bool:
    """判断错误是否属于「配置/授权类」——是则不走兜底，直接暴露。"""
    if not err:
        return False
    return any(m in err for m in _NON_FALLBACK_ERROR_MARKERS)


def _assert_http_reachable(base_url: str, timeout: float = 3.0) -> None:
    """对 base_url 的 host:port 做一次短超时 TCP 连接预检；不可达则抛异常。

    用于在真正发起（可能 300s 超时的）合成请求前快速判断局域网/云主机是否在线，
    把「主机离线」从 OS 级 ~21s 连接超时（Windows WinError 10060）缩短到 ~3s，
    让上层兜底（edge_tts）几乎即时生效。
    """
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    host = parsed.hostname
    if not host:
        return  # 解析不出主机名就不预检，交给后续请求自然报错
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    key = f"{host}:{port}"
    now = time.monotonic()
    # 冷却期内：跳过 TCP 探测，直接抛「缓存命中」错（上层据此秒回落且不刷 WARNING）。
    with _tts_unreachable_lock:
        until = _tts_unreachable_until.get(key, 0.0)
    if until and now < until:
        raise RuntimeError(
            f"tts_host_unreachable_cached:{key}:retry_in_{int(until - now)}s")
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            with _tts_unreachable_lock:
                _tts_unreachable_until.pop(key, None)  # 探测成功 → 解除短路
            return
    except OSError as exc:
        with _tts_unreachable_lock:
            _tts_unreachable_until[key] = now + _TTS_UNREACHABLE_TTL_SEC
        raise RuntimeError(
            f"tts_host_unreachable:{host}:{port}:{type(exc).__name__}") from exc


# ── TTS 输出缓存（进程级、有界 LRU、线程安全）────────────────────────────────
# 问候 / FAQ / 常用句会被反复合成——同一 (text, voice, backend, format, emotion,
# 参考音频指纹) 命中缓存可直接复用字节，省外部调用与延迟。只缓存**字节**（短语音
# 几 KB~几百 KB），neutral 情绪 == 与升级前完全一致的输出，缓存对行为无副作用。
_TTS_CACHE_LOCK = threading.Lock()
_TTS_CACHE: "OrderedDict[str, Tuple[bytes, str, str, str]]" = OrderedDict()
# value = (audio_bytes, fmt, provider, voice)
_TTS_CACHE_MAX = 128


def _tts_cache_get(key: str) -> Optional[Tuple[bytes, str, str, str]]:
    if not key:
        return None
    with _TTS_CACHE_LOCK:
        item = _TTS_CACHE.get(key)
        if item is None:
            return None
        _TTS_CACHE.move_to_end(key)
        return item


def _tts_cache_put(key: str, value: Tuple[bytes, str, str, str], *, max_entries: int) -> None:
    if not key or not value or not value[0]:
        return
    with _TTS_CACHE_LOCK:
        _TTS_CACHE[key] = value
        _TTS_CACHE.move_to_end(key)
        cap = max(1, int(max_entries))
        while len(_TTS_CACHE) > cap:
            _TTS_CACHE.popitem(last=False)


def reset_tts_cache() -> None:
    """清空 TTS 输出缓存（测试用 / 音色变更后强制重合成）。"""
    with _TTS_CACHE_LOCK:
        _TTS_CACHE.clear()


def _reference_fingerprint(voice_profile: Dict[str, Any]) -> str:
    """参考音频指纹（路径+大小+mtime）——换了参考音频则缓存键自动失效。"""
    try:
        ref = str((voice_profile or {}).get("reference_audio_path") or "").strip()
        if not ref:
            return ""
        st = os.stat(ref)
        return f"{ref}:{st.st_size}:{int(st.st_mtime)}"
    except Exception:
        return ""


@dataclass
class TTSResult:
    ok: bool = False
    audio_path: str = ""
    text: str = ""
    provider: str = ""
    voice: str = ""
    format: str = ""
    latency_ms: int = 0
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
    # P3-A: 合成完成后测得的音频时长（秒）；-1.0 = 未能测量
    duration_sec: float = -1.0
    # P3-A: 时长测量来源："wave_header" | "mp3_frame" | "ffprobe" | "mutagen" | "unknown"
    duration_source: str = "unknown"


class TTSPipeline:
    """Generate speech from text.

    Config:
        enabled: true/false
        backend: edge_tts | pyttsx3 | openai | elevenlabs | voice_clone_command | coqui_http | disabled
        voice: provider-specific voice id
        model: online model name, defaults to gpt-4o-mini-tts
        format: mp3 | wav | opus
        out_dir: tmp_voice_replies
        api_key/base_url: online provider credentials
        voice_profile:
          enabled: true
          owner_consent: true
          speaker_id: my_voice
          reference_audio_path: D:/voice/me.wav
          backend: voice_clone_command
          command_args: [python, tools/glm_tts_infer.py, --text, "{text}", --ref, "{reference_audio}", --out, "{out}"]
          command_template: python tools/glm_tts_infer.py --text {text} --ref {reference_audio} --out {out}
    """

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.backend = str(cfg.get("backend", "edge_tts")).strip().lower()
        self.voice = str(
            cfg.get("voice")
            or ("ja-JP-NanamiNeural" if self.backend == "edge_tts" else "alloy")
        ).strip()
        self.model = str(cfg.get("model") or "gpt-4o-mini-tts").strip()
        self.format = str(cfg.get("format") or "mp3").strip().lower()
        self.out_dir = Path(str(cfg.get("out_dir") or "tmp_voice_replies"))
        self.api_key = str(cfg.get("api_key") or "").strip()
        self.dashscope_api_key = str(cfg.get("dashscope_api_key") or "").strip()
        self.dashscope_region = str(cfg.get("dashscope_region") or "").strip()
        self.base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
        self.instructions = str(cfg.get("instructions") or "").strip()
        self.voice_profile = (
            cfg.get("voice_profile") if isinstance(cfg.get("voice_profile"), dict) else {}
        )
        # 局域网克隆主机配置（LAN 优先 → 云端兜底）；由 resolve_voice_cfg 注入
        self.voice_clone_lan = (
            cfg.get("voice_clone_lan")
            if isinstance(cfg.get("voice_clone_lan"), dict)
            else {}
        )
        # ── 后端不可达/失败时的兜底合成 ──────────────────────────────────────
        # 主后端（如 coqui_http / voice_clone_command 指向的局域网/云主机）连不上时，
        # 回落到免额外基建的在线 edge_tts，避免「生成失败 + WinError 10060」直接抛给用户。
        # 兜底会丢掉克隆音色（换成通用音色），但「有声音」远胜「硬失败」。
        self.fallback_on_error = bool(cfg.get("fallback_on_error", True))
        self.fallback_backend = str(cfg.get("fallback_backend") or "edge_tts").strip().lower()
        self.fallback_voice = str(cfg.get("fallback_voice") or "zh-CN-XiaoxiaoNeural").strip()
        # ── P0：TTS 输出缓存（默认开；neutral 输出与升级前一致，缓存无行为副作用）──
        cache_cfg = cfg.get("tts_cache") if isinstance(cfg.get("tts_cache"), dict) else {}
        self.cache_enabled = bool(cache_cfg.get("enabled", True))
        self.cache_max_entries = int(cache_cfg.get("max_entries", _TTS_CACHE_MAX) or _TTS_CACHE_MAX)
        # ── P1：情感层（默认关 → 不传 emotion 即 neutral，零行为变更）──
        emo_cfg = cfg.get("emotion") if isinstance(cfg.get("emotion"), dict) else {}
        self.emotion_enabled = bool(emo_cfg.get("enabled", False))
        self.emotion_default = str(emo_cfg.get("default") or "warm").strip().lower()
        # ── P2-Cloud：ElevenLabs v3 付费情感旗舰档配置 ──
        self.elevenlabs = (
            cfg.get("elevenlabs") if isinstance(cfg.get("elevenlabs"), dict) else {}
        )
        # ── P3：可观测（provider_stats "tts" namespace）+ 成本费率 ──
        self.metrics_enabled = bool(cfg.get("metrics_enabled", True))
        self.cost_rates = (
            cfg.get("cost_per_1k_chars")
            if isinstance(cfg.get("cost_per_1k_chars"), dict) else {}
        )

    def stats(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "voice": self.voice,
            "model": self.model,
            "format": self.format,
            "out_dir": str(self.out_dir),
            "voice_profile_enabled": bool(self.voice_profile.get("enabled", False)),
            "voice_profile_speaker": str(self.voice_profile.get("speaker_id") or ""),
        }

    async def synthesize(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        timeout_sec: float = 30.0,
        emotion: Any = None,
    ) -> TTSResult:
        """合成语音。``emotion`` 可为 None / 情绪字符串 / dict / EmotionSpec。

        - 不传 ``emotion`` 且未开 ``emotion.enabled`` → neutral（与升级前完全一致）。
        - 命中 TTS 缓存（同 text+voice+backend+format+情绪+参考音频指纹）→ 直接复用字节。
        """
        from src.ai.voice_emotion import NEUTRAL, coerce_emotion, derive_emotion

        text_s = str(text or "")
        if emotion is not None:
            spec = coerce_emotion(emotion)
        elif self.emotion_enabled:
            spec = derive_emotion(text=text_s, default=self.emotion_default)
        else:
            spec = NEUTRAL

        # ── P0：缓存查找（命中即秒回，省外部调用）──
        if self.enabled and self.cache_enabled and text_s.strip():
            eff_backend = self._effective_backend()
            eff_voice = voice or self._effective_voice()
            cache_key = self._cache_key(text_s, eff_voice, eff_backend, spec)
            hit = _tts_cache_get(cache_key)
            if hit is not None:
                cached = self._result_from_cache(hit, text_s)
                if cached is not None:
                    self._record_stats(cached, text_s, cache_hit=True)
                    return cached
        else:
            cache_key = ""

        rv = await self._synthesize_uncached(
            text_s, voice=voice, timeout_sec=timeout_sec, spec=spec)

        # ── 成功且非缓存命中 → 写入缓存 ──
        if (self.cache_enabled and cache_key and rv.ok and rv.audio_path
                and not rv.extra.get("cache_hit")):
            try:
                data = Path(rv.audio_path).read_bytes()
                if data:
                    _tts_cache_put(
                        cache_key, (data, rv.format, rv.provider, rv.voice),
                        max_entries=self.cache_max_entries)
            except Exception:
                pass
        self._record_stats(rv, text_s, cache_hit=False)
        return rv

    def _record_stats(self, rv: "TTSResult", text: str, *, cache_hit: bool) -> None:
        """记 TTS 用量到 provider_stats "tts" namespace（成功/失败/成本/缓存命中）。绝不抛。"""
        if not self.metrics_enabled:
            return
        try:
            from src.ai.provider_stats import get_provider_stats
            stats = get_provider_stats("tts", "tts")
            if cache_hit:
                stats.record_cache_hit()
                return
            # 仅在该轮真正发生过合成（含失败）时记一次
            if not rv.text.strip():
                return
            provider = rv.provider or self._effective_backend()
            if rv.ok:
                from src.ai.voice_routing import estimate_tts_cost
                cost = estimate_tts_cost(provider, len(text or ""), self.cost_rates)
                stats.record(provider, ok=True, latency_ms=rv.latency_ms, cost_usd=cost)
                if rv.extra.get("fallback_from"):
                    stats.record_fallback()
            elif rv.error not in ("pipeline_disabled", "empty_text"):
                stats.record(provider, ok=False, latency_ms=rv.latency_ms)
        except Exception:
            pass

    def _cache_key(self, text: str, voice: str, backend: str, spec: Any) -> str:
        """TTS 缓存键：克隆类后端额外并入参考音频指纹（换音频自动失效）。"""
        ref_fp = ""
        if backend in ("voice_clone_lan", "voice_clone_command", "coqui_http"):
            ref_fp = _reference_fingerprint(self.voice_profile)
        emo = spec.cache_key() if spec is not None else ""
        base = "|".join([
            backend, voice or "", self.format, self.model or "",
            self.instructions or "", emo, ref_fp, text,
        ])
        return hashlib.sha1(base.encode("utf-8")).hexdigest()

    def _result_from_cache(
        self, hit: Tuple[bytes, str, str, str], text: str,
    ) -> Optional["TTSResult"]:
        """把缓存字节落盘成新文件并构造 TTSResult。失败返回 None（回落正常合成）。"""
        data, fmt, provider, voice = hit
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            suffix = fmt or self.format
            out = self.out_dir / (
                f"tts-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.{suffix}")
            out.write_bytes(data)
        except Exception:
            return None
        rv = TTSResult(
            ok=True, text=text, provider=provider, voice=voice,
            format=fmt, audio_path=str(out))
        rv.extra["bytes"] = len(data)
        rv.extra["cache_hit"] = True
        try:
            dur, src = compute_audio_duration_sec(str(out), fmt)
            rv.duration_sec = float(dur)
            rv.duration_source = str(src)
        except Exception:
            rv.duration_sec = -1.0
            rv.duration_source = "unknown"
        return rv

    async def _synthesize_uncached(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        timeout_sec: float = 30.0,
        spec: Any = None,
    ) -> TTSResult:
        rv = TTSResult(
            text=str(text or ""),
            provider=self._effective_backend(),
            voice=voice or self._effective_voice(),
            format=self.format,
        )
        if not self.enabled:
            rv.error = "pipeline_disabled"
            return rv
        if not rv.text.strip():
            rv.error = "empty_text"
            return rv
        self.out_dir.mkdir(parents=True, exist_ok=True)
        suffix = "wav" if self.backend == "pyttsx3" else self.format
        out = self.out_dir / f"tts-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.{suffix}"
        t0 = time.monotonic()
        # ── 局域网克隆优先：在线则走 LAN 零样本克隆；不可用/失败按配置回落云端 ──
        if self._should_try_lan():
            lan_rv = await self._try_lan_clone(rv, out, t0)
            if lan_rv is not None:
                return lan_rv  # LAN 成功 或 硬失败(未开兜底)；None = 回落云端

        # ── 主后端合成 ──
        primary_backend = self._effective_backend()
        err = await self._run_backend(
            rv, rv.text, out, rv.voice, primary_backend, rv.format, timeout_sec,
            spec=spec)
        if err is None:
            rv.latency_ms = int((time.monotonic() - t0) * 1000)
            return rv

        # ── 主后端失败 → 回落到免基建的在线 edge_tts（避免硬失败 / WinError 10060）──
        # 仅对「传输/运行时」失败兜底；配置/授权类错误（缺同意、缺参考音频等）应直接
        # 暴露给用户，不能用通用音色悄悄掩盖。
        fb = self.fallback_backend
        if (self.fallback_on_error and fb and fb != primary_backend
                and not _is_non_fallback_error(err)):
            # 冷却期内的「缓存命中不可达」是已知稳态 → DEBUG，避免主机长时间离线时
            # 每次语音合成都刷 WARNING；首次探测失败（刚写入缓存）仍按 WARNING 记。
            _cached_dead = "tts_host_unreachable_cached:" in (err or "")
            (logger.debug if _cached_dead else logger.warning)(
                "[tts] backend '%s' failed (%s) → 回落 '%s'", primary_backend, err, fb)
            fb_fmt = "mp3" if fb == "edge_tts" else self.format
            fb_out = out.with_suffix(f".{fb_fmt}")
            fb_err = await self._run_backend(
                rv, rv.text, fb_out, self.fallback_voice, fb, fb_fmt, timeout_sec,
                spec=spec)
            if fb_err is None:
                rv.provider = fb
                rv.format = fb_fmt
                rv.voice = self.fallback_voice
                rv.extra["fallback_from"] = primary_backend
                rv.extra["primary_error"] = err
                rv.latency_ms = int((time.monotonic() - t0) * 1000)
                return rv
            err = f"{err} | fallback({fb}):{fb_err}"

        rv.error = err
        rv.latency_ms = int((time.monotonic() - t0) * 1000)
        return rv

    async def _run_backend(
        self,
        rv: "TTSResult",
        text: str,
        out: Path,
        voice: str,
        backend: str,
        fmt: str,
        timeout_sec: float,
        *,
        spec: Any = None,
    ) -> Optional[str]:
        """用指定 backend 合成到 out。成功 → 写回 rv 并返回 None；失败 → 返回错误串。"""
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._synthesize_sync, text, out, voice, backend, spec),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            return f"tts_timeout({timeout_sec:.0f}s)"
        except Exception as ex:
            return f"{type(ex).__name__}: {ex}"
        if not (out.exists() and out.stat().st_size > 0):
            return "empty_audio"
        rv.ok = True
        rv.audio_path = str(out)
        rv.extra["bytes"] = out.stat().st_size
        # ── P3-A：合成后立即测时长，带上 duration_source 供上游审计 ──
        try:
            dur, src = compute_audio_duration_sec(str(out), fmt)
            rv.duration_sec = float(dur)
            rv.duration_source = str(src)
        except Exception:
            rv.duration_sec = -1.0
            rv.duration_source = "unknown"
        return None

    def _effective_backend(self) -> str:
        if bool(self.voice_profile.get("enabled", False)):
            return str(self.voice_profile.get("backend") or self.backend).strip().lower()
        return self.backend

    def _effective_voice(self) -> str:
        if bool(self.voice_profile.get("enabled", False)):
            return str(self.voice_profile.get("speaker_id") or self.voice).strip()
        return self.voice

    def _should_try_lan(self) -> bool:
        """是否应尝试局域网克隆：LAN 启用 + 已请求克隆(有同意+参考音频文件)。"""
        lan = self.voice_clone_lan or {}
        if not lan.get("enabled"):
            return False
        vp = self.voice_profile or {}
        if not (vp.get("enabled") and vp.get("owner_consent")):
            return False
        ref = str(vp.get("reference_audio_path") or "").strip()
        return bool(ref) and Path(ref).is_file()

    async def _try_lan_clone(
        self, rv: "TTSResult", out: Path, t0: float,
    ) -> Optional["TTSResult"]:
        """尝试局域网零样本克隆。

        返回值语义：
          - TTSResult：LAN 成功，或硬失败且未开云端兜底（直接定稿）
          - None：局域网不可用/失败且允许兜底 → 调用方回落云端
        """
        from src.ai.voice_clone_client import VoiceCloneClient

        lan = VoiceCloneClient(self.voice_clone_lan)
        ref = str(self.voice_profile.get("reference_audio_path") or "").strip()
        ref_text = str(self.voice_profile.get("reference_text") or "").strip()
        # fish_speech 返回 WAV：用 .wav 产物并据此标记格式/测时长
        lan_out = out.with_suffix(".wav")

        def _finalize_err(msg: str) -> "TTSResult":
            rv.error = msg
            rv.latency_ms = int((time.monotonic() - t0) * 1000)
            return rv

        # 健康探测（短超时 + 进程缓存）
        if not await asyncio.to_thread(lan.health_ok):
            if lan.cloud_fallback:
                logger.info("[tts] voice_clone_lan unreachable → 回落云端")
                return None
            return _finalize_err("voice_clone_lan_unreachable")

        def _do_clone() -> None:
            lan.synthesize_clone(rv.text, ref, lan_out, reference_text=ref_text)

        try:
            await asyncio.wait_for(
                asyncio.to_thread(_do_clone),
                timeout=lan.synth_timeout_sec,
            )
        except Exception as ex:
            try:
                lan_out.unlink(missing_ok=True)  # type: ignore[call-arg]
            except Exception:
                pass
            if lan.cloud_fallback:
                logger.warning("[tts] voice_clone_lan failed (%s) → 回落云端", ex)
                return None
            return _finalize_err(f"voice_clone_lan_failed:{str(ex)[:200]}")

        if lan_out.exists() and lan_out.stat().st_size > 0:
            rv.ok = True
            rv.provider = "voice_clone_lan"
            rv.format = "wav"
            rv.audio_path = str(lan_out)
            rv.extra["bytes"] = lan_out.stat().st_size
            rv.extra["lan_base_url"] = lan.base_url
            try:
                dur, src = compute_audio_duration_sec(str(lan_out), "wav")
                rv.duration_sec = float(dur)
                rv.duration_source = str(src)
            except Exception:
                rv.duration_sec = -1.0
                rv.duration_source = "unknown"
            rv.latency_ms = int((time.monotonic() - t0) * 1000)
            return rv

        # 产物为空
        if lan.cloud_fallback:
            return None
        return _finalize_err("voice_clone_lan_empty")

    def _validate_voice_profile(self) -> None:
        if not bool(self.voice_profile.get("enabled", False)):
            return
        if not bool(self.voice_profile.get("owner_consent", False)):
            raise RuntimeError("voice_profile_requires_owner_consent")
        ref = str(self.voice_profile.get("reference_audio_path") or "").strip()
        if not ref:
            raise RuntimeError("voice_profile_missing_reference_audio_path")
        if not Path(ref).is_file():
            raise RuntimeError(f"voice_profile_reference_audio_missing:{ref}")

    def _synthesize_sync(
        self, text: str, out: Path, voice: str, backend: Optional[str] = None,
        spec: Any = None,
    ) -> None:
        backend = (backend or self._effective_backend())
        if backend == "edge_tts":
            asyncio.run(self._edge_tts(text, out, voice, spec))
            return
        if backend == "pyttsx3":
            import pyttsx3  # type: ignore

            engine = pyttsx3.init()
            if voice:
                for v in engine.getProperty("voices") or []:
                    if voice.lower() in (str(getattr(v, "id", "")) + str(getattr(v, "name", ""))).lower():
                        engine.setProperty("voice", getattr(v, "id", ""))
                        break
            engine.save_to_file(text, str(out))
            engine.runAndWait()
            return
        if backend == "openai":
            from openai import OpenAI  # type: ignore

            if not self.api_key:
                raise RuntimeError("missing api_key for openai TTS")
            kwargs: Dict[str, Any] = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            client = OpenAI(**kwargs)
            req: Dict[str, Any] = {
                "model": self.model,
                "voice": voice or self.voice or "alloy",
                "input": text,
                "response_format": self.format,
            }
            # P1：情感 → instructions（在运营已配置的 instructions 之后追加，不覆盖）
            instr = self.instructions
            if spec is not None and not getattr(spec, "is_neutral", lambda: True)():
                try:
                    from src.ai.voice_emotion import to_openai_instructions
                    instr = to_openai_instructions(spec, base=self.instructions)
                except Exception:
                    instr = self.instructions
            if instr:
                req["instructions"] = instr
            resp = client.audio.speech.create(**req)
            if hasattr(resp, "write_to_file"):
                resp.write_to_file(str(out))
            else:
                data = getattr(resp, "content", b"")
                out.write_bytes(data)
            return
        if backend == "voice_clone_command":
            self._synthesize_voice_clone_command(text, out)
            return
        if backend == "coqui_http":
            self._synthesize_coqui_http(text, out)
            return
        if backend == "elevenlabs":
            self._synthesize_elevenlabs(text, out, voice, spec)
            return
        if backend == "disabled":
            raise RuntimeError("backend disabled")
        raise RuntimeError(f"unknown backend {backend}")

    def _synthesize_voice_clone_command(self, text: str, out: Path) -> None:
        self._validate_voice_profile()
        tpl = str(self.voice_profile.get("command_template") or "").strip()
        raw_args = self.voice_profile.get("command_args")
        if not tpl and not isinstance(raw_args, list):
            raise RuntimeError("voice_profile_missing_command")
        ref = str(self.voice_profile.get("reference_audio_path") or "").strip()
        speaker = str(self.voice_profile.get("speaker_id") or "my_voice").strip()
        raw_values = {
            "text": text,
            "out": str(out),
            "reference_audio": ref,
            "speaker": speaker,
            "model": str(self.voice_profile.get("model") or self.model),
        }
        timeout = float(self.voice_profile.get("command_timeout_sec", 120) or 120)
        if isinstance(raw_args, list):
            cmd_args = [str(x).format(**raw_values) for x in raw_args]
            env = os.environ.copy()
            if self.dashscope_api_key:
                env["DASHSCOPE_API_KEY"] = self.dashscope_api_key
            if self.dashscope_region:
                env["DASHSCOPE_REGION"] = self.dashscope_region
            r = subprocess.run(
                cmd_args,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        else:
            quoted_values = {k: shlex.quote(v) for k, v in raw_values.items()}
            cmd = tpl.format(**quoted_values)
            env = os.environ.copy()
            if self.dashscope_api_key:
                env["DASHSCOPE_API_KEY"] = self.dashscope_api_key
            if self.dashscope_region:
                env["DASHSCOPE_REGION"] = self.dashscope_region
            r = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "")[:500]
            raise RuntimeError(f"voice_clone_command_failed:{msg}")

    def _synthesize_elevenlabs(
        self, text: str, out: Path, voice: str, spec: Any = None,
    ) -> None:
        """ElevenLabs v3 合成（情感 + 克隆音色）。voice_id 取 voice_profile.voice 优先。"""
        from src.ai.elevenlabs_client import ElevenLabsClient, output_format_for

        el = self.elevenlabs or {}
        # model：优先 elevenlabs.model_id；其次顶层 model（若以 eleven 开头）；否则默认 v3
        model_id = str(el.get("model_id") or "").strip()
        if not model_id and str(self.model or "").lower().startswith("eleven"):
            model_id = self.model
        client = ElevenLabsClient({
            "api_key": el.get("api_key") or self.api_key,
            "base_url": el.get("base_url") or "",
            "model_id": model_id or "eleven_v3",
            "timeout_sec": el.get("timeout_sec") or 120,
            "similarity_boost": el.get("similarity_boost") or 0.75,
        })
        # voice_id：人设 voice_profile.voice（云端音色 ID）优先，回落 voice/全局
        vp = self.voice_profile or {}
        voice_id = str(vp.get("voice") or voice or self.voice or "").strip()
        out_fmt = output_format_for(self.format)
        client.synthesize(text, voice_id, out, emotion=spec, output_format=out_fmt)

    def _synthesize_coqui_http(self, text: str, out: Path) -> None:
        """Call Coqui XTTS-v2 server (custom OpenAI-compatible API).

        voice_profile.enabled + reference_audio_path
            → POST /v1/audio/clone  (JSON: text + reference_audio_base64)
        otherwise
            → POST /v1/audio/speech (JSON: model + input + voice + language)
        """
        import base64 as _b64
        import json as _json
        import urllib.request as _ur

        base = (self.base_url or "http://127.0.0.1:7851").rstrip("/")
        # 快速可达性预检：主机离线时 ~3s 失败并触发兜底，避免 OS TCP 连接超时
        # （WinError 10060）拖到 ~21s。预检通过才走正常合成（合成本身仍给足超时）。
        _assert_http_reachable(base, timeout=3.0)
        vp = self.voice_profile
        fmt = (self.format or "wav").lower()
        language = str(vp.get("language") or "zh-cn")
        auth_header = f"Bearer {self.api_key or 'coqui'}"

        use_clone = (
            bool(vp.get("enabled"))
            and bool(vp.get("owner_consent"))
            and bool(vp.get("reference_audio_path"))
            and Path(str(vp.get("reference_audio_path", ""))).is_file()
        )

        if use_clone:
            ref_path = str(vp["reference_audio_path"])
            ref_b64 = _b64.b64encode(Path(ref_path).read_bytes()).decode("ascii")
            payload = _json.dumps({
                "text": text,
                "language": language,
                "reference_audio_base64": ref_b64,
            }).encode()
            url = f"{base}/v1/audio/clone"
        else:
            voice_id = str(vp.get("speaker_id") or self.voice or "female_01")
            payload = _json.dumps({
                "model": self.model or "xtts_v2",
                "input": text,
                "voice": voice_id,
                "language": language,
                "response_format": fmt,
            }).encode()
            url = f"{base}/v1/audio/speech"

        req = _ur.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": auth_header,
            },
        )
        with _ur.urlopen(req, timeout=300) as resp:
            response_bytes = resp.read()
        if not response_bytes:
            raise RuntimeError("coqui_http: empty response from TTS server")
        # /v1/audio/clone returns JSON: {"audio_base64": "..."}
        # /v1/audio/speech returns raw audio bytes
        if use_clone:
            try:
                resp_json = _json.loads(response_bytes.decode("utf-8"))
                audio_b64 = resp_json.get("audio_base64") or resp_json.get("audio")
                if not audio_b64:
                    raise RuntimeError(f"coqui_http: no audio_base64 in response: {list(resp_json.keys())}")
                audio_bytes = _b64.b64decode(audio_b64)
            except (_json.JSONDecodeError, KeyError, ValueError) as e:
                raise RuntimeError(f"coqui_http: failed to parse clone response: {e}")
        else:
            audio_bytes = response_bytes
        out.write_bytes(audio_bytes)

    async def _edge_tts(
        self, text: str, out: Path, voice: str, spec: Any = None,
    ) -> None:
        import edge_tts  # type: ignore

        kwargs: Dict[str, Any] = {}
        # P1：情感 → edge_tts rate/pitch 近似情绪（neutral 不调，行为不变）
        if spec is not None and not getattr(spec, "is_neutral", lambda: True)():
            try:
                from src.ai.voice_emotion import edge_prosody
                kwargs.update(edge_prosody(spec))
            except Exception:
                kwargs = {}
        communicate = edge_tts.Communicate(text, voice or self.voice, **kwargs)
        await communicate.save(str(out))


# ── P3-A: 音频时长测量 ──────────────────────────────────────────
# 目的：synthesize 成功后拿到 audio_path，立刻测 duration，交给上游做范围校验。
# 之前的 745:39 WAV bug 源于 DataSize=INT_MAX（头被填 0x7FFFFFFF）。
# 这里哪怕是 header 坏的文件，`wave.open()` 也会抛异常或返回极大值，
# 调用方应对照 voice_output.max_seconds 做硬上限，超了就回退文字。

def _duration_from_wave(path: str) -> float:
    """stdlib wave 模块解析 WAV：nframes / framerate。

    若 header 声明的 DataSize 异常（如 2147483647 = INT_MAX 的 bug），
    nframes 会被报为天文数字 —— 调用方据此可直接判无效。
    """
    import wave
    with wave.open(path, "rb") as w:
        frames = int(w.getnframes())
        rate = int(w.getframerate() or 0)
        if rate <= 0:
            return -1.0
        return float(frames) / float(rate)


def _duration_from_mp3(path: str) -> float:
    """轻量级 MP3 时长估算：扫描前几帧拿 bitrate，再 filesize / bitrate。

    对 CBR MP3 精度 ±1s；VBR 不够准但够做上限校验。
    无第三方依赖 —— mutagen / pydub 都不装。
    """
    try:
        import os
        size = os.path.getsize(path)
        if size <= 128:
            return -1.0
        # MPEG-1/2 Layer 3 bitrate table（kbps）
        bitrate_tab_v1_l3 = [
            None, 32, 40, 48, 56, 64, 80, 96,
            112, 128, 160, 192, 224, 256, 320, None,
        ]
        bitrate_tab_v2_l3 = [
            None, 8, 16, 24, 32, 40, 48, 56,
            64, 80, 96, 112, 128, 144, 160, None,
        ]
        samplerate_tab = {
            (3, 0): 44100, (3, 1): 48000, (3, 2): 32000,
            (2, 0): 22050, (2, 1): 24000, (2, 2): 16000,
            (0, 0): 11025, (0, 1): 12000, (0, 2): 8000,
        }
        with open(path, "rb") as f:
            head = f.read(10)
            # 跳过 ID3v2
            offset = 0
            if head[:3] == b"ID3":
                tag_size = (
                    (head[6] & 0x7F) << 21
                    | (head[7] & 0x7F) << 14
                    | (head[8] & 0x7F) << 7
                    | (head[9] & 0x7F)
                )
                offset = 10 + tag_size
                f.seek(offset)
            # 扫描同步字
            buf = f.read(4096)
            for i in range(len(buf) - 4):
                if buf[i] == 0xFF and (buf[i + 1] & 0xE0) == 0xE0:
                    b1 = buf[i + 1]
                    b2 = buf[i + 2]
                    version = (b1 >> 3) & 0x03  # 0=v2.5, 2=v2, 3=v1
                    layer = (b1 >> 1) & 0x03    # 1=layer3
                    if layer != 1:
                        continue
                    br_idx = (b2 >> 4) & 0x0F
                    sr_idx = (b2 >> 2) & 0x03
                    if br_idx in (0, 15) or sr_idx == 3:
                        continue
                    tab = bitrate_tab_v1_l3 if version == 3 else bitrate_tab_v2_l3
                    bitrate_kbps = tab[br_idx]
                    samplerate = samplerate_tab.get((version, sr_idx))
                    if not bitrate_kbps or not samplerate:
                        continue
                    # 音频 payload 长度（毛估：减去可能的 ID3v1 128 字节）
                    audio_bytes = size - offset
                    tail_chk = max(0, audio_bytes - 128)
                    if tail_chk > 0:
                        audio_bytes = tail_chk
                    return (audio_bytes * 8.0) / (bitrate_kbps * 1000.0)
        return -1.0
    except Exception:
        return -1.0


def compute_audio_duration_sec(
    path: str, fmt: str = "",
) -> tuple[float, str]:
    """测量音频文件时长，返回 (seconds, source_tag)。

    source_tag ∈ {"wave_header", "mp3_frame", "unknown"}；
    -1.0 表示未能测量，调用方应把此视为"不可信"。
    """
    if not path or not os.path.isfile(path):
        return -1.0, "unknown"
    fmt_low = (fmt or Path(path).suffix.lstrip(".")).lower()
    # WAV 先试 stdlib
    if fmt_low in ("wav", "wave") or path.lower().endswith(".wav"):
        try:
            d = _duration_from_wave(path)
            if d > 0:
                return d, "wave_header"
        except Exception:
            pass
    # MP3
    if fmt_low == "mp3" or path.lower().endswith(".mp3"):
        d = _duration_from_mp3(path)
        if d > 0:
            return d, "mp3_frame"
    # 其他格式（opus/m4a/aac）暂无轻量解析器
    return -1.0, "unknown"


_tts_singleton: Optional[TTSPipeline] = None


def get_tts_pipeline(cfg: Optional[Dict[str, Any]] = None) -> TTSPipeline:
    global _tts_singleton
    if _tts_singleton is None:
        _tts_singleton = TTSPipeline(cfg or {})
    return _tts_singleton


def reset_tts_pipeline() -> None:
    global _tts_singleton
    _tts_singleton = None
