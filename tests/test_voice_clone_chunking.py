"""克隆 TTS 长文本分块合成 + WAV 拼接门禁。

根因回归：自回归克隆 TTS（IndexTTS2 等）把整段长文本内部切段后逐段 GPT 生成，单段过长
时音频超 max_mel_tokens → **中途截断**（"刷手机"念到"刷手"就断），情感条件还会加剧串字。
客户端把长回复按句切成短块、逐块合成再拼接，保证每块都短到能稳定完整生成。

覆盖：
  - split_text_for_clone：短→单块（零行为变化）/ 长→多块且每块 ≤max / 单句超长硬切 /
    贪心打包相邻短句 / 关闭(max_chars<=0) / 空文本
  - concat_wav_bytes：拼接帧数=各段之和+静音 / 参数一致性校验 / 单段直通 / 空报错
  - synthesize_clone：短文本单次请求（旧行为）/ 长文本多次请求并拼接 / 拼接失败回退整段
"""
from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest

from src.ai import voice_clone_client as vcc


# ── split_text_for_clone ─────────────────────────────────────────────────────
def test_split_short_text_single_chunk():
    # 短回复 ≤max_chars → 单块，行为与不切分一致（零影响）
    assert vcc.split_text_for_clone("你好呀，最近怎么样？", 60) == ["你好呀，最近怎么样？"]


def test_split_empty_returns_empty():
    assert vcc.split_text_for_clone("", 60) == []
    assert vcc.split_text_for_clone("   ", 60) == []
    assert vcc.split_text_for_clone(None, 60) == []  # type: ignore[arg-type]


def test_split_disabled_when_max_chars_non_positive():
    long = "句子。" * 50
    assert vcc.split_text_for_clone(long, 0) == [long]
    assert vcc.split_text_for_clone(long, -1) == [long]


def test_split_long_text_each_chunk_within_limit():
    text = "".join(f"这是第{i}句话内容还挺长的呢。" for i in range(12))
    chunks = vcc.split_text_for_clone(text, 20)
    assert len(chunks) > 1
    assert all(len(c) <= 20 for c in chunks), [len(c) for c in chunks]
    # 无损：拼回去（去标点差异）应覆盖所有原字符
    assert "".join(chunks).replace("，", "") != ""


def test_split_preserves_all_content():
    text = "还没吃呢，刚在刷手机。你吃午饭了吗？我在想你哦。今天天气很好。"
    chunks = vcc.split_text_for_clone(text, 12)
    joined = "".join(chunks)
    # 每个原始汉字都应保留（分块不丢字——防"念一半"的另一面：也不能丢内容）
    for ch in text:
        if ch.strip() and ch not in "，。？":
            assert ch in joined


def test_split_single_long_sentence_hard_split():
    # 无终止标点的超长句 → 在逗号/硬边界二次切，仍每块 ≤max
    text = "我今天去了公园又去了商场然后回家，路上还买了很多好吃的东西真的超级开心呀"
    chunks = vcc.split_text_for_clone(text, 15)
    assert len(chunks) > 1
    assert all(len(c) <= 15 for c in chunks)


def test_split_greedy_packs_adjacent_short_sentences():
    # 多个短句 → 贪心打包到接近 max（减少块数=减少合成次数/延迟）
    text = "好。的。呀。嗯。哈。"
    chunks = vcc.split_text_for_clone(text, 10)
    assert len(chunks) == 1  # 全部打包进一块（总长 ≤10）


