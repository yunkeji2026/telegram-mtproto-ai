"""4 平台共通的数据结构

设计原则：
- 全部用 `dataclass(frozen=False)` —— 4 个 service 返回的 dict 可直接
  `RpaStatusSummary(**status_dict)` 转换，不需要逐字段映射。
- 字段都有默认值或 `Optional`，让"部分字段缺失"也能构造。
- 不引入业务逻辑，纯数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ════════════════════════════════════════════════════════════════════════
# Enums
# ════════════════════════════════════════════════════════════════════════


class RpaPlatform(str, Enum):
    """4 个 RPA 平台标识。值与 web 路由前缀对齐。"""

    TELEGRAM = "telegram"
    LINE = "line"
    MESSENGER = "messenger"
    WHATSAPP = "whatsapp"

    @property
    def api_prefix(self) -> str:
        """对应的 web API 路径前缀。"""
        mapping = {
            RpaPlatform.TELEGRAM: "/api/telegram",
            RpaPlatform.LINE: "/api/line-rpa",
            RpaPlatform.MESSENGER: "/api/messenger-rpa",
            RpaPlatform.WHATSAPP: "/api/whatsapp-rpa",
        }
        return mapping[self]

    @property
    def display_name(self) -> str:
        return {
            RpaPlatform.TELEGRAM: "Telegram",
            RpaPlatform.LINE: "LINE",
            RpaPlatform.MESSENGER: "Messenger",
            RpaPlatform.WHATSAPP: "WhatsApp",
        }[self]


class PendingStatus(str, Enum):
    """审核队列项的状态。覆盖 LINE / WhatsApp / Messenger 三家命名。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT = "sent"
    ERROR = "error"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    # Messenger 特有：陪护模式延迟发送
    DEFERRED = "deferred"


