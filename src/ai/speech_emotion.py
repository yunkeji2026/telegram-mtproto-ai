"""音频情绪识别（SER）— 从**声学语气**里听出对方情绪，而非从文字猜。

为什么需要：本项目的情绪链此前**只从文字**分析（`quick_analyze` 出标签、
`analyze_emotion` 出强度）。文字抓不到「嘴上说没事、声音却在抖」这类**言不由衷**。
本模块用 emotion2vec+（FunASR，9 类 SER 基座模型）对**语音文件**做一次前向，
得到声学情绪标签 + 置信度，交由 `emotion_fusion.fuse_emotion` 与文字情绪融合，
汇入既有 `conversation_meta.last_emotion / last_emotion_intensity`，让共情回复、
危机安全网、主动护栏、出站情感声**全部**用上更准的信号。

设计哲学（对齐 `src/ai/audio_pipeline.py`）：
- **惰加载 + 软降级**：模型仅在首次识别时加载；缺 `funasr`/`torch` 依赖或加载失败
  → circuit breaker 打开、返回「无情绪」，调用链退回纯文字情绪（**零破坏**）。
- **纯函数 mapping 与 IO 分离**：标签映射是无依赖纯函数（常驻可测）；模型推理才碰 IO。
- **默认关**（遵循本仓库 feature flag 约定），经 `config.local.yaml` overlay 开。
- **隐私红线**：只产出情绪标签/分数，**绝不返回或落库任何音频原文/波形**。

emotion2vec+ 9 类（`iic/emotion2vec_plus_*`）：
    0 angry / 1 disgusted / 2 fearful / 3 happy / 4 neutral /
    5 other / 6 sad / 7 surprised / 8 unknown
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── 纯函数：emotion2vec 标签 → 系统情绪语义（无依赖，常驻可测）───────────────────

# 规整后的 9 类英文标准名（供内部使用；模型可能返回 "生气/angry" 这类中英合串）。
_CANON = ("angry", "disgusted", "fearful", "happy", "neutral",
          "other", "sad", "surprised", "unknown")

# emotion2vec 类 → 本系统 `last_emotion` 中文标签词表（与 chat_assistant_service
# ._detect_emotion 同域：低落/生气/焦虑/积极/平稳），保证融合后落库口径一致。
_E2V_TO_CN = {
    "angry": "生气",
    "disgusted": "生气",
    "fearful": "焦虑",
    "happy": "积极",
    "neutral": "平稳",
    "sad": "低落",
    "surprised": "平稳",   # 惊讶价性含糊 → 归中性，避免污染负面/正面判定
    "other": "平稳",
    "unknown": "平稳",
}

# emotion2vec 类 → 粗粒度维度（positive/negative/neutral），供护栏/危机分级。
_E2V_TO_DIM = {
    "angry": "negative",
    "disgusted": "negative",
    "fearful": "negative",
    "sad": "negative",
    "happy": "positive",
    "neutral": "neutral",
    "surprised": "neutral",
    "other": "neutral",
    "unknown": "neutral",
}

# emotion2vec 类 → 近似 arousal（激活度 0~1）。sad 低唤醒、angry/fearful 高唤醒。
_E2V_AROUSAL = {
    "angry": 0.8, "fearful": 0.75, "surprised": 0.7, "happy": 0.65,
    "disgusted": 0.55, "neutral": 0.3, "sad": 0.4, "other": 0.3, "unknown": 0.3,
}

# 出站「回应」情绪映射（RESPONSE，非镜像）：对方什么语气 → 我们用什么语气回。
# 关键：对方难过我们**温柔共情**而非跟着难过；对方生气我们**歉意安抚**去降级。
_PEER_TO_REPLY_EMOTION = {
    "sad": "empathetic",
    "fearful": "calm",
    "angry": "apologetic",
    "disgusted": "apologetic",
    "happy": "happy",
    "surprised": "warm",
    "neutral": None,       # 中性 → 不干预，交回其它信号（intent/人设）决定
    "other": None,
    "unknown": None,
}


def normalize_e2v_label(raw: Any) -> str:
    """把模型返回的标签（可能是 "生气/angry" / "angry" / 索引名）规整成标准英文类名。

    鲁棒：小写后按英文关键词子串命中；无法识别 → "unknown"（保守）。
    """
    s = str(raw or "").strip().lower()
    if not s:
        return "unknown"
    # 关键词 → 标准名（覆盖 disgust/fear/surprise 词干变体）
    for key, canon in (
        ("angry", "angry"), ("anger", "angry"),
        ("disgust", "disgusted"),
        ("fear", "fearful"),
        ("happy", "happy"), ("joy", "happy"),
        ("neutral", "neutral"),
        ("sad", "sad"),
        ("surprise", "surprised"),
        ("other", "other"),
        ("unknown", "unknown"),
    ):
        if key in s:
            return canon
    return "unknown"


def pick_top_emotion(
    labels: List[Any], scores: List[float]
) -> Tuple[str, float, Dict[str, float]]:
    """从并行的 labels/scores 取 argmax，返回 (标准类名, 分数, {标准类名: 分数})。

    纯函数：空/长度不齐 → ("unknown", 0.0, {})。
    """
    if not labels or not scores:
        return "unknown", 0.0, {}
    agg: Dict[str, float] = {}
    for lb, sc in zip(labels, scores):
        try:
            f = float(sc)
        except (TypeError, ValueError):
            continue
        canon = normalize_e2v_label(lb)
        # 同类多命中取最大（理论上不会，防御）
        agg[canon] = max(agg.get(canon, 0.0), f)
    if not agg:
        return "unknown", 0.0, {}
    top = max(agg.items(), key=lambda kv: kv[1])
    return top[0], float(top[1]), agg


def map_audio_emotion(raw_label: Any, score: float,
                      *, min_confidence: float = 0.5) -> Dict[str, Any]:
    """emotion2vec 结果 → 与 `analyze_emotion` 同形的情绪 dict（供融合器消费）。

    返回：``{primary_emotion(中文标签), dimension, primary_intensity, valence,
    arousal, raw_label, score, source:"audio", confident:bool}``。
    低于 ``min_confidence`` 或 other/unknown → 视为「无有效声学信号」(confident=False,
    dimension=neutral)，融合时不覆盖文字判断。
    """
    canon = normalize_e2v_label(raw_label)
    try:
        sc = max(0.0, min(1.0, float(score)))
    except (TypeError, ValueError):
        sc = 0.0
    confident = sc >= float(min_confidence) and canon not in ("other", "unknown")
    dim = _E2V_TO_DIM.get(canon, "neutral") if confident else "neutral"
    cn = _E2V_TO_CN.get(canon, "平稳") if confident else "平稳"
    # 强度：用置信度作声学表达强度代理（neutral 压低）。
    if not confident or canon == "neutral":
        intensity = min(sc, 0.3)
    else:
        intensity = sc
    arousal = _E2V_AROUSAL.get(canon, 0.3)
    if dim == "negative":
        valence = -sc
    elif dim == "positive":
        valence = sc
    else:
        valence = 0.0
    return {
        "primary_emotion": cn,
        "dimension": dim,
        "primary_intensity": round(float(intensity), 4),
        "valence": round(float(valence), 4),
        "arousal": round(float(arousal), 4),
        "raw_label": canon,
        "score": round(sc, 4),
        "source": "audio",
        "confident": bool(confident),
    }


def peer_emotion_to_reply(raw_label: Any, score: float,
                          *, min_confidence: float = 0.5) -> Optional[str]:
    """对方声学情绪 → 我方出站回应情绪（TTS EMOTIONS 之一）；不确定/中性 → None。"""
    canon = normalize_e2v_label(raw_label)
    try:
        sc = float(score)
    except (TypeError, ValueError):
        sc = 0.0
    if sc < float(min_confidence):
        return None
    return _PEER_TO_REPLY_EMOTION.get(canon)


def audio_distress_level(audio_emo: Optional[Dict[str, Any]],
                         *, min_confidence: float = 0.6) -> str:
    """声学困扰分级（安全用，**保守**）：仅返回 ``none`` | ``elevated``。

    刻意**不产出 severe**——severe（自伤/轻生）是安全红线，须由文字 `detect_crisis`
    明确命中，声学单独判定误报代价过高。这里只在**高置信** sad/fearful 时抬到 elevated
    （更共情、可带资源），与文字危机取较高档合并（见 skill_manager 联动）。
    """
    if not isinstance(audio_emo, dict):
        return "none"
    if not audio_emo.get("confident"):
        return "none"
    canon = str(audio_emo.get("raw_label") or "")
    try:
        sc = float(audio_emo.get("score") or 0.0)
    except (TypeError, ValueError):
        sc = 0.0
    if canon in ("sad", "fearful") and sc >= float(min_confidence):
        return "elevated"
    return "none"


# ── SER 识别器（惰加载 emotion2vec；软降级）──────────────────────────────────────

@dataclass
class SpeechEmotionResult:
    ok: bool = False
    emotion: str = "unknown"          # 标准英文类名
    score: float = 0.0
    all_scores: Dict[str, float] = field(default_factory=dict)
    model: str = ""
    latency_ms: int = 0
    error: str = ""

    def as_emotion_dict(self, *, min_confidence: float = 0.5) -> Optional[Dict[str, Any]]:
        """成功 → 与 analyze_emotion 同形的音频情绪 dict；失败 → None。"""
        if not self.ok:
            return None
        return map_audio_emotion(self.emotion, self.score,
                                 min_confidence=min_confidence)


class SpeechEmotionRecognizer:
    """emotion2vec+ 的惰加载包装（circuit breaker + 软降级），风格对齐 AudioPipeline。

    Config：
        enabled: true/false
        backend: funasr | disabled
        model: iic/emotion2vec_plus_large | ..._base | ..._seed
        hub: ms | hf
        device: cpu | cuda
        min_confidence: 0.5      # 低于此视为无有效声学信号
        cb_cooldown_sec: 300     # 加载失败后多久不再重试
        remote:                  # 可选：远程 GPU SER（176 asr_server /v1/audio/emotion）
          base_url: http://192.168.0.176:8765   # 空=纯本地（旧行为）
          timeout_sec: 10
          cb_cooldown_sec: 120   # 远程失败后冷却期内直走本地，绝不反复打死端点

    远程优先、本地兜底：remote.base_url 配置时先试远程（服务端只回 labels/scores
    原始数组，标签→系统语义的映射仍在本模块单一出口）；远程超时/HTTP 错 → 进冷却
    并回落**本地 funasr**（现行为），语音链路零阻断。
    """

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        cfg = cfg or {}
        self._cfg = dict(cfg)
        self.enabled = bool(cfg.get("enabled", False))
        self.backend = str(cfg.get("backend", "funasr")).strip().lower()
        self.model_name = str(cfg.get("model") or "iic/emotion2vec_plus_large").strip()
        self.hub = str(cfg.get("hub") or "ms").strip().lower()
        self.device = str(cfg.get("device") or "cpu").strip().lower()
        self.min_confidence = float(cfg.get("min_confidence", 0.5) or 0.5)
        self.cb_cooldown_sec = float(cfg.get("cb_cooldown_sec", 300) or 300)

        rem = cfg.get("remote") or {}
        if not isinstance(rem, dict):
            rem = {}
        self.remote_base = str(rem.get("base_url") or "").strip().rstrip("/")
        self.remote_timeout = float(rem.get("timeout_sec", 10) or 10)
        self.remote_cb_sec = float(rem.get("cb_cooldown_sec", 120) or 120)
        self._remote_bad_until: float = 0.0
        self._post_fn: Any = None   # 测试钩子：(url, path, timeout) -> (status, dict)

        self._model: Any = None
        self._lock = threading.Lock()
        self._cb_open_until: float = 0.0
        self._last_error: str = ""

    def _remote_usable(self) -> bool:
        return bool(self.remote_base) and time.time() >= self._remote_bad_until

    def is_available(self) -> bool:
        if not self.enabled or self.backend == "disabled":
            return False
        if self._remote_usable():
            return True   # 远程可用时不受本地加载熔断影响
        if self._cb_open_until > 0 and time.time() < self._cb_open_until:
            return False
        return True

    def _load_model(self) -> bool:
        if self._model is not None:
            return True
        if not self.is_available():
            return False
        with self._lock:
            if self._model is not None:
                return True
            t0 = time.monotonic()
            try:
                from funasr import AutoModel  # type: ignore
                self._model = AutoModel(
                    model=self.model_name,
                    hub=self.hub,
                    device=self.device,
                    disable_update=True,
                )
                ms = int((time.monotonic() - t0) * 1000)
                logger.info(
                    "[speech_emotion] model loaded model=%s hub=%s device=%s took=%dms",
                    self.model_name, self.hub, self.device, ms,
                )
                return True
            except ImportError as ex:
                self._last_error = f"missing dependency: {ex!s} (pip install -U funasr)"
                self._cb_open_until = time.time() + self.cb_cooldown_sec
                logger.warning("[speech_emotion] %s, circuit open for %.0fs",
                               self._last_error, self.cb_cooldown_sec)
                return False
            except Exception as ex:  # noqa: BLE001
                self._last_error = f"{type(ex).__name__}: {ex}"
                self._cb_open_until = time.time() + self.cb_cooldown_sec
                logger.warning("[speech_emotion] load failed: %s, circuit open for %.0fs",
                               self._last_error, self.cb_cooldown_sec)
                return False

    def _recognize_remote(self, audio_path: str) -> Optional[SpeechEmotionResult]:
        """远程 GPU SER。未配置/冷却中/失败 → None（调用方回落本地）。绝不抛。"""
        if not self._remote_usable():
            return None
        url = f"{self.remote_base}/v1/audio/emotion"
        t0 = time.monotonic()
        try:
            if self._post_fn is not None:
                status, payload = self._post_fn(url, audio_path, self.remote_timeout)
            else:
                import os as _os

                import httpx
                with open(audio_path, "rb") as f:
                    resp = httpx.post(
                        url,
                        files={"file": (_os.path.basename(audio_path), f)},
                        timeout=self.remote_timeout,
                    )
                status, payload = resp.status_code, (
                    resp.json() if resp.status_code == 200 else {})
            if status != 200:
                raise RuntimeError(f"http_{status}")
            labels = list(payload.get("labels") or [])
            scores = list(payload.get("scores") or [])
            emo, score, agg = pick_top_emotion(labels, scores)
            return SpeechEmotionResult(
                ok=True, emotion=emo, score=score, all_scores=agg,
                model=f"remote:{payload.get('model') or ''}",
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as ex:  # noqa: BLE001 - 远程任何失败都回落本地
            self._remote_bad_until = time.time() + self.remote_cb_sec
            logger.warning(
                "[speech_emotion] remote SER failed (%s: %s), fallback to local "
                "for %.0fs", type(ex).__name__, ex, self.remote_cb_sec)
            return None

    def recognize(self, audio_path: str) -> SpeechEmotionResult:
        """同步识别一个音频文件的情绪。任何异常都软降级为 ok=False（绝不抛）。"""
        if not self.enabled or self.backend == "disabled":
            return SpeechEmotionResult(ok=False, error="disabled")
        remote = self._recognize_remote(audio_path)
        if remote is not None:
            return remote
        if not self.is_available():
            return SpeechEmotionResult(ok=False, error=self._last_error or "disabled")
        if not self._load_model():
            return SpeechEmotionResult(ok=False, error=self._last_error or "load_failed")
        t0 = time.monotonic()
        try:
            res = self._model.generate(
                audio_path, granularity="utterance", extract_embedding=False)
            item = res[0] if isinstance(res, (list, tuple)) and res else res
            labels = (item or {}).get("labels") or []
            scores = (item or {}).get("scores") or []
            emo, score, agg = pick_top_emotion(list(labels), list(scores))
            return SpeechEmotionResult(
                ok=True, emotion=emo, score=score, all_scores=agg,
                model=self.model_name,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as ex:  # noqa: BLE001 - 软降级
            self._last_error = f"{type(ex).__name__}: {ex}"
            logger.debug("[speech_emotion] recognize failed: %s", self._last_error,
                         exc_info=True)
            return SpeechEmotionResult(ok=False, error=self._last_error)

    async def recognize_async(self, audio_path: str) -> SpeechEmotionResult:
        """异步包装：模型推理丢到线程池，避免阻塞事件循环。"""
        import asyncio
        return await asyncio.to_thread(self.recognize, audio_path)


_SINGLETON: Optional[SpeechEmotionRecognizer] = None
_SINGLETON_LOCK = threading.Lock()


def get_speech_emotion_recognizer(
    cfg: Optional[Dict[str, Any]] = None,
) -> SpeechEmotionRecognizer:
    """进程级单例（首次传 cfg 定型；此后复用，避免重复加载模型）。"""
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = SpeechEmotionRecognizer(cfg or {})
    return _SINGLETON


def reset_speech_emotion_recognizer() -> None:
    """测试用：重置单例。"""
    global _SINGLETON
    with _SINGLETON_LOCK:
        _SINGLETON = None


__all__ = [
    "SpeechEmotionResult",
    "SpeechEmotionRecognizer",
    "get_speech_emotion_recognizer",
    "reset_speech_emotion_recognizer",
    "normalize_e2v_label",
    "pick_top_emotion",
    "map_audio_emotion",
    "peer_emotion_to_reply",
    "audio_distress_level",
]