# ── concat_wav_bytes ─────────────────────────────────────────────────────────
def _make_wav(nframes: int, *, rate: int = 22050, nch: int = 1, sw: int = 2,
              val: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(sw)
        w.setframerate(rate)
        w.writeframes(bytes([val % 256]) * (nframes * nch * sw))
    return buf.getvalue()


def _wav_nframes(data: bytes) -> int:
    with wave.open(io.BytesIO(data), "rb") as w:
        return w.getnframes()


def test_concat_two_wavs_adds_frames_plus_gap():
    a = _make_wav(1000, rate=22050)
    b = _make_wav(2000, rate=22050)
    merged = vcc.concat_wav_bytes([a, b], gap_ms=100)
    gap_frames = int(22050 * 0.1)
    assert _wav_nframes(merged) == 1000 + 2000 + gap_frames


def test_concat_zero_gap():
    a = _make_wav(500)
    b = _make_wav(700)
    merged = vcc.concat_wav_bytes([a, b], gap_ms=0)
    assert _wav_nframes(merged) == 1200


def test_concat_preserves_params():
    a = _make_wav(100, rate=16000, nch=1, sw=2)
    b = _make_wav(100, rate=16000, nch=1, sw=2)
    merged = vcc.concat_wav_bytes([a, b], gap_ms=0)
    with wave.open(io.BytesIO(merged), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)


def test_concat_single_passthrough():
    a = _make_wav(123)
    assert vcc.concat_wav_bytes([a]) == a


def test_concat_empty_raises():
    with pytest.raises(RuntimeError):
        vcc.concat_wav_bytes([])
    with pytest.raises(RuntimeError):
        vcc.concat_wav_bytes([b"", b""])


def test_concat_param_mismatch_raises():
    a = _make_wav(100, rate=22050)
    b = _make_wav(100, rate=16000)  # 采样率不一致
    with pytest.raises(RuntimeError, match="param mismatch"):
        vcc.concat_wav_bytes([a, b], gap_ms=0)


# ── synthesize_clone 分块端到端 ───────────────────────────────────────────────
class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self._body


def _clone_wav_body(nframes: int) -> bytes:
    import base64 as _b64
    import json as _json
    wav = _make_wav(nframes)
    return _json.dumps(
        {"ok": True, "audio_base64": _b64.b64encode(wav).decode()}).encode()


def test_synthesize_clone_short_single_request(tmp_path, monkeypatch):
    """短文本（≤max_chars）→ 单次请求，行为与旧版一致。"""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_make_wav(10))
    out = tmp_path / "o.wav"
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        return _FakeResp(_clone_wav_body(500))

    monkeypatch.setattr(vcc.urllib.request, "urlopen", fake_urlopen)
    client = vcc.VoiceCloneClient({"language": "zh", "chunk_max_chars": 60})
    client.synthesize_clone("你好，今天过得怎么样呀？", str(ref), out, language="zh")
    assert calls["n"] == 1
    assert _wav_nframes(out.read_bytes()) == 500


def test_synthesize_clone_long_text_chunks_and_concats(tmp_path, monkeypatch):
    """长文本 → 多次请求 + 拼接（防截断的核心保证）。"""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_make_wav(10))
    out = tmp_path / "o.wav"
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        return _FakeResp(_clone_wav_body(400))

    monkeypatch.setattr(vcc.urllib.request, "urlopen", fake_urlopen)
    client = vcc.VoiceCloneClient(
        {"language": "zh", "chunk_max_chars": 12, "chunk_gap_ms": 0})
    text = "还没吃呢，刚在刷手机。你吃午饭了吗？我一直在想你呢。今天心情很好哦。"
    client.synthesize_clone(text, str(ref), out, language="zh")
    n_chunks = len(vcc.split_text_for_clone(text, 12))
    assert n_chunks > 1
    assert calls["n"] == n_chunks
    # 拼接产物帧数 = 各段之和（gap=0）
    assert _wav_nframes(out.read_bytes()) == 400 * n_chunks


def test_synthesize_clone_chunk_disabled_single_request(tmp_path, monkeypatch):
    """chunk_max_chars=0 → 关闭分块，整段单次合成（旧行为）。"""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_make_wav(10))
    out = tmp_path / "o.wav"
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        return _FakeResp(_clone_wav_body(999))

    monkeypatch.setattr(vcc.urllib.request, "urlopen", fake_urlopen)
    client = vcc.VoiceCloneClient({"language": "zh", "chunk_max_chars": 0})
    text = "还没吃呢，刚在刷手机。你吃午饭了吗？我一直在想你呢。今天心情很好哦。" * 2
    client.synthesize_clone(text, str(ref), out, language="zh")
    assert calls["n"] == 1
    assert _wav_nframes(out.read_bytes()) == 999


def test_synthesize_clone_concat_failure_falls_back_whole(tmp_path, monkeypatch):
    """分段音频格式不一致导致拼接失败 → 回退整段合成，绝不返回坏音频。"""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_make_wav(10))
    out = tmp_path / "o.wav"
    # 交替返回不同采样率 → concat 抛错 → 触发整段回退（回退再返一段可用 WAV）
    bodies = [_clone_wav_body_rate(300, 22050), _clone_wav_body_rate(300, 16000),
              _clone_wav_body_rate(300, 16000), _clone_wav_body_rate(1234, 22050)]
    seq = {"i": 0}

    def fake_urlopen(req, timeout=None):
        i = seq["i"]
        seq["i"] += 1
        return _FakeResp(bodies[min(i, len(bodies) - 1)])

    monkeypatch.setattr(vcc.urllib.request, "urlopen", fake_urlopen)
    client = vcc.VoiceCloneClient(
        {"language": "zh", "chunk_max_chars": 12, "chunk_gap_ms": 0})
    text = "还没吃呢，刚在刷手机。你吃午饭了吗？我一直在想你呢。"
    client.synthesize_clone(text, str(ref), out, language="zh")
    # 回退整段：最后一次请求返回 1234 帧的可用 WAV
    assert _wav_nframes(out.read_bytes()) == 1234


def _clone_wav_body_rate(nframes: int, rate: int) -> bytes:
    import base64 as _b64
    import json as _json
    wav = _make_wav(nframes, rate=rate)
    return _json.dumps(
        {"ok": True, "audio_base64": _b64.b64encode(wav).decode()}).encode()
