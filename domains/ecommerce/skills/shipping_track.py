"""E-commerce shipping/logistics tracking skill."""

import re
from typing import Dict, Any, Optional

from src.skills.base import Skill


class ShippingTrackSkill(Skill):
    """Handles shipping inquiries: tracking, delivery status, delays."""

    def __init__(self, config, ai_client):
        super().__init__(config, ai_client)
        self.priority = 4

    _SHIPPING_KW = re.compile(
        r"快递|物流|发货|到货|运单|包裹|延误|没收到|配送|"
        r"shipping|delivery|tracking|parcel|courier|dispatch",
        re.IGNORECASE,
    )

    async def execute(self, text: str, user_id: str, context: Dict[str, Any]) -> Optional[str]:
        if not self._SHIPPING_KW.search(text or ""):
            return None

        try:
            reply = await self.ai_client.generate_reply_with_intent(
                user_message=text,
                intent="shipping_track",
                user_context=context,
                strategy_overrides=self._get_strategy_overrides(context),
            )
            if reply:
                return reply
        except Exception as e:
            self.logger.warning("Shipping track AI failed: %s", e)

        return self._kb_fallback("shipping_track")
