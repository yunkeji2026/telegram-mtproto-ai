"""Messenger RPA Web/REST 路由。

挂载点（参考 line_rpa_routes 但更精简）：
    GET  /messenger-rpa                       — 简易卡片页（待补 HTML 模板）
    GET  /api/messenger-rpa/status            — service 状态 + 最近一次 run
    GET  /api/messenger-rpa/recent            — 最近 N 条 run 历史
    GET  /api/messenger-rpa/approvals         — 待审批/全部审批列表
    GET  /api/messenger-rpa/approvals/{id}    — 单条审批详情
    POST /api/messenger-rpa/approvals/{id}/approve  — 批准 → 后台自动发送
    POST /api/messenger-rpa/approvals/{id}/reject   — 驳回
    POST /api/messenger-rpa/trigger           — 立即跑一次 run_once
    POST /api/messenger-rpa/accounts/{id}/send-to — 指定账号向某会话名发送固定文本（不经 LLM）
    POST /api/messenger-rpa/pause             — {"seconds":300} 暂停 N 秒
    POST /api/messenger-rpa/resume            — 恢复

依赖：
- request.app.state.messenger_rpa_service: MessengerRpaService
- request.app.state.messenger_rpa_state_store: MessengerRpaStateStore
"""
from __future__ import annotations

import copy
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)


_SENSITIVE_KEYS = {
    "api_key", "token", "secret", "password", "authorization",
    "zhipu_api_key", "openai_api_key", "telegram_bot_token",
}


