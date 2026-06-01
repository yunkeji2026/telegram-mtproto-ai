"""统一收件箱持久层（Phase A：统一数据地基）。

对外暴露：
- models：InboxConversation / InboxMessage / MessageAnalysis 数据类
- store：InboxStore（SQLite，复刻 ContactStore 范式）
- ingest：把 unified_inbox 现有聚合结果旁路写入持久层的映射函数

设计原则（见 docs/实现设计_PhaseA_统一数据地基.md）：
- 纯旁路 + 可回落：store 不可用/为空时，调用方自动退回原实时聚合逻辑，
  收件箱始终可用。
- 不破坏 RPA 主线：本层只读各平台聚合结果再落库，不改任何 runner。
"""

from .models import InboxConversation, InboxMessage, MessageAnalysis
from .store import InboxStore

__all__ = [
    "InboxConversation",
    "InboxMessage",
    "MessageAnalysis",
    "InboxStore",
]
