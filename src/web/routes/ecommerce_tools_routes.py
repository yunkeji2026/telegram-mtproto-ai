"""电商工具路由（Phase D）。

端点：
  GET  /api/tools/ecommerce/order     ?order_no= 或 ?text=   — 查订单（含物流）
  GET  /api/tools/ecommerce/track     ?tracking_no= 或 ?text= — 查物流
  POST /api/tools/ecommerce/resolve   {text}                  — 从文本自动抽号并查询

返回带 facts 字段（to_context_facts），供回复引擎做事实校验注入。
依赖 app.state.ecommerce_tools；未启用时返回 503。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from domains.ecommerce.hooks import extract_order_no, extract_tracking_no
from src.web.web_i18n import tr


def _get_tools(request: Request):
    svc = getattr(request.app.state, "ecommerce_tools", None)
    if svc is None:
        raise HTTPException(503, tr(request, "err.ec.tools_disabled"))
    return svc


def register_ecommerce_tools_routes(app, *, api_auth):
    @app.get("/api/tools/ecommerce/order")
    async def api_order(request: Request, order_no: str = "", text: str = "", _=Depends(api_auth)):
        svc = _get_tools(request)
        q = (order_no or "").strip() or extract_order_no(text)
        if not q:
            raise HTTPException(400, tr(request, "err.ec.no_order_no"))
        res = await svc.lookup_order(q)
        out = res.to_dict()
        out["facts"] = res.to_context_facts()
        return {"ok": res.ok, "result": out}

    @app.get("/api/tools/ecommerce/track")
    async def api_track(request: Request, tracking_no: str = "", text: str = "", _=Depends(api_auth)):
        svc = _get_tools(request)
        q = (tracking_no or "").strip() or extract_tracking_no(text)
        if not q:
            raise HTTPException(400, tr(request, "err.ec.no_tracking_no"))
        res = await svc.track_shipment(q)
        out = res.to_dict()
        out["facts"] = res.to_context_facts()
        return {"ok": res.ok, "result": out}

    @app.post("/api/tools/ecommerce/resolve")
    async def api_resolve(request: Request, _=Depends(api_auth)):
        """从一段客户消息里自动抽取订单号/物流号并查询（接 message_analysis.order_no 场景）。"""
        svc = _get_tools(request)
        body = await request.json()
        text = str(body.get("text") or "")
        order_no = str(body.get("order_no") or "").strip() or extract_order_no(text)
        tracking_no = str(body.get("tracking_no") or "").strip() or extract_tracking_no(text)
        results = []
        if order_no:
            r = await svc.lookup_order(order_no)
            d = r.to_dict()
            d["facts"] = r.to_context_facts()
            results.append(d)
        if tracking_no and tracking_no != order_no:
            r = await svc.track_shipment(tracking_no)
            d = r.to_dict()
            d["facts"] = r.to_context_facts()
            results.append(d)
        if not results:
            raise HTTPException(400, tr(request, "err.ec.no_id_recognized"))
        return {"ok": True, "count": len(results), "results": results}

    @app.get("/api/tools/ecommerce/cache_stats")
    async def api_cache_stats(request: Request, _=Depends(api_auth)):
        """缓存命中率快照（观测缓存收益）。"""
        svc = _get_tools(request)
        return {"ok": True, "stats": svc.cache_stats()}
