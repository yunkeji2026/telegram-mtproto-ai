"""Messenger RPA Web/REST 路由。

挂载点（参考 line_rpa_routes 但更精简）：
    GET  /messenger-rpa                       — 简易卡片页（待补 HTML 模板）
    GET  /api/messenger-rpa/status            — service 状态 + 最近一次 run
    GET  /api/messenger-rpa/recent            — 最近 N 条 run 历史
    GET  /api/messenger-rpa/approvals         — 待审批/全部审批列表
    GET  /api/messenger-rpa/approvals/{id}    — 单条审批详情
    POST /api/messenger-rpa/approvals/{id}/approve  — 批准 → 后台自动发送
    POST /api/messenger-rpa/approvals/{id}/reject   — 驳回
    POST /api/messenger-rpa/trigger           — 立即跑一次 run_once
    POST /api/messenger-rpa/accounts/{id}/send-to — 指定账号向某会话名发送固定文本（不经 LLM）
    POST /api/messenger-rpa/pause             — {"seconds":300} 暂停 N 秒
    POST /api/messenger-rpa/resume            — 恢复

手动发送队列（P28）：
    POST /api/messenger-rpa/send-manual                 — 入队一条主动发送任务
    GET  /api/messenger-rpa/send-queue                  — 列出队列（?limit=30&include_done=0）
    GET  /api/messenger-rpa/send-queue/{item_id}        — 查询单条任务
    POST /api/messenger-rpa/send-queue/{item_id}/cancel — 取消待发任务

依赖：
- request.app.state.messenger_rpa_service: MessengerRpaService
- request.app.state.messenger_rpa_state_store: MessengerRpaStateStore
"""
from __future__ import annotations

import copy
import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

logger = logging.getLogger(__name__)


_HANDOFF_STATUSES = {
    "new",
    "assigned",
    "in_progress",
    "line_sent",
    "line_added",
    "converted",
    "lost",
    "paused",
}
_LINE_HANDOFF_STATUSES = {
    "not_sent",
    "sent",
    "added",
    "accepted",
    "engaged",
    "converted",
    "lost",
}
_HANDOFF_PRIORITIES = {"", "low", "mid", "high", "urgent"}


_SENSITIVE_KEYS = {
    "api_key", "token", "secret", "password", "authorization",
    "zhipu_api_key", "openai_api_key", "telegram_bot_token",
}


def _redact_cfg(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if str(k).lower() in _SENSITIVE_KEYS:
                out[k] = "***"
            else:
                out[k] = _redact_cfg(v)
        return out
    if isinstance(obj, list):
        return [_redact_cfg(v) for v in obj]
    return obj


def _messenger_cfg(config_manager: Any) -> Dict[str, Any]:
    cfg = getattr(config_manager, "config", None) or {}
    mr = cfg.get("messenger_rpa") or {}
    return mr if isinstance(mr, dict) else {}


def _dict_cfg(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _save_messenger_cfg(config_manager: Any, mr_cfg: Dict[str, Any]) -> None:
    root = getattr(config_manager, "config", None)
    if not isinstance(root, dict):
        root = {}
        config_manager.config = root
    root["messenger_rpa"] = mr_cfg
    ok = config_manager.save()
    if ok is False:
        raise HTTPException(500, "保存 messenger_rpa 配置失败")


def _refresh_service_runtime(request: Request, mr_cfg: Dict[str, Any]) -> None:
    svc = _get_service(request)
    if svc is None:
        return
    try:
        setattr(svc, "_cfg", dict(mr_cfg))
        if hasattr(svc, "_merged"):
            setattr(svc, "_merged_cfg", svc._merged())
        live_cfg = svc._reload_runtime_cfg() if hasattr(svc, "_reload_runtime_cfg") else mr_cfg
        runner = getattr(svc, "_runner", None)
        if runner is not None and hasattr(runner, "refresh_cfg"):
            runner.refresh_cfg(live_cfg)
        for r in getattr(svc, "_runners", {}).values():
            if hasattr(r, "refresh_cfg"):
                r.refresh_cfg(live_cfg)
    except Exception:
        logger.debug("messenger_rpa runtime config refresh failed", exc_info=True)


def _normalize_profiles(payload: Dict[str, Any]) -> Dict[str, Any]:
    default_id = str(payload.get("default") or "").strip()
    profiles = payload.get("profiles") or []
    if not isinstance(profiles, list):
        raise HTTPException(400, "profiles 必须是数组")
    seen = set()
    clean: List[Dict[str, Any]] = []
    for raw in profiles:
        if not isinstance(raw, dict):
            raise HTTPException(400, "profile 必须是对象")
        item = copy.deepcopy(raw)
        pid = str(item.get("id") or item.get("name") or "").strip()
        if not pid:
            raise HTTPException(400, "profile.id 不能为空")
        if pid in seen:
            raise HTTPException(400, f"profile.id 重复: {pid}")
        seen.add(pid)
        item["id"] = pid
        lang = str(item.get("language") or "auto").strip() or "auto"
        item["language"] = lang
        clean.append(item)
    if default_id and default_id not in seen:
        raise HTTPException(400, f"default profile 不存在: {default_id}")
    if not default_id and clean:
        default_id = str(clean[0]["id"])
    return {"default": default_id, "profiles": clean}


def _profile_id_for_chat(reply_profiles: Dict[str, Any], chat_key: str, chat_name: str) -> str:
    profiles = reply_profiles.get("profiles") or []
    default_id = str(reply_profiles.get("default") or "")
    ck = (chat_key or "").lower()
    cn = (chat_name or "").lower()
    for p in profiles:
        if not isinstance(p, dict):
            continue
        keys = p.get("match_chat_keys") or []
        names = p.get("match_names") or []
        if isinstance(keys, str):
            keys = [keys]
        if isinstance(names, str):
            names = [names]
        if any(str(k).strip().lower() and str(k).strip().lower() in ck for k in keys):
            return str(p.get("id") or "")
        if any(str(n).strip().lower() and str(n).strip().lower() in cn for n in names):
            return str(p.get("id") or "")
    return default_id


def _mobile_auto_snapshot(config_manager: Any) -> Dict[str, Any]:
    cfg = getattr(config_manager, "config", None) or {}
    mr_cfg = _messenger_cfg(config_manager)
    from src.integrations.messenger_rpa.device_directory import (
        MobileAutoDeviceDirectory,
    )
    directory = MobileAutoDeviceDirectory.from_messenger_cfg(mr_cfg)
    devices = directory.list_devices()
    bindings = directory.account_bindings(
        messenger_accounts=mr_cfg.get("accounts") if isinstance(mr_cfg.get("accounts"), list) else [],
        reply_profiles=_dict_cfg(mr_cfg.get("reply_profiles")),
        contacts_cfg=cfg.get("contacts") or {},
    )
    api_base = _mobile_auto_api_base(mr_cfg)
    base_screen_path = "/api/messenger-rpa/mobile-auto/devices"
    out_bindings = bindings.get("accounts") or []
    for row in out_bindings:
        if not isinstance(row, dict):
            continue
        serial = str(row.get("adb_serial") or "").strip()
        mobile = row.get("mobile") if isinstance(row.get("mobile"), dict) else {}
        row["screen_url"] = (
            f"{base_screen_path}/{urllib.parse.quote(serial, safe='')}/screenshot"
            if serial else ""
        )
        row["mobile_auto_status"] = {
            "api_base": api_base,
            "root_path": str(directory.root),
            "device_number": row.get("device_number") or mobile.get("number") or "",
            "device_alias": row.get("device_alias") or mobile.get("alias") or "",
            "sources": mobile.get("sources") or [],
            "openclaw": mobile.get("openclaw") or {},
        }
    return {
        "mobile_auto": {
            "root_path": devices.get("root_path", ""),
            "openclaw_db_path": devices.get("openclaw_db_path", ""),
            "api_base": api_base,
        },
        "devices": devices.get("devices") or [],
        "device_summary": devices.get("summary") or {},
        "conflicts": devices.get("conflicts") or [],
        "bindings": out_bindings,
        "binding_summary": bindings.get("summary") or {},
    }


def _mobile_auto_api_base(mr_cfg: Dict[str, Any]) -> str:
    ma_cfg = mr_cfg.get("mobile_auto") or {}
    if not isinstance(ma_cfg, dict):
        ma_cfg = {}
    base = (
        ma_cfg.get("api_base")
        or ma_cfg.get("base_url")
        or ma_cfg.get("url")
        or "http://127.0.0.1:18080"
    )
    return str(base).strip().rstrip("/")


def _mobile_auto_base_reachable(base: str, *, timeout: float = 0.25) -> bool:
    """Fast TCP probe before fanning out to multiple mobile-auto endpoints."""
    try:
        import socket
        parsed = urllib.parse.urlparse(base)
        host = parsed.hostname
        if not host:
            return False
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return True
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _mobile_auto_get_json(
    base: str,
    path: str,
    *,
    timeout: float = 4.0,
) -> Dict[str, Any] | List[Any]:
    url = f"{base.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Accept": "application/json"}
    api_key = ""
    try:
        import os
        api_key = (os.environ.get("OPENCLAW_API_KEY") or "").strip()
    except Exception:
        api_key = ""
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw or "{}")


def _mobile_auto_post_json(
    base: str,
    path: str,
    body: Dict[str, Any] | None = None,
    *,
    timeout: float = 8.0,
) -> Dict[str, Any] | List[Any]:
    url = f"{base.rstrip('/')}/{path.lstrip('/')}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        import os
        api_key = (os.environ.get("OPENCLAW_API_KEY") or "").strip()
    except Exception:
        api_key = ""
    if api_key:
        headers["X-API-Key"] = api_key
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw or "{}")


