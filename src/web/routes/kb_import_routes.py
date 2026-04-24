"""
Knowledge Base import API routes — extracted from admin.py.

Endpoints:
- POST /api/kb/import      — parse document content into KB entries
- POST /api/kb/import/save — save parsed entries to KB store
"""

from pathlib import Path

from fastapi import Depends, HTTPException, Request


def register_kb_import_routes(app, auth_dep, config_manager, audit_store=None):
    """Register KB import API endpoints."""

    @app.post("/api/kb/import")
    async def api_kb_import(request: Request, _=Depends(auth_dep)):
        from src.utils.kb_importer import KBImporter
        data = await request.json()
        content = data.get("content", "")
        filename = data.get("filename", "upload")
        file_type = data.get("file_type", "txt")
        category = data.get("category", "")
        chunk_size = int(data.get("chunk_size", 500))

        if not content:
            raise HTTPException(400, "content required")

        importer = KBImporter()
        entries = importer.import_text_content(
            content=content,
            filename=filename,
            file_type=file_type,
            category=category,
            chunk_size=chunk_size,
        )
        return {"entries": entries, "count": len(entries)}

    @app.post("/api/kb/import/save")
    async def api_kb_import_save(request: Request, _=Depends(auth_dep)):
        from src.utils.kb_importer import KBImporter
        data = await request.json()
        entries = data.get("entries", [])
        if not entries:
            raise HTTPException(400, "no entries to save")

        kb = None
        try:
            from src.utils.kb_store import KnowledgeBaseStore
            kb_path = Path(config_manager.config_path).parent / "knowledge_base.db"
            if kb_path.exists():
                kb = KnowledgeBaseStore(kb_path)
        except Exception:
            pass

        if not kb:
            raise HTTPException(503, "KB store not available")

        importer = KBImporter(kb_store=kb)
        ok, err = importer.save_entries_to_kb(entries)
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "kb_import", f"saved={ok} errors={err}")
        return {"ok": True, "saved": ok, "errors": err}
