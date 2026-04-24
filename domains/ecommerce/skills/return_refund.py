"""E-commerce return/refund processing skill."""

import re
from typing import Dict, Any, Optional

from src.skills.base import Skill


class ReturnRefundSkill(Skill):
    """Handles return and refund requests."""

    def __init__(self, config, ai_client):
        super().__init__(config, ai_client)
        self.priority = 5

    _RETURN_KW = re.compile(
        r"退款|退货|退钱|换货|退换|运费险|售后|不满意|质量问题|"
        r"refund|return|exchange|warranty|defective",
        re.IGNORECASE,
    )

    async def execute(self, text: str, user_id: str, context: Dict[str, Any]) -> Optional[str]:
        if not self._RETURN_KW.search(text or ""):
            return None

        try:
            reply = await self.ai_client.generate_reply_with_intent(
                user_message=text,
                intent="return_refund",
                user_context=context,
                strategy_overrides=self._get_strategy_overrides(context),
            )
            if reply:
                return reply
        except Exception as e:
            self.logger.warning("Return/refund AI failed: %s", e)

        return self._kb_fallback("return_refund")
