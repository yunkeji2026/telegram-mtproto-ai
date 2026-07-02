"""Manage a LOCAL voice-clone (IndexTTS2) subprocess tied to the app lifecycle.

Feature-flagged via ``minicpm_clone.local_autostart``. When enabled, the main
app launches the local IndexTTS2 adapter on startup and (optionally) terminates
it on shutdown, so the GPU TTS service starts/stops together with the app.

Design invariants (must never break app startup/shutdown):
- **Never block / never raise into the caller.** Spawn is best-effort and
  non-blocking (unless ``ready_wait_sec>0``); any failure logs a warning and the
  app keeps running — voice simply falls back to edge via the existing
  ``minicpm_clone.cloud_fallback`` path.
- **Reuse-if-healthy.** If the adapter is already serving on the port (e.g. an
  independently-launched instance), attach instead of double-spawning.
- **Stop-together (Windows).** The child is assigned to a Job Object with
  ``KILL_ON_JOB_CLOSE`` so it is reaped when the app dies for *any* reason (even
  a hard TerminateProcess), preventing leaked GPU VRAM. Graceful stop also
  terminates the process tree explicitly (belt + suspenders).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ai_chat_assistant.LocalTTSSupervisor")

# Decision outcomes (kept as constants for testability).
ACT_DISABLED = "disabled"
ACT_ATTACH = "attach"
ACT_SPAWN = "spawn"

_DEFAULT_CWD = "D:/workspace/index-tts"
_DEFAULT_COMMAND: List[str] = [
    "D:/workspace/index-tts/.venv/Scripts/python.exe",
    "aitr_indextts2_server.py",
]


class LocalTTSSupervisor:
    """Lifecycle manager for a local IndexTTS2 adapter subprocess."""

    def __init__(self, minicpm_clone_cfg: Optional[Dict[str, Any]] = None):
        mcc = minicpm_clone_cfg or {}
        la = mcc.get("local_autostart") or {}
        self.enabled: bool = bool(la.get("enabled", False))
        self.cwd: str = str(la.get("cwd") or _DEFAULT_CWD)
        self.command: List[str] = [str(c) for c in (la.get("command") or _DEFAULT_COMMAND)]
        self.env_extra: Dict[str, str] = {
            str(k): str(v) for k, v in (la.get("env") or {}).items()
        }
        self.stop_with_app: bool = bool(la.get("stop_with_app", True))
        self.reuse_if_healthy: bool = bool(la.get("reuse_if_healthy", True))
        self.ready_wait_sec: float = float(la.get("ready_wait_sec", 0) or 0)
        self.base_url: str = str(mcc.get("base_url") or "http://127.0.0.1:7899").rstrip("/")
        self.health_path: str = str(mcc.get("health_path") or "/health")

        self._proc: Optional[subprocess.Popen] = None
        self._job: Optional[int] = None          # Windows job handle (kept open for app life)
        self._logf = None                         # child stdout/stderr log file handle
        self._managed: bool = False               # True only if *we* spawned the process

    def reload_from_config(self, minicpm_clone_cfg: Optional[Dict[str, Any]] = None) -> None:
        """Re-read ``local_autostart`` from merged config (overlay toggle 后即时生效)."""
        mcc = minicpm_clone_cfg or {}
        la = mcc.get("local_autostart") or {}
        self.enabled = bool(la.get("enabled", False))
        self.stop_with_app = bool(la.get("stop_with_app", True))
        self.reuse_if_healthy = bool(la.get("reuse_if_healthy", True))
        self.ready_wait_sec = float(la.get("ready_wait_sec", 0) or 0)
        if la.get("cwd"):
            self.cwd = str(la.get("cwd"))
        if la.get("command"):
            self.command = [str(c) for c in la.get("command")]
        if isinstance(la.get("env"), dict):
            self.env_extra = {str(k): str(v) for k, v in la.get("env").items()}
        if mcc.get("base_url"):
            self.base_url = str(mcc.get("base_url")).rstrip("/")
        if mcc.get("health_path"):
            self.health_path = str(mcc.get("health_path"))

    def status_snapshot(self) -> Dict[str, Any]:
        """Runtime status for ops API / dashboard (never raises)."""
        health = self._health(timeout=1.5)
        reachable = health is not None
        proc = self._proc
        pid: Optional[int] = None
        try:
            if proc is not None and proc.poll() is None:
                pid = int(proc.pid)
        except Exception:
            pid = None
        if reachable and not self._managed and pid is None:
            mode = "attached"   # 外部/计划任务已起，主程序复用
        elif self._managed:
            mode = "managed"    # 本进程拉起并托管
        elif self.enabled:
            mode = "enabled_down"
        else:
            mode = "off"
        return {
            "enabled": self.enabled,
            "stop_with_app": self.stop_with_app,
            "reuse_if_healthy": self.reuse_if_healthy,
            "managed": self._managed,
            "attached": reachable and not self._managed,
            "reachable": reachable,
            "model_loaded": bool(health and health.get("model_loaded")),
            "loading": bool(health and health.get("loading")),
            "worker_running": bool(health and health.get("worker_running")),
            "pid": pid,
            "base_url": self.base_url,
            "mode": mode,
            "health": health or {},
        }

    async def apply_enabled(self, enabled: bool) -> Dict[str, Any]:
        """Runtime toggle: flip ``enabled`` and start/stop the child as appropriate."""
        prev = self.enabled
        self.enabled = bool(enabled)
        if enabled and not prev:
            ok = await self.start()
            return {"runtime_action": "start", "runtime_ok": ok}
        if not enabled and prev:
            await self.stop()
            return {"runtime_action": "stop", "runtime_ok": True}
        return {"runtime_action": "noop", "runtime_ok": True}

    # ── health probe ────────────────────────────────────────────────────────
    def _health(self, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
        """GET /health → parsed dict, or None if unreachable/non-200."""
        url = self.base_url + self.health_path
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (local only)
                if getattr(r, "status", 200) != 200:
                    return None
                return json.loads(r.read().decode("utf-8") or "{}")
        except Exception:
            return None

    def _is_loaded(self, timeout: float = 2.0) -> bool:
        h = self._health(timeout=timeout)
        return bool(h and h.get("model_loaded"))

    # ── decision (pure-ish; monkeypatch _health in tests) ────────────────────
    def _decide(self) -> str:
        if not self.enabled:
            return ACT_DISABLED
        if self.reuse_if_healthy and self._health(timeout=2.0) is not None:
            return ACT_ATTACH
        return ACT_SPAWN

    # ── start ─────────────────────────────────────────────────────────────────
    async def start(self) -> bool:
        """Bring the local TTS up per config. Returns True if it is (or will be) available."""
        action = self._decide()
        if action == ACT_DISABLED:
            logger.info("本机 TTS 托管未启用（minicpm_clone.local_autostart.enabled=false）")
            return False
        if action == ACT_ATTACH:
            self._managed = False
            logger.info(
                "本机 IndexTTS2 已在 %s 运行（model_loaded=%s）→ 复用，不重复拉起",
                self.base_url, self._is_loaded(1.5),
            )
            return True
        # ACT_SPAWN — offload blocking Popen/exists checks to a thread
        ok = await asyncio.to_thread(self._spawn)
        if not ok:
            return False
        if self.ready_wait_sec > 0:
            await self._wait_ready(self.ready_wait_sec)
        return True

    def _spawn(self) -> bool:
        cwd = Path(self.cwd)
        if not cwd.is_dir():
            logger.warning("本机 TTS 托管跳过：工作目录不存在 %s", cwd)
            return False
        exe = self.command[0] if self.command else ""
        try:
            if exe and os.path.isabs(exe) and not Path(exe).exists():
                logger.warning("本机 TTS 托管跳过：解释器不存在 %s（可在 config 覆盖 command）", exe)
                return False
        except Exception:
            pass

        env = dict(os.environ)
        env.setdefault("INDEXTTS2_EAGER", "1")   # 常驻已载入，首条语音即用克隆声
        env.setdefault("INDEXTTS2_FP16", "1")
        env.setdefault("INDEXTTS2_PORT", str(self._port_from_base()))
        for k, v in self.env_extra.items():
            env[k] = v

        creationflags = 0
        if sys.platform == "win32":
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )

        try:
            log_dir = Path("logs")
            log_dir.mkdir(exist_ok=True)
            self._logf = open(log_dir / "indextts2_managed.log", "ab", buffering=0)
        except Exception:
            self._logf = None

        try:
            self._proc = subprocess.Popen(
                self.command,
                cwd=str(cwd),
                env=env,
                stdout=(self._logf or subprocess.DEVNULL),
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except Exception as ex:
            logger.warning("本机 TTS 托管拉起失败: %s", ex)
            self._proc = None
            self._close_logf()
            return False

        self._managed = True
        logger.info(
            "本机 IndexTTS2 已由主程序拉起 pid=%s（eager 载入 ~60-90s；就绪前语音回落 edge，日志 logs/indextts2_managed.log）",
            self._proc.pid,
        )
        if sys.platform == "win32" and self.stop_with_app:
            self._assign_job(self._proc.pid)
        return True

    def _port_from_base(self) -> int:
        try:
            return int(self.base_url.rsplit(":", 1)[1].split("/")[0])
        except Exception:
            return 7899

    # ── Windows Job Object: reap child tree when the app dies (any reason) ────
    def _assign_job(self, pid: int) -> None:
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
            JobObjectExtendedLimitInformation = 9

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                    ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.OpenProcess.restype = wintypes.HANDLE

            job = k32.CreateJobObjectW(None, None)
            if not job:
                return
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
            ):
                k32.CloseHandle(job)
                return
            PROCESS_TERMINATE = 0x0001
            PROCESS_SET_QUOTA = 0x0100
            hproc = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
            if not hproc:
                k32.CloseHandle(job)
                return
            assigned = k32.AssignProcessToJobObject(job, hproc)
            k32.CloseHandle(hproc)
            if not assigned:
                k32.CloseHandle(job)
                return
            self._job = job  # keep open: closing (or process death) triggers the kill
            logger.info("本机 IndexTTS2 已绑定 Job Object（主程序无论如何退出都自动回收子进程）")
        except Exception as ex:
            logger.debug("Job Object 绑定失败（降级为显式 stop）: %s", ex)

    # ── stop ────────────────────────────────────────────────────────────────
    async def stop(self) -> None:
        """Terminate the managed child (only if we spawned it and stop_with_app)."""
        if not (self._managed and self.stop_with_app):
            return
        proc = self._proc
        if proc is not None:
            logger.info("正在关闭本机 IndexTTS2（随主程序退出）...")
            await asyncio.to_thread(self._kill_tree, proc)
        self._close_job()
        self._close_logf()
        self._proc = None
        self._managed = False

    def _kill_tree(self, proc: subprocess.Popen) -> None:
        try:
            if proc.poll() is not None:
                return
        except Exception:
            pass
        if sys.platform == "win32":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
                )
                return
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass
        else:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _close_job(self) -> None:
        if self._job is None:
            return
        try:
            import ctypes
            ctypes.WinDLL("kernel32").CloseHandle(self._job)
        except Exception:
            pass
        self._job = None

    def _close_logf(self) -> None:
        if self._logf is not None:
            try:
                self._logf.close()
            except Exception:
                pass
            self._logf = None

    async def _wait_ready(self, max_sec: float) -> bool:
        t0 = time.time()
        while time.time() - t0 < max_sec:
            if self._is_loaded(2.0):
                logger.info("本机 IndexTTS2 就绪（%.0fs）", time.time() - t0)
                return True
            await asyncio.sleep(3.0)
        logger.warning(
            "本机 IndexTTS2 在 %.0fs 内未就绪；继续启动（语音先回落 edge，就绪后自动恢复）", max_sec
        )
        return False
