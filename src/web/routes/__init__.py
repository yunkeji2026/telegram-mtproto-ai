"""
Web admin route modules — extracted from admin.py for better maintainability.

Each module provides a register_XYZ_routes(app, ...) function that mounts
a group of related endpoints.
"""

from src.web.routes.persona_routes import register_persona_routes
from src.web.routes.kb_import_routes import register_kb_import_routes

__all__ = ["register_persona_routes", "register_kb_import_routes"]
