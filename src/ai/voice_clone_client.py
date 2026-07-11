"""局域网语音克隆客户端 — 调"语音主机"上的零样本克隆 HTTP 服务。

设计与 ``src/ai/faceswap_client.py`` 同源：薄 HTTP 封装 + ``health_ok()`` 健康探测，
供 ``TTSPipeline`` 做「**局域网优先 → 云端兜底**」调度。

实际后端为 **fish_speech_s2**（AvatarHub Voice Clone，默认 192.168.0.188:7855）：
    GET  {base_url}/health
        Reply: {"status":"ok","engine":"fish_speech_s2","model_loaded":true}
    POST {base_url}/v1/tts/clone     # 零样本声音克隆
        Body:  {"text", "reference_audio_b64", "reference_text", "language":"zh",
                "return_base64": true}
        Reply: {"ok":true, "audio_base64":"<WAV b64>", "sample_rate":44100, "n_refs":1}

输出为 **WAV**（44.1kHz）。健康探测结果按 base_url 做**进程级短缓存**（默认 30s），
避免每条消息都探一次。

可单测的纯函数（无网络/IO）：
  - ``build_clone_payload`` — 克隆合成请求体
  - ``parse_clone_response`` — 解析响应为音频字节
  - ``effective_clone_language`` — 按待合成文本推导合成语言（防「中文声纹念英文」）
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import threading
import time
import urllib.request
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 进程级健康缓存：base_url -> (expires_monotonic, ok)
_HEALTH_CACHE: Dict[str, Tuple[float, bool]] = {}
# 进程级「已触发按需载入」时间戳：base_url -> last_trigger_monotonic（冷却防重复触发）
_LOAD_TRIGGER: Dict[str, float] = {}


def build_clone_payload(
    *, text: str, reference_audio_b64: str, reference_text: str = "",
    language: str = "zh", instructions: str = "",
) -> bytes:
    """零样本克隆请求体（JSON bytes）。fish_speech 与 MiniCPM-o 等主机共用本契约。

    - reference_text（参考音频里说的原文）填了效果更好；空则省略。
    - instructions（情感/语气自然语言指令，如「用温暖略带笑意的语气说」）是**结构化字段**，
      主机据此调情绪但**绝不会被读出来**（与内联标记不同，零 garble）；不支持的主机忽略即可。
    """
    body: Dict[str, Any] = {
        "text": text,
        "reference_audio_b64": reference_audio_b64,
        "language": language,
        "return_base64": True,
    }
    if reference_text:
        body["reference_text"] = reference_text
    if instructions:
        body["instructions"] = instructions
    return json.dumps(body).encode()


def effective_clone_language(text: str, default: str = "zh") -> str:
    """按**待合成文本的实际语种**推导克隆合成语言，防「中文声纹念英文」。

    根因：``VoiceCloneClient.language`` 固定取 config（默认 ``zh``），无论回复正文
    实际是哪种语言都把 ``language:"zh"`` 发给克隆主机。英文/越南文等回复被按中文
    音系发音 → garble。本函数用确定性 ``detect_language`` 按文本内容纠正合成语言。

    规则（保守，绝不比现状更糟）：
      - 文本为空 / 检测不可用 / 检测为 ``unknown`` → 回落 ``default``（=旧行为）。
      - 检测出明确语种（zh/en/vi/es/pt/...）→ 用之（中文回复仍判 zh，行为不变；
        英文回复由 zh 纠正为 en，严格改善）。
    纯函数、best-effort（``detect_language`` 惰性导入，任何异常回落 default，绝不抛）。
    """
    d = (str(default or "").strip() or "zh")
    t = str(text or "").strip()
    if not t:
        return d
    try:
        from src.ai.translation_service import detect_language
        lang = (detect_language(t) or "").strip().lower()
    except Exception:
        return d
    if not lang or lang == "unknown":
        return d
    return lang


# ── 长文本分块合成（防自回归克隆 TTS 长句整段生成时截断/漂移）─────────────────
# 根因：IndexTTS2 等自回归克隆 TTS 把整段文本内部切「段」后逐段 GPT 生成；单段过长
# （默认 max_text_tokens_per_segment=120）时其音频可能超 max_mel_tokens(1500) → 该段
# **中途截断**（"刷手机"念到"刷手"就断）；情感条件(emo_text)还会加剧漂移/串字。客户端
# 把长回复按句切成 ≤N 字的短块、逐块合成再拼接，保证每块都短到能稳定完整生成——
# 与后端无关的兜底保证（换任何克隆主机都有效）。短回复（≤N 字）单块直发，零行为变化。
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?…\n；;])")
_CHUNK_SECONDARY_SEPS = "，,、：: "


def _split_sentences_keep(text: str) -> List[str]:
    """按终止/强停顿标点切句，保留标点（换行也当边界）。空片段剔除。"""
    parts = _SENT_SPLIT_RE.split(str(text or ""))
    return [p for p in (x.strip() for x in parts) if p]


def _hard_split_long(seg: str, max_chars: int) -> List[str]:
    """单句仍超长 → 在逗号/顿号/空格等次级边界硬切，无边界则按 max_chars 硬切。"""
    out: List[str] = []
    rest = seg.strip()
    while len(rest) > max_chars:
        window = rest[:max_chars]
        cut = -1
        for sep in _CHUNK_SECONDARY_SEPS:
            idx = window.rfind(sep)
            if idx > cut:
                cut = idx
        if cut <= 0:
            cut = max_chars - 1  # 无任何边界 → 硬切（含标点位）
        out.append(rest[:cut + 1].strip())
        rest = rest[cut + 1:].strip()
    if rest:
        out.append(rest)
    return [c for c in out if c]


def split_text_for_clone(text: str, max_chars: int = 60) -> List[str]:
    """把长文本切成每块 ≤``max_chars`` 的合成块（供逐块克隆合成后拼接）。

    - ``text`` 空 → ``[]``；``max_chars<=0`` 或文本 ≤max_chars → 单块 ``[text]``
      （短回复零影响、行为与不切分一致）。
    - 否则按句切分后**贪心打包**相邻短句到 ≤max_chars；单句超长则二次硬切。
    纯函数、防御式。
    """
    t = str(text or "").strip()
    if not t:
        return []
    if max_chars <= 0 or len(t) <= max_chars:
        return [t]
    pieces: List[str] = []
    for s in _split_sentences_keep(t):
        if len(s) <= max_chars:
            pieces.append(s)
        else:
            pieces.extend(_hard_split_long(s, max_chars))
    chunks: List[str] = []
    cur = ""
    for p in pieces:
        if not cur:
            cur = p
        elif len(cur) + len(p) <= max_chars:
            cur += p
        else:
            chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    return [c.strip() for c in chunks if c.strip()]


def concat_wav_bytes(parts: List[bytes], gap_ms: int = 120) -> bytes:
    """把多段 WAV 字节按序拼接为一段 WAV（块间插 ``gap_ms`` 静音，更自然）。

    各段 (声道/位深/采样率) 必须一致（同一克隆主机同次合成天然一致）；不一致则抛，
    调用方据此回退整段合成（绝不返回错拼的坏音频）。仅用标准库 wave/io，纯函数。
    """
    bufs = [p for p in parts if p]
    if not bufs:
        raise RuntimeError("concat_wav_bytes: no parts")
    if len(bufs) == 1:
        return bufs[0]
    params: Optional[Tuple[int, int, int]] = None
    frames_list: List[bytes] = []
    for b in bufs:
        with wave.open(io.BytesIO(b), "rb") as w:
            cur = (w.getnchannels(), w.getsampwidth(), w.getframerate())
            frames = w.readframes(w.getnframes())
        if params is None:
            params = cur
        elif cur != params:
            raise RuntimeError(f"concat_wav_bytes: param mismatch {cur} != {params}")
        frames_list.append(frames)
    assert params is not None
    nch, sw, rate = params
    gap_frames = max(0, int(rate * (max(0, gap_ms) / 1000.0)))
    silence = b"\x00" * (gap_frames * nch * sw)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(sw)
        w.setframerate(rate)
        for i, frames in enumerate(frames_list):
            if i > 0 and silence:
                w.writeframes(silence)
            w.writeframes(frames)
    return out.getvalue()


def parse_clone_response(body: bytes) -> bytes:
    """解析克隆合成响应为音频字节（WAV）。

    支持：
      - JSON {"ok":true, "audio_base64":"<b64>"}（fish_speech）/ {"audio":"<b64>"}
      - 裸音频字节（非 JSON）
    服务返回 ``ok:false`` 时抛出其错误信息。
    """
    if not body:
        raise RuntimeError("voice_clone_lan: empty response")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body  # 裸音频字节
    if isinstance(data, dict):
        if data.get("ok") is False:
            msg = data.get("error") or data.get("message") or "clone failed"
            raise RuntimeError(f"voice_clone_lan: {str(msg)[:200]}")
        b64 = data.get("audio_base64") or data.get("audio")
        if not b64:
            raise RuntimeError(
                f"voice_clone_lan: no audio in response keys={list(data.keys())}")
        return base64.b64decode(b64)
    raise RuntimeError("voice_clone_lan: unexpected response shape")


class VoiceCloneClient:
    """局域网零样本语音克隆 HTTP 客户端。"""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        cfg = cfg or {}
        self.enabled: bool = bool(cfg.get("enabled", False))
        self.base_url: str = str(
            cfg.get("base_url") or "http://192.168.0.188:7855").rstrip("/")
        self.protocol: str = str(cfg.get("protocol") or "fish_speech").strip().lower()
        self.clone_path: str = str(cfg.get("clone_path") or "/v1/tts/clone")
        self.health_path: str = str(cfg.get("health_path") or "/health")
        self.health_timeout_sec: float = float(cfg.get("health_timeout_sec") or 1.5)
        self.health_cache_sec: float = float(cfg.get("health_cache_sec") or 30)
        self.synth_timeout_sec: float = float(cfg.get("synth_timeout_sec") or 300)
        self.language: str = str(cfg.get("language") or "zh")
        # 按待合成文本推导合成语言（防「中文声纹念英文」）。默认开——这是纠正
        # 「无论文本语种都发 language:zh」的既有缺陷，非新子系统；中文回复行为不变，
        # 仅英文/他语回复被纠正。个别主机若因此异常可置 false 退回旧行为（opt-out）。
        self.auto_language: bool = bool(cfg.get("auto_language", True))
        self.api_key: str = str(cfg.get("api_key") or "")
        self.cloud_fallback: bool = bool(cfg.get("cloud_fallback", True))
        # 惰性载入主机（如 MiniCPM-o supervisor：常驻 0 显存，按需拉起 worker）「模型未载入」时，
        # 是否后台自动触发一次载入（POST load_path），使主机重启后无需人工干预即自愈：本条仍回落
        # edge，约 20–30s 载入完成后自动恢复克隆声。默认开；置 false 退回旧行为（仅回落不触发）。
        self.load_path: str = str(cfg.get("load_path") or "/v1/model/load")
        self.auto_load: bool = bool(cfg.get("auto_load", True))
        self.load_cooldown_sec: float = float(cfg.get("load_cooldown_sec") or 60)
        # 长文本分块合成（防长句整段生成截断/漂移）：文本超 chunk_max_chars 即按句切成
        # 短块逐块合成后拼接。默认 60 字（约 ≤12s 音频/段，稳在 IndexTTS2 max_mel 上限内）；
        # 置 0 关闭（退回整段单次合成，旧行为）。块间插 chunk_gap_ms 静音更自然。
        self.chunk_max_chars: int = int(cfg.get("chunk_max_chars", 60) or 0)
        self.chunk_gap_ms: int = int(cfg.get("chunk_gap_ms", 120) or 0)

    @classmethod
    def from_config(cls, full_config: Dict[str, Any]) -> "VoiceCloneClient":
        return cls((full_config or {}).get("voice_clone_lan") or {})

    # ── 健康探测（带进程级短缓存）─────────────────────────────────────────────
    def health_ok(self, *, use_cache: bool = True) -> bool:
        now = time.monotonic()
        if use_cache:
            hit = _HEALTH_CACHE.get(self.base_url)
            if hit and hit[0] > now:
                return hit[1]
        ok = self._probe_health()
        _HEALTH_CACHE[self.base_url] = (now + self.health_cache_sec, ok)
        return ok

    def _probe_health(self) -> bool:
        d = self.probe_health_detail()
        if not d["reachable"]:
            return False
        ml = d["model_loaded"]
        # 无 model_loaded 字段（老 fish 常驻主机）→ 视为可用；有则以其为准（未载入=不可用）
        return True if ml is None else bool(ml)

    def probe_health_detail(self) -> Dict[str, Any]:
        """探测健康并返回明细 ``{reachable, model_loaded, loading}``（best-effort，绝不抛）。

        - ``reachable``：HTTP 2xx（主机/看守进程活着）——用于区分「彻底不可达」vs「可达但模型未载入」
        - ``model_loaded``：健康体 ``model_loaded``；无此字段 → ``None``（老主机常驻，视为已就绪）
        - ``loading``：健康体 ``loading``（正在载入中，不必再触发）
        """
        url = f"{self.base_url}{self.health_path}"
        detail: Dict[str, Any] = {
            "reachable": False, "model_loaded": None, "loading": False}
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=self.health_timeout_sec) as r:
                if not (200 <= int(getattr(r, "status", 200)) < 300):
                    return detail
                body = r.read()
            detail["reachable"] = True
            try:
                data = json.loads(body.decode("utf-8"))
                if isinstance(data, dict):
                    if "model_loaded" in data:
                        detail["model_loaded"] = bool(data["model_loaded"])
                    detail["loading"] = bool(data.get("loading", False))
            except Exception:
                pass
            return detail
        except Exception as exc:
            logger.debug("[voice_clone_lan] health probe failed %s: %s", url, exc)
            return detail

    def mark_health_ok(self) -> None:
        """把本 base_url 的健康缓存刷成 True（载入完成后调用，下一条消息立即走克隆）。"""
        _HEALTH_CACHE[self.base_url] = (
            time.monotonic() + self.health_cache_sec, True)

    # ── 按需载入（惰性主机自愈）──────────────────────────────────────────────
    def _do_model_load(self) -> bool:
        """阻塞式触发主机载入模型（POST load_path），成功则刷新健康缓存。best-effort。

        单独抽出（不含线程/冷却）便于同步单测。返回是否载入就绪。
        """
        url = f"{self.base_url}{self.load_path}"
        try:
            headers: Dict[str, str] = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            req = urllib.request.Request(
                url, method="POST", data=b"{}", headers=headers)
            with urllib.request.urlopen(req, timeout=self.synth_timeout_sec) as r:
                body = r.read()
            ok = True
            try:
                data = json.loads(body.decode("utf-8"))
                if isinstance(data, dict) and "model_loaded" in data:
                    ok = bool(data["model_loaded"])
            except Exception:
                ok = True
            if ok:
                self.mark_health_ok()
                logger.info("[voice_clone] 后台载入完成，克隆声已就绪：%s", url)
            else:
                logger.warning("[voice_clone] 后台载入返回未就绪：%s", url)
            return ok
        except Exception as exc:
            logger.warning("[voice_clone] 后台载入失败 %s: %s", url, exc)
            return False

    def request_model_load_async(self) -> bool:
        """惰性主机「可达但模型未载入」时后台触发一次载入（fire-and-forget，不阻塞调用方）。

        - 进程级冷却（``load_cooldown_sec``，默认 60s）防重复触发（载入约需 20–30s，其间来的
          消息不再重复发起）；
        - daemon 线程执行 :meth:`_do_model_load`，本条消息照常回落 edge，稍后自动恢复克隆声。
        返回 True 表示本次已发起触发；False 表示处于冷却中（已有一次在途）。
        """
        now = time.monotonic()
        last = _LOAD_TRIGGER.get(self.base_url, 0.0)
        if now - last < self.load_cooldown_sec:
            return False
        _LOAD_TRIGGER[self.base_url] = now
        threading.Thread(
            target=self._do_model_load, name="voice-clone-load", daemon=True
        ).start()
        return True

    # ── 零样本克隆合成 ───────────────────────────────────────────────────────
    def synthesize_clone(
        self, text: str, reference_audio_path: str, out: Path,
        *, reference_text: str = "", instructions: str = "",
        language: Optional[str] = None,
    ) -> None:
        """文本 + 参考音频 → 克隆音色合成（WAV），写入 out。失败抛异常。

        ``instructions``：情感/语气自然语言指令（结构化字段，不会被读出），支持的主机
        （如 MiniCPM-o）据此带情绪；不支持的主机忽略。
        ``language``：显式指定合成语言（调用方已知回复语种时传入=最高优先）；为空时，
        若 ``auto_language`` 开则按文本内容推导（防「中文声纹念英文」），否则用配置默认。
        """
        ref = Path(reference_audio_path)
        if not ref.is_file():
            raise RuntimeError(f"reference_audio_missing:{reference_audio_path}")
        ref_b64 = base64.b64encode(ref.read_bytes()).decode("ascii")
        lang = str(language or "").strip()
        if not lang:
            lang = (
                effective_clone_language(text, self.language)
                if self.auto_language else self.language
            )
        if lang != self.language:
            logger.info(
                "[voice_clone] 合成语言按文本纠正: %s → %s", self.language, lang)
        # V：上线观测——记一次合成语言决策（纠正时按目标语种归类）。best-effort，绝不阻塞合成。
        try:
            from src.ai.voice_synth_stats import get_voice_synth_stats
            get_voice_synth_stats().record(default_lang=self.language, used_lang=lang)
        except Exception:
            pass

        # 长文本 → 按句切块逐块合成再拼接（防长句整段生成时截断/漂移）；短文本单块直发。
        chunks = (
            split_text_for_clone(text, self.chunk_max_chars)
            if self.chunk_max_chars > 0 else [str(text or "")]
        )
        if len(chunks) <= 1:
            audio = self._request_clone(
                chunks[0] if chunks else str(text or ""),
                ref_b64, lang, reference_text, instructions)
            if not audio:
                raise RuntimeError("voice_clone_lan: decoded empty audio")
            Path(out).write_bytes(audio)
            return

        parts: List[bytes] = []
        for ch in chunks:
            a = self._request_clone(ch, ref_b64, lang, reference_text, instructions)
            if not a:
                raise RuntimeError("voice_clone_lan: decoded empty audio (chunk)")
            parts.append(a)
        try:
            merged = concat_wav_bytes(parts, gap_ms=self.chunk_gap_ms)
        except Exception as ex:
            # 拼接失败（极少见：分段音频格式不一致）→ 回退整段单次合成，绝不返回坏音频。
            logger.warning(
                "[voice_clone] 分块拼接失败(%s)，回退整段合成 chunks=%d", ex, len(chunks))
            audio = self._request_clone(
                str(text or ""), ref_b64, lang, reference_text, instructions)
            if not audio:
                raise RuntimeError("voice_clone_lan: decoded empty audio")
            Path(out).write_bytes(audio)
            return
        Path(out).write_bytes(merged)
        logger.info("[voice_clone] 分块合成完成：%d 段拼接 (max_chars=%d)",
                    len(parts), self.chunk_max_chars)

    def _request_clone(
        self, text: str, ref_b64: str, lang: str,
        reference_text: str, instructions: str,
    ) -> bytes:
        """单次克隆合成请求 → 音频字节（供整段/分块共用）。失败抛异常。"""
        payload = build_clone_payload(
            text=text, reference_audio_b64=ref_b64,
            reference_text=reference_text, language=lang,
            instructions=instructions)
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            f"{self.base_url}{self.clone_path}", data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=self.synth_timeout_sec) as resp:
            body = resp.read()
        return parse_clone_response(body)


def reset_health_cache() -> None:
    """清空健康缓存（测试用 / 配置变更后强制重探）。"""
    _HEALTH_CACHE.clear()


def reset_load_state() -> None:
    """清空按需载入冷却记录（测试用）。"""
    _LOAD_TRIGGER.clear()
