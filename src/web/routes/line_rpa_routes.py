"""LINE RPA 自动聊天 —— Web 页面 + REST API。

页面：
    GET /line-rpa                      — 卡片式管理页

REST：
    GET  /api/line-rpa/status          — 运行状态 / 运行统计 / 最近一次结果
    GET  /api/line-rpa/recent          — 最近 N 条 run 历史（可只看有对方消息的）
    GET  /api/line-rpa/chats           — 最近活跃的会话列表
    POST /api/line-rpa/pause           — {"seconds": 300} 暂停 N 秒
    POST /api/line-rpa/resume          — 立刻恢复
    POST /api/line-rpa/trigger         — 立即触发下一轮
    GET  /api/line-rpa/config          — 当前有效配置（敏感信息脱敏）
    PUT  /api/line-rpa/config          — 热更新配置段（写 config.yaml）
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

logger = logging.getLogger(__name__)


def _get_service(request: Request):
    svc = getattr(request.app.state, "line_rpa_service", None)
    return svc


def _redact_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """最小脱敏：当前 line_rpa 段没有 key 字段，但 vision_read_fallback 可能含 api_key。"""
    try:
        import copy
        c = copy.deepcopy(cfg) if cfg else {}
    except Exception:
        c = dict(cfg or {})
    for sub in ("vision_read_fallback",):
        if isinstance(c.get(sub), dict):
            for k in list(c[sub].keys()):
                if any(s in k.lower() for s in ("key", "secret", "token", "password")):
                    if c[sub][k]:
                        c[sub][k] = "***"
    return c


def register_line_rpa_routes(app, *, page_auth, api_auth, templates, config_manager,
                              audit_store=None):
    """在 FastAPI app 上挂载 LINE RPA 相关路由。"""

    @app.get("/line-rpa", response_class=HTMLResponse)
    async def line_rpa_page(request: Request, _=Depends(page_auth)):
        # 权限由 page_auth + _require_role 负责（在 admin.py 里增加 line_rpa key）
        return templates.TemplateResponse(request, "line_rpa.html", {})

    @app.get("/api/line-rpa/status")
    async def api_line_rpa_status(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            # 服务未构建（line_rpa.enabled=false 或构建异常）
            return {
                "available": False,
                "enabled_cfg": bool((config_manager.config or {})
                                    .get("line_rpa", {}).get("enabled")),
                "hint": "line_rpa.enabled=false 或服务未启动；在 设置 或 config.yaml 中打开后重启进程",
            }
        st = svc.status()
        st["available"] = True
        return st

    @app.get("/api/line-rpa/recent")
    async def api_line_rpa_recent(request: Request, limit: int = 50,
                                   only_with_peer: int = 0):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "runs": []}
        rows = svc.recent_runs(limit=limit, only_with_peer=bool(only_with_peer))
        return {"available": True, "runs": rows}

    @app.get("/api/line-rpa/metrics", response_class=PlainTextResponse)
    async def api_line_rpa_metrics(request: Request):
        """P3-6：Prometheus 文本导出。

        暴露指标：
            line_rpa_available            (0/1)
            line_rpa_paused               (0/1)
            line_rpa_runs_total           1h / 24h 窗口
            line_rpa_runs_ok              1h / 24h 窗口
            line_rpa_sent_total           1h / 24h 窗口
            line_rpa_avg_send_ms          最近窗口
            line_rpa_last_unread          最后一轮看到的未读数
            line_rpa_last_chats_processed
            line_rpa_step_count{step=...} 24h 各 step 计数
        """
        api_auth(request)
        lines: list = []

        def _m(name: str, help_txt: str, type_: str = "gauge"):
            lines.append(f"# HELP {name} {help_txt}")
            lines.append(f"# TYPE {name} {type_}")

        svc = _get_service(request)
        if svc is None:
            _m("line_rpa_available", "Whether LineRpaService is running")
            lines.append("line_rpa_available 0")
            return "\n".join(lines) + "\n"

        st = svc.status()
        _m("line_rpa_available", "Whether LineRpaService is running")
        lines.append("line_rpa_available 1")
        _m("line_rpa_paused", "Whether LineRpaService is currently paused")
        lines.append(f"line_rpa_paused {1 if st.get('paused') else 0}")

        stats_24 = st.get("stats_24h") or {}
        stats_1 = st.get("stats_1h") or {}

        _m("line_rpa_runs_total", "Total runs recorded")
        lines.append(f'line_rpa_runs_total{{window="1h"}} {int(stats_1.get("total", 0) or 0)}')
        lines.append(f'line_rpa_runs_total{{window="24h"}} {int(stats_24.get("total", 0) or 0)}')

        _m("line_rpa_runs_ok", "Runs marked ok")
        lines.append(f'line_rpa_runs_ok{{window="1h"}} {int(stats_1.get("ok", 0) or 0)}')
        lines.append(f'line_rpa_runs_ok{{window="24h"}} {int(stats_24.get("ok", 0) or 0)}')

        _m("line_rpa_sent_total", "Runs with step=sent")
        lines.append(f'line_rpa_sent_total{{window="1h"}} {int(stats_1.get("sent", 0) or 0)}')
        lines.append(f'line_rpa_sent_total{{window="24h"}} {int(stats_24.get("sent", 0) or 0)}')

        _m("line_rpa_avg_send_ms", "Average total_ms for step=sent over window")
        lines.append(f'line_rpa_avg_send_ms{{window="24h"}} {float(stats_24.get("avg_send_ms", 0) or 0)}')
        lines.append(f'line_rpa_avg_send_ms{{window="1h"}} {float(stats_1.get("avg_send_ms", 0) or 0)}')

        last_extras = st.get("last_run_extras") or {}
        last_unread = last_extras.get("unread_count")
        _m("line_rpa_last_unread", "Unread count seen in the last run (-1 = unknown)")
        try:
            lines.append(f"line_rpa_last_unread {int(last_unread if last_unread is not None else -1)}")
        except (TypeError, ValueError):
            lines.append("line_rpa_last_unread -1")

        last_cp = last_extras.get("chats_processed")
        _m("line_rpa_last_chats_processed", "Chats processed in the last multi-chat run")
        try:
            lines.append(f"line_rpa_last_chats_processed {int(last_cp if last_cp is not None else 0)}")
        except (TypeError, ValueError):
            lines.append("line_rpa_last_chats_processed 0")

        _m("line_rpa_step_count", "Count per step over window", type_="counter")
        for s in (stats_24.get("steps") or [])[:10]:
            step_name = str(s.get("step") or "unknown").replace('"', "_")[:40]
            cnt = int(s.get("count") or 0)
            lines.append(f'line_rpa_step_count{{step="{step_name}",window="24h"}} {cnt}')

        return "\n".join(lines) + "\n"

    @app.get("/api/line-rpa/notifications")
    async def api_line_rpa_notifications(request: Request):
        """P3-1：通知栏双校验 — 拉一次 dumpsys notification 并做健康对账。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "reason": "service_not_started"}
        try:
            data = await svc.notification_snapshot()
        except Exception as e:  # noqa: BLE001
            return {
                "available": False,
                "reason": f"snapshot_exception:{e}",
            }
        data["available"] = True
        return data

    # ── P4-5：告警闭环 ────────────────────────────────────

    @app.get("/api/line-rpa/timeline")
    async def api_line_rpa_timeline(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "items": []}
        try:
            minutes = max(1, min(24 * 60, int(request.query_params.get("minutes", 60))))
        except (TypeError, ValueError):
            minutes = 60
        try:
            limit = max(1, min(500, int(request.query_params.get("limit", 200))))
        except (TypeError, ValueError):
            limit = 200
        items = svc.timeline(minutes=minutes, limit=limit)
        return {"available": True, "items": items, "minutes": minutes}

    @app.get("/api/line-rpa/audit")
    async def api_line_rpa_audit(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "items": []}
        target_type = request.query_params.get("target_type") or None
        try:
            limit = max(1, min(500, int(request.query_params.get("limit", 100))))
        except (TypeError, ValueError):
            limit = 100
        return {
            "available": True,
            "items": svc.list_audit(target_type=target_type, limit=limit),
        }

    @app.get("/api/line-rpa/alerts")
    async def api_line_rpa_alerts(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "items": [], "unacked": 0}
        only_unacked = request.query_params.get("only_unacked", "1") != "0"
        try:
            limit = max(1, min(200, int(request.query_params.get("limit", 50))))
        except (TypeError, ValueError):
            limit = 50
        items = svc.list_alerts(only_unacked=only_unacked, limit=limit)
        return {
            "available": True,
            "items": items,
            "unacked": svc.alerts_count_unacked(),
        }

    @app.post("/api/line-rpa/alerts/{alert_id}/ack")
    async def api_line_rpa_alerts_ack(alert_id: int, request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service_not_started")
        user = ""
        try:
            user = getattr(request.state, "user", "") or ""
        except Exception:
            user = ""
        res = svc.ack_alert(alert_id, by=str(user))
        if res is None:
            raise HTTPException(404, "alert_not_found")
        return {"ok": True, "item": res}

    @app.post("/api/line-rpa/alerts/ack_all")
    async def api_line_rpa_alerts_ack_all(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service_not_started")
        user = ""
        try:
            user = getattr(request.state, "user", "") or ""
        except Exception:
            user = ""
        n = svc.ack_all_alerts(by=str(user))
        return {"ok": True, "acked": int(n)}

    # ── P4-3：Human-in-the-Loop 审核队列 ──────────────────

    @app.get("/api/line-rpa/pending")
    async def api_line_rpa_pending(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "items": [], "stats": {}}
        status_q = request.query_params.get("status") or None
        try:
            limit = max(1, min(200, int(request.query_params.get("limit", 50))))
        except (TypeError, ValueError):
            limit = 50
        items = svc.list_pending(status=status_q, limit=limit)
        return {
            "available": True,
            "items": items,
            "stats": svc.pending_stats(),
        }

    @app.post("/api/line-rpa/pending/{pending_id}/resolve")
    async def api_line_rpa_pending_resolve(pending_id: int, request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service_not_started")
        try:
            body = await request.json()
        except Exception:
            body = {}
        action = str(body.get("action") or "").strip().lower()
        if action not in {"approve", "reject", "edit_approve", "cancel"}:
            raise HTTPException(400, "invalid action")
        final_reply = body.get("final_reply")
        if final_reply is not None and not isinstance(final_reply, str):
            raise HTTPException(400, "final_reply must be string")
        user = ""
        try:
            user = getattr(request.state, "user", "") or ""
        except Exception:
            user = ""
        res = svc.resolve_pending(
            pending_id, action=action, final_reply=final_reply, by=str(user),
        )
        if res is None:
            raise HTTPException(404, "pending_not_found")
        return {"ok": True, "item": res}

    @app.get("/api/line-rpa/screenshot/{name}")
    async def api_line_rpa_screenshot(name: str, request: Request):
        """读取失败留痕截图（仅允许 failure_shots.dir 目录下的 .png）。"""
        api_auth(request)
        # 安全检查：只允许单层文件名，不得包含路径分隔符
        if ("/" in name) or ("\\" in name) or (".." in name):
            raise HTTPException(400, "invalid filename")
        if not name.lower().endswith(".png"):
            raise HTTPException(400, "only .png allowed")
        lr_cfg = (config_manager.config or {}).get("line_rpa", {}) or {}
        fs_cfg = lr_cfg.get("failure_shots") or {}
        shots_dir = Path(fs_cfg.get("dir") or "logs/line_rpa/failures").resolve()
        fpath = (shots_dir / name).resolve()
        # 防目录穿越：resolve 后必须在 shots_dir 下
        try:
            fpath.relative_to(shots_dir)
        except ValueError:
            raise HTTPException(400, "path escape rejected")
        if not fpath.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(str(fpath), media_type="image/png")

    @app.get("/api/line-rpa/chats")
    async def api_line_rpa_chats(request: Request, limit: int = 30):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "chats": []}
        return {"available": True, "chats": svc.list_chats(limit=limit)}

    @app.post("/api/line-rpa/pause")
    async def api_line_rpa_pause(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "LINE RPA 服务未启动")
        try:
            body = await request.json()
        except Exception:
            body = {}
        seconds = float(body.get("seconds", 300) or 300)
        if seconds < 0:
            seconds = 0
        if seconds > 86400:
            seconds = 86400
        svc.pause_for(seconds)
        if audit_store:
            actor = request.session.get("username", "web_admin")
            audit_store.log(actor, "line_rpa_pause", f"seconds={int(seconds)}")
        return {"ok": True, "pause_remaining_sec": int(seconds)}

    @app.post("/api/line-rpa/resume")
    async def api_line_rpa_resume(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "LINE RPA 服务未启动")
        svc.resume()
        if audit_store:
            actor = request.session.get("username", "web_admin")
            audit_store.log(actor, "line_rpa_resume", "")
        return {"ok": True}

    @app.post("/api/line-rpa/trigger")
    async def api_line_rpa_trigger(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "LINE RPA 服务未启动")
        svc.trigger_once()
        return {"ok": True}

    @app.get("/api/line-rpa/config")
    async def api_line_rpa_config(request: Request):
        api_auth(request)
        svc = _get_service(request)
        raw_cfg = (config_manager.config or {}).get("line_rpa", {}) or {}
        if svc is not None:
            effective = svc.effective_config()
        else:
            effective = raw_cfg
        return {
            "raw": _redact_cfg(raw_cfg),
            "effective": _redact_cfg(effective),
        }

    @app.put("/api/line-rpa/config")
    async def api_line_rpa_config_update(request: Request):
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body 必须是对象")
        # 只允许更新白名单字段，避免误写入破坏式配置
        ALLOWED = {
            "enabled", "adb_serial", "prefer_line_device",
            "chat_key", "default_reply_lang",
            "peer_left_ratio", "after_launch_sleep_sec",
            "redump_before_send", "use_backend_persona", "reply_style_hint",
            "human_pacing", "service", "screenshot_ocr",
            "navigation", "failure_shots",
            "self_names", "group_reply_policy", "reply_style_hint_mentioned",
            "peer_multi_bubble",
            "reply_mode", "approve_max_deliver_per_cycle",
            "health_check",
            "approve_stale_check", "approve_pending_ttl_hours",
            "alert_thresholds",
            "vision_scan",
        }
        bad = [k for k in body.keys() if k not in ALLOWED]
        if bad:
            raise HTTPException(400, f"不允许的字段: {bad}")
        cfg = config_manager.config or {}
        lr = cfg.get("line_rpa") or {}
        if not isinstance(lr, dict):
            lr = {}
        for k, v in body.items():
            if isinstance(v, dict) and isinstance(lr.get(k), dict):
                merged = dict(lr[k])
                merged.update(v)
                lr[k] = merged
            else:
                lr[k] = v
        cfg["line_rpa"] = lr
        try:
            config_manager.save()
        except Exception as e:
            raise HTTPException(500, f"保存配置失败: {e}")
        # 热更新 service
        svc = _get_service(request)
        if svc is not None:
            try:
                svc.reconfigure(lr)
            except Exception:
                logger.debug("service.reconfigure 失败", exc_info=True)
        if audit_store:
            actor = request.session.get("username", "web_admin")
            audit_store.log(actor, "line_rpa_config_update",
                            f"keys={list(body.keys())}")
        return {"ok": True, "updated_keys": list(body.keys())}
