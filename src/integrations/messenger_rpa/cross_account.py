"""跨账号协调器：共享画像缓存 + 同一用户同时只允许一个账号聊天。

设计原则：
- 纯 asyncio 单线程操作，dict 修改在事件循环内原子，无须额外 Lock
- 轻量级：不持有 DB 连接，所有状态内存化；重启后自然重建
- 失效安全：coordinator 为 None 时各账号独立运行，不影响正常流程
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class CrossAccountCoordinator:
    """多账号共享协调器，单例由 MessengerRpaService 持有并注入所有 Runner。

    两大功能：
    1. **同用户聊天互斥**：同一 external_id（用户名）同时只能由一个账号处理。
       Runner 进入 per-chat 处理段前调 try_lock，结束时调 unlock。
    2. **跨账号画像共享**：任一账号提取了用户画像后推给 coordinator；
       其他账号在 prompt 注入时优先从 coordinator 取，免重复 LLM 调用。
    """

    def __init__(self) -> None:
        # external_id → account_id（当前持有该用户聊天权的账号）
        self._active_chats: Dict[str, str] = {}
        self._active_since: Dict[str, float] = {}
        self._lock_ttl_sec = 180.0
        # external_id → {account_id, portrait_json, ts}
        self._shared_portrait: Dict[str, Dict[str, Any]] = {}

    # ── 聊天互斥锁 ─────────────────────────────────────────────────

    def try_lock(self, external_id: str, account_id: str) -> bool:
        """尝试为 account_id 锁定对 external_id 的聊天权。

        asyncio 单线程安全（无 await，dict 操作原子）。
        同账号可重入返回 True；被其他账号占用返回 False。
        """
        if not external_id:
            return True  # 无 external_id 时不参与互斥
        holder = self._active_chats.get(external_id)
        if holder is not None:
            since = float(self._active_since.get(external_id) or 0.0)
            if since and (time.time() - since) > self._lock_ttl_sec:
                logger.warning(
                    "[cross_account] stale chat lock expired external_id=%r holder=%s",
                    external_id, holder,
                )
                self._active_chats.pop(external_id, None)
                self._active_since.pop(external_id, None)
                holder = None
        if holder is not None and holder != account_id:
            return False
        self._active_chats[external_id] = account_id
        self._active_since[external_id] = time.time()
        return True

    def unlock(self, external_id: str, account_id: str) -> None:
        """释放聊天锁（只释放自己持有的，防止意外释放他人锁）。"""
        if external_id and self._active_chats.get(external_id) == account_id:
            del self._active_chats[external_id]
            self._active_since.pop(external_id, None)

    def active_chat_holder(self, external_id: str) -> Optional[str]:
        """返回当前持有该用户聊天锁的 account_id；无则 None。"""
        return self._active_chats.get(external_id)

    # ── 跨账号共享画像缓存 ──────────────────────────────────────────

    def update_portrait(
        self,
        external_id: str,
        account_id: str,
        portrait_json: str,
        ts: float = 0.0,
    ) -> None:
        """某账号提取了最新画像，推送到全局缓存（只保留最新的）。"""
        if not external_id or not portrait_json:
            return
        ts = ts or time.time()
        existing = self._shared_portrait.get(external_id)
        if existing is None or ts >= existing.get("ts", 0.0):
            self._shared_portrait[external_id] = {
                "account_id": account_id,
                "portrait_json": portrait_json,
                "ts": ts,
            }

    def get_portrait(self, external_id: str) -> Optional[str]:
        """获取该用户最新的跨账号共享画像 JSON（任意账号提取均可）。"""
        entry = self._shared_portrait.get(external_id)
        return entry["portrait_json"] if entry else None

    # ── 运营可视化快照 ──────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """运营看板快照：当前活跃聊天 + 画像缓存概况。"""
        now = time.time()
        return {
            "active_chats": dict(self._active_chats),
            "active_chats_count": len(self._active_chats),
            "portrait_cache_count": len(self._shared_portrait),
            "portrait_cache": {
                eid: {
                    "account_id": v["account_id"],
                    "ts": v["ts"],
                    "age_sec": int(now - v["ts"]),
                }
                for eid, v in self._shared_portrait.items()
            },
        }
