"""知识库路由（Phase E1 批 5 / 子批 A：条目 CRUD + 版本历史）。

复用 AdminRouteContext，新增 kb_store / fire_webhook 字段（延迟挂载：_kb_store
在 create_app ~2500 行才创建，故在其后 set 再 register）。

子批 A 端点（与抽出前逐行一致）：
  GET  /knowledge
  GET  /api/kb/entries            GET  /api/kb/entries/{entry_id}
  POST /api/kb/check-conflict     POST /api/kb/check-trigger-overlaps
  POST /api/kb/entries            PUT  /api/kb/entries/{entry_id}
  GET  /api/kb/entries/{entry_id}/versions
  GET  /api/kb/versions/{version_id}   POST /api/kb/versions/{version_id}/restore
  DELETE /api/kb/entries/{entry_id}

其余 kb 端点（error-codes/examples/rules/feedback/translate/embed/...）留待后续子批。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import List

from fastapi import BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from src.utils.kb_store import KB_CATEGORIES
from src.web.kb_ai_helpers import ai_translate_entry, auto_fill_entry

logger = logging.getLogger(__name__)


def register_kb_routes(app, ctx):
    from src.web.admin import templates

    _kb_store = ctx.kb_store
    config_manager = ctx.config_manager
    audit_store = ctx.audit_store
    _api_auth = ctx.api_auth
    _require_auth = ctx.require_auth
    _fire_webhook = ctx.fire_webhook

    def _run_kb_conflict_checkers(data: dict) -> list:
        """Run all registered KB conflict checkers from domain packs."""
        warnings = []
        for checker in getattr(app.state, "kb_conflict_checkers", []):
            try:
                result = checker(data)
                if result:
                    warnings.extend(result)
            except Exception:
                pass
        return warnings

    def _format_trigger_overlap_messages(overlaps: list) -> list:
        """将 find_trigger_overlaps 结果格式化为可读中文列表。"""
        lines = []
        for o in overlaps or []:
            st = "【已启用】" if o.get("other_enabled") else "【已停用】"
            cat = o.get("other_category") or ""
            cat_s = f"「{cat}」" if cat else ""
            if o.get("kind") == "exact":
                lines.append(
                    f"{st} 触发词「{o.get('my_trigger', '')}」与条目 {cat_s}"
                    f"《{o.get('other_title', '')}》（id={o.get('other_id', '')}）"
                    f"中的「{o.get('other_trigger', '')}」完全相同"
                )
            else:
                lines.append(
                    f"{st} 触发词「{o.get('my_trigger', '')}」与条目 {cat_s}"
                    f"《{o.get('other_title', '')}》（id={o.get('other_id', '')}）"
                    f"的「{o.get('other_trigger', '')}」存在包含关系，可能影响命中排序"
                )
        return lines

    @app.get("/knowledge", response_class=HTMLResponse)
    async def knowledge_page(request: Request):
        _require_auth(request)
        stats = _kb_store.stats()
        return templates.TemplateResponse(request, "knowledge.html", {
            "categories": KB_CATEGORIES,
            "stats": stats,
        })

    # ---------- 知识条目 ----------
    @app.get("/api/kb/entries")
    async def api_kb_list(
        request: Request,
        category: str = "",
        search: str = "",
        enabled_only: bool = False,
    ):
        _api_auth(request)
        entries = _kb_store.list_entries(category=category, enabled_only=enabled_only, search=search)
        for e in entries:
            try:
                e["triggers"] = json.loads(e.get("triggers", "[]"))
            except Exception:
                e["triggers"] = []
        return {"entries": entries, "total": len(entries)}

    @app.get("/api/kb/entries/{entry_id}")
    async def api_kb_get_entry(request: Request, entry_id: str):
        _api_auth(request)
        entry = _kb_store.get_entry(entry_id)
        if not entry:
            raise HTTPException(status_code=404)
        try:
            entry["triggers"] = json.loads(entry.get("triggers", "[]"))
        except Exception:
            entry["triggers"] = []
        try:
            entry["negative_triggers"] = json.loads(entry.get("negative_triggers", "[]"))
        except Exception:
            entry["negative_triggers"] = []
        return entry

    @app.post("/api/kb/check-conflict")
    async def api_kb_check_conflict(request: Request):
        """前端实时检测 KB 条目是否与域包数据冲突（由域包注册检测器）"""
        _api_auth(request)
        data = await request.json()
        warnings = _run_kb_conflict_checkers(data)
        return {"has_conflict": bool(warnings), "warnings": warnings}

    @app.post("/api/kb/check-trigger-overlaps")
    async def api_kb_check_trigger_overlaps(request: Request):
        """检测触发词与其他条目的重复 / 包含关系（保存前或编辑时调用）。"""
        _api_auth(request)
        data = await request.json()
        eid = (data.get("entry_id") or data.get("id") or "").strip() or None
        overlaps = _kb_store.find_trigger_overlaps(eid, data.get("triggers", []))
        msgs = _format_trigger_overlap_messages(overlaps)
        return {
            "has_overlap": bool(overlaps),
            "overlaps": overlaps,
            "overlap_messages": msgs,
        }

    @app.post("/api/kb/entries")
    async def api_kb_add_entry(request: Request):
        _api_auth(request)
        data = await request.json()
        overlaps = _kb_store.find_trigger_overlaps(None, data.get("triggers", []))
        if overlaps and not data.get("_force_save_triggers"):
            return {
                "ok": False,
                "trigger_overlap": True,
                "overlaps": overlaps,
                "overlap_messages": _format_trigger_overlap_messages(overlaps),
            }
        conflict_warnings = _run_kb_conflict_checkers(data)
        if conflict_warnings and not data.get("_force_save"):
            return {"ok": False, "conflict": True, "warnings": conflict_warnings}
        entry_id = _kb_store.add_entry(data)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_add_entry", entry_id)
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop().create_task(_fire_webhook(
                "kb_change", actor, data.get("title", entry_id),
                f"新增知识条目: {data.get('title', entry_id)}"
            ))
        except RuntimeError:
            pass
        return {"id": entry_id, "ok": True,
                "warnings": conflict_warnings if conflict_warnings else None}

    @app.put("/api/kb/entries/{entry_id}")
    async def api_kb_update_entry(request: Request, entry_id: str):
        _api_auth(request)
        data = await request.json()
        if "triggers" in data:
            overlaps = _kb_store.find_trigger_overlaps(entry_id, data.get("triggers", []))
            if overlaps and not data.get("_force_save_triggers"):
                return {
                    "ok": False,
                    "trigger_overlap": True,
                    "overlaps": overlaps,
                    "overlap_messages": _format_trigger_overlap_messages(overlaps),
                }
        conflict_warnings = _run_kb_conflict_checkers(data)
        if conflict_warnings and not data.get("_force_save"):
            return {"ok": False, "conflict": True, "warnings": conflict_warnings}
        actor = request.session.get("username", "web_admin")
        _kb_store.save_version(entry_id, editor=actor)
        ok = _kb_store.update_entry(entry_id, data)
        if audit_store:
            audit_store.log(actor, "kb_update_entry", entry_id)
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop().create_task(_fire_webhook(
                "kb_change", actor, data.get("title", entry_id),
                f"更新知识条目: {data.get('title', entry_id)}"
            ))
        except RuntimeError:
            pass
        return {"ok": ok,
                "warnings": conflict_warnings if conflict_warnings else None}

    # ---------- 版本历史 ----------
    @app.get("/api/kb/entries/{entry_id}/versions")
    async def api_kb_entry_versions(request: Request, entry_id: str):
        _api_auth(request)
        return {"versions": _kb_store.list_versions(entry_id)}

    @app.get("/api/kb/versions/{version_id}")
    async def api_kb_get_version(request: Request, version_id: str):
        _api_auth(request)
        ver = _kb_store.get_version(version_id)
        if not ver:
            raise HTTPException(status_code=404)
        return ver

    @app.post("/api/kb/versions/{version_id}/restore")
    async def api_kb_restore_version(request: Request, version_id: str):
        _api_auth(request)
        actor = request.session.get("username", "web_admin")
        ok = _kb_store.restore_version(version_id, editor=actor)
        if not ok:
            raise HTTPException(status_code=404)
        if audit_store:
            audit_store.log(actor, "kb_restore_version", version_id)
        return {"ok": True}

    @app.delete("/api/kb/entries/{entry_id}")
    async def api_kb_delete_entry(request: Request, entry_id: str):
        _api_auth(request)
        # 获取标题再删除，用于通知
        entry_before = _kb_store.get_entry(entry_id)
        title_before = (entry_before or {}).get("title", entry_id)
        _kb_store.delete_entry(entry_id)
        # 同步删除该条目关联的图片文件
        from pathlib import Path as _P
        img_names = _kb_store.delete_all_entry_images(entry_id)
        img_dir = _P(config_manager.config_path).parent / "kb_images"
        for fname in img_names:
            try:
                (img_dir / fname).unlink(missing_ok=True)
            except Exception:
                pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_delete_entry", entry_id)
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop().create_task(_fire_webhook(
                "kb_change", actor, title_before,
                f"删除知识条目: {title_before}"
            ))
        except RuntimeError:
            pass
        return {"ok": True}

    # ---------- 错误码 ----------
    @app.get("/api/kb/error-codes")
    async def api_kb_list_ec(request: Request):
        _api_auth(request)
        return {"error_codes": _kb_store.list_error_codes()}

    @app.post("/api/kb/error-codes")
    async def api_kb_add_ec(request: Request):
        _api_auth(request)
        data = await request.json()
        ec_id = _kb_store.add_error_code(data)
        return {"id": ec_id, "ok": True}

    @app.put("/api/kb/error-codes/{ec_id}")
    async def api_kb_update_ec(request: Request, ec_id: str):
        _api_auth(request)
        data = await request.json()
        ok = _kb_store.update_error_code(ec_id, data)
        return {"ok": ok}

    @app.delete("/api/kb/error-codes/{ec_id}")
    async def api_kb_delete_ec(request: Request, ec_id: str):
        _api_auth(request)
        _kb_store.delete_error_code(ec_id)
        return {"ok": True}

    # ---------- 对话示例 ----------
    @app.get("/api/kb/examples")
    async def api_kb_list_examples(request: Request, category: str = "", language: str = ""):
        _api_auth(request)
        return {"examples": _kb_store.list_examples(category=category, language=language)}

    @app.post("/api/kb/examples")
    async def api_kb_add_example(request: Request):
        _api_auth(request)
        data = await request.json()
        ex_id = _kb_store.add_example(data)
        return {"id": ex_id, "ok": True}

    @app.delete("/api/kb/examples/{ex_id}")
    async def api_kb_delete_example(request: Request, ex_id: str):
        _api_auth(request)
        _kb_store.delete_example(ex_id)
        return {"ok": True}

    # ---------- 硬规则 ----------
    @app.get("/api/kb/rules")
    async def api_kb_list_rules(request: Request):
        _api_auth(request)
        return {"rules": _kb_store.get_rules(enabled_only=False)}

    @app.post("/api/kb/rules")
    async def api_kb_add_rule(request: Request):
        _api_auth(request)
        data = await request.json()
        rule_id = _kb_store.add_rule(data)
        return {"id": rule_id, "ok": True}

    @app.delete("/api/kb/rules/{rule_id}")
    async def api_kb_delete_rule(request: Request, rule_id: str):
        _api_auth(request)
        _kb_store.delete_rule(rule_id)
        return {"ok": True}

    # ---------- 反馈 ----------
    @app.get("/api/kb/feedback")
    async def api_kb_list_feedback(request: Request, limit: int = 50):
        _api_auth(request)
        return {"feedback": _kb_store.list_feedback(limit=limit)}

    @app.post("/api/kb/feedback")
    async def api_kb_add_feedback(request: Request):
        # 不要求登录，bot 进程可直接调用
        data = await request.json()
        fb_id = _kb_store.add_feedback(data)
        return {"id": fb_id, "ok": True}

    @app.post("/api/kb/feedback/{fb_id}/promote")
    async def api_kb_promote_feedback(request: Request, fb_id: str):
        _api_auth(request)
        ok = _kb_store.promote_feedback_to_example(fb_id)
        return {"ok": ok}

    # ---------- 沙盒测试 ----------
    @app.post("/api/kb/sandbox")
    async def api_kb_sandbox(request: Request):
        _api_auth(request)
        data = await request.json()
        query = data.get("query", "")
        lang = data.get("lang", "zh")
        t0 = time.time()
        result = _kb_store.search(query, top_k=5, lang=lang)
        ai_context = _kb_store.build_ai_context_from_result(result, lang=lang)
        elapsed_ms = int((time.time() - t0) * 1000)
        for e in result.get("entries", []):
            try:
                e["triggers"] = json.loads(e.get("triggers", "[]"))
            except Exception:
                e["triggers"] = []
        return {
            "search_result": result,
            "ai_context": ai_context,
            "elapsed_ms": elapsed_ms,
            "search_mode": result.get("search_mode", "bm25"),
        }

    @app.post("/api/kb/sandbox/save-example")
    async def api_kb_sandbox_save_example(request: Request):
        """将沙盒对话另存为 KB 对话示例（高质量示例反哺知识库）"""
        _api_auth(request)
        data = await request.json()
        user_msg = (data.get("user_message") or "").strip()
        ai_reply  = (data.get("ai_reply") or "").strip()
        category  = data.get("category", "其他")
        lang      = data.get("lang", "zh")
        if not user_msg or not ai_reply:
            raise HTTPException(400, "user_message 和 ai_reply 不能为空")
        ex_id = _kb_store.add_example({
            "category": category,
            "user_message": user_msg,
            "correct_reply": ai_reply,
            "language": lang,
            "quality": 1,
            "source": "sandbox",
        })
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_sandbox_save_example", ex_id, user_msg[:80], ai_reply[:80])
        return {"ok": True, "id": ex_id}

    @app.get("/api/kb/category-stats")
    async def api_kb_category_stats(request: Request):
        """分类使用统计：每个分类的条目数、总使用次数、零使用条目数"""
        _api_auth(request)
        with _kb_store._conn() as c:
            rows = c.execute(
                "SELECT category, COUNT(*) as cnt, "
                "SUM(use_count) as total_use, "
                "SUM(CASE WHEN use_count=0 AND enabled=1 THEN 1 ELSE 0 END) as zero_use "
                "FROM kb_entries WHERE enabled=1 "
                "GROUP BY category ORDER BY total_use DESC"
            ).fetchall()
        return {"categories": [dict(r) for r in rows]}

    # ── 导出 / 导入（Phase 8）----------
    @app.get("/api/kb/export")
    async def api_kb_export(request: Request, fmt: str = "json"):
        """导出启用的知识库为 JSON（fmt=json）或 YAML（fmt=yaml，需 PyYAML）"""
        _api_auth(request)
        data = _kb_store.export_all()
        ts = time.strftime("%Y%m%d_%H%M%S")
        if fmt == "yaml":
            try:
                import yaml as _yaml
                content = _yaml.dump(data, allow_unicode=True,
                                     default_flow_style=False, sort_keys=False)
                return Response(
                    content, media_type="text/yaml",
                    headers={"Content-Disposition":
                             f'attachment; filename="kb_export_{ts}.yaml"'},
                )
            except ImportError:
                pass  # 降级为 JSON
        content = json.dumps(data, ensure_ascii=False, indent=2)
        return Response(
            content, media_type="application/json",
            headers={"Content-Disposition":
                     f'attachment; filename="kb_export_{ts}.json"'},
        )

    @app.post("/api/kb/import")
    async def api_kb_import(request: Request):
        """
        批量导入知识库。
        Body: {data: <export dict>, mode: "skip"|"update"}
        """
        _api_auth(request)
        body = await request.json()
        data = body.get("data") or body   # 支持直接发 export dict 或包装格式
        mode = body.get("mode", "skip")
        # 安全检查：必须含 entries/error_codes/rules 键之一
        if not any(k in data for k in ("entries", "error_codes", "rules", "version")):
            raise HTTPException(status_code=400, detail="无效的导入格式")
        result = _kb_store.import_from_data(data, mode=mode)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_import",
                            f"added={result['added']},updated={result['updated']}")
        return result

    @app.get("/api/kb/export-csv")
    async def api_kb_export_csv(request: Request):
        """导出知识条目为 CSV（含 BOM，Excel 可直接打开）"""
        _api_auth(request)
        ts = time.strftime("%Y%m%d_%H%M%S")
        content = _kb_store.export_csv()
        return Response(
            content.encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="kb_entries_{ts}.csv"'},
        )

    @app.post("/api/kb/import-csv")
    async def api_kb_import_csv(request: Request):
        """
        从 CSV 文本导入知识条目。
        Body: {csv: "<csv text>", mode: "skip"|"update"}
        """
        _api_auth(request)
        body = await request.json()
        csv_text = body.get("csv", "")
        mode     = body.get("mode", "skip")
        if not csv_text:
            raise HTTPException(status_code=400, detail="CSV 内容为空")
        result = _kb_store.import_from_csv(csv_text, mode=mode)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_import_csv",
                            f"added={result['added']},updated={result['updated']}")
        return result

    # ── P1-2 冷启动向导：空 KB 一键播种场景起步 FAQ 包 ----------
    @app.get("/api/kb/cold-start")
    async def api_kb_cold_start(request: Request):
        """KB 冷启动现状 + 可选起步包列表（供向导渲染）。"""
        _api_auth(request)
        from src.utils.kb_starter import kb_readiness, list_starter_packs
        return {"ok": True, "readiness": kb_readiness(_kb_store),
                "packs": list_starter_packs()}

    @app.post("/api/kb/seed-pack")
    async def api_kb_seed_pack(request: Request):
        """播种某场景起步包到 KB（按标题去重）。Body: {domain, dedup?}"""
        _api_auth(request)
        from src.utils.kb_starter import kb_readiness, seed_starter_pack
        body = await request.json()
        domain = str(body.get("domain") or "general")
        dedup = bool(body.get("dedup", True))
        try:
            added, skipped, titles = seed_starter_pack(
                _kb_store, domain, dedup=dedup)
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_seed_pack",
                            f"domain={domain},added={added},skipped={skipped}")
        return {"ok": True, "added": added, "skipped": skipped,
                "added_titles": titles, "readiness": kb_readiness(_kb_store)}

    # ── 过期检测 / 批量操作 / 使用率（批 5D）----------
    @app.get("/api/kb/stale")
    async def api_kb_stale(request: Request, days: int = 7):
        _api_auth(request)
        return {"stale": _kb_store.get_stale_entries(days=days), "days": days}

    @app.post("/api/kb/entries/bulk-disable")
    async def api_kb_bulk_disable(request: Request):
        _api_auth(request)
        data = await request.json()
        ids = data.get("ids", [])
        count = _kb_store.bulk_disable(ids)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_bulk_disable", f"{count} entries")
        return {"ok": True, "count": count}

    @app.post("/api/kb/entries/batch-update")
    async def api_kb_batch_update(request: Request):
        """
        批量更新条目属性。
        Body: {ids: [...], enabled: 0|1, category: "..."}
        只更新传入的字段，ids 为必填。
        """
        _api_auth(request)
        data   = await request.json()
        ids    = data.get("ids", [])
        if not ids:
            raise HTTPException(status_code=400, detail="ids 不能为空")
        updates: dict = {}
        if "enabled" in data:
            updates["enabled"] = int(bool(data["enabled"]))
        if "category" in data and data["category"]:
            updates["category"] = str(data["category"])
        if not updates:
            raise HTTPException(status_code=400, detail="未提供可更新的字段")
        count = 0
        with _kb_store._conn() as c:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            for eid in ids:
                c.execute(
                    f"UPDATE kb_entries SET {set_clause} WHERE id=?",
                    list(updates.values()) + [eid],
                )
                count += 1
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_batch_update",
                            f"ids={len(ids)},fields={list(updates.keys())}")
        return {"ok": True, "count": count}

    @app.get("/api/kb/usage-ranking")
    async def api_kb_usage_ranking(request: Request, limit: int = 20):
        _api_auth(request)
        with _kb_store._conn() as c:
            top = c.execute(
                "SELECT id, title, category, use_count, enabled, rating "
                "FROM kb_entries ORDER BY use_count DESC LIMIT ?",
                (limit,)
            ).fetchall()
            zero = c.execute(
                "SELECT COUNT(*) FROM kb_entries WHERE use_count=0 AND enabled=1"
            ).fetchone()[0]
        return {"ranking": [dict(r) for r in top], "never_used": zero}

    # ── 命中率 / 向量化统计 / 隐式反馈（批 5D，只读为主）----------
    @app.get("/api/kb/query-analytics")
    async def api_kb_query_analytics(request: Request, hours: int = 24):
        """返回过去 N 小时的 KB 命中率统计（每小时分桶）"""
        _api_auth(request)
        return _kb_store.get_query_analytics(hours=min(hours, 168))  # 最多7天

    @app.get("/api/kb/today-hit-rate")
    async def api_kb_today_hit_rate(request: Request):
        """今日命中率摘要（供 dashboard 快速展示）"""
        _api_auth(request)
        return _kb_store.get_today_hit_rate()

    @app.get("/api/kb/embed-stats")
    async def api_kb_embed_stats(request: Request):
        """读取 skill_manager 模块级 Embedding API / 缓存命中统计"""
        _api_auth(request)
        try:
            from src.skills.skill_manager import _EMBED_STATS, _EMBED_CACHE, _EMBED_CACHE_MAX
            import time as _time
            uptime_s = int(_time.time() - _EMBED_STATS.get("session_start", _time.time()))
            kb_q = _EMBED_STATS.get("kb_queries", 0)
            kb_h = _EMBED_STATS.get("kb_hits", 0)
            api  = _EMBED_STATS.get("api_calls", 0)
            chit = _EMBED_STATS.get("cache_hits", 0)
            return {
                **_EMBED_STATS,
                "cache_size":    len(_EMBED_CACHE),
                "cache_max":     _EMBED_CACHE_MAX,
                "cache_hit_pct": round(chit / (api + chit) * 100) if (api + chit) else 0,
                "kb_hit_pct":    round(kb_h / kb_q * 100) if kb_q else 0,
                "uptime_s":      uptime_s,
            }
        except ImportError:
            return {"error": "skill_manager 未加载，请确认 bot 正在运行"}

    @app.post("/api/kb/implicit-feedback")
    async def api_kb_implicit_feedback(request: Request):
        """bot 检测到用户隐式情绪信号后调用，自动记录反馈"""
        data = await request.json()
        fb_id = _kb_store.add_feedback({
            "user_message":  data.get("user_message", ""),
            "ai_reply":      data.get("ai_reply", ""),
            "score":         int(data.get("score", 0)),
            "correction":    data.get("correction", ""),
            "operator":      data.get("operator", "auto_detection"),
        })
        return {"id": fb_id, "ok": True}

    # ═══════════════════════════════════════════════════════════════════
    # Phase 5: 向量化 / 查重 / 知识库备份管理（批 5E：整组搬迁助手+state+端点）
    # ═══════════════════════════════════════════════════════════════════
    _embed_progress: dict = {
        "running": False, "total": 0, "done": 0, "failed": 0, "msg": ""
    }
    _kb_backup_dir = Path(config_manager.config_path).parent / "kb_backups"

    async def _call_embed_api(texts: List[str]) -> List[List[float]]:
        """
        调用智能体 Embedding API，批量返回向量列表。
        模型优先从 ai.embedding_model 读取，默认 text-embedding-v2。
        """
        import httpx as _httpx
        ai_cfg = config_manager.config.get("ai", {})
        api_key = ai_cfg.get("api_key", "")
        base_url = (ai_cfg.get("base_url", "https://api.deepseek.com")).rstrip("/")
        model = ai_cfg.get("embedding_model", "text-embedding-v2")
        if not api_key or not texts:
            return []
        try:
            async with _httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{base_url}/embeddings",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={"model": model, "input": texts},
                )
                data = resp.json()
                items = sorted(data["data"], key=lambda x: x["index"])
                return [item["embedding"] for item in items]
        except Exception as _e:
            logger.warning("Embedding API 调用失败: %s", _e)
            return []

    def _build_embed_text(entry: dict) -> str:
        """将条目字段拼接成用于向量化的文本（字段权重体现在顺序与重复）"""
        triggers = entry.get("triggers", "[]")
        if isinstance(triggers, str):
            try:
                triggers = " ".join(json.loads(triggers))
            except Exception:
                pass
        parts = [
            str(triggers) * 2,              # 触发词权重加倍
            entry.get("title", "") * 1,
            entry.get("scenario", ""),
            entry.get("steps", ""),
            entry.get("example_reply_zh", ""),
        ]
        return " ".join(p for p in parts if p).strip()[:800]  # 截断防超 token

    async def _run_embed_all():
        """后台任务：增量向量化所有未处理条目（批量 20 条/次）"""
        pending = _kb_store.get_entries_without_embedding()
        _embed_progress.update({
            "running": True, "total": len(pending),
            "done": 0, "failed": 0, "msg": f"开始向量化 {len(pending)} 条…"
        })
        batch_size = 20
        for i in range(0, len(pending), batch_size):
            batch = pending[i: i + batch_size]
            texts  = [_build_embed_text(e) for e in batch]
            vectors = await _call_embed_api(texts)
            if not vectors or len(vectors) != len(batch):
                _embed_progress["failed"] += len(batch)
                _embed_progress["msg"] = f"第 {i} 批 Embedding API 调用失败"
            else:
                for entry, vec in zip(batch, vectors):
                    _kb_store.set_single_embedding(entry["id"], vec)
                _embed_progress["done"] += len(batch)
                _embed_progress["msg"] = (
                    f"已完成 {_embed_progress['done']}/{_embed_progress['total']}"
                )
        _embed_progress["running"] = False
        _embed_progress["msg"] = (
            f"完成！成功 {_embed_progress['done']} 条，"
            f"失败 {_embed_progress['failed']} 条"
        )

    @app.post("/api/kb/embed-all")
    async def api_kb_embed_all(request: Request, background_tasks: BackgroundTasks):
        _api_auth(request)
        if _embed_progress.get("running"):
            return {"ok": False, "msg": "向量化任务正在运行中"}
        pending_cnt = len(_kb_store.get_entries_without_embedding())
        if not pending_cnt:
            return {"ok": False, "msg": "所有条目已完成向量化，无需重新处理"}
        background_tasks.add_task(_run_embed_all)
        return {"ok": True, "pending": pending_cnt}

    @app.get("/api/kb/embed-progress")
    async def api_kb_embed_progress(request: Request):
        _api_auth(request)
        cov = _kb_store.embedding_coverage()
        return {**_embed_progress, "coverage": cov}

    @app.post("/api/kb/entries/{entry_id}/embed")
    async def api_kb_embed_single(request: Request, entry_id: str):
        _api_auth(request)
        entry = _kb_store.get_entry(entry_id)
        if not entry:
            raise HTTPException(status_code=404)
        text = _build_embed_text(entry)
        vecs = await _call_embed_api([text])
        if not vecs:
            return {"ok": False, "msg": "Embedding API 调用失败"}
        _kb_store.set_single_embedding(entry_id, vecs[0])
        return {"ok": True}

    @app.get("/api/kb/embed-coverage")
    async def api_kb_embed_coverage(request: Request):
        _api_auth(request)
        return _kb_store.embedding_coverage()

    # ---------- 查重 ----------
    @app.get("/api/kb/duplicates")
    async def api_kb_duplicates(request: Request, threshold: float = 0.85):
        _api_auth(request)
        pairs = _kb_store.find_duplicates(threshold=threshold)
        return {"pairs": pairs, "count": len(pairs), "threshold": threshold}

    # ---------- 知识库备份 ----------
    @app.post("/api/kb/backup")
    async def api_kb_backup(request: Request):
        _api_auth(request)
        path = _kb_store.backup(_kb_backup_dir)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_backup", path)
        return {"ok": True, "path": path}

    @app.get("/api/kb/backups")
    async def api_kb_backups(request: Request):
        _api_auth(request)
        return {"backups": _kb_store.list_backups(_kb_backup_dir)}

    @app.post("/api/kb/restore/{filename}")
    async def api_kb_restore(request: Request, filename: str):
        _api_auth(request)
        # 仅允许在 backup_dir 内的文件
        backup_path = _kb_backup_dir / filename
        if not backup_path.exists() or backup_path.parent != _kb_backup_dir:
            raise HTTPException(status_code=404)
        _kb_store.restore(backup_path)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_restore", filename)
        return {"ok": True}

    # ── 翻译端点（批 5H：直接用 kb_ai_helpers.ai_translate_entry 纯函数）----------
    @app.post("/api/kb/entries/{entry_id}/auto-translate")
    async def api_kb_auto_translate(request: Request, entry_id: str):
        _api_auth(request)
        entry = _kb_store.get_entry(entry_id)
        if not entry:
            raise HTTPException(status_code=404)
        data = await request.json()
        langs = data.get("langs", ["en", "ur", "pt", "ar"])
        results = await ai_translate_entry(config_manager, entry, langs)
        saved = []
        for lang, fields in results.items():
            if isinstance(fields, dict) and fields:
                _kb_store.upsert_translation(entry_id, lang, fields, auto=True)
                saved.append(lang)
        return {"ok": True, "translated_to": saved, "results": results}

    @app.get("/api/kb/translation-gaps")
    async def api_kb_translation_gaps(request: Request):
        """分析翻译缺口：按使用频率排序，优先翻译高频条目"""
        _api_auth(request)
        target_langs = ["en", "ur", "pt", "ar"]
        entries = _kb_store.list_entries(enabled_only=True)
        gaps = []
        for e in entries:
            full = _kb_store.get_entry(e["id"])
            trans = (full or {}).get("translations", {})
            missing = [l for l in target_langs if l not in trans]
            if missing:
                gaps.append({
                    "entry_id":   e["id"],
                    "title":      e.get("title", ""),
                    "category":   e.get("category", ""),
                    "use_count":  e.get("use_count", 0),
                    "missing":    missing,
                    "has":        [l for l in target_langs if l in trans],
                })
        gaps.sort(key=lambda x: -x["use_count"])
        total = len(entries)
        fully_translated = total - len(gaps)
        return {
            "total_entries":      total,
            "fully_translated":   fully_translated,
            "coverage_pct":       round(fully_translated / total * 100) if total else 0,
            "gaps":               gaps[:20],
            "gap_count":          len(gaps),
        }

    @app.post("/api/kb/translate-all")
    async def api_kb_translate_all(request: Request):
        _api_auth(request)
        data = await request.json()
        langs = data.get("langs", ["en", "ur", "pt", "ar"])
        force = data.get("force", False)
        entries = _kb_store.list_entries(enabled_only=True)
        summary = {"total": len(entries), "translated": 0, "skipped": 0, "failed": 0}
        for entry in entries:
            if not force:
                full = _kb_store.get_entry(entry["id"])
                existing = set((full or {}).get("translations", {}).keys())
                target_langs = [l for l in langs if l not in existing]
                if not target_langs:
                    summary["skipped"] += 1
                    continue
            else:
                target_langs = langs
            try:
                trans = await ai_translate_entry(config_manager, entry, target_langs)
                for lang, fields in trans.items():
                    if isinstance(fields, dict) and fields:
                        _kb_store.upsert_translation(entry["id"], lang, fields, auto=True)
                summary["translated"] += 1
            except Exception:
                summary["failed"] += 1
        return summary

    @app.get("/api/kb/translate-progress")
    async def api_kb_translate_progress(request: Request, force: int = 0):
        """SSE 流式批量翻译进度（GET，通过 session cookie 鉴权）"""
        _api_auth(request)
        langs = ["en", "ur", "pt", "ar"]
        entries = _kb_store.list_entries(enabled_only=True)
        total = len(entries)

        async def _stream():
            yield f"data: {json.dumps({'type':'start','total':total})}\n\n"
            translated = skipped = failed = 0
            for i, entry in enumerate(entries):
                title_short = (entry.get("title") or "")[:24]
                if not force:
                    full = _kb_store.get_entry(entry["id"])
                    existing = set((full or {}).get("translations", {}).keys())
                    target_langs = [l for l in langs if l not in existing]
                    if not target_langs:
                        skipped += 1
                        yield f"data: {json.dumps({'type':'progress','i':i+1,'total':total,'translated':translated,'skipped':skipped,'failed':failed,'title':title_short,'action':'skip'})}\n\n"
                        continue
                else:
                    target_langs = langs
                try:
                    trans = await ai_translate_entry(config_manager, entry, target_langs)
                    for lang, fields in trans.items():
                        if isinstance(fields, dict) and fields:
                            _kb_store.upsert_translation(entry["id"], lang, fields, auto=True)
                    translated += 1
                    yield f"data: {json.dumps({'type':'progress','i':i+1,'total':total,'translated':translated,'skipped':skipped,'failed':failed,'title':title_short,'action':'done'})}\n\n"
                except Exception as _te:
                    failed += 1
                    yield f"data: {json.dumps({'type':'progress','i':i+1,'total':total,'translated':translated,'skipped':skipped,'failed':failed,'title':title_short,'action':'fail','error':str(_te)[:80]})}\n\n"
            yield f"data: {json.dumps({'type':'done','total':total,'translated':translated,'skipped':skipped,'failed':failed})}\n\n"

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"},
        )

    # ── 重译 / Miss→条目 / 采纳建议（批 5I：直接用 kb_ai_helpers 纯函数）----------
    @app.post("/api/kb/translations/{trans_id}/retranslate")
    async def api_kb_trans_retranslate(request: Request, trans_id: str):
        _api_auth(request)
        with _kb_store._conn() as c:
            row = c.execute(
                "SELECT entry_id, lang FROM kb_translations WHERE id=?", (trans_id,)
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404)
        entry = _kb_store.get_entry(row["entry_id"])
        if not entry:
            raise HTTPException(status_code=404)
        results = await ai_translate_entry(config_manager, entry, [row["lang"]])
        if results.get(row["lang"]):
            _kb_store.upsert_translation(row["entry_id"], row["lang"],
                                         results[row["lang"]], auto=True)
            return {"ok": True, "result": results[row["lang"]]}
        return {"ok": False, "msg": "翻译API无返回"}

    @app.post("/api/kb/miss-to-entry")
    async def api_kb_miss_to_entry(request: Request):
        _api_auth(request)
        data = await request.json()
        query = (data.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="query 不能为空")
        _title = data.get("title", query[:50])
        _cat = data.get("category", "其他")
        entry_id = _kb_store.add_entry({
            "category":         _cat,
            "title":            _title,
            "triggers":         [query],
            "scenario":         f"用户发送了: {query}",
            "steps":            data.get("steps", ""),
            "principles":       data.get("principles", ""),
            "example_reply_zh": data.get("example_reply_zh", ""),
        })
        _kb_store.delete_miss_entry(query)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_miss_to_entry", entry_id)
        # L1: 后台自动用 AI 填充条目内容
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop().create_task(
                auto_fill_entry(config_manager, _kb_store, entry_id, _title, _cat,
                                source_query=query))
        except Exception:
            pass
        return {"id": entry_id, "ok": True}

    # ── P3-2 质量→KB 改进闭环：把「AI 答错被改写/拒绝」会话沉淀为 KB 条目 ──
    @app.get("/api/kb/improvements")
    async def api_kb_improvements(request: Request, days: int = 7, limit: int = 20):
        """改进候选：近期被坐席改写/拒绝的 AI 草稿 → 客户问句 + 改写后好答案。"""
        _api_auth(request)
        import time as _t
        inbox = getattr(request.app.state, "inbox_store", None)
        if inbox is None or not hasattr(inbox, "get_kb_improvement_candidates"):
            return {"ok": True, "candidates": [], "available": False}
        days = max(1, min(90, int(days or 7)))
        since = _t.time() - days * 86400
        try:
            cands = inbox.get_kb_improvement_candidates(since, limit=int(limit or 20))
        except Exception:
            return {"ok": True, "candidates": [], "available": False}
        return {"ok": True, "candidates": cands, "available": True, "days": days}

    @app.post("/api/kb/improvements/convert")
    async def api_kb_improvements_convert(request: Request):
        """把一条改进候选转为 KB 条目（trigger=客户问句，reply=改写后答案）+ 后台 AI 填充。"""
        _api_auth(request)
        data = await request.json()
        question = (data.get("question") or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="question 不能为空")
        _title = (data.get("title") or question[:50]).strip()
        _cat = data.get("category", "其他")
        reply = (data.get("suggested_reply") or "").strip()
        entry_id = _kb_store.add_entry({
            "category": _cat,
            "title": _title,
            "triggers": [question],
            "scenario": f"客户问：{question}",
            "example_reply_zh": reply,
        })
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_improve_convert", entry_id)
        # 无改写答案（rejected 候选）时后台用 AI 填充；有答案则保留人工好答案不覆盖
        if not reply:
            import asyncio as _asyncio
            try:
                _asyncio.get_running_loop().create_task(
                    auto_fill_entry(config_manager, _kb_store, entry_id, _title, _cat,
                                    source_query=question))
            except Exception:
                pass
        return {"id": entry_id, "ok": True, "ai_filled": not reply}

    @app.post("/api/kb/accept-suggestion")
    async def api_kb_accept_suggestion(request: Request):
        """一键采纳建议 → 创建新 KB 条目 + 后台 AI 自动填充"""
        _api_auth(request)
        data = await request.json()
        _title = data.get("title", "")
        _cat = data.get("category", "其他")
        entry_id = _kb_store.add_entry({
            "category":         _cat,
            "title":            _title,
            "triggers":         data.get("triggers", []),
            "scenario":         data.get("scenario", ""),
            "steps":            data.get("steps", ""),
            "principles":       data.get("principles", ""),
            "example_reply_zh": data.get("example_reply_zh", ""),
        })
        query = (data.get("source_query") or "").strip()
        if query:
            _kb_store.delete_miss_entry(query)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_accept_suggestion", entry_id)
        # L1: 后台自动用 AI 填充条目内容
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop().create_task(
                auto_fill_entry(config_manager, _kb_store, entry_id, _title, _cat,
                                source_query=query))
        except Exception:
            pass
        return {"id": entry_id, "ok": True}

    # ── 健康统计 / Miss 日志 / 翻译审核 / 图片 / 种子 / 维护建议（批 5K）----------
    def _miss_table_exists(conn) -> bool:
        try:
            conn.execute("SELECT 1 FROM kb_miss_log LIMIT 1")
            return True
        except Exception:
            return False

    @app.get("/api/kb/health-stats")
    async def api_kb_health_stats(request: Request):
        _api_auth(request)
        stats = _kb_store.stats()
        with _kb_store._conn() as c:
            top_used = c.execute(
                "SELECT title, category, use_count FROM kb_entries "
                "WHERE use_count>0 ORDER BY use_count DESC LIMIT 5"
            ).fetchall()
            never_used = c.execute(
                "SELECT COUNT(*) FROM kb_entries WHERE use_count=0 AND enabled=1"
            ).fetchone()[0]
            recent_fb = c.execute(
                "SELECT COUNT(*) FROM kb_feedback "
                "WHERE created_at > datetime('now','-7 days')"
            ).fetchone()[0]
            recent_good = c.execute(
                "SELECT COUNT(*) FROM kb_feedback WHERE score=1 "
                "AND created_at > datetime('now','-7 days')"
            ).fetchone()[0]
            trans_coverage: dict = {}
            for lang in ["en", "ur", "pt", "ar"]:
                cnt = c.execute(
                    "SELECT COUNT(DISTINCT entry_id) FROM kb_translations WHERE lang=?",
                    (lang,)
                ).fetchone()[0]
                total = stats["total_entries"] or 1
                trans_coverage[lang] = round(cnt / total * 100, 1)
            miss_rows = c.execute(
                "SELECT query, cnt FROM kb_miss_log ORDER BY cnt DESC LIMIT 8"
            ).fetchall() if _miss_table_exists(c) else []
        return {
            "stats": stats,
            "top_used": [dict(r) for r in top_used],
            "never_used": never_used,
            "recent_feedback_7d": recent_fb,
            "recent_good_7d": recent_good,
            "recent_satisfaction": round(recent_good / recent_fb * 100, 1) if recent_fb else 0,
            "translation_coverage": trans_coverage,
            "miss_queries": [dict(r) for r in miss_rows],
        }

    # ---------- Miss 日志写入（供 bot 内部调用） ----------
    @app.post("/api/kb/miss-log")
    async def api_kb_miss_log(request: Request):
        data = await request.json()
        query = (data.get("query") or "").strip()[:200]
        if not query:
            return {"ok": False}
        _kb_store.log_miss(query)
        return {"ok": True}

    # ---------- 翻译审核 ----------
    @app.get("/api/kb/translations/pending")
    async def api_kb_trans_pending(request: Request, limit: int = 100):
        _api_auth(request)
        return {"pending": _kb_store.get_pending_translations(limit=limit)}

    @app.post("/api/kb/translations/{trans_id}/confirm")
    async def api_kb_trans_confirm(request: Request, trans_id: str):
        _api_auth(request)
        ok = _kb_store.confirm_translation(trans_id)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_confirm_translation", trans_id)
        return {"ok": ok}

    @app.put("/api/kb/translations/{trans_id}")
    async def api_kb_trans_update(request: Request, trans_id: str):
        """手动修改翻译内容并标记为已审核"""
        _api_auth(request)
        data = await request.json()
        allowed = ("title", "scenario", "steps", "principles", "example_reply", "forbidden")
        sets = ", ".join(f"{k}=?" for k in allowed if k in data)
        vals = [data[k] for k in allowed if k in data]
        if not sets:
            return {"ok": False}
        import time as _time
        now = _time.strftime("%Y-%m-%dT%H:%M:%S")
        vals += [now, trans_id]
        with _kb_store._conn() as c:
            c.execute(
                f"UPDATE kb_translations SET {sets}, auto_translated=0, updated_at=? WHERE id=?",
                vals
            )
        return {"ok": True}

    @app.delete("/api/kb/miss-log")
    async def api_kb_delete_miss(request: Request):
        _api_auth(request)
        data = await request.json()
        query = (data.get("query") or "").strip()
        if query:
            _kb_store.delete_miss_entry(query)
        return {"ok": True}

    # ── 知识条目图片附件 ──────────────────────────────────────
    @app.post("/api/kb/entries/{entry_id}/images")
    async def api_kb_upload_image(entry_id: str, request: Request,
                                  file: UploadFile = File(...),
                                  caption: str = Form("")):
        """上传图片并关联到指定知识条目（JPEG/PNG/GIF/WEBP，≤5 MB）"""
        _api_auth(request)
        allowed_types = {"image/jpeg", "image/png", "image/gif", "image/webp"}
        if file.content_type not in allowed_types:
            raise HTTPException(400, "仅支持 JPEG/PNG/GIF/WEBP 图片")
        data = await file.read()
        if len(data) > 5 * 1024 * 1024:
            raise HTTPException(400, "图片大小不能超过 5 MB")
        # 确保存储目录存在
        from pathlib import Path as _P
        img_dir = _P(config_manager.config_path).parent / "kb_images"
        img_dir.mkdir(exist_ok=True)
        # 生成唯一文件名（保留原始扩展名）
        import uuid as _uuid
        ext = (file.filename or "img.jpg").rsplit(".", 1)[-1].lower()
        filename = f"{_uuid.uuid4().hex[:16]}.{ext}"
        (img_dir / filename).write_bytes(data)
        img_id = _kb_store.add_entry_image(entry_id, filename, caption, len(data))
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_upload_image", entry_id, "", filename)
        return {"ok": True, "id": img_id, "filename": filename, "url": f"/kb-images/{filename}"}

    @app.get("/api/kb/entries/{entry_id}/images")
    async def api_kb_get_images(entry_id: str, request: Request):
        """获取条目的图片列表"""
        _api_auth(request)
        imgs = _kb_store.get_entry_images(entry_id)
        for img in imgs:
            img["url"] = f"/kb-images/{img['filename']}"
        return {"images": imgs}

    @app.delete("/api/kb/images/{img_id}")
    async def api_kb_delete_image(img_id: str, request: Request):
        """删除图片记录及物理文件"""
        _api_auth(request)
        filename = _kb_store.delete_entry_image(img_id)
        if not filename:
            raise HTTPException(404, "图片不存在")
        from pathlib import Path as _P
        img_file = _P(config_manager.config_path).parent / "kb_images" / filename
        try:
            img_file.unlink(missing_ok=True)
        except Exception:
            pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_delete_image", img_id, "", filename)
        return {"ok": True}

    # ── 种子数据导入 ──────────────────────────────────────────
    @app.post("/api/kb/seed")
    async def api_kb_seed(request: Request):
        """导入内置示例知识条目（电商/SaaS场景）"""
        _api_auth(request)
        body = await request.json()
        category = body.get("category", "all")
        from src.utils.kb_store import seed_kb_examples
        result = seed_kb_examples(_kb_store, category=category)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_seed", category,
                            "", f"added={result['added']}")
        if result["added"] > 0:
            _kb_store._rebuild_index()
        return result

    @app.get("/api/kb/maintenance-advice")
    async def api_kb_maintenance_advice(request: Request):
        """返回知识库健康诊断报告（健康分 + 可操作建议列表）"""
        _api_auth(request)
        return _kb_store.get_maintenance_advice()

    # ── 报告 / 图片静态 / AI生成 / 导出MD / 统计 / 手动翻译 / 沙盒AI / 建议（批 5L）----------
    @app.get("/api/kb/report")
    async def api_kb_report(request: Request, fmt: str = "html"):
        """生成知识库分析报告（自包含 HTML，可打印为 PDF）"""
        _api_auth(request)
        from src.web.kb_report import build_kb_report
        html_content = build_kb_report(_kb_store, audit_store)
        ts = time.strftime("%Y%m%d_%H%M%S")
        return Response(
            html_content.encode("utf-8"),
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="kb_report_{ts}.html"'},
        )

    @app.get("/kb-images/{filename}")
    async def kb_image_serve(filename: str, request: Request):
        """静态服务知识库图片文件"""
        _api_auth(request)
        from pathlib import Path as _P
        import mimetypes as _mt
        img_dir = _P(config_manager.config_path).parent / "kb_images"
        filepath = img_dir / filename
        # 防路径穿越
        if not filepath.resolve().is_relative_to(img_dir.resolve()):
            raise HTTPException(403, "访问被拒绝")
        if not filepath.exists():
            raise HTTPException(404, "图片不存在")
        mime = _mt.guess_type(str(filepath))[0] or "image/jpeg"
        return Response(
            filepath.read_bytes(),
            media_type=mime,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.post("/api/kb/ai-generate")
    async def api_kb_ai_generate(request: Request):
        """
        根据给定主题，用 AI 自动补全知识条目的所有字段。
        输入: {topic, category, hint, lang}
        输出: {ok, entry: {triggers, scenario, steps, principles, example_reply_zh, forbidden}}
        """
        _api_auth(request)
        import httpx as _httpx, re as _re
        data = await request.json()
        topic    = (data.get("topic") or "").strip()
        category = data.get("category", "其他")
        hint     = data.get("hint", "")
        if not topic:
            raise HTTPException(400, "topic 不能为空")

        ai_cfg   = config_manager.config.get("ai", {})
        api_key  = ai_cfg.get("api_key", "")
        base_url = (ai_cfg.get("base_url", "https://api.deepseek.com")).rstrip("/")
        model    = ai_cfg.get("model", "deepseek-chat")
        if not api_key:
            raise HTTPException(400, "AI 未配置，请先在设置页面填入 API Key")

        sys_prompt = (
            "你是一位资深客服话术专家，专注于电商、SaaS、金融支付领域的客户服务。"
            "请根据给定主题，生成一条完整的知识库条目，严格以 JSON 格式返回，不要任何解释和代码块标记。"
        )
        hint_part = f"\n额外提示：{hint}" if hint else ""
        user_prompt = f"""主题/标题：{topic}
