"""多 Bot 协同配置框架 — 按群 ID 路由到不同 session/账号"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger("MultiBot")


class BotRouter:
    """根据 chat_id 决定由哪个 bot session 处理消息"""

    def __init__(self, config: dict):
        self._routes: Dict[int, str] = {}
        self._default_session: str = ""
        self._sessions: List[str] = []
        self._load(config)

    def _load(self, config: dict):
        mb = config.get("multi_bot", {})
        if not mb.get("enabled"):
            return
        self._default_session = mb.get("default_session", "")
        for rule in mb.get("routes", []):
            session = rule.get("session", "")
            for cid in rule.get("chat_ids", []):
                self._routes[int(cid)] = session
        self._sessions = list(set(
            [self._default_session] + list(self._routes.values())
        ))
        if self._routes:
            logger.info("多 Bot 路由已加载: %d 条规则, %d 个 session",
                        len(self._routes), len(self._sessions))

    @property
    def enabled(self) -> bool:
        return bool(self._routes)

    @property
    def sessions(self) -> List[str]:
        return self._sessions

    def get_session(self, chat_id: int) -> str:
        return self._routes.get(chat_id, self._default_session)

    def should_handle(self, chat_id: int, current_session: str) -> bool:
        target = self.get_session(chat_id)
        if not target:
            return True
        return target == current_session

    def list_routes(self) -> List[Dict]:
        result = []
        for cid, session in self._routes.items():
            result.append({"chat_id": cid, "session": session})
        return result
