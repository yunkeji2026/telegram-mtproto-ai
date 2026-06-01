"""设置 / 回复逻辑 / 意图关键词 路由 — 从 admin.py 抽出（Phase E1 批 4）。

复用 AdminRouteContext（批 3 引入）——本批**零新增 ctx 字段**，验证容器设计到位。

端点（与抽出前逐行一致）：
  GET  /settings                         POST /api/settings/save
  GET  /api/reply-logic                  POST /api/reply-logic
  GET  /api/settings/intent-keywords     PUT  /api/settings/intent-keywords
  POST /api/settings/test-intent         GET  /api/settings/test-webhook
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)


def register_settings_routes(app, ctx):
    from src.web.admin import templates, invalidate_schedule_status_cache

    config_manager = ctx.config_manager
    audit_store = ctx.audit_store
    telegram_client = ctx.telegram_client
    _api_auth = ctx.api_auth
    _api_write = ctx.api_write
    _require_role = ctx.require_role

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        _require_role(request, "settings")
        cfg = config_manager.config or {}
        ai  = cfg.get("ai", {})
        wb  = cfg.get("web_admin", {})
        tg  = cfg.get("telegram", {})
        notif = cfg.get("notifications", cfg.get("webhook", {}))
        he = cfg.get("human_escalation", {})
        try:
            he_agents_json = json.dumps(he.get("agents", []), ensure_ascii=False, indent=2)
        except Exception:
            he_agents_json = "[]"
        try:
            he_work_hours_json = json.dumps(he.get("work_hours", {}), ensure_ascii=False, indent=2)
        except Exception:
            he_work_hours_json = "{}"
        try:
            he_work_exceptions_json = json.dumps(he.get("work_exceptions", {}), ensure_ascii=False, indent=2)
        except Exception:
            he_work_exceptions_json = "{}"
        try:
            he_agent_teams_json = json.dumps(he.get("agent_teams", []), ensure_ascii=False, indent=2)
        except Exception:
            he_agent_teams_json = "[]"
        return templates.TemplateResponse(request, "settings.html", {
            "ai": ai, "wb": wb, "tg": tg, "notif": notif, "he": he,
            "he_agents_json": he_agents_json,
            "he_work_hours_json": he_work_hours_json,
            "he_work_exceptions_json": he_work_exceptions_json,
            "he_agent_teams_json": he_agent_teams_json,
        })

    @app.post("/api/settings/save")
    async def api_settings_save(request: Request, _=Depends(_api_write("manage_settings"))):
        body = await request.json()
        section = body.get("section", "")
        fields  = body.get("fields", {})
        if not section or not fields:
            raise HTTPException(400, "section 和 fields 不能为空")

        allowed_sections = {"ai", "voice_ai", "web_admin", "telegram", "notifications", "human_escalation"}
        if section not in allowed_sections:
            raise HTTPException(400, f"不允许修改 section: {section}")

        cfg = config_manager.config
        if cfg is None:
            cfg = {}
            config_manager.config = cfg
        updated = []
        if section == "voice_ai":
            mr = cfg.get("messenger_rpa")
            if not isinstance(mr, dict):
                mr = {}
                cfg["messenger_rpa"] = mr
            vo = mr.get("voice_output")
            if not isinstance(vo, dict):
                vo = {}
                mr["voice_output"] = vo
            vp = vo.get("voice_profile")
            if not isinstance(vp, dict):
                vp = {}
                vo["voice_profile"] = vp
            if "dashscope_api_key" in fields:
                key = str(fields.get("dashscope_api_key") or "").strip()
                if key:
                    vo["dashscope_api_key"] = key
                    updated.append("dashscope_api_key")
            simple_map = {
                "enabled": ("enabled", bool),
                "mode": ("mode", str),
                "trigger": ("trigger", str),
                "backend": ("backend", str),
                "voice": ("voice", str),
                "model": ("model", str),
                "format": ("format", str),
                "region": ("dashscope_region", str),
                "voice_profile_path": ("voice_profile_path", str),
                "send_audit_dir": ("send_audit_dir", str),
            }
            for src, (dst, caster) in simple_map.items():
                if src not in fields:
                    continue
                val = fields.get(src)
                if caster is bool:
                    val = bool(val)
                else:
                    val = str(val or "").strip()
                    if val == "" and dst not in ("voice_profile_path",):
                        continue
                vo[dst] = val
                updated.append(dst)
            for src, dst in (
                ("speaker_id", "speaker_id"),
                ("reference_audio_path", "reference_audio_path"),
            ):
                if src in fields and str(fields.get(src) or "").strip():
                    vp[dst] = str(fields.get(src) or "").strip()
                    updated.append(f"voice_profile.{dst}")
            vp["enabled"] = bool(fields.get("profile_enabled", vp.get("enabled", True)))
            vp["owner_consent"] = bool(fields.get("owner_consent", vp.get("owner_consent", True)))
            vp["backend"] = "voice_clone_command"
            region = str(vo.get("dashscope_region") or fields.get("region") or "cn").strip() or "cn"
            profile_path = str(
                vo.get("voice_profile_path")
                or fields.get("voice_profile_path")
                or "D:/workspace/telegram-mtproto-ai/voice_samples/qwen_my_voice.json"
            ).strip()
            vo["backend"] = "voice_clone_command"
            vo["voice"] = str(fields.get("voice") or vo.get("voice") or "my_voice").strip()
            vo["model"] = str(fields.get("model") or vo.get("model") or "qwen3-tts-vc-2026-01-22").strip()
            vo["format"] = str(fields.get("format") or vo.get("format") or "wav").strip()
            vp["command_args"] = [
                "python",
                "D:/workspace/telegram-mtproto-ai/tools/qwen_tts_wrapper.py",
                "--region", region,
                "--text", "{text}",
                "--out", "{out}",
                "--voice-profile", profile_path,
                "--language-type", "Japanese",
            ]
            config_manager.save()
            try:
                from src.ai.tts_pipeline import reset_tts_pipeline
                reset_tts_pipeline()
            except Exception:
                logger.debug("reset_tts_pipeline failed", exc_info=True)
            return {"ok": True, "updated": sorted(set(updated))}
        if section not in cfg:
            cfg[section] = {}

        # 逐字段更新（跳过空值以防止意外清空）
        updated = []
        if section == "human_escalation":
            if "agents_json" in fields:
                raw = fields.get("agents_json")
                try:
                    parsed = json.loads(raw) if (raw and str(raw).strip()) else []
                except json.JSONDecodeError as e:
                    raise HTTPException(400, f"agents_json 格式错误: {e}")
                if not isinstance(parsed, list):
                    raise HTTPException(400, "agents_json 必须是 JSON 数组")
                cfg[section]["agents"] = parsed
                updated.append("agents")
            if "work_hours_json" in fields:
                raw = fields.get("work_hours_json")
                try:
                    parsed = json.loads(raw) if (raw and str(raw).strip()) else {}
                except json.JSONDecodeError as e:
                    raise HTTPException(400, f"work_hours_json 格式错误: {e}")
                if not isinstance(parsed, dict):
                    raise HTTPException(400, "work_hours_json 必须是 JSON 对象")
                cfg[section]["work_hours"] = parsed
                updated.append("work_hours")
            if "work_exceptions_json" in fields:
                raw = fields.get("work_exceptions_json")
                try:
                    parsed = json.loads(raw) if (raw and str(raw).strip()) else {}
                except json.JSONDecodeError as e:
                    raise HTTPException(400, f"work_exceptions_json 格式错误: {e}")
                if not isinstance(parsed, dict):
                    raise HTTPException(400, "work_exceptions_json 必须是 JSON 对象")
                cfg[section]["work_exceptions"] = parsed
                updated.append("work_exceptions")
            if "agent_teams_json" in fields:
                raw = fields.get("agent_teams_json")
                try:
                    parsed = json.loads(raw) if (raw and str(raw).strip()) else []
                except json.JSONDecodeError as e:
                    raise HTTPException(400, f"agent_teams_json 格式错误: {e}")
                if not isinstance(parsed, list):
                    raise HTTPException(400, "agent_teams_json 必须是 JSON 数组")
                cfg[section]["agent_teams"] = parsed
                updated.append("agent_teams")

        _ftg_fields = {}
        for k, v in fields.items():
            if section == "human_escalation" and k in (
                "agents_json", "work_hours_json", "work_exceptions_json", "agent_teams_json",
            ):
                continue
            if section == "human_escalation" and k.startswith("forward_to_group_"):
                _ftg_fields[k.replace("forward_to_group_", "")] = v
                continue
            # API Key 等敏感字段若为空白则跳过（保留原值）
            if isinstance(v, str) and v.strip() == "" and k in ("api_key", "secret_key", "auth_token", "webhook_url"):
                continue
            # 数值转换
            if isinstance(v, str) and v.strip().lstrip("-").replace(".", "", 1).isdigit():
                try:
                    v = float(v) if "." in v else int(v)
                except ValueError:
                    pass
            # 布尔转换
            if isinstance(v, str) and v.lower() in ("true", "false"):
                v = v.lower() == "true"
            if section == "human_escalation" and k == "human_user_id":
                if isinstance(v, str) and v.strip() == "":
                    v = 0
                elif v is None:
                    v = 0
            cfg[section][k] = v
            updated.append(k)

        if section == "human_escalation" and _ftg_fields:
            ftg = cfg[section].get("forward_to_group")
            if not isinstance(ftg, dict):
                ftg = {}
            enabled_val = _ftg_fields.get("enabled")
            if isinstance(enabled_val, str):
                enabled_val = enabled_val.lower() == "true"
            ftg["enabled"] = bool(enabled_val) if enabled_val is not None else ftg.get("enabled", False)
            if "id" in _ftg_fields:
                ftg["group_id"] = str(_ftg_fields["id"]).strip()
            if "link" in _ftg_fields:
                ftg["group_link"] = str(_ftg_fields["link"]).strip()
            cfg[section]["forward_to_group"] = ftg
            updated.append("forward_to_group")

        try:
            config_manager.save()
        except Exception as e:
            raise HTTPException(500, f"配置保存失败: {e}")

        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "save_settings",
                            f"section={section}", "", f"fields={updated}")
        if section == "human_escalation" and telegram_client:
            h = getattr(telegram_client, "_human_escalation", None)
            if h:
                try:
                    h.reload_config(config_manager.config or {})
                except Exception:
                    pass
        if section == "human_escalation":
            invalidate_schedule_status_cache()
        return {"ok": True, "updated": updated}

    # ── 回复逻辑管理 API ──────────────────────────────────────
    @app.get("/api/reply-logic")
    async def api_reply_logic_get(request: Request, _=Depends(_api_auth)):
        cfg = config_manager.config or {}
        tg = cfg.get("telegram", {})
        trigger_cfg = cfg.get("trigger", {})
        reply_logic = tg.get("reply_logic", {})
        group_reply = tg.get("group_reply", {})

        trigger_rules = {}
        try:
            tr_path = trigger_cfg.get("config_file", "config/trigger_rules.yaml")
            import yaml as _yaml
            full_path = Path(config_manager.config_path).parent.parent / tr_path \
                if hasattr(config_manager, "config_path") else Path(tr_path)
            if full_path.exists():
                with open(full_path, "r", encoding="utf-8") as f:
                    trigger_rules = _yaml.safe_load(f) or {}
        except Exception:
            pass

        l2_threshold = (trigger_rules.get("l2_semantic_trigger", {})
                        .get("confidence_thresholds", {})
                        .get("reply_threshold", 0.75))
        l3_cooldown = (trigger_rules.get("l3_context_filter", {})
                       .get("cooldown", {})
                       .get("default_cooldown", 90))

        return {
            "trigger_enabled": trigger_cfg.get("enabled", False),
            "group_reply_mode": group_reply.get("mode", "mention_or_keyword"),
            "reply_chain_enabled": reply_logic.get("reply_chain", {}).get("enabled", True),
            "follow_up_enabled": reply_logic.get("follow_up", {}).get("enabled", True),
            "follow_up_lookback": reply_logic.get("follow_up", {}).get("lookback_count", 10),
            "session_window_enabled": reply_logic.get("session_window", {}).get("enabled", True),
            "session_window_minutes": reply_logic.get("session_window", {}).get("reply_within_minutes", 45),
            "l2_fallback_enabled": reply_logic.get("l2_fallback", {}).get("enabled", True),
            "ai_context_reply_enabled": reply_logic.get("ai_context_reply", {}).get("enabled", True),
            "l2_reply_threshold": l2_threshold,
            "l3_cooldown_seconds": l3_cooldown,
            "group_reply_keywords": group_reply.get("keywords", []),
        }

    @app.post("/api/reply-logic")
    async def api_reply_logic_save(request: Request, _=Depends(_api_write("manage_settings"))):
        body = await request.json()
        cfg = config_manager.config
        if cfg is None:
            cfg = {}
            config_manager.config = cfg
        if "telegram" not in cfg:
            cfg["telegram"] = {}
        tg = cfg["telegram"]
        if "reply_logic" not in tg:
            tg["reply_logic"] = {}
        rl = tg["reply_logic"]
        if "group_reply" not in tg:
            tg["group_reply"] = {}
        gr = tg["group_reply"]
        if "trigger" not in cfg:
            cfg["trigger"] = {}

        updated = []

        if "trigger_enabled" in body:
            cfg["trigger"]["enabled"] = bool(body["trigger_enabled"])
            updated.append("trigger.enabled")

        if "group_reply_mode" in body:
            gr["mode"] = body["group_reply_mode"]
            updated.append("group_reply.mode")

        for sub_key, cfg_path in [
            ("reply_chain_enabled", ("reply_chain", "enabled")),
            ("follow_up_enabled", ("follow_up", "enabled")),
            ("session_window_enabled", ("session_window", "enabled")),
            ("l2_fallback_enabled", ("l2_fallback", "enabled")),
            ("ai_context_reply_enabled", ("ai_context_reply", "enabled")),
        ]:
            if sub_key in body:
                section_name, field = cfg_path
                if section_name not in rl:
                    rl[section_name] = {}
                rl[section_name][field] = bool(body[sub_key])
                updated.append(f"reply_logic.{section_name}.{field}")

        if "follow_up_lookback" in body:
            if "follow_up" not in rl:
                rl["follow_up"] = {}
            rl["follow_up"]["lookback_count"] = int(body["follow_up_lookback"])
            updated.append("reply_logic.follow_up.lookback_count")

        if "session_window_minutes" in body:
            if "session_window" not in rl:
                rl["session_window"] = {}
            rl["session_window"]["reply_within_minutes"] = int(body["session_window_minutes"])
            updated.append("reply_logic.session_window.reply_within_minutes")

        # trigger_rules.yaml 更新
        tr_updated = False
        if "l2_reply_threshold" in body or "l3_cooldown_seconds" in body:
            try:
                import yaml as _yaml
                tr_path_str = cfg.get("trigger", {}).get("config_file", "config/trigger_rules.yaml")
                full_path = Path(config_manager.config_path).parent.parent / tr_path_str \
                    if hasattr(config_manager, "config_path") else Path(tr_path_str)
                if full_path.exists():
                    with open(full_path, "r", encoding="utf-8") as f:
                        tr = _yaml.safe_load(f) or {}

                    if "l2_reply_threshold" in body:
                        if "l2_semantic_trigger" not in tr:
                            tr["l2_semantic_trigger"] = {}
                        if "confidence_thresholds" not in tr["l2_semantic_trigger"]:
                            tr["l2_semantic_trigger"]["confidence_thresholds"] = {}
                        tr["l2_semantic_trigger"]["confidence_thresholds"]["reply_threshold"] = \
                            round(float(body["l2_reply_threshold"]), 2)
                        updated.append("trigger_rules.l2_reply_threshold")
                        tr_updated = True

                    if "l3_cooldown_seconds" in body:
                        if "l3_context_filter" not in tr:
                            tr["l3_context_filter"] = {}
                        if "cooldown" not in tr["l3_context_filter"]:
                            tr["l3_context_filter"]["cooldown"] = {}
                        tr["l3_context_filter"]["cooldown"]["default_cooldown"] = \
                            int(body["l3_cooldown_seconds"])
                        updated.append("trigger_rules.l3_cooldown")
                        tr_updated = True

                    if tr_updated:
                        with open(full_path, "w", encoding="utf-8") as f:
                            _yaml.dump(tr, f, allow_unicode=True, default_flow_style=False,
                                       sort_keys=False)
            except Exception as e:
                logger.warning("更新 trigger_rules.yaml 失败: %s", e)

        # reload four_layer_trigger if threshold/cooldown changed
        if tr_updated and telegram_client:
            flt = getattr(telegram_client, "four_layer_trigger", None)
            if flt and hasattr(flt, "reload_config"):
                try:
                    flt.reload_config()
                except Exception:
                    pass

        try:
            config_manager.save()
        except Exception as e:
            raise HTTPException(500, f"配置保存失败: {e}")

        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "save_reply_logic", "", "", f"fields={updated}")
        return {"ok": True, "updated": updated}

    # ── 意图关键词管理（M3 热更新） ──────────────────────────
    @app.get("/api/settings/intent-keywords")
    async def api_get_intent_keywords(request: Request):
        """获取所有意图关键词配置"""
        _api_auth(request)
        cfg = config_manager.config or {}
        intent_cfg = cfg.get("intent", {})
        return {
            "keywords": intent_cfg.get("keywords", {}),
            "patterns": intent_cfg.get("patterns", {}),
        }

    @app.put("/api/settings/intent-keywords")
    async def api_update_intent_keywords(request: Request):
        """更新意图关键词配置 + 热更新 SkillManager"""
        _api_auth(request)
        body = await request.json()
        new_kw = body.get("keywords")
        if not isinstance(new_kw, dict):
            raise HTTPException(400, "keywords 必须是 {intent: [kw1, kw2, ...]} 格式")

        cfg = config_manager.config
        if "intent" not in cfg:
            cfg["intent"] = {}
        cfg["intent"]["keywords"] = new_kw
        try:
            config_manager.save()
        except Exception as e:
            raise HTTPException(500, f"保存失败: {e}")

        # 热更新 SkillManager
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm:
                sm.intent_keywords = new_kw
        actor = request.session.get("username", "web_admin")
        if audit_store:
            audit_store.log(actor, "update_intent_keywords", "",
                            "", f"intents={list(new_kw.keys())}")
        return {"ok": True, "intents": list(new_kw.keys())}

    @app.post("/api/settings/test-intent")
    async def api_test_intent(request: Request):
        """测试意图路由：输入消息，返回识别结果"""
        _api_auth(request)
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text 不能为空")
        result = {"text": text, "intent": "direct_chat", "matched_by": "none"}
        if telegram_client:
            sm = getattr(telegram_client, "skill_manager", None)
            if sm and hasattr(sm, "_recognize_intent"):
                intent = sm._recognize_intent(text)
                result["intent"] = intent
                result["matched_by"] = "skill_manager"
        else:
            cfg = config_manager.config or {}
            kw = cfg.get("intent", {}).get("keywords", {})
            text_lower = text.lower()
            for intent_name, keywords in kw.items():
                for k in (keywords or []):
                    if k.lower() in text_lower:
                        result["intent"] = intent_name
                        result["matched_by"] = f"keyword:{k}"
                        break
                if result["matched_by"] != "none":
                    break
        return result

    @app.get("/api/settings/test-webhook")
    async def api_settings_test_webhook(request: Request):
        """发送一条测试 Webhook 通知"""
        _api_auth(request)
        cfg = config_manager.config or {}
        url = cfg.get("notifications", {}).get("webhook_url", "")
        if not url:
            raise HTTPException(400, "未配置 Webhook URL")
        try:
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=10) as c:
                r = await c.post(url, json={
                    "type": "test",
                    "message": "AI 助手系统 Webhook 测试消息",
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                })
            return {"ok": r.status_code < 300, "status": r.status_code}
        except Exception as e:
            return {"ok": False, "error": str(e)}
