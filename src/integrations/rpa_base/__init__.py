"""RPA 跨平台基类与共用工具

本包不包含任何特定平台的业务逻辑，仅提供：

- 协议（`protocols`）—— `typing.Protocol`，用于类型化 web 路由 / 总览
  页面对 4 个 RPA service 的统一访问。

- 数据结构（`types`）—— dataclass + Enum，刻画 4 平台共同的概念
  （RpaPlatform、PendingStatus、PendingItem、AlertItem、RpaStatusSummary）。

- 通用工具（`daily_cap`）—— 线程安全的每日上限跟踪器，便于跨平台复用。

现有 4 个 service（telegram / line_rpa / messenger_rpa / whatsapp_rpa）
**不需要**重构即可与本模块协作；本模块是"鸭子类型 + 显式契约"风格。
"""

from .protocols import (
    RpaService,
    RpaStateStore,
    RpaServiceWithPending,
    RpaServiceWithAlerts,
)
from .types import (
    RpaPlatform,
    PendingStatus,
    AlertSeverity,
    PendingItem,
    AlertItem,
    RpaStatusSummary,
)
from .daily_cap import DailyCapTracker

__all__ = [
    # protocols
    "RpaService",
    "RpaStateStore",
    "RpaServiceWithPending",
    "RpaServiceWithAlerts",
    # types
    "RpaPlatform",
    "PendingStatus",
    "AlertSeverity",
    "PendingItem",
    "AlertItem",
    "RpaStatusSummary",
    # tools
    "DailyCapTracker",
]
