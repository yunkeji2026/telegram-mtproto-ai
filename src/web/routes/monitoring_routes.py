"""监控 / 指标 / reactivation dry-run 审核路由（Phase E1 批 G2-①）。

复用 AdminRouteContext（新增 boot_ts / event_tracker 字段）。本子批为自包含监控端点
（不含 SSE /api/events，因其依赖 admin 内 _sse_clients 广播状态）。

端点（与抽出前逐行一致）：
  GET  /api/system-info        GET /api/vision-stats        GET /api/bot-metrics
  GET  /api/reactivation/dry-run-samples   POST /api/reactivation/dry-run-feedback
  GET  /api/audit/activity
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import Request


def register_monitoring_routes(app, ctx):
    config_manager = ctx.config_manager
    audit_store = ctx.audit_store
    telegram_client = ctx.telegram_client
    user_store = ctx.user_store
    _kb_store = ctx.kb_store
    boot_ts = ctx.boot_ts
    _api_auth = ctx.api_auth

    @app.get("/api/system-info")
    async def api_system_info(request: Request):
        """返回运行状态摘要供 dashboard 状态栏使用"""
        _api_auth(request)

        import asyncio as _aio

        def _gather():
            bot_online = False
            if telegram_client:
                bot_online = bool(getattr(telegram_client, "running", False))

            last_activity = None
            if audit_store:
                last = audit_store.last_entry()
                if last:
                    last_activity = last.get("ts", "")

            mem_mb = None
            try:
                import psutil as _ps
                mem_mb = round(_ps.Process().memory_info().rss / 1024 / 1024, 1)
            except Exception:
                try:
                    import resource as _res
                    mem_mb = round(_res.getrusage(_res.RUSAGE_SELF).ru_maxrss / 1024, 1)
                except Exception:
                    pass

            kb_entries = 0
            kb_db_mb = None
            try:
                s = _kb_store.stats()
                kb_entries = s.get("total_entries", 0)
                db_p = getattr(_kb_store, "db_path", None) or getattr(_kb_store, "_db_path", None)
                if db_p and Path(str(db_p)).exists():
                    kb_db_mb = round(Path(str(db_p)).stat().st_size / 1024 / 1024, 2)
            except Exception:
                pass

            ai_cfg = {}
            try:
                ai_cfg = config_manager.config.get("ai", {}) if config_manager.config else {}
            except Exception:
                pass
            embedding_ok = bool(ai_cfg.get("api_key", ""))

            uptime_s = int(time.time() - boot_ts) if boot_ts else 0

            return {
                "bot_online":    bot_online,
                "last_activity": last_activity,
                "memory_mb":     mem_mb,
                "uptime_s":      uptime_s,
                "kb_entries":    kb_entries,
                "kb_db_mb":      kb_db_mb,
                "admin_users":   user_store.user_count(),
                "embedding_ok":  embedding_ok,
            }

        return await _aio.to_thread(_gather)

    @app.get("/api/vision-stats")
    async def api_vision_stats(request: Request):
        """Vision 调用统计——按 (task_name, model) 分桶，含 p50/p95/p99/avg
        + 失败原因 breakdown。

        Query params:
          since_hours: int = 24
          task: str = "" 仅按该 task 过滤
        """
        _api_auth(request)
        try:
            from src.integrations.messenger_rpa import vision_metrics as _vm
            since_hours = float(request.query_params.get("since_hours") or 24)
            task = (request.query_params.get("task") or "").strip() or None
            since_sec = max(60.0, min(since_hours * 3600.0, 30 * 24 * 3600.0))
            rows = _vm.summary(since_sec=since_sec, task_name=task)
            errors = _vm.error_breakdown(since_sec=since_sec, task_name=task)
            return {
                "since_hours": since_hours,
                "task": task,
                "tasks": [
                    {
                        "task_name": r.task_name,
                        "model": r.model,
                        "count": r.count,
                        "ok_count": r.ok_count,
                        "fail_count": r.fail_count,
                        "ok_rate": round(r.ok_rate, 4),
                        "p50_ms": r.p50_ms,
                        "p95_ms": r.p95_ms,
                        "p99_ms": r.p99_ms,
                        "avg_ms": r.avg_ms,
                        "max_ms": r.max_ms,
                    }
                    for r in rows
                ],
                "errors": errors,
            }
        except Exception as e:
            return {"error": f"vision_stats_unavailable:{type(e).__name__}"}

    @app.get("/api/bot-metrics")
    async def api_bot_metrics(request: Request):
        _api_auth(request)
        try:
            from src.monitoring.metrics_store import get_metrics_store
            ms = get_metrics_store()
            snap = ms.snapshot()
            return {
                "messages_received": snap.get("messages_received", 0),
                "messages_replied":  snap.get("messages_replied", 0),
                "api_calls":         snap.get("api_calls", 0),
                "errors_count":      snap.get("errors_count", 0),
                "response_time_avg": snap.get("response_time_avg_ms", 0),
                "response_time_p99": snap.get("response_time_p99_ms", 0),
                "queue_size":        snap.get("queue_size", 0),
                "queue_drops":       snap.get("queue_drops", 0),
                "active_tasks":      snap.get("active_tasks", 0),
                "concurrency_limit": snap.get("concurrency_limit", 0),
                "trigger_layers":    snap.get("trigger_layers", {}),
                "circuit_breaker":   snap.get("circuit_breaker", {}),
                "reply_quality":     snap.get("reply_quality", {}),
                "local_llm_fallback": snap.get("local_llm_fallback", {}),
                "memory":            snap.get("memory", {}),
                "companion_safe_skip": snap.get("companion_safe_skip", {}),
                "deferred_queue":    snap.get("deferred_queue", {}),
                "reactivation":      snap.get("reactivation", {}),
                "pacing":            snap.get("pacing", {}),
                "peer_typing_prefetch": snap.get("peer_typing_prefetch", {}),
                "anti_repeat":       snap.get("anti_repeat", {}),
                "startup_advisories": snap.get("startup_advisories", {}),
                "ai_healthy":        ms.ai_healthy(),
                "ai_errors":         ms._ai_consecutive_errors,
                "uptime_s":          round(ms.uptime_seconds()),
                "last_message_at":   snap.get("last_message_at"),
            }
        except Exception:
            return {"error": "metrics unavailable"}

    @app.get("/api/admin/anti-repeat-advice")
    async def api_anti_repeat_advice(request: Request):
        """防复读运行时调参建议：读累计指标 → 纯函数给「缓存扩缩 / 语义层开关」建议。

        样本不足时 ``sample_ok=false`` 且 ``suggestions=[]``（不给噪音）。只读，不改配置。
        """
        _api_auth(request)
        try:
            from src.monitoring.metrics_store import get_metrics_store
            from src.utils.anti_repeat_advisor import evaluate_anti_repeat_tuning
            ar = get_metrics_store().snapshot().get("anti_repeat", {})
            cfg = getattr(config_manager, "config", None) or {}
            _sem = (((((cfg.get("inbox") or {}).get("auto_draft") or {})
                      .get("anti_repeat") or {}).get("semantic")) or {}) if isinstance(cfg, dict) else {}
            cur_max = int(_sem.get("embed_cache_max", 0) or 0) or 512
            return {"ok": True, **evaluate_anti_repeat_tuning(
                ar, cfg, current_cache_max=cur_max)}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}:{ex}"}

    # ★ W2-D5.1：reactivation dry_run 样本审核端点
    @app.get("/api/reactivation/dry-run-samples")
    async def api_reactivation_dry_samples(
        request: Request, limit: int = 50, before_ts: float = 0,
    ):
        """返回 reactivation_loop dry_run 模式下最近生成的话术样本。"""
        _api_auth(request)
        try:
            from src.monitoring.metrics_store import get_metrics_store
            samples = get_metrics_store().reactivation_dry_samples(
                limit=limit,
                before_ts=before_ts if before_ts > 0 else None,
            )
            return {"count": len(samples), "samples": samples}
        except Exception as ex:
            return {"error": f"{type(ex).__name__}:{ex}"}

    # ★ W2-D6.2：dry_run sample feedback（运营点 like/dislike）
    @app.post("/api/reactivation/dry-run-feedback")
    async def api_reactivation_dry_feedback(request: Request):
        """提交对 dry_run 样本的人工反馈。

        body: {"sample_ts": float, "verdict": "like"|"dislike", "reason": "..."(opt)}
        """
        _api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        verdict = str(body.get("verdict", "")).strip().lower()
        if verdict not in ("like", "dislike"):
            return {"error": "verdict must be 'like' or 'dislike'"}
        sample_ts = float(body.get("sample_ts") or 0)
        reason = str(body.get("reason", "") or "")[:300]
        try:
            from src.monitoring.metrics_store import get_metrics_store
            ms = get_metrics_store()
            ms.record_reactivation_feedback(verdict, sample_ts)
            # ★ W2-D7.5：dislike → 把 reply_text 加入 in-memory 黑名单
            if verdict == "dislike" and sample_ts > 0:
                samples = ms.reactivation_dry_samples(limit=200)
                for s in samples:
                    if abs(float(s.get("ts") or 0) - sample_ts) < 1.0:
                        ms.add_disliked_reply(s.get("reply_text", ""))
                        break
        except Exception:
            pass
        if audit_store:
            try:
                audit_store.add_entry(
                    user="admin",
                    action="reactivation_dry_feedback",
                    detail=(
                        f"verdict={verdict} sample_ts={sample_ts} "
                        f"reason={reason[:120]}"
                    ),
                )
            except Exception:
                pass
        return {"ok": True, "verdict": verdict, "sample_ts": sample_ts}

    # ── O·P 联动质量看板：care + reactivation 发送质量统一视图 ──────
    @app.get("/api/companion/quality-overview")
    async def api_companion_quality_overview(request: Request, window_hours: float = 24):
        """两条主动线（care/reactivation）的 skip 原因 + like/dislike 反馈 + dry_run 计数。"""
        _api_auth(request)
        try:
            from src.monitoring.metrics_store import get_metrics_store
            win = max(1.0, min(float(window_hours or 24), 720)) * 3600.0
            return {"ok": True, **get_metrics_store().companion_quality_overview(
                window_sec=win)}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}:{ex}"}

    # ── 质量趋势（持久化时序）：companion_quality_overview 的历史快照 ──
    @app.get("/api/companion/quality-trend")
    async def api_companion_quality_trend(request: Request, hours: float = 24):
        """返回质量看板的时序快照（care/reactivation 的 like/skip/dry 随时间变化）。

        数据由 QualityTrendSnapshotter 周期落地（companion.quality_trend.enabled 开）；
        未启用 → enabled:false，不报错。
        """
        _api_auth(request)
        import time as _t
        store = getattr(request.app.state, "quality_trend_store", None)
        if store is None:
            return {"ok": True, "enabled": False,
                    "message": "质量趋势持久化未启用（companion.quality_trend.enabled=false）"}
        try:
            since = _t.time() - max(1.0, min(float(hours or 24), 720)) * 3600.0
            points = store.recent(since_ts=since, limit=3000)
            return {"ok": True, "enabled": True, "count": len(points),
                    "points": points}
        except Exception as ex:
            return {"ok": False, "enabled": True, "error": f"{type(ex).__name__}:{ex}"}

    # ── 操作记录活动热力图 ────────────────────────────────────
    @app.get("/api/audit/activity")
    async def api_audit_activity(request: Request, days: int = 84):
        """返回过去 N 天每日活动数量，供热力图使用"""
        _api_auth(request)
        if not audit_store:
            return {"days": {}, "max": 0}
        try:
            rows = audit_store._conn.execute(
                "SELECT DATE(ts) as day, COUNT(*) as cnt "
                "FROM audit_log "
                "WHERE ts >= date('now', ? || ' days') "
                "GROUP BY day ORDER BY day",
                (f"-{days}",),
            ).fetchall()
            day_map = {r["day"]: r["cnt"] for r in rows}
            max_cnt = max(day_map.values(), default=1)
            return {"days": day_map, "max": max_cnt}
        except Exception:
            return {"days": {}, "max": 0}
