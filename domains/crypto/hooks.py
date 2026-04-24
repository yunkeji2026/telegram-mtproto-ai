"""
Crypto domain hook — token tickers, price queries, trading questions.
"""

import re
from typing import Any, Dict, List, Optional, Set

from src.hooks.base import DomainHook, HookContext

# Short tickers that may confuse language detection or intent
_TICKER_AMBIGUOUS = frozenset({
    "btc", "eth", "sol", "bnb", "okb", "doge", "pepe", "ai", "nft",
})

_PRICE_PAT = re.compile(
    r"价格|行情|涨跌|k线|走势|price|pump|dump|目标价|支撑|阻力",
    re.IGNORECASE,
)
_TRADE_PAT = re.compile(
    r"交易|合约|杠杆|现货|止损|止盈|仓位|开仓|平仓|做多|做空",
    re.IGNORECASE,
)


class CryptoDomainHook(DomainHook):
    """Crypto: detect tickers, price/trading-oriented queries."""

    def __init__(self, config=None):
        self._config = config

    async def on_intent_resolved(self, intent: str, ctx: HookContext) -> str:
        text = (ctx.text or "").strip()
        tl = text.lower()
        if _PRICE_PAT.search(text):
            return "market_discussion"
        if _TRADE_PAT.search(text):
            return "trading_education"
        if re.search(r"\b[a-z]{2,6}\b", tl):
            # Bare ticker-style tokens (very rough)
            words = re.findall(r"\b[a-z]{2,6}\b", tl)
            if words and all(w in _TICKER_AMBIGUOUS for w in words) and len(text) <= 32:
                return "token_lookup"
        return intent

    def get_narrow_reply_config(self) -> Optional[Dict[str, Any]]:
        return {
            "risk_reminder_substrings": [
                "投资", "买入", "卖出", "梭哈", "杠杆", "合约", "预测", "目标价",
            ],
        }

    def get_ambiguous_tokens(self) -> Set[str]:
        return set(_TICKER_AMBIGUOUS)

    def get_extra_intent_keywords(self) -> Dict[str, List[str]]:
        return {
            "market_discussion": ["行情", "价格", "走势", "分析"],
            "trading_education": ["交易", "止损", "杠杆", "仓位"],
            "token_lookup": ["代币", "symbol", "ticker"],
        }

    def is_domain_metrics_query(self, text: str) -> bool:
        t = (text or "").lower()
        return "价格" in (text or "") or "price" in t or "quote" in t
