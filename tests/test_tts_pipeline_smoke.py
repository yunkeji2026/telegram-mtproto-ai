"""TTS pipeline smoke tests."""
from __future__ import annotations


def test_tts_pipeline_imports_clean():
    from src.ai import tts_pipeline

    r = tts_pipeline.TTSResult()
    assert r.ok is False
    assert r.audio_path == ""
    assert r.latency_ms == 0


def test_tts_pipeline_disabled_soft_fails():
    import asyncio
    from src.ai.tts_pipeline import TTSPipeline

    async def run():
        p = TTSPipeline({"enabled": False})
        rv = await p.synthesize("hello")
        assert rv.ok is False
        assert rv.error == "pipeline_disabled"

    asyncio.run(run())


def test_tts_voice_profile_requires_owner_consent(tmp_path):
    import asyncio
    from src.ai.tts_pipeline import TTSPipeline

    ref = tmp_path / "me.wav"
    ref.write_bytes(b"wav")

    async def run():
        p = TTSPipeline({
            "enabled": True,
            "backend": "voice_clone_command",
            "voice_profile": {
                "enabled": True,
                "owner_consent": False,
                "speaker_id": "my_voice",
                "reference_audio_path": str(ref),
                "backend": "voice_clone_command",
                "command_template": "echo {text} > {out}",
            },
        })
        rv = await p.synthesize("hello")
        assert rv.ok is False
        assert "voice_profile_requires_owner_consent" in rv.error

    asyncio.run(run())


def test_tts_voice_clone_command_uses_reference_audio(tmp_path, monkeypatch):
    import asyncio
    from subprocess import CompletedProcess
    from src.ai.tts_pipeline import TTSPipeline

    ref = tmp_path / "me.wav"
    ref.write_bytes(b"wav")
    calls = []

    def fake_run(cmd, shell, capture_output, text, timeout, env=None):
        calls.append(cmd)
        out_arg = cmd.split("--out ", 1)[1].strip().strip("'\"")
        with open(out_arg, "wb") as f:
            f.write(b"audio")
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("src.ai.tts_pipeline.subprocess.run", fake_run)

    async def run():
        p = TTSPipeline({
            "enabled": True,
            "backend": "voice_clone_command",
            "format": "wav",
            "out_dir": str(tmp_path),
            "voice_profile": {
                "enabled": True,
                "owner_consent": True,
                "speaker_id": "my_voice",
                "reference_audio_path": str(ref),
                "backend": "voice_clone_command",
                "command_template": "clone --text {text} --ref {reference_audio} --out {out}",
            },
        })
        rv = await p.synthesize("hello")
        assert rv.ok is True
        assert rv.provider == "voice_clone_command"
        assert rv.voice == "my_voice"
        assert "--ref" in calls[0]
        assert str(ref) in calls[0]

    asyncio.run(run())


def test_tts_voice_clone_command_args_avoids_shell(tmp_path, monkeypatch):
    import asyncio
    from subprocess import CompletedProcess
    from src.ai.tts_pipeline import TTSPipeline

    ref = tmp_path / "me.wav"
    ref.write_bytes(b"wav")
    calls = []

    def fake_run(cmd, shell, capture_output, text, timeout, env=None):
        calls.append((cmd, shell))
        out_arg = cmd[cmd.index("--out") + 1]
        with open(out_arg, "wb") as f:
            f.write(b"audio")
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("src.ai.tts_pipeline.subprocess.run", fake_run)

    async def run():
        p = TTSPipeline({
            "enabled": True,
            "backend": "voice_clone_command",
            "format": "wav",
            "out_dir": str(tmp_path),
            "voice_profile": {
                "enabled": True,
                "owner_consent": True,
                "speaker_id": "my_voice",
                "reference_audio_path": str(ref),
                "backend": "voice_clone_command",
                "command_args": [
                    "clone", "--text", "{text}",
                    "--ref", "{reference_audio}",
                    "--out", "{out}",
                ],
            },
        })
        rv = await p.synthesize("hello")
        assert rv.ok is True
        assert calls[0][1] is False
        assert calls[0][0][calls[0][0].index("--ref") + 1] == str(ref)

    asyncio.run(run())


