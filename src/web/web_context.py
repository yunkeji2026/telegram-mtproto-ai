"""
WebContext — shared dependency container passed to domain web route plugins.

Domain packs that declare `web.routes: true` in their manifest should provide
a `register_routes(ctx: WebContext, app: FastAPI)` function in
`domains/<name>/web/routes.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates


@dataclass
class WebContext:
    """Bundles shared dependencies that domain route plugins need."""

    config_manager: Any
    audit_store: Any
    event_tracker: Any
    templates: "Jinja2Templates"
    user_store: Any

    page_auth: Any = None
    api_auth: Any = None
    api_write_factory: Optional[Callable] = None

    auto_snapshot: Optional[Callable] = None
    broadcast_config_reload: Optional[Callable] = None
    fire_webhook: Optional[Callable] = None
    sync_domain_exchange_rates: Optional[Callable] = None

    domain_name: str = ""
    domain_web_pages: list = field(default_factory=list)


@dataclass
class AdminRouteContext:
    """Phase E1：admin.py 路由拆分用的依赖容器。

    把 create_app 内反复出现的核心闭包/单例打包，避免每个 register_*_routes
    都 thread 8-10 个 kwargs（rule of three：批 3 起采用）。闭包在 create_app
    内定义完毕后构造本 ctx 再传入各 register。
    """

    config_manager: Any
    audit_store: Any = None
    telegram_client: Any = None
    user_store: Any = None
    token: str = ""

    # 鉴权闭包（在 create_app 内定义）
    page_auth: Optional[Callable] = None          # Depends 依赖
    api_auth: Optional[Callable] = None            # Depends 依赖 / 直接调用
    api_write: Optional[Callable] = None           # 工厂：api_write(perm) -> 依赖
    require_auth: Optional[Callable] = None        # 直接调用
    require_role: Optional[Callable] = None        # 直接调用 require_role(req, page_key)

    # 其它常用闭包
    auto_snapshot: Optional[Callable] = None       # auto_snapshot(name, content, actor)
    get_intent_display_names: Optional[Callable] = None
    fire_webhook: Optional[Callable] = None        # async fire_webhook(event, actor, target, summary)

    # 延迟挂载的单例（在 create_app 内对应对象创建后再 set，如 kb_store 在 ~2500 行才建）
    kb_store: Any = None

    # 监控/报表组用（G2）
    event_tracker: Any = None
    boot_ts: float = 0.0

    # 域包声明的 web 页面/仪表盘 widget（health-check / alert-status 等据此分支）
    domain_web_pages: list = field(default_factory=list)
    domain_dashboard_widgets: list = field(default_factory=list)

    # 页面路由用：模板引擎单例 + 实时日志缓冲（页面路由抽出后经此注入）
    templates: Any = None
    log_buffer: Any = None
