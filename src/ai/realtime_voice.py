"""实时共情语音（full-duplex empathic voice）—— 纯函数核心。

为什么单独成模块：实时语音通话是一条**新产品面**（浏览器麦克风 ↔ 我们的 WebSocket
网关 ↔ 语音主机上的 speech-language 模型，主力 **MiniCPM-o 4.5** 自托管：中英母语、
全双工、情感 SOTA、短参考音即克隆）。本模块只放**与框架无关、可单测**的部分：

  - ``RealtimeVoiceConfig``     —— 从全局 config 的 ``realtime_voice`` 段解析出强类型配置
  - ``build_call_system_prompt`` —— 把人设 / 记忆 / 语言 / 共情守则拼成**文本系统提示**
                                     （音色由「音频系统提示」=参考音承载，另走 voice_ref）
  - ``build_session_init``       —— 网关→主机的会话初始化负载（system_prompt + 语言 +
                                     参考音 + 采样率 + 模式）
  - 事件帧/解析助手             —— 浏览器/网关/主机之间的 JSON 控制 + 音频事件归一化

设计原则（与 ``voice_emotion`` / ``voice_clone_client`` 同源）：
  - **纯函数、无 IO/网络/框架依赖**，可离线单测。
  - **防御式**：脏输入安全退化，绝不抛异常进实时主链（卡死通话比降级更糟）。
  - **契约自有**：我们定义网关↔主机协议（见事件常量），主机端按本契约实现即可接入，
    与 MiniCPM-o 内部 API 解耦（也便于 mock host 做契约测试）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ── 网关 ↔ 主机 / 浏览器 事件类型（我们自有契约，主机按此实现）────────────────────
# 客户端（浏览器/网关）→ 主机
EV_SESSION_INIT = "session.init"        # 开场：系统提示 + 语言 + 参考音 + 采样率
EV_INPUT_AUDIO = "input_audio"          # 用户音频分片（base64 PCM16）
EV_INPUT_TEXT = "input_text"            # 可选：文本输入（打字转语音回）
EV_INPUT_DONE = "input_done"            # 用户一段说完（半双工/兜底用）
EV_INTERRUPT = "interrupt"              # 用户插话 → 让主机立即停说
EV_SESSION_CLOSE = "session.close"

# 主机 → 客户端
EV_READY = "ready"                      # 主机已就绪（会话已初始化）
EV_TRANSCRIPT_USER = "transcript.user"  # 用户语音的转写（含 emotion 可选）
EV_TRANSCRIPT_ASSISTANT = "transcript.assistant"  # 助手回复文本（伴随音频）
EV_TRANSCRIPT_TRANSLATION = "transcript.translation"  # 网关侧：某条转写的译文（双语字幕，按 tid 关联气泡）
EV_OUTPUT_AUDIO = "output_audio"        # 助手语音分片（base64 PCM16/Opus）
EV_TURN_END = "turn.end"                # 助手本轮说完
EV_ERROR = "error"

_CLIENT_EVENTS = frozenset({
    EV_SESSION_INIT, EV_INPUT_AUDIO, EV_INPUT_TEXT, EV_INPUT_DONE,
    EV_INTERRUPT, EV_SESSION_CLOSE,
})
_HOST_EVENTS = frozenset({
    EV_READY, EV_TRANSCRIPT_USER, EV_TRANSCRIPT_ASSISTANT, EV_OUTPUT_AUDIO,
    EV_TURN_END, EV_ERROR,
})

# MiniCPM-o 4.5 实时语音稳定支持的语种（speech 侧）。其它一律回落 default。
_SUPPORTED_LANGS = ("zh", "en")
_DEFAULT_SAMPLE_RATE = 16000


@dataclass(frozen=True)
class RealtimeVoiceConfig:
    """``config.yaml::realtime_voice`` 的强类型视图。默认 **关**（新子系统约定）。"""
    enabled: bool = False
    base_url: str = "http://127.0.0.1:7860"   # 语音主机（MiniCPM-o server）
    ws_path: str = "/v1/realtime"               # 实时全双工 WebSocket
    health_path: str = "/health"
    oneshot_path: str = "/v1/tts/clone"         # 一次性克隆合成（Track A 复用）
    load_path: str = "/v1/model/load"           # 按需把模型载入显存
    unload_path: str = "/v1/model/unload"       # 释放显存
    model: str = "minicpm-o-4_5"
    default_voice: str = ""                     # 主机内置音色名（无参考音时用）
    default_language: str = "zh"
    sample_rate: int = _DEFAULT_SAMPLE_RATE
    health_timeout_sec: float = 1.5
    health_cache_sec: float = 30.0
    session_idle_timeout_sec: float = 90.0
    max_session_sec: float = 1800.0            # 单次通话上限（仿 EVI 30min）
    api_key: str = ""
    # 共情守则附加到系统提示尾部（可被 config 覆盖）。
    guidance: str = ""
    # 通话接通后人设是否「主动先开口」（克隆真声的开场白）；默认开，可经 opener.enabled 关。
    opener_enabled: bool = True
    # 双语字幕：把助手转写译成运营阅读语言（默认 zh）在通话界面叠显；同语言自动跳过（零成本）。
    subtitle_enabled: bool = True
    subtitle_lang: str = "zh"

    @classmethod
    def from_config(cls, full_config: Optional[Dict[str, Any]]) -> "RealtimeVoiceConfig":
        cfg = {}
        if isinstance(full_config, dict):
            rv = full_config.get("realtime_voice")
            if isinstance(rv, dict):
                cfg = rv
        def _s(key: str, default: str) -> str:
            v = cfg.get(key)
            return str(v).strip() if v not in (None, "") else default
        def _f(key: str, default: float) -> float:
            try:
                return float(cfg.get(key))
            except (TypeError, ValueError):
                return default
        def _i(key: str, default: int) -> int:
            try:
                return int(cfg.get(key))
            except (TypeError, ValueError):
                return default
        lang = _s("default_language", "zh").lower()
        if lang not in _SUPPORTED_LANGS:
            lang = "zh"
        opener = cfg.get("opener")
        if isinstance(opener, dict):
            opener_enabled = bool(opener.get("enabled", True))
        elif "opener_enabled" in cfg:
            opener_enabled = bool(cfg.get("opener_enabled"))
        else:
            opener_enabled = True
        sub = cfg.get("subtitle")
        sub = sub if isinstance(sub, dict) else {}
        subtitle_enabled = bool(sub.get("enabled", True))
        sub_lang = str(sub.get("lang") or "zh").strip().lower()
        if sub_lang not in _SUPPORTED_LANGS:
            sub_lang = "zh"
        return cls(
            enabled=bool(cfg.get("enabled", False)),
            base_url=_s("base_url", "http://127.0.0.1:7860").rstrip("/"),
            ws_path=_s("ws_path", "/v1/realtime"),
            health_path=_s("health_path", "/health"),
            oneshot_path=_s("oneshot_path", "/v1/tts/clone"),
            load_path=_s("load_path", "/v1/model/load"),
            unload_path=_s("unload_path", "/v1/model/unload"),
            model=_s("model", "minicpm-o-4_5"),
            default_voice=_s("default_voice", ""),
            default_language=lang,
            sample_rate=_i("sample_rate", _DEFAULT_SAMPLE_RATE),
            health_timeout_sec=_f("health_timeout_sec", 1.5),
            health_cache_sec=_f("health_cache_sec", 30.0),
            session_idle_timeout_sec=_f("session_idle_timeout_sec", 90.0),
            max_session_sec=_f("max_session_sec", 1800.0),
            api_key=_s("api_key", ""),
            guidance=_s("guidance", ""),
            opener_enabled=opener_enabled,
            subtitle_enabled=subtitle_enabled,
            subtitle_lang=sub_lang,
        )

    def ws_url(self) -> str:
        """http(s)://host → ws(s)://host + ws_path。"""
        base = self.base_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return f"{base}{self.ws_path}"


