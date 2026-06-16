"""统一收件箱——代理池 / 指纹路由域（巨石拆分 slice 8：路由域试切第一刀）。

把自包含、仅依赖 ``api_auth`` 的代理池（``/api/proxies*``）与指纹
（``/api/fingerprints*``）端点，从 ``register_unified_inbox_routes`` 的巨型闭包中
抽出，封装为 ``register_proxy_fingerprint_routes(app, *, api_auth)``，由主 register
顺序调用。端点路径/方法/响应零变化（由 admin_route_inventory URL 契约守卫保证）。

这是路由域拆分的**模式验证刀**：子注册函数只接收自身真正需要的依赖（此处仅
``api_auth``），其余服务（proxy_pool / fingerprint_store）由本模块自行 import，
不再依赖 routes 的模块级符号，故无循环 import。
"""

from __future__ import annotations

from fastapi import Request

from src.integrations.fingerprint import get_fingerprint_store, summarize as fp_summarize
from src.integrations.proxy_pool import get_proxy_pool


def register_proxy_fingerprint_routes(app, *, api_auth) -> None:
    """挂载代理池 + 指纹相关端点（M4：用户自填，一号一代理 / 一号一指纹）。"""

    # ── 代理池（M4：用户自填，一号一代理） ──────────────────────────────────
    @app.get("/api/proxies")
    async def api_proxies_list(request: Request):
        api_auth(request)
        return {"ok": True, "proxies": get_proxy_pool().list()}

    @app.post("/api/proxies")
    async def api_proxies_add(request: Request):
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            entry = get_proxy_pool().add(
                scheme=str((body or {}).get("scheme") or "socks5"),
                host=str((body or {}).get("host") or "").strip(),
                port=int((body or {}).get("port") or 0),
                username=str((body or {}).get("username") or ""),
                password=str((body or {}).get("password") or ""),
                label=str((body or {}).get("label") or ""),
            )
            return {"ok": True, "proxy": entry}
        except (ValueError, TypeError) as ex:
            return {"ok": False, "detail": str(ex)}

    @app.delete("/api/proxies/{proxy_id}")
    async def api_proxies_remove(proxy_id: str, request: Request):
        api_auth(request)
        get_proxy_pool().remove(proxy_id)
        return {"ok": True}

    @app.post("/api/proxies/{proxy_id}/test")
    async def api_proxies_test(proxy_id: str, request: Request):
        api_auth(request)
        ok = await get_proxy_pool().test(proxy_id)
        return {"ok": True, "reachable": ok,
                "status": "ok" if ok else "fail"}

    # ── 指纹（M4：自研，一号一指纹） ────────────────────────────────────────
    @app.get("/api/fingerprints")
    async def api_fingerprints_list(request: Request):
        api_auth(request)
        items = get_fingerprint_store().list()
        for it in items:
            it["summary"] = fp_summarize(it.get("profile") or {})
        return {"ok": True, "fingerprints": items}

    @app.post("/api/fingerprints/generate")
    async def api_fingerprints_generate(request: Request):
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        fp = get_fingerprint_store().create(
            seed=str((body or {}).get("seed") or "") or None,
            label=str((body or {}).get("label") or ""),
        )
        fp["summary"] = fp_summarize(fp.get("profile") or {})
        return {"ok": True, **fp}
