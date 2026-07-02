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
    POST /api/line-rpa/accept-friends  — 手动触发一次好友申请接受
    GET  /api/line-rpa/device-screenshot — ADB 设备按需截屏（PNG）
    GET  /api/line-rpa/config          — 当前有效配置（敏感信息脱敏）
    PUT  /api/line-rpa/config          — 热更新配置段（写 config.yaml）

手动发送队列（P28）：
    POST /api/line-rpa/send-manual              — 入队一条主动发送任务
    GET  /api/line-rpa/send-queue               — 列出队列（?limit=30&include_done=0）
    GET  /api/line-rpa/send-queue/{item_id}     — 查询单条任务
    POST /api/line-rpa/send-queue/{item_id}/cancel — 取消待发任务
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from src.web.web_i18n import tr

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

    @app.get("/api/line-rpa/log-tail", response_class=PlainTextResponse)
    async def api_line_rpa_log_tail(request: Request, n: int = 80):
        """最近 N 行 LINE RPA 相关日志（从 logs/app.log 过滤）。"""
        api_auth(request)
        from pathlib import Path as _P
        for candidate in ("logs/app.log", "logs/bot.log", "app.log"):
            p = _P(candidate)
            if p.exists():
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                    lines = [l for l in text.splitlines()
                             if "line_rpa" in l.lower() or "LineRpa" in l]
                    return PlainTextResponse("\n".join(lines[-max(1, min(200, n)):]))
                except Exception as e:
                    return PlainTextResponse(f"Error reading {candidate}: {e}")
        return PlainTextResponse("")

    @app.get("/api/line-rpa/chats")
    async def api_line_rpa_chats(request: Request, limit: int = 30):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "chats": []}
        return {"available": True, "chats": svc.list_chats(limit=limit)}

    # ── P8-2: 聊天历史分析 ────────────────────────────────

    @app.get("/api/line-rpa/sessions/{chat_key:path}")
    async def api_line_sessions(request: Request, chat_key: str):
        """按 4h 间隔分组的会话摘要。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "sessions": []}
        return {"available": True, "sessions": svc.sessions_for_chat(chat_key), "chat_key": chat_key}

    @app.get("/api/line-rpa/chat-history/{chat_key:path}")
    async def api_line_chat_history(request: Request, chat_key: str,
                                     limit: int = 10, offset: int = 0):
        """分页拉取指定联系人的对话记录（含 intent_tag）。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "messages": [], "total": 0}
        msgs = svc.chat_history(chat_key, limit=limit, offset=offset)
        total = svc.total_turns_for_chat(chat_key)
        return {"available": True, "messages": msgs, "total": total, "offset": offset}

    @app.get("/api/line-rpa/customer-profile/{chat_key:path}")
    async def api_line_customer_profile(request: Request, chat_key: str):
        """联系人全量画像（历史统计 + 意图分布）。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "profile": {}}
        return {"available": True, "profile": svc.customer_profile(chat_key), "chat_key": chat_key}

    @app.get("/api/line-rpa/search")
    async def api_line_search(request: Request, q: str = "", intent: str = "",
                               days: int = 30, limit: int = 20):
        """跨联系人全文检索。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "results": [], "q": q}
        results = svc.search_history(q, intent=intent, days=days, limit=min(limit, 50))
        return {"available": True, "results": results, "q": q, "intent": intent}

    @app.get("/api/line-rpa/intent-stats")
    async def api_line_intent_stats(request: Request, hours: float = 168.0):
        """近 N 小时意图分布统计。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            return {"available": False, "distribution": {}, "total_turns": 0}
        stats = svc.intent_stats(window_hours=hours)
        return {"available": True, **stats}

    @app.post("/api/line-rpa/pause")
    async def api_line_rpa_pause(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, tr(request, "err.rpa.service_not_started", platform="LINE"))
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
            raise HTTPException(503, tr(request, "err.rpa.service_not_started", platform="LINE"))
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
            raise HTTPException(503, tr(request, "err.rpa.service_not_started", platform="LINE"))
        auto_started = False
        if hasattr(svc, "is_running") and not svc.is_running:
            if hasattr(svc, "force_start"):
                auto_started = await svc.force_start()
        svc.trigger_once()
        return {"ok": True, "auto_started": auto_started, "is_running": getattr(svc, "is_running", None)}

    @app.post("/api/line-rpa/accept-friends")
    async def api_line_rpa_accept_friends(request: Request):
        """手动触发一次好友申请接受（在当前屏幕 XML 查找同意按钮并点击）。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, tr(request, "err.rpa.service_not_started", platform="LINE"))
        try:
            res = await svc._runner.maybe_auto_accept_friends(max_accept=10)
        except Exception as e:
            raise HTTPException(500, str(e))
        return {"ok": True, "result": res}

    @app.get("/api/line-rpa/device-screenshot")
    async def api_line_rpa_device_screenshot(request: Request):
        """按需从 ADB 设备截屏，返回 PNG（不依赖失败留痕目录）。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, tr(request, "err.rpa.service_not_started", platform="LINE"))
        runner = svc._runner
        serial = runner._serial
        if not serial:
            raise HTTPException(503, tr(request, "err.rpa.no_adb_device"))
        try:
            from src.integrations.line_rpa import screen_ocr, adb_helpers as _adb
            png = await asyncio.to_thread(screen_ocr.capture_screen_png, serial, _adb)
        except Exception as e:
            raise HTTPException(500, tr(request, "err.rpa.screenshot_failed", err=e))
        if not png:
            raise HTTPException(503, tr(request, "err.rpa.screenshot_empty"))
        return Response(content=png, media_type="image/png")

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
            raise HTTPException(400, tr(request, "err.rpa.body_must_be_object"))
        # 只允许更新白名单字段，避免误写入破坏式配置
        ALLOWED = {
            "enabled", "adb_serial", "prefer_line_device",
            "chat_key", "default_reply_lang", "daily_cap",
            "peer_left_ratio", "after_launch_sleep_sec",
            "redump_before_send", "use_backend_persona", "reply_style_hint",
            "human_pacing", "service", "screenshot_ocr",
            "navigation", "failure_shots",
            "self_names", "group_reply_policy", "reply_style_hint_mentioned",
            "peer_multi_bubble",
            "reply_mode", "approve_max_deliver_per_cycle",
            "health_check",
            "approve_stale_check", "approve_pending_ttl_hours",
            "alert_thresholds", "auto_accept",
            "vision_scan",
        }
        bad = [k for k in body.keys() if k not in ALLOWED]
        if bad:
            raise HTTPException(400, tr(request, "err.rpa.disallowed_fields", bad=bad))
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
            raise HTTPException(500, tr(request, "err.set.save_config_failed", err=e))
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

    # ── P28：手动发送队列 ──────────────────────────────────────────────────

    @app.post("/api/line-rpa/send-manual")
    async def api_line_rpa_send_manual(request: Request):
        """入队一条主动发送任务。

        Body JSON: {"chat_key": "...", "peer_name": "...", "text": "..."}
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, tr(request, "err.rpa.service_not_started", platform="LINE"))
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
        chat_key = (body.get("chat_key") or "").strip()
        peer_name = (body.get("peer_name") or "").strip()
        text = (body.get("text") or "").strip()
        if not chat_key:
            raise HTTPException(400, tr(request, "err.rpa.chat_key_required"))
        if not text:
            raise HTTPException(400, tr(request, "err.set.text_required"))
        try:
            actor = request.session.get("username", "web_admin")
        except Exception:
            actor = "api"
        try:
            item_id = svc.enqueue_send(
                chat_key=chat_key, peer_name=peer_name, text=text, created_by=actor,
            )
        except Exception as e:
            raise HTTPException(500, str(e))
        if audit_store:
            audit_store.log(actor, "line_rpa_send_manual_enqueue",
                            f"id={item_id} chat_key={chat_key}")
        return {"ok": True, "item_id": item_id}

    @app.get("/api/line-rpa/send-queue")
    async def api_line_rpa_send_queue_list(request: Request):
        """列出发送队列。可选参数: limit（默认30）、include_done（0/1）。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, tr(request, "err.rpa.service_not_started", platform="LINE"))
        try:
            limit = int(request.query_params.get("limit", 30))
            include_done = request.query_params.get("include_done", "0") not in ("0", "false", "")
        except ValueError:
            raise HTTPException(400, tr(request, "err.rpa.limit_must_be_int"))
        items = svc.list_send_queue(limit=limit, include_done=include_done)
        return {"items": items, "count": len(items)}

    @app.get("/api/line-rpa/send-queue/{item_id}")
    async def api_line_rpa_send_queue_get(item_id: int, request: Request):
        """查询单条发送任务。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, tr(request, "err.rpa.service_not_started", platform="LINE"))
        item = svc.get_send_queue_item(item_id)
        if item is None:
            raise HTTPException(404, tr(request, "err.rpa.queue_item_not_found", item_id=item_id))
        return item

    @app.post("/api/line-rpa/send-queue/{item_id}/cancel")
    async def api_line_rpa_send_queue_cancel(item_id: int, request: Request):
        """取消一条待发任务（仅限 queued 状态）。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, tr(request, "err.rpa.service_not_started", platform="LINE"))
        ok = svc.cancel_send_queue_item(item_id)
        if not ok:
            raise HTTPException(409, tr(request, "err.rpa.queue_item_not_cancelable", item_id=item_id))
        try:
            actor = request.session.get("username", "web_admin")
        except Exception:
            actor = "api"
        if audit_store:
            audit_store.log(actor, "line_rpa_send_queue_cancel", f"id={item_id}")
        return {"ok": True, "item_id": item_id}

    # ── P7-D: 对话语言锁定 ──────────────────────────────────────────────

    @app.post("/api/line-rpa/chat-lang-lock")
    async def api_line_chat_lang_lock(request: Request):
        """锁定或解锁 LINE 指定对话的回复语言。

        Body: {chat_key: str, lang: str|null}
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, tr(request, "err.rpa.service_not_started", platform="LINE"))
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON body")
        chat_key = str(body.get("chat_key") or "").strip()
        if not chat_key:
            raise HTTPException(400, "chat_key is required")
        lang_raw = body.get("lang")
        lang = str(lang_raw).strip().lower() if lang_raw else ""
        _VALID_LANGS = {
            "zh", "en", "de", "ja", "ko", "fr", "es", "ar", "ru",
            "hi", "it", "pt", "nl", "pl", "tr", "cs", "hu",
        }
        if lang and lang not in _VALID_LANGS:
            raise HTTPException(400, tr(request, "err.rpa.lang_unsupported", lang=lang, langs=sorted(_VALID_LANGS)))
        ss = getattr(svc, "_state_store", None) or getattr(
            getattr(svc, "_runner", None), "_state_store", None
        )
        if ss is None:
            raise HTTPException(503, tr(request, "err.rpa.state_store_unavailable"))
        try:
            ss.set_forced_lang(chat_key, lang or None)
        except Exception as e:
            raise HTTPException(500, tr(request, "err.rpa.write_failed", err=e))
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
        action = f"锁定为 {lang}" if lang else "解除锁定（恢复自动/默认）"
        return {"ok": True, "chat_key": chat_key, "forced_lang": lang or None, "action": action}

    # ── P13-D: 批量取消端点 ──────────────────────────────────────────────

    @app.post("/api/line-rpa/pending/cancel-all")
    async def api_line_pending_cancel_all(request: Request):
        """P13-D: 立即取消所有 pending/approved 行（批量清空）。"""
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
                audit_store.log(actor, "line_pending_cancel_all", f"cancelled={len(cancelled)}")
            except Exception:
                pass
        return {"ok": True, "cancelled": len(cancelled), "ids": cancelled}

    # ── P7-A: TTS ready 轮询端点 ─────────────────────────────────────────

    @app.post("/api/line-rpa/pending/{pending_id}/retry-tts")
    async def api_line_pending_retry_tts(pending_id: int, request: Request):
        """P12-D: 重置 TTS ERROR 哨兵，让 runner 下一轮自动重新生成 TTS 预览。"""
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
            return {"ok": False, "error": "not found or not in pending/approved status"}
        return {"ok": True, "pending_id": pending_id}

    @app.get("/api/line-rpa/pending-tts")
    async def api_line_pending_tts(request: Request, ids: str = ""):
        """P7-A: 返回指定 pending_id 列表的 tts_path 映射，供前端轮询。

        Query: ?ids=1,2,3  Returns: {"ok":true, "paths":{"1":"...", "2":""}}
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
