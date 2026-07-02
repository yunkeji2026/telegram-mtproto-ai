"""统一收件箱——坐席工作台 HTML 页面壳路由域（巨石拆分 slice 17 + slice 38a）。

把统一收件箱/工作台相关 HTML 页面从 ``register_unified_inbox_routes`` 巨型闭包中外移为
``register_workspace_pages_routes(app, *, page_auth, templates, config_manager)``：

- slice 38a：主工作台 ``/workspace``（unified_inbox.html）+ 旧入口 ``/unified-inbox`` redirect
- slice 17：``/workspace/contacts|tasks|dash|escalations`` 四个子页面壳

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from starlette.responses import RedirectResponse

from src.web.routes.unified_inbox_auth import _is_supervisor

logger = logging.getLogger(__name__)


def register_workspace_pages_routes(
    app, *, page_auth, templates, config_manager=None,
) -> None:
    """挂载工作台 HTML 页面（主入口 + 子页面壳）。"""

    # P5-2：「成交/完成」阶段单一来源 → 注入工作台各页 JS（收件箱 done 筛选 / KPI /
    # 看板已成交卡片同口径，防前后端漂移）。排序保证 tojson 输出稳定、测试可断言。
    try:
        from src.contacts.models import (
            FUNNEL_DONE_STAGES as _FUNNEL_DONE_STAGES,
            WON_STAGES as _WON_STAGES,
        )
        _funnel_done_sorted = sorted(_FUNNEL_DONE_STAGES)
        _won_sorted = sorted(_WON_STAGES)
    except Exception:  # 极端 import 失败也不阻断页面渲染
        # stage-source-allow: 仅为 models import 失败时的兜底默认，非成交判定逻辑（P5-2c 扫描门禁豁免）
        _funnel_done_sorted = ["BONDED", "CONVERTED", "LINE_ACCEPTED", "LINE_ENGAGED"]  # stage-source-allow
        _won_sorted = ["BONDED", "CONVERTED"]  # stage-source-allow

    def _page_ctx(request: Request) -> Dict[str, Any]:
        ctx: Dict[str, Any] = {
            "user_name": request.session.get("username") or "",
            "user_display_name": request.session.get("display_name")
            or request.session.get("username") or "",
            "funnel_done_stages": _funnel_done_sorted,
            "won_stages": _won_sorted,
        }
        # P3：账号手机号显示脱敏开关（治理化，默认脱敏=True；演示/隐私可控）
        ctx["mask_account_phone"] = True
        try:
            if config_manager is not None:
                _cfg = config_manager.config or {}
                _wa = _cfg.get("web_admin", {}) or {}
                if _wa.get("site_name"):
                    ctx["site_name"] = _wa.get("site_name")
                _sp = (_cfg.get("accounts", {}) or {}).get("self_profile", {}) or {}
                if "mask_phone" in _sp:
                    ctx["mask_account_phone"] = bool(_sp.get("mask_phone"))
        except Exception:
            pass
        return ctx

    @app.get("/workspace", response_class=HTMLResponse)
    async def workspace_page(request: Request, _=Depends(page_auth)):
        return templates.TemplateResponse(request, "unified_inbox.html", _page_ctx(request))

    @app.get("/unified-inbox")
    async def unified_inbox_redirect(request: Request, _=Depends(page_auth)):
        """旧入口：保留并 307→ 新独立工作台 /workspace。"""
        return RedirectResponse("/workspace", status_code=307)

    @app.get("/workspace/contacts", response_class=HTMLResponse)
    async def workspace_contacts_page(request: Request, _=Depends(page_auth)):
        return templates.TemplateResponse(request, "contacts_list.html", _page_ctx(request))

    @app.get("/workspace/tasks", response_class=HTMLResponse)
    async def workspace_tasks_page(request: Request, _=Depends(page_auth)):
        return templates.TemplateResponse(request, "tasks.html", _page_ctx(request))

    @app.get("/workspace/dash", response_class=HTMLResponse)
    async def workspace_dash_page(request: Request, _=Depends(page_auth)):
        return templates.TemplateResponse(request, "workspace_dashboard.html", _page_ctx(request))

    @app.get("/workspace/escalations", response_class=HTMLResponse)
    async def workspace_escalations_page(request: Request, _=Depends(page_auth)):
        if not _is_supervisor(request):
            return RedirectResponse("/workspace/dash", status_code=307)
        return templates.TemplateResponse(request, "escalation_log.html", _page_ctx(request))

    @app.get("/workspace/roi", response_class=HTMLResponse)
    async def workspace_roi_page(request: Request, _=Depends(page_auth)):
        # P0-3：老板视角 ROI 门面（主管专属；非主管回落今日概览）
        if not _is_supervisor(request):
            return RedirectResponse("/workspace/dash", status_code=307)
        return templates.TemplateResponse(request, "workspace_roi.html", _page_ctx(request))

    @app.get("/workspace/setup", response_class=HTMLResponse)
    async def workspace_setup_page(request: Request, _=Depends(page_auth)):
        # P1-1：渠道接入向导（主管专属；非主管回落今日概览）
        if not _is_supervisor(request):
            return RedirectResponse("/workspace/dash", status_code=307)
        return templates.TemplateResponse(request, "setup_wizard.html", _page_ctx(request))

    @app.get("/workspace/kb-start", response_class=HTMLResponse)
    async def workspace_kb_start_page(request: Request, _=Depends(page_auth)):
        # P1-2：知识库冷启动向导（主管专属；非主管回落今日概览）
        if not _is_supervisor(request):
            return RedirectResponse("/workspace/dash", status_code=307)
        return templates.TemplateResponse(request, "kb_cold_start.html", _page_ctx(request))

    @app.get("/workspace/golive", response_class=HTMLResponse)
    async def workspace_golive_page(request: Request, _=Depends(page_auth)):
        # P2-1：上线自检清单（主管专属；非主管回落今日概览）
        if not _is_supervisor(request):
            return RedirectResponse("/workspace/dash", status_code=307)
        return templates.TemplateResponse(request, "golive_checklist.html", _page_ctx(request))

    @app.get("/workspace/ai-quality", response_class=HTMLResponse)
    async def workspace_ai_quality_page(request: Request, _=Depends(page_auth)):
        # P3-1：AI 回复质量闭环看板（主管专属；非主管回落今日概览）
        if not _is_supervisor(request):
            return RedirectResponse("/workspace/dash", status_code=307)
        return templates.TemplateResponse(request, "ai_quality.html", _page_ctx(request))

    @app.get("/workspace/usage", response_class=HTMLResponse)
    async def workspace_usage_page(request: Request, _=Depends(page_auth)):
        # C0-2：用量计量看板（主管/老板专属；非主管回落今日概览）
        if not _is_supervisor(request):
            return RedirectResponse("/workspace/dash", status_code=307)
        return templates.TemplateResponse(request, "workspace_usage.html", _page_ctx(request))
