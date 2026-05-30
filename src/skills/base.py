"""
Skill base class — shared by all domain skills and generic skills.
Extracted from skill_manager.py to avoid circular imports.
"""

import random
from typing import Dict, Any, Optional

from src.utils.logger import LoggerMixin
from src.utils.domain_policy import effective_domain_name
from src.ai.ai_client import AIClient


class Skill(LoggerMixin):
    """Skill base class"""

    def __init__(self, config, ai_client: AIClient):
        self.config = config
        self.ai_client = ai_client
        self.name = self.__class__.__name__
        self.priority = 5

    def _get_strategy_overrides(self, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        strategy = context.get('_reply_strategy', {})
        so = {}
        if 'temperature' in strategy and strategy['temperature']:
            so['temperature'] = strategy['temperature']
        if 'max_tokens' in strategy and strategy['max_tokens']:
            so['max_tokens'] = strategy['max_tokens']
        if 'context_rounds' in strategy:
            so['context_rounds'] = strategy['context_rounds']
        if strategy.get('model'):
            so['model'] = strategy['model']
        if 'thinking_budget' in strategy:
            so['thinking_budget'] = strategy['thinking_budget']
        return so or None

    async def execute(
        self,
        text: str,
        user_id: str,
        context: Dict[str, Any]
    ) -> Optional[str]:
        raise NotImplementedError("子类必须实现execute方法")

    def _get_kb_store(self):
        try:
            from src.utils.kb_registry import get_kb_store
            return get_kb_store(self.config, require_exists=True)
        except Exception:
            return None

    def _kb_reply(self, template_key: str, **kwargs) -> Optional[str]:
        kb = self._get_kb_store()
        if kb:
            return kb.get_direct_reply(template_key, **kwargs)
        return None

    _FALLBACK_EN = {
        "greeting": "Hi there! I'm Camille, how can I help you?",
        "channel_info": "Let me check the channel status for you.",
        "complaint": "I understand your concern. Let me look into this for you right away.",
        "order_query": "Let me check your order status. Could you share the order number or payment screenshot?",
        "small_talk": "Hi! I'm here to help with orders, channel status, and payment inquiries.",
        "status_check": "Let me check the current status for you.",
        "default": "Hi, how can I assist you?",
    }

    def _kb_fallback(self, intent: str, lang: str = "zh") -> str:
        kb = self._get_kb_store()
        if kb:
            reply = kb.get_fallback(intent)
            if reply:
                if lang and lang != "zh":
                    return self._FALLBACK_EN.get(intent, self._FALLBACK_EN["default"])
                return reply
        if lang and lang != "zh":
            return self._FALLBACK_EN.get(intent, self._FALLBACK_EN["default"])
        try:
            cfg = self.config.config if hasattr(self.config, "config") else {}
            if isinstance(cfg, dict) and effective_domain_name(cfg) == "conversion":
                return random.choice(
                    [
                        "嗯嗯我在～怎么啦？",
                        "在呀，找我呢？",
                        "在的，你说～",
                    ]
                )
        except Exception:
            pass
        return "在呀，你说～"  # S3: 改为伴侣风格，去客服腔

    def _get_template_reply(self, template_name: str, context: Optional[Dict[str, Any]] = None) -> Optional[str]:
        reply = self._kb_reply(template_name, **(context or {}))
        if reply:
            return reply
        from src.utils.template_engine import render_template
        if hasattr(self.config, 'get_dynamic_templates_config'):
            templates_config = self.config.get_dynamic_templates_config() or {}
            template_list = templates_config.get(template_name)
            if template_list is None:
                templates_config = self.config.get_templates_config()
                template_list = templates_config.get(template_name, [])
        else:
            templates_config = self.config.get_templates_config()
            template_list = templates_config.get(template_name, [])
        if isinstance(template_list, str):
            template_list = [template_list]
        if template_list:
            raw = random.choice(template_list)
            return render_template(raw, context) if context else raw
        return None
