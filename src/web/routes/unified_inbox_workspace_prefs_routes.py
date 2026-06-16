"""统一收件箱——坐席告警偏好 / SLA·首响明细 / SLA一键建任务路由域（巨石拆分 slice 14）。

把"坐席告警偏好读写（含 P3 match_language 技能语言声明）+ SLA/首响明细下钻 +
SLA 超时一键生成跟进任务"这一子域，从 ``register_unified_inbox_routes`` 巨型闭包中外移为
``register_workspace_prefs_routes(app, *, api_auth)``，由主 register 在**原位置**顺序调用。
端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + prefs.languages 专项断言）。

依赖全部朝下：sla 配置/明细族（unified_inbox_sla）、auth 身份/跟进事件、services 存储；
prefs.languages 写链路仍走 inbox.set_agent_languages（坐席语言栈，供 auto_assign match_language）。
只收 api_auth 一个参数（本域无 page/templates/config 需求）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request

from src.web.routes.unified_inbox_auth import _publish_follow_up, _session_agent
from src.web.routes.unified_inbox_services import (
    _contacts_gateway,
    _contacts_store,
    _inbox_store,
)
from src.web.routes.unified_inbox_sla import (
    _agent_frt_detail,
    _agent_sla_cfg,
    _sla_cfg,
    _sla_detail,
)

logger = logging.getLogger(__name__)


def register_workspace_prefs_routes(app, *, api_auth) -> None:
    """挂载坐席告警偏好 / SLA·首响明细 / SLA一键建任务端点（/api/workspace/prefs|sla-detail|agent-frt-detail|sla/create-task）。"""

    @app.get("/api/workspace/prefs")
    async def api_workspace_prefs_get(request: Request):
        """当前坐席告警偏好 + 全局默认阈值（供设置面板回显）。"""
        api_auth(request)
        glob = _sla_cfg(request)
        inbox = _inbox_store(request)
        agent = _session_agent(request)
        prefs = (inbox.get_agent_prefs(agent["agent_id"])
                 if inbox is not None else
                 {"warn_sec": 0, "crit_sec": 0, "muted": 0,
                  "dnd_start": -1, "dnd_end": -1})
        return {"ok": True, "prefs": prefs,
                "global_warn_sec": glob["warn"], "global_crit_sec": glob["crit"],
                "effective": _agent_sla_cfg(request)}

    @app.post("/api/workspace/prefs")
    async def api_workspace_prefs_set(request: Request):
        """保存当前坐席告警偏好：{warn_sec,crit_sec,muted,dnd_start,dnd_end}。

        warn_sec/crit_sec=0 表示沿用全局；dnd_start/dnd_end 为本地分钟(0-1439)，
        -1=关闭免打扰。
        """
        api_auth(request)
        inbox = _inbox_store(request)
        if inbox is None:
            return {"ok": False, "error": "inbox_disabled"}
        body = await request.json()

        def _int(key: str, default: int = 0) -> int:
            try:
                return int(body.get(key, default))
            except (TypeError, ValueError):
                return default

        def _clamp_min(v: int) -> int:
            return v if v == -1 else max(0, min(1439, v))

        agent = _session_agent(request)
        prefs = inbox.set_agent_prefs(
            agent["agent_id"],
            warn_sec=max(0, _int("warn_sec")),
            crit_sec=max(0, _int("crit_sec")),
            muted=1 if body.get("muted") else 0,
            dnd_start=_clamp_min(_int("dnd_start", -1)),
            dnd_end=_clamp_min(_int("dnd_end", -1)),
        )
        # P3：坐席技能语言声明（供 auto_assign match_language；只在 body 显式带 languages 时更新）。
        if "languages" in body:
            from src.ai.translation_service import normalize_lang
            raw = body.get("languages")
            items = raw if isinstance(raw, list) else str(raw or "").split(",")
            norm: List[str] = []
            for it in items:
                code = normalize_lang(str(it).strip())
                if code and code not in norm:
                    norm.append(code)
            prefs = inbox.set_agent_languages(agent["agent_id"], ",".join(norm))
        return {"ok": True, "prefs": prefs}

    @app.get("/api/workspace/sla-detail")
    async def api_workspace_sla_detail(
        request: Request, scope: str = "critical", agent: Optional[str] = None,
    ):
        """SLA/首响明细下钻清单（仪表盘卡片/坐席行点开）。"""
        api_auth(request)
        scope = scope if scope in {"waiting", "breaching", "critical",
                                   "unresponded"} else "critical"
        return _sla_detail(request, scope=scope, agent=agent)

    @app.get("/api/workspace/agent-frt-detail")
    async def api_workspace_agent_frt_detail(
        request: Request, agent: str = "", days: int = 7,
    ):
        """某坐席窗口内首响会话明细（绩效榜下钻）。"""
        api_auth(request)
        return _agent_frt_detail(request, agent=str(agent or ""), days=days)

    @app.post("/api/workspace/sla/create-task")
    async def api_workspace_sla_create_task(request: Request):
        """SLA 超时会话一键生成跟进任务（告警→行动闭环）。

        body: {platform, chat_key, conversation_id?, name?, wait_sec?,
               due_in_hours?(默认2), assignee?(默认本人), note?}
        会话经 (platform, chat_key) 解析 contact，note 预填 SLA 上下文。
        """
        api_auth(request)
        body = await request.json()
        store = _contacts_store(request)
        gw = _contacts_gateway(request)
        if store is None or gw is None:
            return {"ok": False, "error": "contacts_disabled"}
        platform = str(body.get("platform") or "").strip()
        chat_key = str(body.get("chat_key") or "").strip()
        conv = str(body.get("conversation_id") or "").strip()
        if (not platform or not chat_key) and conv:
            parts = conv.split(":")
            if len(parts) >= 3:
                platform = platform or parts[0]
                chat_key = chat_key or ":".join(parts[2:])
        if not platform or not chat_key:
            raise HTTPException(400, "缺少 platform/chat_key 或 conversation_id")
        cmap = store.resolve_contacts_by_external([(platform, chat_key)])
        contact_id = cmap.get((platform, chat_key))
        if not contact_id:
            return {"ok": False, "error": "contact_not_found"}
        try:
            due_in_hours = float(body.get("due_in_hours") or 2)
        except (TypeError, ValueError):
            due_in_hours = 2.0
        due_in_hours = max(0.0, min(24.0 * 30, due_in_hours))
        due_at = int(time.time() + due_in_hours * 3600)
        agent = _session_agent(request)
        assignee = str(body.get("assignee") or "").strip() or agent["agent_id"]
        wait_sec = 0
        try:
            wait_sec = int(body.get("wait_sec") or 0)
        except (TypeError, ValueError):
            wait_sec = 0
        prefix = ("SLA 超时未回复 %d 分钟，请尽快跟进" % (wait_sec // 60)
                  if wait_sec > 0 else "SLA 超时未回复，请尽快跟进")
        extra = str(body.get("note") or "").strip()
        note = prefix + ("；" + extra if extra else "")
        out = gw.add_follow_up_task(
            contact_id, due_at=due_at, note=note,
            assignee=assignee, operator=agent["agent_id"])
        if out.get("ok"):
            _publish_follow_up("added", contact_id=contact_id,
                               task_id=out.get("task_id") or "", assignee=assignee)
            out["contact_id"] = contact_id
            out["due_at"] = due_at
        return out
