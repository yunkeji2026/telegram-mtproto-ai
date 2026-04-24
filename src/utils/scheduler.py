"""轻量异步定时任务调度器 — 纯 asyncio，无外部依赖"""

import asyncio
import logging
import time
from typing import Callable, Dict, List, Optional, Any

logger = logging.getLogger("Scheduler")


class ScheduledTask:
    __slots__ = ("name", "interval", "chat_id", "command", "callback",
                 "enabled", "_last_run", "_handle")

    def __init__(self, name: str, interval: int, chat_id: int,
                 command: str, callback: Callable, enabled: bool = True):
        self.name = name
        self.interval = max(interval, 30)
        self.chat_id = chat_id
        self.command = command
        self.callback = callback
        self.enabled = enabled
        self._last_run: float = 0
        self._handle: Optional[asyncio.Task] = None


class TaskScheduler:

    def __init__(self):
        self._tasks: Dict[str, ScheduledTask] = {}
        self._running = False

    def add_task(self, name: str, interval: int, chat_id: int,
                 command: str, callback: Callable, enabled: bool = True):
        task = ScheduledTask(name, interval, chat_id, command, callback, enabled)
        self._tasks[name] = task
        logger.info("定时任务已注册: %s (每 %ds → chat %s: %s)", name, interval, chat_id, command)

    def remove_task(self, name: str):
        task = self._tasks.pop(name, None)
        if task and task._handle:
            task._handle.cancel()

    def start(self):
        if self._running:
            return
        self._running = True
        for task in self._tasks.values():
            if task.enabled:
                task._handle = asyncio.get_running_loop().create_task(self._run_loop(task))
        logger.info("调度器已启动，%d 个任务", len(self._tasks))

    def stop(self):
        self._running = False
        for task in self._tasks.values():
            if task._handle:
                task._handle.cancel()
                task._handle = None

    async def _run_loop(self, task: ScheduledTask):
        await asyncio.sleep(min(task.interval, 60))
        while self._running and task.enabled:
            try:
                await task.callback(task.chat_id, task.command)
                task._last_run = time.time()
                logger.debug("定时任务执行: %s → %s", task.name, task.command)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("定时任务 %s 执行失败: %s", task.name, e)
            await asyncio.sleep(task.interval)

    def list_tasks(self) -> List[Dict[str, Any]]:
        result = []
        for t in self._tasks.values():
            result.append({
                "name": t.name,
                "interval": t.interval,
                "chat_id": t.chat_id,
                "command": t.command,
                "enabled": t.enabled,
                "last_run": time.strftime("%H:%M:%S", time.localtime(t._last_run)) if t._last_run else "-",
            })
        return result

    @classmethod
    def from_config(cls, config: dict, send_callback: Callable) -> "TaskScheduler":
        scheduler = cls()
        tasks_cfg = config.get("scheduled_tasks", {})
        if not tasks_cfg.get("enabled"):
            return scheduler
        for item in tasks_cfg.get("tasks", []):
            name = item.get("name", "unnamed")
            interval = int(item.get("interval_seconds", 3600))
            chat_id = int(item.get("chat_id", 0))
            command = item.get("command", "")
            enabled = item.get("enabled", True)
            if chat_id and command:
                scheduler.add_task(name, interval, chat_id, command, send_callback, enabled)
        return scheduler
