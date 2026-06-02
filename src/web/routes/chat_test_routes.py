"""全链路对话自测 API（Phase E1 续拆，从 admin.py 抽出）。

端点（与抽出前逐行一致）：
  POST /api/chat/test   H1 意图→策略→KB→AI 回复→画像；G1 通道模拟+SOP；F3 多轮会话

测试会话缓存（_test_sessions）为模块内闭包状态，register 仅调用一次 → 生命周期同 app，
与抽出前语义一致；不污染生产 ctx_store。依赖经 AdminRouteContext 注入。
"""

from __future__ import annotations

import time
import uuid
from typing import Dict

from fastapi import HTTPException, Request


def register_chat_test_routes(app, ctx) -> None:
    telegram_client = ctx.telegram_client
    _api_auth = ctx.api_auth
    _kb_store = ctx.kb_store
    domain_web_pages = ctx.domain_web_pages

    # F3: 测试会话缓存（内存级，不污染生产 ctx_store）
    _test_sessions: Dict[str, Dict] = {}
    _TEST_SESSION_TTL = 1800  # 30 分钟

    def _get_test_session(session_id: str) -> Dict:
        """获取或创建测试会话"""
        now = time.time()
        # 清理过期 session
        expired = [k for k, v in _test_sessions.items()
                   if now - v.get("_ts", 0) > _TEST_SESSION_TTL]
        for k in expired:
            _test_sessions.pop(k, None)
        if session_id and session_id in _test_sessions:
            _test_sessions[session_id]["_ts"] = now
            return _test_sessions[session_id]
        sid = session_id or str(uuid.uuid4())[:12]
        _test_sessions[sid] = {"_ts": now, "_sid": sid, "_history": [], "_turn": 0}
        return _test_sessions[sid]

    @app.post("/api/chat/test")
    async def api_chat_test(request: Request):
        """
        全链路自测端点：
        - H1: 意图识别 → 策略选择 → KB 搜索 → AI 回复 → 画像
        - G1: channel_overrides 模拟通道状态 + SOP 合规检查
        - F3: session_id 支持多轮对话（30分钟TTL，不影响生产数据）
        """
        _api_auth(request)
        data = await request.json()
        message = (data.get("message") or "").strip()
        user_id = data.get("user_id", "__test_user__")
        channel_overrides = data.get("channel_overrides")
        user_emotion = data.get("user_emotion", "")
        session_id = data.get("session_id", "")
        if not message:
            raise HTTPException(400, "message 不能为空")

        # F3: 获取/创建测试会话
        sess = _get_test_session(session_id)
        session_id = sess["_sid"]
        sess["_turn"] += 1

        t0 = time.time()
        trace = {"steps": []}

        def _step(name, detail):
            trace["steps"].append({
                "step": name,
                "detail": detail,
                "elapsed_ms": int((time.time() - t0) * 1000),
            })

        # 1. 意图识别
        sm = None
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
        if not sm:
            return {"ok": False, "error": "SkillManager 未初始化（Bot 未运行）"}

        intent = sm._recognize_intent(message)
        strategy, strategy_id = sm.get_strategy_for_intent(intent, user_id)
        _step("intent", {"recognized": intent, "strategy_id": strategy_id})

        # 2. KB 搜索
        kb_hit = False
        kb_context = ""
        kb_entries = []
        kb_score = 0.0
        result = {"search_mode": "bm25"}
        try:
            result = _kb_store.search(message, top_k=3, lang="zh")
            if result.get("entries"):
                kb_score = result["entries"][0].get("_score", 0)
                kb_entries = [
                    {"title": e.get("title", ""), "score": e.get("_score", 0),
                     "category": e.get("category", "")}
                    for e in result["entries"][:3]
                ]
            kb_context = _kb_store.build_ai_context_from_result(result, lang="zh")
            kb_hit = bool(kb_context)
        except Exception as _e:
            _step("kb_error", str(_e))
        _step("kb_search", {
            "hit": kb_hit, "top_score": round(kb_score, 3),
            "entries": kb_entries, "mode": result.get("search_mode", "bm25"),
        })

        channel_status_text = ""
        _has_ch_page = any(p.get("key") == "ch" for p in domain_web_pages)
        if _has_ch_page:
            if channel_overrides and isinstance(channel_overrides, dict):
                parts = [f"{k.upper()}: {v}" for k, v in channel_overrides.items()]
                channel_status_text = "，".join(parts)
                _step("channel_override", channel_overrides)
            elif intent in ("channel_info", "status_check"):
                channel_status_text = sm._get_live_channel_status()
                if channel_status_text:
                    _step("channel_live", channel_status_text)

        # 3. AI 回复
        ai_reply = None
        try:
            mock_ctx = {
                "user_id": user_id,
                "intent": intent,
                "current_intent": intent,
                "_reply_strategy": strategy or {},
            }
            # F3: 注入多轮对话历史
            if sess["_history"]:
                mock_ctx["_conversation_history"] = sess["_history"][-6:]
                _step("session", {"id": session_id, "turn": sess["_turn"],
                                  "history_rounds": len(sess["_history"]) // 2})
            if kb_context:
                mock_ctx["kb_context"] = kb_context
            if channel_status_text:
                mock_ctx["channel_status_info"] = channel_status_text
            if user_emotion:
                mock_ctx["user_emotion_hint"] = user_emotion
                mock_ctx["_user_profile"] = {"tone": user_emotion}
            so = {}
            for _sk in ("temperature", "max_tokens", "context_rounds", "model", "thinking_budget"):
                if _sk in (strategy or {}):
                    so[_sk] = strategy[_sk]
            ai_reply = await sm.ai_client.generate_reply_with_intent(
                user_message=message,
                intent=intent,
                user_context=mock_ctx,
                strategy_overrides=so or None,
            )
        except Exception as _e:
            _step("ai_error", str(_e))
        _step("ai_reply", {
            "reply": (ai_reply or "")[:500],
            "length": len(ai_reply or ""),
        })

        # 4. 画像快照
        profile = {}
        try:
            ctx_store = getattr(sm, "_context_store", None)
            if ctx_store and user_id in ctx_store._cache:
                profile = ctx_store._cache[user_id].get("_user_profile", {})
        except Exception:
            pass
        _step("profile", profile if profile else {"note": "测试用户无历史画像"})

        sop_check = None
        if _has_ch_page and channel_overrides and ai_reply:
            sop_check = {"passed": True, "warnings": []}
            reply_lower = ai_reply.lower()
            for ch_name, ch_status in channel_overrides.items():
                ch_up = ch_name.upper()
                status_lower = ch_status.lower()
                if "维护" in status_lower:
                    if ch_up.lower() not in reply_lower and "维护" not in reply_lower:
                        sop_check["passed"] = False
                        sop_check["warnings"].append(
                            f"{ch_up} 处于维护中，但回复未提及维护状态")
                elif "波动" in status_lower:
                    if "波动" not in reply_lower and "成功率" not in reply_lower and "偏低" not in reply_lower:
                        sop_check["warnings"].append(
                            f"{ch_up} 有波动，回复未明确提及波动/成功率风险")
            if sop_check["warnings"]:
                sop_check["passed"] = False
            _step("sop_check", sop_check)

        # F3: 将本轮加入会话历史
        if ai_reply:
            sess["_history"].append({"role": "user", "content": message[:200]})
            sess["_history"].append({"role": "assistant", "content": ai_reply[:300]})
            if len(sess["_history"]) > 20:
                sess["_history"] = sess["_history"][-12:]

        total_ms = int((time.time() - t0) * 1000)
        resp = {
            "ok": True,
            "message": message,
            "intent": intent,
            "strategy_id": strategy_id,
            "kb_hit": kb_hit,
            "kb_top_score": round(kb_score, 3),
            "reply": (ai_reply or ""),
            "total_ms": total_ms,
            "trace": trace,
            "session_id": session_id,
            "turn": sess["_turn"],
        }
        if sop_check is not None:
            resp["sop_check"] = sop_check
        return resp
