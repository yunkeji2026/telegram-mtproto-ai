"""情绪融合门禁：文字情绪 + 音频声学情绪 → 落库信号（fuse_emotion 纯函数）。"""
import pytest

from src.ai.emotion_fusion import cn_label_dimension, fuse_emotion
from src.ai.speech_emotion import map_audio_emotion


@pytest.mark.parametrize("label,dim", [
    ("低落", "negative"), ("生气", "negative"), ("焦虑", "negative"),
    ("积极", "positive"),
    ("平稳", "neutral"), ("简短", "neutral"), ("", "neutral"),
])
def test_cn_label_dimension(label, dim):
    assert cn_label_dimension(label) == dim


def test_no_audio_passthrough_text():
    out = fuse_emotion(text_label="低落", text_intensity=0.6, audio_emo=None)
    assert out["label"] == "低落"
    assert out["source"] == "text"
    assert out["audio_used"] is False


def test_audio_not_confident_passthrough():
    au = map_audio_emotion("sad", 0.3)   # 低置信 → confident False
    out = fuse_emotion(text_label="积极", text_intensity=0.5, audio_emo=au)
    assert out["label"] == "积极"
    assert out["audio_used"] is False


def test_text_neutral_audio_wins():
    """言不由衷：文字平稳、声音难过 → 采信声学。"""
    au = map_audio_emotion("sad", 0.85)
    out = fuse_emotion(text_label="平稳", text_intensity=-1.0, audio_emo=au)
    assert out["label"] == "低落"
    assert out["dimension"] == "negative"
    assert out["source"] == "audio"
    assert out["audio_used"] is True


def test_same_dimension_keeps_text_boosts_intensity():
    au = map_audio_emotion("sad", 0.9)   # intensity≈0.9
    out = fuse_emotion(text_label="低落", text_intensity=0.5, audio_emo=au)
    assert out["label"] == "低落"          # 文字标签更具体，保留
    assert out["source"] == "fused"
    assert out["intensity"] >= 0.9 - 1e-6  # 取较大


def test_audio_neutral_keeps_text():
    au = map_audio_emotion("neutral", 0.95)
    out = fuse_emotion(text_label="生气", text_intensity=0.7, audio_emo=au)
    assert out["label"] == "生气"
    assert out["audio_used"] is False


def test_conflict_low_score_keeps_text():
    """文字积极、声学负面但分数不够高(<0.7) → 保守保留文字，不翻转。"""
    au = map_audio_emotion("sad", 0.55)   # confident(>=0.5) 但 < 冲突采信阈 0.7
    out = fuse_emotion(text_label="积极", text_intensity=0.6, audio_emo=au,
                       high_conflict_score=0.7)
    assert out["label"] == "积极"
    assert out["audio_used"] is False


def test_conflict_high_score_audio_wins():
    """反讽：文字积极、声学高置信负面(>=0.7) → 采信声学语气。"""
    au = map_audio_emotion("angry", 0.85)
    out = fuse_emotion(text_label="积极", text_intensity=0.6, audio_emo=au,
                       high_conflict_score=0.7)
    assert out["label"] == "生气"
    assert out["dimension"] == "negative"
    assert out["source"] == "audio"
