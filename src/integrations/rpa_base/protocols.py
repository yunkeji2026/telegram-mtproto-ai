"""RPA service / state_store 的统一接口契约

使用 `typing.Protocol` 做结构子类型（duck typing），4 个现有 service
**无需继承**即可满足契约。

主要消费者：
- web 路由（统一处理逻辑）
- 跨平台总览页（聚合 4 个平台数据）
- 健康检查 / metrics 收集

注意：这里**只声明**接口，不实现任何业务。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


# ════════════════════════════════════════════════════════════════════════
# Service Protocols
# ════════════════════════════════════════════════════════════════════════


@runtime_checkable
class RpaService(Protocol):
    """所有 RPA service 应该满足的最小接口。

    实测对照（基于现有 4 个 service 的方法签名）：

    | 方法 | LINE | WhatsApp | Messenger | Telegram |
    |------|------|----------|-----------|----------|
    | status() | ✓ | ✓ | ✓ | (经 admin 聚合) |
    | effective_config() | ✓ | ✓ | (raw_cfg) | (config.yaml 直读) |
    | pause_for(s) | ✓ | ✓ | ✓ | (N/A) |
    | resume() | ✓ | ✓ | ✓ | (N/A) |
    | trigger_once() | ✓ | ✓ | ✓ | (N/A) |
    | recent_runs(limit, only_with_peer) | ✓ | ✓ | (recent_runs(limit)) | (N/A) |

    Telegram 因为是 MTProto 直发（不是 RPA 轮询模式），它的"running"
    状态由 telegram_client.running 暴露，没有 service 对象。这是预期
    差异：本 Protocol 不强制 Telegram 实现，由调用方做平台兼容。
    """

    def status(self) -> Dict[str, Any]:
        """返回当前状态（应包含 running/paused/available 等字段）。

        最小字段集：
        - available: bool
        - enabled: bool
        - running: bool
        - paused: bool
        - reply_mode: str (auto/approve/off)
        - stats_24h: dict 包含 sent/total/avg_ms

        其他字段（pending_count, daily_cap, daily_sent, unacked_alerts,
        pause_remaining_sec, hint）按需返回。
        """
        ...

    def effective_config(self) -> Dict[str, Any]:
        """返回当前生效的合并配置（config.yaml + 实时 PUT 的累积）。"""
        ...

    def pause_for(self, seconds: float) -> None:
        """暂停 N 秒。0 表示立即恢复。"""
        ...

    def resume(self) -> None:
        """立即恢复运行。"""
        ...


@runtime_checkable
class RpaServiceWithPending(RpaService, Protocol):
    """带审核队列能力的 RPA service。

    支持 reply_mode=approve 的平台必须实现。
    """

    def list_pending(
        self, *, status: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """列出审核队列项。status 可选过滤。"""
        ...

    def resolve_pending(
        self,
        pending_id: int,
        action: str,
        *,
        text: Optional[str] = None,
        by: str = "",
    ) -> Optional[Dict[str, Any]]:
        """处置审核队列项。

        - action='approve': 批准（可附带 text 覆盖原回复文本）
        - action='reject': 拒绝
        - action='send': 直接标记已发送（调用方已确认发出）

        返回更新后的记录 dict，或 None 表示未找到。
        """
        ...

    def pending_stats(self) -> Dict[str, int]:
        """按 status 分组的计数 dict，如 {'pending':3,'sent':10,'rejected':1}"""
        ...


@runtime_checkable
class RpaServiceWithAlerts(RpaService, Protocol):
    """带告警闭环的 RPA service。"""

    def list_alerts(
        self, *, only_unacked: bool = True, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """列出告警。only_unacked=True 仅返回未确认的。"""
        ...

    def ack_alert(self, alert_id: int, *, by: str = "") -> Optional[Dict[str, Any]]:
        """确认单条告警。"""
        ...

    def ack_all_alerts(self, *, by: str = "") -> int:
        """确认所有未确认告警。返回受影响的条数。"""
        ...


# ════════════════════════════════════════════════════════════════════════
# State Store Protocols (低层，DB 操作层)
# ════════════════════════════════════════════════════════════════════════


@runtime_checkable
class RpaStateStore(Protocol):
    """RPA state_store 的最小接口。

    每个 RPA 模块有自己的 sqlite DB（line_rpa_state.db / wa_rpa_state.db
    / messenger_rpa_state.db），schema 各不相同，但都暴露这几个核心方法。
    """

    def recent_runs(
        self, limit: int = 50, *, only_with_peer: bool = False
    ) -> List[Dict[str, Any]]:
        """最近 N 条 run 记录（按 ts 倒序）。"""
        ...

    def run_stats(self, hours: float = 24.0) -> Dict[str, Any]:
        """按时间窗聚合统计：sent / total / ok / avg_ms 等。"""
        ...
