"""统一收件箱——QA 质检评分 / 流失预警 · 活跃热力图路由域（巨石拆分 slice 22）。

把 ``register_unified_inbox_routes`` 巨型闭包**尾部**两段相邻子域整体外移为
``register_qa_churn_routes(app, *, api_auth)``，由主 register 在**原位置**调用：

- Phase 34 QA 质检评分：``conv/{id}/qa-score`` (GET/POST) + ``agent-qa-stats``
- Phase 35 流失预警 / 活跃热力图：``churn-risks`` + ``activity-heatmap``

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 22 端点契约断言）。

依赖全部朝下：services._inbox_store；ChurnPredictor 为 handler 内局部 import。只收
api_auth 一个参数（零闭包私有依赖）。
"""

from __future__ import annotations

import logging

from fastapi import Request

from src.web.routes.unified_inbox_services import _inbox_store
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)


def register_qa_churn_routes(app, *, api_auth) -> None:
    """挂载 QA 质检评分 + 流失预警 + 活跃热力图端点。"""

    # ─── Phase 34: QA 质检评分 ───────────────────────────────────────────

    @app.get("/api/workspace/conv/{conversation_id}/qa-score")
    async def api_conv_qa_score_get(conversation_id: str, request: Request):
        """Y1：读取已存储的质检评分（不触发重新计算）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        result = store.get_qa_score(conversation_id)
        if result is None:
            return {"ok": True, "conversation_id": conversation_id, "qa": None, "computed": False}
        return {"ok": True, "conversation_id": conversation_id, "qa": result, "computed": True}

    @app.post("/api/workspace/conv/{conversation_id}/qa-score")
    async def api_conv_qa_score_compute(conversation_id: str, request: Request):
        """Y1：立即计算并存储质检评分（可在归档/关闭时调用）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        result = store.compute_and_store_qa_score(conversation_id)
        return {"ok": True, "conversation_id": conversation_id, "qa": result}

    @app.get("/api/workspace/agent-qa-stats")
    async def api_agent_qa_stats(request: Request, days: int = 30):
        """Y1：聚合各坐席最近 N 天的质检评分统计（团队看板）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "agents": []}
        days = max(1, min(90, int(days or 30)))
        stats = store.batch_agent_qa_stats(days=days)
        return {"ok": True, "days": days, "agents": stats, "count": len(stats)}

    # ─── Phase 35: 流失预警 ──────────────────────────────────────────────

    @app.get("/api/workspace/churn-risks")
    async def api_churn_risks(
        request: Request,
        silence_days: int = 7,
        limit: int = 50,
    ):
        """Z1：返回高/中流失风险会话列表（按风险分降序）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "items": []}
        from src.inbox.churn_predictor import ChurnPredictor

        silence_days = max(1, min(60, int(silence_days or 7)))
        limit = max(1, min(200, int(limit or 50)))

        # 拉取候选（沉默时间 ≥ silence_days 的未归档会话）
        candidates = store.list_churn_risk_conversations(
            silence_days=silence_days, limit=limit * 3
        )

        # 补全 last_dir（用于判断末条是否入站）
        if candidates:
            cids = [c["conversation_id"] for c in candidates if c.get("conversation_id")]
            last_dirs = store.last_message_dirs(cids)
            for c in candidates:
                cid = c["conversation_id"]
                info = last_dirs.get(cid, {})
                c["last_dir"] = info.get("direction", "in")
                c["last_text"] = info.get("text", "")

        results = ChurnPredictor().batch_predict(candidates, silence_threshold_days=silence_days)
        results = results[:limit]

        # 持久化高风险结果到 conversation_meta
        for r in results:
            if r["risk_level"] == "high":
                try:
                    store.store_churn_risk(
                        r["conversation_id"], r["risk_level"], r["reasons"]
                    )
                except Exception:
                    pass

        return {
            "ok": True,
            "silence_days": silence_days,
            "items": results,
            "high_count": sum(1 for r in results if r["risk_level"] == "high"),
            "medium_count": sum(1 for r in results if r["risk_level"] == "medium"),
        }

    @app.get("/api/workspace/activity-heatmap")
    async def api_workspace_activity_heatmap(
        request: Request,
        days: int = 30,
        platform: str = "",
        direction: str = "inbound",
    ):
        """W1：获取最近 N 天消息量按星期×小时的分布矩阵（用于热力图可视化）。"""
        api_auth(request)
        store = _inbox_store(request)
        if store is None:
            return {"ok": False, "error": tr(request, "err.svc.inbox_not_ready")}
        days = max(1, min(365, int(days or 30)))
        data = store.activity_heatmap(days=days, platform=str(platform or ""), direction=str(direction or "inbound"))
        return {"ok": True, **data}
