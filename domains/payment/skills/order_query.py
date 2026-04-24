"""订单查询技能"""

import re
from typing import Dict, Any, Optional

from src.skills.base import Skill


class OrderQuerySkill(Skill):
    """订单查询技能"""

    def __init__(self, config, ai_client):
        super().__init__(config, ai_client)
        self.priority = 3

    async def execute(self, text, user_id, context):
        """处理订单查询"""
        order_patterns = [r'订单\s*[：:]\s*(\S+)', r'订单号\s*[：:]\s*(\S+)', r'(\d{10,})']
        order_number = None

        for pattern in order_patterns:
            match = re.search(pattern, text)
            if match:
                order_number = match.group(1) if len(match.groups()) > 0 else match.group(0)
                break

        try:
            reply = await self.ai_client.generate_reply_with_intent(
                user_message=text,
                intent='order_query',
                user_context=context,
                strategy_overrides=self._get_strategy_overrides(context)
            )
            if reply:
                return reply
        except Exception as e:
            self.logger.warning(f"AI生成订单查询回复失败: {e}")

        if order_number:
            r = self._kb_reply("order_query_with_number_fallback", order_number=order_number)
            return r or self._kb_fallback("order_query")
        return self._kb_fallback("order_query")
