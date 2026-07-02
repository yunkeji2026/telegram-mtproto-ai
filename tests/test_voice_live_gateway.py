"""实时语音通话网关：装配纯函数 + WS 握手降级路径（契约）。"""
from __future__ import annotations

import json
import types

import pytest

from src.web.routes.voice_live_routes import (
    build_call_init,
    parse_client_event,
    parse_client_hello,
)


# ── build_call_init（纯装配）──────────────────────────────────────────────────
def test_build_call_init_with_reference_audio_clones_voice(tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF\x00\x00\x00\x00WAVEdata")
    voice_ctx = {"voice_cfg": {"backend": "voice_clone_lan", "voice": "ignored",
                               "voice_profile": {"reference_audio_path": str(ref)}}}
    init = build_call_init(
        voice_ctx=voice_ctx,
        persona={"name": "林小雨", "role": "温柔的陪伴者"},
        memory_bullets_text="对方喜欢夜跑\n养了只布偶猫",
        customer_language="zh",
        cfg={"realtime_voice": {"enabled": True, "sample_rate": 16000}})
    assert init["type"] == "session.init"
    assert init.get("voice_ref_b64")          # 有参考音 → 克隆
    assert "voice" not in init                 # 不再带内置音色名
    assert init["language"] == "zh"
    assert "林小雨" in init["system_prompt"]
    assert "夜跑" in init["system_prompt"] and "布偶猫" in init["system_prompt"]


def test_build_call_init_without_reference_uses_builtin_voice():
    voice_ctx = {"voice_cfg": {"backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural",
                               "voice_profile": {}}}
    init = build_call_init(
        voice_ctx=voice_ctx, persona={"name": "Aria"},
        memory_bullets_text="", customer_language="en",
        cfg={"realtime_voice": {"default_voice": "fallback_a"}})
    assert "voice_ref_b64" not in init
    assert init["voice"] == "zh-CN-XiaoxiaoNeural"   # voice_cfg.voice 优先
    assert init["language"] == "en"


def test_build_call_init_empty_context_safe():
    init = build_call_init(voice_ctx={}, persona=None, cfg={})
    assert init["type"] == "session.init"
    assert init["system_prompt"].strip()             # 仍给守则
    assert init["language"] == "zh"


# ── 容错解析 ─────────────────────────────────────────────────────────────────
def test_parse_client_hello_tolerant():
    h = parse_client_hello(json.dumps({"token": "t", "persona_id": "p", "language": "zh"}))
    assert h == {"token": "t", "persona_id": "p", "chat_key": "", "memory_key": "", "language": "zh"}
    assert parse_client_hello("not json")["persona_id"] == ""
    assert parse_client_hello(123)["token"] == ""


def test_parse_client_event_tolerant():
    assert parse_client_event(json.dumps({"type": "input_audio", "audio_b64": "x"}))["type"] == "input_audio"
    assert parse_client_event("bad") == {}
    assert parse_client_event(json.dumps([1, 2])) == {}


def test_count_memory_bullets():
    from src.web.routes.voice_live_routes import count_memory_bullets
    assert count_memory_bullets("") == 0
    assert count_memory_bullets(None) == 0                     # 容 None
    assert count_memory_bullets("• a\n• b\n\n  \n• c") == 3    # 忽略空/纯空白行


def test_memory_bullets_list():
    from src.web.routes.voice_live_routes import memory_bullets
    assert memory_bullets("") == []
    assert memory_bullets(None) == []
    assert memory_bullets("• a\n\n  \n  • b ") == ["• a", "• b"]   # strip + 去空行


def test_discover_reference_audio(tmp_path):
    from src.web.routes.voice_live_routes import discover_reference_audio
    # 空 id / 无文件 → ""（降级到内置音色）
    assert discover_reference_audio("", tmp_path) == ""
    assert discover_reference_audio("zhang_jingguang", tmp_path) == ""
    # 命名约定命中 → 返回该路径
    f = tmp_path / "zhang_jingguang.wav"
    f.write_bytes(b"RIFFxxxxWAVE")
    assert discover_reference_audio("zhang_jingguang", tmp_path) == str(f)
    # 空文件不算（防 0 字节占位被当成有效参考音）
    e = tmp_path / "lin_xiaoyu.mp3"
    e.write_bytes(b"")
    assert discover_reference_audio("lin_xiaoyu", tmp_path) == ""
    # 多扩展名按优先级（wav 先于 mp3）
    (tmp_path / "chen_meiling.mp3").write_bytes(b"id3xxxx")
    (tmp_path / "chen_meiling.wav").write_bytes(b"RIFFxxxxWAVE")
    assert discover_reference_audio("chen_meiling", tmp_path).endswith("chen_meiling.wav")


def test_normalize_reference_audio_to_16k_mono(tmp_path):
    """用户常给 48k 立体声 → 必须归一到 16k 单声道，否则克隆主机合不出声。"""
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    pytest.importorskip("soxr")
    import base64
    import io
    import wave

    from src.web.routes.voice_live_routes import _b64_ref_audio
    sr = 48000
    t = np.linspace(0, 0.5, int(sr * 0.5), endpoint=False, dtype="float32")
    tone = (0.3 * np.sin(2 * np.pi * 220 * t)).astype("float32")
    stereo = np.stack([tone, tone], axis=1)        # 48k 立体声
    f = tmp_path / "p.wav"
    sf.write(str(f), stereo, sr, subtype="PCM_16")
    raw = base64.b64decode(_b64_ref_audio(str(f)))
    with wave.open(io.BytesIO(raw), "rb") as w:
        assert w.getnchannels() == 1               # 折叠单声道
        assert w.getframerate() == 16000           # 重采样 16k
        assert w.getsampwidth() == 2               # 16-bit PCM
        assert w.getnframes() > 0


def test_persona_voice_upload_delete(tmp_path, monkeypatch):
    """上传真人声 → 归一化落盘 16k 单声道 → has_reference 翻真；删除 → 回落默认音。"""
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    pytest.importorskip("soxr")
    import io
    import wave

    from fastapi.testclient import TestClient
    import src.web.routes.voice_live_routes as V
    monkeypatch.setattr(V, "_REF_AUDIO_DIR", tmp_path)
    client = TestClient(_app(True))

    sr = 48000      # 模拟用户常见的 48k 立体声录音
    t = np.linspace(0, 0.6, int(sr * 0.6), endpoint=False, dtype="float32")
    stereo = np.stack([(0.3 * np.sin(2 * np.pi * 200 * t)).astype("float32")] * 2, axis=1)
    buf = io.BytesIO()
    sf.write(buf, stereo, sr, format="WAV", subtype="PCM_16")
    wav_bytes = buf.getvalue()

    r = client.post("/api/voice/persona-voice?persona_id=test_x", content=wav_bytes)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and j["has_reference"] and j["persona_id"] == "test_x"
    # 体检随上传返回：0.6s 录音 → 红灯「录音过短」，带可执行建议
    assert isinstance(j.get("health"), dict)
    assert j["health"]["grade"] == "red" and "录音过短" in j["health"]["issues"]
    assert j["health"]["hints"]
    saved = tmp_path / "test_x.wav"
    assert saved.is_file()
    with wave.open(str(saved), "rb") as w:               # 落盘即归一化
        assert w.getnchannels() == 1 and w.getframerate() == 16000
    assert V.discover_reference_audio("test_x", tmp_path) == str(saved)
    meta = V.reference_audio_meta("test_x", tmp_path)
    assert meta["has_reference"] is True
    assert isinstance(meta.get("health"), dict) and meta["health"]["grade"] == "red"

    # 路径穿越 id → 400；垃圾字节 → decode_failed；空 body → 400
    assert client.post("/api/voice/persona-voice?persona_id=../evil",
                       content=wav_bytes).status_code == 400
    rb = client.post("/api/voice/persona-voice?persona_id=test_y", content=b"not audio")
    assert rb.status_code == 400 and rb.json()["error"] == "decode_failed"
    assert client.post("/api/voice/persona-voice?persona_id=test_z",
                       content=b"").status_code == 400

    rd = client.delete("/api/voice/persona-voice?persona_id=test_x")
    assert rd.status_code == 200 and rd.json()["ok"] and not saved.exists()


def test_preview_uses_clone_when_engine_loaded(monkeypatch):
    """引擎已载 + 人设有参考音 → 试听走 MiniCPM 克隆（engine=clone），与真实通话同源，
    且把人设情绪作 instructions（系统侧语气、绝不读出）一并带上，回显在响应里。"""
    from fastapi.testclient import TestClient
    import src.web.routes.voice_live_routes as V
    captured = {}
    monkeypatch.setattr(V, "discover_reference_audio", lambda *a, **k: "fake.wav")
    monkeypatch.setattr(V, "_b64_ref_audio", lambda *a, **k: "QUJD")
    monkeypatch.setattr(V.RealtimeVoiceClient, "model_status", lambda self: {"model_loaded": True})

    def _fake_clone(self, text, ref_b64, **k):
        captured["text"] = text
        captured["instructions"] = k.get("instructions")
        return b"RIFFclonewav"

    monkeypatch.setattr(V.RealtimeVoiceClient, "clone_oneshot", _fake_clone)
    r = TestClient(_app(True)).post("/api/voice/preview",
                                    json={"persona_id": "lin_xiaoyu", "text": "你好呀"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and j["engine"] == "clone" and j["format"] == "wav"
    # 情绪打通：克隆调用收到非空 instructions（至少 warm 基线），响应也回显该语气
    assert isinstance(captured.get("instructions"), str) and captured["instructions"]
    assert isinstance(j.get("instructions"), str) and j["instructions"]


def test_build_clone_instructions_from_persona_baseline():
    """无显式情绪时，按人设基线（voice_profile.emotion）现派生语气——试听"对语气"。"""
    import src.web.routes.voice_live_routes as V
    vc = {"emotion": None, "persona": {"voice_profile": {"emotion": "playful"}}}
    instr = V.build_clone_instructions(vc, "你好呀")
    assert isinstance(instr, str) and instr
    assert ("俏皮" in instr) or ("活泼" in instr)


def test_build_clone_instructions_prefers_explicit_emotion():
    """voice_ctx 已带派生情绪（会话情绪开关开）→ 直接用它，不被人设基线覆盖。"""
    import src.web.routes.voice_live_routes as V
    from src.ai.voice_emotion import EmotionSpec, to_qwen_instructions
    spec = EmotionSpec("sad", intensity=0.7)
    vc = {"emotion": spec, "persona": {"voice_profile": {"emotion": "playful"}}}
    assert V.build_clone_instructions(vc, "今天有点累") == to_qwen_instructions(spec)


def test_build_clone_instructions_layers_base_then_emotion():
    """运营 voice_profile.instructions 作 base，情绪叠加其后（与消息渠道克隆同口径）。"""
    import src.web.routes.voice_live_routes as V
    vc = {"emotion": None,
          "persona": {"voice_profile": {"emotion": "warm"}},
          "voice_cfg": {"instructions": "始终用中文"}}
    instr = V.build_clone_instructions(vc, "你好")
    assert instr.startswith("始终用中文")   # base 在前
    assert "温暖" in instr                    # 情绪在后


def test_build_clone_instructions_safe_on_garbage():
    """脏输入（None / 怪类型）绝不抛异常，恒返回字符串。"""
    import src.web.routes.voice_live_routes as V
    assert isinstance(V.build_clone_instructions(None, ""), str)
    assert isinstance(V.build_clone_instructions({"emotion": 123, "persona": "x"}, "hi"), str)


def test_call_tone_directive_from_voice_profile_baseline():
    """首句情绪锚：runtime voice_profile.emotion → 一句「天然语气 + 随对方情绪走」的基调，
    且内建安全（含「先共情安抚」），绝不指令念出。"""
    import src.web.routes.voice_live_routes as V
    vc = {"voice_cfg": {"voice_profile": {"emotion": "playful"}}}
    d = V.build_call_tone_directive(vc, None)
    assert "俏皮" in d and "随对方情绪走" in d
    assert "先共情安抚" in d                     # 情绪安全内建


def test_call_tone_directive_persona_fallback_and_neutral():
    """无 voice_cfg 基线 → 回落人设 dict 推断；无任何基线/中性 → ""（交给通用守则）。"""
    import src.web.routes.voice_live_routes as V
    d = V.build_call_tone_directive({}, {"personality": {"traits": ["严谨", "专业", "冷静"]}})
    assert "认真" in d or "郑重" in d            # serious 基线
    assert V.build_call_tone_directive(None, None) == ""
    assert V.build_call_tone_directive({"voice_cfg": {"voice_profile": {"emotion": "neutral"}}},
                                       None) == ""


def test_call_init_system_prompt_carries_persona_tone():
    """build_call_init 端到端：playful 人设 → session.init 的 system_prompt 带上语气基调。"""
    import src.web.routes.voice_live_routes as V
    voice_ctx = {"voice_cfg": {"voice_profile": {"emotion": "playful"}}}
    init = V.build_call_init(voice_ctx=voice_ctx,
                             persona={"name": "林小雨"}, memory_bullets_text="",
                             customer_language="zh", cfg={"realtime_voice": {"enabled": True}})
    assert "俏皮" in init.get("system_prompt", "")


# ── 路由装配（确定性；WS 握手/中继靠 mock host + 部署后实测验证）──────────────
def _app(enabled: bool):
    from fastapi import FastAPI
    from src.web.routes.voice_live_routes import register_voice_live_routes
    app = FastAPI()
    cm = types.SimpleNamespace(config={"realtime_voice": {"enabled": enabled}})
    register_voice_live_routes(app, api_auth=None, config_manager=cm)
    return app


def test_routes_registered():
    app = _app(True)
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/voice/live" in paths              # WS 全双工网关
    assert "/ops/voice-call" in paths              # 试拨页
    assert "/api/voice/engine/status" in paths     # 显存开关：状态
    assert "/api/voice/engine/load" in paths       # 显存开关：载入
    assert "/api/voice/engine/unload" in paths     # 显存开关：释放
    assert "/api/voice/conversations" in paths     # 最近会话下拉（chat_key 免手填）
    assert "/api/voice/live/readiness" in paths    # 试拨前校准（参考音/功能链/引擎）


def test_voice_live_readiness_disabled(monkeypatch):
    from fastapi.testclient import TestClient
    import src.web.routes.voice_live_routes as V
    monkeypatch.setattr(V, "collect_voice_ref_readiness_summary",
                        lambda *a, **k: {"persona_count": 0, "with_reference": 0, "worst_grade": "none"})
    j = TestClient(_app(False)).get("/api/voice/live/readiness").json()
    assert j["ok"] is True and j["verdict"] == "inactive"


def test_voice_live_readiness_ready(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.web.routes.voice_live_routes import register_voice_live_routes
    import src.web.routes.voice_live_routes as V
    app = FastAPI()
    cm = types.SimpleNamespace(config={
        "realtime_voice": {"enabled": True, "base_url": "http://127.0.0.1:7860"}})
    register_voice_live_routes(app, api_auth=None, config_manager=cm)
    monkeypatch.setattr(V, "collect_voice_ref_readiness_summary", lambda *a, **k: {
        "persona_count": 1, "with_reference": 1, "worst_grade": "green"})
    monkeypatch.setattr(V.RealtimeVoiceClient, "model_status", lambda self: {"model_loaded": True})
    j = TestClient(app).get("/api/voice/live/readiness").json()
    assert j["ok"] is True and j["verdict"] == "ready" and j["chain"]["opener_enabled"] is True


def test_voice_live_ws_handshake_accepts(monkeypatch):
    """回归（根因）：WS 端点签名 ``ws: WebSocket`` 经 ``from __future__ import annotations``
    字符串化后，若 ``WebSocket`` 不在模块全局，FastAPI 解析不出该参数→握手前即 close，
    浏览器永远「未连接」。本测真正 websocket_connect：断言 accept 成功（收到结构化事件，
    而非 connect 前 WebSocketDisconnect）。无主机→优雅回 voice_host_unreachable。"""
    from fastapi.testclient import TestClient
    import src.web.routes.voice_live_routes as V
    monkeypatch.setattr(V.RealtimeVoiceClient, "health_ok", lambda self: False)
    with TestClient(_app(True)).websocket_connect("/api/voice/live") as ws:
        ws.send_text(json.dumps({"type": "hello", "persona_id": "x"}))
        ev = json.loads(ws.receive_text())
    assert ev == {"type": "error", "error": "voice_host_unreachable"}


def test_voice_live_ws_disabled_closes_after_accept():
    """关时也须先 accept 再回 disabled（而非握手前 close）——确保前端拿到明确原因。"""
    from fastapi.testclient import TestClient
    with TestClient(_app(False)).websocket_connect("/api/voice/live") as ws:
        ev = json.loads(ws.receive_text())
    assert ev == {"type": "error", "error": "realtime_voice_disabled"}


# ── 通话观测埋点（网关生命周期 → RealtimeVoiceStats）契约 ──────────────────────
# 纯 stats 单测证明「计数对」，这里证明「网关在对的生命周期点真的调了它」。
def test_stats_attempt_and_host_unreachable(monkeypatch):
    """enabled + 健康探测失败：记一次 attempt + health_fail + ended(host_unreachable)，未接通。"""
    from fastapi.testclient import TestClient
    import src.web.routes.voice_live_routes as V
    from src.ai.realtime_voice_stats import get_realtime_voice_stats
    st = get_realtime_voice_stats(); st.reset()
    monkeypatch.setattr(V.RealtimeVoiceClient, "health_ok", lambda self: False)
    with TestClient(_app(True)).websocket_connect("/api/voice/live") as ws:
        ws.send_text(json.dumps({"type": "hello", "persona_id": "x"}))
        ev = json.loads(ws.receive_text())
    assert ev["error"] == "voice_host_unreachable"
    d = st.dump()
    assert d["attempts"] == 1 and d["connected"] == 0
    assert d["health_fail"] == 1 and d["health_ok"] == 0
    assert d["by_end_reason"].get("host_unreachable") == 1
    st.reset()


def test_stats_disabled_not_counted():
    """关闭态：accept 后即回 disabled，不计 attempt（非真实通话尝试，enabled 闸之前）。"""
    from fastapi.testclient import TestClient
    from src.ai.realtime_voice_stats import get_realtime_voice_stats
    st = get_realtime_voice_stats(); st.reset()
    with TestClient(_app(False)).websocket_connect("/api/voice/live") as ws:
        json.loads(ws.receive_text())
    assert st.dump()["attempts"] == 0
    st.reset()


def test_stats_unauthorized_counted(monkeypatch):
    """带 access_token 且握手口令不符：记 attempt + ended(unauthorized)，不触发健康探测。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.web.routes.voice_live_routes import register_voice_live_routes
    from src.ai.realtime_voice_stats import get_realtime_voice_stats
    st = get_realtime_voice_stats(); st.reset()
    app = FastAPI()
    cm = types.SimpleNamespace(
        config={"realtime_voice": {"enabled": True, "access_token": "s3cr3t"}})
    register_voice_live_routes(app, api_auth=None, config_manager=cm)
    with TestClient(app).websocket_connect("/api/voice/live") as ws:
        ws.send_text(json.dumps({"type": "hello", "token": "wrong"}))
        ev = json.loads(ws.receive_text())
    assert ev["error"] == "unauthorized"
    d = st.dump()
    assert d["attempts"] == 1 and d["connected"] == 0
    assert d["by_end_reason"].get("unauthorized") == 1
    assert d["health_ok"] == 0 and d["health_fail"] == 0
    st.reset()


def test_stats_engine_load_unload(monkeypatch):
    """显存 load/unload 端点各记一次 engine_action，供 ops 卡观测。"""
    from fastapi.testclient import TestClient
    import src.web.routes.voice_live_routes as V
    from src.ai.realtime_voice_stats import get_realtime_voice_stats
    st = get_realtime_voice_stats(); st.reset()
    monkeypatch.setattr(V.RealtimeVoiceClient, "load_model", lambda self: {"model_loaded": True})
    monkeypatch.setattr(V.RealtimeVoiceClient, "unload_model", lambda self: {"model_loaded": False})
    c = TestClient(_app(True))
    assert c.post("/api/voice/engine/load").status_code == 200
    assert c.post("/api/voice/engine/unload").status_code == 200
    d = st.dump()
    assert d["engine_load"] == 1 and d["engine_unload"] == 1
    st.reset()


def test_engine_status_reports_auth_required(monkeypatch):
    """status 端点回显 auth_required，供 ops 卡决定是否展示口令框。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.web.routes.voice_live_routes import register_voice_live_routes
    import src.web.routes.voice_live_routes as V
    monkeypatch.setattr(V.RealtimeVoiceClient, "model_status", lambda self: {"model_loaded": False})
    app = FastAPI()
    cm = types.SimpleNamespace(
        config={"realtime_voice": {"enabled": True, "access_token": "tok"}})
    register_voice_live_routes(app, api_auth=None, config_manager=cm)
    j = TestClient(app).get("/api/voice/engine/status").json()
    assert j["enabled"] is True and j["auth_required"] is True and j["model_loaded"] is False


# ── 最近会话下拉 + 记忆链路（回归：曾因双重断裂导致语音长期记忆从不生效）──────────
# 断裂①：记忆库挂在 skill_manager._episodic_store，路由却读未设置的 app.state.episodic_memory
# 断裂②：bot 管线按 platform:chat_key 落库，语音页用裸 chat_key 查 → 永远 miss
def test_voice_conversations_memory_link():
    from fastapi.testclient import TestClient
    app = _app(True)

    class _Epi:  # 仅 "telegram:111" 有事实；裸键 "111" 无（复刻真实落库口径）
        def get_bullets_for_prompt(self, key, max_items=8, **kw):
            return "• 喜欢夜跑" if str(key) == "telegram:111" else ""

    rows = [
        {"chat_key": "111", "platform": "telegram", "chat_type": "private",
         "display_name": "小雨", "last_text": "在吗～", "last_ts": 100.0},
        {"chat_key": "222", "platform": "telegram", "chat_type": "private",
         "display_name": "阿龙", "last_text": "hi", "last_ts": 90.0},
        {"chat_key": "-100999", "platform": "telegram", "chat_type": "group",
         "display_name": "某群", "last_text": "x", "last_ts": 80.0},
    ]

    class _Inbox:
        def list_conversations(self, *, limit=50, platform=""):
            return list(rows)

    # 关键：只挂 skill_manager（不挂 app.state.episodic_memory），证明回落解析能取到库
    app.state.inbox_store = _Inbox()
    app.state.skill_manager = types.SimpleNamespace(_episodic_store=_Epi())

    r = TestClient(app).get("/api/voice/conversations?limit=10")
    assert r.status_code == 200
    by_name = {c["name"]: c for c in r.json()["conversations"]}

    assert "某群" not in by_name                            # 群组/频道被过滤
    assert by_name["小雨"]["memory_key"] == "telegram:111"  # 断裂②修复：平台限定键
    assert by_name["小雨"]["has_memory"] is True            # 断裂①修复：经 skill_manager 回落取到库
    assert by_name["阿龙"]["has_memory"] is False           # 库里无事实 → 标记为假（不误报）
    assert by_name["小雨"]["chat_key"] == "111"             # 裸键仍原样供语音/情绪上下文


# ── 通话「主动开场白」（人设接通后先开口，克隆真声）─────────────────────────────
def test_build_opener_text_uses_persona_opener():
    import src.web.routes.voice_live_routes as V
    txt = V.build_opener_text({"speaking": {"openers": ["嗨，是我，林小雨～"]}}, "zh")
    assert txt.startswith("嗨") and "林小雨" in txt


def test_build_opener_text_cleans_placeholder_and_falls_back():
    import src.web.routes.voice_live_routes as V
    # 占位 xxxx 清掉后过短 → 语言默认开场；未知语言 → 回 zh
    assert V.build_opener_text({"speaking": {"openers": ["xxxx"]}}, "en") == V._OPENER_DEFAULT["en"]
    assert V.build_opener_text(None, "zh") == V._OPENER_DEFAULT["zh"]
    assert V.build_opener_text({"speaking": {}}, "fr") == V._OPENER_DEFAULT["zh"]


def test_build_opener_text_never_half_sentence():
    import src.web.routes.voice_live_routes as V
    # 省略号吊半句 → 裁到最后一个完整句末（不再"只说一半"）
    assert V.build_opener_text(
        {"speaking": {"openers": ["哇最近太忙了！你有没有…"]}}, "zh") == "哇最近太忙了！"
    # 纯吊半句、无完整句末 → 回落完整默认开场（绝不念半句）
    assert V.build_opener_text(
        {"speaking": {"openers": ["哎你说的这个，我想到一件事…"]}}, "zh") == V._OPENER_DEFAULT["zh"]
    # 含 xxx 占位的吊半句模板 → 回落默认（不念"我最近在，…"半成品）
    assert V.build_opener_text(
        {"speaking": {"openers": ["我最近在xxx，想起了一件事…"]}}, "zh") == V._OPENER_DEFAULT["zh"]
    # 不含省略号的完整短句 → 原样保留（不误删波浪号、不强行兜底）
    assert V.build_opener_text({"speaking": {"openers": ["在呢~"]}}, "zh") == "在呢~"
    assert V.build_opener_text(
        {"speaking": {"openers": ["刚下班，累但是满足，你怎么样？"]}}, "zh") == "刚下班，累但是满足，你怎么样？"


def test_wav_to_pcm16_roundtrip_and_garbage_safe():
    sf = pytest.importorskip("soundfile")
    import io
    import numpy as np
    import src.web.routes.voice_live_routes as V
    buf = io.BytesIO()
    sf.write(buf, np.zeros(1200, dtype="float32"), 16000, format="WAV", subtype="PCM_16")
    pcm, sr = V._wav_to_pcm16(buf.getvalue())
    assert sr == 16000 and len(pcm) == 1200 * 2          # 单声道 PCM16 = 2 字节/采样
    assert V._wav_to_pcm16(b"not a wav") == (b"", 0)     # 垃圾输入安全


class _FakeVoiceClient:
    def __init__(self, wav, loaded=True):
        self._wav = wav
        self._loaded = loaded
        self.calls = []

    def model_status(self):
        return {"model_loaded": self._loaded}

    def clone_oneshot(self, text, ref_b64, *, reference_text="", language="zh",
                      instructions="", timeout=60.0):
        self.calls.append({"text": text, "language": language, "instructions": instructions})
        return self._wav


def _opener_ctx(ref_path):
    return {
        "voice_ctx": {"voice_cfg": {"voice_profile": {
            "reference_audio_path": ref_path, "emotion": "playful"}}},
        "persona": {"speaking": {"openers": ["嗨，是我呀～"]}},
    }


async def test_prepare_call_opener_synthesizes(monkeypatch):
    sf = pytest.importorskip("soundfile")
    import io
    import numpy as np
    import src.web.routes.voice_live_routes as V
    buf = io.BytesIO()
    sf.write(buf, np.zeros(4800, dtype="float32"), 24000, format="WAV", subtype="PCM_16")
    monkeypatch.setattr(V, "_b64_ref_audio", lambda p: "QUJD")     # 跳过真实磁盘读
    client = _FakeVoiceClient(buf.getvalue())
    out = await V._prepare_call_opener(client, _opener_ctx("ref.wav"), {"language": "zh"})
    assert out and out["sr"] == 24000 and out["pcm"] and out["text"].startswith("嗨")
    assert client.calls and client.calls[0]["text"].startswith("嗨")   # 走克隆合成


async def test_prepare_call_opener_skips_without_ref_or_unloaded(monkeypatch):
    import src.web.routes.voice_live_routes as V
    monkeypatch.setattr(V, "_b64_ref_audio", lambda p: "QUJD")
    # 无参考音 → None（连模型都不查）
    assert await V._prepare_call_opener(
        _FakeVoiceClient(b"x"),
        {"voice_ctx": {"voice_cfg": {"voice_profile": {}}}, "persona": {}},
        {"language": "zh"}) is None
    # 有参考音但模型未载入 → None
    assert await V._prepare_call_opener(
        _FakeVoiceClient(b"x", loaded=False), _opener_ctx("ref.wav"), {"language": "zh"}) is None


async def test_emit_opener_emits_transcript_audio_turnend():
    import src.web.routes.voice_live_routes as V

    class _WS:
        def __init__(self):
            self.sent = []

        async def send_text(self, s):
            self.sent.append(s)

    ws = _WS()
    await V._emit_opener(ws, {"text": "嗨", "pcm": b"\x00\x00" * 48000, "sr": 24000})  # 2s
    types_seen = [json.loads(x)["type"] for x in ws.sent]
    assert types_seen[0] == "transcript.assistant"
    assert types_seen.count("output_audio") >= 1
    assert types_seen[-1] == "turn.end"
    # 空/无音频 → 静默不发
    ws2 = _WS()
    await V._emit_opener(ws2, None)
    await V._emit_opener(ws2, {"text": "x", "pcm": b"", "sr": 24000})
    assert ws2.sent == []


# ── 双语字幕（助手转写译成运营阅读语言，按 tid 关联气泡）─────────────────────────
class _FakeTS:
    def __init__(self, translated, *, ok=True, provider="deepl", target="zh"):
        self._t, self._ok, self._p, self._tgt = translated, ok, provider, target
        self.calls = []

    async def translate(self, text, *, target_lang="", source_lang="", style="chat", engine=""):
        self.calls.append({"text": text, "target": target_lang, "source": source_lang})
        return types.SimpleNamespace(translated_text=self._t, ok=self._ok,
                                     provider=self._p, target_lang=self._tgt)


class _CapWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, s):
        self.sent.append(s)


async def test_send_subtitle_emits_translation():
    import src.web.routes.voice_live_routes as V
    ws, ts = _CapWS(), _FakeTS("你好吗？")
    await V._send_subtitle(ws, "How are you?", tid=3, target_lang="zh", translation_service=ts)
    assert len(ws.sent) == 1
    ev = json.loads(ws.sent[0])
    assert ev["type"] == "transcript.translation"
    assert ev["tid"] == 3 and ev["text"] == "你好吗？" and ev["lang"] == "zh"
    # 源语言交给服务自动检测（不硬塞通话语言），防 zh 开场白被当 en 翻译而 garble
    assert ts.calls[0]["target"] == "zh" and ts.calls[0]["source"] == ""


async def test_send_subtitle_skips_identity_failure_and_noop():
    import src.web.routes.voice_live_routes as V
    # 同语言（provider=identity）→ 不发（同语言通话零字幕）
    ws = _CapWS()
    await V._send_subtitle(ws, "你好", tid=1, target_lang="zh",
                           translation_service=_FakeTS("你好", provider="identity"))
    assert ws.sent == []
    # 翻译失败 ok=False → 不发
    ws = _CapWS()
    await V._send_subtitle(ws, "hi", tid=1, target_lang="zh",
                           translation_service=_FakeTS("", ok=False))
    assert ws.sent == []
    # 译文==原文 → 不发
    ws = _CapWS()
    await V._send_subtitle(ws, "hi", tid=1, target_lang="zh",
                           translation_service=_FakeTS("hi"))
    assert ws.sent == []
    # 无 service / 空文本 / 空目标语言 → 不发
    ws = _CapWS()
    await V._send_subtitle(ws, "hi", tid=1, target_lang="zh", translation_service=None)
    await V._send_subtitle(ws, "", tid=1, target_lang="zh", translation_service=_FakeTS("x"))
    await V._send_subtitle(ws, "hi", tid=1, target_lang="", translation_service=_FakeTS("x"))
    assert ws.sent == []
