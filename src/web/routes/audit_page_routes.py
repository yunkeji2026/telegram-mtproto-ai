"""审计日志页面路由（Phase E1 续拆，从 admin.py 抽出）。

端点：
  GET /audit
  GET /audit/export

依赖：templates / page_auth / audit_store。
"""

from __future__ import annotations

import csv
import io as _io
import time

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse


def register_audit_page_routes(app, ctx) -> None:
    templates = ctx.templates
    _page_auth = ctx.page_auth
    audit_store = ctx.audit_store

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_page(request: Request, _=Depends(_page_auth),
                         action: str = "", keyword: str = "", limit: int = 50,
                         operator: str = "", channel: str = "",
                         date_from: str = "", date_to: str = "",
                         page: int = 1):
        all_entries = []
        if audit_store:
            all_entries = audit_store.query(limit=500, action=action, keyword=keyword)
        if operator:
            all_entries = [e for e in all_entries if operator.lower() in str(e.get("user_id", "")).lower()]
        if channel:
            all_entries = [e for e in all_entries if channel.lower() in str(e.get("target", "")).lower()]
        if date_from:
            all_entries = [e for e in all_entries if str(e.get("ts", "")) >= date_from]
        if date_to:
            all_entries = [e for e in all_entries if str(e.get("ts", ""))[:10] <= date_to]
        total = len(all_entries)
        per_page = limit
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        records = all_entries[(page - 1) * per_page: page * per_page]
        all_actions = sorted(set(e.get("action", "") for e in all_entries if e.get("action")))
        all_operators = sorted(set(str(e.get("user_id", "")) for e in all_entries if e.get("user_id")))
        qs_parts = []
        if action:
            qs_parts.append(f"action={action}")
        if keyword:
            qs_parts.append(f"keyword={keyword}")
        if operator:
            qs_parts.append(f"operator={operator}")
        if channel:
            qs_parts.append(f"channel={channel}")
        if date_from:
            qs_parts.append(f"date_from={date_from}")
        if date_to:
            qs_parts.append(f"date_to={date_to}")
        qs_parts.append(f"limit={limit}")
        query_str = "&".join(qs_parts)
        return templates.TemplateResponse(request, "audit.html", {
            "records": records,
            "total": total, "page": page, "total_pages": total_pages,
            "query_str": query_str,
            "filters": {"action": action, "keyword": keyword, "operator": operator,
                        "channel": channel, "date_from": date_from, "date_to": date_to},
            "all_actions": all_actions, "all_operators": all_operators,
        })

    @app.get("/audit/export")
    async def audit_export(request: Request, _=Depends(_page_auth),
                           action: str = "", operator: str = "",
                           channel: str = "", date_from: str = "", date_to: str = ""):
        """导出审计记录为 CSV（UTF-8 BOM，Excel 直接打开不乱码）。"""
        all_entries = audit_store.query(limit=10000) if audit_store else []
        if action:
            all_entries = [e for e in all_entries if e.get("action", "").startswith(action)]
        if operator:
            all_entries = [e for e in all_entries if e.get("user_id", "") == operator]
        if channel:
            kw = channel.lower()
            all_entries = [e for e in all_entries if
                           kw in e.get("target", "").lower() or
                           kw in e.get("action", "").lower() or
                           kw in (e.get("new_val") or "").lower()]
        if date_from:
            all_entries = [e for e in all_entries if str(e.get("ts", "")) >= date_from]
        if date_to:
            end = date_to + "T23:59:59"
            all_entries = [e for e in all_entries if str(e.get("ts", "")) <= end]

        buf = _io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["# 导出时间", time.strftime("%Y-%m-%d %H:%M:%S")])
        writer.writerow(["# 筛选条件",
                         f"操作={action or '全部'}",
                         f"操作人={operator or '全部'}",
                         f"关键词={channel or '无'}",
                         f"日期={date_from or '不限'}~{date_to or '不限'}"])
        writer.writerow(["# 记录总数", len(all_entries)])
        writer.writerow([])
        writer.writerow(["序号", "时间", "操作类型", "目标", "操作人", "旧值", "新值", "快照ID"])
        for i, e in enumerate(all_entries, 1):
            writer.writerow([
                i,
                e.get("ts", ""),
                e.get("action", ""),
                e.get("target", ""),
                e.get("user_id", ""),
                e.get("old_val", "") or "",
                e.get("new_val", "") or "",
                e.get("snapshot_id", "") or "",
            ])
        content = buf.getvalue().encode("utf-8-sig")
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"audit_{ts}.csv"
        if action or operator or channel:
            tag = (action or operator or channel or "filtered").replace(" ", "_")[:20]
            filename = f"audit_{tag}_{ts}.csv"
        return StreamingResponse(
            iter([content]),
            media_type="text/csv; charset=utf-8-sig",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
