"""局域网语音克隆（LAN 优先 → 云端兜底）单测。

覆盖：
  - voice_clone_client 纯函数：请求体构造 / 响应解析 / 健康探测缓存
  - TTSPipeline._should_try_lan 路由判定
  - TTSPipeline._try_lan_clone：在线成功 / 不可用回落 / 失败回落 / 不兜底硬失败
  - resolve_voice_cfg 注入 voice_clone_lan
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from src.ai import voice_clone_client as vcc
from src.ai.tts_pipeline import TTSPipeline, TTSResult


# ── 纯函数 ───────────────────────────────────────────────────────────────────
def test_build_clone_payload_shape():
    raw = vcc.build_clone_payload(
        text="你好", reference_audio_b64="QUJD",
        reference_text="原文", language="ja")
    data = json.loads(raw.decode())
    assert data["text"] == "你好"
    assert data["language"] == "ja"
    assert data["reference_audio_b64"] == "QUJD"
    assert data["reference_text"] == "原文"
    assert data["return_base64"] is True


def test_build_clone_payload_omits_empty_reference_text():
    data = json.loads(vcc.build_clone_payload(
        text="t", reference_audio_b64="QUJD").decode())
    assert "reference_text" not in data


def test_parse_clone_response_ok_false_raises():
    body = json.dumps({"ok": False, "error": "no speech in ref"}).encode()
    with pytest.raises(RuntimeError, match="no speech"):
        vcc.parse_clone_response(body)


def test_parse_clone_response_json_audio_base64():
    audio = b"\x00\x01\x02hello"
    body = json.dumps({"audio_base64": base64.b64encode(audio).decode()}).encode()
    assert vcc.parse_clone_response(body) == audio


def test_parse_clone_response_json_audio_key():
    audio = b"abcd"
    body = json.dumps({"audio": base64.b64encode(audio).decode()}).encode()
    assert vcc.parse_clone_response(body) == audio


def test_parse_clone_response_raw_bytes():
    raw = b"\xff\xf3rawmp3bytes"
    assert vcc.parse_clone_response(raw) == raw


def test_parse_clone_response_empty_raises():
    with pytest.raises(RuntimeError):
        vcc.parse_clone_response(b"")


def test_parse_clone_response_json_no_audio_raises():
    with pytest.raises(RuntimeError):
        vcc.parse_clone_response(json.dumps({"oops": 1}).encode())


# ── effective_clone_language 纯函数（防「中文声纹念英文」）─────────────────────
def test_effective_clone_language_chinese_keeps_default():
    # 中文回复：判 zh，与旧行为一致（不改）
    assert vcc.effective_clone_language("你好，今天过得怎么样？", "zh") == "zh"


def test_effective_clone_language_english_corrects_from_zh():
    # 英文回复：由 zh 纠正为 en（严格改善，防中文音系念英文 garble）
    assert vcc.effective_clone_language("Hey, how are you doing today?", "zh") == "en"


def test_effective_clone_language_empty_returns_default():
    assert vcc.effective_clone_language("", "zh") == "zh"
    assert vcc.effective_clone_language("   ", "en") == "en"


def test_effective_clone_language_blank_default_falls_back_zh():
    assert vcc.effective_clone_language("", "") == "zh"


def test_effective_clone_language_detect_failure_returns_default(monkeypatch):
    # detect_language 抛异常 → 回落 default，绝不抛（best-effort 不阻断合成）
    import src.ai.translation_service as ts
    monkeypatch.setattr(
        ts, "detect_language",
        lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
    assert vcc.effective_clone_language("whatever text here", "zh") == "zh"


def test_effective_clone_language_unknown_returns_default(monkeypatch):
    import src.ai.translation_service as ts
    monkeypatch.setattr(ts, "detect_language", lambda t: "unknown")
    assert vcc.effective_clone_language("....", "zh") == "zh"


# ── synthesize_clone：合成语言按文本纠正（端到端 payload 断言）──────────────────
class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self._body


def _clone_ok_body() -> bytes:
    return json.dumps(
        {"ok": True, "audio_base64": base64.b64encode(b"WAVdata").decode()}).encode()


def _capture_clone_language(monkeypatch, client, text, ref, out, **kw) -> str:
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data
        return _FakeResp(_clone_ok_body())

    monkeypatch.setattr(vcc.urllib.request, "urlopen", fake_urlopen)
    client.synthesize_clone(text, str(ref), out, **kw)
    return json.loads(captured["data"].decode())["language"]


def test_synthesize_clone_corrects_language_from_text(tmp_path, monkeypatch):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFFxxxx")
    out = tmp_path / "o.wav"
    client = vcc.VoiceCloneClient({"language": "zh"})  # 默认 auto_language=True
    lang = _capture_clone_language(
        monkeypatch, client, "Hello, how are you today?", ref, out)
    assert lang == "en"  # zh → en 纠正
    assert out.read_bytes() == b"WAVdata"


def test_synthesize_clone_keeps_chinese(tmp_path, monkeypatch):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFFxxxx")
    out = tmp_path / "o.wav"
    client = vcc.VoiceCloneClient({"language": "zh"})
    lang = _capture_clone_language(
        monkeypatch, client, "你好，最近怎么样呀？", ref, out)
    assert lang == "zh"  # 中文回复行为不变


def test_synthesize_clone_explicit_language_wins(tmp_path, monkeypatch):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFFxxxx")
    out = tmp_path / "o.wav"
    client = vcc.VoiceCloneClient({"language": "zh"})
    lang = _capture_clone_language(
        monkeypatch, client, "Hello there", ref, out, language="ja")
    assert lang == "ja"  # 显式指定=最高优先，不走自动检测


def test_synthesize_clone_auto_language_off_keeps_config(tmp_path, monkeypatch):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFFxxxx")
    out = tmp_path / "o.wav"
    client = vcc.VoiceCloneClient({"language": "zh", "auto_language": False})
    lang = _capture_clone_language(
        monkeypatch, client, "Hello, how are you today?", ref, out)
    assert lang == "zh"  # opt-out → 退回旧行为（固定 config 语言）


def test_health_ok_uses_cache(monkeypatch):
    vcc.reset_health_cache()
    calls = {"n": 0}

    def fake_probe(self):
        calls["n"] += 1
        return True

    monkeypatch.setattr(vcc.VoiceCloneClient, "_probe_health", fake_probe)
    client = vcc.VoiceCloneClient({"base_url": "http://lan:8000", "health_cache_sec": 60})
    assert client.health_ok() is True
    assert client.health_ok() is True  # 命中缓存
    assert calls["n"] == 1
    assert client.health_ok(use_cache=False) is True  # 强制重探
    assert calls["n"] == 2


def test_health_ok_cache_expires(monkeypatch):
    vcc.reset_health_cache()
    calls = {"n": 0}
    monkeypatch.setattr(
        vcc.VoiceCloneClient, "_probe_health",
        lambda self: (calls.__setitem__("n", calls["n"] + 1) or True))
    client = vcc.VoiceCloneClient({"base_url": "http://lan:8000", "health_cache_sec": 0.05})
    client.health_ok()
    time.sleep(0.12)
    client.health_ok()
    assert calls["n"] == 2  # 缓存过期 → 重探


# ── probe_health_detail：区分「可达但未载入」vs「彻底不可达」──────────────────
def _health_resp(body: dict):
    return lambda req, timeout=None: _FakeResp(json.dumps(body).encode())


def test_probe_health_detail_reachable_not_loaded(monkeypatch):
    # MiniCPM-o supervisor 常驻但 worker 未起：reachable=True, model_loaded=False
    monkeypatch.setattr(vcc.urllib.request, "urlopen", _health_resp(
        {"supervisor": True, "worker_running": False,
         "model_loaded": False, "loading": False}))
    d = vcc.VoiceCloneClient({"base_url": "http://mc:7860"}).probe_health_detail()
    assert d == {"reachable": True, "model_loaded": False, "loading": False}


def test_probe_health_detail_loaded(monkeypatch):
    monkeypatch.setattr(vcc.urllib.request, "urlopen", _health_resp(
        {"model_loaded": True, "loading": False}))
    d = vcc.VoiceCloneClient({"base_url": "http://mc:7860"}).probe_health_detail()
    assert d["reachable"] is True and d["model_loaded"] is True


def test_probe_health_detail_loading(monkeypatch):
    monkeypatch.setattr(vcc.urllib.request, "urlopen", _health_resp(
        {"model_loaded": False, "loading": True}))
    d = vcc.VoiceCloneClient({"base_url": "http://mc:7860"}).probe_health_detail()
    assert d["reachable"] is True and d["loading"] is True


def test_probe_health_detail_no_model_field_is_none(monkeypatch):
    # 老 fish 常驻主机无 model_loaded 字段 → None（视为已就绪，health_ok 返 True）
    monkeypatch.setattr(vcc.urllib.request, "urlopen", _health_resp({"status": "ok"}))
    d = vcc.VoiceCloneClient({"base_url": "http://mc:7860"}).probe_health_detail()
    assert d["reachable"] is True and d["model_loaded"] is None


def test_probe_health_detail_unreachable(monkeypatch):
    def boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(vcc.urllib.request, "urlopen", boom)
    d = vcc.VoiceCloneClient({"base_url": "http://mc:7860"}).probe_health_detail()
    assert d["reachable"] is False


# ── 按需载入自愈：_do_model_load / request_model_load_async ─────────────────────
def test_do_model_load_refreshes_health_cache(monkeypatch):
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.urllib.request, "urlopen", _health_resp({"model_loaded": True}))
    c = vcc.VoiceCloneClient({"base_url": "http://mc:7860", "health_cache_sec": 60})
    assert c._do_model_load() is True
    # 载入成功即把健康缓存刷成 True → 下一条消息命中缓存直接走克隆（不必等缓存过期）
    assert c.health_ok() is True


def test_do_model_load_not_ready_returns_false(monkeypatch):
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.urllib.request, "urlopen", _health_resp({"model_loaded": False}))
    c = vcc.VoiceCloneClient({"base_url": "http://mc:7860"})
    assert c._do_model_load() is False


def test_do_model_load_failure_returns_false(monkeypatch):
    vcc.reset_health_cache()

    def boom(req, timeout=None):
        raise OSError("host down")

    monkeypatch.setattr(vcc.urllib.request, "urlopen", boom)
    c = vcc.VoiceCloneClient({"base_url": "http://mc:7860"})
    assert c._do_model_load() is False  # best-effort，绝不抛


def test_request_model_load_async_cooldown(monkeypatch):
    import threading

    vcc.reset_load_state()
    calls = {"n": 0}
    done = threading.Event()

    def fake_do(self):
        calls["n"] += 1
        done.set()
        return True

    monkeypatch.setattr(vcc.VoiceCloneClient, "_do_model_load", fake_do)
    c = vcc.VoiceCloneClient({"base_url": "http://mc:7860", "load_cooldown_sec": 60})
    assert c.request_model_load_async() is True    # 首次触发
    assert done.wait(2.0)                           # 后台线程已执行 _do_model_load
    assert c.request_model_load_async() is False    # 冷却期内不再重复触发
    assert calls["n"] == 1


# ── TTSPipeline 路由 ─────────────────────────────────────────────────────────
def _ref_audio(tmp_path: Path) -> Path:
    p = tmp_path / "ref.wav"
    p.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    return p


def _pipeline(tmp_path: Path, *, lan_enabled=True, fallback=True, with_ref=True):
    ref = _ref_audio(tmp_path)
    return TTSPipeline({
        "enabled": True,
        "backend": "edge_tts",
        "format": "mp3",
        "out_dir": str(tmp_path / "out"),
        "voice_profile": {
            "enabled": True,
            "owner_consent": True,
            "reference_audio_path": str(ref) if with_ref else "",
        },
        "voice_clone_lan": {
            "enabled": lan_enabled,
            "base_url": "http://lan:8000",
            "cloud_fallback": fallback,
        },
    })


def test_should_try_lan_true(tmp_path):
    assert _pipeline(tmp_path)._should_try_lan() is True


def test_should_try_lan_false_when_lan_disabled(tmp_path):
    assert _pipeline(tmp_path, lan_enabled=False)._should_try_lan() is False


def test_should_try_lan_false_without_reference(tmp_path):
    assert _pipeline(tmp_path, with_ref=False)._should_try_lan() is False


async def test_synthesize_lan_success(tmp_path, monkeypatch):
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: True)

    def fake_clone(self, text, ref, out, *, reference_text="", instructions=""):
        Path(out).write_bytes(b"RIFF\x00\x00\x00\x00WAVEcloned")

    monkeypatch.setattr(vcc.VoiceCloneClient, "synthesize_clone", fake_clone)
    p = _pipeline(tmp_path)
    rv = await p.synthesize("你好世界")
    assert rv.ok is True
    assert rv.provider == "voice_clone_lan"
    assert rv.format == "wav"
    assert Path(rv.audio_path).is_file()
    assert rv.audio_path.endswith(".wav")
    assert rv.extra.get("lan_base_url") == "http://lan:8000"


async def test_lan_inline_emotion_tag_opt_in(tmp_path, monkeypatch):
    """开 voice_clone_lan.emotion_inline_tags → fish 文本前注入情感标记。"""
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: True)
    seen = {}

    def fake_clone(self, text, ref, out, *, reference_text="", instructions=""):
        seen["text"] = text
        Path(out).write_bytes(b"RIFF\x00\x00\x00\x00WAVEcloned")

    monkeypatch.setattr(vcc.VoiceCloneClient, "synthesize_clone", fake_clone)
    p = _pipeline(tmp_path)
    p.cache_enabled = False  # 防 TTS 缓存跨用例命中掩盖真实合成
    p.voice_clone_lan["emotion_inline_tags"] = True
    rv = await p.synthesize("你好世界", emotion="happy")
    assert rv.ok is True
    assert seen["text"] == "(joyful) 你好世界"


async def test_lan_no_inline_tag_when_flag_off(tmp_path, monkeypatch):
    """默认（flag 关）→ 发原文，不注入标记（防 server 不识别时读出括号）。"""
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: True)
    seen = {}

    def fake_clone(self, text, ref, out, *, reference_text="", instructions=""):
        seen["text"] = text
        Path(out).write_bytes(b"RIFF\x00\x00\x00\x00WAVEcloned")

    monkeypatch.setattr(vcc.VoiceCloneClient, "synthesize_clone", fake_clone)
    p = _pipeline(tmp_path)
    p.cache_enabled = False  # 防 TTS 缓存跨用例命中掩盖真实合成
    rv = await p.synthesize("你好世界", emotion="happy")
    assert rv.ok is True
    assert seen["text"] == "你好世界"


async def test_lan_clone_passes_emotion_instructions(tmp_path, monkeypatch):
    """情感非中性 → 结构化 instructions 下发主机（默认开，不会被读出，零 garble）。"""
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: True)
    seen = {}

    def fake_clone(self, text, ref, out, *, reference_text="", instructions=""):
        seen["text"] = text
        seen["instructions"] = instructions
        Path(out).write_bytes(b"RIFF\x00\x00\x00\x00WAVEcloned")

    monkeypatch.setattr(vcc.VoiceCloneClient, "synthesize_clone", fake_clone)
    p = _pipeline(tmp_path)
    p.cache_enabled = False
    rv = await p.synthesize("你好世界", emotion="happy")
    assert rv.ok is True
    assert seen["instructions"] and "语气" in seen["instructions"]
    assert seen["text"] == "你好世界"   # 未开 inline tags → 文本不动


async def test_lan_clone_neutral_no_instructions(tmp_path, monkeypatch):
    """neutral → 不下发 instructions（与升级前一致）。"""
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: True)
    seen = {}

    def fake_clone(self, text, ref, out, *, reference_text="", instructions=""):
        seen["instructions"] = instructions
        Path(out).write_bytes(b"RIFF\x00\x00\x00\x00WAVEcloned")

    monkeypatch.setattr(vcc.VoiceCloneClient, "synthesize_clone", fake_clone)
    p = _pipeline(tmp_path)
    p.cache_enabled = False
    rv = await p.synthesize("你好世界", emotion="neutral")
    assert rv.ok is True
    assert seen["instructions"] == ""


async def test_lan_unreachable_returns_none_for_fallback(tmp_path, monkeypatch):
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: False)
    p = _pipeline(tmp_path, fallback=True)
    out = tmp_path / "out.mp3"
    rv = TTSResult(text="hi", format="mp3")
    res = await p._try_lan_clone(rv, out, time.monotonic())
    assert res is None  # → 回落云端


async def test_lan_unreachable_hard_fail_without_fallback(tmp_path, monkeypatch):
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: False)
    p = _pipeline(tmp_path, fallback=False)
    out = tmp_path / "out.mp3"
    rv = TTSResult(text="hi", format="mp3")
    res = await p._try_lan_clone(rv, out, time.monotonic())
    assert res is rv
    assert res.error == "voice_clone_lan_unreachable"


async def test_lan_synth_failure_falls_back(tmp_path, monkeypatch):
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: True)

    def boom(self, text, ref, out, *, reference_text=""):
        raise RuntimeError("server 500")

    monkeypatch.setattr(vcc.VoiceCloneClient, "synthesize_clone", boom)
    p = _pipeline(tmp_path, fallback=True)
    out = tmp_path / "out.mp3"
    rv = TTSResult(text="hi", format="mp3")
    res = await p._try_lan_clone(rv, out, time.monotonic())
    assert res is None  # 失败但允许兜底


async def test_lan_synth_failure_no_fallback_errors(tmp_path, monkeypatch):
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: True)
    monkeypatch.setattr(
        vcc.VoiceCloneClient, "synthesize_clone",
        lambda self, text, ref, out, **kw: (_ for _ in ()).throw(RuntimeError("x")))
    p = _pipeline(tmp_path, fallback=False)
    out = tmp_path / "out.mp3"
    rv = TTSResult(text="hi", format="mp3")
    res = await p._try_lan_clone(rv, out, time.monotonic())
    assert res is rv
    assert res.error.startswith("voice_clone_lan_failed")


# ── resolve_voice_cfg 注入 ───────────────────────────────────────────────────
def test_resolve_voice_cfg_injects_lan():
    from src.ai.persona_voice import resolve_voice_cfg
    full = {
        "telegram": {"voice_reply": {"backend": "edge_tts"}},
        "voice_clone_lan": {"enabled": True, "base_url": "http://lan:8000"},
    }
    cfg = resolve_voice_cfg(None, full)
    assert cfg.get("voice_clone_lan", {}).get("enabled") is True
    assert cfg["voice_clone_lan"]["base_url"] == "http://lan:8000"
