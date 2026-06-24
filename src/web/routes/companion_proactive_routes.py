"""陪伴主动话题调度·可观测预览 API（dry-run 观测，无副作用）。

端点：
  GET /api/companion/proactive/preview?limit=  本轮"会主动联系谁、引用哪条记忆、
      带哪些背景事实、是否被 care 去重让路"——不发送、不写冷却，即便功能未启用也可看。

预览能力由 main 在启动时挂到 ``app.state.companion_proactive_preview``（一个无参/可选
limit 的可调用对象）。未就绪（inbox/skill 未起）时该属性缺失，端点返回 available=false，
不报错——与本仓"子系统未就绪软降级"约定一致。
"""
from __future__ import annotations

import logging

from fastapi import Depends, Request

logger = logging.getLogger(__name__)


def register_companion_proactive_routes(app, *, api_auth) -> None:
    @app.get("/api/companion/proactive/preview")
    async def api_companion_proactive_preview(
        request: Request, limit: int = 50, _=Depends(api_auth),
    ):
        """主动话题本轮候选预览（dry-run）。不触发发送。"""
        fn = getattr(request.app.state, "companion_proactive_preview", None)
        if fn is None:
            return {
                "ok": True, "available": False, "enabled": False,
                "plans": [], "candidates": 0,
                "message": "主动话题预览未就绪（inbox/skill_manager 未起或未配置 companion）",
            }
        try:
            data = fn(limit=max(1, min(int(limit or 50), 200)))
        except Exception:
            logger.warning("companion proactive preview 失败", exc_info=True)
            return {"ok": False, "available": True, "plans": [],
                    "message": "预览计算失败"}
        out = {"ok": True, "available": True}
        out.update(data if isinstance(data, dict) else {})
        return out

    @app.post("/api/companion/proactive/sample")
    async def api_companion_proactive_sample(request: Request, _=Depends(api_auth)):
        """试发采样：对某会话生成 AI 实际会发的那句话，但**不发送**（开闸前先读文案）。

        body: ``{conversation_id}``。会真实调用一次 AI（有 token 成本），无发送/无写冷却。
        """
        fn = getattr(request.app.state, "companion_proactive_generate", None)
        if fn is None:
            return {"ok": True, "available": False, "generated": False,
                    "message": "试发未就绪（inbox/skill_manager 未起或 ai 未就绪）"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        cid = str((body or {}).get("conversation_id") or "").strip()
        if not cid:
            return {"ok": False, "generated": False, "message": "缺 conversation_id"}
        # Stage O：slot ∈ {morning,night} → 试发每日仪式问候（晨/晚安）；空 → 沉默回访开场。
        slot = str((body or {}).get("slot") or "").strip().lower()
        try:
            data = await fn(cid, slot) if slot else await fn(cid)
        except Exception:
            logger.warning("companion proactive sample 失败", exc_info=True)
            return {"ok": False, "generated": False, "message": "生成失败"}
        out = {"ok": True, "available": True}
        out.update(data if isinstance(data, dict) else {})
        return out

    def _sample_store(request: Request):
        return getattr(request.app.state, "companion_sample_store", None)

    @app.post("/api/companion/proactive/sample/{sid}/rate")
    async def api_companion_proactive_rate(sid: int, request: Request, _=Depends(api_auth)):
        """对一条试发采样评分（质量回流）。body: {rating: up|down, edited_text?, note?}。"""
        store = _sample_store(request)
        if store is None:
            return {"ok": False, "message": "评分存储未就绪"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        rating = str((body or {}).get("rating") or "").strip().lower()
        if rating not in ("up", "down"):
            return {"ok": False, "message": "rating 须为 up 或 down"}
        ok = store.rate(
            int(sid), rating,
            edited_text=str((body or {}).get("edited_text") or "")[:500],
            note=str((body or {}).get("note") or "")[:500],
        )
        if not ok:
            return {"ok": False, "message": "采样不存在或评分失败"}
        return {"ok": True, "rated": int(sid), "rating": rating}

    @app.get("/api/companion/proactive/samples")
    async def api_companion_proactive_samples(
        request: Request, limit: int = 50, rating: str = "", _=Depends(api_auth),
    ):
        """采样列表 + 聚合统计（好评率 / 按 mode 分），供调 prompt/阈值。"""
        store = _sample_store(request)
        if store is None:
            return {"ok": True, "available": False, "items": [], "stats": {}}
        items = store.list_recent(limit=max(1, min(int(limit or 50), 500)),
                                  rating=str(rating or ""))
        return {"ok": True, "available": True, "items": items, "stats": store.stats()}

    @app.get("/api/companion/proactive/tuning-advice")
    async def api_companion_proactive_tuning_advice(
        request: Request, _=Depends(api_auth),
    ):
        """基于采样评分给"调参建议"（只读、人审）：按 mode 好评率 + 针对性建议 +
        few-shot 候选（高赞文案 / 差评改写）。绝不自动改配置。"""
        from src.integrations.companion_sample_store import build_tuning_advice

        store = _sample_store(request)
        if store is None:
            return {"ok": True, "available": False, "advice": {}}
        rated = store.list_recent(limit=200, rating="up") \
            + store.list_recent(limit=200, rating="down")
        advice = build_tuning_advice(store.stats(), rated)
        return {"ok": True, "available": True, "advice": advice}


__all__ = ["register_companion_proactive_routes"]
