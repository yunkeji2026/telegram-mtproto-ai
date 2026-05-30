"""WhatsApp RPA — Web 页面 + REST API 路由。

REST 端点：
    GET  /api/whatsapp-rpa/status          — 运行状态 / 统计
    GET  /api/whatsapp-rpa/recent          — 最近 N 条 run 历史
    GET  /api/whatsapp-rpa/conversations   — 对话聚合视图（按 chat_key 分组，含人设名）
    GET  /api/whatsapp-rpa/chat-history    — 指定联系人历史消息（chat_key 参数）
    GET  /api/whatsapp-rpa/alerts          — 告警列表
    POST /api/whatsapp-rpa/alerts/{id}/ack — 确认单条告警
    POST /api/whatsapp-rpa/alerts/ack_all  — 全部确认
    GET  /api/whatsapp-rpa/pending         — 待审批队列
    POST /api/whatsapp-rpa/pending/{id}/resolve — 审批
    POST /api/whatsapp-rpa/pause           — 暂停 N 秒
    POST /api/whatsapp-rpa/resume          — 恢复
    POST /api/whatsapp-rpa/trigger         — 立即触发
    POST /api/whatsapp-rpa/accept-contacts — 手动触发联系人接受
    GET  /api/whatsapp-rpa/device-screenshot — ADB 按需截屏
    GET  /api/whatsapp-rpa/config          — 当前有效配置
    PUT  /api/whatsapp-rpa/config          — 热更新配置
    GET  /api/whatsapp-rpa/log-tail        — 最近日志
    GET  /api/whatsapp-rpa/timeline        — 事件时间轴
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

logger = logging.getLogger(__name__)


def _get_service(request: Request):
    return getattr(request.app.state, "whatsapp_rpa_service", None)


def _get_services(request: Request) -> list:
    """返回所有 WA 账号的 service 列表（多账号支持）。"""
    svcs = getattr(request.app.state, "whatsapp_rpa_services", None)
    if svcs:
        return list(svcs)
    primary = _get_service(request)
    return [primary] if primary else []


def _redact_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    import copy
    c = copy.deepcopy(cfg)
    for sub in list(c.keys()):
        if not isinstance(c[sub], dict):
            continue
        for k in list(c[sub].keys()):
            if any(s in k.lower() for s in ("key", "secret", "token", "password")):
                if c[sub][k]:
                    c[sub][k] = "***"
    return c


def _group_alerts(alerts: list) -> list:
    """P5-4: API 层告警聚合 — 同一 kind 在 1h 内只保留最新一条，附加 count 字段。
    无 DB 变更，纯内存操作。"""
    import time as _time
    horizon = _time.time() - 3600
    groups: dict = {}
    recent_ids: set = set()
    for a in alerts:
        kind = a.get("kind") or "unknown"
        ts = float(a.get("ts") or 0)
        if ts >= horizon:
            if kind not in groups:
                groups[kind] = dict(a)
                groups[kind]["count"] = 1
                recent_ids.add(a.get("id"))
            else:
                groups[kind]["count"] += 1
                if ts > float(groups[kind].get("ts") or 0):
                    saved_count = groups[kind]["count"]
                    groups[kind] = dict(a)
                    groups[kind]["count"] = saved_count
        else:
            # 超过 1h 的告警直接附加 count=1 不合并
            a2 = dict(a)
            a2.setdefault("count", 1)
            groups[f"__old_{a.get('id')}"] = a2
    return list(groups.values())


def register_whatsapp_rpa_routes(
    app,
    *,
    page_auth,
    api_auth,
    templates,
    config_manager,
    audit_store=None,
):
    """在 FastAPI app 上挂载 WhatsApp RPA 相关路由。"""

    # ── Web 页面 ─────────────────────────────────────────────────────────

    @app.get("/whatsapp-rpa", response_class=HTMLResponse)
    async def whatsapp_rpa_page(request: Request, _=Depends(page_auth)):
        return templates.TemplateResponse(request, "whatsapp_rpa.html", {})

    # ── 状态 ─────────────────────────────────────────────────────────────

    @app.get("/api/whatsapp-rpa/status")
    async def api_wa_status(request: Request):
        api_auth(request)
        svcs = _get_services(request)
        if not svcs:
            return {"available": False, "running": False}
        # 主账号作为基础数据，多账号时合并 running/paused/KPI
        st = svcs[0].status()
        st["available"] = True
        for extra in svcs[1:]:
            try:
                es = extra.status()
                if es.get("running"):   st["running"] = True
                if es.get("enabled"):   st["enabled"] = True
                if es.get("available"): st["available"] = True
                for k in ("daily_sent", "daily_cap"):
                    if k in es: st[k] = (st.get(k) or 0) + (es.get(k) or 0)
            except Exception:
                pass
        return st

    @app.get("/api/whatsapp-rpa/recent")
    async def api_wa_recent(request: Request, limit: int = 50,
                            only_with_peer: int = 0):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "runs": []}
        return {
            "available": True,
            "runs": svc.recent_runs(limit=limit, only_with_peer=bool(only_with_peer)),
        }

    @app.get("/api/whatsapp-rpa/conversations")
    async def api_wa_conversations(request: Request, limit: int = 30,
                                   hours: float = 48.0):
        """对话聚合视图：按 chat_key 分组，每个联系人一行，附带人设名称。
        仅返回有真实 peer_text 的对话，100% 过滤心跳噪声。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "conversations": []}
        convs = svc.recent_conversations(limit=limit, hours=hours)

        # 构建 account_id → persona_name 映射（只查一次 PM）
        _acct_persona: Dict[str, str] = {}
        try:
            from src.utils.persona_manager import PersonaManager
            pm = PersonaManager.get_instance()
            wa_cfg = (config_manager.config or {}).get("whatsapp_rpa") or {}
            global_pids = list(wa_cfg.get("persona_ids") or [])
            for acc in (wa_cfg.get("accounts") or []):
                aid = str(acc.get("account_id") or acc.get("adb_serial") or "")
                pids = list(acc.get("persona_ids") or global_pids)
                if aid and pids:
                    p = pm.get_persona_by_id(pids[0])
                    _acct_persona[aid] = (p or {}).get("name", pids[0])
            if not _acct_persona and global_pids:
                p = pm.get_persona_by_id(global_pids[0])
                _acct_persona["default"] = (p or {}).get("name", global_pids[0])
        except Exception:
            pass

        # 拆解 chat_key "wa:account_id:peer_name" → 友好字段
        for conv in convs:
            ckey = conv.get("chat_key", "")
            parts = ckey.split(":", 2)
            aid = parts[1] if len(parts) >= 2 else ""
            conv["peer_name"] = parts[2] if len(parts) >= 3 else ckey
            conv["account_id"] = aid
            conv["persona_name"] = _acct_persona.get(aid) or _acct_persona.get("default") or ""

        return {"available": True, "conversations": convs}

    @app.get("/api/whatsapp-rpa/chat-history")
    async def api_wa_chat_history(
        request: Request, chat_key: str, limit: int = 10, offset: int = 0
    ):
        """指定联系人的历史消息列表（分页，含 intent_tag）。P6-A: 新增 offset 参数。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "history": [], "total": 0}
        if not chat_key:
            raise HTTPException(400, "chat_key 不能为空")
        history = svc.chat_history(chat_key=chat_key, limit=min(limit, 50), offset=offset)
        total = svc.total_turns_for_chat(chat_key=chat_key)
        return {
            "available": True, "history": history,
            "chat_key": chat_key, "total": total,
            "offset": offset, "limit": limit,
        }

    @app.get("/api/whatsapp-rpa/chat-history/{chat_key:path}")
    async def api_wa_chat_history_path(
        request: Request, chat_key: str, limit: int = 10, offset: int = 0
    ):
        """P11-A: 路径参数变体，与 LINE/Messenger 接口风格对齐（供 rpa.analytics.openDetail 调用）。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "history": [], "total": 0}
        history = svc.chat_history(chat_key=chat_key, limit=min(limit, 50), offset=offset)
        total = svc.total_turns_for_chat(chat_key=chat_key)
        return {
            "available": True, "history": history,
            "chat_key": chat_key, "total": total,
            "offset": offset, "limit": limit,
        }

    @app.get("/api/whatsapp-rpa/sessions/{chat_key:path}")
    async def api_wa_sessions(request: Request, chat_key: str):
        """P6-A: 返回联系人按 4h 间隔分组的会话摘要列表（含意图标签、轮次统计）。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "sessions": []}
        sessions = svc.sessions_for_chat(chat_key=chat_key)
        return {"available": True, "sessions": sessions, "chat_key": chat_key}

    @app.get("/api/whatsapp-rpa/customer-profile/{chat_key:path}")
    async def api_wa_customer_profile(request: Request, chat_key: str):
        """P7-B: 联系人全量画像（历史统计 + 意图分布 + 亲密度）。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "profile": {}}
        profile = svc.customer_profile(chat_key=chat_key)
        return {"available": True, "profile": profile, "chat_key": chat_key}

    @app.get("/api/whatsapp-rpa/search")
    async def api_wa_search(
        request: Request, q: str = "", intent: str = "",
        days: int = 30, limit: int = 20,
    ):
        """P7-A: 跨联系人全文检索聊天记录。q=关键词, intent=意图过滤, days=时间范围。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "results": [], "q": q}
        results = svc.search_history(q, intent=intent, days=days, limit=min(limit, 50))
        return {"available": True, "results": results, "q": q, "intent": intent, "days": days}

    @app.get("/api/whatsapp-rpa/intent-stats")
    async def api_wa_intent_stats(request: Request, hours: float = 168.0):
        """P7-D: 近 N 小时意图分布统计（默认 7 天）。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "distribution": {}, "total_turns": 0}
        stats = svc.intent_stats(window_hours=hours)
        return {"available": True, **stats}

    # ── P4-B: 手动发送队列 ───────────────────────────────────────────────

    @app.post("/api/whatsapp-rpa/send-manual")
    async def api_wa_send_manual(request: Request):
        """入队一条主动发送任务。Body: {chat_key, peer_name, text}"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "WhatsApp RPA 服务未初始化")
        body = await request.json()
        chat_key = (body.get("chat_key") or "").strip()
        peer_name = (body.get("peer_name") or "").strip()
        text = (body.get("text") or "").strip()
        if not chat_key or not peer_name or not text:
            raise HTTPException(400, "chat_key / peer_name / text 均不能为空")
        if len(text) > 2000:
            raise HTTPException(400, "消息过长（最大 2000 字）")
        item_id = svc.enqueue_send(chat_key=chat_key, peer_name=peer_name, text=text)
        return {"ok": True, "item_id": item_id, "status": "queued"}

    @app.get("/api/whatsapp-rpa/send-queue")
    async def api_wa_send_queue(request: Request, include_done: int = 0, limit: int = 30):
        """查看手动发送队列。include_done=1 时同时返回已完成的记录。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "queue": []}
        items = svc.list_send_queue(limit=min(limit, 50), include_done=bool(include_done))
        return {"available": True, "queue": items, "total": len(items)}

    @app.get("/api/whatsapp-rpa/send-queue/{item_id}")
    async def api_wa_send_queue_item(item_id: int, request: Request):
        """P15-C: 单条 send-queue 查询（替代客户端拉 50 条找 id）。
        P16-C: 支持 ETag/304 — 状态未变时返回空体，零字节传输。
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service_unavailable")
        item = svc.get_send_queue_item(int(item_id))
        if not item:
            raise HTTPException(404, "item_not_found")
        # P16-C: ETag = status + 关键时间戳（ts/sent_at）
        etag_seed = f"{item.get('status','')}-{item.get('ts','')}-{item.get('sent_at','')}"
        import hashlib as _h
        etag = '"' + _h.sha1(etag_seed.encode("utf-8")).hexdigest()[:16] + '"'
        if request.headers.get("if-none-match") == etag:
            from fastapi import Response
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
        from fastapi.responses import JSONResponse
        return JSONResponse(
            content={"available": True, "item": item},
            headers={"ETag": etag, "Cache-Control": "no-cache"},
        )

    @app.post("/api/whatsapp-rpa/send-queue/{item_id}/cancel")
    async def api_wa_send_queue_cancel(item_id: int, request: Request):
        """取消一条排队中的发送任务。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "WhatsApp RPA 服务未初始化")
        svc.cancel_send_queue_item(item_id)
        return {"ok": True, "item_id": item_id}

    # ── 告警 ─────────────────────────────────────────────────────────────

    @app.get("/api/whatsapp-rpa/alerts")
    async def api_wa_alerts(
        request: Request, only_unacked: int = 1, limit: int = 50, grouped: int = 1
    ):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "alerts": [], "unacked": 0}
        raw = svc.list_alerts(only_unacked=bool(only_unacked), limit=limit)
        alerts = _group_alerts(raw) if grouped else raw
        return {
            "available": True,
            "alerts": alerts,
            "unacked": svc.alerts_count_unacked(),
        }

    @app.post("/api/whatsapp-rpa/alerts/{alert_id}/ack")
    async def api_wa_alerts_ack(alert_id: int, request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "WhatsApp RPA 服务未启动")
        actor = getattr(request.state, "user", {}).get("username", "web")
        res = svc.ack_alert(alert_id, by=actor)
        if res is None:
            raise HTTPException(404, "alert_not_found")
        return {"ok": True, "item": res}

    @app.post("/api/whatsapp-rpa/alerts/ack_all")
    async def api_wa_alerts_ack_all(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "WhatsApp RPA 服务未启动")
        actor = getattr(request.state, "user", {}).get("username", "web")
        n = svc.ack_all_alerts(by=actor)
        return {"ok": True, "acked": n}

    # ── 待审批队列 ────────────────────────────────────────────────────────

    @app.get("/api/whatsapp-rpa/pending")
    async def api_wa_pending(request: Request, status: str = "", limit: int = 50):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "items": [], "stats": {}}
        items = svc.list_pending(status=status or None, limit=limit)
        return {"available": True, "items": items, "stats": svc.pending_stats()}

    @app.get("/api/whatsapp-rpa/pending-tts")
    async def api_wa_pending_tts(request: Request, ids: str = ""):
        """P14-A: 返回指定 pending_id 列表的 tts_path 映射，供前端轮询。

        Query: ?ids=1,2,3  Returns: {"ok":true,"paths":{"1":"...", "2":""}}
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"ok": False, "paths": {}}
        ss = getattr(svc, "_state_store", None) or getattr(
            getattr(svc, "_runner", None), "_state_store", None
        )
        if ss is None:
            return {"ok": False, "paths": {}}
        try:
            id_list = [int(x.strip()) for x in ids.split(",") if x.strip().isdigit()]
        except Exception:
            return {"ok": False, "paths": {}}
        if not id_list:
            return {"ok": True, "paths": {}}
        result = {}
        for pid in id_list[:20]:
            row = ss.get_pending(pid)
            result[str(pid)] = str(row.get("tts_path") or "") if row else ""
        return {"ok": True, "paths": result}

    @app.post("/api/whatsapp-rpa/pending/{pending_id}/retry-tts")
    async def api_wa_pending_retry_tts(pending_id: int, request: Request):
        """P14-C: 重置 WA pending TTS ERROR 哨兵，下一轮自动重新生成。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"ok": False, "error": "service unavailable"}
        ss = getattr(svc, "_state_store", None) or getattr(
            getattr(svc, "_runner", None), "_state_store", None
        )
        if ss is None:
            return {"ok": False, "error": "state store unavailable"}
        ok = ss.reset_pending_tts(pending_id)
        if not ok:
            return {"ok": False, "error": "not found or not pending/approved"}
        return {"ok": True, "pending_id": pending_id}

    @app.post("/api/whatsapp-rpa/pending/cancel-all")
    async def api_wa_pending_cancel_all(request: Request):
        """P13-D: 立即取消所有 WA pending 行。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"ok": False, "error": "service unavailable"}
        ss = getattr(svc, "_state_store", None) or getattr(
            getattr(svc, "_runner", None), "_state_store", None
        )
        if ss is None:
            return {"ok": False, "error": "state store unavailable"}
        cancelled = ss.cancel_all_open_pending()
        if audit_store and cancelled:
            try:
                actor = getattr(request.state, "user", {}).get("username", "web")
                audit_store.log(actor, "wa_pending_cancel_all", f"cancelled={len(cancelled)}")
            except Exception:
                pass
        return {"ok": True, "cancelled": len(cancelled)}

    @app.post("/api/whatsapp-rpa/pending/{pending_id}/resolve")
    async def api_wa_pending_resolve(pending_id: int, request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "WhatsApp RPA 服务未启动")
        body = await request.json()
        action = str(body.get("action", "reject"))
        if action not in {"approve", "reject", "send"}:
            raise HTTPException(400, "action 需为 approve/reject/send")
        actor = getattr(request.state, "user", {}).get("username", "web")
        res = svc.resolve_pending(pending_id, action, by=actor)
        if res is None:
            raise HTTPException(404, "pending_not_found")
        if audit_store:
            audit_store.log(actor, "wa_rpa_pending_resolve",
                            f"id={pending_id} action={action}")
        return {"ok": True, "item": res}

    # ── 时间轴 ────────────────────────────────────────────────────────────

    @app.get("/api/whatsapp-rpa/timeline")
    async def api_wa_timeline(request: Request, minutes: int = 60, limit: int = 200):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "items": []}
        return {
            "available": True,
            "items": svc.timeline(minutes=minutes, limit=limit),
            "minutes": minutes,
        }

    # ── 控制 ─────────────────────────────────────────────────────────────

    @app.post("/api/whatsapp-rpa/pause")
    async def api_wa_pause(request: Request):
        api_auth(request)
        svcs = _get_services(request)
        if not svcs:
            raise HTTPException(503, "WhatsApp RPA 服务未启动")
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        seconds = float(body.get("seconds", 300) or 300)
        for svc in svcs:
            svc.pause_for(seconds)
        actor = getattr(request.state, "user", {}).get("username", "web")
        if audit_store:
            audit_store.log(actor, "wa_rpa_pause", f"seconds={int(seconds)}")
        return {"ok": True, "pause_remaining_sec": int(seconds)}

    @app.post("/api/whatsapp-rpa/resume")
    async def api_wa_resume(request: Request):
        api_auth(request)
        svcs = _get_services(request)
        if not svcs:
            raise HTTPException(503, "WhatsApp RPA 服务未启动")
        for svc in svcs:
            svc.resume()
        actor = getattr(request.state, "user", {}).get("username", "web")
        if audit_store:
            audit_store.log(actor, "wa_rpa_resume", "")
        return {"ok": True}

    @app.post("/api/whatsapp-rpa/trigger")
    async def api_wa_trigger(request: Request):
        api_auth(request)
        svcs = _get_services(request)
        if not svcs:
            raise HTTPException(503, "WhatsApp RPA 服务未启动")
        any_started = False
        for svc in svcs:
            if hasattr(svc, "is_running") and not svc.is_running:
                if hasattr(svc, "force_start"):
                    started = await svc.force_start()
                    if started:
                        any_started = True
            svc.trigger_once()
        primary = svcs[0]
        return {"ok": True, "auto_started": any_started, "is_running": getattr(primary, "is_running", None)}

    @app.post("/api/whatsapp-rpa/reset-circuit-breaker")
    async def api_wa_reset_circuit_breaker(request: Request):
        """重置 WhatsApp（及可选其他平台）的熔断器，立即解除等待。"""
        api_auth(request)
        dc_svc = getattr(request.app.state, "device_coordinator_service", None)
        if dc_svc is None:
            raise HTTPException(503, "DeviceCoordinatorService 未启动")
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        serial = body.get("serial") or None
        platform = body.get("platform") or "whatsapp"
        reset = dc_svc.reset_circuit_breaker(serial=serial, platform_type=platform)
        return {"ok": True, "reset": reset}

    @app.post("/api/whatsapp-rpa/accept-contacts")
    async def api_wa_accept_contacts(request: Request):
        """手动触发联系人申请接受（在当前屏幕 XML 查找同意按钮）。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "WhatsApp RPA 服务未启动")
        try:
            res = await svc._runner.maybe_auto_accept_contacts(max_accept=10)
        except Exception as e:
            raise HTTPException(500, str(e))
        return {"ok": True, "result": res}

    @app.get("/api/whatsapp-rpa/device-screenshot")
    async def api_wa_device_screenshot(request: Request):
        """按需从 ADB 设备截屏，返回 PNG。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "WhatsApp RPA 服务未启动")
        runner = svc._runner
        serial = runner._serial
        if not serial:
            raise HTTPException(503, "未找到 ADB 设备（serial 未设置）")
        try:
            from src.integrations.line_rpa import screen_ocr, adb_helpers as _adb
            png = await asyncio.to_thread(screen_ocr.capture_screen_png, serial, _adb)
        except Exception as e:
            raise HTTPException(500, f"截屏失败: {e}")
        if not png:
            raise HTTPException(503, "截屏返回空（ADB 设备未就绪）")
        return Response(content=png, media_type="image/png")

    # ── 配置 ─────────────────────────────────────────────────────────────

    @app.get("/api/whatsapp-rpa/config")
    async def api_wa_config(request: Request):
        api_auth(request)
        svc = _get_service(request)
        raw_cfg = (config_manager.config or {}).get("whatsapp_rpa", {}) or {}
        effective = svc.effective_config() if svc else raw_cfg
        return {
            "raw": _redact_cfg(raw_cfg),
            "effective": _redact_cfg(effective),
        }

    @app.put("/api/whatsapp-rpa/config")
    async def api_wa_config_update(request: Request):
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be object")

        ALLOWED = {
            "enabled", "adb_serial", "use_business_app",
            "default_reply_lang", "daily_cap", "reply_mode",
            "after_launch_sleep_sec", "service", "human_pacing",
            "auto_accept", "voice_input", "voice_output", "media_input",
            "proactive", "quiet_until", "blacklist",
            "stop_contact_quiet_minutes", "stop_contact_blacklist", "stop_contact_keywords",
            "stop_contact_escalation_hours",
            "stop_contact_strong_threshold", "stop_contact_weak_threshold", "stop_contact_enable_negative_check",
            "proactive_templates",
            "emoticons",
        }
        bad = [k for k in body.keys() if k not in ALLOWED]
        if bad:
            raise HTTPException(400, f"不允许的字段: {bad}")

        cfg = config_manager.config or {}
        wa = cfg.get("whatsapp_rpa") or {}
        if not isinstance(wa, dict):
            wa = {}
        for k, v in body.items():
            if isinstance(v, dict) and isinstance(wa.get(k), dict):
                wa[k] = {**wa[k], **v}
            else:
                wa[k] = v
        cfg["whatsapp_rpa"] = wa
        config_manager.config = cfg
        try:
            config_manager.save()
        except Exception as e:
            raise HTTPException(500, f"配置保存失败: {e}")

        svc = _get_service(request)
        if svc:
            svc.reconfigure(wa)

        actor = getattr(request.state, "user", {}).get("username", "web")
        if audit_store:
            audit_store.log(actor, "wa_rpa_config_update", str(list(body.keys())))
        return {"ok": True, "updated_keys": list(body.keys())}

    @app.get("/api/whatsapp-rpa/proactive-stats")
    async def api_wa_proactive_stats(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "WhatsApp RPA 服务未启动")
        try:
            return {"ok": True, **svc.proactive_status()}
        except Exception as e:
            raise HTTPException(500, f"proactive_stats 失败: {e}")

    @app.get("/api/whatsapp-rpa/proactive-metrics")
    async def api_wa_proactive_metrics(request: Request):
        """轻量版 metrics：包含 stop_contact 触发数、黑名单/静默活跃数。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "WhatsApp RPA 服务未启动")
        try:
            data = svc.proactive_status()
            return {
                "ok": True,
                "stop_contact_hits_24h": data.get("stop_contact_hits_24h", 0),
                "stop_contact_state": data.get("stop_contact_state") or {},
                "proactive_sent_today": data.get("sent_today", 0),
            }
        except Exception as e:
            raise HTTPException(500, f"proactive_metrics 失败: {e}")

    # ── P15-c: 防骚扰/静默控制 ────────────────────────────────────────────

    @app.post("/api/whatsapp-rpa/chat-quiet")
    async def api_wa_chat_quiet(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "WhatsApp RPA 服务未启动")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON body")
        chat_key = str(body.get("chat_key") or "").strip()
        minutes = float(body.get("minutes") or 0)
        if not chat_key:
            raise HTTPException(400, "chat_key is required")
        try:
            svc.set_chat_quiet(chat_key, minutes)
        except Exception as e:
            raise HTTPException(500, f"set_chat_quiet 失败: {e}")
        return {"ok": True, "chat_key": chat_key, "minutes": minutes}

    @app.post("/api/whatsapp-rpa/chat-blacklist")
    async def api_wa_chat_blacklist(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "WhatsApp RPA 服务未启动")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON body")
        chat_key = str(body.get("chat_key") or "").strip()
        flag_raw = body.get("blacklist", True)
        flag = bool(flag_raw)
        if not chat_key:
            raise HTTPException(400, "chat_key is required")
        try:
            svc.set_chat_blacklist(chat_key, flag)
        except Exception as e:
            raise HTTPException(500, f"set_chat_blacklist 失败: {e}")
        return {"ok": True, "chat_key": chat_key, "blacklist": flag}

    # ── P4-A: 对话语言锁定 ────────────────────────────────────────────────

    @app.post("/api/whatsapp-rpa/chat-lang-lock")
    async def api_wa_chat_lang_lock(request: Request):
        """锁定或解锁指定对话的 TTS/回复语言。

        Body: {chat_key: str, lang: str|null}
          lang = XTTS 代码（如 "de"/"ja"/"zh-cn"）→ 锁定
          lang = null/""  → 解除锁定（恢复自动检测）
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "WhatsApp RPA 服务未启动")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON body")
        chat_key = str(body.get("chat_key") or "").strip()
        if not chat_key:
            raise HTTPException(400, "chat_key is required")
        lang_raw = body.get("lang")
        lang = str(lang_raw).strip() if lang_raw else ""

        from src.integrations.whatsapp_rpa.lang_detect import XTTS_SUPPORTED
        if lang and lang not in XTTS_SUPPORTED:
            raise HTTPException(400, f"不支持的语言代码: {lang}。支持: {sorted(XTTS_SUPPORTED)}")

        store = getattr(svc, "_state_store", None) or getattr(
            getattr(svc, "_runner", None), "_state_store", None
        )
        if store is None:
            raise HTTPException(503, "state_store 不可用")
        try:
            store.upsert_chat_state(chat_key, forced_lang=lang or None)
        except Exception as e:
            raise HTTPException(500, f"写入失败: {e}")
        # P10-D: 语言已变更—立即让 lang-dist 缓存失效
        try:
            from src.web.routes.rpa_overview_routes import invalidate_lang_dist_cache
            invalidate_lang_dist_cache()
        except Exception:
            pass
        # P13-E: 记录最近一次语言锁变更时间
        try:
            import time as _t
            svc._last_lang_lock_ts = _t.time()
        except Exception:
            pass
        action = f"锁定为 {lang}" if lang else "解除锁定（恢复自动检测）"
        return {"ok": True, "chat_key": chat_key, "forced_lang": lang or None, "action": action}

    # ── 多语言 TTS 测试（P3-D） ───────────────────────────────────────────

    @app.post("/api/whatsapp-rpa/tts-test")
    async def api_wa_tts_test(request: Request):
        """生成 WhatsApp 语音预览（使用真实 voice_output 配置 + 语言覆盖）。

        Body: {text: str, language?: str}
        language 为 XTTS-v2 语言代码，如 "de"/"ja"/"zh-cn"；省略时使用配置默认值。
        Returns: {ok, url, duration_sec, provider, language, error?}
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON body")
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text is required")
        if len(text) > 400:
            raise HTTPException(400, "text too long (max 400 chars)")
        language: Optional[str] = str(body.get("language") or "").strip() or None

        wa_cfg = (config_manager.config or {}).get("whatsapp_rpa") or {}
        voice_cfg: Dict[str, Any] = dict(wa_cfg.get("voice_output") or {})
        voice_cfg["enabled"] = True
        if language:
            vp = dict(voice_cfg.get("voice_profile") or {})
            vp["language"] = language
            voice_cfg["voice_profile"] = vp

        from pathlib import Path as _Path
        import uuid as _uuid
        _preview_dir = _Path("tmp_tts_preview")
        _preview_dir.mkdir(parents=True, exist_ok=True)
        uid = _uuid.uuid4().hex[:10]
        suffix = voice_cfg.get("format", "wav")
        preview_path = _preview_dir / f"ttspreview-{uid}.{suffix}"

        try:
            from src.ai.tts_pipeline import get_tts_pipeline
            tts = get_tts_pipeline(voice_cfg)
            rv = await asyncio.wait_for(
                tts.synthesize(text, timeout_sec=float(voice_cfg.get("timeout_sec", 60) or 60)),
                timeout=65.0,
            )
        except Exception as e:
            logger.warning("[wa_rpa/tts-test] error: %s", e)
            return {"ok": False, "error": str(e)[:300]}

        if not rv.ok:
            return {"ok": False, "error": rv.error or "TTS 合成失败"}

        try:
            _Path(rv.audio_path).rename(preview_path)
        except Exception:
            preview_path = _Path(rv.audio_path)

        _eff_lang = language or str(
            (wa_cfg.get("voice_output") or {}).get("voice_profile", {}).get("language") or "?"
        )
        return {
            "ok": True,
            "url": f"/api/voice/tts-test/{preview_path.name}",
            "filename": preview_path.name,
            "duration_sec": round(rv.duration_sec, 2),
            "provider": rv.provider,
            "language": _eff_lang,
            "format": rv.format,
        }

    # ── 语音管道统计 ──────────────────────────────────────────────────────

    @app.get("/api/whatsapp-rpa/voice-metrics")
    async def api_wa_voice_metrics(request: Request):
        """返回语音管道 STT/TTS 统计（内存级，重启清零）。"""
        api_auth(request)
        svc = _get_service(request)
        runner = getattr(svc, "_runner", None) if svc else None
        if runner is None:
            return {"ok": False, "error": "runner_not_available", "metrics": {}}
        return {"ok": True, "metrics": runner.get_voice_metrics()}

    @app.get("/api/whatsapp-rpa/media-metrics")
    async def api_wa_media_metrics(request: Request):
        """返回媒体管道统计（内存级，重启清零）。"""
        api_auth(request)
        svc = _get_service(request)
        runner = getattr(svc, "_runner", None) if svc else None
        if runner is None:
            return {"ok": False, "error": "runner_not_available", "metrics": {}}
        return {"ok": True, "metrics": runner.get_media_metrics()}

    @app.get("/api/whatsapp-rpa/pipeline-metrics")
    async def api_wa_pipeline_metrics(request: Request):
        """合并返回语音 + 媒体全管道统计。"""
        api_auth(request)
        svc = _get_service(request)
        runner = getattr(svc, "_runner", None) if svc else None
        if runner is None:
            return {"ok": False, "error": "runner_not_available", "voice": {}, "media": {}}
        return {
            "ok": True,
            "voice": runner.get_voice_metrics(),
            "media": runner.get_media_metrics(),
        }

    # ── 日志流 ────────────────────────────────────────────────────────────

    @app.get("/api/whatsapp-rpa/log-tail", response_class=PlainTextResponse)
    async def api_wa_log_tail(request: Request, n: int = 80):
        """最近 N 行 WhatsApp RPA 相关日志。"""
        api_auth(request)
        for candidate in ("logs/app.log", "logs/bot.log", "app.log"):
            p = Path(candidate)
            if p.exists():
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                    lines = [l for l in text.splitlines()
                             if "whatsapp_rpa" in l.lower() or "WhatsAppRpa" in l]
                    return PlainTextResponse(
                        "\n".join(lines[-max(1, min(200, n)):])
                    )
                except Exception as e:
                    return PlainTextResponse(f"Error reading {candidate}: {e}")
        return PlainTextResponse("")

    # ── P15-i: 主动续聊模板效果分析 ────────────────────────────────────────

    @app.get("/api/whatsapp-rpa/template-analytics")
    async def api_wa_template_analytics(request: Request, hours: int = 168):
        """返回主动续聊模板效果分析数据（默认7天）。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"ok": False, "error": "service_not_available"}

        try:
            # 获取服务中的模板统计
            status = svc.proactive_status()
            tpl_stats = status.get("template_stats", {})
            replied_48h = status.get("proactive_replied_48h", 0)
            replied_by_cat = status.get("replied_by_category", {})

            # 从 timeline 获取详细数据
            state = getattr(svc, "_state", None)
            timeline_data = []
            if state:
                try:
                    tl = state.timeline(minutes=hours*60, limit=2000)
                    for rec in tl:
                        kind = rec.get("kind")
                        if kind in ("proactive_sent", "proactive_replied"):
                            detail = json.loads(rec.get("detail") or "{}")
                            timeline_data.append({
                                "ts": rec.get("ts"),
                                "kind": kind,
                                "chat_key": detail.get("chat_key"),
                                "category": detail.get("template_category") or detail.get("category"),
                                "idx": detail.get("template_idx") or detail.get("idx"),
                            })
                except Exception:
                    pass

            # 计算各类别回复率
            category_performance = {}
            for cat, stat in tpl_stats.items():
                sent = stat.get("sent", 0)
                replied = replied_by_cat.get(cat, 0)
                category_performance[cat] = {
                    "sent": sent,
                    "replied": replied,
                    "reply_rate": round(replied / sent, 3) if sent > 0 else 0.0,
                }

            return {
                "ok": True,
                "hours": hours,
                "template_stats": tpl_stats,
                "replied_48h": replied_48h,
                "replied_by_category": replied_by_cat,
                "category_performance": category_performance,
                "timeline_count": len(timeline_data),
                "config": {
                    "rotation_strategy": svc._merged_cfg.get("proactive_templates", {}).get("rotation_strategy", "round_robin"),
                    "ab_test_enabled": svc._merged_cfg.get("proactive_templates", {}).get("ab_test_enabled", True),
                },
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}
