"""LAN GPU 音频服务（asr176）健康灯：探测决策 + /health 解析 + 健康组件语义。

背景：176 的 ASR/SER 服务此前只有远端看门狗自愈，主站看板对它是盲区
（挂了→链路静默降级 CPU，无人知道）。现 collect_health 周期探测其 /health
（60s TTL 缓存、3s 超时、仅私网自建服务才探），亮进运行时健康灯。
"""
from __future__ import annotations

import json

import src.inbox.health_watchdog as hw
from src.inbox.health_watchdog import audio_probe_target, probe_audio_service
from src.utils.health import build_health


def _cfg(enabled=True, provider="openai_compatible",
         base_url="http://192.168.0.176:8765/v1"):
    return {"voice_recognition": {
        "enabled": enabled, "provider": provider, "base_url": base_url}}


# ---------- audio_probe_target：探测决策（纯函数） ----------

def test_target_happy_path_strips_v1():
    assert audio_probe_target(_cfg()) == "http://192.168.0.176:8765/health"


def test_target_disabled_or_missing():
    assert audio_probe_target(_cfg(enabled=False)) == ""
    assert audio_probe_target({}) == ""
    assert audio_probe_target(_cfg(base_url="")) == ""


def test_target_non_openai_provider_skipped():
    assert audio_probe_target(_cfg(provider="faster_whisper")) == ""


def test_target_public_host_skipped():
    """公网云 ASR 没有我们的 /health 契约，不探（防误报 warn）。"""
    assert audio_probe_target(_cfg(base_url="https://api.openai.com/v1")) == ""


def test_target_private_ranges():
    assert audio_probe_target(_cfg(base_url="http://10.1.2.3:8765/v1"))
    assert audio_probe_target(_cfg(base_url="http://172.16.0.9:8765"))
    assert audio_probe_target(_cfg(base_url="http://127.0.0.1:8765/v1"))
    assert audio_probe_target(_cfg(base_url="http://172.15.0.9:8765")) == ""  # 非私网 172 段


# ---------- probe_audio_service：/health 解析 + TTL 缓存 ----------

class _FakeResp:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _reset_cache():
    hw._AUDIO_PROBE_CACHE.update({"ts": 0.0, "url": "", "result": None})


def test_probe_parses_health_fields(monkeypatch):
    _reset_cache()
    calls = {"n": 0}

    def _fake_urlopen(url, timeout=0):
        calls["n"] += 1
        return _FakeResp({"status": "ok", "model": "large-v3-turbo", "device": "cuda",
                          "ser_model": "iic/emotion2vec_plus_large",
                          "asr_loaded": True, "ser_loaded": True})

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    out = probe_audio_service(_cfg())
    assert out["reachable"] is True
    assert out["asr_loaded"] and out["ser_loaded"] and out["ser_expected"]
    assert out["device"] == "cuda"
    # TTL 缓存：紧接着再探不打网络
    out2 = probe_audio_service(_cfg())
    assert out2 is out and calls["n"] == 1
    # force 绕过缓存
    probe_audio_service(_cfg(), force=True)
    assert calls["n"] == 2
    _reset_cache()


def test_probe_unreachable_is_soft(monkeypatch):
    _reset_cache()

    def _boom(url, timeout=0):
        raise OSError("connect timeout")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    out = probe_audio_service(_cfg())
    assert out["reachable"] is False and "connect" in out["error"]
    _reset_cache()


def test_probe_none_when_not_configured():
    _reset_cache()
    assert probe_audio_service({}) is None


def test_probe_ser_off_service(monkeypatch):
    """服务端 SER 关闭（ser_model 空）→ ser_expected False，不因 ser_loaded=False 扣分。"""
    _reset_cache()
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: _FakeResp(
        {"status": "ok", "ser_model": "", "asr_loaded": True, "ser_loaded": False}))
    out = probe_audio_service(_cfg())
    assert out["reachable"] and out["asr_loaded"] and not out["ser_expected"]
    _reset_cache()


# ---------- build_health：audio 组件语义 ----------

def _audio_comp(health):
    return next((c for c in health["components"] if c["id"] == "audio"), None)


def _base_kw():
    return dict(db_ok=True, ai_provider="openai_compatible", ai_key_ok=True)


def test_health_no_audio_service_no_component():
    h = build_health(**_base_kw())
    assert _audio_comp(h) is None


def test_health_audio_ok():
    h = build_health(**_base_kw(), audio_service={
        "url": "http://x/health", "reachable": True, "latency_ms": 12,
        "asr_loaded": True, "ser_loaded": True, "ser_expected": True})
    c = _audio_comp(h)
    assert c["status"] == "ok" and "12ms" in c["detail"]


def test_health_audio_unreachable_warns_yellow_not_red():
    h = build_health(**_base_kw(), audio_service={
        "url": "http://x/health", "reachable": False, "error": "timeout"})
    c = _audio_comp(h)
    assert c["status"] == "warn" and "降级" in c["detail"]
    assert h["light"] == "yellow"          # 软性降级：黄灯不红灯


def test_health_audio_warming_up_warns():
    h = build_health(**_base_kw(), audio_service={
        "url": "http://x/health", "reachable": True,
        "asr_loaded": False, "ser_loaded": False, "ser_expected": True})
    assert _audio_comp(h)["status"] == "warn"


def test_health_audio_ser_disabled_still_ok():
    h = build_health(**_base_kw(), audio_service={
        "url": "http://x/health", "reachable": True,
        "asr_loaded": True, "ser_loaded": False, "ser_expected": False})
    assert _audio_comp(h)["status"] == "ok"