# ── 语言选择 ─────────────────────────────────────────────────────────────────
def pick_language(text: Optional[str], *, default: str = "zh") -> str:
    """据文本粗判 zh/en（含 CJK→zh，否则 en）。脏输入退化到 default。

    实时语音里语言决定主机的发音/韵律，宁可保守：默认跟会话客户语言（由调用方传入
    default），仅在明显是另一语种时切换。
    """
    d = default if default in _SUPPORTED_LANGS else "zh"
    t = str(text or "").strip()
    if not t:
        return d
    has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in t)
    if has_cjk:
        return "zh"
    # 含拉丁字母且无 CJK → en
    if any(("a" <= ch.lower() <= "z") for ch in t):
        return "en"
    return d


# ── 系统提示（文本侧；音色走音频系统提示=参考音）──────────────────────────────
_DEFAULT_GUIDANCE = (
    "你正在和对方进行**实时语音通话**。像真人那样自然说话：句子短、口语化、有停顿和语气；"
    "顺着对方的情绪走——对方低落就放轻放慢、温柔安抚，对方开心就轻快回应。"
    "一次只说一两句，把话语权交回去，别长篇大论。"
    "全程保持人设，绝不承认自己是 AI / 模型 / 机器人，也不要念出括号、表情符号或标点说明。"
    "如果对方说的是另一种语言，就用对方的语言回应。"
)


