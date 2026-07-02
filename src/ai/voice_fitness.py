"""上下文感知语音触发评分（决定一条 AI 回复该用**语音**还是**文字**发出）。

旧的语音决策只看 ``trigger + 长度``，把项目早已算好并落库的情绪/亲密度/对话信号
全浪费了。本模块把「何时发语音」升级为**多信号情境评分**，让陪伴 AI 像真人一样
**只在该用声音的时刻才发语音**（情绪峰值/需要被安抚/客户也发语音），平时发文字——
"懂分寸"的克制才是高级的拟人感（见方案三角度分析）。

设计原则（与项目一脉相承）：
- **纯函数**：输入信号 → ``VoiceDecision``，零 IO、零副作用 → 易测、可作常驻门禁。
- **信号已有**：``analyze_emotion``(回复情感浓度) + ``conversation_meta``(客户此刻情绪/
  亲密度) + ``list_recent_messages``(频率)，全部现成，本模块只做"接线 + 加权"。
- **绝不阻塞**：任何异常都按"发文字"保守处理（``reason=error``），由调用方回落文本。
- **默认 opt-in**：仅 ``trigger=smart`` 时启用；其余 trigger 行为不变。

评分（权重/阈值全可配，见 ``DEFAULTS`` / ``config.yaml::...voice.smart``）：
- 硬否决：空 / 超长 / 含不可念内容(URL·长数字·代码) / 危机 → 文字
- 硬肯定：客户发了语音且 ``peer_voice_always`` → 语音（对等回应，最自然）
- 情境分：回复情绪强度 + 情绪类(非中性) + 客户此刻情绪强度 + 亲密度 + 短句
- 事务减分：回复含报价/售后/物流等事务内容 → 减分（语义"该用文字"）
- 频率衰减：近窗口语音占比超上限 → 强减分（保证"克制"，模拟真人偶尔发语音）
- ``score ≥ threshold`` → 语音
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

# 默认参数（定稿："克制"手感：阈值偏高 + 语音占比上限 1/4）。
# 经 config ``inbox.l2_autosend.voice.smart`` 覆盖；调高 threshold/调低 max_voice_ratio
# = 更克制少语音，反之更活跃。
DEFAULTS: Dict[str, Any] = {
    "threshold": 0.55,          # score ≥ 此值才发语音（越高越克制）
    "peer_voice_always": True,  # 客户发语音 → 硬性回语音（对等）
    "max_voice_ratio": 0.25,    # 近窗口 outbound 语音占比上限（超过→频率衰减）
    "freq_penalty": 0.30,       # 频率超限的减分量
    "short_len": 40,            # 短句阈值（≤ 加分，> 减分）
    "max_chars": 120,           # 超长硬否决（与 voice.max_chars 对齐）
    "transactional_penalty": 0.25,  # 回复含事务内容（报价/退款/物流…）→ 减分（语义"该用文字"）
    "weights": {
        "emotion": 0.35,        # 回复自身情绪强度（该不该用声音表达）
        "dimension": 0.25,      # 回复带明确情绪（非中性）
        "peer_emotion": 0.20,   # 客户此刻情绪强度（需不需要被声音安抚）
        "intimacy": 0.15,       # 关系越深越多语音
        "short": 0.10,          # 短句更适合语音
    },
}

# 不可念内容：语音念 URL/长数字/代码片段体验差（听不清、要反复听）→ 一律发文字。
_URL_RE = re.compile(r"(https?://|www\.|\b\w+\.(?:com|cn|net|org|io|me)\b)", re.I)
_LONG_DIGITS_RE = re.compile(r"\d{7,}")          # 电话/卡号/长价格串
_CODE_RE = re.compile(r"(</?\w+>|[{};]|\bdef\s|\bclass\s|=>|::)")
# 事务性内容：报价/售后/物流/凭证类回复念语音体验差且易错，倾向发文字（减分，非硬否决——
# "退款的事我帮你弄好啦"这类带安抚的事务回复偶尔也可语音）。用明确短语降低对情感陪聊的误伤。
_TRANSACTIONAL_RE = re.compile(
    r"(报价|费率|价格|多少钱|套餐价|优惠|折扣|退款|退货|换货|售后|物流|快递|运单|"
    r"发货|到货|订单号|账号|密码|验证码|转账|汇款|付款方式|"
    r"\bprice\b|\brefund\b|\border\b|\bshipping\b|\btracking\b|\bpayment\b)", re.I)


@dataclass
class VoiceDecision:
    """语音触发决策结果。``reason`` 供观测（看板按原因分布调阈值）。"""
    send_voice: bool
    score: float
    reason: str


def _clamp01(x: float) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def has_unspeakable(text: str) -> bool:
    """文本是否含「不适合念成语音」的内容（URL / 长数字 / 代码片段）。"""
    t = text or ""
    return bool(_URL_RE.search(t) or _LONG_DIGITS_RE.search(t) or _CODE_RE.search(t))


def is_transactional(text: str) -> bool:
    """回复是否含事务性内容（报价/售后/物流/凭证…）——语义上"更该用文字"。

    与 has_unspeakable 互补：后者抓"不可念"的形式（URL/数字），本函数抓"该用文字"的
    语义（如"退款流程""费率多少"——无 URL/数字但仍宜文字）。仅减分不硬否决。
    """
    return bool(_TRANSACTIONAL_RE.search(text or ""))


def voice_fitness(
    text: str,
    *,
    peer_sent_voice: bool = False,
    recent_voice_ratio: float = 0.0,
    peer_emotion: str = "",
    peer_emotion_intensity: float = -1.0,
    intimacy: float = 0.0,
    crisis_block: bool = False,
    cfg: Optional[Dict[str, Any]] = None,
) -> VoiceDecision:
    """情境评分：本条回复该用语音(``send_voice=True``)还是文字发出。

    参数（除 ``text`` 外均可选，缺省退化为 Stage1 最小信号集，零依赖外部状态）：
    - ``peer_sent_voice``：客户上一条入站是否语音（对等回应的最强信号）。
    - ``recent_voice_ratio``：近窗口 outbound 语音占比（0~1），频率控制。
    - ``peer_emotion`` / ``peer_emotion_intensity``：客户此刻情绪（来自
      ``conversation_meta``；intensity<0=未知，不计分）。
    - ``intimacy``：关系亲密度（0~1）。
    - ``crisis_block``：危机护栏是否要求抑制（severe）→ 硬否决，走安全网文本。
    - ``cfg``：``smart{}`` 参数块（覆盖 ``DEFAULTS``）。

    保证：任何异常 → ``VoiceDecision(False, 0, "error")``（保守发文字，绝不抛出阻塞出站）。
    """
    try:
        c = dict(DEFAULTS)
        if isinstance(cfg, dict):
            c.update({k: v for k, v in cfg.items() if k != "weights"})
            if isinstance(cfg.get("weights"), dict):
                c["weights"] = {**DEFAULTS["weights"], **cfg["weights"]}
        w = c["weights"]

        t = (text or "").strip()
        # ── 硬否决 ──
        if not t:
            return VoiceDecision(False, 0.0, "empty")
        if len(t) > int(c["max_chars"]):
            return VoiceDecision(False, 0.0, "too_long")
        if has_unspeakable(t):
            return VoiceDecision(False, 0.0, "unspeakable")
        if crisis_block:
            return VoiceDecision(False, 0.0, "crisis_safe")

        # ── 硬肯定：对等回应（客户发语音→回语音，最自然，绕过评分）──
        if peer_sent_voice and bool(c["peer_voice_always"]):
            return VoiceDecision(True, 1.0, "peer_voice")

        # ── 情境评分 ──
        from src.utils.emotional_context import analyze_emotion
        emo = analyze_emotion(t) or {}
        score = 0.0
        score += float(w["emotion"]) * _clamp01(emo.get("primary_intensity", 0.0))
        if str(emo.get("dimension") or "neutral") != "neutral":
            score += float(w["dimension"])
        if peer_emotion_intensity is not None and float(peer_emotion_intensity) >= 0:
            score += float(w["peer_emotion"]) * _clamp01(peer_emotion_intensity)
        score += float(w["intimacy"]) * _clamp01(intimacy)
        score += float(w["short"]) if len(t) <= int(c["short_len"]) else -float(w["short"])
        # 事务性内容（报价/售后/物流…）→ 减分（语义"该用文字"，补 has_unspeakable 的语义盲区）
        if is_transactional(t):
            score -= float(c["transactional_penalty"])

        # ── 频率衰减（保证"克制"：近期已多发语音→压低）──
        if float(recent_voice_ratio) >= float(c["max_voice_ratio"]):
            score -= float(c["freq_penalty"])

        send = score >= float(c["threshold"])
        return VoiceDecision(
            send, round(score, 3), "smart_voice" if send else "low_fitness")
    except Exception:
        return VoiceDecision(False, 0.0, "error")


__all__ = ["VoiceDecision", "voice_fitness", "has_unspeakable",
           "is_transactional", "DEFAULTS"]
