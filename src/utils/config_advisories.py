"""
启动时根据配置打印建议与告警（生产基线），可选写入审计表。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class AdvisoryEvent:
    """单条启动建议：level 为 logging 级别名（info/warning/debug）。"""

    level: str
    code: str
    message: str


def collect_production_advisories(config: Dict[str, Any]) -> List[AdvisoryEvent]:
    """收集全部事件（无副作用，便于测试）。"""
    if not isinstance(config, dict):
        return []
    out: List[AdvisoryEvent] = []

    web = config.get("web_admin") or {}
    host = str(web.get("host", "127.0.0.1") or "").strip()
    if host in ("0.0.0.0", "::", "[::]"):
        out.append(
            AdvisoryEvent(
                "warning",
                "web_bind_all_interfaces",
                f"生产基线：Web 监听 {host} 表示绑定所有网卡，请确认仅内网可达或前置反向代理与访问控制。",
            )
        )
    elif host and host not in ("127.0.0.1", "localhost", "::1"):
        out.append(
            AdvisoryEvent(
                "warning",
                "web_non_loopback",
                "生产基线：Web 管理端监听 "
                f"{host}。若对公网开放，请使用 HTTPS、强密码、防火墙或 VPN，并定期轮换 web_admin.auth_token。",
            )
        )

    mem = config.get("memory") or {}
    vec = mem.get("vector") or {}
    st = vec.get("backfill_on_startup") or {}
    per = vec.get("backfill_periodic") or {}
    if st.get("enabled") and per.get("enabled"):
        out.append(
            AdvisoryEvent(
                "info",
                "memory_backfill_both_enabled",
                "配置提示：情景记忆「启动补全」与「周期补全」均已开启，"
                "启动补全将自动跳过以避免重复嵌入。",
            )
        )

    bud = vec.get("daily_embed_budget") or {}
    if bud.get("enabled"):
        out.append(
            AdvisoryEvent(
                "info",
                "memory_daily_embed_budget",
                f"情景记忆补全已启用日预算：max_calls={bud.get('max_calls', '?')}（UTC 日，仅统计补全路径）。",
            )
        )

    mon = config.get("monitoring") or {}
    if mon.get("enabled", True):
        port = mon.get("metrics_port", 9090)
        out.append(
            AdvisoryEvent(
                "debug",
                "monitoring_port",
                f"监控 API 端口：{port}（请仅内网访问或配合 token）",
            )
        )

    return out


def log_advisory_events(logger: logging.Logger, events: List[AdvisoryEvent]) -> None:
    """将事件写入 logger。"""
    level_map = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }
    for e in events:
        lvl = level_map.get(e.level.lower(), logging.INFO)
        logger.log(lvl, "%s", e.message)


def record_warning_advisories_to_audit(
    audit_store: Any, events: List[AdvisoryEvent]
) -> int:
    """
    将 warning 级别事件写入审计（action=config_advisory，target=code，new_val=message）。
    返回写入条数。
    """
    if not audit_store or not events:
        return 0
    n = 0
    for e in events:
        if e.level.lower() != "warning":
            continue
        try:
            msg = (e.message or "")[:450]
            audit_store.log(
                "system",
                "config_advisory",
                e.code,
                "",
                msg,
            )
            n += 1
        except Exception:
            pass
    return n


def log_production_advisories(logger: logging.Logger, config: Dict[str, Any]) -> None:
    """兼容旧接口：收集并打印日志。"""
    log_advisory_events(logger, collect_production_advisories(config))