def build_call_system_prompt(
    *,
    persona: Optional[Dict[str, Any]] = None,
    memory_bullets: Optional[List[str]] = None,
    language: str = "zh",
    base_prompt: str = "",
    extra_guidance: str = "",
    emotion_tone: str = "",
) -> str:
    """拼实时语音通话的**文本系统提示**。

    - ``base_prompt``：上游已有的人设画像提示（若提供，则作为主体，不重复造轮子）。
    - 否则从 persona dict 现拼一段简洁画像。
    - ``memory_bullets``：长期记忆要点（来自 EpisodicMemoryStore.get_bullets_for_prompt）。
    - ``emotion_tone``：人设语气基调指令（**首句情绪锚**，让通话从开口就对味；空则不加）。
    - 末尾统一附「实时语音共情守则」（可被 extra_guidance 覆盖/追加）。
    """
    parts: List[str] = []
    base = str(base_prompt or "").strip()
    if base:
        parts.append(base)
    elif isinstance(persona, dict) and persona:
        parts.append(_persona_blurb(persona))

    bullets = [str(b).strip() for b in (memory_bullets or []) if str(b).strip()]
    if bullets:
        joined = "\n".join(f"- {b}" for b in bullets[:12])
        parts.append(f"你记得关于对方的一些事（自然地融入对话，不要生硬罗列）：\n{joined}")

    guidance = str(extra_guidance or "").strip() or _DEFAULT_GUIDANCE
    parts.append(guidance)

    tone = str(emotion_tone or "").strip()
    if tone:
        parts.append(tone)

    lang_name = {"zh": "中文", "en": "English"}.get(language, language)
    parts.append(f"默认用{lang_name}交流。")
    return "\n\n".join(p for p in parts if p).strip()


def _persona_blurb(persona: Dict[str, Any]) -> str:
    """从 persona dict 拼一段简洁人设画像（base_prompt 缺省时的兜底）。"""
    name = str(persona.get("name") or "").strip()
    role = str(persona.get("role") or "").strip()
    bg = str(persona.get("background") or "").strip()
    bits: List[str] = []
    if name:
        bits.append(f"你是{name}。")
    if role:
        bits.append(role if role.endswith(("。", ".", "！", "!")) else role + "。")
    p = persona.get("personality")
    if isinstance(p, dict):
        traits = [str(x).strip() for x in (p.get("traits") or []) if str(x).strip()]
        if traits:
            bits.append("性格：" + "、".join(traits[:6]) + "。")
        style = str(p.get("style") or "").strip()
        if style:
            bits.append("说话风格：" + style + "。")
    if bg:
        bits.append(bg if bg.endswith(("。", ".", "！", "!")) else bg + "。")
    return " ".join(bits).strip() or "你是一位温暖、真诚的陪伴者。"


