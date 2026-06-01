"""KB AI 自动化运行时 — 翻译扫描 / 知识库演化 / 自愈（Phase E1 批 5G）。

整组搬迁自 admin.py 的「KB AI 自动化」缠绕簇：locks + run_* + 3 个手动触发端点
+ 3 个后台循环。循环不在此自启动，而是 stash 到 app.state.kb_ai_loops，由 admin
的 startup 处理器统一 create_task（与 weekly_report / watcher 等其它后台任务并列）。

AI 调用复用 src/web/kb_ai_helpers 的纯函数（批 5F 已解耦），不再依赖 admin 闭包。

端点（与抽出前逐行一致）：
  POST /api/kb/translate-sweep   POST /api/kb/evolve-sweep   POST /api/kb/self-heal
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import Request

from src.web.kb_ai_helpers import ai_translate_entry, auto_fill_entry

logger = logging.getLogger(__name__)


def register_kb_ai_routes(app, ctx):
    config_manager = ctx.config_manager
    _kb_store = ctx.kb_store
    audit_store = ctx.audit_store
    _api_auth = ctx.api_auth

    # ── K2: 翻译扫描 ────────────────────────────────────────
    _translate_sweep_lock = asyncio.Lock()

    async def _run_translate_sweep(max_items: int = 5) -> dict:
        """后台扫描并处理翻译请求（去重+限速）"""
        async with _translate_sweep_lock:
            pending = _kb_store.get_pending_translate_requests(limit=max_items)
            if not pending:
                return {"processed": 0, "success": 0, "failed": 0}
            success = 0
            failed = 0
            for req in pending:
                entry = _kb_store.get_entry(req["entry_id"])
                if not entry:
                    _kb_store.delete_miss_entry(req["query"])
                    continue
                try:
                    results = await ai_translate_entry(config_manager, entry, [req["lang"]])
                    if results.get(req["lang"]):
                        _kb_store.upsert_translation(
                            req["entry_id"], req["lang"],
                            results[req["lang"]], auto=True
                        )
                        success += 1
                    else:
                        failed += 1
                except Exception as _te:
                    logger.warning("K2 自动翻译失败 entry=%s lang=%s: %s",
                                   req["entry_id"], req["lang"], _te)
                    failed += 1
                _kb_store.delete_miss_entry(req["query"])
            if success:
                logger.info("K2 自动翻译完成: %d 成功, %d 失败", success, failed)
            return {"processed": len(pending), "success": success, "failed": failed}

    @app.post("/api/kb/translate-sweep")
    async def api_kb_translate_sweep(request: Request):
        """手动触发翻译扫描"""
        _api_auth(request)
        result = await _run_translate_sweep(max_items=10)
        return {"ok": True, **result}

    # K2: 后台定时翻译扫描（每 10 分钟执行一次）
    async def _translate_sweep_loop():
        await asyncio.sleep(60)  # 启动后等 60 秒再开始
        while True:
            try:
                await _run_translate_sweep(max_items=3)
            except Exception as _e:
                logger.debug("K2 翻译扫描循环异常: %s", _e)
            await asyncio.sleep(600)  # 每 10 分钟

    # ── J2: 知识库自动演化 — 从 top_misses 自动创建草稿条目 ──
    _kb_evolve_lock = asyncio.Lock()

    async def _run_kb_evolve(max_items: int = 3) -> dict:
        """扫描高频 miss，创建禁用状态的草稿条目 + AI 自动填充"""
        async with _kb_evolve_lock:
            misses = _kb_store.get_miss_stats(top_k=20)
            # 过滤掉翻译请求和低频 miss
            candidates = [
                m for m in misses
                if not m["query"].startswith("[TRANSLATE:")
                and m["cnt"] >= 3
            ][:max_items]
            if not candidates:
                return {"processed": 0, "created": 0}
            created = 0
            for m in candidates:
                query = m["query"]
                # 检查是否已有相似条目（避免重复创建）
                existing = _kb_store.search(query, top_k=1)
                if existing.get("entries") and existing["entries"][0].get("score", 0) > 0.5:
                    continue
                cat = _kb_store._guess_category(query) if hasattr(_kb_store, "_guess_category") else "其他"
                title = query[:50]
                entry_id = _kb_store.add_entry({
                    "category": cat,
                    "title": title,
                    "triggers": [query],
                    "scenario": f"用户高频询问: {query}",
                    "steps": "",
                    "principles": "",
                    "example_reply_zh": "",
                    "enabled": 0,  # 草稿状态，需运营审核
                })
                _kb_store.delete_miss_entry(query)
                # L1: 触发 AI 自动填充
                try:
                    asyncio.create_task(
                        auto_fill_entry(config_manager, _kb_store, entry_id, title, cat,
                                        source_query=query))
                except Exception:
                    pass
                created += 1
                logger.info("J2 自动创建草稿条目: query='%s' entry=%s", query[:50], entry_id)
            return {"processed": len(candidates), "created": created}

    @app.post("/api/kb/evolve-sweep")
    async def api_kb_evolve_sweep(request: Request):
        """手动触发知识库自动演化"""
        _api_auth(request)
        result = await _run_kb_evolve(max_items=10)
        return {"ok": True, **result}

    # J2: 后台定时知识库演化（每 6 小时执行一次）
    async def _kb_evolve_loop():
        await asyncio.sleep(300)  # 启动后等 5 分钟
        while True:
            try:
                await _run_kb_evolve(max_items=5)
            except Exception as _e:
                logger.debug("J2 知识库演化循环异常: %s", _e)
            await asyncio.sleep(21600)  # 每 6 小时

    # ── H2: 知识库自愈 ──────────────────────────────────────
    @app.post("/api/kb/self-heal")
    async def api_kb_self_heal(request: Request):
        """手动触发知识库自愈巡检"""
        _api_auth(request)
        result = _kb_store.run_self_heal(stale_days=14)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_self_heal", "",
                            "", f"expanded={result['triggers_expanded']} "
                                f"archived={result['entries_archived']}")
        return {"ok": True, **result}

    # H2: 后台定时自愈（每 12 小时执行一次）
    async def _kb_self_heal_loop():
        await asyncio.sleep(600)  # 启动后等 10 分钟
        while True:
            try:
                result = _kb_store.run_self_heal(stale_days=14)
                if result["triggers_expanded"] or result["entries_archived"]:
                    logger.info("H2 自愈完成: expanded=%d archived=%d overloaded=%d",
                                result["triggers_expanded"],
                                result["entries_archived"],
                                result["overloaded_flagged"])
            except Exception as _e:
                logger.debug("H2 自愈循环异常: %s", _e)
            await asyncio.sleep(43200)  # 每 12 小时

    # 后台循环不在此自启动：stash 到 app.state，由 admin startup 统一 create_task
    app.state.kb_ai_loops = [
        _translate_sweep_loop, _kb_evolve_loop, _kb_self_heal_loop,
    ]
