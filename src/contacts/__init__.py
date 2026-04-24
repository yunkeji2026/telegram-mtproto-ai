"""跨平台 Contact / Journey / HandoffToken 核心模块。

一个 Contact 代表一个"人"（跨 Messenger / LINE / Telegram），
通过 ChannelIdentity 持有他在各平台的身份。HandoffToken 是
Messenger 引流到 LINE 时使用的短码，LINE 端消费 token 即可完成身份合并。
"""

from .models import (
    Contact,
    ChannelIdentity,
    HandoffToken,
    Journey,
    MergeSignals,
    MergeDecision,
)
from .store import ContactStore
from .handoff import HandoffTokenService, TokenError, TokenExpired, TokenAlreadyConsumed
from .merge import MergeService, score_signals
from .gateway import (
    ContactGateway, JourneyContext, MergeOutcome, HandoffAttempt, new_trace_id,
)
from .rpa_hooks import (
    BeforeReplyDecision, ContactHooks, GatewayContactHooks, NoopContactHooks,
)
from .bootstrap import ContactsSubsystem, bootstrap_contacts_subsystem
from .ids import new_id, new_token

__all__ = [
    "Contact",
    "ChannelIdentity",
    "HandoffToken",
    "Journey",
    "MergeSignals",
    "MergeDecision",
    "ContactStore",
    "HandoffTokenService",
    "TokenError",
    "TokenExpired",
    "TokenAlreadyConsumed",
    "MergeService",
    "score_signals",
    "ContactGateway",
    "JourneyContext",
    "MergeOutcome",
    "HandoffAttempt",
    "new_trace_id",
    "BeforeReplyDecision",
    "ContactHooks",
    "GatewayContactHooks",
    "NoopContactHooks",
    "ContactsSubsystem",
    "bootstrap_contacts_subsystem",
    "new_id",
    "new_token",
]