# ── 会话初始化负载（网关 → 主机）──────────────────────────────────────────────
def build_session_init(
    *,
    system_prompt: str,
    language: str = "zh",
    voice_ref_b64: Optional[str] = None,
    voice: Optional[str] = None,
    sample_rate: int = _DEFAULT_SAMPLE_RATE,
    model: str = "",
    mode: str = "audio",
) -> Dict[str, Any]:
    """构造 ``session.init`` 负载。

    ``voice_ref_b64`` 是参考音频（base64），即 MiniCPM-o 的「音频系统提示」——给了就克隆
    该音色；没给则用主机内置 ``voice`` 名。``mode``: audio(纯语音全双工) | omni(带视频)。
    """
    lang = language if language in _SUPPORTED_LANGS else "zh"
    payload: Dict[str, Any] = {
        "type": EV_SESSION_INIT,
        "mode": mode if mode in ("audio", "omni") else "audio",
        "language": lang,
        "system_prompt": str(system_prompt or "").strip(),
        "sample_rate": int(sample_rate) if int(sample_rate or 0) > 0 else _DEFAULT_SAMPLE_RATE,
    }
    if model:
        payload["model"] = str(model)
    if voice_ref_b64:
        payload["voice_ref_b64"] = str(voice_ref_b64)
    elif voice:
        payload["voice"] = str(voice)
    return payload


def input_audio_event(audio_b64: str, *, seq: Optional[int] = None) -> Dict[str, Any]:
    """用户音频分片事件（base64 PCM16 单声道）。"""
    ev: Dict[str, Any] = {"type": EV_INPUT_AUDIO, "audio_b64": str(audio_b64 or "")}
    if seq is not None:
        ev["seq"] = int(seq)
    return ev


def interrupt_event() -> Dict[str, Any]:
    return {"type": EV_INTERRUPT}


# ── 事件序列化/解析 ───────────────────────────────────────────────────────────
def dumps_event(ev: Dict[str, Any]) -> str:
    """事件 → JSON 字符串（紧凑、ensure_ascii=False 便于中文调试）。"""
    return json.dumps(ev, ensure_ascii=False, separators=(",", ":"))


def parse_host_event(raw: Any) -> Dict[str, Any]:
    """把主机来的原始消息（str/bytes/dict）归一化成 ``{type, ...}``。

    无法识别 → ``{"type":"error","error":...}``（绝不抛，实时链路里抛=掉线）。
    """
    data: Any = raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            data = raw.decode("utf-8")
        except Exception:
            return {"type": EV_ERROR, "error": "undecodable_bytes"}
    if isinstance(data, str):
        s = data.strip()
        if not s:
            return {"type": EV_ERROR, "error": "empty"}
        try:
            data = json.loads(s)
        except Exception:
            return {"type": EV_ERROR, "error": "bad_json"}
    if not isinstance(data, dict):
        return {"type": EV_ERROR, "error": "not_object"}
    etype = str(data.get("type") or "").strip()
    if etype not in _HOST_EVENTS:
        return {"type": EV_ERROR, "error": f"unknown_event:{etype or '∅'}", "raw_type": etype}
    return data


def is_host_event(etype: str) -> bool:
    return etype in _HOST_EVENTS


def is_client_event(etype: str) -> bool:
    return etype in _CLIENT_EVENTS


__all__ = [
    "RealtimeVoiceConfig",
    "pick_language",
    "build_call_system_prompt",
    "build_session_init",
    "input_audio_event",
    "interrupt_event",
    "dumps_event",
    "parse_host_event",
    "is_host_event",
    "is_client_event",
    # 事件常量
    "EV_SESSION_INIT", "EV_INPUT_AUDIO", "EV_INPUT_TEXT", "EV_INPUT_DONE",
    "EV_INTERRUPT", "EV_SESSION_CLOSE",
    "EV_READY", "EV_TRANSCRIPT_USER", "EV_TRANSCRIPT_ASSISTANT",
    "EV_TRANSCRIPT_TRANSLATION", "EV_OUTPUT_AUDIO", "EV_TURN_END", "EV_ERROR",
]
