"""
Persona Manager — handles loading, binding, and prompt assembly for personas.

Supports:
- Loading persona from domain pack persona.yaml
- Per-chat persona binding (different groups use different personas)
- Dynamic system prompt assembly: persona context + domain prompt + KB context
- Runtime persona override via Web admin API
"""

import logging
import copy
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

logger = logging.getLogger("PersonaManager")

RUNTIME_PERSONA_FILENAME = "persona_runtime.yaml"
PROFILES_RUNTIME_FILENAME = "profiles_runtime.yaml"
BINDINGS_RUNTIME_FILENAME = "bindings_runtime.yaml"
GLOBAL_RULES_FILENAME = "global_rules.yaml"
_HISTORY_MAXLEN = 3  # versions kept per profile

# Default persona when none is configured
_DEFAULT_PERSONA: Dict[str, Any] = {
    "name": "Assistant",
    "role": "AI 助手",
    "personality": {
        "traits": ["友好", "专业"],
        "style": "自然聊天风格",
        "emoji_level": "moderate",
    },
    "speaking": {
        "openers": [],
        "forbidden_phrases": ["作为一个AI"],
        "reply_length": "moderate",
        "max_reply_sentences": 5,
        "language_follow": True,
    },
    "identity": {
        "deny_ai": False,
        "deny_ai_reply": "",
        "claim_human": False,
    },
    "boundaries": {
        "topics_to_avoid": [],
        "escalation_phrases": [],
    },
}


