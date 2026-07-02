"""信息/日志/分析 页面路由（Phase E1 续拆，从 admin.py 抽出）。

端点（与抽出前逐行一致）：
  GET /help            帮助页
  GET /training        客服培训全屏幻灯片
  GET /logs            实时日志页
  GET /logs/stream     日志 SSE 流
  GET /analytics       分析页
  GET /cases           Case 面板页

首批经 AdminRouteContext 注入 templates / log_buffer 的页面路由（之前页面路由因
需 templates 留在 admin.py）。依赖：templates / page_auth / event_tracker / log_buffer。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from src.web.web_i18n import tr

# docs/training/... 相对仓库根：本文件位于 src/web/routes/ → parents[3] 为仓库根
_TRAINING_SLIDES_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "training" /
    "客服培训演示_AI助手系统.html"
)


def register_page_routes(app, ctx) -> None:
    templates = ctx.templates
    _page_auth = ctx.page_auth
    event_tracker = ctx.event_tracker
    log_buffer = ctx.log_buffer

    @app.get("/help", response_class=HTMLResponse)
    async def help_page(request: Request, _=Depends(_page_auth)):
        from src.web.help_commands import get_help_sections

        return templates.TemplateResponse(request, "help.html", {
            "request": request,
            "help_sections": get_help_sections(),
        })

    @app.get("/training", response_class=HTMLResponse)
    async def training_slides_page(request: Request, _=Depends(_page_auth)):
        """客服培训用全屏 HTML 幻灯片（需登录）。"""
        if not _TRAINING_SLIDES_PATH.is_file():
            raise HTTPException(status_code=404, detail=tr(request, "err.page.training_not_found"))
        html = _TRAINING_SLIDES_PATH.read_text(encoding="utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(request: Request, _=Depends(_page_auth), limit: int = 200):
        recent = []
        if log_buffer:
            recent = log_buffer.get_recent(limit)
        return templates.TemplateResponse(request, "logs.html", {
            "recent": recent, "limit": limit,
        })

    @app.get("/logs/stream")
    async def logs_stream(request: Request, _=Depends(_page_auth)):
        if not log_buffer:
            return StreamingResponse(iter([]), media_type="text/event-stream")

        async def _generate():
            import asyncio as _asyncio, json as _json
            q = log_buffer.subscribe()
            try:
                while True:
                    try:
                        # 30 秒超时：超时则发心跳，防止代理/Nginx 断连
                        entry = await _asyncio.wait_for(q.get(), timeout=30.0)
                        yield f"data: {_json.dumps(entry, ensure_ascii=False)}\n\n"
                    except _asyncio.TimeoutError:
                        yield "data: ping\n\n"
            except Exception:
                pass
            finally:
                log_buffer.unsubscribe(q)

        return StreamingResponse(_generate(), media_type="text/event-stream",
                                 headers={
                                     "Cache-Control": "no-cache, no-transform",
                                     "X-Accel-Buffering": "no",
                                     "Connection": "keep-alive",
                                 })

    @app.get("/analytics", response_class=HTMLResponse)
    async def analytics_page(request: Request, _=Depends(_page_auth), hours: int = 24):
        data = {"cmd_stats": [], "hourly": [], "top_users": [], "resp_dist": {}, "total": 0}
        if event_tracker:
            data["cmd_stats"] = event_tracker.command_stats(hours)
            data["hourly"] = event_tracker.hourly_trend(hours)
            data["top_users"] = event_tracker.top_users(hours)
            data["resp_dist"] = event_tracker.response_time_distribution(hours)
            data["total"] = event_tracker.total_events(hours)
        data["hours"] = hours
        return templates.TemplateResponse(request, "analytics.html", {**data})

    @app.get("/cases", response_class=HTMLResponse)
    async def cases_page(request: Request, _=Depends(_page_auth)):
        return templates.TemplateResponse(request, "cases.html", {})
