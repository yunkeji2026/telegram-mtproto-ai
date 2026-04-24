"""状态查询技能"""

from typing import Dict, Any, Optional

from src.skills.base import Skill


class StatusCheckSkill(Skill):
    """状态查询技能"""

    def __init__(self, config, ai_client):
        super().__init__(config, ai_client)
        self.priority = 5

    async def execute(self, text, user_id, context):
        try:
            reply = await self.ai_client.generate_reply_with_intent(
                user_message=text, intent='status_check',
                user_context=context,
                strategy_overrides=self._get_strategy_overrides(context)
            )
            if reply:
                return reply
        except Exception as e:
            self.logger.warning(f"AI生成状态查询回复失败: {e}")
        return self._kb_fallback("status_check")
