"""E-commerce product query skill."""

import re
from typing import Dict, Any, Optional

from src.skills.base import Skill


class ProductQuerySkill(Skill):
    """Handles product inquiries: specs, pricing, availability, recommendations."""

    def __init__(self, config, ai_client):
        super().__init__(config, ai_client)
        self.priority = 3

    _PRODUCT_KW = re.compile(
        r"商品|产品|规格|尺寸|颜色|价格|多少钱|有货|库存|型号|推荐|"
        r"product|price|stock|size|color|specs|recommend",
        re.IGNORECASE,
    )

    async def execute(self, text: str, user_id: str, context: Dict[str, Any]) -> Optional[str]:
        if not self._PRODUCT_KW.search(text or ""):
            return None

        try:
            reply = await self.ai_client.generate_reply_with_intent(
                user_message=text,
                intent="product_query",
                user_context=context,
                strategy_overrides=self._get_strategy_overrides(context),
            )
            if reply:
                return reply
        except Exception as e:
            self.logger.warning("Product query AI failed: %s", e)

        return self._kb_fallback("product_query")