def test_tts_voice_clone_command_injects_dashscope_env(tmp_path, monkeypatch):
    import asyncio
    from subprocess import CompletedProcess
    from src.ai.tts_pipeline import TTSPipeline

    ref = tmp_path / "me.wav"
    ref.write_bytes(b"wav")
    envs = []

    def fake_run(cmd, shell, capture_output, text, timeout, env=None):
        envs.append(env or {})
        out_arg = cmd[cmd.index("--out") + 1]
        with open(out_arg, "wb") as f:
            f.write(b"audio")
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("src.ai.tts_pipeline.subprocess.run", fake_run)

    async def run():
        p = TTSPipeline({
            "enabled": True,
            "backend": "voice_clone_command",
            "format": "wav",
            "out_dir": str(tmp_path),
            "dashscope_api_key": "sk-test",
            "dashscope_region": "cn",
            "voice_profile": {
                "enabled": True,
                "owner_consent": True,
                "speaker_id": "my_voice",
                "reference_audio_path": str(ref),
                "backend": "voice_clone_command",
                "command_args": ["clone", "--text", "{text}", "--out", "{out}"],
            },
        })
        rv = await p.synthesize("hello")
        assert rv.ok is True
        assert envs[0]["DASHSCOPE_API_KEY"] == "sk-test"
        assert envs[0]["DASHSCOPE_REGION"] == "cn"

    asyncio.run(run())


def test_tts_falls_back_to_edge_when_network_backend_fails(tmp_path, monkeypatch):
    """coqui_http 等网络后端连不上时（如 LAN 主机离线 / WinError 10060），
    应自动回落到 edge_tts 出声，而不是把 URLError 硬抛给用户。"""
    import asyncio
    from src.ai.tts_pipeline import TTSPipeline

    ref = tmp_path / "me.wav"
    ref.write_bytes(b"wav")
    calls = {"coqui": 0, "edge": 0}

    def fake_coqui(self, text, out):
        calls["coqui"] += 1
        from urllib.error import URLError
        raise URLError("<urlopen error [WinError 10060]>")

    async def fake_edge(self, text, out, voice):
        calls["edge"] += 1
        out.write_bytes(b"ID3edge-audio-bytes" + b"\x00" * 512)

    monkeypatch.setattr(TTSPipeline, "_synthesize_coqui_http", fake_coqui)
    monkeypatch.setattr(TTSPipeline, "_edge_tts", fake_edge)

    async def run():
        p = TTSPipeline({
            "enabled": True,
            "backend": "coqui_http",
            "base_url": "http://192.168.0.166:7851",
            "format": "wav",
            "out_dir": str(tmp_path),
            "voice_profile": {
                "enabled": True,
                "owner_consent": True,
                "backend": "coqui_http",
                "reference_audio_path": str(ref),
            },
        })
        rv = await p.synthesize("你好")
        assert rv.ok is True
        assert rv.provider == "edge_tts"
        assert rv.extra.get("fallback_from") == "coqui_http"
        assert "WinError 10060" in rv.extra.get("primary_error", "")
        assert calls["coqui"] == 1 and calls["edge"] == 1

    asyncio.run(run())


def test_tts_consent_error_does_not_fall_back(tmp_path, monkeypatch):
    """owner_consent 缺失属配置/授权错误：必须硬失败，不能用通用音色掩盖。"""
    import asyncio
    from src.ai.tts_pipeline import TTSPipeline

    ref = tmp_path / "me.wav"
    ref.write_bytes(b"wav")
    edge_called = {"n": 0}

    async def fake_edge(self, text, out, voice):
        edge_called["n"] += 1
        out.write_bytes(b"ID3edge" + b"\x00" * 512)

    monkeypatch.setattr(TTSPipeline, "_edge_tts", fake_edge)

    async def run():
        p = TTSPipeline({
            "enabled": True,
            "backend": "voice_clone_command",
            "out_dir": str(tmp_path),
            "voice_profile": {
                "enabled": True,
                "owner_consent": False,
                "reference_audio_path": str(ref),
                "backend": "voice_clone_command",
                "command_template": "echo {text} > {out}",
            },
        })
        rv = await p.synthesize("hello")
        assert rv.ok is False
        assert "voice_profile_requires_owner_consent" in rv.error
        assert edge_called["n"] == 0

    asyncio.run(run())


def test_qwen_wrapper_loads_env_local_secret(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.local").write_text(
        "DASHSCOPE_API_KEY=abc123\nDASHSCOPE_REGION=cn\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    from tools.qwen_tts_wrapper import _load_local_secret

    assert _load_local_secret("DASHSCOPE_API_KEY") == "abc123"
