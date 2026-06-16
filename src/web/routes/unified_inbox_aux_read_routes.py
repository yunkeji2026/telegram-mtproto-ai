"""统一收件箱——辅助读路径路由域（巨石拆分 slice 37a）。

把 ``register_unified_inbox_routes`` 巨型闭包中物理分离但逻辑内聚的三端点外移为
``register_aux_read_routes(app, *, api_auth, config_manager=None)``，由主 register
在**原位置**调用：

- ``unified-inbox/templates``：快捷回复模板（workspace + messenger + templates.yaml）
- ``unified-inbox/search-messages``：跨会话消息全文检索（SQLite LIKE）
- ``unified-inbox/kb-search``：KB 内联检索 + 平台/意图 context re-ranking

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 37a 端点契约断言）。

依赖全部朝下：context._collect_quick_templates、services._inbox_store。
kb-search 读 app.state.kb_store（handler 内）。收 api_auth + config_manager（templates 需读 yaml）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import Request

from src.web.routes.unified_inbox_context import _collect_quick_templates
from src.web.routes.unified_inbox_services import _inbox_store

logger = logging.getLogger(__name__)


def _rerank_kb_entries(
    raw_entries: List[Dict[str, Any]],
    *,
    platform: str,
    intent: str,
    limit: int,
    result: Dict[str, Any],
    is_auto: bool,
) -> List[Dict[str, Any]]:
    """Phase 17 context re-ranking：平台/意图加权 + 截取 top-k。"""
    plat_ctx = str(platform or "").lower()
    intent_ctx = str(intent or "").lower()
    scored: List[tuple] = []
    for row in raw_entries:
        base = float(row.get("_score") or 0.5)
        boost = 0.0
        row_plat = str(row.get("platform") or "").lower()
        if row_plat and plat_ctx and row_plat == plat_ctx:
            boost += 0.15
        row_kws = " ".join([
            str(row.get("category") or ""),
            str(row.get("keywords") or ""),
            str(row.get("scenario") or ""),
            str(row.get("title") or ""),
        ]).lower()
        if intent_ctx and intent_ctx in row_kws:
            boost += 0.10
        if row.get("example_reply_zh"):
            boost += 0.05
        scored.append((base + boost, row))

    scored.sort(key=lambda x: -x[0])
    entries: List[Dict[str, Any]] = []
    for score, row in scored[:limit]:
        answer = (
            row.get("example_reply_zh")
            or row.get("example_reply")
            or row.get("steps")
            or ""
        )
        entries.append({
            "entry_id": row.get("id") or row.get("entry_id") or "",
            "title": row.get("title") or row.get("scenario") or "",
            "answer": str(answer).strip(),
            "category": row.get("category") or "",
            "score": round(score, 3),
            "search_mode": row.get("_mode") or result.get("search_mode"),
            "auto": is_auto,
        })
    return entries


def register_aux_read_routes(app, *, api_auth, config_manager=None) -> None:
    """挂载快捷模板 / 消息搜索 / KB 检索端点。"""

    @app.get("/api/unified-inbox/templates")
    async def api_unified_inbox_templates(request: Request):
        """快捷回复模板（workspace + messenger approval + templates.yaml）。"""
        api_auth(request)
        tpls = _collect_quick_templates(config_manager)
        return {"ok": True, "templates": tpls, "count": len(tpls)}

    @app.get("/api/unified-inbox/search-messages")
    async def api_unified_inbox_search_messages(
        request: Request,
        q: str = "",
        limit: int = 20,
        platform: str = "",
    ):
        """Phase 21：跨会话消息全文检索（SQLite LIKE），供坐席工作台搜索消息内容。

        返回：[{message_id, conversation_id, text, ts, direction, platform, display_name}]
        """
        api_auth(request)
        query = str(q or "").strip()
        if not query or len(query) < 2:
            return {"ok": True, "results": [], "q": query}
        limit = max(1, min(50, int(limit or 20)))
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "results": [], "error": "inbox_store 不可用"}
        try:
            hits = store.search_messages(query, limit=limit, platform=str(platform or ""))
        except Exception:
            logger.debug("search_messages 失败", exc_info=True)
            return {"ok": False, "results": [], "error": "search_failed"}
        results = []
        for r in hits:
            cid = str(r.get("conversation_id") or "")
            txt = str(r.get("text") or "")
            results.append({
                "message_id": str(r.get("message_id") or ""),
                "conversation_id": cid,
                "text": txt,
                "text_snippet": txt[:120] + ("…" if len(txt) > 120 else ""),
                "ts": r.get("ts") or 0,
                "direction": r.get("direction") or "in",
                "platform": str(r.get("platform") or ""),
                "display_name": str(r.get("display_name") or ""),
            })
        return {"ok": True, "results": results, "q": query, "count": len(results)}

    @app.get("/api/unified-inbox/kb-search")
    async def api_unified_inbox_kb_search(
        request: Request,
        q: str = "",
        limit: int = 5,
        platform: str = "",
        intent: str = "",
        auto: str = "",
    ):
        """KB 内联检索：坐席在工作台快速查话术/知识条目。

        新增参数（Phase 17）：
          platform  — 当前会话平台，用于 platform 字段加权
          intent    — 当前会话意图（AI 分析结果），用于 category/keyword 加权
          auto=1    — 自动触发模式，limit 降为 3，只返回高置信条目
        """
        api_auth(request)
        query = str(q or "").strip()
        is_auto = str(auto or "").lower() in ("1", "true", "yes")
        if is_auto:
            limit = max(1, min(4, int(limit or 3)))
        else:
            limit = max(1, min(10, int(limit or 5)))
        kb = getattr(request.app.state, "kb_store", None)
        if kb is None:
            return {"ok": False, "entries": [], "error": "kb_unavailable"}
        if not query:
            return {"ok": True, "entries": [], "search_mode": "none"}
        fetch_k = min(limit * 3, 20)
        try:
            result = kb.search(query, top_k=fetch_k)
        except Exception:
            logger.debug("kb-search 失败", exc_info=True)
            return {"ok": False, "entries": [], "error": "search_failed"}
        raw_entries: List[Dict[str, Any]] = result.get("entries") or []
        plat_ctx = str(platform or "").lower()
        intent_ctx = str(intent or "").lower()
        entries = _rerank_kb_entries(
            raw_entries,
            platform=platform,
            intent=intent,
            limit=limit,
            result=result,
            is_auto=is_auto,
        )
        return {
            "ok": True,
            "entries": entries,
            "search_mode": result.get("search_mode") or "bm25",
            "context_reranked": bool(plat_ctx or intent_ctx),
        }
