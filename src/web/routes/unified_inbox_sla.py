"""统一收件箱——SLA / 首响 / 升级快照（巨石拆分 slice 6）。

从 ``unified_inbox_routes.py`` 抽出的 **SLA 阈值解析 + 告警/升级快照 + 首响明细下钻**
族：全局/个人 SLA 阈值、当前坐席告警快照（受个人静默影响）、团队升级安全网快照
（全局口径、不受查看者静默影响）、SLA/首响明细下钻、坐席首响绩效下钻。

依赖层级：仅依赖 services（_inbox_store）、auth（_session_agent）、helpers（_dnd_active），
不反向依赖 routes，故无循环 import。routes.py 等价重导出，对外引用路径保持不变。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import Request

from src.web.routes.unified_inbox_auth import _session_agent
from src.web.routes.unified_inbox_helpers import _dnd_active
from src.web.routes.unified_inbox_services import _inbox_store

logger = logging.getLogger(__name__)

_SLA_WARN_SEC = 1800  # 客户消息未回复超过该秒数标记 SLA 警告（默认 30 分钟）
_SLA_CRIT_SEC = 7200  # 超过该秒数标记严重超时（默认 2 小时）


def _sla_cfg(request: Request) -> Dict[str, int]:
    """SLA 阈值（秒）：config.inbox.sla_warn_sec / sla_crit_sec，带默认值。"""
    warn, crit = _SLA_WARN_SEC, _SLA_CRIT_SEC
    cm = getattr(request.app.state, "config_manager", None)
    cfg = getattr(cm, "config", None) if cm is not None else None
    if isinstance(cfg, dict):
        ib = cfg.get("inbox") or {}
        try:
            warn = int(ib.get("sla_warn_sec", warn) or warn)
            crit = int(ib.get("sla_crit_sec", crit) or crit)
        except (TypeError, ValueError):
            pass
    if crit < warn:
        crit = warn
    return {"warn": warn, "crit": crit}


def _agent_sla_cfg(request: Request) -> Dict[str, Any]:
    """全局 SLA 阈值叠加当前坐席个性化覆盖 + 免打扰/静音状态。"""
    base = _sla_cfg(request)
    warn, crit = base["warn"], base["crit"]
    muted = False
    dnd = False
    inbox = _inbox_store(request)
    if inbox is not None:
        try:
            agent = _session_agent(request)
            prefs = inbox.get_agent_prefs(agent["agent_id"])
            if int(prefs.get("warn_sec") or 0) > 0:
                warn = int(prefs["warn_sec"])
            if int(prefs.get("crit_sec") or 0) > 0:
                crit = int(prefs["crit_sec"])
            muted = bool(prefs.get("muted"))
            dnd = _dnd_active(prefs)
        except Exception:
            logger.debug("读取坐席告警偏好失败（已忽略）", exc_info=True)
    if crit < warn:
        crit = warn
    return {"warn": warn, "crit": crit, "muted": muted, "dnd": dnd}


def _sla_alert_snapshot(request: Request) -> Dict[str, Any]:
    """当前 SLA 快照：等待/警告/严重计数 + 严重超时会话清单（告警徽标/SSE 用）。

    阈值按当前坐席个性化覆盖；静音或免打扰时段则 items 置空 + quiet=true，
    使徽标与 SSE toast 在该坐席侧静默（计数仍照常返回供仪表盘参考）。
    """
    inbox = _inbox_store(request)
    if inbox is None:
        return {"ok": True, "waiting": 0, "breaching": 0, "critical": 0,
                "items": [], "quiet": False}
    sla = _agent_sla_cfg(request)
    quiet = bool(sla["muted"] or sla["dnd"])
    exclude_groups = _alerts_exclude_groups(request)
    convs = inbox.list_conversations(limit=500)
    cmap = {str(c.get("conversation_id") or ""): c for c in convs}
    dirs = inbox.last_message_dirs(list(cmap))
    archived = _archived_set(inbox, list(cmap))
    snoozed = _snoozed_set(inbox, list(cmap))
    now = time.time()
    waiting = breaching = 0
    items: List[Dict[str, Any]] = []
    for cid, info in dirs.items():
        if info.get("direction") != "in":
            continue
        if cid in archived:
            continue  # 已归档=已处理/已忽略，不再计入告警（前端「忽略」即清零）
        if cid in snoozed:
            continue  # 已搁置=坐席「稍后再看」，到点/客户回复前不计入告警
        if exclude_groups and _is_non_alert_conv(cmap.get(cid) or {}):
            continue  # 群组/频道不计入 SLA 告警，改走「群组动态」
        waiting += 1
        wait = now - (info.get("ts") or now)
        if wait >= sla["warn"]:
            breaching += 1
        if wait >= sla["crit"]:
            c = cmap.get(cid) or {}
            items.append({
                "conversation_id": cid,
                "platform": str(c.get("platform") or ""),
                "account_id": str(c.get("account_id") or "default"),
                "chat_key": str(c.get("chat_key") or ""),
                "name": str(c.get("display_name") or c.get("chat_key") or cid),
                "wait_sec": int(wait),
            })
    items.sort(key=lambda x: -x["wait_sec"])
    return {"ok": True, "waiting": waiting, "breaching": breaching,
            "critical": len(items), "items": [] if quiet else items[:50],
            "quiet": quiet, "warn_sec": sla["warn"], "crit_sec": sla["crit"]}


_NON_ALERT_CHAT_TYPES = ("group", "channel")


def _alerts_exclude_groups(request: Request) -> bool:
    """是否把群组/频道排除出升级/SLA 告警（默认开）。

    config.inbox.alerts.exclude_groups：群组消息不抢回复时效、不该刷升级告警，
    改由前端「群组动态」被动展示。设为 false 可恢复旧行为（群组也告警）。
    """
    cm = getattr(request.app.state, "config_manager", None)
    cfg = getattr(cm, "config", None) if cm is not None else None
    if isinstance(cfg, dict):
        alerts = ((cfg.get("inbox") or {}).get("alerts") or {})
        if isinstance(alerts, dict) and "exclude_groups" in alerts:
            return bool(alerts.get("exclude_groups"))
    return True


def _is_non_alert_conv(conv: Dict[str, Any]) -> bool:
    """该会话是否属于「不告警」类型（群组/频道）。

    主判据是 ``chat_type``；但历史会话可能在 chat_type 特性之前入库、被默认成
    ``private``，导致 Telegram 群组/频道（chat_key 为负数 id，如 -100.../-5...）泄漏进
    SLA「严重超时/待接管」。这里加一道按 chat_key 的兜底，负数 Telegram 会话一律按群组排除。
    """
    conv = conv or {}
    if str(conv.get("chat_type") or "private").lower() in _NON_ALERT_CHAT_TYPES:
        return True
    plat = str(conv.get("platform") or "").lower()
    chat_key = str(conv.get("chat_key") or "")
    conv_id = str(conv.get("conversation_id") or "")
    if plat == "telegram":
        ck = chat_key
        if not ck:
            # 退而从 conversation_id（telegram:<account>:<chat_key>）尾段取
            parts = conv_id.split(":")
            ck = parts[-1] if parts else ""
        if ck.startswith("-"):
            return True
    if plat == "line":
        # LINE 官方会话 key 形如 line:group:<id> / line:room:<id>（群/房间皆非告警）
        low = (chat_key + " " + conv_id).lower()
        if ":group:" in low or ":room:" in low:
            return True
    return False


def _archived_set(inbox, conv_ids: List[str]) -> set:
    """已归档会话 id 集合（conversation_meta.archived=1）。

    归档=已处理/已忽略，不应再刷 SLA/升级告警 —— 这给前端「忽略」按钮一个
    真正能让「严重超时/待接管」清零的杠杆（接管只去掉「无人接管」，回复才去掉超时；
    归档则把整条移出告警口径）。
    """
    if inbox is None or not conv_ids:
        return set()
    try:
        meta = inbox.list_conv_tags_map(list(conv_ids))
        return {cid for cid, m in meta.items() if m.get("archived")}
    except Exception:
        logger.debug("读取归档状态失败（已忽略）", exc_info=True)
        return set()


def _snoozed_set(inbox, conv_ids: List[str]) -> set:
    """搁置中的会话 id 集合（conversation_meta.snooze_until>now）。

    搁置=坐席「稍后再看」：到点/客户回复前不刷 SLA/升级告警——给「待接管」队列一个
    不删会话也能暂时移出视线的杠杆（到点由 snoozed_ids 的 now 过滤自动重浮；客户再来
    消息由 ingest 侧 clear_snooze 立即重浮）。与「忽略/归档」互补：搁置是临时、会自动回来。
    """
    if inbox is None or not conv_ids:
        return set()
    try:
        return inbox.snoozed_ids() & set(conv_ids)
    except Exception:
        logger.debug("读取搁置状态失败（已忽略）", exc_info=True)
        return set()


def _presence_stale_sec(request: Request) -> float:
    """在线判定窗口（秒）：config.workspace.presence_stale_sec，默认 120。"""
    cm = getattr(request.app.state, "config_manager", None)
    cfg = getattr(cm, "config", None) if cm is not None else None
    if isinstance(cfg, dict):
        ws = cfg.get("workspace") or {}
        try:
            return max(30.0, float(ws.get("presence_stale_sec") or 120))
        except (TypeError, ValueError):
            pass
    return 120.0


def _escalation_snapshot(request: Request) -> Dict[str, Any]:
    """升级快照（团队安全网，**全局口径、不受查看者个人静默影响**）。

    列出"严重超时(≥全局 crit)且无人有效处理"的会话 + 原因：
      unclaimed=无人认领 / holder_offline=认领坐席离线 / holder_quiet=认领坐席静音或免打扰。
    用于 6-18 个人可静默后的兜底：被放下的会话不能就此无人管。
    """
    inbox = _inbox_store(request)
    if inbox is None:
        return {"ok": True, "count": 0, "items": []}
    sla = _sla_cfg(request)  # 全局团队阈值，不叠加个人覆盖
    exclude_groups = _alerts_exclude_groups(request)
    now = time.time()
    convs = inbox.list_conversations(limit=500)
    cmap = {str(c.get("conversation_id") or ""): c for c in convs}
    dirs = inbox.last_message_dirs(list(cmap))
    archived = _archived_set(inbox, list(cmap))
    snoozed = _snoozed_set(inbox, list(cmap))
    claim_map: Dict[str, Dict[str, str]] = {}
    try:
        for cl in inbox.list_conversation_claims():
            claim_map[str(cl.get("conversation_id") or "")] = {
                "agent_id": str(cl.get("agent_id") or ""),
                "agent_name": str(cl.get("agent_name") or ""),
            }
    except Exception:
        logger.debug("escalation claim 读取失败（已忽略）", exc_info=True)
    online: Dict[str, str] = {}
    try:
        for p in inbox.list_agent_presence(
                active_within_sec=_presence_stale_sec(request)):
            online[str(p.get("agent_id") or "")] = str(p.get("status") or "")
    except Exception:
        logger.debug("escalation presence 读取失败（已忽略）", exc_info=True)
    items: List[Dict[str, Any]] = []
    for cid, info in dirs.items():
        if info.get("direction") != "in":
            continue
        if cid in archived:
            continue  # 已归档=已处理/已忽略，不再进升级告警
        if cid in snoozed:
            continue  # 已搁置=坐席「稍后再看」，到点/客户回复前不进升级告警
        if exclude_groups and _is_non_alert_conv(cmap.get(cid) or {}):
            continue  # 群组/频道不进升级告警
        wait = now - (info.get("ts") or now)
        if wait < sla["crit"]:
            continue
        cl = claim_map.get(cid)
        reason = ""
        if not cl or not cl["agent_id"]:
            reason = "unclaimed"
        else:
            aid = cl["agent_id"]
            status = online.get(aid)
            if status not in ("online", "busy"):
                reason = "holder_offline"
            else:
                try:
                    prefs = inbox.get_agent_prefs(aid)
                    if prefs.get("muted") or _dnd_active(prefs):
                        reason = "holder_quiet"
                except Exception:
                    reason = ""
        if not reason:
            continue
        c = cmap.get(cid) or {}
        items.append({
            "conversation_id": cid,
            "platform": str(c.get("platform") or ""),
            "account_id": str(c.get("account_id") or "default"),
            "chat_key": str(c.get("chat_key") or ""),
            "name": str(c.get("display_name") or c.get("chat_key") or cid),
            "wait_sec": int(max(0, wait)),
            "reason": reason,
            "agent_id": cl["agent_id"] if cl else "",
            "agent_name": (cl["agent_name"] if cl else "") or "",
        })
    items.sort(key=lambda x: -x["wait_sec"])
    today_count = 0
    try:
        lt = time.localtime(now)
        midnight = time.mktime(
            (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        today_count = inbox.count_escalations_since(midnight)
    except Exception:
        logger.debug("escalation today_count 失败（已忽略）", exc_info=True)
    return {"ok": True, "count": len(items), "items": items[:50],
            "today_count": today_count, "crit_sec": sla["crit"]}


def _sla_detail(
    request: Request, scope: str = "critical", agent: Optional[str] = None,
) -> Dict[str, Any]:
    """SLA/首响明细下钻：按 scope 列出会话清单（带坐席归属，供仪表盘点开跳转）。

    scope: waiting(全部待回复) / breaching(≥warn) / critical(≥crit) / unresponded(今日进线未回复)。
    agent: 传入则按 claim 坐席过滤（""=未认领）。
    """
    inbox = _inbox_store(request)
    if inbox is None:
        return {"ok": True, "scope": scope, "items": [], "count": 0}
    sla = _sla_cfg(request)
    exclude_groups = _alerts_exclude_groups(request)
    now = time.time()
    convs = inbox.list_conversations(limit=500)
    cmap = {str(c.get("conversation_id") or ""): c for c in convs}
    claim_map: Dict[str, Dict[str, str]] = {}
    try:
        for cl in inbox.list_conversation_claims():
            claim_map[str(cl.get("conversation_id") or "")] = {
                "agent_id": str(cl.get("agent_id") or ""),
                "agent_name": str(cl.get("agent_name") or ""),
            }
    except Exception:
        logger.debug("sla-detail claim 读取失败（已忽略）", exc_info=True)

    def _mk(cid: str, wait: float, level: str) -> Dict[str, Any]:
        c = cmap.get(cid) or {}
        cl = claim_map.get(cid)
        return {
            "conversation_id": cid,
            "platform": str(c.get("platform") or ""),
            "account_id": str(c.get("account_id") or "default"),
            "chat_key": str(c.get("chat_key") or ""),
            "name": str(c.get("display_name") or c.get("chat_key") or cid),
            "wait_sec": int(max(0, wait)),
            "level": level,
            "agent_id": cl["agent_id"] if cl else "",
            "agent_name": (cl["agent_name"] if cl else "") or "",
        }

    items: List[Dict[str, Any]] = []
    if scope == "unresponded":
        lt = time.localtime(now)
        midnight = time.mktime(
            (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        for r in inbox.first_response_rows(midnight):
            if exclude_groups and _is_non_alert_conv(cmap.get(r["cid"]) or {}):
                continue
            if r["t_out"] is None:
                wait = now - r["t_in"]
                level = ("crit" if wait >= sla["crit"]
                         else "warn" if wait >= sla["warn"] else "")
                items.append(_mk(r["cid"], wait, level))
    else:
        thr = (sla["crit"] if scope == "critical"
               else sla["warn"] if scope == "breaching" else 0)
        dirs = inbox.last_message_dirs(list(cmap))
        for cid, info in dirs.items():
            if info.get("direction") != "in":
                continue
            if exclude_groups and _is_non_alert_conv(cmap.get(cid) or {}):
                continue
            wait = now - (info.get("ts") or now)
            if wait < thr:
                continue
            level = ("crit" if wait >= sla["crit"]
                     else "warn" if wait >= sla["warn"] else "")
            items.append(_mk(cid, wait, level))

    if agent is not None:
        items = [it for it in items if it["agent_id"] == agent]
    items.sort(key=lambda x: -x["wait_sec"])
    return {"ok": True, "scope": scope, "count": len(items),
            "items": items[:200]}


def _agent_frt_detail(
    request: Request, agent: str, days: int = 7,
) -> Dict[str, Any]:
    """某坐席在窗口内的首响会话明细（绩效榜下钻）。"""
    inbox = _inbox_store(request)
    if inbox is None:
        return {"ok": True, "agent": agent, "days": 7, "count": 0, "items": []}
    sla = _sla_cfg(request)
    span = 30 if int(days or 7) >= 30 else 7
    now = time.time()
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    since = midnight - (span - 1) * 86400
    convs = inbox.list_conversations(limit=500)
    cmap = {str(c.get("conversation_id") or ""): c for c in convs}
    items: List[Dict[str, Any]] = []
    for r in inbox.agent_first_responses(since):
        if r["resp_ts"] is None or r["agent_id"] != agent:
            continue
        frt = max(0, int(r["resp_ts"] - r["t_in"]))
        c = cmap.get(r["cid"]) or {}
        items.append({
            "conversation_id": r["cid"],
            "platform": str(c.get("platform") or ""),
            "account_id": str(c.get("account_id") or "default"),
            "chat_key": str(c.get("chat_key") or ""),
            "name": str(c.get("display_name") or c.get("chat_key") or r["cid"]),
            "frt_sec": frt,
            "attained": frt <= sla["warn"],
            "responded_at": r["resp_ts"],
        })
    items.sort(key=lambda x: -x["frt_sec"])
    return {"ok": True, "agent": agent, "days": span,
            "count": len(items), "items": items[:200]}
