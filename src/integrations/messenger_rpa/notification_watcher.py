"""Messenger 通知监听器（轻量、无 root）。

原理：
    持续 `adb shell dumpsys notification --noredact | grep pkg=com.facebook.orca`
    diff 出"上一秒不存在但这一秒存在"的新通知 → 触发 trigger。
    
为什么不用 vision 30s 轮询？
    - 30s 内对方发消息：要等到下一轮才回，最差延迟 30s，平均 15s
    - 通知 diff：500ms tick，平均延迟 0.25s，且没消息时几乎 0 成本
    - 30s 一次 vision = 0.4 元成本/小时 vs notification listener ≈ 0 成本

无 root 限制：
    - 部分 MIUI/HyperOS 默认对 dumpsys notification 做了脱敏（content 不可读），
      但 NotificationRecord(...) 的元数据（id/tag/key）始终可读，足够判 "有新消息"
    - 通知锁屏隐藏（vis=PRIVATE）也不影响 dump，只影响锁屏可见
    - 只能感知"通知系统知道有新消息" —— 静音/勿扰场景下 Messenger 仍可能发通知

调用：
    watcher = MessengerNotificationWatcher(serial="192.168.0.113:5555")
    async for evt in watcher.watch():
        print(evt)  # {"type": "new", "key": "...", "user": 0}
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, Optional, Set

from src.integrations.line_rpa import adb_helpers as adb

logger = logging.getLogger(__name__)


_RE_NOTIF_RECORD = re.compile(
    r"NotificationRecord\(0x[0-9a-fA-F]+:\s+pkg=(?P<pkg>\S+)\s+"
    r"user=UserHandle\{(?P<user>-?\d+)\}\s+"
    r"id=(?P<id>-?\d+)\s+"
    r"tag=(?P<tag>\S+)\s+"
    r"importance=(?P<imp>\d+)\s+"
    r"key=(?P<key>[^\s:]+)"
)


@dataclass
class NotifEvent:
    type: str  # "new" / "removed" / "snapshot"
    pkg: str
    user_id: int
    notif_id: int
    tag: str
    key: str
    ts: float = field(default_factory=time.time)


def _dump_messenger_notif_keys(
    serial: str, target_pkg: str = "com.facebook.orca"
) -> Set[str]:
    """单次快照：返回 com.facebook.orca 当前活跃通知的 key 集合。"""
    r = adb.run_adb(
        ["shell", "dumpsys", "notification", "--noredact"],
        serial=serial,
        timeout=10.0,
    )
    if r.returncode != 0:
        logger.debug("dumpsys notification 失败: %s", r.stderr)
        return set()
    keys: Set[str] = set()
    for line in (r.stdout or "").splitlines():
        if "NotificationRecord" not in line or target_pkg not in line:
            continue
        m = _RE_NOTIF_RECORD.search(line)
        if m and m.group("pkg") == target_pkg:
            keys.add(m.group("key"))
    return keys


def _parse_notif_record(line: str) -> Optional[NotifEvent]:
    m = _RE_NOTIF_RECORD.search(line)
    if not m:
        return None
    return NotifEvent(
        type="snapshot",
        pkg=m.group("pkg"),
        user_id=int(m.group("user")),
        notif_id=int(m.group("id")),
        tag=m.group("tag"),
        key=m.group("key"),
    )


class MessengerNotificationWatcher:
    """轻量异步 watcher。

    使用方式：
        watcher = MessengerNotificationWatcher("192.168.0.113:5555")
        async for evt in watcher.watch(poll_ms=500, target_user=0):
            if evt.type == "new":
                run_once_callback()

    设计：
        - 启动时拍一张 baseline 快照，**不触发**任何 new 事件（避免冷启动洪峰）
        - 之后每 poll_ms 拍一次，与 baseline diff
        - 新增 key → emit "new"，移除 key → emit "removed"，更新 baseline
    """

    def __init__(
        self,
        serial: str,
        *,
        target_pkg: str = "com.facebook.orca",
        target_user: Optional[int] = None,
    ) -> None:
        self.serial = serial
        self.target_pkg = target_pkg
        self.target_user = target_user
        self._stop = False
        self._baseline: Set[str] = set()

    def stop(self) -> None:
        self._stop = True

    async def watch(
        self, *, poll_ms: int = 500, max_idle_ms: Optional[int] = None
    ) -> AsyncIterator[NotifEvent]:
        """持续 yield NotifEvent。max_idle_ms 时长内无事件自动结束（None=不结束）。"""
        loop = asyncio.get_running_loop()
        # ★ 用独立线程池跑 dump，避免 producer/RPA 业务 task 排队抢 default executor
        from concurrent.futures import ThreadPoolExecutor
        dump_pool = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="notif_watch_dump"
        )
        self._baseline = await loop.run_in_executor(
            dump_pool, _dump_messenger_notif_keys, self.serial, self.target_pkg
        )
        logger.info(
            "[notif_watcher] baseline keys=%d serial=%s pkg=%s",
            len(self._baseline), self.serial, self.target_pkg,
        )
        last_event_ts = time.time()

        while not self._stop:
            await asyncio.sleep(poll_ms / 1000.0)
            try:
                cur = await loop.run_in_executor(
                    dump_pool, _dump_messenger_notif_keys, self.serial, self.target_pkg
                )
            except Exception:
                logger.exception("[notif_watcher] dump 异常")
                continue

            new_keys = cur - self._baseline
            removed_keys = self._baseline - cur
            if new_keys or removed_keys:
                logger.debug(
                    "[notif_watcher] tick base=%d cur=%d new=%d rm=%d",
                    len(self._baseline), len(cur), len(new_keys), len(removed_keys),
                )

            for key in new_keys:
                user_id = self._parse_user_from_key(key)
                if (
                    self.target_user is not None
                    and user_id is not None
                    and user_id != self.target_user
                ):
                    continue
                evt = NotifEvent(
                    type="new",
                    pkg=self.target_pkg,
                    user_id=user_id if user_id is not None else -1,
                    notif_id=self._parse_id_from_key(key),
                    tag="",
                    key=key,
                )
                last_event_ts = time.time()
                yield evt

            for key in removed_keys:
                user_id = self._parse_user_from_key(key)
                if (
                    self.target_user is not None
                    and user_id is not None
                    and user_id != self.target_user
                ):
                    continue
                evt = NotifEvent(
                    type="removed",
                    pkg=self.target_pkg,
                    user_id=user_id if user_id is not None else -1,
                    notif_id=self._parse_id_from_key(key),
                    tag="",
                    key=key,
                )
                last_event_ts = time.time()
                yield evt

            if cur != self._baseline:
                self._baseline = cur

            if max_idle_ms is not None:
                if (time.time() - last_event_ts) * 1000 > max_idle_ms:
                    logger.info(
                        "[notif_watcher] idle %dms 超限，退出 watch",
                        max_idle_ms,
                    )
                    break

        dump_pool.shutdown(wait=False)

    @staticmethod
    def _parse_user_from_key(key: str) -> Optional[int]:
        # key 格式: <user>|<pkg>|<id>|<tag>|<uid>
        try:
            return int(key.split("|", 1)[0])
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _parse_id_from_key(key: str) -> int:
        try:
            return int(key.split("|")[2])
        except (ValueError, IndexError):
            return 0


__all__ = ["MessengerNotificationWatcher", "NotifEvent"]