class AlertSeverity(str, Enum):
    """告警严重程度。"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# ════════════════════════════════════════════════════════════════════════
# Dataclasses
# ════════════════════════════════════════════════════════════════════════


@dataclass
class PendingItem:
    """审核队列单条记录。

    各平台返回 dict 的字段名略有差异：
    - LINE:      ts / peer_text / proposed_reply / chat_key / status
    - WhatsApp:  ts / peer_text / proposed_reply / chat_key / status
    - Messenger: created_at / peer_text / reply_text / chat_key / status

    本 dataclass 用 `from_dict()` 做兼容映射，调用方可以拿到统一字段。
    """

    id: int
    status: PendingStatus
    ts: float
    chat_key: str
    peer_text: str = ""
    proposed_reply: str = ""
    peer_name: str = ""
    reply_lang: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PendingItem":
        ts = d.get("ts") or d.get("created_at") or 0.0
        # Messenger 用 reply_text，其他平台用 proposed_reply
        proposed = d.get("proposed_reply") or d.get("reply_text") or ""
        # status 字段做大小写兼容
        raw_status = str(d.get("status") or "pending").lower()
        try:
            status = PendingStatus(raw_status)
        except ValueError:
            status = PendingStatus.PENDING
        return cls(
            id=int(d.get("id") or 0),
            status=status,
            ts=float(ts),
            chat_key=str(d.get("chat_key") or ""),
            peer_text=str(d.get("peer_text") or ""),
            proposed_reply=str(proposed),
            peer_name=str(d.get("peer_name") or d.get("chat_name") or ""),
            reply_lang=str(d.get("reply_lang") or ""),
            extra={k: v for k, v in d.items() if k not in {
                "id", "status", "ts", "created_at", "chat_key",
                "peer_text", "proposed_reply", "reply_text",
                "peer_name", "chat_name", "reply_lang",
            }},
        )

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": self.id,
            "status": self.status.value,
            "ts": self.ts,
            "chat_key": self.chat_key,
            "peer_text": self.peer_text,
            "proposed_reply": self.proposed_reply,
            "peer_name": self.peer_name,
            "reply_lang": self.reply_lang,
        }
        if self.extra:
            d["extra"] = dict(self.extra)
        return d


@dataclass
class AlertItem:
    """告警记录。"""

    id: int
    severity: AlertSeverity
    ts: float
    code: str = ""
    title: str = ""
    detail: Any = None
    acked: bool = False
    acked_at: Optional[float] = None
    acked_by: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AlertItem":
        ts = d.get("ts") or d.get("created_at") or 0.0
        raw_sev = str(d.get("severity") or d.get("level") or "warning").lower()
        try:
            sev = AlertSeverity(raw_sev)
        except ValueError:
            sev = AlertSeverity.WARNING
        return cls(
            id=int(d.get("id") or 0),
            severity=sev,
            ts=float(ts),
            code=str(d.get("code") or d.get("kind") or ""),
            title=str(d.get("title") or d.get("message") or ""),
            detail=d.get("detail"),
            acked=bool(d.get("acked")),
            acked_at=float(d["acked_at"]) if d.get("acked_at") else None,
            acked_by=str(d.get("acked_by") or ""),
        )


@dataclass
class RpaStatusSummary:
    """一个 RPA 平台的状态总览（供跨平台总览页 / 健康检查使用）。

    覆盖最常用的 8 个字段。各 service.status() 字段不完全一致，
    用 `from_status_dict()` 做兼容映射。
    """

    platform: RpaPlatform
    available: bool = False
    enabled: bool = False
    running: bool = False
    paused: bool = False
    pause_remaining_sec: float = 0.0
    reply_mode: str = "auto"
    # KPI（24h 维度）
    sent_24h: int = 0
    total_24h: int = 0
    avg_ms_24h: float = 0.0
    # 待审批 / 告警
    pending_count: int = 0
    unacked_alerts: int = 0
    # 每日上限
    daily_cap: int = 0
    daily_sent: int = 0
    # 末次时间
    last_run_ts: float = 0.0
    # 附加
    hint: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_status_dict(
        cls, platform: RpaPlatform, status: Dict[str, Any]
    ) -> "RpaStatusSummary":
        """从 service.status() 返回的 dict 构造摘要。

        兼容 4 个 service 不完全一致的字段命名：
        - LINE 用 `stats_24h.sent / .total / .avg_send_ms`
        - WhatsApp 用 `stats_24h.sent / .total / .avg_ms`
        - Messenger 用 `send_stats.sent_24h / .total_24h`
        - Telegram 字段散落，由调用方先映射好
        """
        s24 = (
            status.get("stats_24h")
            or status.get("send_stats")
            or {}
        )
        # 不同 service 的 24h 平均耗时字段名
        avg_ms = (
            s24.get("avg_ms")
            or s24.get("avg_send_ms")
            or 0.0
        )
        # 已发数和总数
        sent_24h = int(s24.get("sent") or s24.get("sent_24h") or 0)
        total_24h = int(s24.get("total") or s24.get("total_24h") or 0)
        # pending_count 多源
        pending_count = int(
            status.get("pending_count")
            or (status.get("pending_stats") or {}).get("pending")
            or (status.get("approval_sla") or {}).get("pending_count")
            or 0
        )
        # 末次运行 ts
        last_ts = float(
            status.get("last_tick_ts")
            or (status.get("last_run") or {}).get("ts")
            or 0.0
        )
        return cls(
            platform=platform,
            available=bool(status.get("available", True)),
            enabled=bool(status.get("enabled") or status.get("enabled_cfg") or False),
            running=bool(status.get("running") or False),
            paused=bool(status.get("paused") or False),
            pause_remaining_sec=float(status.get("pause_remaining_sec") or 0.0),
            reply_mode=str(status.get("reply_mode") or "auto"),
            sent_24h=sent_24h,
            total_24h=total_24h,
            avg_ms_24h=float(avg_ms),
            pending_count=pending_count,
            # 兼容 LINE (`alerts_unacked`) 与 WhatsApp (`unacked_alerts`) 两种命名
            unacked_alerts=int(
                status.get("unacked_alerts")
                or status.get("alerts_unacked")
                or 0
            ),
            daily_cap=int(status.get("daily_cap") or 0),
            daily_sent=int(status.get("daily_sent") or sent_24h),
            last_run_ts=last_ts,
            hint=str(status.get("hint") or ""),
            raw=dict(status),
        )

    @property
    def success_rate(self) -> float:
        """24h 成功率（%）。total=0 时返回 0.0。"""
        if self.total_24h <= 0:
            return 0.0
        return round(self.sent_24h / self.total_24h * 100.0, 1)

    @property
    def health_status(self) -> str:
        """健康状态：ok / warn / err / offline / paused。

        与前端 .rpa-status-dot 修饰符对齐。
        """
        if not self.available or not self.enabled:
            return "offline"
        if self.paused:
            return "paused"
        if not self.running:
            return "err"
        if self.unacked_alerts > 0:
            return "warn"
        return "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform.value,
            "platform_name": self.platform.display_name,
            "api_prefix": self.platform.api_prefix,
            "available": self.available,
            "enabled": self.enabled,
            "running": self.running,
            "paused": self.paused,
            "pause_remaining_sec": self.pause_remaining_sec,
            "reply_mode": self.reply_mode,
            "sent_24h": self.sent_24h,
            "total_24h": self.total_24h,
            "avg_ms_24h": self.avg_ms_24h,
            "success_rate": self.success_rate,
            "pending_count": self.pending_count,
            "unacked_alerts": self.unacked_alerts,
            "daily_cap": self.daily_cap,
            "daily_sent": self.daily_sent,
            "last_run_ts": self.last_run_ts,
            "health_status": self.health_status,
            "hint": self.hint,
        }