分类：{category}{hint_part}

请返回以下 JSON（字段名必须完全匹配，值用中文）：
{{
  "triggers": ["关键词1","关键词2","关键词3","关键词4","关键词5"],
  "scenario": "1-2句话：描述什么情况下用户会发这类消息",
  "steps": "1. 第一步\\n2. 第二步\\n3. 第三步（处理这类问题的标准步骤）",
  "principles": "处理此类问题的核心原则（1-2句话）",
  "example_reply_zh": "客服标准回复示例（100字以内，语气友好专业，可加适当emoji）",
  "forbidden": "绝对不能说或不能做的事情（1-2条）"
}}"""

        try:
            async with _httpx.AsyncClient(timeout=40) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={"model": model,
                          "messages": [
                              {"role": "system", "content": sys_prompt},
                              {"role": "user",   "content": user_prompt},
                          ],
                          "max_tokens": 800, "temperature": 0.7,
                          "response_format": {"type": "json_object"}},
                )
            result = resp.json()
            raw    = result["choices"][0]["message"]["content"]
        except Exception as _e:
            return {"ok": False, "error": f"AI 调用失败: {_e}"}

        # 解析 JSON（尝试直接解析，失败则提取代码块中的 JSON）
        entry = {}
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            m = _re.search(r'\{[\s\S]+\}', raw)
            if m:
                try:
                    entry = json.loads(m.group())
                except Exception:
                    return {"ok": False, "error": "AI 返回了无法解析的格式", "raw": raw[:500]}

        # 规范化字段
        if "triggers" in entry and isinstance(entry["triggers"], list):
            entry["triggers"] = [str(t) for t in entry["triggers"] if str(t).strip()]
        for field in ("scenario", "steps", "principles", "example_reply_zh", "forbidden"):
            if field not in entry:
                entry[field] = ""

        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_ai_generate", topic)
        return {"ok": True, "entry": entry, "topic": topic, "category": category}

    @app.get("/api/kb/export-markdown")
    async def api_kb_export_markdown(request: Request):
        """生成可读的 Markdown 格式知识库文档（按分类组织）"""
        _api_auth(request)
        import re as _re

        with _kb_store._conn() as c:
            entries = c.execute(
                "SELECT category, title, triggers, scenario, steps, principles, "
                "example_reply_zh, forbidden, use_count "
                "FROM kb_entries WHERE enabled=1 "
                "ORDER BY category, use_count DESC, title"
            ).fetchall()

        lines = [
            "# 知识库文档",
            f"\n> 导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')} · 共 {len(entries)} 条条目\n",
        ]

        current_cat = None
        for row in entries:
            e = dict(row)
            if e["category"] != current_cat:
                current_cat = e["category"]
                lines.append(f"\n## {current_cat}\n")

            # 解析触发词
            try:
                triggers = json.loads(e["triggers"] or "[]")
            except Exception:
                triggers = []
            trigger_str = " / ".join(f"`{t}`" for t in triggers[:6]) if triggers else "—"

            lines.append(f"### {e['title']}")
            lines.append(f"\n**触发词**：{trigger_str}")
            if e.get("scenario"):
                lines.append(f"\n**使用场景**：{e['scenario']}")
            if e.get("steps"):
                lines.append(f"\n**处理步骤**：\n\n{e['steps']}")
            if e.get("principles"):
                lines.append(f"\n**注意原则**：{e['principles']}")
            if e.get("example_reply_zh"):
                lines.append(f"\n**标准回复**：\n\n> {e['example_reply_zh']}")
            if e.get("forbidden"):
                lines.append(f"\n**禁止事项**：{e['forbidden']}")
            lines.append(f"\n_使用次数：{e.get('use_count', 0)}_\n")
            lines.append("---")

        md = "\n".join(lines)
        ts = time.strftime("%Y%m%d_%H%M%S")
        return Response(
            md.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="kb_{ts}.md"'},
        )

    @app.get("/api/kb/stats")
    async def api_kb_stats(request: Request):
        _api_auth(request)
        return _kb_store.stats()

    @app.post("/api/kb/entries/{entry_id}/translate")
    async def api_kb_translate(request: Request, entry_id: str):
        _api_auth(request)
        data = await request.json()
        lang = data.get("lang", "en")
        fields = {k: v for k, v in data.items() if k != "lang"}
        trans_id = _kb_store.upsert_translation(entry_id, lang, fields, auto=False)
        return {"id": trans_id, "ok": True}

    @app.post("/api/kb/sandbox/ai-reply")
    async def api_kb_sandbox_ai_reply(request: Request):
        _api_auth(request)
        import httpx as _httpx
        data = await request.json()
        query = data.get("query", "")
        kb_context = data.get("kb_context", "")
        lang = data.get("lang", "zh")
        ai_cfg = config_manager.config.get("ai", {})
        api_key = ai_cfg.get("api_key", "")
        base_url = (ai_cfg.get("base_url", "https://api.deepseek.com")).rstrip("/")
        model = ai_cfg.get("model", "deepseek-chat")
        if not api_key:
            return {"reply": "AI 未配置，无法模拟回复", "ok": False}
        sys_prompt = (
            ai_cfg.get("system_prompt")
            or config_manager.config.get("system_prompt", "")
            or "你是一位专业的 AI 助手，回复简洁准确。"
        )
        messages = [{"role": "system", "content": sys_prompt}]
        if kb_context:
            messages.append({"role": "system",
                              "content": f"【知识库参考材料（请优先参考）】:\n{kb_context}"})
        messages.append({"role": "user", "content": query})
        try:
            async with _httpx.AsyncClient(timeout=35) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={"model": model, "messages": messages,
                          "max_tokens": 500, "temperature": 0.7},
                )
                result = resp.json()
                reply = result["choices"][0]["message"]["content"]
                return {"reply": reply.strip(), "ok": True}
        except Exception as _e:
            return {"reply": f"AI 调用失败: {_e}", "ok": False}

    @app.get("/api/kb/auto-suggestions")
    async def api_kb_auto_suggestions(request: Request):
        """综合 miss + 弱命中 + 过载条目，返回自动建议列表"""
        _api_auth(request)
        return {
            "suggestions": _kb_store.get_auto_suggestions(),
            "weak_hits":   _kb_store.get_weak_hits(top_k=10),
            "overloaded":  _kb_store.get_overloaded_entries(),
        }

    @app.get("/api/kb/reply-quality")
    async def api_kb_reply_quality(request: Request, days: int = 7):
        """回复质量统计：满意度、负面信号趋势、重复提问频率"""
        _api_auth(request)
        return _kb_store.get_reply_quality_stats(days=min(days, 30))
