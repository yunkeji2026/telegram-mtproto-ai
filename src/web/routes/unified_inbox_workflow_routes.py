"""统一收件箱——下一步动作推荐 / 自定义动作·工作链 / 工作链执行可视化路由域（巨石拆分 slice 20）。

把两段连续且共享依赖面的子域，从 ``register_unified_inbox_routes`` 巨型闭包中整体外移为
``register_workflow_routes(app, *, api_auth)``，由主 register 在**原位置**调用：

- Phase 37 下一步动作推荐 + 自定义动作/工作链：
  ``conv/{id}/next-actions`` / ``conv/{id}/execute-action`` /
  ``workflow-actions`` CRUD / ``workflow-chains`` CRUD
- Phase 47 工作链执行可视化：
  ``chain-executions`` / ``conv/{id}/chain-executions`` /
  ``chain-executions/{exec_id}/cancel`` / ``conv/{id}/start-chain``

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 20 端点契约断言）。

依赖全部朝下：services 存储、auth._agent_from_request；推荐器/工作链监控/event_bus
均为 handler 内局部 import。只收 api_auth 一个参数（零闭包私有依赖）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import Depends, HTTPException, Request

from src.web.routes.unified_inbox_auth import _agent_from_request
from src.web.routes.unified_inbox_services import _contacts_store, _inbox_store

logger = logging.getLogger(__name__)


def register_workflow_routes(app, *, api_auth) -> None:
    """挂载动作推荐 / 自定义动作·工作链 CRUD / 工作链执行可视化端点。"""

    # ─── Phase 37: 下一步动作推荐 + 自定义动作/工作链 ───────────────────

    @app.get("/api/workspace/conv/{conversation_id}/next-actions")
    async def api_conv_next_actions(conversation_id: str, request: Request):
        """AA1：推荐当前会话下一步动作（内置场景动作 + 用户自定义）。

        Query 参数可传入会话上下文加速推荐（否则从 store 自动拉取）：
          silence_hours, message_count, churn_risk_level
        """
        api_auth(request)
        store = _inbox_store(request)
        from src.inbox.next_action_recommender import NextActionRecommender

        # 拉取最新消息（用于信号检测）
        last_msg_text = ""
        last_msg_direction = "in"
        message_count = 0
        silence_hours = 0.0
        churn_risk_level = ""
        risk_signals: List[Dict[str, Any]] = []

        if store is not None:
            try:
                rows = store._conn.execute(
                    """SELECT direction, text, ts FROM messages
                       WHERE conversation_id = ? ORDER BY ts DESC LIMIT 30""",
                    (conversation_id,),
                ).fetchall()
                if rows:
                    message_count = store._conn.execute(
                        "SELECT COUNT(*) as c FROM messages WHERE conversation_id = ?",
                        (conversation_id,),
                    ).fetchone()["c"]
                    last = rows[0]
                    last_msg_text = str(last["text"] or "")
                    last_msg_direction = str(last["direction"] or "in")
                    import time as _t
                    silence_hours = max(0.0, (_t.time() - float(last["ts"] or 0)) / 3600)

                # 读取流失风险
                meta = store.get_conv_meta(conversation_id) or {}
                churn_raw = str(meta.get("churn_risk") or "").strip()
                if churn_raw:
                    import json as _j
                    cd = _j.loads(churn_raw)
                    churn_risk_level = str(cd.get("level") or "")
            except Exception:
                logger.debug("next-actions 上下文拉取失败（已忽略）", exc_info=True)

        # 拉取自定义动作（已启用）
        custom_actions: List[Dict[str, Any]] = []
        if store is not None:
            try:
                raw = store.list_workflow_actions()
                for act in raw:
                    import json as _j
                    try:
                        cfg = _j.loads(act.get("config_json") or "{}")
                    except Exception:
                        cfg = {}
                    try:
                        triggers = _j.loads(act.get("trigger_conditions") or '["any"]')
                    except Exception:
                        triggers = ["any"]
                    custom_actions.append({**act, "config": cfg, "trigger_conditions": triggers})
            except Exception:
                pass

        rec = NextActionRecommender()
        actions = rec.recommend(
            risk_signals=risk_signals,
            last_msg_text=last_msg_text,
            last_msg_direction=last_msg_direction,
            message_count=message_count,
            silence_hours=silence_hours,
            churn_risk_level=churn_risk_level,
            custom_actions=custom_actions,
            limit=6,
        )
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "actions": actions,
            "context": {
                "message_count": message_count,
                "silence_hours": round(silence_hours, 1),
                "churn_risk_level": churn_risk_level,
                "last_direction": last_msg_direction,
            },
        }

    @app.post("/api/workspace/conv/{conversation_id}/execute-action")
    async def api_conv_execute_action(conversation_id: str, request: Request, _=Depends(api_auth)):
        """AA1：执行一个动作（发话术/创建任务/打标签/启动工作链）。"""
        body = await request.json()
        action_type = str(body.get("action_type") or "")
        config = body.get("config") or {}
        store = _inbox_store(request)
        import time as _t
        now = _t.time()
        result: Dict[str, Any] = {"ok": True, "action_type": action_type}

        if action_type == "task":
            # 创建跟进任务
            due_hours = float(config.get("due_hours") or 72)
            note = str(config.get("note") or "")
            contacts_store = _contacts_store(request)
            agent_id = request.session.get("agent_id") or request.session.get("username") or ""
            if contacts_store:
                try:
                    meta = store.get_conv_meta(conversation_id) if store else {}
                    contact_id = (meta or {}).get("contact_id", "")
                    if contact_id:
                        contacts_store.add_follow_up_task(
                            contact_id, now + due_hours * 3600, note=note, assignee=agent_id
                        )
                        result["task_created"] = True
                except Exception:
                    pass

        elif action_type == "tag":
            # 添加标签
            tag = str(config.get("tag") or "")
            if tag and store:
                try:
                    existing_tags = store.get_conv_tags(conversation_id)
                    if tag not in existing_tags:
                        store.set_conv_tags(conversation_id, existing_tags + [tag])
                    result["tag"] = tag
                except Exception:
                    pass

        elif action_type == "note":
            # 添加内部注解
            body_text = str(config.get("note_body") or config.get("hint") or "")
            agent_id = request.session.get("agent_id") or request.session.get("username") or ""
            agent_name = request.session.get("display_name") or agent_id
            if body_text and store:
                try:
                    store.add_conv_note(
                        conversation_id, body_text,
                        agent_id=agent_id, agent_name=agent_name,
                    )
                    result["note_added"] = True
                except Exception:
                    pass

        elif action_type == "chain":
            # 启动工作链
            chain_id = str(config.get("chain_id") or "")
            if chain_id and store:
                try:
                    exec_id = store.start_chain_execution(
                        chain_id, conversation_id,
                        {"agent": request.session.get("username")},
                        schedule_first_step=True,
                    )
                    result["exec_id"] = exec_id
                except Exception:
                    pass

        elif action_type == "escalate":
            # 发布升级事件
            try:
                from src.integrations.shared.event_bus import get_event_bus
                get_event_bus().publish("escalation", {
                    "conversation_id": conversation_id,
                    "reason": str(config.get("reason") or "human_escalate"),
                    "initiated_by": request.session.get("username") or "",
                    "ts": now,
                })
                result["escalated"] = True
            except Exception:
                pass

        return result

    # ── 自定义动作管理 ────────────────────────────────────────────────────

    @app.get("/api/workspace/workflow-actions")
    async def api_workflow_actions_list(request: Request):
        """AA1：列出所有自定义动作。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "actions": []}
        actions = store.list_workflow_actions()
        import json as _j
        for a in actions:
            try:
                a["config"] = _j.loads(a.get("config_json") or "{}")
            except Exception:
                a["config"] = {}
        return {"ok": True, "actions": actions}

    @app.post("/api/workspace/workflow-actions")
    async def api_workflow_actions_create(request: Request, _=Depends(api_auth)):
        """AA1：创建自定义动作。"""
        body = await request.json()
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        action_id = store.upsert_workflow_action(body)
        return {"ok": True, "action_id": action_id}

    @app.put("/api/workspace/workflow-actions/{action_id}")
    async def api_workflow_actions_update(action_id: str, request: Request, _=Depends(api_auth)):
        body = await request.json()
        body["action_id"] = action_id
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        store.upsert_workflow_action(body)
        return {"ok": True, "action_id": action_id}

    @app.delete("/api/workspace/workflow-actions/{action_id}")
    async def api_workflow_actions_delete(action_id: str, request: Request, _=Depends(api_auth)):
        store = _inbox_store(request)
        if store is None:
            return {"ok": False}
        ok = store.delete_workflow_action(action_id)
        return {"ok": ok}

    # ── 工作链管理 ────────────────────────────────────────────────────────

    @app.get("/api/workspace/workflow-chains")
    async def api_workflow_chains_list(request: Request):
        """AA1：列出所有工作链。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "chains": []}
        import json as _j
        chains = store.list_workflow_chains()
        for c in chains:
            try:
                c["steps"] = _j.loads(c.get("steps_json") or "[]")
            except Exception:
                c["steps"] = []
            try:
                c["trigger_conditions"] = _j.loads(c.get("trigger_conditions") or "{}")
            except Exception:
                c["trigger_conditions"] = {}
        return {"ok": True, "chains": chains}

    @app.post("/api/workspace/workflow-chains")
    async def api_workflow_chains_create(request: Request, _=Depends(api_auth)):
        body = await request.json()
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": "inbox_store 不可用"}
        chain_id = store.upsert_workflow_chain(body)
        return {"ok": True, "chain_id": chain_id}

    @app.put("/api/workspace/workflow-chains/{chain_id}")
    async def api_workflow_chains_update(chain_id: str, request: Request, _=Depends(api_auth)):
        body = await request.json()
        body["chain_id"] = chain_id
        store = _inbox_store(request)
        if store is None:
            return {"ok": False}
        store.upsert_workflow_chain(body)
        return {"ok": True, "chain_id": chain_id}

    @app.delete("/api/workspace/workflow-chains/{chain_id}")
    async def api_workflow_chains_delete(chain_id: str, request: Request, _=Depends(api_auth)):
        store = _inbox_store(request)
        if store is None:
            return {"ok": False}
        ok = store.delete_workflow_chain(chain_id)
        return {"ok": ok}

    # ─── Phase 47: 工作链执行可视化 ─────────────────────────────────────

    @app.get("/api/workspace/chain-executions")
    async def api_chain_executions_list(
        request: Request,
        status: str = "",
        conversation_id: str = "",
        limit: int = 50,
    ):
        """P47：全局工作链执行监控列表。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "executions": [], "count": 0}
        from src.inbox.workflow_monitor import enrich_executions
        rows = store.list_chain_executions(
            status=status.strip(), conversation_id=conversation_id.strip(), limit=limit,
        )
        enriched = enrich_executions(rows)
        running = sum(1 for e in enriched if e.get("status") == "running")
        return {
            "ok": True,
            "executions": enriched,
            "count": len(enriched),
            "running_count": running,
        }

    @app.get("/api/workspace/conv/{conversation_id}/chain-executions")
    async def api_conv_chain_executions(
        conversation_id: str, request: Request, status: str = "", limit: int = 20,
    ):
        """P47：会话级工作链执行记录。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": True, "executions": [], "conversation_id": conversation_id}
        from src.inbox.workflow_monitor import enrich_executions
        rows = store.list_chain_executions(
            conversation_id=conversation_id, status=status.strip(), limit=limit,
        )
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "executions": enrich_executions(rows),
            "count": len(rows),
        }

    @app.post("/api/workspace/chain-executions/{exec_id}/cancel")
    async def api_cancel_chain_execution(
        exec_id: str, request: Request, _=Depends(api_auth),
    ):
        """P47：取消运行中的工作链执行。"""
        store = _inbox_store(request)
        if store is None:
            raise HTTPException(503, "inbox_store 不可用")
        ex = store.get_workflow_execution(exec_id)
        if not ex:
            raise HTTPException(404, "执行记录不存在")
        if ex.get("status") != "running":
            raise HTTPException(422, "仅可取消运行中的工作链")
        body = {}
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                body = await request.json()
            except Exception:
                body = {}
        reason = str(body.get("reason") or "坐席手动取消").strip()
        agent_id, agent_name = _agent_from_request(request)
        ok = store.cancel_workflow_execution(exec_id)
        if not ok:
            raise HTTPException(422, "取消失败")
        try:
            from src.integrations.shared.event_bus import get_event_bus
            import time as _t
            get_event_bus().publish("workflow_execution_cancelled", {
                "exec_id": exec_id,
                "conversation_id": ex.get("conversation_id"),
                "chain_id": ex.get("chain_id"),
                "chain_name": ex.get("chain_name", ""),
                "agent_id": agent_id,
                "agent_name": agent_name,
                "reason": reason,
                "ts": _t.time(),
            })
        except Exception:
            pass
        from src.inbox.workflow_monitor import enrich_execution
        refreshed = store.get_workflow_execution(exec_id)
        return {
            "ok": True,
            "exec_id": exec_id,
            "execution": enrich_execution(refreshed or ex),
        }

    @app.post("/api/workspace/conv/{conversation_id}/start-chain")
    async def api_conv_start_chain(conversation_id: str, request: Request, _=Depends(api_auth)):
        """AA1：为会话启动指定工作链。"""
        body = await request.json()
        chain_id = str(body.get("chain_id") or "")
        store = _inbox_store(request)
        if not chain_id or store is None:
            return {"ok": False, "error": "缺少 chain_id"}
        if store.has_running_chain(conversation_id, chain_id):
            return {"ok": False, "error": "该会话已有同链运行中"}
        exec_id = store.start_chain_execution(
            chain_id, conversation_id,
            {"agent": request.session.get("username") or ""},
            schedule_first_step=True,
        )
        return {"ok": True, "exec_id": exec_id, "conversation_id": conversation_id}
