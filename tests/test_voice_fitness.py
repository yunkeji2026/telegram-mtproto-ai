"""上下文感知语音触发评分（src/ai/voice_fitness.py）单测。

策略：硬规则用例不依赖情绪词典（确定性）；评分逻辑用例 monkeypatch analyze_emotion
固定 intensity/dimension 以隔离加权/阈值/频率；另有真实 analyze_emotion 的方向性集成用例。
"""
from __future__ import annotations

from src.ai.voice_fitness import voice_fitness, has_unspeakable, is_transactional


def _emo(monkeypatch, *, intensity=0.3, dimension="neutral"):
    monkeypatch.setattr(
        "src.utils.emotional_context.analyze_emotion",
        lambda t: {"primary_emotion": "x", "primary_intensity": intensity,
                   "dimension": dimension, "all_emotions": {},
                   "valence": 0.0, "arousal": 0.0})


# ── 硬否决 ────────────────────────────────────────────────────────
def test_empty_text_is_text():
    assert voice_fitness("").send_voice is False
    assert voice_fitness("   ").reason == "empty"


def test_too_long_is_text():
    d = voice_fitness("一" * 200)
    assert d.send_voice is False and d.reason == "too_long"


def test_url_is_text():
    d = voice_fitness("给你链接 https://example.com/x")
    assert d.send_voice is False and d.reason == "unspeakable"


def test_long_digits_is_text():
    d = voice_fitness("我的电话 13800001111")
    assert d.send_voice is False and d.reason == "unspeakable"


def test_code_is_text():
    d = voice_fitness("运行 def foo(): pass")
    assert d.send_voice is False and d.reason == "unspeakable"


def test_crisis_block_is_text():
    d = voice_fitness("抱抱你别难过", crisis_block=True)
    assert d.send_voice is False and d.reason == "crisis_safe"


# ── 硬肯定：对等回应 ──────────────────────────────────────────────
def test_peer_voice_always_returns_voice():
    d = voice_fitness("好的呀", peer_sent_voice=True)
    assert d.send_voice is True and d.reason == "peer_voice"


def test_peer_voice_but_url_still_text():
    # 硬否决优先于对等：客户发语音但回复含网址 → 仍发文字
    d = voice_fitness("看这个 www.x.com", peer_sent_voice=True)
    assert d.send_voice is False and d.reason == "unspeakable"


def test_peer_voice_off_falls_to_scoring(monkeypatch):
    _emo(monkeypatch, intensity=0.3, dimension="neutral")
    d = voice_fitness("好的", peer_sent_voice=True, cfg={"peer_voice_always": False})
    assert d.send_voice is False  # 关掉对等硬规则 → 走评分（中性低分）


# ── 情境评分 ──────────────────────────────────────────────────────
def test_high_emotion_short_is_voice(monkeypatch):
    _emo(monkeypatch, intensity=0.8, dimension="positive")
    d = voice_fitness("想你啦")  # 0.35*0.8 + 0.25 + 0.10 = 0.63 ≥ 0.55
    assert d.send_voice is True and d.reason == "smart_voice"


def test_neutral_plain_is_text(monkeypatch):
    _emo(monkeypatch, intensity=0.3, dimension="neutral")
    d = voice_fitness("好的收到")  # 0.35*0.3 + 0 + 0.10 = 0.205 < 0.55
    assert d.send_voice is False and d.reason == "low_fitness"


def test_peer_emotion_intensity_adds(monkeypatch):
    _emo(monkeypatch, intensity=0.5, dimension="positive")
    # base: 0.35*0.5 + 0.25 + 0.10 = 0.525 < 0.55 → 文字
    assert voice_fitness("嗯呢").send_voice is False
    # + 客户此刻高情绪 0.20*1.0 → 0.725 → 语音（需要被声音安抚）
    assert voice_fitness("嗯呢", peer_emotion_intensity=1.0).send_voice is True


def test_intimacy_adds(monkeypatch):
    _emo(monkeypatch, intensity=0.5, dimension="positive")
    d = voice_fitness("嗯呢", intimacy=1.0)  # 0.525 + 0.15 = 0.675
    assert d.send_voice is True


def test_long_sentence_penalty(monkeypatch):
    _emo(monkeypatch, intensity=0.8, dimension="positive")
    assert voice_fitness("想你啦").send_voice is True  # 短句 0.63
    long_t = "想你啦" + "好" * 50  # >40字 但 ≤120 → 短句减分
    assert voice_fitness(long_t).send_voice is False    # 0.43 < 0.55


def test_frequency_decay_throttles_voice(monkeypatch):
    _emo(monkeypatch, intensity=0.8, dimension="positive")
    assert voice_fitness("想你啦").send_voice is True   # 无频率压力 0.63
    # 近窗口语音占比超上限 → -0.30 → 0.33 < 0.55（保证"克制"）
    d = voice_fitness("想你啦", recent_voice_ratio=0.5)
    assert d.send_voice is False and d.reason == "low_fitness"


# ── cfg 覆盖 ──────────────────────────────────────────────────────
def test_cfg_threshold_override(monkeypatch):
    _emo(monkeypatch, intensity=0.3, dimension="neutral")
    assert voice_fitness("好的").send_voice is False          # 默认阈值
    assert voice_fitness("好的", cfg={"threshold": 0.1}).send_voice is True


def test_cfg_weights_override(monkeypatch):
    _emo(monkeypatch, intensity=0.3, dimension="neutral")
    d = voice_fitness("好的", cfg={"weights": {"short": 0.9}})
    assert d.send_voice is True  # short 0.9 + 0.105 = 1.005


# ── has_unspeakable ───────────────────────────────────────────────
def test_has_unspeakable():
    assert has_unspeakable("https://x.com") is True
    assert has_unspeakable("打给我 13800138000") is True
    assert has_unspeakable("def f():") is True
    assert has_unspeakable("今天天气真好呀") is False


# ── is_transactional（事务词减分）─────────────────────────────────
def test_is_transactional():
    assert is_transactional("费率是多少呀") is True
    assert is_transactional("退款流程走完啦") is True
    assert is_transactional("refund done") is True
    assert is_transactional("今天好想你呀") is False


def test_transactional_reply_penalized(monkeypatch):
    _emo(monkeypatch, intensity=0.8, dimension="positive")
    assert voice_fitness("想你啦").send_voice is True          # 无事务词 0.63
    # 同等情绪但含事务词 → -0.25 → 0.38 < 0.55 → 文字
    assert voice_fitness("退款已经帮你处理啦").send_voice is False


# ── 真实 analyze_emotion 集成（方向性）────────────────────────────
def test_real_emotion_neutral_filler_is_text():
    assert voice_fitness("嗯嗯好的").send_voice is False


def test_error_path_is_safe(monkeypatch):
    def _boom(_t):
        raise RuntimeError("boom")
    monkeypatch.setattr("src.utils.emotional_context.analyze_emotion", _boom)
    d = voice_fitness("正常文本不含否决项")
    assert d.send_voice is False and d.reason == "error"
