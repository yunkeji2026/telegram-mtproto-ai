"""语音引擎「显存按需」开关：主机 load/unload 端点 + 未载入护栏 + 配置解析。

与别的 AI 服务共用同一张卡时，用此开关在「用时载入 / 闲时释放」之间切换显存占用。
纯逻辑/契约层：真引擎只在 lazy 构造（不导入 torch、无需 GPU），故可常驻 CI。
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from src.ai.realtime_voice import RealtimeVoiceConfig
from tools.minicpm_o_server import (
    EngineNotLoaded,
    MiniCPMOEngine,
    MockEngine,
    WorkerSupervisor,
    build_app,
    build_supervisor_app,
)


# ── 主机：load/unload 端点翻转 model_loaded（mock，无 GPU）─────────────────────
def test_mock_host_load_unload_toggles_status():
    c = TestClient(build_app(MockEngine()))
    assert c.get("/health").json()["model_loaded"] is True
    assert c.post("/v1/model/unload").json()["model_loaded"] is False
    assert c.get("/v1/model/status").json()["model_loaded"] is False
    assert c.post("/v1/model/load").json()["model_loaded"] is True


# ── 主机：未载入时推理被拒（真引擎 lazy，不触发 torch / 不需 GPU）──────────────
def test_minicpmo_requires_loaded_before_inference():
    e = MiniCPMOEngine("does-not-matter", device="cpu", lazy=True)
    assert e.model_loaded is False
    with pytest.raises(EngineNotLoaded):
        e.synth_clone("你好", b"")
    with pytest.raises(EngineNotLoaded):
        e.new_session({"sample_rate": 16000})


def test_minicpmo_clone_endpoint_returns_409_when_unloaded():
    c = TestClient(build_app(MiniCPMOEngine("x", device="cpu", lazy=True)))
    assert c.get("/health").json()["model_loaded"] is False
    ref = base64.b64encode(b"RIFF0000WAVEdata").decode()
    r = c.post("/v1/tts/clone", json={"text": "hi", "reference_audio_b64": ref})
    assert r.status_code == 409 and r.json()["error"] == "model_not_loaded"


# ── 看守模式：worker 未起时状态 / 克隆护栏（不 spawn 真子进程，纯逻辑）────────────
def test_supervisor_status_and_guards_when_worker_down():
    sup = WorkerSupervisor(["noop"], "http://127.0.0.1:59997")   # 永不 spawn → is_running False
    c = TestClient(build_supervisor_app(sup))
    h = c.get("/health").json()
    assert h["supervisor"] is True
    assert h["worker_running"] is False and h["model_loaded"] is False
    assert c.get("/v1/model/status").json()["model_loaded"] is False
    ref = base64.b64encode(b"RIFF0000WAVEdata").decode()
    r = c.post("/v1/tts/clone", json={"text": "hi", "reference_audio_b64": ref})
    assert r.status_code == 409 and r.json()["error"] == "model_not_loaded"


def test_supervisor_worker_ws_url_scheme():
    sup = WorkerSupervisor(["noop"], "http://127.0.0.1:7861")
    assert sup.worker_ws_url() == "ws://127.0.0.1:7861/v1/realtime"


# ── 配置：load_path / unload_path 默认 + 覆盖 ────────────────────────────────
def test_config_engine_paths_defaults_and_override():
    d = RealtimeVoiceConfig.from_config(None)
    assert d.load_path == "/v1/model/load"
    assert d.unload_path == "/v1/model/unload"
    o = RealtimeVoiceConfig.from_config(
        {"realtime_voice": {"load_path": "/x/load", "unload_path": "/x/unload"}})
    assert o.load_path == "/x/load" and o.unload_path == "/x/unload"
