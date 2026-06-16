"""统一收件箱——回复模板库 API 路由域（巨石拆分 slice 27）。

把 ``register_unified_inbox_routes`` 巨型闭包中的 I3 模板库子域整体外移为
``register_template_routes(app, *, api_auth)``，由主 register 在**原位置**调用：

- I3 模板库 API：``reply-templates`` (GET/POST) + ``reply-templates/{id}`` (PUT/DELETE)
  + ``reply-templates/{id}/use`` (POST)

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 27 端点契约断言）。

域内私有 helper ``_template_store``（原 register 闭包内函数，仅被模板 handler 使用）随域
下沉为模块级函数。依赖全部朝下：services._inbox_store。只收 api_auth 一个参数。
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from src.web.routes.unified_inbox_services import _inbox_store

logger = logging.getLogger(__name__)


def _template_store(request: Request):
    """取 inbox_store 作模板库后端；未启用则 503。"""
    s = _inbox_store(request)
    if s is None:
        raise HTTPException(503, "模板库未启用（需 inbox_store）")
    return s


def register_template_routes(app, *, api_auth) -> None:
    """挂载回复模板库 CRUD + 使用计数端点。"""

    # ── I3 模板库 API ─────────────────────────────────────────────

    @app.get("/api/reply-templates")
    async def api_templates_list(
        request: Request,
        language: str = "",
        platform: str = "",
        scene: str = "",
        search: str = "",
        limit: int = 100,
    ):
        """I3：列出回复模板（支持语言/平台/场景/关键词过滤）。"""
        api_auth(request)
        ts = _template_store(request)
        templates = ts.list_templates(
            language=language, platform=platform, scene=scene,
            search=search, limit=min(200, max(1, int(limit or 100)))
        )
        return {"ok": True, "templates": templates, "count": len(templates)}

    @app.post("/api/reply-templates")
    async def api_templates_create(request: Request):
        """I3：创建新模板（坐席/主管均可，主管审核后启用）。"""
        api_auth(request)
        ts = _template_store(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "请求体解析失败")
        title = str(body.get("title") or "").strip()
        content = str(body.get("content") or "").strip()
        if not title or not content:
            raise HTTPException(400, "title 和 content 不能为空")
        tid = ts.create_template(
            title=title,
            content=content,
            language=str(body.get("language") or "zh"),
            platform=str(body.get("platform") or ""),
            scene=str(body.get("scene") or ""),
            created_by=str(body.get("created_by") or "agent"),
        )
        return {"ok": True, "id": tid, "title": title}

    @app.put("/api/reply-templates/{template_id}")
    async def api_templates_update(request: Request, template_id: str):
        """I3：更新模板字段（仅主管可完全编辑；普通坐席不能修改）。"""
        api_auth(request)
        ts = _template_store(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "请求体解析失败")
        updated = ts.update_template(
            template_id,
            title=body.get("title"),
            content=body.get("content"),
            language=body.get("language"),
            platform=body.get("platform"),
            scene=body.get("scene"),
            is_active=body.get("is_active"),
        )
        if not updated:
            raise HTTPException(404, "模板不存在")
        return {"ok": True, "id": template_id}

    @app.delete("/api/reply-templates/{template_id}")
    async def api_templates_delete(request: Request, template_id: str):
        """I3：软删除模板（主管专属）。"""
        api_auth(request)
        # 主管校验
        role = request.scope.get("session", {}).get("role", "")
        if role not in {"master", "admin"}:
            try:
                role = request.session.get("role", "")
            except Exception:
                role = ""
        if role not in {"master", "admin"}:
            raise HTTPException(403, "删除模板需要主管权限")
        ts = _template_store(request)
        deleted = ts.delete_template(template_id)
        if not deleted:
            raise HTTPException(404, "模板不存在")
        return {"ok": True, "id": template_id, "deleted": True}

    @app.post("/api/reply-templates/{template_id}/use")
    async def api_templates_use(request: Request, template_id: str):
        """I3：记录模板使用（用量统计，best-effort）。"""
        api_auth(request)
        ts = _template_store(request)
        ts.increment_template_usage(template_id)
        return {"ok": True, "id": template_id}