class PersonaManager:
    """Manages persona lifecycle, multi-group binding, and prompt assembly."""

    _instance: Optional["PersonaManager"] = None

    def __init__(self):
        self._default_persona: Dict[str, Any] = copy.deepcopy(_DEFAULT_PERSONA)
        self._chat_personas: Dict[str, Dict[str, Any]] = {}  # inline/snapshot bindings
        self._chat_bindings: Dict[str, str] = {}  # P4: reference bindings — chat_id → profile_id
        self._domain_persona: Optional[Dict[str, Any]] = None
        # profile store: id → persona dict
        self._profile_personas: Dict[str, Dict[str, Any]] = {}
        # version history: id → deque of {ts, persona} dicts
        self._profile_history: Dict[str, deque] = {}
        # change hooks: callables fired on every mutation
        self._change_hooks: List[Callable] = []
        # monotonic timestamp of last mutation (time.time())
        self._last_changed_at: float = 0.0
        # P6: source tracking — pid → 'config'|'canonical'|'runtime'|'studio'|'mrpa'
        self._profile_sources: Dict[str, str] = {}
        # P7: monotonic ts of last sync to personas.yaml (0 = never synced this session)
        self._last_canonical_sync_at: float = 0.0
        # S6-RULES: global_rules.yaml hot-reload cache
        self._global_rules: Optional[Dict[str, Any]] = None
        self._global_rules_mtime: float = 0.0
        self._global_rules_path: Optional[Path] = None

    @classmethod
    def get_instance(cls) -> "PersonaManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        cls._instance = None

    def set_domain_persona(self, persona_data: Dict[str, Any]):
        """Set the domain-level default persona (loaded from domain pack or runtime file)."""
        if persona_data:
            self._domain_persona = copy.deepcopy(persona_data)
            logger.info(
                "Domain persona set: name='%s', role='%s'",
                persona_data.get("name", "?"),
                persona_data.get("role", "?"),
            )

    # ── S6-RULES: global_rules.yaml hot-reload ──────────────────────────────
    def _load_global_rules(self) -> Dict[str, Any]:
        """Load global_rules.yaml with mtime-based hot-reload. Returns cached dict."""
        if self._global_rules_path is None:
            # auto-discover: same dir as persona_runtime or config/
            for candidate in [
                Path(__file__).resolve().parents[2] / "config" / GLOBAL_RULES_FILENAME,
            ]:
                if candidate.exists():
                    self._global_rules_path = candidate
                    break
            if self._global_rules_path is None:
                return {}
        p = self._global_rules_path
        if not p.exists():
            return self._global_rules or {}
        try:
            mt = p.stat().st_mtime
            if mt != self._global_rules_mtime or self._global_rules is None:
                with open(p, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                self._global_rules = data
                self._global_rules_mtime = mt
                if mt != 0:
                    logger.info("global_rules.yaml loaded (mtime=%.0f, %d constraints)",
                                mt, len(data.get("reply_constraints", [])))
        except Exception as exc:
            logger.warning("global_rules.yaml load failed: %s", exc)
        return self._global_rules or {}

    def get_global_rules(self) -> Dict[str, Any]:
        """Public accessor — returns the current global rules dict (hot-reloaded)."""
        return self._load_global_rules()

    _BACKUP_MAX = 3  # S6-RULES P2-c: keep last N backups

    def save_global_rules(self, data: Dict[str, Any]) -> bool:
        """Save global_rules.yaml with backup rotation. Returns True on success."""
        if self._global_rules_path is None:
            self._load_global_rules()  # discover path
        if self._global_rules_path is None:
            self._global_rules_path = (
                Path(__file__).resolve().parents[2] / "config" / GLOBAL_RULES_FILENAME
            )
        try:
            p = self._global_rules_path
            # S6-RULES P2-c: rotate backups before overwrite
            if p.exists():
                self._rotate_backups(p)
            with open(p, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            self._global_rules = data
            self._global_rules_mtime = p.stat().st_mtime
            logger.info("global_rules.yaml saved (%d constraints)",
                        len(data.get("reply_constraints", [])))
            return True
        except Exception as exc:
            logger.error("global_rules.yaml save failed: %s", exc)
            return False

    def _rotate_backups(self, path: Path):
        """Keep last N backups as .bak.1 (newest) … .bak.N (oldest)."""
        try:
            for i in range(self._BACKUP_MAX, 1, -1):
                older = path.with_suffix(f".yaml.bak.{i}")
                newer = path.with_suffix(f".yaml.bak.{i-1}")
                if newer.exists():
                    if older.exists():
                        older.unlink()
                    newer.rename(older)
            bak1 = path.with_suffix(".yaml.bak.1")
            if bak1.exists():
                bak1.unlink()
            import shutil
            shutil.copy2(path, bak1)
        except Exception as exc:
            logger.warning("global_rules backup rotation failed: %s", exc)

    def list_backups(self) -> list:
        """Return list of backup dicts [{slot, mtime_iso, path}, …]."""
        if self._global_rules_path is None:
            self._load_global_rules()
        if self._global_rules_path is None:
            return []
        import datetime
        result = []
        for i in range(1, self._BACKUP_MAX + 1):
            bp = self._global_rules_path.with_suffix(f".yaml.bak.{i}")
            if bp.exists():
                mt = bp.stat().st_mtime
                result.append({
                    "slot": i,
                    "mtime_iso": datetime.datetime.fromtimestamp(mt).isoformat(timespec="seconds"),
                    "path": str(bp),
                })
        return result

    def restore_backup(self, slot: int) -> bool:
        """Restore a backup by slot number. Returns True on success."""
        if self._global_rules_path is None:
            self._load_global_rules()
        if self._global_rules_path is None:
            return False
        bp = self._global_rules_path.with_suffix(f".yaml.bak.{slot}")
        if not bp.exists():
            return False
        try:
            with open(bp, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return self.save_global_rules(data)
        except Exception as exc:
            logger.error("global_rules restore from slot %d failed: %s", slot, exc)
            return False

    @staticmethod
    def _assemble_constraints(constraints: list, platform: str = "") -> str:
        """Assemble constraints list into numbered text, respecting enabled and platforms flags."""
        if not constraints:
            return ""
        _plat = (platform or "").strip().lower()
        parts = ["【回复硬约束】"]
        n = 0
        for c in constraints:
            if not c.get("enabled", True):
                continue
            # P5-a: platform scope — empty list means all platforms
            plats = c.get("platforms") or []
            if plats and _plat and _plat not in [p.strip().lower() for p in plats]:
                continue
            rule_text = c.get("rule", "").strip()
            if rule_text:
                n += 1
                parts.append(f"{n}. {rule_text}")
        if n == 0:
            return ""
        return "\n".join(parts)

    def _build_constraints_text(self, platform: str = "") -> str:
        """Build the reply constraints block from global_rules.yaml (or fallback to hardcoded)."""
        rules = self._load_global_rules()
        return self._assemble_constraints(rules.get("reply_constraints", []), platform=platform)

    def preview_constraints_text(self, rules_data: Dict[str, Any], platform: str = "") -> str:
        """Preview assembled prompt text from arbitrary rules data (for UI live preview).
        Returns all sections: constraints + all platform rules + all funnel tones.
        """
        sections = []
        # constraints
        ct = self._assemble_constraints(rules_data.get("reply_constraints", []), platform=platform)
        if ct:
            sections.append(ct)
        # platform rules
        for key, entry in (rules_data.get("platform_rules", {}) or {}).items():
            label = entry.get("label", key)
            rule = (entry.get("rule", "") or "").strip()
            if rule:
                sections.append(f"【{label}】{rule}")
        # funnel tones
        for key, entry in (rules_data.get("funnel_tone", {}) or {}).items():
            label = entry.get("label", key)
            tone = (entry.get("tone", "") or "").strip()
            if tone:
                sections.append(f"【漏斗阶段：{label}】{tone}")
        return "\n\n".join(sections)

    def _build_platform_constraints(self, platform: str) -> str:
        """Build platform-specific constraints from global_rules.yaml (or fallback)."""
        rules = self._load_global_rules()
        plat_rules = rules.get("platform_rules", {})
        _plat = (platform or "").strip().lower()
        # normalize platform aliases
        for key in ("whatsapp", "line", "messenger", "telegram"):
            if key in _plat:
                entry = plat_rules.get(key, {})
                if entry:
                    label = entry.get("label", f"{key} 约束")
                    rule = entry.get("rule", "").strip()
                    if rule:
                        return f"【{label}】{rule}"
                break
        return ""

    def _build_funnel_tone(self, funnel_stage: str) -> str:
        """Build funnel stage tone guidance from global_rules.yaml (or fallback)."""
        rules = self._load_global_rules()
        funnel = rules.get("funnel_tone", {})
        _stage = (funnel_stage or "").strip().lower()
        entry = funnel.get(_stage, {})
        if entry:
            label = entry.get("label", _stage)
            tone = entry.get("tone", "").strip()
            if tone:
                return f"【漏斗阶段：{label}】{tone}"
        return ""

    @staticmethod
    def runtime_file_path(config_path: Path, explicit: str = "") -> Path:
        """persona_runtime 文件路径（与 config.yaml 同目录，除非显式指定相对/绝对路径）。"""
        base = Path(config_path).resolve().parent
        ex = (explicit or "").strip()
        if ex:
            p = Path(ex)
            return p if p.is_absolute() else (base / p)
        return base / RUNTIME_PERSONA_FILENAME

    def load_runtime_default_persona(
        self,
        config_path: Path,
        root_config: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        若存在 persona_runtime.yaml 且启用持久化配置，则加载并覆盖当前域默认人设。
        返回是否已应用覆盖。
        """
        root_config = root_config or {}
        pp = root_config.get("persona_persistence") or {}
        if not pp.get("enabled", True):
            return False
        path = self.runtime_file_path(
            config_path, str(pp.get("path") or "")
        )
        raw = self.load_persona_file(path)
        if not raw or not isinstance(raw, dict):
            return False
        pdata = raw.get("default_persona")
        if not isinstance(pdata, dict) or not pdata:
            if "name" in raw or "role" in raw:
                pdata = raw
            else:
                return False
        self.set_domain_persona(pdata)
        logger.info("已从 %s 加载运行时人设覆盖", path.name)
        return True

    @staticmethod
    def profiles_runtime_file_path(config_path: Path, explicit: str = "") -> Path:
        """profiles_runtime 文件路径（与 config.yaml 同目录，除非显式指定）。"""
        base = Path(config_path).resolve().parent
        ex = (explicit or "").strip()
        if ex:
            p = Path(ex)
            return p if p.is_absolute() else (base / p)
        return base / PROFILES_RUNTIME_FILENAME

    def persist_profiles(
        self,
        config_manager: Any,
    ) -> bool:
        """Web 保存 profile 后写入 profiles_runtime.yaml（与 config 同目录）。"""
        if not config_manager:
            return False
        cfg_path = getattr(config_manager, "config_path", None)
        if not cfg_path:
            return False
        root = getattr(config_manager, "config", None) or {}
        pp = root.get("persona_persistence") or {}
        if not pp.get("enabled", True):
            return False
        path = self.profiles_runtime_file_path(
            Path(cfg_path), str(pp.get("profiles_path") or "")
        )
        # Serialise history: deque → plain list (maxlen=3 already limits size)
        serialised_history = {
            pid: list(entries)
            for pid, entries in self._profile_history.items()
            if entries
        }
        # P5-B: Only persist operator-owned profiles (exclude _mrpa_source auto-imports).
        # _mrpa_source profiles are re-imported from config on next startup so no need
        # to persist them — this keeps profiles_runtime.yaml clean and git-friendly.
        operator_profiles = {
            pid: copy.deepcopy(p)
            for pid, p in self._profile_personas.items()
            if not p.get("_mrpa_source")
        }
        # Only include history for operator-owned profiles
        serialised_history = {
            pid: list(entries)
            for pid, entries in self._profile_history.items()
            if entries and pid in operator_profiles
        }
        wrapper = {
            "profiles": operator_profiles,
            "_history": serialised_history,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        ok = self.save_persona_file(path, wrapper)
        if ok:
            logger.info(
                "profiles 已持久化到 %s (%d 条，已过滤 _mrpa_source 自动导入)",
                path, len(operator_profiles),
            )
        return ok

    def load_profiles_runtime(
        self,
        config_path: Path,
        root_config: Optional[Dict[str, Any]] = None,
    ) -> int:
        """若存在 profiles_runtime.yaml 且启用持久化，则加载并合并到 profile store。
        运行时 profiles 优先于 config.yaml::personas.profiles（覆盖同 id 条目）。
        返回合并的 profile 数量。
        """
        root_config = root_config or {}
        pp = root_config.get("persona_persistence") or {}
        if not pp.get("enabled", True):
            return 0
        path = self.profiles_runtime_file_path(
            config_path, str(pp.get("profiles_path") or "")
        )
        raw = self.load_persona_file(path)
        if not raw or not isinstance(raw, dict):
            return 0
        profiles_data = raw.get("profiles")
        if not isinstance(profiles_data, dict) or not profiles_data:
            return 0
        count = 0
        for pid, pdata in profiles_data.items():
            if isinstance(pdata, dict) and pid:
                self._profile_personas[str(pid)] = copy.deepcopy(pdata)
                self._profile_sources[str(pid)] = "runtime"  # P6: runtime overrides canonical
                count += 1
        # Restore history (optional section, ignore if absent/malformed)
        history_data = raw.get("_history")
        if isinstance(history_data, dict):
            for pid, entries in history_data.items():
                if isinstance(entries, list) and pid:
                    q = self._profile_history.setdefault(
                        str(pid), deque(maxlen=_HISTORY_MAXLEN)
                    )
                    for e in entries[-_HISTORY_MAXLEN:]:
                        if isinstance(e, dict):
                            q.append(copy.deepcopy(e))
        if count:
            logger.info("已从 %s 加载 %d 个运行时 profile", path.name, count)
        return count

    def load_personas_canonical(self, config_manager: Any) -> int:
        """P5-D: Load operator-curated profiles from personas.yaml (canonical layer).

        Load order:
          1. config.yaml::personas.profiles   — base layer (load_profiles_from_config)
          2. personas.yaml                    — canonical operator definitions (this method)
          3. profiles_runtime.yaml            — session overrides (load_profiles_runtime)

        personas.yaml profiles are NOT tagged with _mrpa_source so they are treated as
        operator-owned and will not be clobbered by Messenger RPA import on restart.
        Returns number of profiles loaded.
        """
        try:
            cfg = getattr(config_manager, "config", None) or {}
            pp = cfg.get("persona_persistence") or {}
            if not pp.get("enabled", True):
                return 0
            personas_data = config_manager.get_personas_config()
            if not personas_data or not isinstance(personas_data, dict):
                return 0
            profiles = personas_data.get("profiles")
            if not isinstance(profiles, dict) or not profiles:
                return 0
            count = 0
            for pid, pdata in profiles.items():
                if isinstance(pdata, dict) and pid:
                    self._profile_personas[str(pid)] = copy.deepcopy(pdata)
                    self._profile_sources[str(pid)] = "canonical"  # P6: canonical overrides config
                    count += 1
            if count:
                logger.info("已从 personas.yaml 加载 %d 个规范 profile", count)
            return count
        except Exception as e:
            logger.warning("load_personas_canonical failed: %s", e)
            return 0

    def persist_default_persona(
        self,
        persona_data: Dict[str, Any],
        config_manager: Any,
    ) -> bool:
        """Web 保存默认人设后写入 persona_runtime.yaml（与 config 同目录）。"""
        if not persona_data or not config_manager:
            return False
        cfg_path = getattr(config_manager, "config_path", None)
        if not cfg_path:
            return False
        root = getattr(config_manager, "config", None) or {}
        pp = root.get("persona_persistence") or {}
        if not pp.get("enabled", True):
            return False
        path = self.runtime_file_path(Path(cfg_path), str(pp.get("path") or ""))
        wrapper = {
            "default_persona": copy.deepcopy(persona_data),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        ok = self.save_persona_file(path, wrapper)
        if ok:
            logger.info("人设已持久化到 %s", path)
        return ok

    def bind_chat_persona(self, chat_id: str, persona_data: Dict[str, Any]):
        """Bind a specific persona to a chat (group/private)."""
        self._chat_personas[str(chat_id)] = copy.deepcopy(persona_data)
        logger.info(
            "Chat %s bound to persona '%s'",
            chat_id, persona_data.get("name", "?"),
        )
        self._fire_change_hooks("chat_bind", chat_id=chat_id, persona=persona_data)

    def unbind_chat_persona(self, chat_id: str):
        """Remove per-chat persona binding (both reference and inline), falling back to domain default."""
        cid = str(chat_id)
        self._chat_bindings.pop(cid, None)  # P4: also clear reference
        self._chat_personas.pop(cid, None)
        self._fire_change_hooks("chat_unbind", chat_id=chat_id)

    def has_chat_binding(self, chat_id: str) -> bool:
        """Return True if this chat has an explicit per-chat binding (reference or inline)."""
        if not chat_id:
            return False
        cid = str(chat_id)
        return cid in self._chat_bindings or cid in self._chat_personas  # P4: check both

    def bind_chat_persona_by_profile_id(self, chat_id: str, profile_id: str) -> bool:
        """Bind a chat to an existing profile by reference (P4: live-resolves on every lookup).

        Unlike bind_chat_persona (inline snapshot), this stores only the profile_id so that
        subsequent edits in /personas are automatically reflected without rebinding.

        Returns True if the profile was found and bound; False if profile_id unknown.
        """
        p = self.get_persona_by_id(profile_id)
        if p is None:
            return False
        cid = str(chat_id)
        self._chat_bindings[cid] = str(profile_id)  # P4: store reference, not snapshot
        self._chat_personas.pop(cid, None)  # P4: clear any stale inline snapshot
        logger.info(
            "Chat %s → profile ref id=%r (name=%r)",
            chat_id, profile_id, p.get("name", "?"),
        )
        self._fire_change_hooks("chat_bind", chat_id=chat_id, persona=p)
        return True

    def load_profiles_from_config(self, config: Dict[str, Any]) -> int:
        """Load persona profiles from config.yaml::personas.profiles into the profile store.
        Returns the number of profiles loaded. Safe to call multiple times (overwrites).
        """
        profiles = (config.get("personas") or {}).get("profiles") or []
        count = 0
        for entry in profiles:
            if not isinstance(entry, dict):
                continue
            pid = str(entry.get("id") or "").strip()
            if not pid:
                continue
            self._profile_personas[pid] = copy.deepcopy(entry)
            self._profile_sources[pid] = "config"  # P6: direct assign — reload sets 'config'
            count += 1
        if count:
            logger.info("PersonaManager: loaded %d profiles from config", count)
        return count

    def get_persona_by_id(self, profile_id: str) -> Optional[Dict[str, Any]]:
        """Look up a persona by its profile id (from personas.profiles[].id)."""
        if not profile_id:
            return None
        return self._profile_personas.get(str(profile_id))

    def list_profile_ids(self) -> List[str]:
        """Return all registered profile ids."""
        return list(self._profile_personas.keys())

    def list_profiles_summary(self) -> List[Dict[str, Any]]:
        """Return a lightweight summary list for the Studio profile browser.

        Each entry: {id, name, role, tags, has_voice, has_history, binding_count}
        """
        result = []
        for pid, p in self._profile_personas.items():
            vp = p.get("voice_profile") or {}
            bc = (
                sum(1 for cp in self._chat_personas.values() if cp.get("id") == pid)
                + sum(1 for ref_pid in self._chat_bindings.values() if ref_pid == pid)  # P4
            )
            # P6: derive source — mrpa flag takes precedence over _profile_sources
            source = "mrpa" if p.get("_mrpa_source") else self._profile_sources.get(pid, "studio")
            result.append({
                "id": pid,
                "name": p.get("name") or pid,
                "role": p.get("role") or "",
                "tags": list(p.get("tags") or []),
                "has_voice": bool(vp.get("enabled") or vp.get("voice") or vp.get("backend")),
                "has_history": bool(self._profile_history.get(pid)),
                "binding_count": bc,
                "source": source,           # P6: 'config'|'canonical'|'runtime'|'studio'|'mrpa'
                "is_mrpa_source": bool(p.get("_mrpa_source")),  # P6: convenience flag
            })
        return result

    def _profile_has_tag(self, profile_id: str, tag: str) -> bool:
        """Return True if the live profile with this ID has the given tag (case-insensitive)."""
        if not profile_id:
            return False
        profile = self._profile_personas.get(str(profile_id))
        if not profile:
            return False
        return tag.strip().lower() in [str(t).strip().lower() for t in (profile.get("tags") or [])]

    def bulk_bind_by_profile(
        self,
        profile_id: str,
        *,
        scope: str = "all_bindings",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Rebind chats/accounts to a target profile in bulk.

        scope="all_bindings" — rebinds every currently-bound chat to profile_id.
        Returns {affected: int, chat_ids: [...], dry_run: bool}.
        """
        profile = self._profile_personas.get(str(profile_id))
        if not profile:
            raise KeyError(f"profile '{profile_id}' not found")
        persona = dict(profile)

        if scope == "all_bindings":
            # P4: include both inline and reference bindings
            target_chats = list(set(list(self._chat_personas.keys()) + list(self._chat_bindings.keys())))
        elif scope.startswith("tag:"):
            filter_tag = scope[4:].strip().lower()
            target_chats = [
                cid for cid, cp in self._chat_personas.items()
                if self._profile_has_tag(cp.get("id", ""), filter_tag)
            ] + [
                cid for cid, ref_pid in self._chat_bindings.items()  # P4
                if self._profile_has_tag(ref_pid, filter_tag)
            ]
        else:
            target_chats = []

        if not dry_run:
            for cid in target_chats:
                if cid in self._chat_bindings:  # P4: update the reference
                    self._chat_bindings[cid] = str(profile_id)
                else:
                    self._chat_personas[cid] = dict(persona)  # inline: update snapshot
                self._fire_change_hooks("chat_bind", chat_id=cid, persona=persona)

        return {"affected": len(target_chats), "chat_ids": target_chats, "dry_run": dry_run}

    def get_profiles_by_tag(self, tag: str) -> List[Dict[str, Any]]:
        """Return all profiles that have the given tag (case-insensitive)."""
        tag_lo = (tag or "").strip().lower()
        if not tag_lo:
            return list(self._profile_personas.values())
        return [
            copy.deepcopy(p)
            for p in self._profile_personas.values()
            if tag_lo in [str(t).lower() for t in (p.get("tags") or [])]
        ]

    def upsert_profile(
        self, profile_id: str, persona_data: Dict[str, Any], *, _track_history: bool = True
    ) -> None:
        """Add or replace a persona profile at runtime (does not persist to disk)."""
        pid = str(profile_id)
        existing = self._profile_personas.get(pid)
        if existing and _track_history:
            q = self._profile_history.setdefault(pid, deque(maxlen=_HISTORY_MAXLEN))
            q.append({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "persona": copy.deepcopy(existing),
            })
        self._profile_personas[pid] = copy.deepcopy(persona_data)
        # P6: mrpa-tagged data keeps 'mrpa' source; operator studio saves become 'studio'
        if persona_data.get("_mrpa_source"):
            self._profile_sources[pid] = "mrpa"
        else:
            self._profile_sources[pid] = "studio"
        self._fire_change_hooks("profile_upsert", profile_id=pid, persona=persona_data)

    def mark_profiles_canonical(self, profile_ids: List[str]) -> None:
        """P7-A: After sync-to-config, mark profiles as 'canonical' source.

        Call this after a successful save_personas() so that
        list_profiles_summary() reflects the sync state immediately
        (unsynced_studio_count drops to 0 for the written profiles).
        Does NOT touch profiles with _mrpa_source — those are never synced.
        """
        import time as _t
        for pid in profile_ids:
            p = self._profile_personas.get(str(pid))
            if p and not p.get("_mrpa_source"):
                self._profile_sources[str(pid)] = "canonical"
        self._last_canonical_sync_at = _t.time()

    def delete_profile(self, profile_id: str) -> bool:
        """Remove a persona profile. Returns True if it existed."""
        pid = str(profile_id)
        existed = self._profile_personas.pop(pid, None) is not None
        if existed:
            self._profile_sources.pop(pid, None)  # P6: clean up source tracking
            self._fire_change_hooks("profile_delete", profile_id=pid)
        return existed

    def get_profile_history(self, profile_id: str) -> List[Dict[str, Any]]:
        """Return version history for a profile (oldest first, max _HISTORY_MAXLEN entries)."""
        return list(self._profile_history.get(str(profile_id), []))

    def revert_profile(self, profile_id: str) -> bool:
        """Restore the previous version of a profile. Returns False if no history."""
        pid = str(profile_id)
        q = self._profile_history.get(pid)
        if not q:
            return False
        prev = q.pop()
        self._profile_personas[pid] = prev["persona"]
        self._fire_change_hooks("profile_revert", profile_id=pid)
        return True

    # ── Change hooks ─────────────────────────────────────────

    def register_change_hook(self, fn: Callable) -> None:
        """Register a callback invoked on every profile/binding mutation.

        Signature: fn(event: str, **kwargs) where event ∈
        {'profile_upsert', 'profile_delete', 'profile_revert', 'chat_bind', 'chat_unbind'}.
        Exceptions inside fn are caught and logged.
        """
        self._change_hooks.append(fn)

    def _fire_change_hooks(self, event: str, **kwargs: Any) -> None:
        self._last_changed_at = time.time()
        for fn in self._change_hooks:
            try:
                fn(event, **kwargs)
            except Exception:
                logger.debug("[persona] change hook %r raised", fn, exc_info=True)

    def get_persona(
        self,
        chat_id: str = "",
        account_persona_id: str = "",
    ) -> Dict[str, Any]:
        """Get the effective persona with 3-tier fallback:

        1. Per-chat binding (``bind_chat_persona``)
        2. Account-level profile (``account_persona_id`` → profile store)
        3. Domain default (``set_domain_persona``) → global hardcoded default
        """
        if chat_id:
            cid = str(chat_id)
            # P4: reference binding — live-resolves to current profile data
            ref_pid = self._chat_bindings.get(cid)
            if ref_pid:
                live = self._profile_personas.get(str(ref_pid))
                if live is not None:
                    return live
                # Profile was deleted — fall through (no stale data served)
            elif cid in self._chat_personas:
                return self._chat_personas[cid]  # inline/legacy snapshot
        if account_persona_id:
            acct_p = self._profile_personas.get(str(account_persona_id))
            if acct_p:
                return acct_p
        if self._domain_persona:
            return self._domain_persona
        return self._default_persona

    _TIER_CHAT = "chat_binding"
    _TIER_ACCOUNT = "account_profile"
    _TIER_DOMAIN = "domain"
    _TIER_DEFAULT = "default"

    def get_persona_with_tier(
        self,
        chat_id: str = "",
        account_persona_id: str = "",
    ) -> tuple:
        """Same 3-tier lookup as get_persona but also returns the resolved tier label.

        Returns:
            (persona_dict, tier_str) where tier_str ∈
            {'chat_binding', 'account_profile', 'domain', 'default'}

        Zero impact on existing callers — they can keep using get_persona().
        """
        if chat_id:
            cid = str(chat_id)
            # P4: reference binding — live-resolves
            ref_pid = self._chat_bindings.get(cid)
            if ref_pid:
                live = self._profile_personas.get(str(ref_pid))
                if live is not None:
                    return live, self._TIER_CHAT
            elif cid in self._chat_personas:
                return self._chat_personas[cid], self._TIER_CHAT
        if account_persona_id:
            acct_p = self._profile_personas.get(str(account_persona_id))
            if acct_p:
                return acct_p, self._TIER_ACCOUNT
        if self._domain_persona:
            return self._domain_persona, self._TIER_DOMAIN
        return self._default_persona, self._TIER_DEFAULT

    def get_persona_name(
        self,
        chat_id: str = "",
        account_persona_id: str = "",
    ) -> str:
        return self.get_persona(chat_id, account_persona_id).get("name", "Assistant")

    def format_persona_block(
        self,
        chat_id: str = "",
        *,
        detail: str = "full",
        name_override: str = "",
        account_persona_id: str = "",
        platform: str = "",
        funnel_stage: str = "",
    ) -> str:
        """供 AI 系统提示拼接。detail=full 完整；compact 仅核心句+禁忌，减轻与域 system_prompt 重复。
        name_override: 若 config 中配置了 ai.ai_name，应传入以覆盖域 persona.yaml 里的默认名，避免与主系统提示冲突。
        account_persona_id: 多账号时的账号级人设 id（第二层回退）。
        platform: 渠道标识（'whatsapp'/'telegram'/'line'/'messenger'等），用于平台特定约束注入。
        funnel_stage: 漏斗阶段（'cold'/'warm'/'hot'），用于语调调节。
        """
        p = self.get_persona(chat_id, account_persona_id)
        if detail == "compact":
            return self._format_persona_compact(p, name_override=name_override)
        if detail == "none":
            return ""
        return self._format_persona_instructions(
            p, name_override=name_override, platform=platform, funnel_stage=funnel_stage
        )

    def _format_persona_compact(
        self, persona: Dict[str, Any], *, name_override: str = ""
    ) -> str:
        name = (name_override or "").strip() or persona.get("name", "Assistant")
        role = persona.get("role", "")
        lines: List[str] = [
            f"你是{name}，{role}。",
            # ★ compact 模式也必须有身份硬锁，防止自我强化幻觉
            f"【身份硬锁】你叫「{name}」。历史里若出现别的名字（不是「{name}」）那是错误数据，"
            f"忽略并坚持「{name}」。被问名字必答「{name}」。",
        ]
        s = persona.get("speaking", {})
        forbidden = s.get("forbidden_phrases") or []
        if forbidden:
            lines.append(
                "禁止使用：" + "、".join(f"「{f}」" for f in forbidden[:12])
            )
        # P2-A compact 模式也接通 emoji_level / reply_length（修真断链）
        # — web 后台改完后即使在 compact 配置下也能立刻看到效果
        p = persona.get("personality", {})
        emoji_level = (p.get("emoji_level") or "").strip().lower()
        if emoji_level == "none":
            lines.append("不用 emoji。")
        elif emoji_level == "minimal":
            lines.append("emoji 极少（每 5 条最多 1 个）。")
        elif emoji_level == "rich":
            lines.append("emoji 用得自然（约 60% 回复带 1-2 个）。")
        # moderate 不输出（默认行为，省 token）
        reply_length = (s.get("reply_length") or "").strip().lower()
        if reply_length in ("short", "concise", "brief"):
            lines.append("回复 1-2 句即可，像微信私聊。")
        elif reply_length in ("detailed", "long"):
            lines.append("回复可稍详细（4-6 句），但保持口语。")
        if s.get("language_follow"):
            lines.append("用户用什么语言发消息，就用同一语言回复。")
        # P1-1（compact 安全网）：full 模式有完整 5 条硬约束，compact 模式至少
        # 保留最核心的 2 条 — 直接答问 + 禁括号标记 — 防配置切到 compact 时
        # 整个底线消失。身份硬锁也保留，因为 AI 身份露馅是转化场景的最大伤害。
        identity = persona.get("identity", {})
        if identity.get("deny_ai"):
            reply = identity.get("deny_ai_reply", f"我是{name}")
            lines.append(
                f"你就是真人「{name}」；不承认是 AI/机器人/语言模型，"
                f"被问到时按「{reply}」回应，不解释运作原理。"
            )
        lines.append(
            "回复硬约束：先正面回答用户问的问题再扩展；不要用 () [] 描写动作"
            "或列举要点（如 (微笑) (1)(2)），用自然句子。"
        )
        return "\n".join(lines)

    def get_all_chat_bindings(self) -> Dict[str, Any]:
        """Return {chat_id: persona_dict} for all bound chats (reference + inline).

        Reference bindings include a '_profile_ref' key so callers can distinguish them.
        """
        result: Dict[str, Any] = {}
        for cid, p in self._chat_personas.items():
            result[cid] = copy.deepcopy(p)
        for cid, pid in self._chat_bindings.items():  # P4: reference bindings
            live = self._profile_personas.get(str(pid))
            if live is not None:
                result[cid] = {**copy.deepcopy(live), "_profile_ref": pid}
            else:
                result[cid] = {"_profile_ref": pid, "_deleted": True}
        return result

    # ── System prompt assembly ──────────────────────────────

    def build_system_prompt(
        self,
        chat_id: str = "",
        domain_prompt: str = "",
        kb_context: str = "",
        extra_context: str = "",
        account_persona_id: str = "",
    ) -> str:
        """Assemble the full system prompt from persona + domain + KB.

        Assembly order:
        1. Persona identity & style instructions
        2. Domain-specific system prompt
        3. KB context (if any)
        4. Extra context (channel status, etc.)
        """
        persona = self.get_persona(chat_id, account_persona_id)
        parts: List[str] = []

        # 1. Persona instructions
        persona_block = self._format_persona_instructions(persona)
        if persona_block:
            parts.append(persona_block)

        # 2. Domain prompt
        if domain_prompt:
            parts.append(domain_prompt)

        # 3. KB context
        if kb_context:
            parts.append(f"【知识库参考】\n{kb_context}")

        # 4. Extra context
        if extra_context:
            parts.append(extra_context)

        return "\n\n".join(parts)

    # P10-C: platform → WA-specific constraints; funnel_stage → tone guidance
    _WA_CONSTRAINTS = (
        "【WhatsApp 专用约束】"
        "回复必须简短（1-2 句），符合 WA 私聊节奏；"
        "不要使用 Markdown 格式（禁用 **粗体**、_斜体_、```代码块```、- 列表符号）；"
        "用纯文字 + emoji，不要有任何排版符号；"
        "长按消息选「回复」（引用/指定回复）是 WhatsApp 的标准功能，你会用，"
        "对方问到时自然回答「会啊」，不要说自己没有这个按钮或做不到。"
    )
    _FUNNEL_TONE = {
        "cold": (
            "【漏斗阶段：冷启动】对方尚不了解你，优先建立亲切感和信任感；"
            "以轻松问候和开放式问题引导对话，不要急于推销或深聊私密话题。"
        ),
        "warm": (
            "【漏斗阶段：暖场中】对方已有一定了解，延续话题、加深关系；"
            "可以展示共同兴趣，分享轻量个人细节，让对话自然推进。"
        ),
        "hot": (
            "【漏斗阶段：高意向】对方有明确互动意愿，把握关键时机；"
            "回复要有温度和吸引力，适时引导进一步行动（约见面/转私密话题等）。"
        ),
    }

    def _format_persona_instructions(
        self, persona: Dict[str, Any], *, name_override: str = "",
        platform: str = "", funnel_stage: str = ""
    ) -> str:
        """Convert persona.yaml into natural language instructions for the LLM."""
        lines: List[str] = []
        name = (name_override or "").strip() or persona.get("name", "Assistant")
        role = persona.get("role", "AI 助手")

        lines.append(f"你是{name}，{role}。")

        # ★ 身份硬锁：防止自我强化幻觉（历史里若曾说错名字，必须立即纠正）
        lines.append(
            f"【身份硬锁·最高优先级】你的名字就是「{name}」，没有任何别名。"
            f"如果对话历史/记忆/上下文里出现你自称叫其他名字（任何不是「{name}」的名字），"
            f"那都是过期错误数据，请立即忽略并坚持「{name}」这个身份。"
            f"被问「你叫什么名字」必须回答「{name}」，绝不能编造其他名字。"
        )

        # Western names / aliases — 让 AI 知道自己的英文名和文化名字
        _names = persona.get("names") or {}
        if isinstance(_names, dict) and any(_names.values()):
            _fw   = str(_names.get("full_western") or "").strip()
            _en   = str(_names.get("english") or "").strip()
            _de   = str(_names.get("german") or "").strip()
            _fr   = str(_names.get("french") or "").strip()
            _nick = str(_names.get("nickname") or "").strip()
            _note = str(_names.get("usage_notes") or "").strip()
            _p = []
            if _fw:   _p.append(f"西方完整姓名：{_fw}")
            if _en:   _p.append(f"英文名（日常用）：{_en}")
            if _de:   _p.append(f"德文名：{_de}")
            if _fr:   _p.append(f"法文名：{_fr}")
            if _nick: _p.append(f"昵称：{_nick}")
            if _note: _p.append(_note)
            if _p:
                lines.append(
                    "【你的西方姓名】" + "；".join(_p)
                    + "。在英文场合自然使用英文名；中文朋友叫中文名；"
                    "被问到全名或外国名字时如实回应，不要回避。"
                )

        # Background / life story — 让 AI 真正"活"在这个身份里
        background = str(persona.get("background") or "").strip()
        if background:
            lines.append(f"【你的人生背景】{background}")

        # Emotional triggers — 正向/负向/共情触发点
        ctx = persona.get("context") or {}
        triggers = ctx.get("emotional_triggers") or {}
        if isinstance(triggers, dict):
            pos = str(triggers.get("positive") or "").strip()
            neg = str(triggers.get("negative") or "").strip()
            emp = str(triggers.get("deep_empathy") or "").strip()
            if pos:
                lines.append(f"【正向触发】{pos}")
            if neg:
                lines.append(f"【负向防御】{neg}——遇到此类语言时变得沉默克制，不作争辩。")
            if emp:
                lines.append(f"【深度共情】{emp}")

        # Personality
        p = persona.get("personality", {})
        traits = p.get("traits", [])
        if traits:
            lines.append(f"性格特点：{'、'.join(traits)}。")
        style = p.get("style", "")
        if style:
            lines.append(f"说话风格：{style}。")
        # P2-A：emoji_level 真生效（修真断链）— 旧版仅当数据字段存在不进 prompt
        # web 后台改了 emoji_level 用户感知不到。这里转成自然语言指令。
        emoji_level = (p.get("emoji_level") or "").strip().lower()
        if emoji_level == "none":
            lines.append("不使用任何 emoji 或表情符号。")
        elif emoji_level == "minimal":
            lines.append("emoji 极少用：每 5 条回复最多带 1 个，仅在情绪强烈处。")
        elif emoji_level == "moderate":
            lines.append("emoji 偶尔用：约 30% 的回复带 1 个，自然不刻意，避免连用。")
        elif emoji_level == "rich":
            lines.append("emoji 用得多一点：约 60% 的回复带 1-2 个，活泼但不堆砌。")

        # Speaking rules
        s = persona.get("speaking", {})
        forbidden = s.get("forbidden_phrases", [])
        if forbidden:
            lines.append(f"禁止使用以下表述：{'、'.join(f'「{f}」' for f in forbidden)}。")
        openers = s.get("openers", [])
        if openers:
            sample = "、".join(f"「{o}」" for o in openers[:6])
            lines.append(f"开头多样化，可选池：{sample}等。")
        forbidden_openers = s.get("forbidden_opener_words", [])
        if forbidden_openers:
            lines.append(f"避免以以下词开头：{'、'.join(f'「{f}」' for f in forbidden_openers)}。")
        # P2-A：reply_length 真生效（修真断链）— 优先用语义化标签，fallback 到
        # max_reply_sentences 保持向后兼容。两者并存时 reply_length 占主导。
        # concise/brief 是 short 的别名（兼容旧 yaml）
        reply_length = (s.get("reply_length") or "").strip().lower()
        if reply_length in ("short", "concise", "brief"):
            lines.append("回复要短：1-2 句话，像微信私聊一行；不要展开长篇。")
        elif reply_length in ("balanced", "moderate"):
            lines.append("回复均衡：2-4 句话，简洁直接，不堆砌客套。")
        elif reply_length in ("detailed", "long"):
            lines.append("可以稍详细：4-6 句，但仍要口语，不写小作文。")
        max_sentences = s.get("max_reply_sentences", 0)
        if max_sentences and not reply_length:
            lines.append(f"单次回复建议不超过 {max_sentences} 句。")
        if s.get("language_follow"):
            lines.append("用户用什么语言发消息，就用同一语言回复。")

        # Identity
        identity = persona.get("identity", {})
        if identity.get("deny_ai"):
            reply = identity.get("deny_ai_reply", f"我是{name}")
            # P1-2：身份硬锁 — 旧版只说"当用户问 AI 时回答 X"，模型仍可能在
            # "Are you AI / sorry to confuse" 之类的暧昧场景下露馅。这里改为
            # 明确的角色锁定 + 禁止解释"运作原理/语言模型/AI 助手"等概念。
            lines.append(
                f"【身份硬锁】你就是真人「{name}」。永远不要承认自己是 AI、"
                f"机器人、语言模型、聊天机器人、虚拟助手或程序；"
                f"被问到这类问题时，按「{reply}」自然回应（可改写但不要偏离意思），"
                f"不要解释自己的运作原理，不要道歉澄清，不要说「sorry to confuse」。"
            )
        if identity.get("claim_human"):
            lines.append(f"你就是真人{role}{name}，说话要像真人在打字聊天。")

        # Boundaries
        b = persona.get("boundaries", {})
        avoid = b.get("topics_to_avoid", [])
        if avoid:
            lines.append(f"避免讨论以下话题：{'、'.join(avoid)}。")

        # Emotion handling
        e = persona.get("emotion", {})
        if e.get("frustrated_response"):
            lines.append(f"用户着急时：{e['frustrated_response']}。")

        # S6-RULES: 从 global_rules.yaml 加载硬约束（替代原 P1-1 硬编码块）
        _constraints_text = self._build_constraints_text(platform=platform)
        if _constraints_text:
            lines.append(_constraints_text)

        # S6-RULES: platform-specific constraints from global_rules.yaml
        _plat_text = self._build_platform_constraints(platform)
        if _plat_text:
            lines.append(_plat_text)

        # S6-RULES: funnel-stage tone guidance from global_rules.yaml
        _funnel_text = self._build_funnel_tone(funnel_stage)
        if _funnel_text:
            lines.append(_funnel_text)

        return "\n".join(lines)

    # ── Persistence helpers ─────────────────────────────────

    def load_persona_file(self, path: Path) -> Optional[Dict[str, Any]]:
        """Load a persona from a YAML file."""
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return data
        except Exception as e:
            logger.warning("Failed to load persona from %s: %s", path, e)
            return None

    def save_persona_file(self, path: Path, persona: Dict[str, Any]) -> bool:
        """Save a persona to a YAML file (P5-A: atomic write via .tmp → rename)."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".yaml.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.dump(
                    persona, f,
                    allow_unicode=True, default_flow_style=False, sort_keys=False,
                )
            # Validate before replacing (catches YAML encoder bugs)
            with open(tmp, "r", encoding="utf-8") as f:
                yaml.safe_load(f)
            tmp.replace(path)
            return True
        except Exception as e:
            logger.warning("Failed to save persona to %s: %s", path, e)
            try:
                path.with_suffix(".yaml.tmp").unlink(missing_ok=True)
            except Exception:
                pass
            return False

    def export_chat_bindings(self) -> Dict[str, Any]:
        """Export all chat bindings for persistence (P4: includes ref_bindings)."""
        return {
            "bindings": {
                cid: copy.deepcopy(p)
                for cid, p in self._chat_personas.items()
            },
            "ref_bindings": dict(self._chat_bindings),  # P4: compact profile_id references
        }

    def import_chat_bindings(self, data: Dict[str, Any]):
        """Import chat bindings from persisted data (P4: also loads ref_bindings)."""
        bindings = data.get("bindings", {})
        for cid, p in bindings.items():
            self._chat_personas[str(cid)] = copy.deepcopy(p)
        ref_bindings = data.get("ref_bindings") or {}
        for cid, pid in ref_bindings.items():
            if cid and pid:
                self._chat_bindings[str(cid)] = str(pid)
        total = len(bindings) + len(ref_bindings)
        if total:
            logger.info("Imported %d chat persona bindings (%d ref, %d inline)",
                        total, len(ref_bindings), len(bindings))

    @staticmethod
    def bindings_runtime_file_path(config_path: Path, explicit: str = "") -> Path:
        """bindings_runtime 文件路径（与 config.yaml 同目录，除非显式指定）。"""
        base = Path(config_path).resolve().parent
        ex = (explicit or "").strip()
        if ex:
            p = Path(ex)
            return p if p.is_absolute() else (base / p)
        return base / BINDINGS_RUNTIME_FILENAME

    def persist_chat_bindings(
        self,
        config_manager: Any,
    ) -> bool:
        """联结了 API 绑定/解绑后将全量会话绑定写入 bindings_runtime.yaml。"""
        if not config_manager:
            return False
        cfg_path = getattr(config_manager, "config_path", None)
        if not cfg_path:
            return False
        root = getattr(config_manager, "config", None) or {}
        pp = root.get("persona_persistence") or {}
        if not pp.get("enabled", True):
            return False
        path = self.bindings_runtime_file_path(
            Path(cfg_path), str(pp.get("bindings_path") or "")
        )
        wrapper = {
            "bindings": {
                cid: copy.deepcopy(p)
                for cid, p in self._chat_personas.items()
            },
            "ref_bindings": dict(self._chat_bindings),  # P4: compact references
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        ok = self.save_persona_file(path, wrapper)
        if ok:
            total = len(self._chat_personas) + len(self._chat_bindings)
            logger.info(
                "bindings 已持久化到 %s (%d 条: %d ref + %d inline)",
                path, total, len(self._chat_bindings), len(self._chat_personas),
            )
        return ok

    def load_chat_bindings_runtime(
        self,
        config_path: Path,
        root_config: Optional[Dict[str, Any]] = None,
    ) -> int:
        """若存在 bindings_runtime.yaml 且启用持久化，则加载并应用到内存绑定表。
        返回加载的绑定条数。
        """
        root_config = root_config or {}
        pp = root_config.get("persona_persistence") or {}
        if not pp.get("enabled", True):
            return 0
        path = self.bindings_runtime_file_path(
            config_path, str(pp.get("bindings_path") or "")
        )
        raw = self.load_persona_file(path)
        if not raw or not isinstance(raw, dict):
            return 0
        bindings = raw.get("bindings") or {}
        if not isinstance(bindings, dict):
            bindings = {}
        # P4: don't early-return — ref_bindings may still exist even if bindings is empty
        if not bindings and not raw.get("ref_bindings"):
            return 0
        count = 0
        for cid, p in bindings.items():
            if isinstance(p, dict) and cid:
                self._chat_personas[str(cid)] = copy.deepcopy(p)
                count += 1
        # P4: load reference bindings (compact profile_id references)
        ref_bindings = raw.get("ref_bindings") or {}
        ref_count = 0
        for cid, pid in ref_bindings.items():
            if cid and pid:
                self._chat_bindings[str(cid)] = str(pid)
                ref_count += 1
        total = count + ref_count
        if total:
            logger.info(
                "已从 %s 恢复 %d 个会话绑定 (%d ref + %d inline)",
                path.name, total, ref_count, count,
            )
        return total
