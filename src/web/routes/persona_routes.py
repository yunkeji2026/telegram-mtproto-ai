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
from src.web.web_i18n import tr

_ROLE_VIEWER = "viewer"
_ROLE_MASTER = "master"


def register_persona_routes(app, auth_dep, audit_store=None, config_manager=None):
    """Register persona management API endpoints。config_manager 用于人设持久化。"""

    # ── 启动时加载（P5-D: 三层顺序）────────────────────────────────────────────
    # 1. config.yaml::personas.profiles  — 基础层
    # 2. personas.yaml                   — 规范运营定义（git 可追蹤，新层）
    # 3. profiles_runtime.yaml           — 会话运行时覆盖（最高优先）
    # 4. bindings_runtime.yaml           — 聊天绑定
    import logging as _logging
    _plog = _logging.getLogger("ai_chat_assistant.persona_routes")
    try:
        from pathlib import Path as _Path
        from src.utils.persona_manager import PersonaManager as _PM
        _pm_init = _PM.get_instance()
        _cfg = getattr(config_manager, "config", None) or {}
        _n1 = _pm_init.load_profiles_from_config(_cfg)             # layer 1: config base
        _n2 = 0
        if config_manager:
            _n2 = _pm_init.load_personas_canonical(config_manager)  # layer 2: canonical yaml
        _cp = getattr(config_manager, "config_path", None)
        _n3 = 0
        if _cp:
            _n3 = _pm_init.load_profiles_runtime(_Path(_cp), _cfg)    # layer 3: session overrides
            _pm_init.load_chat_bindings_runtime(_Path(_cp), _cfg)     # layer 4: bindings
        # 可观测：人设加载结果 + 语音 backend 体检。历史隐性事故——加载静默失败时
        # resolve 会回落默认 TTS、发出非克隆机器音（"声音太假"），过去无任何日志。
        # 这里把"加载了几个 / 几个配了真声克隆 backend"打到日志，便于排障。
        try:
            _all = _pm_init._profile_personas or {}
            _CLONE_BACKENDS = ("minicpm_clone", "fish_speech", "coqui_http", "elevenlabs", "xtts")
            _clone = sum(
                1 for _p in _all.values()
                if str(((_p or {}).get("voice_profile") or {}).get("backend") or "").strip().lower()
                in _CLONE_BACKENDS
            )
            _plog.info(
                "人设加载完成: config=%d canonical=%d runtime=%d；共 %d 个人设，%d 个配了克隆/真声 backend",
                _n1, _n2, _n3, len(_all), _clone,
            )
        except Exception:
            pass
    except Exception as _e:
        _plog.warning(
            "人设运行时加载失败（resolve 将回落默认 TTS，可能发出非克隆机器音）: %s",
            _e, exc_info=True,
        )

    @app.get("/api/persona")
    async def api_persona_get(request: Request, chat_id: str = "",
                               _=Depends(auth_dep)):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        persona = pm.get_persona(chat_id)
        return {
            "persona": persona,
            "chat_id": chat_id,
            "is_default": chat_id == "" or not pm.has_chat_binding(str(chat_id)),
        }

    @app.get("/api/persona/bindings")
    async def api_persona_bindings(request: Request, _=Depends(auth_dep)):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        return {"bindings": pm.get_all_chat_bindings()}

    def _check_write_role(request: Request):
        """Raises 403 if session role is viewer (read-only)."""
        try:
            role = request.session.get("role", "")
        except Exception:
            role = ""
        if role == _ROLE_VIEWER:
            raise HTTPException(403, tr(request, "err.persona.readonly_no_edit"))

    def _check_master_role(request: Request):
        """Raises 403 unless session role is master."""
        try:
            role = request.session.get("role", "")
        except Exception:
            role = ""
        if role and role != _ROLE_MASTER:
            raise HTTPException(403, tr(request, "err.perm.master_only"))

    @app.post("/api/persona/bind")
    async def api_persona_bind(request: Request, _=Depends(auth_dep)):
        _check_write_role(request)
        data = await request.json()
        chat_id = data.get("chat_id")
        persona_data = data.get("persona")
        if not chat_id or not persona_data:
            raise HTTPException(400, "chat_id and persona required")
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        pm.bind_chat_persona(str(chat_id), persona_data)
        try:
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            pm.persist_chat_bindings(cm)
        except Exception:
            pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "persona_bind",
                          f"chat={chat_id} name={persona_data.get('name', '?')}")
        return {"ok": True}

    @app.post("/api/persona/unbind")
    async def api_persona_unbind(request: Request, _=Depends(auth_dep)):
        _check_write_role(request)
        data = await request.json()
        chat_id = data.get("chat_id")
        if not chat_id:
            raise HTTPException(400, "chat_id required")
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        pm.unbind_chat_persona(str(chat_id))
        try:
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            pm.persist_chat_bindings(cm)
        except Exception:
            pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "persona_unbind", f"chat={chat_id}")
        return {"ok": True}

    @app.post("/api/persona/update-default")
    async def api_persona_update_default(request: Request, _=Depends(auth_dep)):
        _check_write_role(request)
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
                                          account_persona_id: str = "",
                                          _=Depends(auth_dep)):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        prompt = pm.build_system_prompt(
            chat_id=chat_id, account_persona_id=account_persona_id
        )
        return {"prompt": prompt, "chat_id": chat_id}

    # ── Profile store CRUD ────────────────────────────────────

    @app.get("/api/personas/profiles")
    async def api_profiles_list(request: Request, tag: str = "",
                                 _=Depends(auth_dep)):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        if tag:
            matching = pm.get_profiles_by_tag(tag)
            ids = [p.get("id", "") for p in matching if p.get("id")]
            profiles = {p["id"]: p for p in matching if p.get("id")}
        else:
            ids = pm.list_profile_ids()
            profiles = {pid: pm.get_persona_by_id(pid) for pid in ids}
        return {
            "profiles": profiles,
            "ids": ids,
            "summary": pm.list_profiles_summary() if not tag else [
                s for s in pm.list_profiles_summary() if s["id"] in set(ids)
            ],
        }

    @app.get("/api/personas/profiles/{profile_id}")
    async def api_profile_get(profile_id: str, request: Request, _=Depends(auth_dep)):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        p = pm.get_persona_by_id(profile_id)
        if p is None:
            raise HTTPException(404, f"Profile '{profile_id}' not found")
        return {"profile_id": profile_id, "persona": p}

    @app.put("/api/personas/profiles/{profile_id}")
    async def api_profile_upsert(profile_id: str, request: Request, _=Depends(auth_dep)):
        _check_write_role(request)
        data = await request.json()
        persona_data = data.get("persona")
        if not persona_data or not isinstance(persona_data, dict):
            raise HTTPException(400, "persona dict required")
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        pm.upsert_profile(profile_id, persona_data)
        try:
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            pm.persist_profiles(cm)
        except Exception:
            pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "profile_upsert",
                          f"id={profile_id} name={persona_data.get('name','?')}")
        return {"ok": True, "profile_id": profile_id}

    @app.delete("/api/personas/profiles/{profile_id}")
    async def api_profile_delete(profile_id: str, request: Request, _=Depends(auth_dep)):
        _check_write_role(request)
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        existed = pm.delete_profile(profile_id)
        try:
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            pm.persist_profiles(cm)
        except Exception:
            pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "profile_delete", f"id={profile_id}")
        return {"ok": existed, "profile_id": profile_id}

    @app.get("/api/personas/profiles/{profile_id}/history")
    async def api_profile_history(profile_id: str, request: Request, _=Depends(auth_dep)):
        """Return the version history for a profile (up to last 3 saves)."""
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        history = pm.get_profile_history(profile_id)
        return {"profile_id": profile_id, "history": history, "count": len(history)}

    @app.post("/api/personas/profiles/{profile_id}/revert")
    async def api_profile_revert(profile_id: str, request: Request, _=Depends(auth_dep)):
        """Revert a profile to its previous saved version."""
        _check_write_role(request)
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        ok = pm.revert_profile(profile_id)
        if not ok:
            raise HTTPException(404, "No history available for this profile")
        try:
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            pm.persist_profiles(cm)
        except Exception:
            pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "profile_revert", f"profile_id={profile_id}")
        return {"ok": True, "profile_id": profile_id, "persona": pm.get_persona_by_id(profile_id)}

    @app.post("/api/personas/bulk-bind")
    async def api_bulk_bind(request: Request, _=Depends(auth_dep)):
        """Rebind all currently-bound chats to a target profile.

        Body: {profile_id: str, scope: str (default 'all_bindings'), dry_run: bool}
        """
        _check_write_role(request)
        data = await request.json()
        profile_id = data.get("profile_id", "")
        scope = data.get("scope", "all_bindings")
        dry_run = bool(data.get("dry_run", False))
        if not profile_id:
            raise HTTPException(400, "profile_id required")
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        try:
            result = pm.bulk_bind_by_profile(profile_id, scope=scope, dry_run=dry_run)
        except KeyError as e:
            raise HTTPException(404, str(e))
        if not dry_run:
            try:
                cm = getattr(request.app.state, "config_manager", None) or config_manager
                pm.persist_chat_bindings(cm)
            except Exception:
                pass
            actor = request.session.get("username", "web_admin")
            if audit_store:
                audit_store.log(actor, "persona_bulk_bind",
                              f"profile_id={profile_id} scope={scope} affected={result['affected']}")
        return {"ok": True, **result}

    @app.post("/api/personas/profiles/reload")
    async def api_profiles_reload(request: Request, _=Depends(auth_dep)):
        """Reload profiles from config.yaml at runtime (no restart needed)."""
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        cfg = getattr(config_manager, "config", None) or {}
        count = pm.load_profiles_from_config(cfg)
        return {"ok": True, "loaded": count}

    @app.get("/api/personas/profiles/export")
    async def api_profiles_export(request: Request, _=Depends(auth_dep)):
        """Export all profiles as a JSON list (master only)."""
        _check_master_role(request)
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        profiles = [
            dict(p, id=pid)
            for pid, p in pm._profile_personas.items()
        ]
        return {"profiles": profiles, "count": len(profiles)}

    @app.post("/api/personas/profiles/import")
    async def api_profiles_import(request: Request, _=Depends(auth_dep)):
        """Import profiles from a JSON list (master only).

        Body: {profiles: [...], mode: 'merge'|'replace'}
        merge (default): add/overwrite individual profiles, keep others.
        replace: clear all profiles then load the new list.
        """
        _check_master_role(request)
        data = await request.json()
        profiles_in = data.get("profiles")
        mode = data.get("mode", "merge")
        if not isinstance(profiles_in, list):
            raise HTTPException(400, "profiles must be a JSON array")
        if mode not in ("merge", "replace"):
            raise HTTPException(400, "mode must be 'merge' or 'replace'")
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        if mode == "replace":
            for pid in list(pm._profile_personas.keys()):
                pm.delete_profile(pid)
        imported = 0
        errors = []
        for entry in profiles_in:
            if not isinstance(entry, dict):
                errors.append("skipped non-dict entry")
                continue
            pid = str(entry.get("id") or "").strip()
            if not pid:
                errors.append("skipped entry without id")
                continue
            pm.upsert_profile(pid, entry)
            imported += 1
        try:
            cm = getattr(request.app.state, "config_manager", None) or config_manager
            pm.persist_profiles(cm)
        except Exception:
            pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "profiles_import",
                          f"mode={mode} imported={imported} errors={len(errors)}")
        return {"ok": True, "imported": imported, "mode": mode, "errors": errors}

    # ── P5-D: Canonical config sync ──────────────────────────

    @app.post("/api/personas/sync-to-config")
    async def api_personas_sync_to_config(request: Request, _=Depends(auth_dep)):
        """Push all operator-owned PM profiles to personas.yaml (canonical config).

        Only profiles WITHOUT _mrpa_source are written — keeps the file clean.
        This file is git-trackable and loaded on next startup (layer 2 in load order).
        """
        _check_master_role(request)
        from src.utils.persona_manager import PersonaManager
        import time as _t
        pm = PersonaManager.get_instance()
        cm = getattr(request.app.state, "config_manager", None) or config_manager
        if not cm or not hasattr(cm, "save_personas"):
            raise HTTPException(503, tr(request, "err.persona.save_unavailable"))
        operator_profiles = {
            pid: dict(p)
            for pid, p in pm._profile_personas.items()
            if not p.get("_mrpa_source")
        }
        data = {
            "profiles": operator_profiles,
            "updated_at": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),
        }
        ok, msg = cm.save_personas(data)
        if not ok:
            raise HTTPException(500, msg)
        # P7-A: mark synced profiles as 'canonical' source immediately
        pm.mark_profiles_canonical(list(operator_profiles.keys()))
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "personas_sync_to_config",
                          f"profiles={len(operator_profiles)}")
        return {"ok": True, "message": msg, "profiles_written": len(operator_profiles)}

    # ── P8-B: Canonical diff endpoint ────────────────────────────

    @app.get("/api/personas/profiles/{profile_id}/diff-canonical")
    async def api_profile_diff_canonical(profile_id: str, request: Request, _=Depends(auth_dep)):
        """Return a field-level diff between the PM's live version and the personas.yaml version.

        Response:
          {profile_id, has_canonical, has_pm_version, is_identical, canonical, current,
           diff: {added, removed, changed:[{field,from,to}], unchanged}}

        added   = keys in current but not in canonical (new fields added in Studio)
        removed = keys in canonical but not in current (fields removed since last sync)
        changed = keys in both but with different values
        unchanged = keys with identical values
        Internal/meta keys (_mrpa_source, id) are excluded from diff for readability.
        """
        from src.utils.persona_manager import PersonaManager
        import json as _json
        pm = PersonaManager.get_instance()
        cm = getattr(request.app.state, "config_manager", None) or config_manager

        current = pm.get_persona_by_id(profile_id)
        canonical: dict = {}
        try:
            if cm and hasattr(cm, "get_personas_config"):
                _pdata = cm.get_personas_config()
                canonical = ((_pdata or {}).get("profiles") or {}).get(profile_id) or {}
        except Exception:
            pass

        _SKIP = {"_mrpa_source"}
        _c_keys = {k for k in (canonical or {}) if k not in _SKIP}
        _p_keys = {k for k in (current or {}) if k not in _SKIP}

        added: dict = {}
        removed: dict = {}
        changed: list = []
        unchanged: list = []

        if current and canonical:
            for k in _p_keys - _c_keys:
                added[k] = (current or {}).get(k)
            for k in _c_keys - _p_keys:
                removed[k] = canonical.get(k)
            for k in _c_keys & _p_keys:
                cv = canonical.get(k)
                pv = (current or {}).get(k)
                # Compare via JSON serialisation to handle nested dicts/lists
                if _json.dumps(cv, sort_keys=True, ensure_ascii=False) == \
                   _json.dumps(pv, sort_keys=True, ensure_ascii=False):
                    unchanged.append(k)
                else:
                    changed.append({"field": k, "from": cv, "to": pv})

        is_identical = (not added and not removed and not changed)

        return {
            "profile_id": profile_id,
            "has_canonical": bool(canonical),
            "has_pm_version": bool(current),
            "is_identical": is_identical,
            "canonical": canonical if canonical else None,
            "current": dict(current) if current else None,
            "diff": {
                "added": added,
                "removed": removed,
                "changed": changed,
                "unchanged": unchanged,
            },
        }

    # ── P9-A: Cross-platform binding list for a specific profile ─

    @app.get("/api/personas/profiles/{profile_id}/bindings")
    async def api_profile_bindings(profile_id: str, request: Request, _=Depends(auth_dep)):
        """List all chats bound to a specific profile with platform detection.

        Response:
          {profile_id, total, by_platform: {tg_private, tg_group, line, mrpa, wa, other},
           bindings: [{chat_id, platform, binding_type}]}

        binding_type: 'reference' (P4 binding_ref) | 'inline' (legacy snapshot)
        """
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()

        def _detect_platform(cid: str) -> str:
            c = str(cid).lower()
            if c.startswith("line_rpa:") or c.startswith("line:"):
                return "line"
            if c.startswith("mrpa:") or c.startswith("messenger:"):
                return "mrpa"
            if c.startswith("wa:") or c.startswith("whatsapp:"):
                return "wa"
            # Telegram: group/channel = large negative int, private = positive int
            try:
                n = int(cid)
                return "tg_group" if n < 0 else "tg_private"
            except ValueError:
                return "other"

        bindings: list = []

        # Reference bindings (P4) — explicit chat_id → profile_id map
        for cid, pid in pm._chat_bindings.items():
            if str(pid) == str(profile_id):
                bindings.append({
                    "chat_id": cid,
                    "platform": _detect_platform(cid),
                    "binding_type": "reference",
                })

        # Inline / legacy snapshot bindings
        for cid, persona in pm._chat_personas.items():
            if str(persona.get("id", "")) == str(profile_id) and \
               cid not in pm._chat_bindings:
                bindings.append({
                    "chat_id": cid,
                    "platform": _detect_platform(cid),
                    "binding_type": "inline",
                })

        by_platform: dict = {}
        for b in bindings:
            p = b["platform"]
            by_platform[p] = by_platform.get(p, 0) + 1

        bindings.sort(key=lambda x: (x["platform"], x["chat_id"]))
        return {
            "profile_id": profile_id,
            "total": len(bindings),
            "by_platform": by_platform,
            "bindings": bindings,
        }

    # ── P9-B: System prompt preview for a profile ─────────────

    @app.get("/api/personas/profiles/{profile_id}/prompt-preview")
    async def api_profile_prompt_preview(
        profile_id: str,
        request: Request,
        detail: str = "full",
        _=Depends(auth_dep),
    ):
        """Return the assembled persona instruction block for a profile.

        ?detail=full   → full _format_persona_instructions output
        ?detail=compact → compact (token-optimised) variant
        ?detail=both    → both variants + character counts

        This is a debug/preview endpoint — no chat context, no domain prompt,
        no KB context. Shows exactly what the persona contributes to the LLM prompt.
        """
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        profile = pm.get_persona_by_id(profile_id)
        if profile is None:
            raise HTTPException(404, f"profile '{profile_id}' not found")

        full_text = pm._format_persona_instructions(profile)
        compact_text = pm._format_persona_compact(profile)

        if detail == "compact":
            return {
                "profile_id": profile_id,
                "profile_name": profile.get("name", profile_id),
                "detail": "compact",
                "prompt": compact_text,
                "char_count": len(compact_text),
            }
        if detail == "both":
            return {
                "profile_id": profile_id,
                "profile_name": profile.get("name", profile_id),
                "detail": "both",
                "full": full_text,
                "compact": compact_text,
                "full_chars": len(full_text),
                "compact_chars": len(compact_text),
            }
        # default: full
        return {
            "profile_id": profile_id,
            "profile_name": profile.get("name", profile_id),
            "detail": "full",
            "prompt": full_text,
            "char_count": len(full_text),
        }

    # ── P10-B: WhatsApp account persona assignment ────────────────

    @app.post("/api/personas/wa-account/{account_id}/assign-profile")
    async def api_wa_assign_profile(account_id: str, request: Request, _=Depends(auth_dep)):
        """Assign a persona profile to a WhatsApp RPA account.

        Body: {"profile_id": "..."}

        Mutates whatsapp_rpa.accounts[].persona_ids in config.yaml, then hot-reloads
        the matching WhatsAppRpaService runner so the change takes effect immediately.
        """
        _check_master_role(request)
        body = await request.json()
        profile_id = str(body.get("profile_id") or "").strip()

        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        if profile_id and pm.get_persona_by_id(profile_id) is None:
            raise HTTPException(404, f"profile '{profile_id}' not found in PersonaManager")

        cm = getattr(request.app.state, "config_manager", None) or config_manager
        wa_cfg: dict = (getattr(cm, "config", None) or {}).get("whatsapp_rpa") or {}
        if not wa_cfg:
            raise HTTPException(404, "whatsapp_rpa config not found")

        pids = [profile_id] if profile_id else []  # 空 = 清除（回默认人设）
        # Mutate in-memory config: find account or fallback to top-level
        accounts: list = wa_cfg.get("accounts") or []
        matched = False
        for acc in accounts:
            aid = str(acc.get("id") or acc.get("account_id") or acc.get("adb_serial") or "")
            if aid == account_id:
                acc["persona_ids"] = pids
                matched = True
                break

        if not matched:
            if account_id in ("default", ""):
                # Single-account mode: set top-level persona_ids
                wa_cfg["persona_ids"] = pids
                matched = True

        if not matched:
            raise HTTPException(404, f"WA account '{account_id}' not found in config")

        # Persist to config.yaml
        cm.config["whatsapp_rpa"] = wa_cfg
        saved = cm.save()

        # Hot-reload matching service runner (best-effort)
        reloaded = False
        try:
            _wa_svcs = getattr(request.app.state, "whatsapp_rpa_services", None) or []
            for svc in _wa_svcs:
                svc_aid = str(getattr(svc, "account_id", "default") or "")
                if svc_aid == account_id or (account_id == "default" and not svc_aid):
                    # Merge updated account config into merged config
                    _merged = svc.effective_config()
                    _merged["persona_ids"] = pids
                    svc.reconfigure(_merged)
                    reloaded = True
        except Exception as _e:
            pass  # non-fatal; config is already persisted

        return {
            "ok": True,
            "account_id": account_id,
            "profile_id": profile_id,
            "config_saved": saved,
            "runner_hot_reloaded": reloaded,
        }

    # ── 统一账号人设绑定：Telegram ────────────────────────────────
    @app.post("/api/personas/tg-account/{account_id}/assign-profile")
    async def api_tg_assign_profile(account_id: str, request: Request, _=Depends(auth_dep)):
        """给 Telegram 账号指定/更换/清除人设 profile。

        Body: {"profile_id": "..."}  —— profile_id 为空表示清除（回默认人设）。
        写 telegram.accounts[].persona_ids；单账号(default) 写扁平 telegram.persona_ids。
        """
        _check_write_role(request)
        body = await request.json()
        profile_id = str(body.get("profile_id") or "").strip()
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        if profile_id and pm.get_persona_by_id(profile_id) is None:
            raise HTTPException(404, f"profile '{profile_id}' not found")

        cm = getattr(request.app.state, "config_manager", None) or config_manager
        if not cm:
            raise HTTPException(503, tr(request, "err.svc.config_manager_not_ready"))
        tg_cfg: dict = (getattr(cm, "config", None) or {}).get("telegram") or {}
        pids = [profile_id] if profile_id else []

        accounts = tg_cfg.get("accounts")
        matched = False
        if isinstance(accounts, list) and accounts:
            for acc in accounts:
                aid = str(acc.get("id") or acc.get("account_id") or "").strip()
                if aid == account_id:
                    acc["persona_ids"] = pids
                    matched = True
                    break
        if not matched and (account_id in ("default", "") or not accounts):
            # 单账号 / default：写扁平槽（注册表 default 分支已支持读取）
            tg_cfg["persona_ids"] = pids
            matched = True
        if not matched:
            raise HTTPException(404, f"TG account '{account_id}' not found")

        cm.config["telegram"] = tg_cfg
        saved = cm.save()
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "tg_assign_profile",
                          f"account={account_id} profile={profile_id or '(cleared)'}")
        return {"ok": True, "account_id": account_id, "profile_id": profile_id,
                "config_saved": saved}

    # ── 统一账号人设绑定：Messenger RPA ───────────────────────────
    @app.post("/api/personas/mrpa-account/{account_id}/assign-profile")
    async def api_mrpa_assign_profile(account_id: str, request: Request, _=Depends(auth_dep)):
        """给 Messenger RPA 账号指定/更换/清除人设 profile。

        Body: {"profile_id": "..."}  —— 空表示清除。
        写 messenger_rpa.accounts[].persona_ids，best-effort 热重载 runner。
        """
        _check_write_role(request)
        body = await request.json()
        profile_id = str(body.get("profile_id") or "").strip()
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        if profile_id and pm.get_persona_by_id(profile_id) is None:
            raise HTTPException(404, f"profile '{profile_id}' not found")

        cm = getattr(request.app.state, "config_manager", None) or config_manager
        if not cm:
            raise HTTPException(503, tr(request, "err.svc.config_manager_not_ready"))
        mrpa_cfg: dict = (getattr(cm, "config", None) or {}).get("messenger_rpa") or {}
        pids = [profile_id] if profile_id else []

        accounts = mrpa_cfg.get("accounts") or []
        matched = False
        for acc in accounts:
            aid = str(acc.get("id") or acc.get("account_id") or acc.get("adb_serial") or "").strip()
            if aid == account_id:
                acc["persona_ids"] = pids
                matched = True
                break
        if not matched and account_id in ("default", ""):
            mrpa_cfg["persona_ids"] = pids
            matched = True
        if not matched:
            raise HTTPException(404, f"Messenger account '{account_id}' not found")

        cm.config["messenger_rpa"] = mrpa_cfg
        saved = cm.save()
        reloaded = False
        try:
            _svcs = getattr(request.app.state, "messenger_rpa_services", None) or []
            for svc in _svcs:
                svc_aid = str(getattr(svc, "account_id", "default") or "")
                if svc_aid == account_id or (account_id == "default" and not svc_aid):
                    if hasattr(svc, "effective_config") and hasattr(svc, "reconfigure"):
                        _merged = svc.effective_config()
                        _merged["persona_ids"] = pids
                        svc.reconfigure(_merged)
                        reloaded = True
        except Exception:
            pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "mrpa_assign_profile",
                          f"account={account_id} profile={profile_id or '(cleared)'}")
        return {"ok": True, "account_id": account_id, "profile_id": profile_id,
                "config_saved": saved, "runner_hot_reloaded": reloaded}

    # ── P7-C: Promote mrpa-imported profile to operator-owned ────

    @app.post("/api/personas/profiles/{profile_id}/promote")
    async def api_profile_promote(profile_id: str, request: Request, _=Depends(auth_dep)):
        """Remove _mrpa_source flag from a profile, making it operator-owned.

        After promotion the profile:
        - survives restarts (persisted to profiles_runtime.yaml)
        - shows source='studio' badge instead of 'mrpa'
        - is included in sync-to-config / persist_profiles
        - is no longer overwritten by Messenger RPA import on restart
        """
        _check_master_role(request)
        from src.utils.persona_manager import PersonaManager
        import copy as _copy
        pm = PersonaManager.get_instance()
        p = pm.get_persona_by_id(profile_id)
        if p is None:
            raise HTTPException(404, f"profile '{profile_id}' not found")
        if not p.get("_mrpa_source"):
            return {"ok": True, "message": "该 profile 已是运营人设，无需升格", "already_operator": True}
        promoted = _copy.deepcopy(p)
        promoted.pop("_mrpa_source", None)  # strip the flag
        pm.upsert_profile(profile_id, promoted, _track_history=True)
        cm = getattr(request.app.state, "config_manager", None) or config_manager
        try:
            pm.persist_profiles(cm)
        except Exception:
            pass
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "profile_promote", f"profile_id={profile_id}")
        return {"ok": True, "message": f"'{profile_id}' 已升格为运营人设（source=studio）", "profile_id": profile_id}

    # ── Runtime status summary ────────────────────────────────

    @app.get("/api/personas/status")
    async def api_personas_status(request: Request, _=Depends(auth_dep)):
        """Runtime summary: profiles, bindings, TG + Messenger account persona routing."""
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        profile_ids = pm.list_profile_ids()
        bindings = pm.get_all_chat_bindings()

        # Telegram accounts
        tg_accounts: list = []
        try:
            from src.client.telegram_account_registry import TelegramAccountRegistry
            _tg_cfg = (getattr(config_manager, "config", None) or {}).get("telegram", {})
            _reg = TelegramAccountRegistry.from_config(_tg_cfg)
            for acc in _reg.all_contexts():
                primary_pid = acc.persona_ids[0] if acc.persona_ids else ""
                tg_accounts.append({
                    "account_id": acc.account_id,
                    "label": acc.label or acc.account_id,
                    "persona_ids": acc.persona_ids,
                    "active_profile": pm.get_persona_by_id(primary_pid) if primary_pid else None,
                })
        except Exception:
            pass

        # Messenger RPA accounts
        mrpa_accounts: list = []
        mrpa_reply_profiles: list = []
        mrpa_imported_count: int = 0
        try:
            from src.integrations.messenger_rpa.account_pool import AccountRegistry
            _mrpa_cfg = (getattr(config_manager, "config", None) or {}).get("messenger_rpa", {})
            _mreg = AccountRegistry.from_config(_mrpa_cfg, config_path="")
            for ctx in _mreg.all_contexts():
                primary_pid = ctx.persona_ids[0] if ctx.persona_ids else ""
                mrpa_accounts.append({
                    "account_id": ctx.account_id,
                    "label": ctx.label or ctx.account_id,
                    "persona_ids": ctx.persona_ids,
                    "active_profile": pm.get_persona_by_id(primary_pid) if primary_pid else None,
                })
            # Count reply_profiles imported into PM (marked with _mrpa_source)
            _rp_cfg = _mrpa_cfg.get("reply_profiles") or {}
            _rp_list = _rp_cfg.get("profiles") or [] if isinstance(_rp_cfg, dict) else []
            for _rp in _rp_list:
                if not isinstance(_rp, dict):
                    continue
                _rpid = str(_rp.get("id") or _rp.get("name") or "").strip()
                if not _rpid:
                    continue
                _pm_profile = pm.get_persona_by_id(_rpid)
                _imported = _pm_profile is not None and bool(
                    _pm_profile.get("_mrpa_source")
                )
                mrpa_reply_profiles.append({
                    "id": _rpid,
                    "name": (_rp.get("persona") or {}).get("name") or _rpid,
                    "imported": _imported,
                    "editable": _imported,
                })
                if _imported:
                    mrpa_imported_count += 1
        except Exception:
            pass

        # WhatsApp RPA accounts
        wa_accounts: list = []
        try:
            _wa_cfg = (getattr(config_manager, "config", None) or {}).get("whatsapp_rpa") or {}
            if isinstance(_wa_cfg, dict) and _wa_cfg.get("enabled"):
                _wa_accs = _wa_cfg.get("accounts") or []
                if _wa_accs:
                    for _acc in _wa_accs:
                        _aid = _acc.get("account_id") or _acc.get("adb_serial", "default")
                        _pids = list(_acc.get("persona_ids") or _wa_cfg.get("persona_ids") or [])
                        primary_pid = _pids[0] if _pids else ""
                        wa_accounts.append({
                            "account_id": _aid,
                            "label": _acc.get("label") or _aid,
                            "persona_ids": _pids,
                            "active_profile": pm.get_persona_by_id(primary_pid) if primary_pid else None,
                        })
                elif _wa_cfg.get("persona_ids"):
                    _pids = list(_wa_cfg.get("persona_ids") or [])
                    primary_pid = _pids[0] if _pids else ""
                    wa_accounts.append({
                        "account_id": "default",
                        "label": "WhatsApp (单账号)",
                        "persona_ids": _pids,
                        "active_profile": pm.get_persona_by_id(primary_pid) if primary_pid else None,
                    })
        except Exception:
            pass

        # Profiles in active use (across all accounts + chat bindings)
        used_ids: set = set()
        for acc in tg_accounts + mrpa_accounts + wa_accounts:
            used_ids.update(acc["persona_ids"])
        for p in bindings.values():
            pid = p.get("id", "") if isinstance(p, dict) else ""
            if pid:
                used_ids.add(pid)

        import datetime as _dt
        lca = pm._last_changed_at
        last_changed_iso = (
            _dt.datetime.fromtimestamp(lca, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if lca else ""
        )
        try:
            _role = request.session.get("role", "")
        except Exception:
            _role = ""

        # P7-B: source breakdown + canonical sync metadata
        summary = pm.list_profiles_summary()
        source_breakdown: dict = {}
        for s in summary:
            src = s.get("source", "studio")
            source_breakdown[src] = source_breakdown.get(src, 0) + 1
        unsynced_studio_count = source_breakdown.get("studio", 0)

        canonical_last_sync = ""
        canonical_last_sync_ts: float = 0.0
        try:
            if config_manager and hasattr(config_manager, "get_personas_config"):
                _pdata = config_manager.get_personas_config()
                canonical_last_sync = (_pdata or {}).get("updated_at", "")
        except Exception:
            pass
        # Also check in-session sync timestamp for more accurate "just synced" UX
        if pm._last_canonical_sync_at:
            canonical_last_sync_ts = pm._last_canonical_sync_at

        return {
            "profile_ids": profile_ids,
            "total_profiles": len(profile_ids),
            "total_bindings": len(bindings),
            "tg_accounts": tg_accounts,
            "mrpa_accounts": mrpa_accounts,
            "mrpa_reply_profiles": mrpa_reply_profiles,
            "mrpa_imported_count": mrpa_imported_count,
            "wa_accounts": wa_accounts,
            "profiles_in_use": list(used_ids),
            "domain_persona_name": pm._domain_persona.get("name", "") if pm._domain_persona else "",
            "last_changed_at": last_changed_iso,
            "last_changed_ts": lca,
            "viewer_mode": _role == _ROLE_VIEWER,
            # P7-B: source tracking
            "source_breakdown": source_breakdown,
            "unsynced_studio_count": unsynced_studio_count,
            "canonical_last_sync": canonical_last_sync,
            "canonical_last_sync_ts": canonical_last_sync_ts,
        }

    # ── S6-RULES: global_rules API ─────────────────────────────────────────

    @app.get("/api/persona/global-rules")
    async def api_global_rules_get(request: Request, _=Depends(auth_dep)):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        rules = pm.get_global_rules()
        return {"ok": True, "rules": rules}

    @app.put("/api/persona/global-rules")
    async def api_global_rules_save(request: Request, _=Depends(auth_dep)):
        _check_write_role(request)
        data = await request.json()
        rules = data.get("rules")
        if not rules or not isinstance(rules, dict):
            raise HTTPException(400, "rules dict required")
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        ok = pm.save_global_rules(rules)
        if not ok:
            raise HTTPException(500, "Failed to save global_rules.yaml")
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "global_rules_save",
                          f"constraints={len(rules.get('reply_constraints', []))}")
        return {"ok": True}

    @app.get("/api/persona/global-rules/backups")
    async def api_global_rules_backups(request: Request, _=Depends(auth_dep)):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        return {"ok": True, "backups": pm.list_backups()}

    @app.post("/api/persona/global-rules/restore/{slot}")
    async def api_global_rules_restore(slot: int, request: Request, _=Depends(auth_dep)):
        _check_write_role(request)
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        ok = pm.restore_backup(slot)
        if not ok:
            raise HTTPException(404, f"Backup slot {slot} not found or restore failed")
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "global_rules_restore", f"slot={slot}")
        return {"ok": True, "rules": pm.get_global_rules()}

    @app.post("/api/persona/global-rules/preview")
    async def api_global_rules_preview(request: Request, _=Depends(auth_dep)):
        """Preview assembled constraint text from provided (unsaved) rules data."""
        from src.utils.persona_manager import PersonaManager
        body = await request.json()
        rules = body.get("rules")
        if not rules or not isinstance(rules, dict):
            raise HTTPException(400, "rules dict required")
        pm = PersonaManager.get_instance()
        platform = body.get("platform", "")
        text = pm.preview_constraints_text(rules, platform=platform)
        return {"ok": True, "text": text}
