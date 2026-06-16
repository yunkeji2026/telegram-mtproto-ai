"""C1-2 试用/Demo 模式：示例数据铺设 / 清空 / 状态 API。

- ``GET  /api/admin/demo``       —— demo 数据现状（present + 计数）。
- ``POST /api/admin/demo/seed``  —— 一键铺示例数据（跨多渠道/多坐席/多处置）。
- ``POST /api/admin/demo/clear`` —— 一键清空（按 demo: 命名空间，绝不碰真实数据）。
"""

from __future__ import annotations

import logging

from fastapi import Request

logger = logging.getLogger(__name__)


def _inbox(request: Request):
    return getattr(request.app.state, "inbox_store", None)


def _kb(config_manager):
    try:
        from src.utils.kb_registry import get_kb_store
        return get_kb_store(config_manager, require_exists=False)
    except Exception:
        logger.debug("demo KB store 取用失败（已忽略）", exc_info=True)
        return None


def register_demo_routes(app, *, api_auth, config_manager=None) -> None:
    @app.get("/api/admin/demo")
    async def api_admin_demo_status(request: Request):
        api_auth(request)
        from src.utils.demo_seeder import demo_status
        st = demo_status(_inbox(request))
        st["ok"] = True
        return st

    @app.post("/api/admin/demo/seed")
    async def api_admin_demo_seed(request: Request):
        api_auth(request)
        from src.utils.demo_seeder import seed_demo
        try:
            body = await request.json()
        except Exception:
            body = {}
        days = int((body or {}).get("days") or 14)
        return seed_demo(_inbox(request), days=days,
                         kb_store=_kb(config_manager), config_manager=config_manager)

    @app.post("/api/admin/demo/clear")
    async def api_admin_demo_clear(request: Request):
        api_auth(request)
        from src.utils.demo_seeder import clear_demo
        return clear_demo(_inbox(request))
