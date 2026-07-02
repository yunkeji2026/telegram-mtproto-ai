"""参考音体检纯函数门禁：clean→green / 过短/削波/静音→red / 略短/偏长→yellow / 脏输入安全。

合成确定性信号（无 IO），断言评级与命中的问题标签，防回归。零误报是硬要求——
一段「干净有停顿的人声」绝不能被判红/黄。
"""
import pytest

np = pytest.importorskip("numpy")

from src.ai.voice_ref_health import analyze_reference_audio


def _sig(dur, sr=16000, amp=0.3, noise=0.0, on=0.4, off=0.15, freq=180.0):
    """合成「语音样」信号：正弦音在 on 段发声、off 段停顿（+ 可选底噪）。"""
    n = int(dur * sr)
    t = np.arange(n) / sr
    tone = amp * np.sin(2 * np.pi * freq * t)
    period = on + off
    gate = ((t % period) < on).astype(np.float64)
    x = tone * gate
    if noise > 0:
        x = x + np.random.RandomState(0).normal(0, noise, n)
    return np.clip(x, -1.0, 1.0).astype(np.float32), sr


def _keys_present(h):
    for k in ("grade", "score", "summary", "duration_sec", "clip_ratio",
              "silence_ratio", "peak_dbfs", "noise_floor_dbfs", "issues", "hints"):
        assert k in h, f"missing key {k}"


def test_clean_recording_is_green():
    x, sr = _sig(8.0, amp=0.3, noise=0.0005)
    h = analyze_reference_audio(x, sr)
    _keys_present(h)
    assert h["grade"] == "green", h
    assert 7.0 <= h["duration_sec"] <= 9.0
    assert h["clip_ratio"] < 0.005
    assert h["silence_ratio"] < 0.5
    assert h["score"] >= 85


def test_too_short_is_red():
    x, sr = _sig(2.0, amp=0.3, noise=0.0005)
    h = analyze_reference_audio(x, sr)
    assert h["grade"] == "red"
    assert "录音过短" in h["issues"]
    assert h["hints"]                      # 有可执行建议


def test_slightly_short_is_yellow():
    x, sr = _sig(4.0, amp=0.3, noise=0.0005)
    h = analyze_reference_audio(x, sr)
    assert h["grade"] == "yellow"
    assert "录音略短" in h["issues"]


def test_clipping_is_red():
    # 2x 正弦削顶 → 大量满幅样本
    x, sr = _sig(8.0, amp=2.0, noise=0.0, on=1.0, off=0.0)
    h = analyze_reference_audio(x, sr)
    assert h["clip_ratio"] > 0.02
    assert h["grade"] == "red"
    assert "削波破音" in h["issues"]


def test_mostly_silence_is_red():
    x, sr = _sig(10.0, amp=0.3, noise=0.0, on=0.3, off=2.0)
    h = analyze_reference_audio(x, sr)
    assert h["silence_ratio"] > 0.7
    assert h["grade"] == "red"
    assert "有效人声过少" in h["issues"]


def test_too_long_is_yellow():
    x, sr = _sig(25.0, amp=0.3, noise=0.0005)
    h = analyze_reference_audio(x, sr)
    assert h["grade"] == "yellow"
    assert "录音偏长" in h["issues"]


def test_near_silent_is_red():
    x, sr = _sig(8.0, amp=0.0005, noise=0.0)
    h = analyze_reference_audio(x, sr)
    assert h["grade"] == "red"
    assert "几乎无声" in h["issues"]


def test_empty_is_red_not_raise():
    h = analyze_reference_audio(np.zeros(0, dtype="float32"), 16000)
    assert h["grade"] == "red"
    assert "空音频" in h["issues"]


def test_garbage_inputs_safe():
    # 脏输入（None / 0 采样率 / 怪类型）→ 不抛异常，恒返回带 grade 的 dict
    for s, sr in [(None, 16000), ([1, 2, 3], 0), ("xx", 16000), (np.full(8000, np.nan), 16000)]:
        h = analyze_reference_audio(s, sr)
        assert isinstance(h, dict) and "grade" in h


def test_continuous_clean_speech_not_false_flagged():
    """零误报红线：连续无停顿但干净的人声（无静音、无削波、时长够）→ 必 green，
    绝不能因「没有停顿」被误判噪声/静音。"""
    x, sr = _sig(8.0, amp=0.3, noise=0.0, on=1.0, off=0.0)  # 全程发声、无 gap
    h = analyze_reference_audio(x, sr)
    assert h["grade"] == "green", h
    assert h["silence_ratio"] < 0.1
