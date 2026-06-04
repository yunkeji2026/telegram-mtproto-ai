"""快照对比页面路由（Phase E1 续拆，从 admin.py 抽出）。

端点：
  GET /diff

/api/rollback 仍留在 admin.py（写权限 API，与 diff 页配套但属 API 层）。

依赖：templates / page_auth / config_manager。
"""

from __future__ import annotations

import difflib

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse


def _resolve_current_file(config_manager, prefix: str):
    """根据快照前缀找到对应的当前配置文件路径。"""
    cfg_dir = config_manager.config_path.parent
    _prefix_file_map = {
        "templates": cfg_dir / "templates.yaml",
        "exchange_rates": cfg_dir / "exchange_rates.yaml",
        "reply_strategies": cfg_dir / "reply_strategies.yaml",
        "quota": cfg_dir / "quota_rules.yaml",
    }
    for key, path in _prefix_file_map.items():
        if prefix.startswith(key):
            return path
    return None


def register_diff_page_routes(app, ctx) -> None:
    templates = ctx.templates
    _page_auth = ctx.page_auth
    config_manager = ctx.config_manager

    @app.get("/diff", response_class=HTMLResponse)
    async def diff_page(request: Request, _=Depends(_page_auth),
                        a: str = "", b: str = ""):
        cfg_dir = config_manager.config_path.parent
        snap_dir = cfg_dir / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        available = sorted([f.stem for f in snap_dir.glob("*.yaml")], reverse=True)

        diff_lines: list = []
        snap_a, snap_b = a, b

        if snap_a:
            file_a = snap_dir / f"{snap_a}.yaml"
            text_a = file_a.read_text(encoding="utf-8").splitlines() if file_a.exists() else []

            if snap_b and snap_b != "__current__":
                file_b = snap_dir / f"{snap_b}.yaml"
                text_b = file_b.read_text(encoding="utf-8").splitlines() if file_b.exists() else []
                tofile = snap_b
            else:
                prefix = snap_a.split("_")[0] if "_" in snap_a else snap_a
                current_file = _resolve_current_file(config_manager, prefix)
                text_b = (
                    current_file.read_text(encoding="utf-8").splitlines()
                    if current_file and current_file.exists()
                    else []
                )
                tofile = "当前配置"

            diff_lines = list(difflib.unified_diff(
                text_a, text_b, fromfile=snap_a, tofile=tofile, lineterm=""
            ))

        add_count = sum(1 for l in diff_lines if l.startswith('+') and not l.startswith('+++'))
        rm_count = sum(1 for l in diff_lines if l.startswith('-') and not l.startswith('---'))

        snapshots = []
        for stem in available:
            snapshots.append({"id": stem, "label": stem.replace("_", " ", 1)})

        return templates.TemplateResponse(request, "diff.html", {
            "snapshots": snapshots,
            "selected_a": snap_a, "selected_b": snap_b,
            "diff_lines": diff_lines,
            "add_count": add_count, "rm_count": rm_count,
        })