def _redact_cfg(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if str(k).lower() in _SENSITIVE_KEYS:
                out[k] = "***"
            else:
                out[k] = _redact_cfg(v)
        return out
    if isinstance(obj, list):
        return [_redact_cfg(v) for v in obj]
    return obj


def _messenger_cfg(config_manager: Any) -> Dict[str, Any]:
    cfg = getattr(config_manager, "config", None) or {}
    mr = cfg.get("messenger_rpa") or {}
    return mr if isinstance(mr, dict) else {}


def _save_messenger_cfg(config_manager: Any, mr_cfg: Dict[str, Any]) -> None:
    root = getattr(config_manager, "config", None)
    if not isinstance(root, dict):
        root = {}
        config_manager.config = root
    root["messenger_rpa"] = mr_cfg
    ok = config_manager.save()
    if ok is False:
        raise HTTPException(500, "保存 messenger_rpa 配置失败")


def _refresh_service_runtime(request: Request, mr_cfg: Dict[str, Any]) -> None:
    svc = _get_service(request)
    if svc is None:
        return
    try:
        setattr(svc, "_cfg", dict(mr_cfg))
        if hasattr(svc, "_merged"):
            setattr(svc, "_merged_cfg", svc._merged())
        live_cfg = svc._reload_runtime_cfg() if hasattr(svc, "_reload_runtime_cfg") else mr_cfg
        runner = getattr(svc, "_runner", None)
        if runner is not None and hasattr(runner, "refresh_cfg"):
            runner.refresh_cfg(live_cfg)
        for r in getattr(svc, "_runners", {}).values():
            if hasattr(r, "refresh_cfg"):
                r.refresh_cfg(live_cfg)
    except Exception:
        logger.debug("messenger_rpa runtime config refresh failed", exc_info=True)


def _normalize_profiles(payload: Dict[str, Any]) -> Dict[str, Any]:
    default_id = str(payload.get("default") or "").strip()
    profiles = payload.get("profiles") or []
    if not isinstance(profiles, list):
        raise HTTPException(400, "profiles 必须是数组")
    seen = set()
    clean: List[Dict[str, Any]] = []
    for raw in profiles:
        if not isinstance(raw, dict):
            raise HTTPException(400, "profile 必须是对象")
        item = copy.deepcopy(raw)
        pid = str(item.get("id") or item.get("name") or "").strip()
        if not pid:
            raise HTTPException(400, "profile.id 不能为空")
        if pid in seen:
            raise HTTPException(400, f"profile.id 重复: {pid}")
        seen.add(pid)
        item["id"] = pid
        lang = str(item.get("language") or "auto").strip() or "auto"
        item["language"] = lang
        clean.append(item)
    if default_id and default_id not in seen:
        raise HTTPException(400, f"default profile 不存在: {default_id}")
    if not default_id and clean:
        default_id = str(clean[0]["id"])
    return {"default": default_id, "profiles": clean}


def _profile_id_for_chat(reply_profiles: Dict[str, Any], chat_key: str, chat_name: str) -> str:
    profiles = reply_profiles.get("profiles") or []
    default_id = str(reply_profiles.get("default") or "")
    ck = (chat_key or "").lower()
    cn = (chat_name or "").lower()
    for p in profiles:
        if not isinstance(p, dict):
            continue
        keys = p.get("match_chat_keys") or []
        names = p.get("match_names") or []
        if isinstance(keys, str):
            keys = [keys]
        if isinstance(names, str):
            names = [names]
        if any(str(k).strip().lower() and str(k).strip().lower() in ck for k in keys):
            return str(p.get("id") or "")
        if any(str(n).strip().lower() and str(n).strip().lower() in cn for n in names):
            return str(p.get("id") or "")
    return default_id


def _get_service(request: Request):
    return getattr(request.app.state, "messenger_rpa_service", None)


def _get_store(request: Request):
    return getattr(request.app.state, "messenger_rpa_state_store", None)


def register_messenger_rpa_routes(
    app,
    *,
    page_auth,
    api_auth,
    templates,
    config_manager,
):
    """挂 Messenger RPA 的 Web + REST 路由。"""

    # ── Web: HTML 页 ────────────────────────────────
    @app.get("/messenger-rpa", response_class=HTMLResponse)
    async def messenger_rpa_page(request: Request):
        # 手动调 page_auth（支持 sync 或 async 都在这里兜）
        res = page_auth(request)
        if hasattr(res, "__await__"):
            await res
        return templates.TemplateResponse(request, "messenger_rpa.html", {})

    # ── REST: 状态 ─────────────────────────────────
    @app.get("/api/messenger-rpa/status")
    async def api_msgr_status(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            st: Dict[str, Any] = {
                "available": False,
                "enabled_cfg": bool(
                    (config_manager.config or {})
                    .get("messenger_rpa", {})
                    .get("enabled")
                ),
                "hint": (
                    "messenger_rpa.enabled=false 或服务未构建；"
                    "在 config.yaml 中开启后重启进程"
                ),
            }
        else:
            st = svc.status()
            st["available"] = True
        # escalation 占位行计数：store-derived 字段，svc 无关，两路都附加
        store = _get_store(request)
        if store is not None:
            try:
                st["pending_empty_count"] = store.count_approvals(
                    status="pending", reply_text_empty=True,
                )
            except Exception:
                logger.exception("pending_empty_count 查询失败")
                st["pending_empty_count"] = -1
        return st

    @app.get("/api/messenger-rpa/config")
    async def api_msgr_config(request: Request):
        """Return Messenger operator-facing configuration."""
        api_auth(request)
        raw_cfg = copy.deepcopy(_messenger_cfg(config_manager))
        svc = _get_service(request)
        effective = raw_cfg
        if svc is not None and hasattr(svc, "_reload_runtime_cfg"):
            try:
                effective = svc._reload_runtime_cfg()
            except Exception:
                effective = raw_cfg
        ops_keys = [
            "enabled", "autostart", "reply_mode", "max_inbox_per_run",
            "run_once_target_names", "target_chat_names",
            "companion_reply_cooldown_sec", "suppress_global_ai_identity",
            "disable_episodic_memory", "language_alignment",
            "default_reply_lang", "companion_mode",
        ]
        return {
            "raw": _redact_cfg(raw_cfg),
            "effective": _redact_cfg(effective),
            "operations": {k: raw_cfg.get(k) for k in ops_keys if k in raw_cfg},
            "accounts": raw_cfg.get("accounts") or [],
            "reply_profiles": raw_cfg.get("reply_profiles") or {},
            "lead_qualification": raw_cfg.get("lead_qualification") or {},
            "safety": raw_cfg.get("safety") or {},
        }

    @app.put("/api/messenger-rpa/config")
    async def api_msgr_config_update(request: Request):
        """Patch safe Messenger RPA settings from the operations console."""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body 必须是对象")
        allowed = {
            "enabled", "autostart", "reply_mode", "max_inbox_per_run",
            "run_once_target_names", "target_chat_names", "test_target_names",
            "companion_reply_cooldown_sec", "suppress_global_ai_identity",
            "disable_episodic_memory", "language_alignment",
            "default_reply_lang", "companion_mode", "lead_qualification",
        }
        bad = [k for k in body.keys() if k not in allowed]
        if bad:
            raise HTTPException(400, f"不允许的字段: {bad}")
        mr_cfg = copy.deepcopy(_messenger_cfg(config_manager))
        for k, v in body.items():
            if k == "lead_qualification":
                if not isinstance(v, dict):
                    raise HTTPException(400, "lead_qualification 必须是对象")
                cur = mr_cfg.get(k) if isinstance(mr_cfg.get(k), dict) else {}
                merged = copy.deepcopy(cur)
                for lk, lv in v.items():
                    if isinstance(lv, dict) and isinstance(merged.get(lk), dict):
                        sub = dict(merged[lk])
                        sub.update(lv)
                        merged[lk] = sub
                    else:
                        merged[lk] = lv
                mr_cfg[k] = merged
            elif k in ("run_once_target_names", "target_chat_names", "test_target_names"):
                if isinstance(v, str):
                    mr_cfg[k] = [x.strip() for x in v.split(",") if x.strip()]
                elif isinstance(v, list):
                    mr_cfg[k] = [str(x).strip() for x in v if str(x or "").strip()]
                else:
                    raise HTTPException(400, f"{k} 必须是字符串或数组")
            else:
                mr_cfg[k] = v
        _save_messenger_cfg(config_manager, mr_cfg)
        _refresh_service_runtime(request, mr_cfg)
        return {"ok": True, "updated_keys": list(body.keys())}

    @app.get("/api/messenger-rpa/personas")
    async def api_msgr_personas(request: Request):
        api_auth(request)
        cfg = _messenger_cfg(config_manager)
        return {
            "reply_profiles": cfg.get("reply_profiles") or {},
            "experiment": cfg.get("persona_experiment") or {},
        }

    @app.put("/api/messenger-rpa/personas")
    async def api_msgr_personas_update(request: Request):
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body 必须是对象")
        rp_body = body.get("reply_profiles", body)
        if not isinstance(rp_body, dict):
            raise HTTPException(400, "reply_profiles 必须是对象")
        normalized = _normalize_profiles(rp_body)
        mr_cfg = copy.deepcopy(_messenger_cfg(config_manager))
        mr_cfg["reply_profiles"] = normalized
        _save_messenger_cfg(config_manager, mr_cfg)
        _refresh_service_runtime(request, mr_cfg)
        return {"ok": True, "reply_profiles": normalized}

    @app.get("/api/messenger-rpa/leads")
    async def api_msgr_leads(request: Request, limit: int = 100):
        """List recent Messenger contacts with ICP/qualification evidence."""
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        cfg = _messenger_cfg(config_manager)
        reply_profiles = cfg.get("reply_profiles") or {}
        chat_states = []
        if hasattr(store, "list_chat_states"):
            chat_states = store.list_chat_states(limit=int(limit or 100))

        contexts: Dict[str, Dict[str, Any]] = {}
        try:
            cfg_dir = Path(config_manager.config_path).parent
            db = cfg_dir / "bot.db"
            if db.exists():
                c = sqlite3.connect(str(db))
                c.row_factory = sqlite3.Row
                rows = c.execute(
                    "SELECT user_id, data, updated_at FROM user_context "
                    "WHERE user_id LIKE ? ORDER BY updated_at DESC LIMIT ?",
                    ("acc_%:%", max(int(limit or 100) * 2, 200)),
                ).fetchall()
                c.close()
                for row in rows:
                    try:
                        data = json.loads(row["data"] or "{}") or {}
                    except Exception:
                        data = {}
                    data["_ctx_updated_at"] = row["updated_at"]
                    contexts[str(row["user_id"])] = data
        except Exception:
            logger.debug("messenger leads context load failed", exc_info=True)

        items: List[Dict[str, Any]] = []
        seen = set()
        for st in chat_states:
            chat_key = str(st.get("chat_key") or "")
            seen.add(chat_key)
            chat_name = str(st.get("chat_name") or chat_key)
            ctx = contexts.get(chat_key) or {}
            lead = ctx.get("lead_qualification") if isinstance(ctx, dict) else {}
            if not isinstance(lead, dict):
                lead = {}
            credit = {}
            try:
                credit = store.get_credit(chat_key)
            except Exception:
                credit = {}
            items.append({
                "chat_key": chat_key,
                "chat_name": chat_name,
                "updated_at": st.get("updated_at") or ctx.get("_ctx_updated_at") or 0,
                "last_sent_at": st.get("last_sent_at") or 0,
                "last_peer_text": st.get("last_peer_text") or ctx.get("last_message") or "",
                "last_reply": st.get("last_reply") or ctx.get("last_reply") or "",
                "credit": credit.get("credit", 100),
                "credit_reason": credit.get("last_reason", ""),
                "reply_lang": ctx.get("reply_lang", ""),
                "persona_id": _profile_id_for_chat(reply_profiles, chat_key, chat_name),
                "lead": lead,
                "score": int(lead.get("icp_score") or 0),
                "stage": str(lead.get("stage") or "unknown"),
                "missing_fields": lead.get("missing_fields") or [],
                "evidence": lead.get("evidence") or [],
            })
        for chat_key, ctx in contexts.items():
            if chat_key in seen:
                continue
            chat_name = str(ctx.get("chat_title") or chat_key.split(":", 1)[-1])
            lead = ctx.get("lead_qualification") if isinstance(ctx, dict) else {}
            if not isinstance(lead, dict):
                lead = {}
            items.append({
                "chat_key": chat_key,
                "chat_name": chat_name,
                "updated_at": ctx.get("_ctx_updated_at") or 0,
                "last_sent_at": 0,
                "last_peer_text": ctx.get("last_message") or "",
                "last_reply": ctx.get("last_reply") or "",
                "credit": 100,
                "credit_reason": "",
                "reply_lang": ctx.get("reply_lang", ""),
                "persona_id": _profile_id_for_chat(reply_profiles, chat_key, chat_name),
                "lead": lead,
                "score": int(lead.get("icp_score") or 0),
                "stage": str(lead.get("stage") or "unknown"),
                "missing_fields": lead.get("missing_fields") or [],
                "evidence": lead.get("evidence") or [],
            })
        items.sort(key=lambda x: (int(x.get("score") or 0), float(x.get("updated_at") or 0)), reverse=True)
        total = len(items)
        high = sum(1 for x in items if int(x.get("score") or 0) >= 80)
        mid = sum(1 for x in items if 40 <= int(x.get("score") or 0) < 80)
        low = sum(1 for x in items if int(x.get("score") or 0) < 40)
        return {
            "items": items[:max(1, min(int(limit or 100), 1000))],
            "summary": {"total": total, "high": high, "mid": mid, "low": low},
            "ts": time.time(),
        }

    @app.get("/api/messenger-rpa/recent")
    async def api_msgr_recent(request: Request, limit: int = 50):
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        return {"runs": store.recent_runs(limit=int(limit or 50))}

    # ── REST: 审批 ─────────────────────────────────
    @app.get("/api/messenger-rpa/approvals")
    async def api_msgr_approvals(
        request: Request,
        status: Optional[str] = "pending",
        limit: int = 50,
        chat_key: Optional[str] = None,
        reply_text_empty: Optional[bool] = None,
    ):
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        norm_status: Optional[str] = (
            None if status in ("", "all", "any") else status
        )
        return {
            "approvals": store.list_approvals(
                status=norm_status,
                chat_key=chat_key,
                reply_text_empty=reply_text_empty,
                limit=int(limit or 50),
            ),
        }

    @app.get("/api/messenger-rpa/approvals/{approval_id}")
    async def api_msgr_approval_detail(
        request: Request, approval_id: int
    ):
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        item = store.get_approval(int(approval_id))
        if not item:
            raise HTTPException(404, f"approval #{approval_id} not found")
        return item

    @app.post("/api/messenger-rpa/approvals/{approval_id}/approve")
    async def api_msgr_approval_approve(
        request: Request, approval_id: int
    ):
        api_auth(request)
        store = _get_store(request)
        svc = _get_service(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        decided_by = str(body.get("decided_by") or "web") or "web"
        note = str(body.get("note") or "")
        # ★ P1-2：允许人工在批准时修改 reply_text（商业化场景常见）
        reply_override_raw = body.get("reply_text")
        reply_override: Optional[str] = None
        if isinstance(reply_override_raw, str):
            candidate = reply_override_raw.strip()
            if candidate:
                reply_override = candidate

        ok = store.decide_approval(
            int(approval_id),
            approve=True,
            decided_by=decided_by,
            decision_note=note,
            reply_text_override=reply_override,
        )
        if not ok:
            raise HTTPException(
                409, f"approval #{approval_id} 状态非 pending，无法批准"
            )

        # 立即触发 service 走一次 send（后台 task，不阻塞响应）
        send_result: Dict[str, Any] = {"requested": False}
        if svc is not None and hasattr(svc, "send_approved_now"):
            try:
                send_result = await svc.send_approved_now(int(approval_id))
            except Exception as ex:
                logger.exception("send_approved_now 异常")
                send_result = {
                    "requested": True,
                    "ok": False,
                    "error": f"{type(ex).__name__}:{ex}",
                }
        return {"ok": True, "approval_id": approval_id, "send": send_result}

    @app.post("/api/messenger-rpa/approvals/{approval_id}/update")
    async def api_msgr_approval_update(
        request: Request, approval_id: int
    ):
        """仅修改 pending 审批的 reply_text，不改变状态。用于"先改文案再决定"。"""
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        new_text = str(body.get("reply_text") or "").strip()
        if not new_text:
            raise HTTPException(400, "reply_text 不能为空")
        ok = store.update_approval_reply(int(approval_id), reply_text=new_text)
        if not ok:
            raise HTTPException(
                409, f"approval #{approval_id} 状态非 pending，无法修改"
            )
        return {"ok": True, "approval_id": approval_id, "reply_text": new_text}

    @app.post("/api/messenger-rpa/approvals/{approval_id}/suggest")
    async def api_msgr_approval_suggest(
        request: Request, approval_id: int
    ):
        """让 SkillManager 基于相同 peer_text 再生成一条候选。

        不覆盖现有 reply_text，返回 {suggestions:[new_text]}；前端可让
        人工对比后决定是否 /update 覆盖。
        """
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        item = store.get_approval(int(approval_id))
        if not item:
            raise HTTPException(404, f"approval #{approval_id} not found")
        if item.get("status") != "pending":
            raise HTTPException(409, "仅 pending 审批支持 Suggest More")

        # 反向调用 SkillManager：不污染实际 conversation_history
        sm = getattr(request.app.state, "skill_manager", None)
        if sm is None:
            # 有些装配路径放在 telegram_client 下
            tg = getattr(request.app.state, "telegram_client", None)
            sm = getattr(tg, "skill_manager", None) if tg else None
        if sm is None:
            raise HTTPException(503, "SkillManager 未注入")

        import asyncio
        import uuid as _uuid

        peer_text = str(item.get("peer_text") or "").strip() or "[空]"
        chat_key = str(item.get("chat_key") or "")
        chat_title = str(item.get("chat_name") or "Messenger Friend")
        peer_kind = str(item.get("peer_kind") or "text")
        cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
        style_hint = str(cfg.get("style_hint") or "").strip()

        ctx = {
            "chat_id": int(_uuid.uuid4().int % (10**9)),  # 临时 id，避免污染真实 chat
            "request_id": f"suggest-{_uuid.uuid4().hex[:10]}",
            "channel": "messenger_rpa",
            "reply_lang": str(cfg.get("default_reply_lang", "zh")),
            "chat_title": chat_title,
            "messenger_rpa_chat_key": f"suggest:{chat_key}",
            "messenger_rpa_peer_kind": peer_kind,
        }
        if style_hint:
            ctx["messenger_rpa_style_hint"] = style_hint

        try:
            payload = await asyncio.wait_for(
                sm.process_message(
                    peer_text,
                    f"suggest:{chat_key}",  # 临时 user_id，独立上下文
                    context=ctx,
                ),
                timeout=45.0,
            )
        except asyncio.TimeoutError:
            raise HTTPException(504, "Suggest More 超时 (>45s)")
        except Exception as ex:
            logger.exception("Suggest More 异常")
            raise HTTPException(
                500, f"suggest failed: {type(ex).__name__}:{ex}"
            )

        if isinstance(payload, dict):
            suggestion = str(payload.get("reply") or payload.get("text") or "")
        else:
            suggestion = str(payload or "")
        suggestion = suggestion.strip()
        return {
            "ok": True,
            "approval_id": approval_id,
            "suggestion": suggestion,
            "original_reply_text": item.get("reply_text") or "",
        }

    # ── P2-6 / P6-3：批量审批（增强）─────────────────
    @app.post("/api/messenger-rpa/approvals/batch")
    async def api_msgr_approval_batch(request: Request):
        """批量批准 / 驳回。P6-3 增强：

        body 字段：
          - ids: int[] 必填（或传 filter 让后端查询 pending）
          - filter: {chat_key?: str, tier?: str, max: int}
            若同时给 ids 与 filter → 取两者交集
          - action: "approve" | "reject"
          - decided_by: str
          - note: str
          - reason: "spam"|"irrelevant"|"low_quality"|"manual" （仅 reject 扣分用，
                    不同 reason 映射到 credit_policy.reject_delta_map；缺省走
                    credit_policy.reject_delta）
          - dry_run: bool  仅返回预览，不真改 DB / 不发送
          - pacing_sec: float  approve 后每条发送间停顿（防 adb 撞车），默认 3.0

        返回：{ok, dry_run, processed, succeeded_ids, failed, send_results}
        """
        import asyncio as _asyncio
        api_auth(request)
        store = _get_store(request)
        svc = _get_service(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        try:
            body = await request.json()
        except Exception:
            body = {}
        action = str(body.get("action") or "").strip().lower()
        if action not in ("approve", "reject"):
            raise HTTPException(400, "action 必须是 approve 或 reject")
        decided_by = str(body.get("decided_by") or "web") or "web"
        note = str(body.get("note") or "")
        reason = str(body.get("reason") or "manual").strip().lower()
        dry_run = bool(body.get("dry_run", False))
        pacing_sec = max(0.0, float(body.get("pacing_sec", 3.0) or 3.0))
        raw_ids = body.get("ids") or []
        filt = body.get("filter") or {}

        # 解析 ids
        ids_set: set = set()
        for raw_id in raw_ids[:500]:
            try:
                ids_set.add(int(raw_id))
            except (TypeError, ValueError):
                pass

        # 解析 filter
        if isinstance(filt, dict) and filt:
            try:
                pending = store.list_approvals(
                    status="pending", limit=int(filt.get("max", 500) or 500),
                )
            except Exception:
                pending = []
            want_ck = str(filt.get("chat_key") or "").strip()
            want_tier = str(filt.get("tier") or "").strip().lower()
            matched: set = set()
            for it in pending:
                if want_ck and str(it.get("chat_key") or "") != want_ck:
                    continue
                if want_tier and (
                    str(it.get("ai_tier") or "").lower() != want_tier
                ):
                    continue
                try:
                    matched.add(int(it["id"]))
                except Exception:
                    continue
            if ids_set:
                ids_set &= matched  # 交集
            else:
                ids_set = matched

        ids = sorted(ids_set)[:100]  # 单次最多 100 条
        if not ids:
            raise HTTPException(400, "ids 解析结果为空（或过滤后无匹配）")

        # ★ dry_run：仅返回预览
        if dry_run:
            preview = []
            for aid in ids:
                it = store.get_approval(aid) or {}
                preview.append({
                    "id": aid,
                    "status": it.get("status"),
                    "chat_key": it.get("chat_key"),
                    "chat_name": it.get("chat_name"),
                    "reply_preview": (it.get("reply_text") or "")[:80],
                    "will_act": it.get("status") == "pending",
                })
            return {
                "ok": True, "dry_run": True, "action": action,
                "processed": len(ids), "preview": preview,
            }

        # ★ 真正执行
        cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
        cred_cfg = cfg.get("credit_policy") or {}
        reject_delta_map: Dict[str, int] = {
            "spam": -30, "irrelevant": -10, "low_quality": -15, "manual": -15,
        }
        for k, v in (cred_cfg.get("reject_delta_map") or {}).items():
            try:
                reject_delta_map[str(k).lower()] = int(v)
            except (TypeError, ValueError):
                pass
        default_delta = int(cred_cfg.get("reject_delta", -15) or -15)

        succeeded: list = []
        failed: list = []
        send_results: list = []
        for idx, aid in enumerate(ids):
            try:
                ok = store.decide_approval(
                    aid, approve=(action == "approve"),
                    decided_by=decided_by, decision_note=note,
                )
            except Exception as ex:
                failed.append({"id": aid, "reason": f"exception:{type(ex).__name__}"})
                continue
            if not ok:
                failed.append({"id": aid, "reason": "not_pending"})
                continue
            succeeded.append(aid)

            # ── reject 扣信用（带 reason 分类）──
            if action == "reject" and cred_cfg.get("enabled", True):
                try:
                    item = store.get_approval(aid) or {}
                    ck = str(item.get("chat_key") or "")
                    if ck:
                        delta = reject_delta_map.get(reason, default_delta)
                        store.adjust_credit(
                            ck, delta,
                            reason=f"batch_reject:{reason}:{note or decided_by}"[:200],
                        )
                except Exception:
                    logger.debug("P6-3 batch reject 扣分失败", exc_info=True)

            # ── approve 后真发（顺序串行 + pacing 防 adb 撞车）──
            if action == "approve" and svc is not None:
                try:
                    sr = await svc.send_approved_now(aid)
                    send_results.append({"id": aid, **(sr or {})})
                except Exception as ex:
                    send_results.append(
                        {"id": aid, "requested": True,
                         "error": f"{type(ex).__name__}: {ex}"}
                    )
                # 不是最后一条 → 停一下
                if idx < len(ids) - 1 and pacing_sec > 0:
                    await _asyncio.sleep(pacing_sec)

        return {
            "ok": True, "dry_run": False, "action": action,
            "processed": len(ids),
            "succeeded_ids": succeeded, "failed": failed,
            "send_results": send_results,
        }

    # ── P2-8：Prometheus 指标暴露（无新增依赖，自写文本格式）────
    @app.get("/api/messenger-rpa/metrics")
    async def api_msgr_metrics(request: Request):
        """Prometheus exposition format (text/plain; version=0.0.4)。

        抓取建议：
          scrape_configs:
            - job_name: messenger_rpa
              metrics_path: /api/messenger-rpa/metrics
              static_configs: [{targets: [host:18787]}]
              authorization: {type: Bearer, credentials: <AUTH_TOKEN>}
        """
        api_auth(request)
        from fastapi.responses import PlainTextResponse
        svc = _get_service(request)
        store = _get_store(request)
        lines: list = []

        def _emit(name: str, help_text: str, typ: str, value, labels: Dict[str, str] = None):
            if value is None:
                return
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {typ}")
            label_str = ""
            if labels:
                parts = [
                    f'{k}="{str(v).replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}"'
                    for k, v in labels.items()
                ]
                label_str = "{" + ",".join(parts) + "}"
            try:
                vnum = float(value)
            except (TypeError, ValueError):
                return
            lines.append(f"{name}{label_str} {vnum}")

        # 1) 服务状态
        if svc is not None:
            try:
                st = svc.status()
            except Exception:
                st = {}
            _emit(
                "messenger_rpa_service_running",
                "1 if RPA loop task is alive", "gauge",
                1 if st.get("running") else 0,
            )
            _emit(
                "messenger_rpa_notif_running",
                "1 if notification watcher is alive", "gauge",
                1 if st.get("notif_running") else 0,
            )
            _emit(
                "messenger_rpa_sla_running",
                "1 if approval SLA loop is alive", "gauge",
                1 if st.get("sla_running") else 0,
            )
            _emit(
                "messenger_rpa_consecutive_empty",
                "Consecutive empty inbox polls", "gauge",
                st.get("consecutive_empty", 0),
            )
            _emit(
                "messenger_rpa_consecutive_unhealthy",
                "Consecutive device-unhealthy ticks", "gauge",
                st.get("consecutive_unhealthy", 0),
            )
            _emit(
                "messenger_rpa_sla_alerts_sent_total",
                "Total SLA overdue alerts pushed since start", "counter",
                st.get("sla_alert_sent_total", 0),
            )
            _emit(
                "messenger_rpa_notif_events_total",
                "Total incoming notification events since start", "counter",
                st.get("notif_event_count", 0),
            )
            # send counters
            sc = st.get("send_counters") or {}
            _emit(
                "messenger_rpa_sends_today",
                "Messenger messages sent today", "gauge",
                sc.get("today", 0),
            )
            # SLA stats
            sla = st.get("approval_sla") or {}
            _emit(
                "messenger_rpa_approvals_pending",
                "Approvals in pending state", "gauge",
                sla.get("pending_count", 0),
            )
            _emit(
                "messenger_rpa_approvals_overdue",
                "Pending approvals older than SLA threshold", "gauge",
                sla.get("overdue_count", 0),
            )
            _emit(
                "messenger_rpa_approvals_oldest_age_seconds",
                "Age of the oldest pending approval in seconds", "gauge",
                sla.get("oldest_age_sec", 0),
            )
            _emit(
                "messenger_rpa_approvals_sla_threshold_seconds",
                "Configured SLA threshold in seconds", "gauge",
                sla.get("threshold_sec", 0),
            )
            # ★ P3-1：风控指标
            risk = st.get("risk") or {}
            status_map = {"normal": 0, "warning_once": 1, "blocked": 2}
            _emit(
                "messenger_rpa_risk_status",
                "Account risk status (0=normal,1=warn,2=blocked)",
                "gauge",
                status_map.get(str(risk.get("status") or "normal"), 0),
            )
            _emit(
                "messenger_rpa_risk_hit_count",
                "Consecutive vision risk hits not yet cleared", "gauge",
                risk.get("hit_count", 0),
            )
            _emit(
                "messenger_rpa_risk_blocked_until_ts",
                "Risk-blocked pause expiration unix ts (0 if not blocked)",
                "gauge",
                risk.get("blocked_until_ts", 0),
            )
            # ★ P4-3：节奏学习指标
            pace = st.get("pace") or {}
            if pace:
                _emit(
                    "messenger_rpa_pace_ratio",
                    "Current-hour send count / historical median (0=no data)",
                    "gauge", pace.get("ratio", 0),
                )
                _emit(
                    "messenger_rpa_pace_current_hour_count",
                    "Sends in the current local hour",
                    "gauge", pace.get("current_hour_count", 0),
                )
                _emit(
                    "messenger_rpa_pace_hist_median",
                    "Historical median of sends at this hour",
                    "gauge", pace.get("hist_median", 0),
                )
                decision_map = {"allow": 0, "throttle": 1, "deny": 2,
                                "allow_on_error": -1}
                _emit(
                    "messenger_rpa_pace_decision",
                    "Pace decision (0=allow 1=throttle 2=deny -1=err)",
                    "gauge",
                    decision_map.get(str(pace.get("decision")), 0),
                )
            # ★ P4-7：信用分分布
            credit = st.get("credit") or {}
            dist = credit.get("distribution") or {}
            if dist:
                lines.append(
                    "# HELP messenger_rpa_chat_credit_distribution"
                    " Chats grouped by credit bucket"
                )
                lines.append(
                    "# TYPE messenger_rpa_chat_credit_distribution gauge"
                )
                for bucket, cnt in dist.items():
                    lines.append(
                        f'messenger_rpa_chat_credit_distribution'
                        f'{{bucket="{bucket}"}} {cnt}'
                    )
                _emit(
                    "messenger_rpa_chat_credit_tracked_total",
                    "Chats with non-default credit", "gauge",
                    credit.get("total_tracked", 0),
                )
                _emit(
                    "messenger_rpa_chat_credit_low_total",
                    "Chats with credit < 40 (force approve or worse)",
                    "gauge",
                    len(credit.get("low_credit_chats") or []),
                )
        # ★ P3-4：进程级 histogram
        try:
            from src.integrations.messenger_rpa.metrics import get_metrics
            md = get_metrics().dump()
        except Exception:
            md = {}
        if md:
            rh = md.get("run_duration") or {}
            if rh.get("count"):
                lines.append("# HELP messenger_rpa_run_duration_seconds End-to-end run_once duration")
                lines.append("# TYPE messenger_rpa_run_duration_seconds histogram")
                cum = rh.get("cum_counts") or []
                for i, b in enumerate(rh.get("buckets") or []):
                    if i < len(cum):
                        lines.append(
                            f'messenger_rpa_run_duration_seconds_bucket{{le="{b}"}} {cum[i]}'
                        )
                if cum:
                    lines.append(
                        f'messenger_rpa_run_duration_seconds_bucket{{le="+Inf"}} {cum[-1]}'
                    )
                lines.append(f"messenger_rpa_run_duration_seconds_sum {rh['sum']}")
                lines.append(f"messenger_rpa_run_duration_seconds_count {rh['count']}")
            # phase histograms（按 phase label 维度输出）
            ph = md.get("phase_duration") or {}
            if any(h.get("count") for h in ph.values()):
                lines.append(
                    "# HELP messenger_rpa_phase_duration_seconds Run phase latency"
                )
                lines.append(
                    "# TYPE messenger_rpa_phase_duration_seconds histogram"
                )
                for phase_name, h in ph.items():
                    if not h.get("count"):
                        continue
                    cum = h.get("cum_counts") or []
                    for i, b in enumerate(h.get("buckets") or []):
                        if i < len(cum):
                            lines.append(
                                f'messenger_rpa_phase_duration_seconds_bucket'
                                f'{{phase="{phase_name}",le="{b}"}} {cum[i]}'
                            )
                    if cum:
                        lines.append(
                            f'messenger_rpa_phase_duration_seconds_bucket'
                            f'{{phase="{phase_name}",le="+Inf"}} {cum[-1]}'
                        )
                    lines.append(
                        f'messenger_rpa_phase_duration_seconds_sum'
                        f'{{phase="{phase_name}"}} {h["sum"]}'
                    )
                    lines.append(
                        f'messenger_rpa_phase_duration_seconds_count'
                        f'{{phase="{phase_name}"}} {h["count"]}'
                    )
            # outcome counters
            outc = md.get("run_outcomes") or {}
            if outc:
                lines.append("# HELP messenger_rpa_runs_total Run outcomes since process start")
                lines.append("# TYPE messenger_rpa_runs_total counter")
                for k, v in outc.items():
                    lines.append(
                        f'messenger_rpa_runs_total{{outcome="{k}"}} {v}'
                    )
            caps = md.get("caption_sources") or {}
            if caps:
                lines.append("# HELP messenger_rpa_caption_source_total Caption resolution source")
                lines.append("# TYPE messenger_rpa_caption_source_total counter")
                for k, v in caps.items():
                    lines.append(
                        f'messenger_rpa_caption_source_total{{source="{k}"}} {v}'
                    )

        # 2) 按 variant 指标
        if store is not None:
            try:
                vs = store.variant_stats()
            except Exception:
                vs = {"variants": {}}
            for vname, d in (vs.get("variants") or {}).items():
                labels = {"variant": vname}
                _emit(
                    "messenger_rpa_variant_chats",
                    "Chats assigned to this variant", "gauge",
                    d.get("chats", 0), labels=labels,
                )
                _emit(
                    "messenger_rpa_variant_escalations_active",
                    "Chats currently in escalation cooldown", "gauge",
                    d.get("escalations_active", 0), labels=labels,
                )
                for st_name in ("pending", "approved", "sent", "rejected"):
                    _emit(
                        f"messenger_rpa_variant_approvals_{st_name}",
                        f"Approvals with status={st_name} by variant",
                        "gauge",
                        d.get(f"apr_{st_name}", 0), labels=labels,
                    )
                _emit(
                    "messenger_rpa_variant_approve_ratio",
                    "sent / (sent + rejected) per variant", "gauge",
                    d.get("approve_ratio"), labels=labels,
                )

        body = "\n".join(lines) + "\n"
        # ★ P6-4：附上 LLM cost/tokens per (model, tier, account)
        try:
            from src.ai.llm_cost import get_llm_cost
            body += get_llm_cost().dump_prom()
        except Exception:
            pass
        return PlainTextResponse(
            content=body,
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # ── P6-4：LLM 成本 / token JSON API ─────────────
    @app.get("/api/messenger-rpa/llm-cost")
    async def api_msgr_llm_cost(request: Request):
        """返回 LLM 成本 & tokens 的分桶聚合（JSON，供运营看板）。"""
        api_auth(request)
        try:
            from src.ai.llm_cost import get_llm_cost
            return get_llm_cost().dump()
        except Exception as ex:
            raise HTTPException(500, f"llm_cost.dump 失败: {ex}")

    # ── P2-3：A/B persona 指标 ──────────────────────
    @app.get("/api/messenger-rpa/variants/stats")
    async def api_msgr_variants_stats(request: Request):
        """按 variant 聚合 Messenger 指标。"""
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
        exp = cfg.get("persona_experiment") or {}
        out = store.variant_stats()
        out["experiment_enabled"] = bool(exp.get("enabled", False))
        out["variants_config"] = [
            {"name": v.get("name"), "weight": v.get("weight")}
            for v in (exp.get("variants") or [])
            if isinstance(v, dict)
        ]
        return out

    # ── P5-1：账号注册表 ───────────────────────────
    @app.get("/api/messenger-rpa/accounts")
    async def api_msgr_accounts(request: Request):
        """列出所有已注册 account（含状态 db 路径、serial、pool 锁状态）。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未注入")
        reg = getattr(svc, "_account_registry", None)
        if reg is None:
            raise HTTPException(503, "account_registry 未初始化")
        return reg.stats()

    # ── P6-1：按账号精确触发 ─────────────────────────
    @app.post("/api/messenger-rpa/accounts/{account_id}/trigger")
    async def api_msgr_account_trigger(request: Request, account_id: str):
        """立即触发指定账号跑一次 run_once（跳过轮询节奏）。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未注入")
        reg = getattr(svc, "_account_registry", None)
        if reg is None or reg.get(account_id) is None:
            raise HTTPException(404, f"未知 account: {account_id}")
        try:
            r = await svc.trigger_once(account_id=account_id)
            return {"ok": True, "account_id": account_id, "result": r}
        except Exception as ex:
            raise HTTPException(500, f"trigger 失败: {ex}")

    @app.post("/api/messenger-rpa/accounts/{account_id}/send-to")
    async def api_msgr_account_send_to(request: Request, account_id: str):
        """指定账号设备：打开 Messenger → 匹配 chat_name 会话 → 发送 text（不经 LLM）。

        Body JSON: ``{"chat_name": "...", "text": "..."}``（兼容 ``message`` / ``reply_text``）
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未注入")
        reg = getattr(svc, "_account_registry", None)
        if reg is None or reg.get(account_id) is None:
            raise HTTPException(404, f"未知 account: {account_id}")
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        chat_name = str(
            body.get("chat_name") or body.get("chat") or "",
        ).strip()
        text = str(
            body.get("text")
            or body.get("message")
            or body.get("reply_text")
            or "",
        ).strip()
        if not chat_name or not text:
            raise HTTPException(
                400,
                "需要 JSON: {\"chat_name\":\"...\",\"text\":\"...\"}",
            )
        try:
            r = await svc.send_to_chat_name_for_account(
                account_id,
                chat_name=chat_name,
                reply_text=text,
            )
            return {"ok": bool(r.get("ok")), "account_id": account_id, "result": r}
        except Exception as ex:
            raise HTTPException(500, f"send-to 失败: {ex}")

    # ── P4-7：信用分 ────────────────────────────────
    @app.get("/api/messenger-rpa/credits")
    async def api_msgr_credits(request: Request):
        """返回所有 tracked chat 的信用分分布 + 低信用名单。"""
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        return store.credit_stats()

    @app.post("/api/messenger-rpa/credits/{chat_key}/reset")
    async def api_msgr_credit_reset(request: Request, chat_key: str):
        """把某 chat 的信用分重置到 100（运营手工介入）。"""
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        cur = store.get_credit(chat_key)
        delta = 100 - int(cur.get("credit", 100))
        r = store.adjust_credit(chat_key, delta, reason="manual_reset")
        return {"ok": True, "chat_key": chat_key, "new_credit": r.get("credit")}

    # ── P3-7：回放包列表 ────────────────────────────
    @app.get("/api/messenger-rpa/replays")
    async def api_msgr_replays(request: Request, limit: int = 50):
        """列出失败 run 的回放 zip 包。"""
        api_auth(request)
        cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
        try:
            from src.integrations.messenger_rpa.replay import list_replays
            items, base = list_replays(cfg, limit=max(1, min(int(limit), 500)))
        except Exception as ex:
            raise HTTPException(500, f"list_replays 失败: {ex}")
        return {"base_dir": str(base), "total": len(items), "items": items}

    # ── P4-6：Replay Rerun (脱机重跑 LLM) ───────────
    @app.post("/api/messenger-rpa/replays/rerun")
    async def api_msgr_replay_rerun(request: Request):
        """脱机重跑某个 zip 里的 LLM 调用，不碰设备。

        body: {zip: "<basename>" | "<abs-path>", override_chat_key?: str}
        return: {old_reply, new_reply, text_for_ai, diff_hint}
        """
        api_auth(request)
        cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        zip_arg = str(body.get("zip") or "").strip()
        if not zip_arg:
            raise HTTPException(400, "zip 参数必填")
        try:
            from src.integrations.messenger_rpa.replay import rerun_from_zip
            result = await rerun_from_zip(
                zip_arg,
                cfg,
                request.app,
                override_chat_key=str(body.get("override_chat_key") or "").strip() or None,
            )
        except FileNotFoundError as ex:
            raise HTTPException(404, str(ex))
        except Exception as ex:
            logger.exception("replay rerun 失败")
            raise HTTPException(500, f"{type(ex).__name__}: {ex}")
        return result

    # ── P2-6：快捷模板 ──────────────────────────────
    @app.get("/api/messenger-rpa/templates")
    async def api_msgr_templates(request: Request):
        """返回配置的快捷回复模板（每次请求都重读 config，支持热加载）。"""
        api_auth(request)
        cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
        tpls = cfg.get("approval_templates") or []
        # 规范化 + 过滤非法项
        out = []
        for t in tpls:
            if not isinstance(t, dict):
                continue
            label = str(t.get("label") or "").strip()
            text = str(t.get("text") or "").strip()
            if label and text:
                out.append({"label": label, "text": text})
        return {"templates": out}

    @app.post("/api/messenger-rpa/approvals/{approval_id}/reject")
    async def api_msgr_approval_reject(
        request: Request, approval_id: int
    ):
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        decided_by = str(body.get("decided_by") or "web") or "web"
        note = str(body.get("note") or "")
        ok = store.decide_approval(
            int(approval_id),
            approve=False,
            decided_by=decided_by,
            decision_note=note,
        )
        if not ok:
            raise HTTPException(
                409, f"approval #{approval_id} 状态非 pending，无法驳回"
            )
        # ★ P4-7：reject → 扣信用
        try:
            cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
            cred_cfg = cfg.get("credit_policy") or {}
            if cred_cfg.get("enabled", True):
                item = store.get_approval(int(approval_id))
                ck = str(item.get("chat_key") or "") if item else ""
                if ck:
                    delta = int(cred_cfg.get("reject_delta", -15) or -15)
                    r = store.adjust_credit(
                        ck, delta, reason=f"reject: {note or decided_by}"[:200],
                    )
                    logger.info(
                        "[messenger_rpa] P4-7 reject credit chat=%s delta=%d → %d",
                        ck, delta, r.get("credit", -1),
                    )
        except Exception:
            logger.debug("P4-7 reject credit 扣分失败", exc_info=True)
        return {"ok": True, "approval_id": approval_id}

    # ── REST: 控制 ─────────────────────────────────
    @app.post("/api/messenger-rpa/trigger")
    async def api_msgr_trigger(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未构建")
        return await svc.trigger_once()

    @app.post("/api/messenger-rpa/pause")
    async def api_msgr_pause(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未构建")
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            seconds = float(body.get("seconds", 300))
        except (TypeError, ValueError):
            seconds = 300.0
        svc.pause_for(max(seconds, 0))
        return {"ok": True, "paused_for": seconds}

    @app.post("/api/messenger-rpa/resume")
    async def api_msgr_resume(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未构建")
        svc.resume()
        return {"ok": True}

    # ── REST: 设备状态面板 ────────────────────────────
    @app.get("/api/messenger-rpa/devices")
    async def api_msgr_devices(request: Request):
        """返回配置设备的在线/屏幕/锁屏状态（不触发 wake，快速只读）。"""
        api_auth(request)
        from src.integrations.messenger_rpa.device_health import probe_devices
        serials: list = []
        svc = _get_service(request)
        if svc is not None and hasattr(svc, "configured_adb_serials"):
            serials = list(svc.configured_adb_serials())
        if not serials:
            cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
            primary = (cfg.get("adb_serial") or "").strip()
            extras = cfg.get("extra_serials") or []
            if primary:
                serials.append(primary)
            for s in extras:
                s = (s or "").strip()
                if s and s not in serials:
                    serials.append(s)
        if not serials:
            return {
                "devices": [],
                "hint": (
                    "messenger_rpa 未配置任何 adb_serial"
                    "（accounts[].adb_serial 或顶层 adb_serial）"
                ),
            }
        results = probe_devices(serials)
        return {"devices": [results[s] for s in serials]}

    # ── REST: 一键校准 ────────────────────────────────
    @app.post("/api/messenger-rpa/calibrate")
    async def api_msgr_calibrate(request: Request):
        """手动触发一次 Inbox 坐标校准。成功会把 calibration 写入
        tmp_messenger_rpa/calibrations/<serial>.json。
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未构建")
        if not hasattr(svc, "calibrate_now"):
            raise HTTPException(501, "service.calibrate_now 不可用")
        try:
            r = await svc.calibrate_now()
            return {"ok": bool(r.get("ok")), "result": r}
        except Exception as ex:
            logger.exception("calibrate_now 异常")
            raise HTTPException(
                500, f"calibrate failed: {type(ex).__name__}:{ex}"
            )

    # ── REST: 对话历史查看（诊断 AI 记忆） ──────────────
    @app.get("/api/messenger-rpa/chat/history")
    async def api_msgr_chat_history(
        request: Request,
        chat_key: str,
        limit: int = 20,
    ):
        """读 bot.db 的 user_context 里当前 chat_key 持久化的 _conversation_history。

        用于运营确认「AI 到底记住了什么」，比光看 runner 单次回复更直观。
        """
        api_auth(request)
        if not chat_key:
            raise HTTPException(400, "chat_key 为空")
        # bot.db 位置随 skill_manager
        try:
            import json
            import sqlite3
            from pathlib import Path

            cfg_dir = Path(config_manager.config_path).parent
            db = cfg_dir / "bot.db"
            if not db.exists():
                raise HTTPException(404, f"bot.db 不存在: {db}")
            c = sqlite3.connect(str(db))
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT data, updated_at FROM user_context WHERE user_id = ?",
                (chat_key,),
            ).fetchone()
            c.close()
            if not row:
                return {
                    "chat_key": chat_key,
                    "exists": False,
                    "turns": [],
                    "summary": "",
                    "last_message": "",
                    "last_reply": "",
                }
            d: Dict[str, Any] = {}
            try:
                d = json.loads(row["data"]) or {}
            except Exception:
                pass
            hist = d.get("_conversation_history") or []
            if limit and len(hist) > int(limit) * 2:
                hist = hist[-int(limit) * 2:]
            return {
                "chat_key": chat_key,
                "exists": True,
                "updated_at": row["updated_at"],
                "turns": hist,
                "turn_count": len(hist) // 2,
                "summary": d.get("_conversation_summary") or "",
                "last_message": d.get("last_message") or "",
                "last_reply": d.get("last_reply") or "",
                "reply_count": d.get("reply_count", 0),
                "current_intent": d.get("current_intent", ""),
                "intent_chain": d.get("_intent_chain") or [],
            }
        except HTTPException:
            raise
        except Exception as ex:
            logger.exception("chat history 读取异常")
            raise HTTPException(
                500, f"history read failed: {type(ex).__name__}:{ex}"
            )

    # ── REST: AdbKeyboard 自动安装 ────────────────────
    @app.post("/api/messenger-rpa/install-adbkeyboard")
    async def api_msgr_install_adbkeyboard(request: Request):
        """对 adb_serial 指定设备跑 ensure_adbkeyboard_installed。
        APK 从 tools/ADBKeyboard.apk 读取。
        """
        api_auth(request)
        cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
        serial = (cfg.get("adb_serial") or "").strip()
        if not serial:
            raise HTTPException(400, "messenger_rpa.adb_serial 未配置")
        ime = (
            cfg.get("adb_keyboard_ime")
            or "com.android.adbkeyboard/.AdbIME"
        )
        pkg = (
            cfg.get("adb_keyboard_package") or "com.android.adbkeyboard"
        )
        from src.integrations.line_rpa import adb_helpers as adb
        try:
            info = adb.ensure_adbkeyboard_installed(
                serial, package=pkg, ime_component=ime, auto_enable=True,
            )
            return {"ok": bool(info.get("installed")), "info": info}
        except Exception as ex:
            logger.exception("ensure_adbkeyboard_installed 异常")
            raise HTTPException(
                500, f"install failed: {type(ex).__name__}:{ex}"
            )

    # ── 账号健康看板：所有账号深度状态 ──────────────────
    @app.get("/api/messenger-rpa/accounts/health")
    async def api_msgr_accounts_health(request: Request, deep: bool = False):
        """对所有已注册账号执行健康检查。

        ``?deep=true`` 时额外检查 ADB Keyboard 安装情况（耗时约 3-5s）。
        返回每台手机的 ADB 状态、屏幕、锁屏、暂停、UI unsafe 等字段。
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未注入")
        if not hasattr(svc, "accounts_health"):
            raise HTTPException(501, "accounts_health 不可用")
        try:
            result = await svc.accounts_health(deep=bool(deep))
            return result
        except Exception as ex:
            logger.exception("accounts_health 异常")
            raise HTTPException(500, f"health check 失败: {type(ex).__name__}:{ex}")

    # ── 账号级暂停 ──────────────────────────────────
    @app.post("/api/messenger-rpa/accounts/{account_id}/pause")
    async def api_msgr_account_pause(request: Request, account_id: str):
        """暂停指定账号 N 秒（默认 300s）。不影响其他账号。

        Body JSON（可选）: ``{"seconds": 300}``
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未注入")
        reg = getattr(svc, "_account_registry", None)
        if reg is None or reg.get(account_id) is None:
            raise HTTPException(404, f"未知 account: {account_id}")
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        seconds = float(body.get("seconds", 300) or 300)
        svc.pause_account(account_id, seconds)
        return {"ok": True, "account_id": account_id, "paused_for_sec": seconds}

    # ── 账号级恢复 ──────────────────────────────────
    @app.post("/api/messenger-rpa/accounts/{account_id}/resume")
    async def api_msgr_account_resume(request: Request, account_id: str):
        """恢复指定账号，同时清除 ui_unsafe 标记。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未注入")
        reg = getattr(svc, "_account_registry", None)
        if reg is None or reg.get(account_id) is None:
            raise HTTPException(404, f"未知 account: {account_id}")
        svc.resume_account(account_id)
        return {"ok": True, "account_id": account_id}

    # ── 清除 UI unsafe 标记（不自动恢复，需再调 resume） ──
    @app.post("/api/messenger-rpa/accounts/{account_id}/clear-unsafe")
    async def api_msgr_account_clear_unsafe(request: Request, account_id: str):
        """仅清除 ui_unsafe 标记，暂停计时仍然有效。
        若需立即恢复，请改用 /accounts/{id}/resume。
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未注入")
        reg = getattr(svc, "_account_registry", None)
        if reg is None or reg.get(account_id) is None:
            raise HTTPException(404, f"未知 account: {account_id}")
        svc.clear_account_ui_unsafe(account_id)
        return {"ok": True, "account_id": account_id}

    # ── 转化漏斗 + A/B 指标看板 ──────────────────────────
    @app.get("/api/messenger-rpa/funnel")
    async def api_msgr_funnel(request: Request):
        """转化漏斗 + Persona A/B 实验指标。

        返回字段：
          funnel        - 各 Journey 阶段当前存量
          conversions   - 关键转化率（engaged/handoff/line_add/line_engage/overall）
          variants      - Persona A/B 各变体审批通过率
          handoff       - 引流话术注入/发送/跳过统计（进程级计数器）
          ab_conclusions- 策略 A/B 测试结论（conclusive/inconclusive/insufficient）
        """
        api_auth(request)
        svc = _get_service(request)

        # 1. 转化漏斗（来自 contacts store）
        funnel: Dict[str, int] = {}
        conversions: Dict[str, Any] = {}
        try:
            cs = getattr(request.app.state, "contacts", None)
            if cs is not None:
                funnel = cs.store.count_journeys_by_stage()
        except Exception:
            pass

        if funnel:
            def _pct(num_key: str, den_key: str) -> Optional[float]:
                n = funnel.get(num_key, 0)
                d = funnel.get(den_key, 0)
                return round(n / d * 100, 1) if d else None

            # 关键阶段（缺失补 0）
            _stages = ["INITIAL", "ENGAGED", "HANDOFF_READY", "HANDOFF_SENT",
                       "LINE_ADDED", "LINE_ACCEPTED", "LINE_ENGAGED", "BONDED",
                       "LOST_HANDOFF", "LOST_LINE_SILENT"]
            funnel = {s: funnel.get(s, 0) for s in _stages}

            total = sum(
                funnel.get(s, 0) for s in
                ["INITIAL", "ENGAGED", "HANDOFF_READY", "HANDOFF_SENT",
                 "LINE_ADDED", "LINE_ACCEPTED", "LINE_ENGAGED", "BONDED"]
            )
            line_engaged = funnel.get("LINE_ENGAGED", 0) + funnel.get("BONDED", 0)
            conversions = {
                "engaged_rate":     _pct("ENGAGED", "INITIAL"),
                "handoff_rate":     _pct("HANDOFF_SENT", "ENGAGED"),
                "line_add_rate":    _pct("LINE_ADDED", "HANDOFF_SENT"),
                "line_engage_rate": (
                    round(line_engaged / funnel.get("LINE_ADDED", 0) * 100, 1)
                    if funnel.get("LINE_ADDED") else None
                ),
                "overall_rate": (
                    round(line_engaged / total * 100, 1) if total else None
                ),
                "total_journeys": total,
            }

        # 2. Persona A/B variant stats
        variants: Dict[str, Any] = {}
        if svc is not None:
            try:
                store = getattr(svc, "_state", None)
                if store is not None:
                    vs = store.variant_stats()
                    variants = vs.get("variants", {})
            except Exception:
                pass

        # 3. Handoff 进程级计数器
        handoff: Dict[str, Any] = {}
        try:
            from src.integrations.messenger_rpa.metrics import get_metrics
            m = get_metrics().dump()
            handoff = {
                "injected_total":  m.get("handoff_injected_total", 0),
                "sent_total":      m.get("handoff_sent_total", 0),
                "by_script":       m.get("handoff_by_script", {}),
                "skipped_reasons": m.get("handoff_skipped", {}),
                "sends_total":     m.get("sends_total", 0),
                "inject_rate": (
                    round(m["handoff_injected_total"] / m["sends_total"] * 100, 1)
                    if m.get("sends_total") else None
                ),
            }
        except Exception:
            pass

        # 4. 策略 A/B 测试结论
        ab_conclusions: list = []
        try:
            from src.utils.strategy_advisor import evaluate_ab_tests
            sm = getattr(svc, "_sm", None) if svc else None
            if sm is not None:
                ab_tests = getattr(sm, "_ab_tests", {}) or {}
                strategies = getattr(sm, "_strategies", {}) or {}
                tracker = getattr(sm, "_strategy_tracker", None)
                if ab_tests and tracker is not None:
                    summary = tracker.get_summary() if hasattr(
                        tracker, "get_summary") else []
                    ab_conclusions = evaluate_ab_tests(
                        ab_tests, summary, strategies)
        except Exception:
            pass

        return {
            "funnel": funnel,
            "conversions": conversions,
            "variants": variants,
            "handoff": handoff,
            "ab_conclusions": ab_conclusions,
            "ts": __import__("time").time(),
        }

    # ── 跨账号协调器快照（活跃聊天锁 + 画像缓存） ──
    @app.get("/api/messenger-rpa/coordinator")
    async def api_msgr_coordinator(request: Request):
        """返回 CrossAccountCoordinator 快照：
        - active_chats: 当前各用户由哪个账号处理（聊天锁）
        - portrait_cache: 各用户最新画像缓存元信息（account、时间、年龄）
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未注入")
        return svc.coordinator_snapshot()

    logger.info("Messenger RPA routes registered (status/approvals/trigger/...)")
