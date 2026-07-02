"""人工转接（human escalation）相关路由 — 从 admin.py 抽出（Phase E1）。

端点（与抽出前完全一致）：
  GET  /api/human-escalation/shift              — 读当前手动值班状态
  POST /api/human-escalation/shift              — 设手动值班（需 manage_settings）
  GET  /api/human-escalation/schedule-status    — 排班自检 + 下一开/关窗估算
  GET  /api/human-escalation/mention-round-robin— 轮询计数快照（只读）
  GET  /api/human-escalation/verify             — 进程内配置/存储一致性自检

依赖通过 register 传入（闭包 + 单例），逻辑与原 admin.py 内联实现逐行一致。
schedule-status 缓存助手仍复用 admin.py 模块级函数，保证与 settings 保存后的
invalidate_schedule_status_cache() 共享同一缓存。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import Depends, HTTPException, Request
from src.web.web_i18n import tr


def register_human_escalation_routes(
    app,
    *,
    api_auth,
    api_write,
    config_manager,
    telegram_client=None,
    audit_store=None,
):
    # 复用 admin.py 模块级缓存助手（与 settings 保存的 invalidate 共享同一状态）
    from src.web.admin import (
        _human_escalation_cfg_hash,
        _schedule_status_cache_get,
        _schedule_status_cache_set,
    )

    @app.get("/api/human-escalation/shift")
    async def api_human_escalation_shift_get(request: Request):
        api_auth(request)
        st = getattr(telegram_client, "_human_escalation_store", None) if telegram_client else None
        if not st:
            return {"ok": False, "on_duty": False, "msg": "store_unavailable"}
        return {"ok": True, "on_duty": bool(st.get_shift_on_duty())}

    @app.post("/api/human-escalation/shift")
    async def api_human_escalation_shift_set(
        request: Request, _=Depends(api_write("manage_settings")),
    ):
        body = await request.json()
        on = bool(body.get("on_duty"))
        st = getattr(telegram_client, "_human_escalation_store", None) if telegram_client else None
        if not st:
            raise HTTPException(503, tr(request, "err.svc.handoff_store_not_ready"))
        st.set_shift_on_duty(on)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "human_escalation_shift", f"on_duty={on}", "", "")
        return {"ok": True, "on_duty": on}

    @app.get("/api/human-escalation/schedule-status")
    async def api_human_escalation_schedule_status(request: Request):
        """排班自检：当前是否在周模板/例外窗口内、手动值班、综合 duty 是否放行、粗估下一开/关窗。"""
        api_auth(request)
        cfg = config_manager.config or {}
        he = cfg.get("human_escalation") or {}
        if not isinstance(he, dict):
            he = {}
        tz = (he.get("timezone") or "UTC").strip() or "UTC"
        wh = he.get("work_hours") if isinstance(he.get("work_hours"), dict) else {}
        wex = he.get("work_exceptions") if isinstance(he.get("work_exceptions"), dict) else {}
        now = datetime.now(timezone.utc)
        st = getattr(telegram_client, "_human_escalation_store", None) if telegram_client else None

        from src.utils.work_schedule import (
            estimate_minutes_until_next_close,
            estimate_minutes_until_next_open,
            is_within_work_hours,
        )
        from src.utils.human_escalation import (
            _resolve_duty_mode,
            active_teams_status,
            duty_allows,
        )

        class _DummyShift:
            def get_shift_on_duty(self):
                return False

        try:
            step = int(he.get("schedule_estimate_step_minutes", 15) or 15)
        except (TypeError, ValueError):
            step = 15
        step = max(1, min(step, 60))
        try:
            fh = int(he.get("schedule_estimate_fine_horizon_hours", 24) or 24)
        except (TypeError, ValueError):
            fh = 24
        fh = max(0, min(fh, 168))
        try:
            ttl = float(he.get("schedule_status_cache_ttl_sec", 30) or 0)
        except (TypeError, ValueError):
            ttl = 30.0
        ttl = max(0.0, min(ttl, 300.0))

        minute_bucket = int(now.timestamp() // 60)
        cache_key = (_human_escalation_cfg_hash(he), minute_bucket)
        partial = _schedule_status_cache_get(cache_key, ttl)
        estimates_cached = partial is not None
        if partial is None:
            try:
                in_sched = is_within_work_hours(now, tz, wh, wex)
            except Exception:
                in_sched = False
            try:
                min_open = estimate_minutes_until_next_open(
                    now, tz, wh, wex, step_minutes=step, fine_horizon_hours=fh,
                )
            except Exception:
                min_open = None
            try:
                min_close = estimate_minutes_until_next_close(
                    now, tz, wh, wex, step_minutes=step, fine_horizon_hours=fh,
                )
            except Exception:
                min_close = None
            try:
                teams = active_teams_status(he)
            except Exception:
                teams = []
            partial = {
                "in_schedule": in_sched,
                "minutes_until_next_open": min_open,
                "minutes_until_next_close": min_close,
                "active_teams": teams,
            }
            _schedule_status_cache_set(cache_key, partial, ttl)

        manual = bool(st.get_shift_on_duty()) if st else False
        dm = _resolve_duty_mode(he)
        duty_eff = duty_allows(he, st or _DummyShift())
        try:
            from zoneinfo import ZoneInfo

            local = now.astimezone(ZoneInfo(tz))
            local_iso = local.isoformat()
        except Exception:
            local_iso = ""

        return {
            "ok": True,
            "enabled": bool(he.get("enabled")),
            "timezone": tz,
            "duty_mode": dm,
            "local_time": local_iso,
            "in_schedule": partial["in_schedule"],
            "manual_shift": manual,
            "duty_effective": duty_eff,
            "minutes_until_next_open": partial["minutes_until_next_open"],
            "minutes_until_next_close": partial["minutes_until_next_close"],
            "estimate_step_minutes": step,
            "team_fallback_to_global": bool(he.get("team_fallback_to_global", True)),
            "team_pick_mode": (he.get("team_pick_mode") or "union"),
            "mention_round_robin_scope": (he.get("mention_round_robin_scope") or "global"),
            "schedule_estimate_step_minutes": step,
            "schedule_estimate_fine_horizon_hours": fh,
            "schedule_status_cache_ttl_sec": ttl,
            "schedule_estimates_cached": estimates_cached,
            "active_teams": partial["active_teams"],
        }

    @app.get("/api/human-escalation/mention-round-robin")
    async def api_human_escalation_mention_round_robin(request: Request):
        """运维：全局 / 按群轮询计数快照（只读）。"""
        api_auth(request)
        st = getattr(telegram_client, "_human_escalation_store", None) if telegram_client else None
        if not st:
            return {"ok": False, "msg": "store_unavailable", "global_idx": 0, "per_chat": []}
        gidx, rows = st.get_round_robin_snapshot(50)
        return {
            "ok": True,
            "global_idx": gidx,
            "per_chat": [
                {"chat_id": c, "idx": i, "updated_at": ts}
                for c, i, ts in rows
            ],
        }

    @app.get("/api/human-escalation/verify")
    async def api_human_escalation_verify(request: Request):
        """
        确认当前进程内 `human_escalation` 是否与 Web 保存后的内存配置一致，
        以及 Helper / SQLite 存储是否已挂载（与 Bot 运行时读取同一 `config_manager.config`）。
        """
        api_auth(request)
        cfg = config_manager.config or {}
        he = cfg.get("human_escalation") or {}
        if not isinstance(he, dict):
            he = {}
        st = getattr(telegram_client, "_human_escalation_store", None) if telegram_client else None
        h = getattr(telegram_client, "_human_escalation", None) if telegram_client else None
        agents = he.get("agents")
        n_agents = len(agents) if isinstance(agents, list) else 0
        teams = he.get("agent_teams")
        n_teams = len(teams) if isinstance(teams, list) else 0
        from src.utils.human_escalation import _resolve_duty_mode

        dm = _resolve_duty_mode(he)
        try:
            rt = int(he.get("repeat_threshold", 3) or 3)
        except (TypeError, ValueError):
            rt = 3
        try:
            uid_fb = int(he.get("human_user_id") or 0)
        except (TypeError, ValueError):
            uid_fb = 0
        return {
            "ok": True,
            "helper_loaded": h is not None,
            "store_loaded": st is not None,
            "config_path": str(getattr(config_manager, "config_path", "") or ""),
            "effective": {
                "enabled": bool(he.get("enabled")),
                "repeat_threshold": max(2, rt),
                "duty_mode": dm,
                "timezone": (he.get("timezone") or "UTC").strip() or "UTC",
                "escalation_cooldown_scope": (
                    (he.get("escalation_cooldown_scope") or "per_normalized_question")
                    .strip()
                    or "per_normalized_question"
                ),
                "agents_count": n_agents,
                "agent_teams_count": n_teams,
                "single_fallback_username": bool(str(he.get("human_username") or "").strip()),
                "single_fallback_user_id": uid_fb,
            },
            "note": "来源：config_manager.config（Web 保存后立即写入同一 dict）；"
            "Bot 处理消息时会对 HumanEscalationHelper.reload_config(同一 dict)。",
        }
