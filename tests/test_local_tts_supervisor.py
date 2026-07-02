"""Unit tests for LocalTTSSupervisor decision + stop-gating logic.

No real subprocess/network: ``_health`` / ``_spawn`` / ``_kill_tree`` are stubbed.
Covers the invariants that matter for the coupled local-TTS lifecycle:
  - disabled → no-op (never spawns)
  - reuse_if_healthy + healthy port → attach (never spawns; not "managed")
  - absent/unhealthy → spawn (managed)
  - reuse_if_healthy=false → always spawn even if healthy
  - stop only kills when WE spawned it AND stop_with_app
"""
from __future__ import annotations

from src.integrations.local_tts_supervisor import (
    ACT_ATTACH,
    ACT_DISABLED,
    ACT_SPAWN,
    LocalTTSSupervisor,
)


def _mk(**la):
    """Build a supervisor from a minicpm_clone.local_autostart override."""
    return LocalTTSSupervisor({
        "base_url": "http://127.0.0.1:7899",
        "health_path": "/health",
        "local_autostart": la,
    })


def test_config_parsing_reads_nested_block():
    sup = _mk(
        enabled=True, stop_with_app=False, reuse_if_healthy=False,
        ready_wait_sec=30, cwd="X:/idx",
        command=["py.exe", "server.py"], env={"A": 1, "B": "two"},
    )
    assert sup.enabled is True
    assert sup.stop_with_app is False
    assert sup.reuse_if_healthy is False
    assert sup.ready_wait_sec == 30.0
    assert sup.cwd == "X:/idx"
    assert sup.command == ["py.exe", "server.py"]
    assert sup.env_extra == {"A": "1", "B": "two"}  # coerced to str
    assert sup.base_url == "http://127.0.0.1:7899"
    assert sup._port_from_base() == 7899


def test_defaults_are_safe_off():
    sup = LocalTTSSupervisor({})  # no local_autostart at all
    assert sup.enabled is False
    assert sup._decide() == ACT_DISABLED


def test_decide_disabled(monkeypatch):
    sup = _mk(enabled=False)
    # even if healthy, disabled short-circuits without probing
    monkeypatch.setattr(sup, "_health", lambda timeout=2.0: {"model_loaded": True})
    assert sup._decide() == ACT_DISABLED


def test_decide_attach_when_healthy(monkeypatch):
    sup = _mk(enabled=True, reuse_if_healthy=True)
    monkeypatch.setattr(sup, "_health", lambda timeout=2.0: {"model_loaded": True})
    assert sup._decide() == ACT_ATTACH


def test_decide_spawn_when_absent(monkeypatch):
    sup = _mk(enabled=True, reuse_if_healthy=True)
    monkeypatch.setattr(sup, "_health", lambda timeout=2.0: None)
    assert sup._decide() == ACT_SPAWN


def test_decide_spawn_when_reuse_disabled_even_if_healthy(monkeypatch):
    sup = _mk(enabled=True, reuse_if_healthy=False)
    monkeypatch.setattr(sup, "_health", lambda timeout=2.0: {"model_loaded": True})
    assert sup._decide() == ACT_SPAWN


async def test_start_disabled_does_not_spawn(monkeypatch):
    sup = _mk(enabled=False)
    calls = []
    monkeypatch.setattr(sup, "_spawn", lambda: calls.append("spawn") or True)
    ok = await sup.start()
    assert ok is False
    assert calls == []
    assert sup._managed is False


async def test_start_attach_does_not_spawn(monkeypatch):
    sup = _mk(enabled=True, reuse_if_healthy=True)
    monkeypatch.setattr(sup, "_health", lambda timeout=2.0: {"model_loaded": True})
    calls = []
    monkeypatch.setattr(sup, "_spawn", lambda: calls.append("spawn") or True)
    ok = await sup.start()
    assert ok is True
    assert calls == []
    assert sup._managed is False  # attached, not managed → won't be stopped


async def test_start_spawns_when_absent(monkeypatch):
    sup = _mk(enabled=True, reuse_if_healthy=True)
    monkeypatch.setattr(sup, "_health", lambda timeout=2.0: None)

    def fake_spawn():
        sup._managed = True
        return True

    monkeypatch.setattr(sup, "_spawn", fake_spawn)
    ok = await sup.start()
    assert ok is True
    assert sup._managed is True


async def test_stop_kills_when_managed(monkeypatch):
    sup = _mk(enabled=True, stop_with_app=True)
    sup._managed = True
    sup._proc = object()  # sentinel
    killed = []
    monkeypatch.setattr(sup, "_kill_tree", lambda proc: killed.append(proc))
    await sup.stop()
    assert len(killed) == 1
    assert sup._proc is None


async def test_stop_noop_when_attached(monkeypatch):
    sup = _mk(enabled=True, stop_with_app=True, reuse_if_healthy=True)
    sup._managed = False          # attached, not spawned by us
    sup._proc = object()
    killed = []
    monkeypatch.setattr(sup, "_kill_tree", lambda proc: killed.append(proc))
    await sup.stop()
    assert killed == []           # must not kill something we didn't start


async def test_stop_noop_when_stop_with_app_false(monkeypatch):
    sup = _mk(enabled=True, stop_with_app=False)
    sup._managed = True
    sup._proc = object()
    killed = []
    monkeypatch.setattr(sup, "_kill_tree", lambda proc: killed.append(proc))
    await sup.stop()
    assert killed == []           # configured to leave it running


def test_status_snapshot_off_when_disabled(monkeypatch):
    sup = _mk(enabled=False)
    monkeypatch.setattr(sup, "_health", lambda timeout=2.0: None)
    snap = sup.status_snapshot()
    assert snap["enabled"] is False
    assert snap["mode"] == "off"
    assert snap["reachable"] is False


def test_status_snapshot_managed(monkeypatch):
    sup = _mk(enabled=True)
    sup._managed = True
    class _P:
        pid = 4242
        @staticmethod
        def poll():
            return None
    sup._proc = _P()
    monkeypatch.setattr(sup, "_health", lambda timeout=2.0: {"model_loaded": True, "loading": False})
    snap = sup.status_snapshot()
    assert snap["mode"] == "managed"
    assert snap["pid"] == 4242
    assert snap["model_loaded"] is True


def test_status_snapshot_attached(monkeypatch):
    sup = _mk(enabled=True, reuse_if_healthy=True)
    sup._managed = False
    monkeypatch.setattr(sup, "_health", lambda timeout=2.0: {"model_loaded": True})
    snap = sup.status_snapshot()
    assert snap["mode"] == "attached"
    assert snap["attached"] is True


def test_reload_from_config_updates_enabled():
    sup = _mk(enabled=False)
    sup.reload_from_config({"local_autostart": {"enabled": True, "stop_with_app": False}})
    assert sup.enabled is True
    assert sup.stop_with_app is False


async def test_apply_enabled_starts_when_turned_on(monkeypatch):
    sup = _mk(enabled=False)
    started = []
    async def fake_start():
        started.append(1)
        return True
    monkeypatch.setattr(sup, "start", fake_start)
    out = await sup.apply_enabled(True)
    assert sup.enabled is True
    assert out["runtime_action"] == "start"
    assert started == [1]


async def test_apply_enabled_stops_when_turned_off(monkeypatch):
    sup = _mk(enabled=True)
    stopped = []
    async def fake_stop():
        stopped.append(1)
    monkeypatch.setattr(sup, "stop", fake_stop)
    out = await sup.apply_enabled(False)
    assert sup.enabled is False
    assert out["runtime_action"] == "stop"
    assert stopped == [1]