def _mobile_auto_status_snapshot(config_manager: Any) -> Dict[str, Any]:
    """Aggregate mobile-auto status into Messenger account rows.

    This keeps the browser simple and gives the RPA console one stable contract
    even if mobile-auto splits data across devices, VPN, performance and tasks.
    """
    mr_cfg = _messenger_cfg(config_manager)
    base = _mobile_auto_api_base(mr_cfg)
    snap = _mobile_auto_snapshot(config_manager)
    errors: Dict[str, str] = {}

    if not _mobile_auto_base_reachable(base):
        errors["mobile_auto"] = f"unreachable: {base}"
        account_rows = []
        for b in snap.get("bindings") or []:
            if not isinstance(b, dict):
                continue
            account_rows.append({
                "row_id": str(b.get("account_id") or b.get("adb_serial") or ""),
                "account_id": str(b.get("account_id") or ""),
                "adb_serial": str(b.get("adb_serial") or ""),
                "device_id": "",
                "device_number": b.get("device_number") or "",
                "device_alias": b.get("device_alias") or "",
                "online": False,
                "device_status": "mobile_auto_unreachable",
                "busy": False,
                "usb_issue": "",
                "is_cluster": False,
                "host_name": "",
                "host_id": "",
                "screen_url": "",
                "active_tasks": [],
                "active_task_count": 0,
                "task_status": "unknown",
            })
        return {
            "ok": False,
            "mobile_auto": snap.get("mobile_auto") or {},
            "summary": {
                "devices_total": len(snap.get("devices") or []),
                "devices_online": 0,
                "cluster_devices": 0,
                "hosts_total": 0,
                "accounts_total": len(account_rows),
                "accounts_online": 0,
                "tasks_active": 0,
                "vpn_connected": 0,
                "health_avg": 0,
                "screen_stats": {},
            },
            "accounts": account_rows,
            "devices": [],
            "errors": errors,
        }

    def fetch(name: str, path: str, fallback: Any):
        try:
            return _mobile_auto_get_json(base, path)
        except Exception as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"
            return fallback

    devices_raw = fetch("devices", "/devices", [])
    cluster_raw = fetch("cluster_devices", "/cluster/devices", {"devices": []})
    perf_raw = fetch("performance", "/devices/performance/all", {"devices": {}})
    vpn_raw = fetch("vpn", "/vpn/status", {"devices": []})
    tasks_raw = fetch("tasks", "/tasks?limit=300", [])
    screen_raw = fetch("screen_stats", "/screen-stats", {})

    devices = devices_raw if isinstance(devices_raw, list) else []
    cluster_devices = (
        cluster_raw.get("devices", []) if isinstance(cluster_raw, dict) else []
    )
    perf_map = (
        perf_raw.get("devices", {}) if isinstance(perf_raw, dict) else {}
    )
    vpn_items = vpn_raw.get("devices", []) if isinstance(vpn_raw, dict) else []
    tasks = tasks_raw if isinstance(tasks_raw, list) else []

    def _device_key(d: Dict[str, Any]) -> str:
        return str(
            d.get("device_id")
            or d.get("current_serial")
            or d.get("serial")
            or d.get("adb_serial")
            or ""
        ).strip()

    merged_devices: Dict[str, Dict[str, Any]] = {}
    for d in devices:
        if not isinstance(d, dict):
            continue
        key = _device_key(d)
        if not key:
            continue
        item = dict(d)
        item.setdefault("_is_cluster", False)
        item.setdefault("host_name", item.get("host_name") or "主控")
        merged_devices[key] = item

    for d in cluster_devices:
        if not isinstance(d, dict):
            continue
        key = _device_key(d)
        if not key:
            continue
        item = dict(d)
        item["_is_cluster"] = True
        item.setdefault("host_name", item.get("host_name") or item.get("host_id") or "Worker")
        if key in merged_devices:
            merged_devices[key].update(item)
        else:
            merged_devices[key] = item

    all_devices = list(merged_devices.values())

    device_meta_index: Dict[str, Dict[str, Any]] = {}
    for meta in snap.get("devices") or []:
        if not isinstance(meta, dict):
            continue
        meta_ids = [
            meta.get("serial"),
            meta.get("current_serial"),
            meta.get("adb_serial"),
            meta.get("device_id"),
        ]
        meta_ids.extend(meta.get("previous_serials") or [])
        for mid in meta_ids:
            mid_s = str(mid or "").strip()
            if mid_s:
                device_meta_index[mid_s] = meta

    for d in all_devices:
        if not isinstance(d, dict):
            continue
        meta = device_meta_index.get(_device_key(d)) or {}
        if not meta:
            continue
        d.setdefault("number", meta.get("number") or "")
        d.setdefault("device_number", meta.get("number") or "")
        d.setdefault("alias", meta.get("alias") or "")
        d.setdefault("name", meta.get("alias") or "")

    device_index: Dict[str, Dict[str, Any]] = {}
    for d in all_devices:
        ids = [d.get("device_id"), d.get("current_serial"), d.get("serial")]
        ids.extend(d.get("alternate_device_ids") or [])
        for did in ids:
            did_s = str(did or "").strip()
            if did_s:
                device_index[did_s] = d

    vpn_map: Dict[str, Dict[str, Any]] = {}
    for item in vpn_items:
        if not isinstance(item, dict):
            continue
        did = str(item.get("device_id") or "").strip()
        if did:
            vpn_map[did] = item

    active_by_device: Dict[str, List[Dict[str, Any]]] = {}
    for t in tasks:
        if not isinstance(t, dict):
            continue
        status = str(t.get("status") or "").lower()
        if status not in {"running", "pending"}:
            continue
        did = str(t.get("device_id") or "").strip()
        if did:
            active_by_device.setdefault(did, []).append(t)

    def screen_proxy_for(device_id: str, is_cluster: bool = False) -> str:
        if not device_id:
            return ""
        quoted = urllib.parse.quote(device_id, safe="")
        if is_cluster:
            return f"/api/messenger-rpa/mobile-auto/cluster/devices/{quoted}/screenshot"
        return f"/api/messenger-rpa/mobile-auto/devices/{quoted}/screenshot"

    def runtime_row(
        *,
        account_id: str,
        serial: str,
        dev: Dict[str, Any],
        binding: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        binding = binding or {}
        did = str(dev.get("device_id") or serial).strip()
        is_cluster = bool(dev.get("_is_cluster"))
        host_name = str(dev.get("host_name") or dev.get("host_id") or ("Worker" if is_cluster else "主控")).strip()
        perf = perf_map.get(did) or perf_map.get(serial) or {}
        vpn = vpn_map.get(did) or vpn_map.get(serial) or {}
        active = active_by_device.get(did) or active_by_device.get(serial) or []
        meta = device_meta_index.get(did) or device_meta_index.get(serial) or {}
        is_online = str(dev.get("status") or "").lower() in {
            "connected", "online", "busy"
        }
        return {
            "row_id": account_id or f"device:{did or serial}",
            "account_id": account_id,
            "adb_serial": serial,
            "device_id": did,
            "device_number": binding.get("device_number") or dev.get("number") or dev.get("device_number") or meta.get("number") or "",
            "device_alias": binding.get("device_alias") or dev.get("alias") or dev.get("name") or meta.get("alias") or "",
            "online": bool(is_online),
            "device_status": dev.get("status") or ("unknown" if serial else "unbound"),
            "busy": bool(dev.get("busy")),
            "usb_issue": dev.get("usb_issue") or "",
            "is_cluster": is_cluster,
            "host_name": host_name,
            "host_id": dev.get("host_id") or "",
            "screen_url": screen_proxy_for(did or serial, is_cluster),
            "battery_level": perf.get("battery_level"),
            "battery_temp": perf.get("battery_temp"),
            "charging": perf.get("charging"),
            "mem_usage": perf.get("mem_usage"),
            "storage_usage": perf.get("storage_usage"),
            "vpn_connected": bool(vpn.get("connected")),
            "vpn_config": vpn.get("config_name") or "",
            "vpn_ip": vpn.get("ip") or "",
            "vpn_country": vpn.get("country") or "",
            "active_tasks": active[:5],
            "active_task_count": len(active),
            "task_status": (
                "running" if any(str(t.get("status")) == "running" for t in active)
                else "pending" if active else "idle"
            ),
        }

    account_rows = []
    for b in snap.get("bindings") or []:
        if not isinstance(b, dict):
            continue
        aid = str(b.get("account_id") or "").strip()
        serial = str(b.get("adb_serial") or "").strip()
        dev = device_index.get(serial, {})
        account_rows.append(runtime_row(account_id=aid, serial=serial, dev=dev, binding=b))

    device_rows = [
        runtime_row(account_id="", serial=str(d.get("device_id") or "").strip(), dev=d)
        for d in all_devices
        if isinstance(d, dict) and str(d.get("device_id") or "").strip()
    ]
    online_count = sum(1 for d in all_devices if str(d.get("status") or "").lower() in {"connected", "online", "busy"})
    cluster_count = sum(1 for d in all_devices if bool(d.get("_is_cluster")))
    hosts = {
        str(d.get("host_name") or d.get("host_id") or "").strip()
        for d in all_devices
        if bool(d.get("_is_cluster")) and str(d.get("host_name") or d.get("host_id") or "").strip()
    }
    return {
        "ok": not bool(errors),
        "mobile_auto": snap.get("mobile_auto") or {},
        "summary": {
            "devices_total": len(all_devices),
            "devices_online": online_count,
            "cluster_devices": cluster_count,
            "hosts_total": len(hosts),
            "accounts_total": len(account_rows),
            "accounts_online": sum(1 for a in account_rows if a.get("online")),
            "tasks_active": sum(len(v) for v in active_by_device.values()),
            "vpn_connected": sum(1 for v in vpn_map.values() if v.get("connected")),
            "health_avg": screen_raw.get("health_avg", 0) if isinstance(screen_raw, dict) else 0,
            "screen_stats": screen_raw if isinstance(screen_raw, dict) else {},
        },
        "accounts": account_rows,
        "devices": device_rows,
        "errors": errors,
    }


def _media_cfg(mr_cfg: Dict[str, Any]) -> Dict[str, Any]:
    voice_input = _dict_cfg(mr_cfg.get("voice_input"))
    if "audio_pipeline" not in voice_input:
        voice_input["audio_pipeline"] = _dict_cfg(mr_cfg.get("audio_pipeline"))
    voice_output = _dict_cfg(mr_cfg.get("voice_output"))
    return {
        "media_handling_policy": mr_cfg.get("media_handling_policy", "ai"),
        "media_include_links": bool(mr_cfg.get("media_include_links", False)),
        "media_deep_understand": _dict_cfg(mr_cfg.get("media_deep_understand")),
        "voice_input": voice_input,
        "voice_output": voice_output,
        "emoji_policy": _dict_cfg(mr_cfg.get("emoji_policy")),
    }


def _voice_runtime_summary(mr_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Operator-facing voice capability model."""
    vi = _dict_cfg(mr_cfg.get("voice_input"))
    vo = _dict_cfg(mr_cfg.get("voice_output"))
    ap = _dict_cfg(vi.get("audio_pipeline")) or _dict_cfg(mr_cfg.get("audio_pipeline"))
    return {
        "input": {
            "enabled": bool(vi.get("enabled", False)),
            "capture_mode": str(vi.get("capture_mode") or "run_as").strip(),
            "prefer_transcribe": vi.get("prefer_transcribe", True) is not False,
            "fallback": str(vi.get("fallback") or "ack_and_approve"),
            "timeout_sec": float(vi.get("timeout_sec", 30) or 30),
            "asr_backend": str(ap.get("backend") or "disabled"),
            "asr_model": str(ap.get("model") or ap.get("model_size") or ""),
            "language": str(ap.get("language") or vi.get("language_hint") or "auto"),
        },
        "output": {
            "enabled": bool(vo.get("enabled", False)),
            "mode": str(vo.get("mode") or "approval_only"),
            "backend": str(vo.get("backend") or "edge_tts"),
            "voice": str(vo.get("voice") or ""),
            "format": str(vo.get("format") or "mp3"),
            "send_text_summary": vo.get("send_text_summary", True) is not False,
            "max_seconds": float(vo.get("max_seconds", 20) or 20),
            "fallback": str(vo.get("fallback") or "text"),
        },
        "safety": {
            "night_quiet": bool(vo.get("night_quiet", True)),
            "approval_first": str(vo.get("mode") or "approval_only") != "auto",
            "max_per_user_daily": int(vo.get("max_per_user_daily", 3) or 3),
            "max_per_account_daily": int(vo.get("max_per_account_daily", 30) or 30),
        },
    }


def _account_id_from_chat_key(chat_key: str, fallback: str = "") -> str:
    """Infer configured account id from namespaced Messenger chat_key."""
    ck = str(chat_key or "")
    if ck.startswith("acc_") and ":" in ck:
        return ck.split(":", 1)[0][4:]
    if fallback:
        return fallback
    if ck.startswith("messenger_rpa:"):
        return "default"
    return ""


def _chat_name_from_key(chat_key: str) -> str:
    ck = str(chat_key or "")
    return ck.split(":", 1)[-1] if ":" in ck else ck


def _iter_account_stores(
    request: Request,
    config_manager: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Return available per-account state stores.

    Runtime service owns the authoritative AccountRegistry.  Tests and degraded
    web-only boots may only expose app.state.messenger_rpa_state_store, so keep
    that fallback.
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    svc = _get_service(request)
    reg = getattr(svc, "_account_registry", None)
    if reg is not None and hasattr(reg, "all_contexts"):
        try:
            for ctx in reg.all_contexts():
                aid = str(getattr(ctx, "account_id", "") or "default")
                if aid in seen:
                    continue
                store = ctx.state_store() if hasattr(ctx, "state_store") else None
                if store is None:
                    continue
                out.append({
                    "account_id": aid,
                    "label": getattr(ctx, "label", "") or aid,
                    "adb_serial": getattr(ctx, "adb_serial", "") or "",
                    "store": store,
                })
                seen.add(aid)
        except Exception:
            logger.debug("account store iteration failed", exc_info=True)
    if config_manager is not None:
        try:
            from src.integrations.messenger_rpa.state_store import (
                MessengerRpaStateStore,
                default_state_db_path,
            )
            mr_cfg = _messenger_cfg(config_manager)
            accounts = mr_cfg.get("accounts") if isinstance(mr_cfg.get("accounts"), list) else []
            max_runs_kept = int(mr_cfg.get("max_runs_kept") or 500)
            for acc in accounts:
                if not isinstance(acc, dict):
                    continue
                aid = str(acc.get("id") or acc.get("account_id") or "").strip()
                if not aid or aid in seen:
                    continue
                store = MessengerRpaStateStore(
                    default_state_db_path(config_manager.config_path, aid),
                    max_runs_kept=max_runs_kept,
                    account_id=aid,
                )
                out.append({
                    "account_id": aid,
                    "label": acc.get("label") or aid,
                    "adb_serial": acc.get("adb_serial") or "",
                    "store": store,
                })
                seen.add(aid)
        except Exception:
            logger.debug("configured account store iteration failed", exc_info=True)
    if out:
        return out
    store = _get_store(request)
    if store is not None:
        aid = str(getattr(store, "account_id", "") or "default")
        out.append({"account_id": aid, "label": aid, "adb_serial": "", "store": store})
    return out


def _store_info_for_chat_key(
    request: Request,
    chat_key: str,
    config_manager: Optional[Any] = None,
) -> tuple[Optional[Any], Dict[str, Any]]:
    """Resolve the per-account store that owns a chat_key."""
    stores = _iter_account_stores(request, config_manager)
    if not stores:
        return None, {}
    account_id = _account_id_from_chat_key(chat_key, "")
    selected: Dict[str, Any] | None = None
    for si in stores:
        if account_id and str(si.get("account_id") or "") != account_id:
            continue
        selected = si
        break
    if selected is None:
        selected = stores[0]
    return selected.get("store"), selected


def _load_bot_contexts(
    config_manager: Any,
    *,
    limit: int = 200,
    chat_key: str = "",
) -> Dict[str, Dict[str, Any]]:
    """Load persisted SkillManager contexts for Messenger chat keys."""
    contexts: Dict[str, Dict[str, Any]] = {}
    try:
        cfg_dir = Path(config_manager.config_path).parent
        db = cfg_dir / "bot.db"
        if not db.exists():
            return contexts
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        if chat_key:
            rows = c.execute(
                "SELECT user_id, data, updated_at FROM user_context WHERE user_id=?",
                (chat_key,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT user_id, data, updated_at FROM user_context "
                "WHERE user_id LIKE ? OR user_id LIKE ? "
                "ORDER BY updated_at DESC LIMIT ?",
                ("acc_%:%", "messenger_rpa:%", max(int(limit or 200), 1)),
            ).fetchall()
        c.close()
        for row in rows:
            try:
                data = json.loads(row["data"] or "{}") or {}
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            data["_ctx_updated_at"] = row["updated_at"]
            contexts[str(row["user_id"])] = data
    except Exception:
        logger.debug("messenger bot context load failed", exc_info=True)
    return contexts


def _binding_index(config_manager: Any) -> Dict[str, Dict[str, Any]]:
    try:
        snap = _mobile_auto_snapshot(config_manager)
        return {
            str(b.get("account_id") or ""): b
            for b in snap.get("bindings") or []
            if isinstance(b, dict) and str(b.get("account_id") or "")
        }
    except Exception:
        logger.debug("binding index load failed", exc_info=True)
        return {}


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v or 0)
    except Exception:
        return default


def _short_text(value: Any, n: int = 160) -> str:
    s = str(value or "").strip()
    return s if len(s) <= n else s[: max(0, n - 1)] + "..."


def _lead_stage_label(score: int, lead: Dict[str, Any], line_id: str = "") -> str:
    if score >= 80 and line_id:
        return "ready_for_handoff"
    if score >= 80:
        return "qualified_missing_line"
    if score >= 40:
        return "nurture"
    return "low_priority"


def _build_handoff_advice(
    *,
    lead: Dict[str, Any],
    score: int,
    missing: List[Any],
    line_id: str,
    chat_name: str,
) -> Dict[str, Any]:
    stage = _lead_stage_label(score, lead, line_id)
    missing_set = {str(x) for x in missing or []}
    if stage == "ready_for_handoff":
        priority = "high"
        next_action = "建议人工客服接手，可在自然上下文中承接 LINE 沟通。"
    elif stage == "qualified_missing_line":
        priority = "high"
        next_action = "线索分数高，但当前账号未配置人工客服 LINE，先补齐对接信息。"
    elif stage == "nurture":
        priority = "mid"
        if "occupation" in missing_set or "income_signal" in missing_set:
            next_action = "继续自然聊天，优先了解工作节奏、生活方式和消费能力信号。"
        else:
            next_action = "继续培养关系，等待对方表达更明确的服务需求后再转人工。"
    else:
        priority = "low"
        next_action = "低优先级，保持礼貌短回，不主动推进 LINE。"

    opening = "こんにちは。さっきの話、少し整理して見ました。無理に急がなくて大丈夫です。"
    if "occupation" in missing_set:
        opening = "そうなんですね。普段はどんなお仕事や生活リズムが多いんですか？"
    elif "age_range" in missing_set:
        opening = "話していて落ち着いた雰囲気を感じました。近い年代の方なのかなと思いました。"
    elif score >= 80 and line_id:
        opening = f"ここだと見落とすこともあるので、よければLINEで続きも話せます。LINE: {line_id}"

    cautions = [
        "不要直接追问年收入、资产、住址、证件或支付信息。",
        "不要承诺感情关系；先共情，再轻问一个问题。",
        "如果对方抗拒转平台，停止推进 LINE，回到普通聊天。",
    ]
    return {
        "priority": priority,
        "handoff_stage": stage,
        "next_action": next_action,
        "recommended_opening": opening,
        "cautions": cautions,
        "line_ready": bool(line_id and score >= 80),
        "line_id": line_id,
        "operator_note": f"{chat_name} 当前适合按 {priority} 优先级处理。",
    }


def _history_counts(ctx: Dict[str, Any]) -> Dict[str, int]:
    hist = ctx.get("_conversation_history") or []
    if not isinstance(hist, list):
        hist = []
    user_n = sum(1 for m in hist if isinstance(m, dict) and str(m.get("role") or "") == "user")
    bot_n = sum(1 for m in hist if isinstance(m, dict) and str(m.get("role") or "") in ("assistant", "bot"))
    return {"history_items": len(hist), "customer_messages": user_n, "bot_messages": bot_n}


def _lead_source_messages(
    *,
    ctx: Dict[str, Any],
    runs: List[Dict[str, Any]],
    approvals: List[Dict[str, Any]],
) -> List[str]:
    """Collect customer-side texts for display-time lead scoring."""
    out: List[str] = []

    def _add(value: Any) -> None:
        s = str(value or "").strip()
        if not s:
            return
        if s not in out:
            out.append(s[:500])

    hist = ctx.get("_conversation_history") or []
    if isinstance(hist, list):
        for msg in hist[-30:]:
            if not isinstance(msg, dict):
                continue
            if str(msg.get("role") or "") == "user":
                _add(msg.get("content") or msg.get("text") or "")
    _add(ctx.get("last_message") or "")
    for r in sorted(runs, key=lambda x: _safe_float(x.get("ts")))[-30:]:
        _add(r.get("peer_text") or "")
    for a in sorted(approvals, key=lambda x: _safe_float(x.get("created_at")))[-30:]:
        _add(a.get("peer_text") or "")
    return out[-16:]


def _derive_lead_profile(
    *,
    lead: Dict[str, Any],
    ctx: Dict[str, Any],
    runs: List[Dict[str, Any]],
    approvals: List[Dict[str, Any]],
    reply_lang: str,
    chat_name: str,
    lead_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Fallback ICP profile from existing history when stored lead data is empty."""
    current = dict(lead or {})
    if int(current.get("icp_score") or 0) > 0 or current.get("evidence"):
        return current
    messages = _lead_source_messages(ctx=ctx, runs=runs, approvals=approvals)
    if not messages:
        return current
    try:
        from src.integrations.messenger_rpa.lead_qualification import (
            LeadQualificationEngine,
        )
        cfg = copy.deepcopy(lead_cfg or {})
        handoff = cfg.get("handoff") if isinstance(cfg.get("handoff"), dict) else {}
        handoff = dict(handoff)
        # Display-time scoring must not mark LINE as sent or force a handoff reply.
        handoff["line_id"] = ""
        cfg["handoff"] = handoff
        engine = LeadQualificationEngine(cfg)
        profile: Dict[str, Any] = {}
        for msg in messages:
            decision = engine.evaluate(
                profile,
                peer_text=msg,
                reply_lang=reply_lang or str(ctx.get("reply_lang") or "ja"),
                chat_name=chat_name,
            )
            profile = decision.profile
        if profile:
            evidence = profile.get("evidence") if isinstance(profile.get("evidence"), list) else []
            if not evidence and int(profile.get("icp_score") or 0) < 40:
                return current
            profile["derived_from_history"] = True
            profile["derived_message_count"] = len(messages)
            profile.setdefault("evidence", [])
            return profile
    except Exception:
        logger.debug("lead history derivation failed", exc_info=True)
    return current


def _build_lead_item(
    *,
    chat_key: str,
    chat_name: str,
    store_info: Dict[str, Any],
    st: Dict[str, Any],
    ctx: Dict[str, Any],
    reply_profiles: Dict[str, Any],
    lead_qualification_cfg: Dict[str, Any],
    binding: Dict[str, Any],
    runs: List[Dict[str, Any]],
    approvals: List[Dict[str, Any]],
) -> Dict[str, Any]:
    lead = ctx.get("lead_qualification") if isinstance(ctx, dict) else {}
    if not isinstance(lead, dict):
        lead = {}
    lead = _derive_lead_profile(
        lead=lead,
        ctx=ctx,
        runs=runs,
        approvals=approvals,
        reply_lang=str(ctx.get("reply_lang") or ""),
        chat_name=chat_name,
        lead_cfg=lead_qualification_cfg,
    )
    score = int(lead.get("icp_score") or 0)
    missing = lead.get("missing_fields") or []
    if not isinstance(missing, list):
        missing = []
    evidence = lead.get("evidence") or []
    if not isinstance(evidence, list):
        evidence = []

    ts_candidates = [
        _safe_float(st.get("updated_at")),
        _safe_float(ctx.get("_ctx_updated_at")),
        _safe_float(ctx.get("last_message_time")),
        _safe_float(ctx.get("last_reply_time")),
    ]
    ts_candidates.extend(_safe_float(r.get("ts")) for r in runs)
    ts_candidates.extend(_safe_float(a.get("created_at")) for a in approvals)
    nonzero_ts = [x for x in ts_candidates if x > 0]
    first_ts = min(nonzero_ts) if nonzero_ts else 0
    last_ts = max(nonzero_ts) if nonzero_ts else 0
    hist_counts = _history_counts(ctx)
    customer_count = max(
        hist_counts["customer_messages"],
        sum(1 for r in runs if str(r.get("peer_text") or "").strip()),
        sum(1 for a in approvals if str(a.get("peer_text") or "").strip()),
    )
    bot_count = max(
        hist_counts["bot_messages"],
        sum(1 for r in runs if str(r.get("reply_text") or "").strip()),
        sum(1 for a in approvals if str(a.get("reply_text") or "").strip()),
        int(ctx.get("reply_count") or 0),
    )
    last_peer = st.get("last_peer_text") or ctx.get("last_message") or ""
    last_reply = st.get("last_reply") or ctx.get("last_reply") or ""
    summary = str(ctx.get("_conversation_summary") or "").strip()
    summary_short = summary or (
        f"最近客户说：{_short_text(last_peer, 90)}；最近回复：{_short_text(last_reply, 90)}"
        if (last_peer or last_reply) else "暂无可用聊天摘要"
    )
    account_id = _account_id_from_chat_key(chat_key, str(store_info.get("account_id") or ""))
    line_id = str(binding.get("line_id") or "")
    advice = _build_handoff_advice(
        lead=lead, score=score, missing=missing, line_id=line_id, chat_name=chat_name,
    )
    return {
        "chat_key": chat_key,
        "chat_name": chat_name,
        "account_id": account_id,
        "account_label": binding.get("label") or store_info.get("label") or account_id,
        "adb_serial": binding.get("adb_serial") or store_info.get("adb_serial") or "",
        "device_number": binding.get("device_number") or "",
        "device_alias": binding.get("device_alias") or "",
        "login_account": binding.get("login_account") or "",
        "line_id": line_id,
        "updated_at": last_ts,
        "first_contact_at": first_ts,
        "last_activity_at": last_ts,
        "talk_duration_sec": max(0, last_ts - first_ts) if first_ts and last_ts else 0,
        "last_sent_at": st.get("last_sent_at") or 0,
        "last_peer_text": last_peer,
        "last_reply": last_reply,
        "reply_lang": ctx.get("reply_lang", ""),
        "forced_lang": st.get("forced_lang") or "",
        "persona_id": _profile_id_for_chat(reply_profiles, chat_key, chat_name),
        "lead": lead,
        "score": score,
        "stage": str(lead.get("stage") or "unknown"),
        "missing_fields": missing,
        "evidence": evidence,
        "summary_short": summary_short,
        "handoff": advice,
        "stats": {
            "run_count": len(runs),
            "approval_count": len(approvals),
            "pending_approval_count": sum(1 for a in approvals if a.get("status") == "pending"),
            "sent_approval_count": sum(1 for a in approvals if a.get("status") == "sent"),
            "customer_message_count": customer_count,
            "bot_reply_count": bot_count,
            "history_items": hist_counts["history_items"],
            "turn_count": max(customer_count, bot_count),
        },
    }


def _timeline_for_chat(
    *,
    runs: List[Dict[str, Any]],
    approvals: List[Dict[str, Any]],
    limit: int = 80,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for r in runs:
        rows.append({
            "source": "run",
            "ts": r.get("ts") or 0,
            "direction": "rpa_run",
            "message_type": r.get("peer_kind") or "",
            "status": "ok" if r.get("ok") else "failed",
            "step": r.get("step") or "",
            "peer_text": r.get("peer_text") or "",
            "reply_text": r.get("reply_text") or "",
            "error": r.get("error") or "",
            "screenshot_path": r.get("screenshot_path") or "",
            "total_ms": r.get("total_ms") or 0,
        })
    for a in approvals:
        rows.append({
            "source": "approval",
            "id": a.get("id"),
            "ts": a.get("created_at") or 0,
            "direction": "approval",
            "message_type": a.get("peer_kind") or "",
            "status": a.get("status") or "",
            "step": a.get("ai_tier") or "",
            "peer_text": a.get("peer_text") or "",
            "reply_text": a.get("reply_text") or "",
            "reply_lang": a.get("reply_lang") or "",
            "decided_by": a.get("decided_by") or "",
            "decision_note": a.get("decision_note") or "",
            "sent_at": a.get("sent_at") or 0,
            "send_error": a.get("send_error") or "",
            "screenshot_path": a.get("screenshot_path") or "",
        })
    rows.sort(key=lambda x: _safe_float(x.get("ts")), reverse=True)
    return rows[: max(1, min(int(limit or 80), 300))]


def _lead_ops_summary(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Operator-facing rollup for the lead workbench."""
    now = time.time()
    handoff_statuses = {
        "new": 0,
        "assigned": 0,
        "in_progress": 0,
        "line_sent": 0,
        "line_added": 0,
        "converted": 0,
        "lost": 0,
        "paused": 0,
    }
    line_statuses = {
        "not_sent": 0,
        "sent": 0,
        "added": 0,
        "accepted": 0,
        "engaged": 0,
        "converted": 0,
        "lost": 0,
    }
    actions = {
        "unassigned": 0,
        "needs_line_config": 0,
        "pending_approval": 0,
        "ready_for_handoff": 0,
        "needs_profile": 0,
        "active_followup": 0,
        "followup_due": 0,
        "followup_today": 0,
    }
    for item in items:
        op = item.get("operator_handoff") if isinstance(item.get("operator_handoff"), dict) else {}
        status = str(op.get("status") or "new")
        line_status = str(op.get("line_status") or "not_sent")
        if status in handoff_statuses:
            handoff_statuses[status] += 1
        if line_status in line_statuses:
            line_statuses[line_status] += 1
        if not str(op.get("owner") or "").strip() and status in {"new", "assigned"}:
            actions["unassigned"] += 1
        if not str(item.get("line_id") or "").strip():
            actions["needs_line_config"] += 1
        stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
        if int(stats.get("pending_approval_count") or 0) > 0:
            actions["pending_approval"] += 1
        handoff = item.get("handoff") if isinstance(item.get("handoff"), dict) else {}
        if bool(handoff.get("line_ready")):
            actions["ready_for_handoff"] += 1
        if item.get("missing_fields"):
            actions["needs_profile"] += 1
        if status in {"assigned", "in_progress", "line_sent", "line_added"}:
            actions["active_followup"] += 1
        follow_ts = _safe_float(op.get("next_followup_at"))
        if follow_ts > 0:
            if follow_ts <= now:
                actions["followup_due"] += 1
            elif follow_ts <= now + 86400:
                actions["followup_today"] += 1
    return {
        "handoff_statuses": handoff_statuses,
        "line_statuses": line_statuses,
        "actions": actions,
    }


def _get_service(request: Request):
    return getattr(request.app.state, "messenger_rpa_service", None)


def _get_store(request: Request):
    return getattr(request.app.state, "messenger_rpa_state_store", None)


def register_messenger_rpa_routes(
    app,
    *,
    page_auth,
    api_auth,
    templates,
    config_manager,
    audit_store=None,
):
    """挂 Messenger RPA 的 Web + REST 路由。"""

    # ── Web: HTML 页 ────────────────────────────────
    @app.get("/messenger-rpa", response_class=HTMLResponse)
    async def messenger_rpa_page(request: Request):
        # 手动调 page_auth（支持 sync 或 async 都在这里兜）
        res = page_auth(request)
        if hasattr(res, "__await__"):
            await res
        return templates.TemplateResponse(request, "messenger_rpa.html", {})

    # ── REST: 状态 ─────────────────────────────────
    @app.get("/api/messenger-rpa/status")
    def api_msgr_status(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            st: Dict[str, Any] = {
                "available": False,
                "enabled_cfg": bool(
                    (config_manager.config or {})
                    .get("messenger_rpa", {})
                    .get("enabled")
                ),
                "hint": (
                    "messenger_rpa.enabled=false 或服务未构建；"
                    "在 config.yaml 中开启后重启进程"
                ),
            }
        else:
            st = svc.status()
            st["available"] = True
        # escalation 占位行计数：store-derived 字段，svc 无关，两路都附加
        store = _get_store(request)
        if store is not None:
            try:
                st["pending_empty_count"] = store.count_approvals(
                    status="pending", reply_text_empty=True,
                )
            except Exception:
                logger.exception("pending_empty_count 查询失败")
                st["pending_empty_count"] = -1
        return st

    @app.get("/api/messenger-rpa/hint-metrics")
    def api_msgr_hint_metrics(request: Request, window: int = 0):
        """P11 (2026-05-04)：messenger_rpa hint 事件计数器快照（JSON）。

        路径名为 hint-metrics 避开同 path 的 Prometheus exposition 端点
        （/api/messenger-rpa/metrics 已用于 Prometheus，见下方）。

        Query params:
          - ``window`` (秒): 0 / 缺省 = 累计；> 0 返回最近 N 秒窗口聚合（趋势）

        来源：reply_decided 时自动从 result.hints 入计数。包含：
          - self_overlap_promote / self_overlap_skip 拦截率
          - thread_title_cache_hit / pre_foreground_cache_hit 命中率
          - xml_inbox_fallback / xml_inbox_supplement 兜底次数
          - cycle_entry_thread_recovered / sticky_force_full_screen_*
          - inject_verify_emoji_normalized 等

        前端 dashboard 同时拉 window=3600 + 累计，可显示"最近 1h vs 历史"
        对比，发现退化（如 cache_hit 突降）立即告警。
        """
        api_auth(request)
        try:
            from src.monitoring.metrics_store import get_metrics_store
            ms = get_metrics_store()
            cumulative = ms.get_messenger_rpa_metrics(window_sec=None)
            recent = (
                ms.get_messenger_rpa_metrics(window_sec=float(window))
                if window and window > 0 else None
            )
        except Exception as ex:
            logger.exception("messenger_rpa metrics 查询失败")
            return {"error": f"{type(ex).__name__}:{ex}", "metrics": {}}
        sorted_cum = dict(sorted(cumulative.items(), key=lambda kv: -kv[1]))
        out = {
            "ok": True,
            "total_events": sum(cumulative.values()),
            "unique_event_names": len(cumulative),
            "metrics": sorted_cum,
        }
        if recent is not None:
            out["window_sec"] = int(window)
            out["window_total"] = sum(recent.values())
            out["window_metrics"] = dict(
                sorted(recent.items(), key=lambda kv: -kv[1])
            )
        return out

    @app.get("/api/messenger-rpa/config")
    def api_msgr_config(request: Request):
        """Return Messenger operator-facing configuration."""
        api_auth(request)
        raw_cfg = copy.deepcopy(_messenger_cfg(config_manager))
        svc = _get_service(request)
        effective = raw_cfg
        if svc is not None and hasattr(svc, "_reload_runtime_cfg"):
            try:
                effective = svc._reload_runtime_cfg()
            except Exception:
                effective = raw_cfg
        ops_keys = [
            "enabled", "autostart", "reply_mode", "max_inbox_per_run",
            "run_once_target_names", "target_chat_names",
            "run_once_start_mode", "force_return_to_chats",
            "thread_title_vision_fallback", "pre_thread_self_xml_guard",
            "stale_peer_after_self_guard",
            "companion_reply_cooldown_sec", "suppress_global_ai_identity",
            "disable_episodic_memory", "language_alignment",
            "default_reply_lang", "force_reply_lang", "companion_mode",
            "interval_sec", "min_interval_sec", "max_interval_sec",
            "backoff_multiplier",
        ]
        return {
            "raw": _redact_cfg(raw_cfg),
            "effective": _redact_cfg(effective),
            "operations": {k: raw_cfg.get(k) for k in ops_keys if k in raw_cfg},
            "accounts": raw_cfg.get("accounts") or [],
            "reply_profiles": raw_cfg.get("reply_profiles") or {},
            "lead_qualification": raw_cfg.get("lead_qualification") or {},
            "media": _media_cfg(raw_cfg),
            "mobile_auto": raw_cfg.get("mobile_auto") or {},
            "safety": raw_cfg.get("safety") or {},
        }

    @app.put("/api/messenger-rpa/config")
    async def api_msgr_config_update(request: Request):
        """Patch safe Messenger RPA settings from the operations console."""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body 必须是对象")
        allowed = {
            "enabled", "autostart", "reply_mode", "max_inbox_per_run",
            "run_once_target_names", "target_chat_names", "test_target_names",
            "run_once_start_mode", "force_return_to_chats",
            "thread_title_vision_fallback", "pre_thread_self_xml_guard",
            "stale_peer_after_self_guard",
            "companion_reply_cooldown_sec", "suppress_global_ai_identity",
            "disable_episodic_memory", "language_alignment",
            "default_reply_lang", "force_reply_lang", "companion_mode",
            "interval_sec", "min_interval_sec", "max_interval_sec",
            "backoff_multiplier",
            "lead_qualification",
            # B6-P1 ops sub-tab 暴露的 dict 字段
            "runaway_guard", "vision_misroute_guard", "sticky_thread",
            "credit_policy", "pace_learning", "ai", "humanize",
            "approval_sla", "portrait", "spam_cooldown_sec",
            "self_skip_cooldown_sec", "per_chat_hourly_cap",
        }
        # 这些字段是 dict — 走深度 merge（保护未在 UI 暴露的子字段不被覆盖）
        dict_merge_keys = {
            "lead_qualification", "runaway_guard", "vision_misroute_guard",
            "sticky_thread", "credit_policy", "pace_learning", "ai",
            "humanize", "approval_sla", "portrait",
        }
        bad = [k for k in body.keys() if k not in allowed]
        if bad:
            raise HTTPException(400, f"不允许的字段: {bad}")
        mr_cfg = copy.deepcopy(_messenger_cfg(config_manager))
        for k, v in body.items():
            if k in dict_merge_keys:
                if not isinstance(v, dict):
                    raise HTTPException(400, f"{k} 必须是对象")
                cur = mr_cfg.get(k) if isinstance(mr_cfg.get(k), dict) else {}
                merged = copy.deepcopy(cur)
                for lk, lv in v.items():
                    if isinstance(lv, dict) and isinstance(merged.get(lk), dict):
                        sub = dict(merged[lk])
                        sub.update(lv)
                        merged[lk] = sub
                    else:
                        merged[lk] = lv
                mr_cfg[k] = merged
            elif k in ("run_once_target_names", "target_chat_names", "test_target_names"):
                if isinstance(v, str):
                    mr_cfg[k] = [x.strip() for x in v.split(",") if x.strip()]
                elif isinstance(v, list):
                    mr_cfg[k] = [str(x).strip() for x in v if str(x or "").strip()]
                else:
                    raise HTTPException(400, f"{k} 必须是字符串或数组")
            elif k == "run_once_start_mode":
                mode = str(v or "").strip().lower()
                if mode not in ("smart_current_thread", "force_chats"):
                    raise HTTPException(
                        400,
                        "run_once_start_mode 必须是 smart_current_thread 或 force_chats",
                    )
                mr_cfg[k] = mode
            else:
                mr_cfg[k] = v
        _save_messenger_cfg(config_manager, mr_cfg)
        _refresh_service_runtime(request, mr_cfg)
        return {"ok": True, "updated_keys": list(body.keys())}

    @app.get("/api/messenger-rpa/personas")
    async def api_msgr_personas(request: Request):
        api_auth(request)
        cfg = _messenger_cfg(config_manager)
        return {
            "reply_profiles": cfg.get("reply_profiles") or {},
            "experiment": cfg.get("persona_experiment") or {},
        }

    @app.put("/api/messenger-rpa/personas")
    async def api_msgr_personas_update(request: Request):
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body 必须是对象")
        rp_body = body.get("reply_profiles", body)
        if not isinstance(rp_body, dict):
            raise HTTPException(400, "reply_profiles 必须是对象")
        normalized = _normalize_profiles(rp_body)
        mr_cfg = copy.deepcopy(_messenger_cfg(config_manager))
        mr_cfg["reply_profiles"] = normalized
        _save_messenger_cfg(config_manager, mr_cfg)
        _refresh_service_runtime(request, mr_cfg)
        return {"ok": True, "reply_profiles": normalized}

    @app.get("/api/messenger-rpa/strategy/runtime")
    async def api_msgr_strategy_runtime(
        request: Request,
        limit: int = 120,
    ):
        """Return backend-backed persona strategy runtime state."""
        api_auth(request)
        store = _get_store(request)
        cfg = _messenger_cfg(config_manager)
        reply_profiles = _dict_cfg(cfg.get("reply_profiles"))
        if store is None:
            return {
                "available": False,
                "summary": {},
                "accounts": [],
                "personas": [],
                "conversation_states": [],
                "jobs": [],
                "chat_runs": [],
                "reply_profiles": reply_profiles,
            }
        try:
            accounts = (
                store.list_strategy_accounts()
                if hasattr(store, "list_strategy_accounts") else []
            )
            personas = (
                store.list_personas(status="")
                if hasattr(store, "list_personas") else []
            )
            states = (
                store.list_conversation_states(limit=limit)
                if hasattr(store, "list_conversation_states") else []
            )
            jobs = (
                store.list_auto_run_jobs(status="all", limit=limit)
                if hasattr(store, "list_auto_run_jobs") else []
            )
            chat_runs = (
                store.list_strategy_chat_runs(limit=limit)
                if hasattr(store, "list_strategy_chat_runs") else []
            )
            audit = (
                store.list_strategy_audit(limit=40)
                if hasattr(store, "list_strategy_audit") else []
            )
        except Exception as ex:
            logger.exception("strategy runtime query failed")
            raise HTTPException(500, f"策略运行状态读取失败: {type(ex).__name__}")
        for j in jobs:
            try:
                incoming = (
                    store.get_incoming_message(j.get("incoming_message_id") or "")
                    if hasattr(store, "get_incoming_message") else {}
                )
                if incoming:
                    j["incoming_text"] = incoming.get("text", "")
                    j["incoming_language"] = incoming.get("language", "")
                conv = (
                    store.get_conversation_state(j.get("customer_id") or "")
                    if hasattr(store, "get_conversation_state") else {}
                )
                if conv:
                    j["memory_summary"] = conv.get("memory_summary", "")
                chat_key = j.get("customer_id") or ""
                if conv.get("chat_key"):
                    chat_key = conv.get("chat_key")
                chat_state = (
                    store.get_chat_state(chat_key)
                    if hasattr(store, "get_chat_state") else {}
                )
                if chat_state:
                    j["last_reply"] = chat_state.get("last_reply", "")
                    j["last_peer_text"] = chat_state.get("last_peer_text", "")
            except Exception:
                logger.debug("strategy job context enrich failed", exc_info=True)
        stage_counts: Dict[str, int] = {}
        for row in states:
            stg = str(row.get("stage") or "new_lead")
            stage_counts[stg] = stage_counts.get(stg, 0) + 1
        job_counts: Dict[str, int] = {}
        for row in jobs:
            js = str(row.get("status") or "unknown")
            job_counts[js] = job_counts.get(js, 0) + 1
        avg_health = 0.0
        if accounts:
            avg_health = sum(float(a.get("health_score") or 0) for a in accounts) / len(accounts)
        return {
            "available": True,
            "summary": {
                "accounts": len(accounts),
                "personas": len(personas),
                "conversation_states": len(states),
                "jobs": len(jobs),
                "pending_jobs": job_counts.get("pending", 0),
                "running_jobs": job_counts.get("running", 0),
                "failed_jobs": job_counts.get("failed", 0),
                "avg_health": round(avg_health, 1),
                "stage_counts": stage_counts,
                "job_counts": job_counts,
                "audit": len(audit),
            },
            "accounts": accounts,
            "personas": personas,
            "conversation_states": states,
            "jobs": jobs,
            "chat_runs": chat_runs,
            "audit": audit,
            "reply_profiles": reply_profiles,
        }

    @app.post("/api/messenger-rpa/strategy/simulate")
    async def api_msgr_strategy_simulate(request: Request):
        """Dry-run the automatic strategy planner without enqueueing a job."""
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "messenger_rpa state_store 未注入")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body 必须是对象")
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text 不能为空")
        customer_id = str(
            body.get("customer_id") or body.get("chat_key") or "simulated_customer"
        ).strip()
        chat_key = str(body.get("chat_key") or customer_id).strip()
        try:
            from src.integrations.messenger_rpa.persona_runtime import AutoRunPlanner

            planner = AutoRunPlanner(store)
            return planner.plan_and_enqueue(
                customer_id=customer_id,
                chat_key=chat_key,
                text=text,
                message_id=str(body.get("message_id") or ""),
                raw_payload={"source": "web_simulator"},
                priority=int(body.get("priority") or 50),
                enqueue=False,
            )
        except Exception as ex:
            logger.exception("strategy simulate failed")
            raise HTTPException(500, f"策略模拟失败: {type(ex).__name__}: {ex}")

    @app.patch("/api/messenger-rpa/strategy/accounts/{account_id}")
    async def api_msgr_strategy_account_update(request: Request, account_id: str):
        """Update runtime account-selector fields."""
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "messenger_rpa state_store 未注入")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body 必须是对象")
        accounts = store.list_strategy_accounts()
        cur = next((a for a in accounts if str(a.get("account_id")) == account_id), None)
        if cur is None:
            raise HTTPException(404, "account not found")

        def _list(name: str) -> List[str]:
            v = body.get(name, cur.get(name) or [])
            if isinstance(v, str):
                return [x.strip() for x in v.split(",") if x.strip()]
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
            return []

        status = str(body.get("status", cur.get("status") or "active")).strip()
        if status not in {"active", "warming", "limited", "disabled", "blocked"}:
            raise HTTPException(400, "status 不合法")
        store.upsert_strategy_account(
            account_id=account_id,
            label=str(body.get("label", cur.get("label") or "")),
            status=status,
            supported_languages=_list("supported_languages"),
            supported_customer_types=_list("supported_customer_types"),
            persona_ids=_list("persona_ids"),
            health_score=float(body.get("health_score", cur.get("health_score") or 100)),
            current_load=int(body.get("current_load", cur.get("current_load") or 0)),
            daily_send_count=int(body.get("daily_send_count", cur.get("daily_send_count") or 0)),
            max_daily_send=int(body.get("max_daily_send", cur.get("max_daily_send") or 200)),
            metadata=cur.get("metadata") or {},
        )
        if hasattr(store, "append_strategy_audit"):
            store.append_strategy_audit(
                action="account.update",
                target_type="account",
                target_id=account_id,
                before=cur,
                after=next((a for a in store.list_strategy_accounts() if str(a.get("account_id")) == account_id), {}),
            )
        return {"ok": True, "account_id": account_id}

    @app.post("/api/messenger-rpa/strategy/personas")
    async def api_msgr_strategy_persona_create(request: Request):
        """Create or copy a reply profile without editing JSON manually."""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body 必须是对象")
        action = str(body.get("action") or "create").strip()
        new_id = str(body.get("id") or body.get("persona_id") or "").strip()
        if not new_id:
            raise HTTPException(400, "id 不能为空")
        mr_cfg = copy.deepcopy(_messenger_cfg(config_manager))
        rp = copy.deepcopy(_dict_cfg(mr_cfg.get("reply_profiles")))
        profiles = rp.get("profiles") if isinstance(rp.get("profiles"), list) else []
        if any(isinstance(p, dict) and str(p.get("id") or "") == new_id for p in profiles):
            raise HTTPException(400, f"persona 已存在: {new_id}")
        if action == "copy":
            source_id = str(body.get("source_id") or "").strip()
            src = next((p for p in profiles if isinstance(p, dict) and str(p.get("id") or "") == source_id), None)
            if src is None:
                raise HTTPException(404, "source persona not found")
            item = copy.deepcopy(src)
            item["id"] = new_id
            item["match_names"] = []
        else:
            item = {
                "id": new_id,
                "language": str(body.get("language") or "auto"),
                "customer_type": str(body.get("customer_type") or ""),
                "style_hint": str(body.get("style_hint") or ""),
                "match_names": [],
                "persona": {
                    "name": str(body.get("name") or new_id),
                    "role": str(body.get("role") or ""),
                    "facts": [],
                    "speaking": {"forbidden_phrases": []},
                },
            }
        profiles.append(item)
        rp["profiles"] = profiles
        if not rp.get("default"):
            rp["default"] = new_id
        normalized = _normalize_profiles(rp)
        before = copy.deepcopy(mr_cfg.get("reply_profiles") or {})
        mr_cfg["reply_profiles"] = normalized
        _save_messenger_cfg(config_manager, mr_cfg)
        _refresh_service_runtime(request, mr_cfg)
        store = _get_store(request)
        if store is not None and hasattr(store, "append_strategy_audit"):
            store.append_strategy_audit(
                action=f"persona.{action}",
                target_type="persona",
                target_id=new_id,
                before=before,
                after=item,
            )
        return {"ok": True, "reply_profiles": normalized, "persona": item}

    @app.patch("/api/messenger-rpa/strategy/personas/{persona_id}")
    async def api_msgr_strategy_persona_update(request: Request, persona_id: str):
        """Structured persona editor backed by reply_profiles config."""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body 必须是对象")
        mr_cfg = copy.deepcopy(_messenger_cfg(config_manager))
        rp = copy.deepcopy(_dict_cfg(mr_cfg.get("reply_profiles")))
        profiles = rp.get("profiles") if isinstance(rp.get("profiles"), list) else []
        idx = None
        for i, p in enumerate(profiles):
            if isinstance(p, dict) and str(p.get("id") or "") == persona_id:
                idx = i
                break
        if idx is None:
            raise HTTPException(404, "persona not found")
        item = copy.deepcopy(profiles[idx])
        for key in ("language", "customer_type", "style_hint"):
            if key in body:
                item[key] = body[key]
        if "match_names" in body:
            v = body.get("match_names")
            item["match_names"] = (
                [x.strip() for x in v.split(",") if x.strip()]
                if isinstance(v, str)
                else [str(x).strip() for x in (v or []) if str(x).strip()]
            )
        persona = item.get("persona") if isinstance(item.get("persona"), dict) else {}
        if "name" in body:
            persona["name"] = str(body.get("name") or "").strip()
        if "role" in body:
            persona["role"] = str(body.get("role") or "").strip()
        if "background_facts" in body:
            facts = body.get("background_facts")
            if isinstance(facts, str):
                facts = [x.strip() for x in facts.splitlines() if x.strip()]
            if isinstance(facts, list):
                persona["facts"] = [str(x).strip() for x in facts if str(x).strip()]
        speaking = persona.get("speaking") if isinstance(persona.get("speaking"), dict) else {}
        if "forbidden_phrases" in body:
            v = body.get("forbidden_phrases")
            speaking["forbidden_phrases"] = (
                [x.strip() for x in v.split(",") if x.strip()]
                if isinstance(v, str)
                else [str(x).strip() for x in (v or []) if str(x).strip()]
            )
        persona["speaking"] = speaking
        item["persona"] = persona
        profiles[idx] = item
        rp["profiles"] = profiles
        normalized = _normalize_profiles(rp)
        before_profiles = copy.deepcopy(mr_cfg.get("reply_profiles") or {})
        mr_cfg["reply_profiles"] = normalized
        _save_messenger_cfg(config_manager, mr_cfg)
        _refresh_service_runtime(request, mr_cfg)
        store = _get_store(request)
        if store is not None and hasattr(store, "upsert_persona"):
            try:
                from src.integrations.messenger_rpa.persona_runtime import (
                    flatten_persona_facts,
                )

                store.upsert_persona(
                    persona_id=persona_id,
                    name=str(persona.get("name") or persona_id),
                    language=str(item.get("language") or "auto"),
                    customer_type=str(item.get("customer_type") or ""),
                    facts=flatten_persona_facts(persona),
                    persona=persona,
                    status=str(item.get("status") or "active"),
                )
            except Exception:
                logger.debug("structured persona upsert to store failed", exc_info=True)
            try:
                if hasattr(store, "append_strategy_audit"):
                    store.append_strategy_audit(
                        action="persona.update",
                        target_type="persona",
                        target_id=persona_id,
                        before=before_profiles,
                        after=item,
                    )
            except Exception:
                logger.debug("persona audit failed", exc_info=True)
        return {"ok": True, "reply_profiles": normalized, "persona": item}

    @app.post("/api/messenger-rpa/strategy/personas/{persona_id}/{action}")
    async def api_msgr_strategy_persona_action(
        request: Request, persona_id: str, action: str
    ):
        api_auth(request)
        if action not in {"disable", "enable", "delete", "set_default"}:
            raise HTTPException(400, "action 只能是 disable / enable / delete / set_default")
        mr_cfg = copy.deepcopy(_messenger_cfg(config_manager))
        rp = copy.deepcopy(_dict_cfg(mr_cfg.get("reply_profiles")))
        profiles = rp.get("profiles") if isinstance(rp.get("profiles"), list) else []
        idx = next((i for i, p in enumerate(profiles) if isinstance(p, dict) and str(p.get("id") or "") == persona_id), None)
        if idx is None:
            raise HTTPException(404, "persona not found")
        before = copy.deepcopy(profiles[idx])
        if action == "set_default":
            rp["default"] = persona_id
        elif action == "delete":
            profiles.pop(idx)
            if rp.get("default") == persona_id:
                rp["default"] = str((profiles[0] or {}).get("id") or "") if profiles else ""
        else:
            profiles[idx]["status"] = "disabled" if action == "disable" else "active"
        rp["profiles"] = profiles
        normalized = _normalize_profiles(rp) if profiles else {"default": "", "profiles": []}
        mr_cfg["reply_profiles"] = normalized
        _save_messenger_cfg(config_manager, mr_cfg)
        _refresh_service_runtime(request, mr_cfg)
        store = _get_store(request)
        if store is not None and hasattr(store, "append_strategy_audit"):
            store.append_strategy_audit(
                action=f"persona.{action}",
                target_type="persona",
                target_id=persona_id,
                before=before,
                after=profiles[idx] if action != "delete" and idx < len(profiles) else {},
            )
        return {"ok": True, "reply_profiles": normalized}

    @app.patch("/api/messenger-rpa/strategy/conversations/{customer_id:path}")
    async def api_msgr_strategy_conversation_update(
        request: Request, customer_id: str
    ):
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "messenger_rpa state_store 未注入")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body 必须是对象")
        cur = store.get_conversation_state(customer_id)
        if not cur:
            raise HTTPException(404, "conversation state not found")
        action = str(body.get("action") or "update").strip()
        used = cur.get("used_persona_facts") or []
        summary = str(cur.get("memory_summary") or "")
        stage = str(body.get("stage") or cur.get("stage") or "new_lead")
        if action == "clear_used_facts":
            used = []
        elif action == "clear_memory":
            summary = ""
        elif action == "handoff":
            stage = "handoff"
        if "memory_summary" in body:
            summary = str(body.get("memory_summary") or "")
        store.update_conversation_state(
            customer_id,
            chat_key=str(body.get("chat_key") or cur.get("chat_key") or ""),
            account_id=str(body.get("account_id") or cur.get("account_id") or ""),
            persona_id=str(body.get("persona_id") or cur.get("persona_id") or ""),
            customer_language=str(body.get("customer_language") or cur.get("customer_language") or ""),
            customer_type=str(body.get("customer_type") or cur.get("customer_type") or ""),
            stage=stage,
            memory_summary=summary,
            recent_topics=list(cur.get("recent_topics") or []),
            used_persona_facts=list(used),
            metadata=cur.get("metadata") or {},
            last_message_at=float(cur.get("last_message_at") or 0),
        )
        if hasattr(store, "append_strategy_audit"):
            store.append_strategy_audit(
                action=f"conversation.{action}",
                target_type="conversation",
                target_id=customer_id,
                before=cur,
                after=store.get_conversation_state(customer_id),
            )
        return {"ok": True, "state": store.get_conversation_state(customer_id)}

    @app.post("/api/messenger-rpa/strategy/jobs/{job_id}/{action}")
    async def api_msgr_strategy_job_action(
        request: Request, job_id: str, action: str
    ):
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "messenger_rpa state_store 未注入")
        before_job = (
            store.get_auto_run_job(job_id)
            if hasattr(store, "get_auto_run_job") else {}
        )
        if action == "retry":
            ok = store.retry_auto_run_job(job_id)
        elif action == "cancel":
            ok = store.cancel_auto_run_job(job_id)
        else:
            raise HTTPException(400, "action 只能是 retry 或 cancel")
        if not ok:
            raise HTTPException(404, "job not found")
        if hasattr(store, "append_strategy_audit"):
            store.append_strategy_audit(
                action=f"job.{action}",
                target_type="job",
                target_id=job_id,
                before=before_job,
                after=(
                    store.get_auto_run_job(job_id)
                    if hasattr(store, "get_auto_run_job") else {}
                ),
            )
        return {"ok": True, "job_id": job_id, "action": action}

    @app.post("/api/messenger-rpa/strategy/audit/{audit_id}/rollback")
    async def api_msgr_strategy_audit_rollback(request: Request, audit_id: int):
        api_auth(request)
        store = _get_store(request)
        if store is None or not hasattr(store, "get_strategy_audit"):
            raise HTTPException(503, "messenger_rpa state_store 未注入")
        rec = store.get_strategy_audit(int(audit_id))
        if not rec:
            raise HTTPException(404, "audit not found")
        action = str(rec.get("action") or "")
        target_type = str(rec.get("target_type") or "")
        target_id = str(rec.get("target_id") or "")
        before = rec.get("before") if isinstance(rec.get("before"), dict) else {}
        if target_type == "persona":
            mr_cfg = copy.deepcopy(_messenger_cfg(config_manager))
            if action in {"persona.update", "persona.create", "persona.copy"}:
                if not isinstance(before.get("profiles"), list):
                    raise HTTPException(400, "该审计记录缺少可回滚的人设配置")
                mr_cfg["reply_profiles"] = _normalize_profiles(before)
            else:
                rp = copy.deepcopy(_dict_cfg(mr_cfg.get("reply_profiles")))
                profiles = rp.get("profiles") if isinstance(rp.get("profiles"), list) else []
                idx = next((i for i, p in enumerate(profiles) if isinstance(p, dict) and str(p.get("id") or "") == target_id), None)
                if action == "persona.delete":
                    if idx is None:
                        profiles.append(before)
                    else:
                        profiles[idx] = before
                elif before:
                    if idx is None:
                        profiles.append(before)
                    else:
                        profiles[idx] = before
                rp["profiles"] = profiles
                if not rp.get("default") and profiles:
                    rp["default"] = str((profiles[0] or {}).get("id") or "")
                mr_cfg["reply_profiles"] = _normalize_profiles(rp) if profiles else {"default": "", "profiles": []}
            _save_messenger_cfg(config_manager, mr_cfg)
            _refresh_service_runtime(request, mr_cfg)
        elif target_type == "account":
            if not before:
                raise HTTPException(400, "该审计记录缺少账号回滚数据")
            store.upsert_strategy_account(
                account_id=target_id,
                label=str(before.get("label") or ""),
                status=str(before.get("status") or "active"),
                supported_languages=list(before.get("supported_languages") or []),
                supported_customer_types=list(before.get("supported_customer_types") or []),
                persona_ids=list(before.get("persona_ids") or []),
                health_score=float(before.get("health_score") or 100),
                current_load=int(before.get("current_load") or 0),
                daily_send_count=int(before.get("daily_send_count") or 0),
                max_daily_send=int(before.get("max_daily_send") or 200),
                metadata=before.get("metadata") or {},
            )
        elif target_type == "conversation":
            if not before:
                raise HTTPException(400, "该审计记录缺少会话回滚数据")
            store.update_conversation_state(
                target_id,
                chat_key=str(before.get("chat_key") or ""),
                account_id=str(before.get("account_id") or ""),
                persona_id=str(before.get("persona_id") or ""),
                customer_language=str(before.get("customer_language") or ""),
                customer_type=str(before.get("customer_type") or ""),
                stage=str(before.get("stage") or "new_lead"),
                memory_summary=str(before.get("memory_summary") or ""),
                recent_topics=list(before.get("recent_topics") or []),
                used_persona_facts=list(before.get("used_persona_facts") or []),
                metadata=before.get("metadata") or {},
                last_message_at=float(before.get("last_message_at") or 0),
            )
        elif target_type == "job":
            if not before:
                raise HTTPException(400, "该审计记录缺少任务回滚数据")
            status = str(before.get("status") or "pending")
            if status == "pending":
                store.retry_auto_run_job(target_id, run_after=float(before.get("run_after") or time.time()))
            elif status == "canceled":
                store.cancel_auto_run_job(target_id, before.get("last_error") or "rollback")
            elif status == "failed":
                store.mark_auto_run_job_failed(target_id, before.get("last_error") or "rollback")
            else:
                raise HTTPException(400, f"暂不支持回滚任务状态: {status}")
        else:
            raise HTTPException(400, "暂不支持该审计类型回滚")
        if hasattr(store, "append_strategy_audit"):
            store.append_strategy_audit(
                action="audit.rollback",
                target_type=target_type,
                target_id=target_id,
                before=rec,
                after={"rolled_back_audit_id": audit_id},
            )
        return {"ok": True, "audit_id": audit_id, "rolled_back": target_id}

    @app.get("/api/messenger-rpa/bindings")
    def api_msgr_bindings(request: Request):
        """Return Messenger account ↔ phone ↔ persona bindings."""
        api_auth(request)
        return _mobile_auto_snapshot(config_manager)

    @app.get("/api/messenger-rpa/mobile-auto/devices/{device_id}/screenshot")
    async def api_msgr_mobile_auto_screenshot(
        request: Request,
        device_id: str,
        mode: str = "grid",
        max_h: int = 360,
        quality: int = 45,
    ):
        """Proxy mobile-auto0423 screenshots so the Messenger console can embed
        the same device wall without depending on browser CORS or hard-coded ports.
        """
        api_auth(request)
        mr_cfg = _messenger_cfg(config_manager)
        base = _mobile_auto_api_base(mr_cfg)
        if not base:
            raise HTTPException(503, "mobile_auto.api_base 未配置")
        q = urllib.parse.urlencode({
            "mode": str(mode or "grid"),
            "max_h": max(120, min(int(max_h or 360), 900)),
            "quality": max(20, min(int(quality or 45), 95)),
            "t": str(int(time.time() * 1000)),
        })
        url = (
            f"{base}/devices/{urllib.parse.quote(device_id, safe='')}"
            f"/screenshot?{q}"
        )
        try:
            req = urllib.request.Request(url, headers={"Accept": "image/*"})
            with urllib.request.urlopen(req, timeout=4.5) as resp:
                body = resp.read()
                ctype = resp.headers.get("content-type") or "image/jpeg"
        except urllib.error.HTTPError as ex:
            raise HTTPException(ex.code, f"mobile-auto screenshot failed: {ex.reason}")
        except Exception as ex:
            raise HTTPException(
                502,
                f"mobile-auto screenshot unavailable: {type(ex).__name__}:{ex}",
            )
        if not body:
            raise HTTPException(502, "mobile-auto screenshot empty")
        return Response(content=body, media_type=ctype)

    @app.get("/api/messenger-rpa/mobile-auto/cluster/devices/{device_id}/screenshot")
    async def api_msgr_mobile_auto_cluster_screenshot(
        request: Request,
        device_id: str,
        mode: str = "grid",
        max_h: int = 360,
        quality: int = 45,
    ):
        """Proxy mobile-auto cluster screenshots, including W03/W175 workers."""
        api_auth(request)
        mr_cfg = _messenger_cfg(config_manager)
        base = _mobile_auto_api_base(mr_cfg)
        if not base:
            raise HTTPException(503, "mobile_auto.api_base 未配置")
        q = urllib.parse.urlencode({
            "mode": str(mode or "grid"),
            "max_h": max(120, min(int(max_h or 360), 900)),
            "quality": max(20, min(int(quality or 45), 95)),
            "t": str(int(time.time() * 1000)),
        })
        url = (
            f"{base}/cluster/devices/{urllib.parse.quote(device_id, safe='')}"
            f"/screenshot?{q}"
        )
        try:
            req = urllib.request.Request(url, headers={"Accept": "image/*"})
            with urllib.request.urlopen(req, timeout=4.5) as resp:
                body = resp.read()
                ctype = resp.headers.get("content-type") or "image/jpeg"
        except urllib.error.HTTPError as ex:
            raise HTTPException(ex.code, f"mobile-auto cluster screenshot failed: {ex.reason}")
        except Exception as ex:
            raise HTTPException(
                502,
                f"mobile-auto cluster screenshot unavailable: {type(ex).__name__}:{ex}",
            )
        if not body:
            raise HTTPException(502, "mobile-auto cluster screenshot empty")
        return Response(content=body, media_type=ctype)

    @app.get("/api/messenger-rpa/mobile-auto/status")
    def api_msgr_mobile_auto_status(request: Request):
        """Aggregate mobile-auto device, VPN, performance and task status."""
        api_auth(request)
        return _mobile_auto_status_snapshot(config_manager)

    @app.post("/api/messenger-rpa/mobile-auto/devices/{device_id}/action")
    async def api_msgr_mobile_auto_device_action(request: Request, device_id: str):
        """Forward a small allow-list of mobile-auto device maintenance actions."""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(400, "body 必须是对象")
        action = str(body.get("action") or "").strip()
        is_cluster = bool(body.get("is_cluster"))
        mr_cfg = _messenger_cfg(config_manager)
        base = _mobile_auto_api_base(mr_cfg)
        if not base:
            raise HTTPException(503, "mobile_auto.api_base 未配置")

        quoted = urllib.parse.quote(device_id, safe="")
        def input_path(kind: str) -> str:
            if is_cluster:
                return f"/cluster/devices/{quoted}/input/{kind}"
            return f"/devices/{quoted}/input/{kind}"

        if action == "refresh":
            path = "/cluster/refresh-devices"
            payload: Dict[str, Any] = {}
        elif action == "reconnect":
            path = "/cluster/batch-reconnect" if is_cluster else f"/devices/{quoted}/reconnect"
            payload = {}
        elif action == "vpn_check":
            path = f"/vpn/health/{quoted}/check"
            payload = {}
        elif action in {"key_back", "key_home", "key_power"}:
            keycodes = {"key_back": 4, "key_home": 3, "key_power": 26}
            path = input_path("key")
            payload = {"keycode": keycodes[action]}
        elif action == "tap":
            x = max(0, min(9999, int(body.get("x") or 0)))
            y = max(0, min(9999, int(body.get("y") or 0)))
            path = input_path("tap")
            payload = {"x": x, "y": y}
        elif action == "tap_ratio":
            xr = max(0.0, min(1.0, float(body.get("x_ratio") or 0)))
            yr = max(0.0, min(1.0, float(body.get("y_ratio") or 0)))
            width = int(body.get("image_width") or 0)
            height = int(body.get("image_height") or 0)
            if is_cluster:
                try:
                    size_raw = _mobile_auto_post_json(
                        base,
                        f"/cluster/devices/{quoted}/shell",
                        {"command": "wm size"},
                        timeout=6.0,
                    )
                    output = str(
                        (size_raw.get("output") if isinstance(size_raw, dict) else "")
                        or ""
                    )
                    m = re.search(r"(\d{3,5})\s*x\s*(\d{3,5})", output)
                    if m:
                        width, height = int(m.group(1)), int(m.group(2))
                except Exception:
                    pass
            else:
                try:
                    size_raw = _mobile_auto_get_json(base, f"/devices/{quoted}/screen-size", timeout=4.0)
                    if isinstance(size_raw, dict):
                        width = int(size_raw.get("width") or width)
                        height = int(size_raw.get("height") or height)
                except Exception:
                    pass
            x = max(0, min(9999, round(width * xr))) if width else 0
            y = max(0, min(9999, round(height * yr))) if height else 0
            path = input_path("tap")
            payload = {"x": x, "y": y}
        elif action == "open_messenger":
            package = str(body.get("package") or "com.facebook.orca").strip()
            allowed_packages = {"com.facebook.orca", "com.facebook.katana"}
            if package not in allowed_packages:
                raise HTTPException(400, f"不允许打开的 package: {package}")
            if is_cluster:
                path = f"/cluster/devices/{quoted}/shell"
                payload = {
                    "command": (
                        f"monkey -p {package} "
                        "-c android.intent.category.LAUNCHER 1"
                    )
                }
            else:
                path = f"/devices/{quoted}/open-app"
                payload = {"package": package}
        else:
            raise HTTPException(400, f"不支持的 mobile-auto 操作: {action}")

        try:
            result = _mobile_auto_post_json(base, path, payload)
        except urllib.error.HTTPError as ex:
            raise HTTPException(ex.code, f"mobile-auto action failed: {ex.reason}")
        except Exception as ex:
            raise HTTPException(
                502,
                f"mobile-auto action unavailable: {type(ex).__name__}:{ex}",
            )
        return {
            "ok": True,
            "action": action,
            "device_id": device_id,
            "is_cluster": is_cluster,
            "path": path,
            "result": result,
        }

    @app.put("/api/messenger-rpa/bindings")
    async def api_msgr_bindings_update(request: Request):
        """Patch safe account binding fields without replacing full config."""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body 必须是对象")
        updates = body.get("accounts") or []
        if not isinstance(updates, list):
            raise HTTPException(400, "accounts 必须是数组")
        mr_cfg = copy.deepcopy(_messenger_cfg(config_manager))
        accounts = mr_cfg.get("accounts") or []
        if not isinstance(accounts, list):
            accounts = []
        by_id = {
            str(a.get("id") or a.get("account_id") or ""): a
            for a in accounts if isinstance(a, dict)
        }
        safe_fields = {
            "label", "adb_serial", "reply_profile_id", "persona_id",
            "mobile_device_id", "device_number", "device_alias",
            "login_account", "messenger_login", "line_id",
        }
        reply_profiles = _dict_cfg(mr_cfg.get("reply_profiles"))
        profile_ids = {
            str(p.get("id") or p.get("name") or "").strip()
            for p in reply_profiles.get("profiles", [])
            if isinstance(p, dict)
        }
        changed = []
        for raw in updates:
            if not isinstance(raw, dict):
                continue
            aid = str(raw.get("id") or raw.get("account_id") or "").strip()
            if not aid or aid not in by_id:
                raise HTTPException(400, f"未知 account: {aid}")
            item = by_id[aid]
            bad = [k for k in raw.keys() if k not in safe_fields and k not in ("id", "account_id")]
            if bad:
                raise HTTPException(400, f"不允许的字段: {bad}")
            has_persona_field = "reply_profile_id" in raw or "persona_id" in raw
            persona = str(raw.get("reply_profile_id") or raw.get("persona_id") or "").strip()
            if persona and profile_ids and persona not in profile_ids:
                raise HTTPException(400, f"reply_profile_id 不存在: {persona}")
            for k in safe_fields:
                if k in raw:
                    item[k] = raw[k]
            if has_persona_field:
                item["reply_profile_id"] = persona
                item.pop("persona_id", None)
            changed.append(aid)
        mr_cfg["accounts"] = accounts
        _save_messenger_cfg(config_manager, mr_cfg)
        _refresh_service_runtime(request, mr_cfg)
        snap = _mobile_auto_snapshot(config_manager)
        snap["ok"] = True
        snap["updated_accounts"] = changed
        return snap

    @app.get("/api/messenger-rpa/media")
    async def api_msgr_media(request: Request):
        """Return media/voice capabilities and config."""
        api_auth(request)
        mr_cfg = _messenger_cfg(config_manager)
        audio_stats: Dict[str, Any] = {}
        voice_input = _dict_cfg(mr_cfg.get("voice_input"))
        audio_cfg = _dict_cfg(voice_input.get("audio_pipeline")) or _dict_cfg(
            mr_cfg.get("audio_pipeline")
        )
        try:
            from src.ai.audio_pipeline import get_audio_pipeline
            audio_stats = get_audio_pipeline(audio_cfg).stats()
        except Exception as exc:
            audio_stats = {"error": f"{type(exc).__name__}: {exc}"}
        tts_stats: Dict[str, Any] = {}
        try:
            from src.ai.tts_pipeline import get_tts_pipeline
            tts_stats = get_tts_pipeline(_dict_cfg(mr_cfg.get("voice_output"))).stats()
        except Exception as exc:
            tts_stats = {"error": f"{type(exc).__name__}: {exc}"}
        media_deep = _dict_cfg(mr_cfg.get("media_deep_understand"))
        voice_output = _dict_cfg(mr_cfg.get("voice_output"))
        return {
            "config": _media_cfg(mr_cfg),
            "voice_runtime": _voice_runtime_summary(mr_cfg),
            "capabilities": {
                "receive_image": True,
                "receive_sticker": True,
                "receive_emoji": True,
                "receive_voice": True,
                "image_understanding": bool(
                    media_deep.get("enabled", True)
                ),
                "voice_transcription": bool(
                    voice_input.get("enabled", False)
                ),
                "send_voice": bool(
                    voice_output.get("enabled", False)
                ),
                "send_voice_note": "planned",
            },
            "audio_pipeline": audio_stats,
            "tts_pipeline": tts_stats,
        }

    @app.put("/api/messenger-rpa/media")
    async def api_msgr_media_update(request: Request):
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body 必须是对象")
        allowed = {
            "media_handling_policy", "media_include_links",
            "media_deep_understand", "voice_input", "voice_output",
            "emoji_policy",
        }
        bad = [k for k in body.keys() if k not in allowed]
        if bad:
            raise HTTPException(400, f"不允许的字段: {bad}")
        mr_cfg = copy.deepcopy(_messenger_cfg(config_manager))
        for k, v in body.items():
            if k in ("media_deep_understand", "voice_input", "voice_output", "emoji_policy"):
                if not isinstance(v, dict):
                    raise HTTPException(400, f"{k} 必须是对象")
                cur = mr_cfg.get(k) if isinstance(mr_cfg.get(k), dict) else {}
                merged = copy.deepcopy(cur)
                merged.update(v)
                if (
                    k == "voice_output"
                    and isinstance(cur.get("voice_profile"), dict)
                    and isinstance(v.get("voice_profile"), dict)
                ):
                    vp = copy.deepcopy(cur.get("voice_profile") or {})
                    vp.update(v.get("voice_profile") or {})
                    merged["voice_profile"] = vp
                mr_cfg[k] = merged
            else:
                mr_cfg[k] = v
        _save_messenger_cfg(config_manager, mr_cfg)
        try:
            from src.ai.audio_pipeline import reset_audio_pipeline
            reset_audio_pipeline()
        except Exception:
            logger.debug("reset_audio_pipeline failed", exc_info=True)
        try:
            from src.ai.tts_pipeline import reset_tts_pipeline
            reset_tts_pipeline()
        except Exception:
            logger.debug("reset_tts_pipeline failed", exc_info=True)
        _refresh_service_runtime(request, mr_cfg)
        return {"ok": True, "config": _media_cfg(mr_cfg)}

    @app.post("/api/messenger-rpa/media/asr-test")
    async def api_msgr_media_asr_test(request: Request):
        """Transcribe a local audio file path using the currently selected ASR."""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
        path = str((body or {}).get("path") or "").strip()
        if not path:
            raise HTTPException(400, "path 必填")
        p = Path(path)
        if not p.exists() or not p.is_file():
            raise HTTPException(404, "音频文件不存在")
        mr_cfg = _messenger_cfg(config_manager)
        vi = _dict_cfg(mr_cfg.get("voice_input"))
        ap_cfg = _dict_cfg(vi.get("audio_pipeline")) or _dict_cfg(
            mr_cfg.get("audio_pipeline")
        )
        ap_cfg["enabled"] = True
        try:
            from src.ai.audio_pipeline import reset_audio_pipeline, get_audio_pipeline
            reset_audio_pipeline()
            ap = get_audio_pipeline(ap_cfg)
            rv = await ap.transcribe_file(
                str(p),
                language_hint=str((body or {}).get("language_hint") or "").strip() or None,
                timeout_sec=float((body or {}).get("timeout_sec") or vi.get("timeout_sec") or 30),
            )
            return {"ok": rv.ok, "result": rv.__dict__}
        except Exception as exc:
            raise HTTPException(500, f"ASR 测试失败: {type(exc).__name__}: {exc}")

    @app.post("/api/messenger-rpa/media/tts-test")
    async def api_msgr_media_tts_test(request: Request):
        """Generate a TTS preview file; does not send anything to Messenger."""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
        text = str((body or {}).get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text 必填")
        if len(text) > 500:
            raise HTTPException(400, "试听文本最多 500 字")
        mr_cfg = _messenger_cfg(config_manager)
        vo_cfg = _dict_cfg(mr_cfg.get("voice_output"))
        vo_cfg["enabled"] = True
        try:
            from src.ai.tts_pipeline import reset_tts_pipeline, get_tts_pipeline
            reset_tts_pipeline()
            tts = get_tts_pipeline(vo_cfg)
            rv = await tts.synthesize(
                text,
                voice=str((body or {}).get("voice") or "").strip() or None,
                timeout_sec=float((body or {}).get("timeout_sec") or vo_cfg.get("timeout_sec") or 30),
            )
            return {"ok": rv.ok, "result": rv.__dict__}
        except Exception as exc:
            raise HTTPException(500, f"TTS 试听失败: {type(exc).__name__}: {exc}")

    @app.get("/api/messenger-rpa/leads")
    def api_msgr_leads(request: Request, limit: int = 100):
        """List recent Messenger contacts with ICP/qualification evidence."""
        api_auth(request)
        stores = _iter_account_stores(request, config_manager)
        if not stores:
            raise HTTPException(503, "state_store 未注入")
        cfg = _messenger_cfg(config_manager)
        reply_profiles = _dict_cfg(cfg.get("reply_profiles"))
        contexts = _load_bot_contexts(
            config_manager,
            limit=max(int(limit or 100) * 3, 300),
        )
        bindings = _binding_index(config_manager)
        runs_by_chat: Dict[str, List[Dict[str, Any]]] = {}
        approvals_by_chat: Dict[str, List[Dict[str, Any]]] = {}
        chat_states: List[tuple[Dict[str, Any], Dict[str, Any]]] = []
        store_by_chat: Dict[str, Dict[str, Any]] = {}
        for si in stores:
            store = si.get("store")
            if store is None:
                continue
            if hasattr(store, "list_chat_states"):
                for st in store.list_chat_states(limit=max(int(limit or 100) * 3, 300)):
                    chat_key = str(st.get("chat_key") or "")
                    chat_states.append((si, st))
                    store_by_chat[chat_key] = si
            try:
                for r in store.recent_runs(limit=1000):
                    ck = str(r.get("chat_key") or "")
                    if ck:
                        runs_by_chat.setdefault(ck, []).append(r)
                        store_by_chat.setdefault(ck, si)
            except Exception:
                logger.debug("lead runs load failed", exc_info=True)
            try:
                for a in store.list_approvals(status=None, limit=1000):
                    ck = str(a.get("chat_key") or "")
                    if ck:
                        approvals_by_chat.setdefault(ck, []).append(a)
                        store_by_chat.setdefault(ck, si)
            except Exception:
                logger.debug("lead approvals load failed", exc_info=True)

        items: List[Dict[str, Any]] = []
        seen = set()
        for si, st in chat_states:
            chat_key = str(st.get("chat_key") or "")
            seen.add(chat_key)
            chat_name = str(st.get("chat_name") or chat_key)
            ctx = contexts.get(chat_key) or {}
            account_id = _account_id_from_chat_key(chat_key, str(si.get("account_id") or ""))
            item = _build_lead_item(
                chat_key=chat_key,
                chat_name=chat_name or _chat_name_from_key(chat_key),
                store_info=si,
                st=st,
                ctx=ctx,
                reply_profiles=reply_profiles,
                lead_qualification_cfg=_dict_cfg(cfg.get("lead_qualification")),
                binding=bindings.get(account_id) or {},
                runs=runs_by_chat.get(chat_key) or [],
                approvals=approvals_by_chat.get(chat_key) or [],
            )
            try:
                credit = si["store"].get_credit(chat_key)
            except Exception:
                credit = {}
            item["credit"] = credit.get("credit", 100)
            item["credit_reason"] = credit.get("last_reason", "")
            try:
                item["operator_handoff"] = si["store"].get_handoff(chat_key)
            except Exception:
                item["operator_handoff"] = {}
            items.append(item)
        for chat_key, ctx in contexts.items():
            if chat_key in seen:
                continue
            chat_name = str(ctx.get("chat_title") or chat_key.split(":", 1)[-1])
            resolved_store, resolved_si = _store_info_for_chat_key(
                request,
                chat_key,
                config_manager,
            )
            si = store_by_chat.get(chat_key) or resolved_si or {
                "account_id": _account_id_from_chat_key(chat_key, "default"),
                "label": _account_id_from_chat_key(chat_key, "default"),
                "store": resolved_store or stores[0].get("store"),
            }
            account_id = _account_id_from_chat_key(chat_key, str(si.get("account_id") or ""))
            item = _build_lead_item(
                chat_key=chat_key,
                chat_name=chat_name,
                store_info=si,
                st={},
                ctx=ctx,
                reply_profiles=reply_profiles,
                lead_qualification_cfg=_dict_cfg(cfg.get("lead_qualification")),
                binding=bindings.get(account_id) or {},
                runs=runs_by_chat.get(chat_key) or [],
                approvals=approvals_by_chat.get(chat_key) or [],
            )
            item["credit"] = 100
            item["credit_reason"] = ""
            try:
                item["operator_handoff"] = si["store"].get_handoff(chat_key)
            except Exception:
                item["operator_handoff"] = {}
            items.append(item)
        items.sort(key=lambda x: (int(x.get("score") or 0), float(x.get("updated_at") or 0)), reverse=True)
        total = len(items)
        high = sum(1 for x in items if int(x.get("score") or 0) >= 80)
        mid = sum(1 for x in items if 40 <= int(x.get("score") or 0) < 80)
        low = sum(1 for x in items if int(x.get("score") or 0) < 40)
        ops = _lead_ops_summary(items)
        return {
            "items": items[:max(1, min(int(limit or 100), 1000))],
            "summary": {
                "total": total,
                "high": high,
                "mid": mid,
                "low": low,
                **ops,
            },
            "ts": time.time(),
        }

    @app.get("/api/messenger-rpa/leads/{chat_key:path}")
    async def api_msgr_lead_detail(
        request: Request,
        chat_key: str,
        history_limit: int = 80,
    ):
        """Customer handoff dossier for human operators."""
        api_auth(request)
        if not chat_key:
            raise HTTPException(400, "chat_key 为空")
        cfg = _messenger_cfg(config_manager)
        reply_profiles = _dict_cfg(cfg.get("reply_profiles"))
        bindings = _binding_index(config_manager)
        account_id = _account_id_from_chat_key(chat_key, "")
        store, selected = _store_info_for_chat_key(request, chat_key, config_manager)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        st = {}
        if store is not None and hasattr(store, "get_chat_state"):
            st = store.get_chat_state(chat_key) or {}
        ctx = (_load_bot_contexts(config_manager, chat_key=chat_key).get(chat_key) or {})
        chat_name = str(
            st.get("chat_name")
            or ctx.get("chat_title")
            or _chat_name_from_key(chat_key)
        )
        runs: List[Dict[str, Any]] = []
        approvals: List[Dict[str, Any]] = []
        if store is not None:
            try:
                runs = [
                    r for r in store.recent_runs(limit=1000)
                    if str(r.get("chat_key") or "") == chat_key
                ]
            except Exception:
                logger.debug("lead detail runs failed", exc_info=True)
            try:
                approvals = store.list_approvals(status=None, chat_key=chat_key, limit=1000)
            except Exception:
                logger.debug("lead detail approvals failed", exc_info=True)
        account_id = account_id or str(selected.get("account_id") or "")
        binding = bindings.get(account_id) or {}
        item = _build_lead_item(
            chat_key=chat_key,
            chat_name=chat_name,
            store_info=selected,
            st=st,
            ctx=ctx,
            reply_profiles=reply_profiles,
            lead_qualification_cfg=_dict_cfg(cfg.get("lead_qualification")),
            binding=binding,
            runs=runs,
            approvals=approvals,
        )
        try:
            handoff_record = store.get_handoff(chat_key)
        except Exception:
            handoff_record = {}
        item["operator_handoff"] = handoff_record
        hist = ctx.get("_conversation_history") or []
        if not isinstance(hist, list):
            hist = []
        max_hist = max(1, min(int(history_limit or 80), 300))
        lead = item.get("lead") if isinstance(item.get("lead"), dict) else {}
        profile = {
            "country": lead.get("country", ""),
            "gender": lead.get("gender", ""),
            "age_range": lead.get("age_range", ""),
            "occupation": lead.get("occupation", ""),
            "occupation_tier": lead.get("occupation_tier", ""),
            "income_band": lead.get("income_band", ""),
            "income_confidence": lead.get("income_confidence", 0),
            "need_tags": lead.get("need_tags") or [],
            "relationship_stage": lead.get("stage", ""),
            "reply_lang": item.get("reply_lang", ""),
        }
        summary = str(ctx.get("_conversation_summary") or "").strip()
        if not summary:
            summary = item.get("summary_short") or ""
        handoff_brief = {
            "one_line": item.get("summary_short") or "",
            "detailed_summary": summary,
            "handoff_advice": item.get("handoff") or {},
            "known_facts": [
                f"{k}: {v}" for k, v in profile.items()
                if v not in ("", None, [], {})
            ][:12],
        }
        return {
            "ok": True,
            "lead": item,
            "account": {
                "account_id": account_id,
                "account_label": item.get("account_label", ""),
                "adb_serial": item.get("adb_serial", ""),
                "device_number": item.get("device_number", ""),
                "device_alias": item.get("device_alias", ""),
                "login_account": item.get("login_account", ""),
                "line_id": item.get("line_id", ""),
            },
            "customer_profile": profile,
            "handoff_brief": handoff_brief,
            "operator_handoff": handoff_record,
            "timeline": _timeline_for_chat(
                runs=runs,
                approvals=approvals,
                limit=max_hist,
            ),
            "history_turns": hist[-max_hist:],
            "approval_rows": approvals[:max_hist],
            "run_rows": runs[:max_hist],
        }

    @app.put("/api/messenger-rpa/leads/{chat_key:path}/handoff")
    async def api_msgr_lead_handoff_update(request: Request, chat_key: str):
        """Persist human-operator follow-up state for a lead."""
        api_auth(request)
        if not chat_key:
            raise HTTPException(400, "chat_key 为空")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body 必须是对象")
        allowed = {
            "owner",
            "status",
            "line_status",
            "priority",
            "outcome",
            "notes",
            "next_followup_at",
            "updated_by",
        }
        bad = [k for k in body.keys() if k not in allowed]
        if bad:
            raise HTTPException(400, f"不允许的字段: {bad}")
        if "status" in body and str(body.get("status") or "") not in _HANDOFF_STATUSES:
            raise HTTPException(400, "status 不合法")
        if (
            "line_status" in body
            and str(body.get("line_status") or "") not in _LINE_HANDOFF_STATUSES
        ):
            raise HTTPException(400, "line_status 不合法")
        if "priority" in body and str(body.get("priority") or "") not in _HANDOFF_PRIORITIES:
            raise HTTPException(400, "priority 不合法")
        next_followup_at = None
        if "next_followup_at" in body:
            raw = body.get("next_followup_at")
            if raw in ("", None):
                next_followup_at = 0.0
            else:
                try:
                    next_followup_at = max(0.0, float(raw))
                except Exception:
                    raise HTTPException(400, "next_followup_at 必须是时间戳数字")
        store, selected = _store_info_for_chat_key(request, chat_key, config_manager)
        if store is None or not hasattr(store, "upsert_handoff"):
            raise HTTPException(503, "state_store 不支持 handoff")
        account_id = _account_id_from_chat_key(
            chat_key,
            str(selected.get("account_id") or ""),
        )
        try:
            record = store.upsert_handoff(
                chat_key,
                account_id=account_id,
                owner=body.get("owner") if "owner" in body else None,
                status=body.get("status") if "status" in body else None,
                line_status=body.get("line_status") if "line_status" in body else None,
                priority=body.get("priority") if "priority" in body else None,
                outcome=body.get("outcome") if "outcome" in body else None,
                notes=body.get("notes") if "notes" in body else None,
                next_followup_at=next_followup_at,
                updated_by=str(body.get("updated_by") or "web"),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "handoff": record}

    @app.get("/api/messenger-rpa/recent")
    def api_msgr_recent(request: Request, limit: int = 50):
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        return {"runs": store.recent_runs(limit=int(limit or 50))}

    # ── P8-2: 聊天历史分析 ────────────────────────────────

    @app.get("/api/messenger-rpa/sessions/{chat_key:path}")
    def api_msgr_sessions(request: Request, chat_key: str):
        """按 4h 间隔分组的会话摘要。"""
        api_auth(request)
        store = _get_store(request)
        if store is None:
            return {"available": False, "sessions": []}
        return {"available": True, "sessions": store.sessions_for_chat(chat_key), "chat_key": chat_key}

    @app.get("/api/messenger-rpa/chat-history/{chat_key:path}")
    def api_msgr_chat_history(request: Request, chat_key: str,
                               limit: int = 10, offset: int = 0):
        """分页拉取指定联系人的对话记录（含 intent_tag）。"""
        api_auth(request)
        store = _get_store(request)
        if store is None:
            return {"available": False, "messages": [], "total": 0}
        msgs = store.chat_history(chat_key, limit=limit, offset=offset)
        total = store.total_turns_for_chat(chat_key)
        return {"available": True, "messages": msgs, "total": total, "offset": offset}

    @app.get("/api/messenger-rpa/customer-profile/{chat_key:path}")
    def api_msgr_customer_profile(request: Request, chat_key: str):
        """联系人全量画像（历史统计 + 意图分布）。"""
        api_auth(request)
        store = _get_store(request)
        if store is None:
            return {"available": False, "profile": {}}
        return {"available": True, "profile": store.customer_profile(chat_key), "chat_key": chat_key}

    @app.get("/api/messenger-rpa/search")
    def api_msgr_search(request: Request, q: str = "", intent: str = "",
                         days: int = 30, limit: int = 20):
        """跨联系人全文检索。"""
        api_auth(request)
        store = _get_store(request)
        if store is None:
            return {"available": False, "results": [], "q": q}
        results = store.search_history(q, intent=intent, days=days, limit=min(limit, 50))
        return {"available": True, "results": results, "q": q, "intent": intent}

    @app.get("/api/messenger-rpa/intent-stats")
    def api_msgr_intent_stats(request: Request, hours: float = 168.0):
        """近 N 小时意图分布统计。"""
        api_auth(request)
        store = _get_store(request)
        if store is None:
            return {"available": False, "distribution": {}, "total_turns": 0}
        stats = store.intent_stats(window_hours=hours)
        return {"available": True, **stats}

    # ── REST: 审批 ─────────────────────────────────
    @app.get("/api/messenger-rpa/approvals")
    def api_msgr_approvals(
        request: Request,
        status: Optional[str] = "pending",
        limit: int = 50,
        chat_key: Optional[str] = None,
        reply_text_empty: Optional[bool] = None,
    ):
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        norm_status: Optional[str] = (
            None if status in ("", "all", "any") else status
        )
        return {
            "approvals": store.list_approvals(
                status=norm_status,
                chat_key=chat_key,
                reply_text_empty=reply_text_empty,
                limit=int(limit or 50),
            ),
        }

    @app.get("/api/messenger-rpa/approvals/{approval_id}")
    async def api_msgr_approval_detail(
        request: Request, approval_id: int
    ):
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        item = store.get_approval(int(approval_id))
        if not item:
            raise HTTPException(404, f"approval #{approval_id} not found")
        return item

    @app.post("/api/messenger-rpa/approvals/{approval_id}/approve")
    async def api_msgr_approval_approve(
        request: Request, approval_id: int
    ):
        api_auth(request)
        store = _get_store(request)
        svc = _get_service(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        decided_by = str(body.get("decided_by") or "web") or "web"
        note = str(body.get("note") or "")
        # ★ P1-2：允许人工在批准时修改 reply_text（商业化场景常见）
        reply_override_raw = body.get("reply_text")
        reply_override: Optional[str] = None
        if isinstance(reply_override_raw, str):
            candidate = reply_override_raw.strip()
            if candidate:
                reply_override = candidate

        ok = store.decide_approval(
            int(approval_id),
            approve=True,
            decided_by=decided_by,
            decision_note=note,
            reply_text_override=reply_override,
        )
        if not ok:
            raise HTTPException(
                409, f"approval #{approval_id} 状态非 pending，无法批准"
            )

        # 立即触发 service 走一次 send（后台 task，不阻塞响应）
        send_result: Dict[str, Any] = {"requested": False}
        if svc is not None and hasattr(svc, "send_approved_now"):
            try:
                send_result = await svc.send_approved_now(int(approval_id))
            except Exception as ex:
                logger.exception("send_approved_now 异常")
                send_result = {
                    "requested": True,
                    "ok": False,
                    "error": f"{type(ex).__name__}:{ex}",
                }
        return {"ok": True, "approval_id": approval_id, "send": send_result}

    @app.post("/api/messenger-rpa/approvals/{approval_id}/update")
    async def api_msgr_approval_update(
        request: Request, approval_id: int
    ):
        """仅修改 pending 审批的 reply_text，不改变状态。用于"先改文案再决定"。"""
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        new_text = str(body.get("reply_text") or "").strip()
        if not new_text:
            raise HTTPException(400, "reply_text 不能为空")
        ok = store.update_approval_reply(int(approval_id), reply_text=new_text)
        if not ok:
            raise HTTPException(
                409, f"approval #{approval_id} 状态非 pending，无法修改"
            )
        return {"ok": True, "approval_id": approval_id, "reply_text": new_text}

    @app.post("/api/messenger-rpa/approvals/{approval_id}/suggest")
    async def api_msgr_approval_suggest(
        request: Request, approval_id: int
    ):
        """让 SkillManager 基于相同 peer_text 再生成一条候选。

        不覆盖现有 reply_text，返回 {suggestions:[new_text]}；前端可让
        人工对比后决定是否 /update 覆盖。
        """
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        item = store.get_approval(int(approval_id))
        if not item:
            raise HTTPException(404, f"approval #{approval_id} not found")
        if item.get("status") != "pending":
            raise HTTPException(409, "仅 pending 审批支持 Suggest More")

        # 反向调用 SkillManager：不污染实际 conversation_history
        sm = getattr(request.app.state, "skill_manager", None)
        if sm is None:
            # 有些装配路径放在 telegram_client 下
            tg = getattr(request.app.state, "telegram_client", None)
            sm = getattr(tg, "skill_manager", None) if tg else None
        if sm is None:
            raise HTTPException(503, "SkillManager 未注入")

        import asyncio
        import uuid as _uuid

        peer_text = str(item.get("peer_text") or "").strip() or "[空]"
        chat_key = str(item.get("chat_key") or "")
        chat_title = str(item.get("chat_name") or "Messenger Friend")
        peer_kind = str(item.get("peer_kind") or "text")
        cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
        style_hint = str(cfg.get("style_hint") or "").strip()

        ctx = {
            "chat_id": int(_uuid.uuid4().int % (10**9)),  # 临时 id，避免污染真实 chat
            "request_id": f"suggest-{_uuid.uuid4().hex[:10]}",
            "channel": "messenger_rpa",
            "reply_lang": str(cfg.get("default_reply_lang", "zh")),
            "chat_title": chat_title,
            "messenger_rpa_chat_key": f"suggest:{chat_key}",
            "messenger_rpa_peer_kind": peer_kind,
        }
        if style_hint:
            ctx["messenger_rpa_style_hint"] = style_hint

        try:
            payload = await asyncio.wait_for(
                sm.process_message(
                    peer_text,
                    f"suggest:{chat_key}",  # 临时 user_id，独立上下文
                    context=ctx,
                ),
                timeout=45.0,
            )
        except asyncio.TimeoutError:
            raise HTTPException(504, "Suggest More 超时 (>45s)")
        except Exception as ex:
            logger.exception("Suggest More 异常")
            raise HTTPException(
                500, f"suggest failed: {type(ex).__name__}:{ex}"
            )

        if isinstance(payload, dict):
            suggestion = str(payload.get("reply") or payload.get("text") or "")
        else:
            suggestion = str(payload or "")
        suggestion = suggestion.strip()
        return {
            "ok": True,
            "approval_id": approval_id,
            "suggestion": suggestion,
            "original_reply_text": item.get("reply_text") or "",
        }

    # ── P2-6 / P6-3：批量审批（增强）─────────────────
    @app.post("/api/messenger-rpa/approvals/batch")
    async def api_msgr_approval_batch(request: Request):
        """批量批准 / 驳回。P6-3 增强：

        body 字段：
          - ids: int[] 必填（或传 filter 让后端查询 pending）
          - filter: {chat_key?: str, tier?: str, max: int}
            若同时给 ids 与 filter → 取两者交集
          - action: "approve" | "reject"
          - decided_by: str
          - note: str
          - reason: "spam"|"irrelevant"|"low_quality"|"manual" （仅 reject 扣分用，
                    不同 reason 映射到 credit_policy.reject_delta_map；缺省走
                    credit_policy.reject_delta）
          - dry_run: bool  仅返回预览，不真改 DB / 不发送
          - pacing_sec: float  approve 后每条发送间停顿（防 adb 撞车），默认 3.0

        返回：{ok, dry_run, processed, succeeded_ids, failed, send_results}
        """
        import asyncio as _asyncio
        api_auth(request)
        store = _get_store(request)
        svc = _get_service(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        try:
            body = await request.json()
        except Exception:
            body = {}
        action = str(body.get("action") or "").strip().lower()
        if action not in ("approve", "reject"):
            raise HTTPException(400, "action 必须是 approve 或 reject")
        decided_by = str(body.get("decided_by") or "web") or "web"
        note = str(body.get("note") or "")
        reason = str(body.get("reason") or "manual").strip().lower()
        dry_run = bool(body.get("dry_run", False))
        pacing_sec = max(0.0, float(body.get("pacing_sec", 3.0) or 3.0))
        raw_ids = body.get("ids") or []
        filt = body.get("filter") or {}

        # 解析 ids
        ids_set: set = set()
        for raw_id in raw_ids[:500]:
            try:
                ids_set.add(int(raw_id))
            except (TypeError, ValueError):
                pass

        # 解析 filter
        if isinstance(filt, dict) and filt:
            try:
                pending = store.list_approvals(
                    status="pending", limit=int(filt.get("max", 500) or 500),
                )
            except Exception:
                pending = []
            want_ck = str(filt.get("chat_key") or "").strip()
            want_tier = str(filt.get("tier") or "").strip().lower()
            matched: set = set()
            for it in pending:
                if want_ck and str(it.get("chat_key") or "") != want_ck:
                    continue
                if want_tier and (
                    str(it.get("ai_tier") or "").lower() != want_tier
                ):
                    continue
                try:
                    matched.add(int(it["id"]))
                except Exception:
                    continue
            if ids_set:
                ids_set &= matched  # 交集
            else:
                ids_set = matched

        ids = sorted(ids_set)[:100]  # 单次最多 100 条
        if not ids:
            raise HTTPException(400, "ids 解析结果为空（或过滤后无匹配）")

        # ★ dry_run：仅返回预览
        if dry_run:
            preview = []
            for aid in ids:
                it = store.get_approval(aid) or {}
                preview.append({
                    "id": aid,
                    "status": it.get("status"),
                    "chat_key": it.get("chat_key"),
                    "chat_name": it.get("chat_name"),
                    "reply_preview": (it.get("reply_text") or "")[:80],
                    "will_act": it.get("status") == "pending",
                })
            return {
                "ok": True, "dry_run": True, "action": action,
                "processed": len(ids), "preview": preview,
            }

        # ★ 真正执行
        cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
        cred_cfg = cfg.get("credit_policy") or {}
        reject_delta_map: Dict[str, int] = {
            "spam": -30, "irrelevant": -10, "low_quality": -15, "manual": -15,
        }
        for k, v in (cred_cfg.get("reject_delta_map") or {}).items():
            try:
                reject_delta_map[str(k).lower()] = int(v)
            except (TypeError, ValueError):
                pass
        default_delta = int(cred_cfg.get("reject_delta", -15) or -15)

        succeeded: list = []
        failed: list = []
        send_results: list = []
        for idx, aid in enumerate(ids):
            try:
                ok = store.decide_approval(
                    aid, approve=(action == "approve"),
                    decided_by=decided_by, decision_note=note,
                )
            except Exception as ex:
                failed.append({"id": aid, "reason": f"exception:{type(ex).__name__}"})
                continue
            if not ok:
                failed.append({"id": aid, "reason": "not_pending"})
                continue
            succeeded.append(aid)

            # ── reject 扣信用（带 reason 分类）──
            if action == "reject" and cred_cfg.get("enabled", True):
                try:
                    item = store.get_approval(aid) or {}
                    ck = str(item.get("chat_key") or "")
                    if ck:
                        delta = reject_delta_map.get(reason, default_delta)
                        store.adjust_credit(
                            ck, delta,
                            reason=f"batch_reject:{reason}:{note or decided_by}"[:200],
                        )
                except Exception:
                    logger.debug("P6-3 batch reject 扣分失败", exc_info=True)

            # ── approve 后真发（顺序串行 + pacing 防 adb 撞车）──
            if action == "approve" and svc is not None:
                try:
                    sr = await svc.send_approved_now(aid)
                    send_results.append({"id": aid, **(sr or {})})
                except Exception as ex:
                    send_results.append(
                        {"id": aid, "requested": True,
                         "error": f"{type(ex).__name__}: {ex}"}
                    )
                # 不是最后一条 → 停一下
                if idx < len(ids) - 1 and pacing_sec > 0:
                    await _asyncio.sleep(pacing_sec)

        return {
            "ok": True, "dry_run": False, "action": action,
            "processed": len(ids),
            "succeeded_ids": succeeded, "failed": failed,
            "send_results": send_results,
        }

    # ── P2-8：Prometheus 指标暴露（无新增依赖，自写文本格式）────
    @app.get("/api/messenger-rpa/metrics")
    async def api_msgr_metrics(request: Request):
        """Prometheus exposition format (text/plain; version=0.0.4)。

        抓取建议：
          scrape_configs:
            - job_name: messenger_rpa
              metrics_path: /api/messenger-rpa/metrics
              static_configs: [{targets: [host:18787]}]
              authorization: {type: Bearer, credentials: <AUTH_TOKEN>}
        """
        api_auth(request)
        from fastapi.responses import PlainTextResponse
        svc = _get_service(request)
        store = _get_store(request)
        lines: list = []

        def _emit(name: str, help_text: str, typ: str, value, labels: Dict[str, str] = None):
            if value is None:
                return
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {typ}")
            label_str = ""
            if labels:
                parts = [
                    f'{k}="{str(v).replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}"'
                    for k, v in labels.items()
                ]
                label_str = "{" + ",".join(parts) + "}"
            try:
                vnum = float(value)
            except (TypeError, ValueError):
                return
            lines.append(f"{name}{label_str} {vnum}")

        # 1) 服务状态
        if svc is not None:
            try:
                st = svc.status()
            except Exception:
                st = {}
            _emit(
                "messenger_rpa_service_running",
                "1 if RPA loop task is alive", "gauge",
                1 if st.get("running") else 0,
            )
            _emit(
                "messenger_rpa_notif_running",
                "1 if notification watcher is alive", "gauge",
                1 if st.get("notif_running") else 0,
            )
            _emit(
                "messenger_rpa_sla_running",
                "1 if approval SLA loop is alive", "gauge",
                1 if st.get("sla_running") else 0,
            )
            _emit(
                "messenger_rpa_consecutive_empty",
                "Consecutive empty inbox polls", "gauge",
                st.get("consecutive_empty", 0),
            )
            _emit(
                "messenger_rpa_consecutive_unhealthy",
                "Consecutive device-unhealthy ticks", "gauge",
                st.get("consecutive_unhealthy", 0),
            )
            _emit(
                "messenger_rpa_sla_alerts_sent_total",
                "Total SLA overdue alerts pushed since start", "counter",
                st.get("sla_alert_sent_total", 0),
            )
            _emit(
                "messenger_rpa_notif_events_total",
                "Total incoming notification events since start", "counter",
                st.get("notif_event_count", 0),
            )
            # send counters
            sc = st.get("send_counters") or {}
            _emit(
                "messenger_rpa_sends_today",
                "Messenger messages sent today", "gauge",
                sc.get("today", 0),
            )
            # SLA stats
            sla = st.get("approval_sla") or {}
            _emit(
                "messenger_rpa_approvals_pending",
                "Approvals in pending state", "gauge",
                sla.get("pending_count", 0),
            )
            _emit(
                "messenger_rpa_approvals_overdue",
                "Pending approvals older than SLA threshold", "gauge",
                sla.get("overdue_count", 0),
            )
            _emit(
                "messenger_rpa_approvals_oldest_age_seconds",
                "Age of the oldest pending approval in seconds", "gauge",
                sla.get("oldest_age_sec", 0),
            )
            _emit(
                "messenger_rpa_approvals_sla_threshold_seconds",
                "Configured SLA threshold in seconds", "gauge",
                sla.get("threshold_sec", 0),
            )
            # ★ P3-1：风控指标
            risk = st.get("risk") or {}
            status_map = {"normal": 0, "warning_once": 1, "blocked": 2}
            _emit(
                "messenger_rpa_risk_status",
                "Account risk status (0=normal,1=warn,2=blocked)",
                "gauge",
                status_map.get(str(risk.get("status") or "normal"), 0),
            )
            _emit(
                "messenger_rpa_risk_hit_count",
                "Consecutive vision risk hits not yet cleared", "gauge",
                risk.get("hit_count", 0),
            )
            _emit(
                "messenger_rpa_risk_blocked_until_ts",
                "Risk-blocked pause expiration unix ts (0 if not blocked)",
                "gauge",
                risk.get("blocked_until_ts", 0),
            )
            # ★ P4-3：节奏学习指标
            pace = st.get("pace") or {}
            if pace:
                _emit(
                    "messenger_rpa_pace_ratio",
                    "Current-hour send count / historical median (0=no data)",
                    "gauge", pace.get("ratio", 0),
                )
                _emit(
                    "messenger_rpa_pace_current_hour_count",
                    "Sends in the current local hour",
                    "gauge", pace.get("current_hour_count", 0),
                )
                _emit(
                    "messenger_rpa_pace_hist_median",
                    "Historical median of sends at this hour",
                    "gauge", pace.get("hist_median", 0),
                )
                decision_map = {"allow": 0, "throttle": 1, "deny": 2,
                                "allow_on_error": -1}
                _emit(
                    "messenger_rpa_pace_decision",
                    "Pace decision (0=allow 1=throttle 2=deny -1=err)",
                    "gauge",
                    decision_map.get(str(pace.get("decision")), 0),
                )
            # ★ P4-7：信用分分布
            credit = st.get("credit") or {}
            dist = credit.get("distribution") or {}
            if dist:
                lines.append(
                    "# HELP messenger_rpa_chat_credit_distribution"
                    " Chats grouped by credit bucket"
                )
                lines.append(
                    "# TYPE messenger_rpa_chat_credit_distribution gauge"
                )
                for bucket, cnt in dist.items():
                    lines.append(
                        f'messenger_rpa_chat_credit_distribution'
                        f'{{bucket="{bucket}"}} {cnt}'
                    )
                _emit(
                    "messenger_rpa_chat_credit_tracked_total",
                    "Chats with non-default credit", "gauge",
                    credit.get("total_tracked", 0),
                )
                _emit(
                    "messenger_rpa_chat_credit_low_total",
                    "Chats with credit < 40 (force approve or worse)",
                    "gauge",
                    len(credit.get("low_credit_chats") or []),
                )
        # ★ P3-4：进程级 histogram
        try:
            from src.integrations.messenger_rpa.metrics import get_metrics
            md = get_metrics().dump()
        except Exception:
            md = {}
        if md:
            rh = md.get("run_duration") or {}
            if rh.get("count"):
                lines.append("# HELP messenger_rpa_run_duration_seconds End-to-end run_once duration")
                lines.append("# TYPE messenger_rpa_run_duration_seconds histogram")
                cum = rh.get("cum_counts") or []
                for i, b in enumerate(rh.get("buckets") or []):
                    if i < len(cum):
                        lines.append(
                            f'messenger_rpa_run_duration_seconds_bucket{{le="{b}"}} {cum[i]}'
                        )
                if cum:
                    lines.append(
                        f'messenger_rpa_run_duration_seconds_bucket{{le="+Inf"}} {cum[-1]}'
                    )
                lines.append(f"messenger_rpa_run_duration_seconds_sum {rh['sum']}")
                lines.append(f"messenger_rpa_run_duration_seconds_count {rh['count']}")
            # phase histograms（按 phase label 维度输出）
            ph = md.get("phase_duration") or {}
            if any(h.get("count") for h in ph.values()):
                lines.append(
                    "# HELP messenger_rpa_phase_duration_seconds Run phase latency"
                )
                lines.append(
                    "# TYPE messenger_rpa_phase_duration_seconds histogram"
                )
                for phase_name, h in ph.items():
                    if not h.get("count"):
                        continue
                    cum = h.get("cum_counts") or []
                    for i, b in enumerate(h.get("buckets") or []):
                        if i < len(cum):
                            lines.append(
                                f'messenger_rpa_phase_duration_seconds_bucket'
                                f'{{phase="{phase_name}",le="{b}"}} {cum[i]}'
                            )
                    if cum:
                        lines.append(
                            f'messenger_rpa_phase_duration_seconds_bucket'
                            f'{{phase="{phase_name}",le="+Inf"}} {cum[-1]}'
                        )
                    lines.append(
                        f'messenger_rpa_phase_duration_seconds_sum'
                        f'{{phase="{phase_name}"}} {h["sum"]}'
                    )
                    lines.append(
                        f'messenger_rpa_phase_duration_seconds_count'
                        f'{{phase="{phase_name}"}} {h["count"]}'
                    )
            # outcome counters
            outc = md.get("run_outcomes") or {}
            if outc:
                lines.append("# HELP messenger_rpa_runs_total Run outcomes since process start")
                lines.append("# TYPE messenger_rpa_runs_total counter")
                for k, v in outc.items():
                    lines.append(
                        f'messenger_rpa_runs_total{{outcome="{k}"}} {v}'
                    )
            caps = md.get("caption_sources") or {}
            if caps:
                lines.append("# HELP messenger_rpa_caption_source_total Caption resolution source")
                lines.append("# TYPE messenger_rpa_caption_source_total counter")
                for k, v in caps.items():
                    lines.append(
                        f'messenger_rpa_caption_source_total{{source="{k}"}} {v}'
                    )
            # P1-E1: 守卫触发计数（reason 维度）
            guards = md.get("guard_skips") or {}
            if guards:
                lines.append(
                    "# HELP messenger_rpa_guard_skips_total "
                    "Safety-guard skip events by reason "
                    "(P0-A inbox/P0-B thread/P0-C runaway/P1-C XML)"
                )
                lines.append(
                    "# TYPE messenger_rpa_guard_skips_total counter"
                )
                for k, v in guards.items():
                    safe = (
                        str(k)
                        .replace("\\", "\\\\")
                        .replace('"', '\\"')
                        .replace("\n", " ")
                    )
                    lines.append(
                        f'messenger_rpa_guard_skips_total'
                        f'{{reason="{safe}"}} {v}'
                    )

        # 2) 按 variant 指标
        if store is not None:
            try:
                vs = store.variant_stats()
            except Exception:
                vs = {"variants": {}}
            for vname, d in (vs.get("variants") or {}).items():
                labels = {"variant": vname}
                _emit(
                    "messenger_rpa_variant_chats",
                    "Chats assigned to this variant", "gauge",
                    d.get("chats", 0), labels=labels,
                )
                _emit(
                    "messenger_rpa_variant_escalations_active",
                    "Chats currently in escalation cooldown", "gauge",
                    d.get("escalations_active", 0), labels=labels,
                )
                for st_name in ("pending", "approved", "sent", "rejected"):
                    _emit(
                        f"messenger_rpa_variant_approvals_{st_name}",
                        f"Approvals with status={st_name} by variant",
                        "gauge",
                        d.get(f"apr_{st_name}", 0), labels=labels,
                    )
                _emit(
                    "messenger_rpa_variant_approve_ratio",
                    "sent / (sent + rejected) per variant", "gauge",
                    d.get("approve_ratio"), labels=labels,
                )

        # ★ P11.8 (2026-05-04)：messenger_rpa hint counters → Prometheus
        # 输出 messenger_rpa_hint_total{name="<hint_base>"} <count>
        try:
            from src.monitoring.metrics_store import get_metrics_store
            _hint_metrics = get_metrics_store().get_messenger_rpa_metrics()
            for _name, _cnt in sorted(_hint_metrics.items()):
                _emit(
                    "messenger_rpa_hint_total",
                    "Cumulative messenger_rpa hint event count",
                    "counter",
                    _cnt,
                    labels={"name": _name},
                )
        except Exception:
            pass

        body = "\n".join(lines) + "\n"
        # ★ P6-4：附上 LLM cost/tokens per (model, tier, account)
        try:
            from src.ai.llm_cost import get_llm_cost
            body += get_llm_cost().dump_prom()
        except Exception:
            pass
        # ★ P57：附上翻译引擎用量（attempts/fail/fallbacks per engine）
        try:
            from src.ai.translation_engine_stats import get_translation_engine_stats
            body += get_translation_engine_stats().dump_prom()
        except Exception:
            pass
        # ★ V：附上语音克隆合成的语言纠正用量（total/corrected/by-lang）
        try:
            from src.ai.voice_synth_stats import get_voice_synth_stats
            body += get_voice_synth_stats().dump_prom()
        except Exception:
            pass
        # ★ P58：附上通用 provider 用量（OCR/ASR 等）
        try:
            from src.ai.provider_stats import all_provider_prom
            body += all_provider_prom()
        except Exception:
            pass
        return PlainTextResponse(
            content=body,
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # ── P6-4：LLM 成本 / token JSON API ─────────────
    @app.get("/api/messenger-rpa/llm-cost")
    async def api_msgr_llm_cost(request: Request):
        """返回 LLM 成本 & tokens 的分桶聚合（JSON，供运营看板）。"""
        api_auth(request)
        try:
            from src.ai.llm_cost import get_llm_cost
            return get_llm_cost().dump()
        except Exception as ex:
            raise HTTPException(500, f"llm_cost.dump 失败: {ex}")

    # ── P2-3：A/B persona 指标 ──────────────────────
    @app.get("/api/messenger-rpa/variants/stats")
    async def api_msgr_variants_stats(request: Request):
        """按 variant 聚合 Messenger 指标。"""
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
        exp = cfg.get("persona_experiment") or {}
        out = store.variant_stats()
        out["experiment_enabled"] = bool(exp.get("enabled", False))
        out["variants_config"] = [
            {"name": v.get("name"), "weight": v.get("weight")}
            for v in (exp.get("variants") or [])
            if isinstance(v, dict)
        ]
        return out

    # ── P5-1：账号注册表 ───────────────────────────
    @app.get("/api/messenger-rpa/accounts")
    async def api_msgr_accounts(request: Request):
        """列出所有已注册 account（含状态 db 路径、serial、pool 锁状态）。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未注入")
        reg = getattr(svc, "_account_registry", None)
        if reg is None:
            raise HTTPException(503, "account_registry 未初始化")
        return reg.stats()

    # ── P6-1：按账号精确触发 ─────────────────────────
    @app.post("/api/messenger-rpa/accounts/{account_id}/trigger")
    async def api_msgr_account_trigger(request: Request, account_id: str):
        """立即触发指定账号跑一次 run_once（跳过轮询节奏）。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未注入")
        reg = getattr(svc, "_account_registry", None)
        if reg is None or reg.get(account_id) is None:
            raise HTTPException(404, f"未知 account: {account_id}")
        try:
            r = await svc.trigger_once(account_id=account_id)
            return {"ok": True, "account_id": account_id, "result": r}
        except Exception as ex:
            raise HTTPException(500, f"trigger 失败: {ex}")

    @app.post("/api/messenger-rpa/accounts/{account_id}/send-to")
    async def api_msgr_account_send_to(request: Request, account_id: str):
        """指定账号设备：打开 Messenger → 匹配 chat_name 会话 → 发送 text（不经 LLM）。

        Body JSON: ``{"chat_name": "...", "text": "..."}``（兼容 ``message`` / ``reply_text``）
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未注入")
        reg = getattr(svc, "_account_registry", None)
        if reg is None or reg.get(account_id) is None:
            raise HTTPException(404, f"未知 account: {account_id}")
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        chat_name = str(
            body.get("chat_name") or body.get("chat") or "",
        ).strip()
        text = str(
            body.get("text")
            or body.get("message")
            or body.get("reply_text")
            or "",
        ).strip()
        if not chat_name or not text:
            raise HTTPException(
                400,
                "需要 JSON: {\"chat_name\":\"...\",\"text\":\"...\"}",
            )
        try:
            r = await svc.send_to_chat_name_for_account(
                account_id,
                chat_name=chat_name,
                reply_text=text,
            )
            return {"ok": bool(r.get("ok")), "account_id": account_id, "result": r}
        except Exception as ex:
            raise HTTPException(500, f"send-to 失败: {ex}")

    # ── P1-E2：紧急停发（运营舆情应急一键灭火）─────────────
    @app.post("/api/messenger-rpa/accounts/{account_id}/chats/emergency_stop")
    async def api_msgr_emergency_stop(request: Request, account_id: str):
        """🚨 一键紧急停发：把指定 chat 加入永久 skip 黑名单 + 设短期内存
        cooldown，立即停止所有自动回复。

        Body JSON: ``{"chat_name": "...", "reason"?: "...",
                       "self_skip_sec"?: 1800}``
          - chat_name 必填
          - reason 默认 "emergency_stop"，写入 skipped_chats.reason
          - self_skip_sec 默认 1800（30 分钟），同时写 P0-4 持久化 self_skip
            表（重启后仍然生效），并通过 _PersistentSelfSkipDict.__setitem__
            同步刷新 runner 内存 dict。设 0 则跳过 self_skip 仅留永久黑名单。

        三层保护：
          1. messenger_rpa_skipped_chats 表 → is_skipped_chat() 永久跳过
             （inbox 阶段 ck check + send_gate L3 都会拦）
          2. messenger_rpa_self_skip 表（P0-4）→ norm_key 维度 cooldown，
             重启自动回填到 _self_skip_until 内存 dict
          3. runner._self_skip_until 内存 dict → 当下生效，不需要等任何
             其他事件触发

        响应：``{"ok": true, "account_id": "...", "chat_name": "...",
                   "chat_key": "...", "reason": "...",
                   "self_skip_until_ts": 1700000000.0}``
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未注入")
        reg = getattr(svc, "_account_registry", None)
        if reg is None:
            raise HTTPException(503, "account_registry 未初始化")
        if reg.get(account_id) is None:
            raise HTTPException(404, f"未知 account: {account_id}")
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(400, "body 必须是 JSON 对象")
        chat_name = str(body.get("chat_name") or "").strip()
        if not chat_name:
            raise HTTPException(400, "chat_name 必填")
        reason = str(body.get("reason") or "emergency_stop").strip()
        try:
            self_skip_sec = float(body.get("self_skip_sec", 1800) or 0)
        except (TypeError, ValueError):
            self_skip_sec = 1800.0
        try:
            runner = svc._get_or_create_runner(account_id)
        except Exception as ex:
            raise HTTPException(500, f"runner 初始化失败: {ex}")
        chat_key = f"{runner._chat_key_prefix}:{chat_name}"
        try:
            runner._state.add_skipped_chat(
                chat_key, chat_name=chat_name, reason=reason,
            )
        except Exception as ex:
            raise HTTPException(
                500, f"add_skipped_chat 失败: {type(ex).__name__}:{ex}",
            )
        self_skip_until = 0.0
        if self_skip_sec > 0:
            try:
                from src.integrations.messenger_rpa.runner import (
                    _self_skip_norm_key,
                )
                norm_key = _self_skip_norm_key(chat_name)
                # 写到 runner 内存 dict 触发 P0-4 同步落库
                # （_PersistentSelfSkipDict.__setitem__ 会自动 epoch->mono 转换）
                _mono_until = (
                    __import__("time").monotonic() + self_skip_sec
                )
                runner._self_skip_until[norm_key] = _mono_until
                self_skip_until = (
                    __import__("time").time() + self_skip_sec
                )
            except Exception:
                # self_skip 失败不致命，永久黑名单已写入
                pass
        return {
            "ok": True,
            "account_id": account_id,
            "chat_name": chat_name,
            "chat_key": chat_key,
            "reason": reason,
            "self_skip_until_ts": self_skip_until,
            "self_skip_sec": self_skip_sec,
        }

    @app.delete(
        "/api/messenger-rpa/accounts/{account_id}/chats/emergency_stop",
    )
    async def api_msgr_emergency_stop_release(
        request: Request, account_id: str,
    ):
        """🔓 释放紧急停发：从黑名单移除 + 清 self_skip cooldown。

        Body JSON 或 query: ``{"chat_name": "..."}``
        响应：``{"ok": true, "removed_blacklist": true|false,
                  "cleared_self_skip": true|false}``
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未注入")
        reg = getattr(svc, "_account_registry", None)
        if reg is None or reg.get(account_id) is None:
            raise HTTPException(404, f"未知 account: {account_id}")
        # body / query 都接受
        chat_name = ""
        try:
            body = await request.json()
            if isinstance(body, dict):
                chat_name = str(body.get("chat_name") or "").strip()
        except Exception:
            pass
        if not chat_name:
            chat_name = str(
                request.query_params.get("chat_name") or "",
            ).strip()
        if not chat_name:
            raise HTTPException(400, "chat_name 必填")
        try:
            runner = svc._get_or_create_runner(account_id)
        except Exception as ex:
            raise HTTPException(500, f"runner 初始化失败: {ex}")
        chat_key = f"{runner._chat_key_prefix}:{chat_name}"
        removed_bl = False
        cleared_ss = False
        try:
            removed_bl = bool(runner._state.remove_skipped_chat(chat_key))
        except Exception:
            pass
        try:
            from src.integrations.messenger_rpa.runner import (
                _self_skip_norm_key,
            )
            norm_key = _self_skip_norm_key(chat_name)
            if norm_key in runner._self_skip_until:
                # _PersistentSelfSkipDict.__delitem__ 会清 DB 表
                del runner._self_skip_until[norm_key]
                cleared_ss = True
        except Exception:
            pass
        return {
            "ok": True,
            "account_id": account_id,
            "chat_name": chat_name,
            "chat_key": chat_key,
            "removed_blacklist": removed_bl,
            "cleared_self_skip": cleared_ss,
        }

    @app.get(
        "/api/messenger-rpa/accounts/{account_id}/chats/skipped",
    )
    async def api_msgr_skipped_chats_list(
        request: Request, account_id: str,
    ):
        """列出该账号当前所有被永久跳过的 chat（含 reason 和创建时间）。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未注入")
        reg = getattr(svc, "_account_registry", None)
        if reg is None or reg.get(account_id) is None:
            raise HTTPException(404, f"未知 account: {account_id}")
        try:
            runner = svc._get_or_create_runner(account_id)
        except Exception as ex:
            raise HTTPException(500, f"runner 初始化失败: {ex}")
        try:
            limit = int(request.query_params.get("limit") or 100)
        except (TypeError, ValueError):
            limit = 100
        try:
            rows = runner._state.list_skipped_chats(limit=limit)
        except Exception as ex:
            raise HTTPException(500, f"list_skipped_chats 失败: {ex}")
        return {
            "ok": True,
            "account_id": account_id,
            "count": len(rows),
            "chats": rows,
        }

    # ── P4-7：信用分 ────────────────────────────────
    @app.get("/api/messenger-rpa/credits")
    async def api_msgr_credits(request: Request):
        """返回所有 tracked chat 的信用分分布 + 低信用名单。"""
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        return store.credit_stats()

    @app.post("/api/messenger-rpa/credits/{chat_key}/reset")
    async def api_msgr_credit_reset(request: Request, chat_key: str):
        """把某 chat 的信用分重置到 100（运营手工介入）。"""
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        cur = store.get_credit(chat_key)
        delta = 100 - int(cur.get("credit", 100))
        r = store.adjust_credit(chat_key, delta, reason="manual_reset")
        return {"ok": True, "chat_key": chat_key, "new_credit": r.get("credit")}

    # ── P3-7：回放包列表 ────────────────────────────
    @app.get("/api/messenger-rpa/replays")
    async def api_msgr_replays(request: Request, limit: int = 50):
        """列出失败 run 的回放 zip 包。"""
        api_auth(request)
        cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
        try:
            from src.integrations.messenger_rpa.replay import list_replays
            items, base = list_replays(cfg, limit=max(1, min(int(limit), 500)))
        except Exception as ex:
            raise HTTPException(500, f"list_replays 失败: {ex}")
        return {"base_dir": str(base), "total": len(items), "items": items}

    # ── P4-6：Replay Rerun (脱机重跑 LLM) ───────────
    @app.post("/api/messenger-rpa/replays/rerun")
    async def api_msgr_replay_rerun(request: Request):
        """脱机重跑某个 zip 里的 LLM 调用，不碰设备。

        body: {zip: "<basename>" | "<abs-path>", override_chat_key?: str}
        return: {old_reply, new_reply, text_for_ai, diff_hint}
        """
        api_auth(request)
        cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        zip_arg = str(body.get("zip") or "").strip()
        if not zip_arg:
            raise HTTPException(400, "zip 参数必填")
        try:
            from src.integrations.messenger_rpa.replay import rerun_from_zip
            result = await rerun_from_zip(
                zip_arg,
                cfg,
                request.app,
                override_chat_key=str(body.get("override_chat_key") or "").strip() or None,
            )
        except FileNotFoundError as ex:
            raise HTTPException(404, str(ex))
        except Exception as ex:
            logger.exception("replay rerun 失败")
            raise HTTPException(500, f"{type(ex).__name__}: {ex}")
        return result

    # ── P2-6：快捷模板 ──────────────────────────────
    @app.get("/api/messenger-rpa/templates")
    async def api_msgr_templates(request: Request):
        """返回配置的快捷回复模板（每次请求都重读 config，支持热加载）。"""
        api_auth(request)
        cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
        tpls = cfg.get("approval_templates") or []
        # 规范化 + 过滤非法项
        out = []
        for t in tpls:
            if not isinstance(t, dict):
                continue
            label = str(t.get("label") or "").strip()
            text = str(t.get("text") or "").strip()
            if label and text:
                out.append({"label": label, "text": text})
        return {"templates": out}

    @app.post("/api/messenger-rpa/approvals/{approval_id}/reject")
    async def api_msgr_approval_reject(
        request: Request, approval_id: int
    ):
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        decided_by = str(body.get("decided_by") or "web") or "web"
        note = str(body.get("note") or "")
        ok = store.decide_approval(
            int(approval_id),
            approve=False,
            decided_by=decided_by,
            decision_note=note,
        )
        if not ok:
            raise HTTPException(
                409, f"approval #{approval_id} 状态非 pending，无法驳回"
            )
        # ★ P4-7：reject → 扣信用
        try:
            cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
            cred_cfg = cfg.get("credit_policy") or {}
            if cred_cfg.get("enabled", True):
                item = store.get_approval(int(approval_id))
                ck = str(item.get("chat_key") or "") if item else ""
                if ck:
                    delta = int(cred_cfg.get("reject_delta", -15) or -15)
                    r = store.adjust_credit(
                        ck, delta, reason=f"reject: {note or decided_by}"[:200],
                    )
                    logger.info(
                        "[messenger_rpa] P4-7 reject credit chat=%s delta=%d → %d",
                        ck, delta, r.get("credit", -1),
                    )
        except Exception:
            logger.debug("P4-7 reject credit 扣分失败", exc_info=True)
        return {"ok": True, "approval_id": approval_id}

    # ── REST: 控制 ─────────────────────────────────
    @app.post("/api/messenger-rpa/trigger")
    async def api_msgr_trigger(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未构建")
        # 若 loop 没跑，先 force_start 再 trigger
        auto_started = False
        if hasattr(svc, "is_running") and not svc.is_running:
            if hasattr(svc, "force_start"):
                auto_started = await svc.force_start()
        result = await svc.trigger_once()
        if isinstance(result, dict):
            result["auto_started"] = auto_started
            result["is_running"] = getattr(svc, "is_running", None)
            return result
        return {"ok": True, "auto_started": auto_started, "is_running": getattr(svc, "is_running", None)}

    @app.post("/api/messenger-rpa/pause")
    async def api_msgr_pause(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未构建")
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            seconds = float(body.get("seconds", 300))
        except (TypeError, ValueError):
            seconds = 300.0
        svc.pause_for(max(seconds, 0))
        return {"ok": True, "paused_for": seconds}

    @app.post("/api/messenger-rpa/resume")
    async def api_msgr_resume(request: Request):
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未构建")
        svc.resume()
        return {"ok": True}

    # ── REST: 设备状态面板 ────────────────────────────
    @app.get("/api/messenger-rpa/devices")
    async def api_msgr_devices(request: Request):
        """返回配置设备的在线/屏幕/锁屏状态（不触发 wake，快速只读）。"""
        api_auth(request)
        from src.integrations.messenger_rpa.device_health import probe_devices
        serials: list = []
        svc = _get_service(request)
        if svc is not None and hasattr(svc, "configured_adb_serials"):
            serials = list(svc.configured_adb_serials())
        if not serials:
            cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
            primary = (cfg.get("adb_serial") or "").strip()
            extras = cfg.get("extra_serials") or []
            if primary:
                serials.append(primary)
            for s in extras:
                s = (s or "").strip()
                if s and s not in serials:
                    serials.append(s)
        if not serials:
            return {
                "devices": [],
                "hint": (
                    "messenger_rpa 未配置任何 adb_serial"
                    "（accounts[].adb_serial 或顶层 adb_serial）"
                ),
            }
        results = probe_devices(serials)
        return {"devices": [results[s] for s in serials]}

    # ── REST: 一键校准 ────────────────────────────────
    @app.post("/api/messenger-rpa/calibrate")
    async def api_msgr_calibrate(request: Request):
        """手动触发一次 Inbox 坐标校准。成功会把 calibration 写入
        tmp_messenger_rpa/calibrations/<serial>.json。
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未构建")
        if not hasattr(svc, "calibrate_now"):
            raise HTTPException(501, "service.calibrate_now 不可用")
        try:
            r = await svc.calibrate_now()
            return {"ok": bool(r.get("ok")), "result": r}
        except Exception as ex:
            logger.exception("calibrate_now 异常")
            raise HTTPException(
                500, f"calibrate failed: {type(ex).__name__}:{ex}"
            )

    # ── REST: 对话历史查看（诊断 AI 记忆） ──────────────
    @app.get("/api/messenger-rpa/chat/history")
    async def api_msgr_chat_history(
        request: Request,
        chat_key: str,
        limit: int = 20,
    ):
        """读 bot.db 的 user_context 里当前 chat_key 持久化的 _conversation_history。

        用于运营确认「AI 到底记住了什么」，比光看 runner 单次回复更直观。
        """
        api_auth(request)
        if not chat_key:
            raise HTTPException(400, "chat_key 为空")
        # bot.db 位置随 skill_manager
        try:
            import json
            import sqlite3
            from pathlib import Path

            cfg_dir = Path(config_manager.config_path).parent
            db = cfg_dir / "bot.db"
            if not db.exists():
                raise HTTPException(404, f"bot.db 不存在: {db}")
            c = sqlite3.connect(str(db))
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT data, updated_at FROM user_context WHERE user_id = ?",
                (chat_key,),
            ).fetchone()
            c.close()
            if not row:
                return {
                    "chat_key": chat_key,
                    "exists": False,
                    "turns": [],
                    "summary": "",
                    "last_message": "",
                    "last_reply": "",
                }
            d: Dict[str, Any] = {}
            try:
                d = json.loads(row["data"]) or {}
            except Exception:
                pass
            hist = d.get("_conversation_history") or []
            if limit and len(hist) > int(limit) * 2:
                hist = hist[-int(limit) * 2:]
            return {
                "chat_key": chat_key,
                "exists": True,
                "updated_at": row["updated_at"],
                "turns": hist,
                "turn_count": len(hist) // 2,
                "summary": d.get("_conversation_summary") or "",
                "last_message": d.get("last_message") or "",
                "last_reply": d.get("last_reply") or "",
                "reply_count": d.get("reply_count", 0),
                "current_intent": d.get("current_intent", ""),
                "intent_chain": d.get("_intent_chain") or [],
            }
        except HTTPException:
            raise
        except Exception as ex:
            logger.exception("chat history 读取异常")
            raise HTTPException(
                500, f"history read failed: {type(ex).__name__}:{ex}"
            )

    # ── REST: AdbKeyboard 自动安装 ────────────────────
    @app.post("/api/messenger-rpa/install-adbkeyboard")
    async def api_msgr_install_adbkeyboard(request: Request):
        """对 adb_serial 指定设备跑 ensure_adbkeyboard_installed。
        APK 从 tools/ADBKeyboard.apk 读取。
        """
        api_auth(request)
        cfg = (config_manager.config or {}).get("messenger_rpa", {}) or {}
        serial = (cfg.get("adb_serial") or "").strip()
        if not serial:
            raise HTTPException(400, "messenger_rpa.adb_serial 未配置")
        ime = (
            cfg.get("adb_keyboard_ime")
            or "com.android.adbkeyboard/.AdbIME"
        )
        pkg = (
            cfg.get("adb_keyboard_package") or "com.android.adbkeyboard"
        )
        from src.integrations.line_rpa import adb_helpers as adb
        try:
            info = adb.ensure_adbkeyboard_installed(
                serial, package=pkg, ime_component=ime, auto_enable=True,
            )
            return {"ok": bool(info.get("installed")), "info": info}
        except Exception as ex:
            logger.exception("ensure_adbkeyboard_installed 异常")
            raise HTTPException(
                500, f"install failed: {type(ex).__name__}:{ex}"
            )

    # ── 账号健康看板：所有账号深度状态 ──────────────────
    @app.get("/api/messenger-rpa/accounts/health")
    async def api_msgr_accounts_health(request: Request, deep: bool = False):
        """对所有已注册账号执行健康检查。

        ``?deep=true`` 时额外检查 ADB Keyboard 安装情况（耗时约 3-5s）。
        返回每台手机的 ADB 状态、屏幕、锁屏、暂停、UI unsafe 等字段。
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未注入")
        if not hasattr(svc, "accounts_health"):
            raise HTTPException(501, "accounts_health 不可用")
        try:
            result = await svc.accounts_health(deep=bool(deep))
            return result
        except Exception as ex:
            logger.exception("accounts_health 异常")
            raise HTTPException(500, f"health check 失败: {type(ex).__name__}:{ex}")

    # ── 账号级暂停 ──────────────────────────────────
    @app.post("/api/messenger-rpa/accounts/{account_id}/pause")
    async def api_msgr_account_pause(request: Request, account_id: str):
        """暂停指定账号 N 秒（默认 300s）。不影响其他账号。

        Body JSON（可选）: ``{"seconds": 300}``
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未注入")
        reg = getattr(svc, "_account_registry", None)
        if reg is None or reg.get(account_id) is None:
            raise HTTPException(404, f"未知 account: {account_id}")
        body: Dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        seconds = float(body.get("seconds", 300) or 300)
        svc.pause_account(account_id, seconds)
        return {"ok": True, "account_id": account_id, "paused_for_sec": seconds}

    # ── 账号级恢复 ──────────────────────────────────
    @app.post("/api/messenger-rpa/accounts/{account_id}/resume")
    async def api_msgr_account_resume(request: Request, account_id: str):
        """恢复指定账号，同时清除 ui_unsafe 标记。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未注入")
        reg = getattr(svc, "_account_registry", None)
        if reg is None or reg.get(account_id) is None:
            raise HTTPException(404, f"未知 account: {account_id}")
        svc.resume_account(account_id)
        return {"ok": True, "account_id": account_id}

    # ── 清除 UI unsafe 标记（不自动恢复，需再调 resume） ──
    @app.post("/api/messenger-rpa/accounts/{account_id}/clear-unsafe")
    async def api_msgr_account_clear_unsafe(request: Request, account_id: str):
        """仅清除 ui_unsafe 标记，暂停计时仍然有效。
        若需立即恢复，请改用 /accounts/{id}/resume。
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未注入")
        reg = getattr(svc, "_account_registry", None)
        if reg is None or reg.get(account_id) is None:
            raise HTTPException(404, f"未知 account: {account_id}")
        svc.clear_account_ui_unsafe(account_id)
        return {"ok": True, "account_id": account_id}

    # ── 转化漏斗 + A/B 指标看板 ──────────────────────────
    @app.get("/api/messenger-rpa/funnel")
    def api_msgr_funnel(request: Request):
        """转化漏斗 + Persona A/B 实验指标。

        返回字段：
          funnel        - 各 Journey 阶段当前存量
          conversions   - 关键转化率（engaged/handoff/line_add/line_engage/overall）
          variants      - Persona A/B 各变体审批通过率
          handoff       - 引流话术注入/发送/跳过统计（进程级计数器）
          ab_conclusions- 策略 A/B 测试结论（conclusive/inconclusive/insufficient）
        """
        api_auth(request)
        svc = _get_service(request)

        # 1. 转化漏斗（来自 contacts store）
        funnel: Dict[str, int] = {}
        conversions: Dict[str, Any] = {}
        try:
            cs = getattr(request.app.state, "contacts", None)
            if cs is not None:
                funnel = cs.store.count_journeys_by_stage()
        except Exception:
            pass

        if funnel:
            def _pct(num_key: str, den_key: str) -> Optional[float]:
                n = funnel.get(num_key, 0)
                d = funnel.get(den_key, 0)
                return round(n / d * 100, 1) if d else None

            # 关键阶段（缺失补 0）
            _stages = ["INITIAL", "ENGAGED", "HANDOFF_READY", "HANDOFF_SENT",
                       "LINE_ADDED", "LINE_ACCEPTED", "LINE_ENGAGED", "BONDED",
                       "LOST_HANDOFF", "LOST_LINE_SILENT"]
            funnel = {s: funnel.get(s, 0) for s in _stages}

            total = sum(
                funnel.get(s, 0) for s in
                ["INITIAL", "ENGAGED", "HANDOFF_READY", "HANDOFF_SENT",
                 "LINE_ADDED", "LINE_ACCEPTED", "LINE_ENGAGED", "BONDED"]
            )
            line_engaged = funnel.get("LINE_ENGAGED", 0) + funnel.get("BONDED", 0)
            conversions = {
                "engaged_rate":     _pct("ENGAGED", "INITIAL"),
                "handoff_rate":     _pct("HANDOFF_SENT", "ENGAGED"),
                "line_add_rate":    _pct("LINE_ADDED", "HANDOFF_SENT"),
                "line_engage_rate": (
                    round(line_engaged / funnel.get("LINE_ADDED", 0) * 100, 1)
                    if funnel.get("LINE_ADDED") else None
                ),
                "overall_rate": (
                    round(line_engaged / total * 100, 1) if total else None
                ),
                "total_journeys": total,
            }

        # 2. Persona A/B variant stats
        variants: Dict[str, Any] = {}
        if svc is not None:
            try:
                store = getattr(svc, "_state", None)
                if store is not None:
                    vs = store.variant_stats()
                    variants = vs.get("variants", {})
            except Exception:
                pass

        # 3. Handoff 进程级计数器
        handoff: Dict[str, Any] = {}
        try:
            from src.integrations.messenger_rpa.metrics import get_metrics
            m = get_metrics().dump()
            handoff = {
                "injected_total":  m.get("handoff_injected_total", 0),
                "sent_total":      m.get("handoff_sent_total", 0),
                "by_script":       m.get("handoff_by_script", {}),
                "skipped_reasons": m.get("handoff_skipped", {}),
                "sends_total":     m.get("sends_total", 0),
                "inject_rate": (
                    round(m["handoff_injected_total"] / m["sends_total"] * 100, 1)
                    if m.get("sends_total") else None
                ),
            }
        except Exception:
            pass

        # 4. 策略 A/B 测试结论
        ab_conclusions: list = []
        try:
            from src.utils.strategy_advisor import evaluate_ab_tests
            sm = getattr(svc, "_sm", None) if svc else None
            if sm is not None:
                ab_tests = getattr(sm, "_ab_tests", {}) or {}
                strategies = getattr(sm, "_strategies", {}) or {}
                tracker = getattr(sm, "_strategy_tracker", None)
                if ab_tests and tracker is not None:
                    summary = tracker.get_summary() if hasattr(
                        tracker, "get_summary") else []
                    ab_conclusions = evaluate_ab_tests(
                        ab_tests, summary, strategies)
        except Exception:
            pass

        return {
            "funnel": funnel,
            "conversions": conversions,
            "variants": variants,
            "handoff": handoff,
            "ab_conclusions": ab_conclusions,
            "ts": __import__("time").time(),
        }

    # ── P1: helper — sync chat persona binding to PersonaManager (write-through) ──
    def _sync_persona_to_pm(
        chat_name: str,
        account_id: str,
        profile_id: Optional[str],
    ) -> None:
        """Mirror a chat-persona binding write to PersonaManager so the runner
        picks it up immediately without restarting.

        profile_id=None → unbind (falls back to auto-match next message).
        Silently no-ops on any error so the primary SQLite write is never blocked.
        """
        try:
            from src.utils.persona_manager import PersonaManager
            from src.integrations.messenger_rpa.state_store import mrpa_chat_cid
            pm = PersonaManager.get_instance()
            svc = getattr(
                getattr(request, "app", None),
                "state", type("_", (), {})()
            )
            svc_obj = getattr(svc, "messenger_rpa_service", None)
            prefix = "messenger_rpa"
            try:
                if svc_obj is not None:
                    reg = getattr(svc_obj, "_account_registry", None)
                    merged = getattr(svc_obj, "_merged_cfg", {})
                    if reg is not None and account_id:
                        ctx = reg.get(account_id)
                        if ctx is not None:
                            mc = ctx.merged_config(merged)
                            prefix = mc.get("chat_key_prefix") or "messenger_rpa"
                    elif reg is not None:
                        # No account_id: use primary account's prefix
                        all_ctx = reg.all_contexts()
                        if all_ctx:
                            mc0 = all_ctx[0].merged_config(merged)
                            prefix = mc0.get("chat_key_prefix") or "messenger_rpa"
            except Exception:
                pass
            cid = mrpa_chat_cid(chat_name, prefix)
            if profile_id:
                ok = pm.bind_chat_persona_by_profile_id(str(cid), profile_id)
                if not ok:
                    logger.debug(
                        "[mrpa_routes] PM double-write: profile_id=%r not in PM store "
                        "(chat=%r prefix=%s) — will bind on next runner message",
                        profile_id, chat_name, prefix,
                    )
            else:
                pm.unbind_chat_persona(str(cid))
        except Exception:
            logger.debug("[mrpa_routes] _sync_persona_to_pm 异常", exc_info=True)

    # ── 跨账号协调器快照（活跃聊天锁 + 画像缓存） ──
    @app.get("/api/messenger-rpa/coordinator")
    async def api_msgr_coordinator(request: Request):
        """返回 CrossAccountCoordinator 快照：
        - active_chats: 当前各用户由哪个账号处理（聊天锁）
        - portrait_cache: 各用户最新画像缓存元信息（account、时间、年龄）
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "service 未注入")
        return svc.coordinator_snapshot()

    # ── B2: per-chat persona binding API ──
    # 让运营在 Web 后台为每个 chat 单独指定人设；批量为账号下所有 chat 设人设
    @app.get("/api/messenger-rpa/chat-persona-bindings")
    async def api_msgr_chat_persona_bindings_list(request: Request):
        """列出所有 chat 的运营手动绑定人设。
        query 参数：account_id（可选，过滤特定账号）"""
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        account_id = str(request.query_params.get("account_id") or "").strip()
        try:
            bindings = store.list_chat_persona_overrides(account_id=account_id)
            return {"ok": True, "bindings": bindings, "count": len(bindings)}
        except Exception as ex:
            raise HTTPException(500, f"list bindings failed: {ex}")

    @app.put("/api/messenger-rpa/chat-persona-bindings/{chat_name}")
    async def api_msgr_chat_persona_binding_set(
        request: Request, chat_name: str,
    ):
        """运营单聊指定 reply_profile_id。
        body: {"reply_profile_id": "...", "account_id": "...", "notes": "..."}"""
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body 必须是对象")
        reply_profile_id = str(body.get("reply_profile_id") or "").strip()
        if not reply_profile_id:
            raise HTTPException(400, "reply_profile_id is required")
        account_id = str(body.get("account_id") or "").strip()
        notes = str(body.get("notes") or "")
        try:
            ok = store.upsert_chat_persona_override(
                chat_name=chat_name,
                reply_profile_id=reply_profile_id,
                account_id=account_id,
                bound_by="web_admin",
                notes=notes,
            )
            # P1: mirror to PM so runner picks it up on next message (no restart needed)
            _sync_persona_to_pm(chat_name, account_id, reply_profile_id)
            return {
                "ok": ok,
                "chat_name": chat_name,
                "reply_profile_id": reply_profile_id,
                "account_id": account_id,
                "pm_synced": True,
            }
        except Exception as ex:
            raise HTTPException(500, f"set binding failed: {ex}")

    @app.delete("/api/messenger-rpa/chat-persona-bindings/{chat_name}")
    async def api_msgr_chat_persona_binding_unset(
        request: Request, chat_name: str,
    ):
        """运营移除某 chat 的指定，回到自动匹配。
        query: account_id（可选）"""
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        account_id = str(request.query_params.get("account_id") or "").strip()
        try:
            removed = store.remove_chat_persona_override(
                chat_name=chat_name, account_id=account_id,
            )
            # P1: also clear PM binding → runner falls back to auto-match next message
            _sync_persona_to_pm(chat_name, account_id, None)
            return {"ok": removed, "chat_name": chat_name, "account_id": account_id}
        except Exception as ex:
            raise HTTPException(500, f"unset failed: {ex}")

    @app.post("/api/messenger-rpa/chat-persona-bindings/batch")
    async def api_msgr_chat_persona_binding_batch(request: Request):
        """批量绑定。
        body: {
          "bindings": [
             {"chat_name": "...", "reply_profile_id": "...", "account_id": "..."},
             ...
          ]
        }
        或者快捷模式：
        body: {
          "account_id": "vwnj_test",
          "reply_profile_id": "warm_companion",
          "chat_names": ["A", "B", "C"]    -- 批量给这些 chat 设同一人设
        }
        """
        api_auth(request)
        store = _get_store(request)
        if store is None:
            raise HTTPException(503, "state_store 未注入")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body 必须是对象")
        bindings = body.get("bindings")
        if not bindings and body.get("chat_names"):
            # 快捷模式：所有 chat 用同一 profile_id
            shared_profile = str(body.get("reply_profile_id") or "").strip()
            shared_account = str(body.get("account_id") or "").strip()
            shared_notes = str(body.get("notes") or "")
            if not shared_profile:
                raise HTTPException(400, "reply_profile_id is required in shortcut mode")
            chat_names = body.get("chat_names") or []
            if not isinstance(chat_names, list):
                raise HTTPException(400, "chat_names 必须是数组")
            bindings = [
                {
                    "chat_name": str(n).strip(),
                    "reply_profile_id": shared_profile,
                    "account_id": shared_account,
                    "notes": shared_notes,
                }
                for n in chat_names if str(n).strip()
            ]
        if not isinstance(bindings, list):
            raise HTTPException(400, "bindings 必须是数组")
        try:
            n = store.batch_upsert_chat_persona_overrides(
                bindings, bound_by="web_admin",
            )
            # P1: mirror each binding to PM
            pm_ok = 0
            for b in bindings:
                try:
                    _sync_persona_to_pm(
                        str(b.get("chat_name") or "").strip(),
                        str(b.get("account_id") or "").strip(),
                        str(b.get("reply_profile_id") or "").strip() or None,
                    )
                    pm_ok += 1
                except Exception:
                    pass
            return {
                "ok": True,
                "applied": n,
                "total_in_request": len(bindings),
                "pm_synced": pm_ok,
            }
        except Exception as ex:
            raise HTTPException(500, f"batch upsert failed: {ex}")

    # ── P28：手动发送队列 ─────────────────────────────────────────────

    @app.post("/api/messenger-rpa/send-manual")
    async def api_msgr_send_manual(request: Request):
        """入队一条主动发送任务。

        Body JSON: {"chat_key": "...", "peer_name": "...", "text": "..."}
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "Messenger RPA 服务未启动")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
        chat_key = (body.get("chat_key") or "").strip()
        peer_name = (body.get("peer_name") or "").strip()
        text = (body.get("text") or "").strip()
        if not chat_key:
            raise HTTPException(400, "chat_key 必填")
        if not text:
            raise HTTPException(400, "text 不能为空")
        try:
            actor = request.session.get("username", "web_admin")
        except Exception:
            actor = "api"
        try:
            item_id = svc.enqueue_send(
                chat_key=chat_key, peer_name=peer_name, text=text, created_by=actor,
            )
        except Exception as e:
            raise HTTPException(500, str(e))
        if audit_store:
            audit_store.log(actor, "messenger_rpa_send_manual_enqueue",
                            f"id={item_id} chat_key={chat_key}")
        return {"ok": True, "item_id": item_id}

    @app.get("/api/messenger-rpa/send-queue")
    async def api_msgr_send_queue_list(request: Request):
        """列出发送队列。可选参数: limit（默认30）、include_done（0/1）。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "Messenger RPA 服务未启动")
        try:
            limit = int(request.query_params.get("limit", 30))
            include_done = request.query_params.get("include_done", "0") not in ("0", "false", "")
        except ValueError:
            raise HTTPException(400, "limit 必须为整数")
        items = svc.list_send_queue(limit=limit, include_done=include_done)
        return {"items": items, "count": len(items)}

    @app.get("/api/messenger-rpa/send-queue/{item_id}")
    async def api_msgr_send_queue_get(item_id: int, request: Request):
        """查询单条发送任务。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "Messenger RPA 服务未启动")
        item = svc.get_send_queue_item(item_id)
        if item is None:
            raise HTTPException(404, f"send_queue item {item_id} 不存在")
        return item

    @app.post("/api/messenger-rpa/send-queue/{item_id}/cancel")
    async def api_msgr_send_queue_cancel(item_id: int, request: Request):
        """取消一条待发任务（仅限 queued 状态）。"""
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "Messenger RPA 服务未启动")
        ok = svc.cancel_send_queue_item(item_id)
        if not ok:
            raise HTTPException(409, f"item {item_id} 不可取消（不存在或已非 queued 状态）")
        try:
            actor = request.session.get("username", "web_admin")
        except Exception:
            actor = "api"
        if audit_store:
            audit_store.log(actor, "messenger_rpa_send_queue_cancel", f"id={item_id}")
        return {"ok": True, "item_id": item_id}

    # ── P5-A: 对话语言锁定（镜像 WhatsApp 实现）────────────────────────────

    @app.post("/api/messenger-rpa/chat-lang-lock")
    async def api_msgr_chat_lang_lock(request: Request):
        """锁定或解锁 Messenger 指定对话的回复语言。

        Body: {chat_key: str, lang: str|null}
          lang = AIClient 语言代码（如 "de"/"ja"/"zh"）→ 锁定
          lang = null/""  → 解除锁定（恢复 profile/自动检测）
        """
        api_auth(request)
        svc = _get_service(request)
        if svc is None:
            raise HTTPException(503, "Messenger RPA 服务未启动")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON body")
        chat_key = str(body.get("chat_key") or "").strip()
        if not chat_key:
            raise HTTPException(400, "chat_key is required")
        lang_raw = body.get("lang")
        lang = str(lang_raw).strip().lower() if lang_raw else ""

        _VALID_LANGS = {
            "zh", "en", "de", "ja", "ko", "fr", "es", "ar", "ru",
            "hi", "it", "pt", "nl", "pl", "tr", "cs", "hu",
        }
        if lang and lang not in _VALID_LANGS:
            raise HTTPException(400, f"不支持的语言代码: {lang}。支持: {sorted(_VALID_LANGS)}")

        try:
            svc.state_store.set_forced_lang(chat_key, lang or None)
        except Exception as e:
            raise HTTPException(500, f"写入失败: {e}")

        # P7-B / P8-C: 立即同步 _context_store
        # 锁定时：写入新 lang；解除时：清空 reply_lang，下次消息循环重新检测
        try:
            _sm = getattr(getattr(svc, "_runner", None), "_sm", None)
            _cs = getattr(_sm, "_context_store", None) if _sm else None
            if _cs is not None:
                uctx = _cs.get(chat_key)
                if uctx is not None:
                    uctx["reply_lang"] = lang  # "" on unlock forces re-detection
                    _cs.mark_dirty(chat_key)
        except Exception:
            pass  # best-effort; state_store is source of truth

        # P13-E: 记录最近一次语言锁变更时间
        try:
            import time as _t
            svc._last_lang_lock_ts = _t.time()
        except Exception:
            pass
        action = f"锁定为 {lang}" if lang else "解除锁定（恢复自动检测）"
        return {"ok": True, "chat_key": chat_key, "forced_lang": lang or None, "action": action}

    logger.info("Messenger RPA routes registered (status/approvals/trigger/...")
