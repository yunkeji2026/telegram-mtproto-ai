"""运营仪表盘只读 API 路由（Phase E1 续拆，从 admin.py 抽出）。

端点（与抽出前逐行一致，依赖全在 AdminRouteContext）：
  GET /api/notifications       策略告警 + 最近审计聚合
  GET /api/snapshots           配置快照列表
  GET /api/trigger-decisions   触发器决策日志尾部

health-check / alert-status 因依赖 create_app 内的 domain_web_pages /
domain_dashboard_widgets（暂未入 ctx），仍留 admin.py。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import Depends, Request


def register_ops_dashboard_routes(app, ctx) -> None:
    config_manager = ctx.config_manager
    event_tracker = ctx.event_tracker
    audit_store = ctx.audit_store
    _page_auth = ctx.page_auth
    _api_auth = ctx.api_auth

    @app.get("/api/notifications")
    async def api_notifications(request: Request, _=Depends(_page_auth)):
        """聚合策略告警 + 最近系统操作，返回通知列表"""
        notifs = []

        # 1. 策略效果告警：quality_score < 40
        if event_tracker:
            try:
                from src.strategy.strategy_analytics import StrategyAnalytics
                sa = StrategyAnalytics(event_tracker)
                summary = sa.summarize(hours=24)
                for s in summary:
                    qs = s.get("quality_score", 100)
                    if qs < 40:
                        notifs.append({
                            "id": f"strategy_{s['strategy_id']}",
                            "type": "strategy",
                            "level": "critical" if qs < 20 else "warn",
                            "title": f"策略告警：{s['strategy_id']}",
                            "body": f"质量评分仅 {qs}/100，建议优化",
                            "ts": "",
                        })
            except Exception:
                pass

        # 2. 最近审计记录（最新 5 条）
        if audit_store:
            try:
                recent = audit_store.query(limit=5)
                for e in recent:
                    notifs.append({
                        "id": f"audit_{e.get('id', '')}",
                        "type": "system",
                        "level": "info",
                        "title": e.get("action", "操作"),
                        "body": e.get("target", ""),
                        "ts": e.get("ts", ""),
                    })
            except Exception:
                pass

        return {"notifications": notifs[:12], "unread": len(notifs)}

    @app.get("/api/snapshots")
    async def api_list_snapshots(request: Request, _=Depends(_api_auth),
                                 prefix: str = "", limit: int = 30):
        """列出可用快照（支持按 prefix 过滤，如 templates / exchange_rates）"""
        cfg_dir = config_manager.config_path.parent
        snap_dir = cfg_dir / "snapshots"
        if not snap_dir.exists():
            return {"snapshots": [], "total": 0}
        glob_pat = f"{prefix}_*.yaml" if prefix else "*.yaml"
        files = sorted(snap_dir.glob(glob_pat), key=lambda f: f.stat().st_mtime, reverse=True)
        result = []
        for f in files[:limit]:
            parts = f.stem.split("_", 3)
            result.append({
                "id": f.stem,
                "prefix": parts[0] if parts else "",
                "ts": "_".join(parts[1:3]) if len(parts) >= 3 else "",
                "actor": parts[3] if len(parts) > 3 else "",
                "size": f.stat().st_size,
                "mtime": int(f.stat().st_mtime),
            })
        return {"snapshots": result, "total": len(files)}

    @app.get("/api/trigger-decisions")
    async def api_trigger_decisions(request: Request, limit: int = 50):
        """读取最近的触发器决策日志（JSON lines）"""
        _api_auth(request)
        limit = max(1, min(200, limit))
        log_path = Path("logs/trigger_decisions.log")
        if not log_path.exists():
            return {"decisions": [], "total": 0}

        def _read_tail():
            try:
                file_size = log_path.stat().st_size
                read_size = min(file_size, 256 * 1024)
                with open(log_path, "rb") as f:
                    if file_size > read_size:
                        f.seek(file_size - read_size)
                    raw = f.read()
                tail_text = raw.decode("utf-8", errors="ignore")
                lines = tail_text.splitlines()
                decisions = []
                for line in reversed(lines):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        decisions.append(obj)
                        if len(decisions) >= limit:
                            break
                    except (json.JSONDecodeError, ValueError):
                        pass
                return {"decisions": decisions, "total": file_size // 120}
            except Exception:
                return {"decisions": [], "total": 0}

        return await asyncio.to_thread(_read_tail)
