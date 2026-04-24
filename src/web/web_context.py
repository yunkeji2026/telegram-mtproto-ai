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
