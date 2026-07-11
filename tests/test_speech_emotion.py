"""音频情绪识别（SER）门禁：纯 mapping + 识别器软降级 + 安全困扰分级。

不触网、不加载任何真实模型（recognizer 用假 model 注入或走 disabled 短路）。
"""
import pytest

from src.ai.speech_emotion import (
    SpeechEmotionRecognizer,
    SpeechEmotionResult,
    audio_distress_level,
    map_audio_emotion,
    normalize_e2v_label,
    peer_emotion_to_reply,
    pick_top_emotion,
)


# ── normalize_e2v_label ──────────────────────────────────────────────
@pytest.mark.parametrize("raw,expect", [
    ("angry", "angry"),
    ("生气/angry", "angry"),
    ("愤怒 anger", "angry"),
    ("厌恶/disgusted", "disgusted"),
    ("害怕/fearful", "fearful"),
    ("恐惧 fear", "fearful"),
    ("开心/happy", "happy"),
    ("中立/neutral", "neutral"),
    ("难过/sad", "sad"),
    ("惊讶/surprised", "surprised"),
    ("<unk>", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
])
def test_normalize_label(raw, expect):
    assert normalize_e2v_label(raw) == expect


def test_pick_top_emotion_argmax():
    labels = ["生气/angry", "难过/sad", "中立/neutral"]
    scores = [0.1, 0.75, 0.15]
    emo, score, agg = pick_top_emotion(labels, scores)
    assert emo == "sad"
    assert score == pytest.approx(0.75)
    assert agg["sad"] == pytest.approx(0.75)


def test_pick_top_emotion_empty():
    assert pick_top_emotion([], []) == ("unknown", 0.0, {})


# ── map_audio_emotion ────────────────────────────────────────────────
def test_map_sad_confident_negative():
    d = map_audio_emotion("sad", 0.82, min_confidence=0.5)
    assert d["primary_emotion"] == "低落"
    assert d["dimension"] == "negative"
    assert d["confident"] is True
    assert d["valence"] < 0
    assert d["source"] == "audio"


def test_map_happy_confident_positive():
    d = map_audio_emotion("happy", 0.9)
    assert d["primary_emotion"] == "积极"
    assert d["dimension"] == "positive"
    assert d["valence"] > 0


def test_map_low_confidence_not_confident():
    d = map_audio_emotion("sad", 0.3, min_confidence=0.5)
    assert d["confident"] is False
    assert d["dimension"] == "neutral"


def test_map_other_unknown_not_confident():
    for lab in ("other", "unknown"):
        d = map_audio_emotion(lab, 0.99)
        assert d["confident"] is False
        assert d["dimension"] == "neutral"


def test_map_neutral_low_intensity():
    d = map_audio_emotion("neutral", 0.9)
    assert d["primary_emotion"] == "平稳"
    assert d["primary_intensity"] <= 0.3


# ── peer_emotion_to_reply（回应式，非镜像）────────────────────────────
@pytest.mark.parametrize("label,score,expect", [
    ("sad", 0.8, "empathetic"),
    ("angry", 0.8, "apologetic"),
    ("disgusted", 0.8, "apologetic"),
    ("fearful", 0.8, "calm"),
    ("happy", 0.8, "happy"),
    ("surprised", 0.8, "warm"),
    ("neutral", 0.9, None),
    ("sad", 0.3, None),          # 低置信 → 不驱动
])
def test_peer_to_reply(label, score, expect):
    assert peer_emotion_to_reply(label, score, min_confidence=0.5) == expect


# ── audio_distress_level（保守：仅 none/elevated，绝不 severe）──────────
def test_distress_sad_high_elevated():
    d = map_audio_emotion("sad", 0.8)
    assert audio_distress_level(d, min_confidence=0.6) == "elevated"


def test_distress_fearful_high_elevated():
    d = map_audio_emotion("fearful", 0.7)
    assert audio_distress_level(d, min_confidence=0.6) == "elevated"


def test_distress_none_when_low_or_positive():
    assert audio_distress_level(map_audio_emotion("sad", 0.55), min_confidence=0.6) == "none"
    assert audio_distress_level(map_audio_emotion("happy", 0.9)) == "none"
    assert audio_distress_level(None) == "none"
    assert audio_distress_level({}) == "none"


def test_distress_never_returns_severe():
    # 无论多高分，声学都不产 severe（安全红线须文字命中）
    for lab in ("sad", "fearful", "angry"):
        assert audio_distress_level(map_audio_emotion(lab, 0.99)) in ("none", "elevated")


# ── 识别器软降级 ──────────────────────────────────────────────────────
def test_recognizer_disabled_soft_degrade():
    r = SpeechEmotionRecognizer({"enabled": False})
    assert r.is_available() is False
    res = r.recognize("nonexistent.wav")
    assert isinstance(res, SpeechEmotionResult)
    assert res.ok is False


def test_recognizer_backend_disabled():
    r = SpeechEmotionRecognizer({"enabled": True, "backend": "disabled"})
    assert r.is_available() is False
    assert r.recognize("x.wav").ok is False


def test_recognizer_parses_injected_model():
    """注入假 funasr model，验证 generate 结果解析（不触网/不加载真模型）。"""
    class _FakeModel:
        def generate(self, *a, **k):
            return [{"labels": ["生气/angry", "难过/sad", "中立/neutral"],
                     "scores": [0.05, 0.8, 0.15]}]

    r = SpeechEmotionRecognizer({"enabled": True, "backend": "funasr"})
    r._model = _FakeModel()  # 跳过真实加载
    res = r.recognize("x.wav")
    assert res.ok is True
    assert res.emotion == "sad"
    assert res.score == pytest.approx(0.8)
    ed = res.as_emotion_dict()
    assert ed["primary_emotion"] == "低落"
    assert ed["confident"] is True


def test_recognizer_generate_exception_soft_degrade():
    class _BoomModel:
        def generate(self, *a, **k):
            raise RuntimeError("cuda oom")

    r = SpeechEmotionRecognizer({"enabled": True, "backend": "funasr"})
    r._model = _BoomModel()
    res = r.recognize("x.wav")
    assert res.ok is False
    assert "cuda oom" in res.error


# ── 远程 GPU SER（176 asr_server /v1/audio/emotion）─────────────────────
_REMOTE_CFG = {
    "enabled": True, "backend": "funasr",
    "remote": {"base_url": "http://gpu:8765", "timeout_sec": 5,
               "cb_cooldown_sec": 120},
}


def test_remote_ser_success_maps_client_side():
    """远程只回 labels/scores 原始数组；标签→系统语义映射仍在客户端单一出口。"""
    calls = []

    def _fake_post(url, path, timeout):
        calls.append(url)
        return 200, {"labels": ["生气/angry", "难过/sad", "中立/neutral"],
                     "scores": [0.1, 0.7, 0.2], "model": "emotion2vec_plus_large"}

    r = SpeechEmotionRecognizer(_REMOTE_CFG)
    r._post_fn = _fake_post
    res = r.recognize("x.wav")
    assert res.ok is True
    assert res.emotion == "sad"
    assert res.model.startswith("remote:")
    assert calls == ["http://gpu:8765/v1/audio/emotion"]
    assert r._model is None   # 远程成功 → 本地模型从未加载


def test_remote_ser_failure_falls_back_to_local_and_cools_down():
    class _FakeLocal:
        def generate(self, *a, **k):
            return [{"labels": ["开心/happy"], "scores": [0.9]}]

    def _boom(url, path, timeout):
        raise RuntimeError("connect timeout")

    r = SpeechEmotionRecognizer(_REMOTE_CFG)
    r._post_fn = _boom
    r._model = _FakeLocal()
    res = r.recognize("x.wav")
    assert res.ok is True
    assert res.emotion == "happy"          # 回落本地结果
    assert not res.model.startswith("remote:")
    assert r._remote_bad_until > 0         # 远程进冷却
    # 冷却期内第二次识别不再打远程（_post_fn 再 raise 会被计数出来）
    boom_calls = []

    def _boom2(url, path, timeout):
        boom_calls.append(url)
        raise RuntimeError("still down")

    r._post_fn = _boom2
    assert r.recognize("x.wav").emotion == "happy"
    assert boom_calls == []


def test_remote_ser_http_error_falls_back():
    def _http500(url, path, timeout):
        return 500, {}

    class _FakeLocal:
        def generate(self, *a, **k):
            return [{"labels": ["中立/neutral"], "scores": [0.8]}]

    r = SpeechEmotionRecognizer(_REMOTE_CFG)
    r._post_fn = _http500
    r._model = _FakeLocal()
    res = r.recognize("x.wav")
    assert res.ok is True
    assert res.emotion == "neutral"


def test_remote_keeps_available_despite_local_breaker():
    """本地加载熔断打开时，远程可用仍应 is_available=True（远程不受本地断路器牵连）。"""
    import time as _t
    r = SpeechEmotionRecognizer(_REMOTE_CFG)
    r._cb_open_until = _t.time() + 300     # 本地断路器打开
    assert r.is_available() is True        # 远程兜住
    r._remote_bad_until = _t.time() + 120  # 远程也冷却 → 整体不可用
    assert r.is_available() is False


def test_no_remote_config_keeps_legacy_behavior():
    r = SpeechEmotionRecognizer({"enabled": True, "backend": "funasr"})
    assert r.remote_base == ""
    assert r._remote_usable() is False
    assert r._recognize_remote("x.wav") is None
