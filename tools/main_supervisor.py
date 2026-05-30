"""main.py supervisor / watchdog — 自动重启 silent crash。

背景：2026-05-05 观察到 main.py 反复 silent crash（无 stderr / Traceback），
模式：跑 30~60 分钟后突然消失。无人值守时 bot 真消息无回复（用户报"为什么
没回 messenger 消息"）。

用法（前台运行，关掉控制台即停 supervisor）：
    python tools/main_supervisor.py

或后台（Windows）：
    Start-Process python -ArgumentList "tools/main_supervisor.py" `
        -WindowStyle Hidden -RedirectStandardOutput "logs/supervisor.log"

行为：
    - 每 30 秒检查 main.py 进程是否存在
    - 死了立即 spawn 新进程，stdout/stderr 重定向到 logs/main_*.log
    - 5 分钟内重启 ≥ 3 次 → 进入 60 秒冷却（防 main.py 配置问题导致死循环重启）
    - supervisor 自身异常捕获 + 写 logs/supervisor.log

不做的事：
    - 不限制 main.py 内存 / CPU（依赖 OS）
    - 不诊断死亡根因（在 logs/main_stderr.log 里看，但通常空）
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

WORKDIR = Path(__file__).resolve().parent.parent
LOG_DIR = WORKDIR / "logs"
SUPERVISOR_LOG = LOG_DIR / "supervisor.log"
MAIN_STDOUT = LOG_DIR / "main_stdout.log"
MAIN_STDERR = LOG_DIR / "main_stderr.log"

CHECK_INTERVAL_SEC = 30
RESTART_COOLDOWN_SEC = 60
RESTART_BURST_THRESHOLD = 3      # 5 分钟内 >= 3 次重启进入冷却
RESTART_BURST_WINDOW_SEC = 300


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    print(line, end="")
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with SUPERVISOR_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def is_main_alive() -> bool:
    """Windows 下用 wmic 查 main.py 进程；Linux/Mac 用 ps -ef。"""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["wmic", "process", "where", "Name='python.exe'",
                 "get", "CommandLine"],
                capture_output=True, text=True, timeout=10,
            ).stdout
        else:
            out = subprocess.run(
                ["ps", "-ef"], capture_output=True, text=True, timeout=10,
            ).stdout
        return any("main.py" in line for line in out.splitlines())
    except Exception as ex:
        _log(f"[is_main_alive ERROR] {type(ex).__name__}: {ex}")
        # 失败时假装活着，避免误重启
        return True


def spawn_main() -> int:
    """spawn 新 main.py 进程，返回 PID。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stdout_f = MAIN_STDOUT.open("a", encoding="utf-8")
    stderr_f = MAIN_STDERR.open("a", encoding="utf-8")
    kwargs: dict = {
        "cwd": str(WORKDIR),
        "stdout": stdout_f,
        "stderr": stderr_f,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    p = subprocess.Popen(
        [sys.executable, "main.py"], **kwargs,
    )
    return p.pid


def main() -> None:
    _log("supervisor started")
    restart_history: deque = deque(maxlen=RESTART_BURST_THRESHOLD + 2)

    while True:
        try:
            if not is_main_alive():
                now = time.time()
                # 检查 burst：5 分钟内重启次数
                recent = [t for t in restart_history if now - t < RESTART_BURST_WINDOW_SEC]
                if len(recent) >= RESTART_BURST_THRESHOLD:
                    _log(
                        f"⚠️ {len(recent)} restarts in last "
                        f"{RESTART_BURST_WINDOW_SEC}s — cooling down "
                        f"{RESTART_COOLDOWN_SEC}s"
                    )
                    time.sleep(RESTART_COOLDOWN_SEC)
                    continue

                _log("main.py DEAD, spawning new process...")
                try:
                    pid = spawn_main()
                    restart_history.append(now)
                    _log(f"spawned PID={pid}")
                    # 给 20s 初始化时间
                    time.sleep(20)
                except Exception as ex:
                    _log(f"[spawn_main ERROR] {type(ex).__name__}: {ex}")
                    time.sleep(RESTART_COOLDOWN_SEC)
            time.sleep(CHECK_INTERVAL_SEC)
        except KeyboardInterrupt:
            _log("supervisor stopped by user (Ctrl+C)")
            return
        except Exception as ex:
            _log(f"[supervisor LOOP ERROR] {type(ex).__name__}: {ex}")
            time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
