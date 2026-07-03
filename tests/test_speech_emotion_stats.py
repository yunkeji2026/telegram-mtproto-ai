"""音频情绪识别用量观测门禁（SpeechEmotionStats）。"""
from src.ai.speech_emotion_stats import SpeechEmotionStats, get_speech_emotion_stats


def test_counts_and_distribution():
    s = SpeechEmotionStats()
    s.record(ok=True, emotion="sad", confident=True)
    s.record(ok=True, emotion="sad", confident=True)
    s.record(ok=True, emotion="neutral", confident=False)  # ok 但不置信 → 不计分布
    s.record(ok=False)                                     # 软降级
    d = s.dump()
    assert d["total"] == 4
    assert d["ok"] == 3
    assert d["confident"] == 2
    assert d["unavailable"] == 1
    assert d["by_emotion"] == {"sad": 2}
    assert d["unavailable_rate"] == round(1 / 4, 4)


def test_prom_output_shape():
    s = SpeechEmotionStats()
    s.record(ok=True, emotion="angry", confident=True)
    prom = s.dump_prom()
    assert "speech_emotion_total 1" in prom
    assert "speech_emotion_ok_total 1" in prom
    assert 'speech_emotion_by_emotion_total{emotion="angry"} 1' in prom


def test_reset():
    s = SpeechEmotionStats()
    s.record(ok=True, emotion="happy", confident=True)
    s.reset()
    d = s.dump()
    assert d["total"] == 0 and d["by_emotion"] == {}


def test_singleton_stable():
    assert get_speech_emotion_stats() is get_speech_emotion_stats()


def test_record_never_raises_on_dirty_input():
    s = SpeechEmotionStats()
    s.record(ok=True, emotion=None, confident=True)  # None emotion → 不计分布、不抛
    d = s.dump()
    assert d["total"] == 1
    assert d["by_emotion"] == {}
