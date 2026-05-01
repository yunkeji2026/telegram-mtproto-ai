"""Bridge helpers for Messenger accounts and the mobile-auto0423 device registry.

This module is intentionally read-mostly.  The Messenger service keeps owning
its runtime config; mobile-auto0423 remains the source for physical phone
numbers, aliases and historical serials.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)


DEFAULT_MOBILE_AUTO_ROOT = Path("D:/workspace/mobile-auto0423")


def _safe_read_json(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8") or "{}") or {}
    except Exception as exc:
        logger.debug("read json failed %s: %s", path, exc)
    return {}


def _safe_read_yaml(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.debug("read yaml failed %s: %s", path, exc)
    return {}


def _norm_serial(value: Any) -> str:
    return str(value or "").strip()


def _norm_number(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{int(value):02d}"
    except Exception:
        s = str(value).strip()
        return s.zfill(2) if s.isdigit() else s


class MobileAutoDeviceDirectory:
    """Read mobile-auto0423 device metadata without importing that project."""

    def __init__(
        self,
        root_path: str | Path = DEFAULT_MOBILE_AUTO_ROOT,
        *,
        openclaw_db_path: str | Path | None = None,
    ) -> None:
        self.root = Path(root_path).expanduser()
        self.config_dir = self.root / "config"
        self.data_dir = self.root / "data"
        self.openclaw_db_path = (
            Path(openclaw_db_path).expanduser()
            if openclaw_db_path else self.data_dir / "openclaw.db"
        )

    @classmethod
    def from_messenger_cfg(cls, mr_cfg: Dict[str, Any]) -> "MobileAutoDeviceDirectory":
        ma = mr_cfg.get("mobile_auto") or {}
        if not isinstance(ma, dict):
            ma = {}
        root = (
            ma.get("root_path")
            or mr_cfg.get("mobile_auto_root")
            or str(DEFAULT_MOBILE_AUTO_ROOT)
        )
        db = ma.get("openclaw_db_path") or ""
        return cls(root, openclaw_db_path=db or None)

    def _load_sources(self) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        aliases = _safe_read_json(self.config_dir / "device_aliases.json")
        registry = _safe_read_json(self.config_dir / "device_registry.json")
        chat_cfg = _safe_read_yaml(self.config_dir / "chat.yaml")
        return aliases, registry, chat_cfg

    def list_devices(self) -> Dict[str, Any]:
        aliases, registry, chat_cfg = self._load_sources()
        by_serial: Dict[str, Dict[str, Any]] = {}
        conflicts: List[Dict[str, Any]] = []

        def ensure(serial: str) -> Dict[str, Any]:
            serial = _norm_serial(serial)
            row = by_serial.setdefault(serial, {
                "serial": serial,
                "current_serial": serial,
                "previous_serials": [],
                "numbers": [],
                "aliases": [],
                "display_names": [],
                "model": "",
                "android_id": "",
                "hw_serial": "",
                "sources": [],
            })
            return row

        def add_number(row: Dict[str, Any], number: Any, source: str) -> None:
            n = _norm_number(number)
            if not n:
                return
            if n not in row["numbers"]:
                if row["numbers"]:
                    conflicts.append({
                        "serial": row["serial"],
                        "field": "number",
                        "existing": list(row["numbers"]),
                        "incoming": n,
                        "source": source,
                    })
                row["numbers"].append(n)

        def add_alias(row: Dict[str, Any], alias: Any) -> None:
            a = str(alias or "").strip()
            if a and a not in row["aliases"]:
                row["aliases"].append(a)

        for key, entry in registry.items():
            if not isinstance(entry, dict):
                continue
            current = _norm_serial(entry.get("current_serial") or key.replace("serial:", ""))
            if not current:
                continue
            row = ensure(current)
            row["stable_id"] = str(key)
            row["current_serial"] = current
            row["hw_serial"] = _norm_serial(entry.get("hw_serial") or row.get("hw_serial"))
            row["android_id"] = _norm_serial(entry.get("android_id") or row.get("android_id"))
            row["model"] = _norm_serial(entry.get("model") or row.get("model"))
            row["sources"].append("device_registry")
            add_number(row, entry.get("number"), "device_registry")
            add_alias(row, entry.get("alias"))
            for ps in entry.get("previous_serials") or []:
                ps = _norm_serial(ps)
                if ps and ps not in row["previous_serials"]:
                    row["previous_serials"].append(ps)

        for serial, entry in aliases.items():
            if not isinstance(entry, dict):
                continue
            serial = _norm_serial(serial)
            if not serial:
                continue
            row = ensure(serial)
            row["sources"].append("device_aliases")
            add_number(row, entry.get("number"), "device_aliases")
            add_alias(row, entry.get("alias"))
            add_alias(row, entry.get("display_label"))
            dn = str(entry.get("display_name") or "").strip()
            if dn and dn not in row["display_names"]:
                row["display_names"].append(dn)
            if not row.get("model") and dn and ":" not in dn:
                row["model"] = dn

        chat_aliases = chat_cfg.get("device_aliases") or {}
        if isinstance(chat_aliases, dict):
            for alias, serial in chat_aliases.items():
                serial = _norm_serial(serial)
                if not serial:
                    continue
                row = ensure(serial)
                row["sources"].append("chat.yaml")
                add_number(row, alias, "chat.yaml")
                add_alias(row, f"{_norm_number(alias)}号" if _norm_number(alias) else alias)

        openclaw = self._openclaw_device_summary()
        for serial, stats in openclaw.items():
            row = ensure(serial)
            row["sources"].append("openclaw.db")
            row["openclaw"] = stats

        devices = []
        for row in by_serial.values():
            numbers = row.get("numbers") or []
            aliases_out = row.get("aliases") or []
            row["number"] = numbers[0] if numbers else ""
            row["alias"] = aliases_out[0] if aliases_out else (
                f"{row['number']}号" if row.get("number") else ""
            )
            row["sources"] = sorted(set(row.get("sources") or []))
            devices.append(row)

        by_number: Dict[str, List[str]] = {}
        for row in devices:
            for n in row.get("numbers") or []:
                by_number.setdefault(str(n), []).append(row.get("serial") or "")
        for n, serials in by_number.items():
            uniq = sorted({s for s in serials if s})
            if len(uniq) > 1:
                conflicts.append({
                    "number": n,
                    "field": "number_cross_serial",
                    "serials": uniq,
                    "source": "merged_sources",
                })

        devices.sort(key=lambda x: (x.get("number") or "99", x.get("serial") or ""))
        return {
            "root_path": str(self.root),
            "openclaw_db_path": str(self.openclaw_db_path),
            "devices": devices,
            "conflicts": conflicts,
            "summary": {
                "total": len(devices),
                "with_number": sum(1 for d in devices if d.get("number")),
                "conflicts": len(conflicts),
            },
        }

    def resolve_serial(self, serial: str) -> Dict[str, Any]:
        serial = _norm_serial(serial)
        if not serial:
            return {}
        snap = self.list_devices()
        for d in snap.get("devices") or []:
            serials = {d.get("serial"), d.get("current_serial")}
            serials.update(d.get("previous_serials") or [])
            if serial in serials:
                return d
        return {}

    def account_bindings(
        self,
        *,
        messenger_accounts: List[Dict[str, Any]],
        reply_profiles: Dict[str, Any],
        contacts_cfg: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not isinstance(messenger_accounts, list):
            messenger_accounts = []
        if not isinstance(reply_profiles, dict):
            reply_profiles = {}
        profiles = reply_profiles.get("profiles") or []
        profile_ids = {
            str(p.get("id") or p.get("name") or "").strip()
            for p in profiles if isinstance(p, dict)
        }
        default_profile = str(reply_profiles.get("default") or "").strip()
        line_map = {}
        if isinstance(contacts_cfg, dict):
            line_map = contacts_cfg.get("line_ids_by_account") or {}
            if not isinstance(line_map, dict):
                line_map = {}
        out: List[Dict[str, Any]] = []
        for entry in messenger_accounts:
            if not isinstance(entry, dict):
                continue
            aid = str(entry.get("id") or entry.get("account_id") or "").strip()
            overlay = entry.get("overrides") or entry.get("config_overlay") or {}
            if not isinstance(overlay, dict):
                overlay = {}
            serial = _norm_serial(entry.get("adb_serial"))
            mobile = self.resolve_serial(serial)
            persona_id = str(
                entry.get("persona_id")
                or entry.get("reply_profile_id")
                or overlay.get("account_reply_profile_id")
                or overlay.get("reply_profile_id")
                or default_profile
                or ""
            ).strip()
            out.append({
                "account_id": aid,
                "enabled": entry.get("enabled", True) is not False,
                "label": entry.get("label") or aid,
                "adb_serial": serial,
                "mobile_device_id": entry.get("mobile_device_id") or mobile.get("stable_id") or "",
                "device_number": _norm_number(entry.get("device_number") or mobile.get("number")),
                "device_alias": entry.get("device_alias") or mobile.get("alias") or "",
                "login_account": entry.get("login_account") or entry.get("messenger_login") or "",
                "persona_id": persona_id,
                "persona_exists": (not persona_id) or persona_id in profile_ids,
                "line_id": entry.get("line_id") or line_map.get(aid, ""),
                "mobile": mobile,
            })
        return {
            "accounts": out,
            "summary": {
                "total": len(out),
                "enabled": sum(1 for a in out if a.get("enabled")),
                "mapped_devices": sum(1 for a in out if a.get("mobile")),
                "persona_missing": sum(1 for a in out if not a.get("persona_exists")),
            },
        }

    def _openclaw_device_summary(self) -> Dict[str, Dict[str, Any]]:
        if not self.openclaw_db_path.exists():
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        try:
            conn = sqlite3.connect(
                f"file:{self.openclaw_db_path}?mode=ro", uri=True, timeout=8
            )
            conn.row_factory = sqlite3.Row
            for table, col in (
                ("facebook_inbox_messages", "device_id"),
                ("fb_account_health", "device_id"),
                ("fb_account_phase", "device_id"),
            ):
                try:
                    rows = conn.execute(
                        f"SELECT {col} AS device_id, COUNT(*) AS n FROM {table} "
                        f"GROUP BY {col} LIMIT 500"
                    ).fetchall()
                except Exception:
                    continue
                for r in rows:
                    did = _norm_serial(r["device_id"])
                    if not did:
                        continue
                    item = out.setdefault(did, {"device_id": did})
                    item[f"{table}_count"] = int(r["n"] or 0)
            conn.close()
        except Exception as exc:
            logger.debug("openclaw summary failed: %s", exc)
        return out


__all__ = ["MobileAutoDeviceDirectory", "DEFAULT_MOBILE_AUTO_ROOT"]
