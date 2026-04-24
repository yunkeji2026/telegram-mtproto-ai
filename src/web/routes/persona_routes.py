"""
Persona management API routes — extracted from admin.py.

Endpoints:
- GET  /api/persona           — get current persona for a chat
- GET  /api/persona/bindings  — list all chat-persona bindings
- POST /api/persona/bind      — bind a persona to a chat
- POST /api/persona/unbind    — unbind a persona from a chat
- POST /api/persona/update-default — update the default persona
- GET  /api/persona/preview-prompt — preview assembled system prompt
"""

from fastapi import Depends, HTTPException, Request


def register_persona_routes(app, auth_dep, audit_store=None, config_manager=None):
    """Register persona management API endpoints。config_manager 用于人设持久化。"""

    @app.get("/api/persona")
    async def api_persona_get(request: Request, chat_id: str = "",
                               _=Depends(auth_dep)):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        persona = pm.get_persona(chat_id)
        return {
            "persona": persona,
            "chat_id": chat_id,
            "is_default": chat_id == "" or str(chat_id) not in pm._chat_personas,
        }

    @app.get("/api/persona/bindings")
    async def api_persona_bindings(request: Request, _=Depends(auth_dep)):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        return {"bindings": pm.get_all_chat_bindings()}

    @app.post("/api/persona/bind")
    async def api_persona_bind(request: Request, _=Depends(auth_dep)):
        data = await request.json()
        chat_id = data.get("chat_id")
        persona_data = data.get("persona")
        if not chat_id or not persona_data:
            raise HTTPException(400, "chat_id and persona required")
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        pm.bind_chat_persona(str(chat_id), persona_data)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "persona_bind",
                          f"chat={chat_id} name={persona_data.get('name', '?')}")
        return {"ok": True}

    @app.post("/api/persona/unbind")
    async def api_persona_unbind(request: Request, _=Depends(auth_dep)):
        data = await request.json()
        chat_id = data.get("chat_id")
        if not chat_id:
            raise HTTPException(400, "chat_id required")
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        pm.unbind_chat_persona(str(chat_id))
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "persona_unbind", f"chat={chat_id}")
        return {"ok": True}

    @app.post("/api/persona/update-default")
    async def api_persona_update_default(request: Request, _=Depends(auth_dep)):
        data = await request.json()
        persona_data = data.get("persona")
        if not persona_data:
            raise HTTPException(400, "persona data required")
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        pm.set_domain_persona(persona_data)
        try:
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            if cm:
                pm.persist_default_persona(persona_data, cm)
        except Exception:
            pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "persona_update_default",
                          f"name={persona_data.get('name', '?')}")
        return {"ok": True}

    @app.get("/api/persona/preview-prompt")
    async def api_persona_preview_prompt(request: Request, chat_id: str = "",
                                          _=Depends(auth_dep)):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        prompt = pm.build_system_prompt(chat_id=chat_id)
        return {"prompt": prompt, "chat_id": chat_id}
