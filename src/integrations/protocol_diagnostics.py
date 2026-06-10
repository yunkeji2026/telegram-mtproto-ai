"""协议栈联调自检（M6③）。

protocol 方式（Telegram pyrogram / WhatsApp Baileys）**无法在无真账号环境联调**，
本模块把「能不能跑起来」收敛成一份**结构化就绪报告**，供：
- CLI ``scripts/protocol_doctor.py`` 一键自检；
- API ``GET /api/accounts/protocol/readiness`` 供前端展示；
- 单测（纯函数 + 可注入，静态部分零网络）。

报告分两层：``readiness_static`` 只读配置 + 进程内状态（不触网，秒回）；``readiness``
在其上补一次 WhatsApp Node 可达性探测（异步）。每项都带 ``ready`` 与人话 ``hints``。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _telegram_report(config: Dict[str, Any]) -> Dict[str, Any]:
    from src.integrations.telegram_protocol_login import (
        is_pyrogram_available, protocol_enabled, resolve_credentials,
    )
    enabled = protocol_enabled(config)
    pyrogram = is_pyrogram_available()
    creds = resolve_credentials(config) is not None
    hints: List[str] = []
    if not enabled:
        hints.append("未启用：config.platform_login.telegram.protocol_enabled: true")
    if not pyrogram:
        hints.append("缺少 pyrogram：pip install pyrogram tgcrypto")
    if not creds:
        hints.append("缺少 api_id/api_hash：填 config.telegram.api_id / api_hash")
    ready = enabled and pyrogram and creds
    if ready:
        hints.append("就绪：用 scripts/create_pyrogram_session.py 生成测试号 session 后扫码新增")
    return {
        "mode_enabled": enabled,
        "pyrogram_available": pyrogram,
        "credentials": creds,
        "ready": ready,
        "hints": hints,
    }


def _whatsapp_report_static(config: Dict[str, Any]) -> Dict[str, Any]:
    from src.integrations.whatsapp_baileys_login import (
        protocol_enabled, service_base_url,
    )
    enabled = protocol_enabled(config)
    url = service_base_url(config)
    hints: List[str] = []
    if not enabled:
        hints.append("未启用：config.platform_login.whatsapp.protocol_enabled: true")
    hints.append(f"Baileys 微服务地址：{url}（cd services/whatsapp-baileys && npm install && node server.js）")
    return {
        "mode_enabled": enabled,
        "service_url": url,
        "service_reachable": None,   # 由 readiness() 异步补
        "ready": enabled,            # 静态层只能判到「已启用」，可达性见异步层
        "hints": hints,
    }


def _orchestrator_report() -> Dict[str, Any]:
    try:
        from src.integrations.account_orchestrator import _orchestrator
        if _orchestrator is None:
            return {"instantiated": False, "loop_running": False, "managed_total": 0}
        st = _orchestrator.status()
        return {
            "instantiated": True,
            "loop_running": bool(st.get("running_loop")),
            "managed_total": int(st.get("total") or 0),
            "by_state": st.get("by_state") or {},
        }
    except Exception:
        logger.debug("[diag] 读取编排器状态失败", exc_info=True)
        return {"instantiated": False, "loop_running": False, "managed_total": 0}


def _ingest_report() -> Dict[str, Any]:
    try:
        from src.integrations.protocol_bridge import get_inbox_sink
        return {"sink_registered": get_inbox_sink() is not None}
    except Exception:
        return {"sink_registered": False}


def readiness_static(config: Dict[str, Any]) -> Dict[str, Any]:
    """只读配置 + 进程内状态的就绪报告（不触网）。"""
    cfg = config or {}
    pl = cfg.get("platform_login", {}) or {}
    tg = _telegram_report(cfg)
    wa = _whatsapp_report_static(cfg)
    orch = _orchestrator_report()
    ingest = _ingest_report()
    overall = bool(
        pl.get("enabled")
        and (tg["ready"] or wa["ready"])
        and ingest["sink_registered"]
    )
    return {
        "platform_login_enabled": bool(pl.get("enabled")),
        "orchestrator_enabled": bool(pl.get("orchestrator_enabled")),
        "telegram": tg,
        "whatsapp": wa,
        "orchestrator": orch,
        "inbox_ingest": ingest,
        "overall_ready": overall,
    }


async def check_whatsapp_reachable(config: Dict[str, Any]) -> bool:
    """探测 Baileys Node 的 /health（best-effort，失败/超时返回 False）。"""
    from src.integrations.whatsapp_baileys_login import _get_json, service_base_url
    try:
        res = await _get_json(f"{service_base_url(config)}/health", timeout=5.0)
        return bool((res or {}).get("ok", False))
    except Exception:
        return False


async def readiness(config: Dict[str, Any]) -> Dict[str, Any]:
    """完整就绪报告：静态报告 + WhatsApp 服务可达性探测。"""
    report = readiness_static(config)
    wa = report["whatsapp"]
    if wa.get("mode_enabled"):
        reachable = await check_whatsapp_reachable(config)
        wa["service_reachable"] = reachable
        wa["ready"] = bool(wa["mode_enabled"] and reachable)
        if not reachable:
            wa["hints"].append("服务不可达：确认 Baileys Node 已启动且 baileys_url 正确")
        # 可达性影响整体（WA 启用却不可达 → 整体未就绪）
        report["overall_ready"] = bool(
            report["platform_login_enabled"]
            and (report["telegram"]["ready"] or wa["ready"])
            and report["inbox_ingest"]["sink_registered"]
        )
    return report


def format_report(report: Dict[str, Any]) -> str:
    """把就绪报告渲染成 CLI 友好文本（✅/⚠️/❌）。"""
    def mark(ok: Any) -> str:
        if ok is True:
            return "✅"
        if ok is None:
            return "⏳"
        return "❌"

    lines: List[str] = []
    lines.append(f"{mark(report['overall_ready'])} 协议栈整体就绪")
    lines.append(f"   platform_login.enabled = {report['platform_login_enabled']}")
    lines.append(f"   orchestrator_enabled   = {report['orchestrator_enabled']}")

    tg = report["telegram"]
    lines.append(f"\n{mark(tg['ready'])} Telegram protocol")
    lines.append(f"   mode_enabled={tg['mode_enabled']} pyrogram={tg['pyrogram_available']} creds={tg['credentials']}")
    for h in tg["hints"]:
        lines.append(f"   - {h}")

    wa = report["whatsapp"]
    lines.append(f"\n{mark(wa['ready'])} WhatsApp Baileys")
    lines.append(f"   mode_enabled={wa['mode_enabled']} reachable={wa['service_reachable']}")
    for h in wa["hints"]:
        lines.append(f"   - {h}")

    orch = report["orchestrator"]
    lines.append(f"\n{mark(orch['loop_running'])} 编排器")
    lines.append(f"   instantiated={orch['instantiated']} loop={orch['loop_running']} managed={orch['managed_total']}")

    ing = report["inbox_ingest"]
    lines.append(f"\n{mark(ing['sink_registered'])} 收件箱入站 sink: registered={ing['sink_registered']}")
    return "\n".join(lines)
