"""运营 Copilot + 测试纠错 API（Phase E1 续拆，从 admin.py 抽出）。

端点（与抽出前逐行一致）：
  POST /api/chat/test/correct   运营纠正 AI 回复 → 负反馈 + KB 优质示例
  POST /api/copilot/query       自然语言查询内部数据，AI 生成回答

依赖经 AdminRouteContext 注入（telegram_client/api_auth/kb_store/audit_store）。
copilot 内的 AI 生成走 skill_manager.ai_client（经 ctx_store 取 sm），无需直接 ai_client。
chat/test 主端点因依赖 _get_test_session 闭包仍留 admin.py（后续 PR）。
"""

from __future__ import annotations

import json
import time

from fastapi import HTTPException, Request


def register_copilot_routes(app, ctx) -> None:
    telegram_client = ctx.telegram_client
    _api_auth = ctx.api_auth
    _kb_store = ctx.kb_store
    audit_store = ctx.audit_store

    @app.post("/api/chat/test/correct")
    async def api_chat_test_correct(request: Request):
        """
        G2: 运营人员纠正 AI 回复。同时：
        1. 创建负面反馈记录（AI 原始回复 + 纠正文本）
        2. 保存正确回复为 KB 优质示例
        """
        _api_auth(request)
        data = await request.json()
        user_message = (data.get("user_message") or "").strip()
        wrong_reply = (data.get("wrong_reply") or "").strip()
        correct_reply = (data.get("correct_reply") or "").strip()
        category = data.get("category", "其他")
        if not user_message or not correct_reply:
            raise HTTPException(400, "user_message 和 correct_reply 不能为空")

        actor = request.session.get("username", "web_admin")

        fb_id = _kb_store.add_feedback({
            "user_message": user_message,
            "ai_reply": wrong_reply,
            "score": -1,
            "correction": correct_reply,
            "operator": actor,
        })

        ex_id = _kb_store.add_example({
            "category": category,
            "user_message": user_message,
            "correct_reply": correct_reply,
            "language": "zh",
            "quality": 1,
            "source": "test_correction",
        })

        if audit_store:
            audit_store.log(actor, "chat_test_correct", ex_id,
                            user_message[:80], correct_reply[:80])

        return {
            "ok": True,
            "feedback_id": fb_id,
            "example_id": ex_id,
        }

    # H4: 运营 Copilot — 自然语言查询内部数据
    def _copilot_get_ctx_store():
        """统一获取 context_store 实例"""
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm:
                return getattr(sm, "_context_store", None), sm
        return None, None

    @app.post("/api/copilot/query")
    async def api_copilot_query(request: Request):
        """
        H4: 接收运营人员的自然语言问题，
        自动调用内部 API 数据源，用 AI 生成回答。
        """
        _api_auth(request)
        data = await request.json()
        question = (data.get("question") or "").strip()
        if not question:
            raise HTTPException(400, "question 不能为空")

        t0 = time.time()
        gathered = {}
        ctx_store, sm = _copilot_get_ctx_store()

        q_lower = question.lower()
        _need_kb = any(k in q_lower for k in (
            "知识库", "kb", "条目", "命中", "miss", "健康", "触发词",
            "弱命中", "未命中", "翻译", "草稿"))
        _need_risk = any(k in q_lower for k in (
            "风险", "at_risk", "满意度", "不满", "流失", "投诉",
            "升级", "escalat", "case"))
        _need_conv = any(k in q_lower for k in (
            "对话", "会话", "活跃", "在线", "conversation", "用户数", "消息"))
        _need_report = any(k in q_lower for k in (
            "日报", "报告", "report", "统计", "概况", "总结", "今天", "昨天"))
        _need_strategy = any(k in q_lower for k in (
            "策略", "strategy", "ab", "a/b", "测试", "温度", "模型", "参数"))
        _need_feedback = any(k in q_lower for k in (
            "反馈", "feedback", "评分", "质量", "好评", "差评"))

        if not any([_need_kb, _need_risk, _need_conv, _need_strategy, _need_feedback]):
            _need_report = True

        # ── 数据采集 ──
        if _need_kb:
            try:
                stats = _kb_store.stats()
                weak = _kb_store.get_weak_hits(top_k=5)
                miss = _kb_store.get_miss_stats(top_k=5)
                stale = _kb_store.get_stale_entries(days=14)
                gathered["kb"] = {
                    "stats": stats,
                    "top_weak_hits": [{"query": w["query"], "count": w["count"],
                                       "avg_score": w["avg_score"]} for w in weak[:5]],
                    "top_misses": [{"query": m["query"], "count": m["cnt"]} for m in miss[:5]],
                    "stale_count": len(stale),
                }
            except Exception as _e:
                gathered["kb_error"] = str(_e)

        if _need_risk and ctx_store:
            try:
                at_risk = []
                for uid, c in ctx_store._cache.items():
                    profile = c.get("_user_profile")
                    if isinstance(profile, dict) and profile.get("at_risk"):
                        at_risk.append({
                            "user_id": uid,
                            "satisfaction": profile.get("satisfaction", 0),
                            "intent": c.get("current_intent", ""),
                            "consecutive": c.get("_consecutive_same_intent", 0),
                            "case_id": c.get("_case_id", ""),
                        })
                at_risk.sort(key=lambda x: x["satisfaction"])
                gathered["at_risk_users"] = at_risk[:10]
                gathered["at_risk_total"] = len(at_risk)
            except Exception as _e:
                gathered["risk_error"] = str(_e)

        if _need_conv and ctx_store:
            try:
                now = time.time()
                active_30 = active_60 = 0
                for uid, c in ctx_store._cache.items():
                    lrt = c.get("last_reply_time", 0)
                    if lrt >= now - 1800:
                        active_30 += 1
                    if lrt >= now - 3600:
                        active_60 += 1
                gathered["conversations"] = {
                    "active_30min": active_30,
                    "active_60min": active_60,
                    "total_cached": len(ctx_store._cache),
                }
            except Exception as _e:
                gathered["conv_error"] = str(_e)

        if _need_strategy and sm:
            try:
                if hasattr(sm, "_strategies"):
                    gathered["strategies"] = {
                        sid: {k: v for k, v in s.items()
                              if k in ("temperature", "max_tokens", "model",
                                       "thinking_budget", "reply_probability")}
                        for sid, s in sm._strategies.items()
                    }
            except Exception as _e:
                gathered["strategy_error"] = str(_e)

        if _need_feedback:
            try:
                with _kb_store._conn() as c:
                    since = time.time() - 86400 * 7
                    fb_rows = c.execute(
                        "SELECT score, COUNT(*) as cnt FROM kb_feedback "
                        "WHERE created_at >= datetime(?, 'unixepoch') GROUP BY score",
                        (since,)
                    ).fetchall()
                    gathered["feedback_7d"] = {
                        str(r["score"]): r["cnt"] for r in fb_rows
                    }
            except Exception as _e:
                gathered["feedback_error"] = str(_e)

        if _need_report:
            try:
                with _kb_store._conn() as c:
                    since = time.time() - 86400
                    total = c.execute(
                        "SELECT COUNT(*) FROM kb_query_log WHERE ts >= ?", (since,)
                    ).fetchone()[0]
                    hits = c.execute(
                        "SELECT COUNT(*) FROM kb_query_log WHERE ts >= ? AND hit=1", (since,)
                    ).fetchone()[0]
                    gathered["daily_summary"] = {
                        "kb_queries_24h": total,
                        "kb_hits_24h": hits,
                        "hit_rate": round(hits / max(total, 1) * 100, 1),
                    }
                if ctx_store:
                    now = time.time()
                    _a30 = sum(1 for c in ctx_store._cache.values()
                               if c.get("last_reply_time", 0) >= now - 1800)
                    _risk = sum(1 for c in ctx_store._cache.values()
                                if isinstance(c.get("_user_profile"), dict)
                                and c["_user_profile"].get("at_risk"))
                    gathered["daily_summary"]["active_users_30min"] = _a30
                    gathered["daily_summary"]["at_risk_users"] = _risk
            except Exception as _e:
                gathered["report_error"] = str(_e)

        # ── AI 生成自然语言回答 ──
        ai_answer = None
        try:
            if sm and hasattr(sm, "ai_client"):
                data_text = json.dumps(gathered, ensure_ascii=False, default=str)[:4000]
                copilot_prompt = (
                    "你是运营数据分析助手 Copilot。基于以下内部系统数据回答运营人员的问题。\n"
                    "回答要求：\n"
                    "- 用简洁的中文，突出关键数据点\n"
                    "- 有异常时主动指出并给出建议\n"
                    "- 数据不足时说明需要哪些额外信息\n"
                    "- 不要编造数据\n\n"
                    f"内部数据:\n{data_text}\n\n"
                    f"运营问题: {question}"
                )
                ai_answer = await sm.ai_client.generate_reply(
                    user_message=copilot_prompt,
                    context={"current_intent": "copilot_query", "kb_context": ""},
                    strategy_overrides={"temperature": 0.3, "max_tokens": 1024},
                )
        except Exception as _e:
            ai_answer = f"AI 生成回答失败: {_e}"

        total_ms = int((time.time() - t0) * 1000)
        return {
            "ok": True,
            "question": question,
            "answer": ai_answer or "暂无法生成回答",
            "data_sources": list(gathered.keys()),
            "raw_data": gathered,
            "total_ms": total_ms,
        }
