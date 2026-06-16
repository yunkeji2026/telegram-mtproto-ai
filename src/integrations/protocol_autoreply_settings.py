"""协议自动回复全局设置（Phase 7 ①）。

让运营**不碰 YAML** 也能调自动回复的全局参数（开关 / 配额 / 熔断 / 营业时段 / 延迟）：
设置写入独立 JSON（``config/protocol_autoreply.json``），运行时与 ``config.yaml`` 的
``protocol_autoreply`` 段**深合并**（JSON 覆盖 YAML），无需改 YAML、无需重启。

设计：纯文件 + 内存缓存 + 线程锁；白名单校验（只接受已知字段，拒绝任意键）；
``effective_settings`` 给读取方一份合并好的 dict，``cfg_with_settings`` 给需要
完整 cfg 形态的调用方（如 run_autoreply）。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: Optional[Dict[str, Any]] = None
_path: Path = Path("config/protocol_autoreply.json")


def _store_path() -> Path:
    return _path


def set_store_path(p: Path) -> None:
    """测试辅助：切换存储路径并清缓存。"""
    global _path, _cache
    with _lock:
        _path = Path(p)
        _cache = None


def _coerce_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(v, (int, float)):
        return bool(v)
    return default


def _coerce_int(v: Any, default: int, lo: int = 0, hi: int = 10 ** 7) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _coerce_hhmm(v: Any, default: str) -> str:
    s = str(v or "").strip()
    parts = s.split(":")
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        hh = max(0, min(23, int(parts[0])))
        mm = max(0, min(59, int(parts[1])))
        return f"{hh:02d}:{mm:02d}"
    return default


def sanitize(patch: Dict[str, Any]) -> Dict[str, Any]:
    """白名单校验：只保留 patch 中出现的已知字段，并归一类型。"""
    out: Dict[str, Any] = {}
    p = patch or {}
    if "enabled" in p:
        out["enabled"] = _coerce_bool(p.get("enabled"), False)
    if isinstance(p.get("rate"), dict):
        r = p["rate"]
        rate: Dict[str, Any] = {}
        if "hourly" in r:
            rate["hourly"] = _coerce_int(r.get("hourly"), 30)
        if "daily" in r:
            rate["daily"] = _coerce_int(r.get("daily"), 200)
        if rate:
            out["rate"] = rate
    if isinstance(p.get("breaker"), dict):
        b = p["breaker"]
        brk: Dict[str, Any] = {}
        if "threshold" in b:
            brk["threshold"] = _coerce_int(b.get("threshold"), 5)
        if "cooldown_sec" in b:
            brk["cooldown_sec"] = _coerce_int(b.get("cooldown_sec"), 300)
        if brk:
            out["breaker"] = brk
    if isinstance(p.get("hours"), dict):
        h = p["hours"]
        hours: Dict[str, Any] = {}
        if "enabled" in h:
            hours["enabled"] = _coerce_bool(h.get("enabled"), False)
        if "start" in h:
            hours["start"] = _coerce_hhmm(h.get("start"), "09:00")
        if "end" in h:
            hours["end"] = _coerce_hhmm(h.get("end"), "23:00")
        if "tz_offset" in h:
            hours["tz_offset"] = _coerce_int(h.get("tz_offset"), 8, lo=-12, hi=14)
        if hours:
            out["hours"] = hours
    if isinstance(p.get("delay"), dict):
        d = p["delay"]
        delay: Dict[str, Any] = {}
        if "min_sec" in d:
            delay["min_sec"] = _coerce_int(d.get("min_sec"), 0, hi=600)
        if "max_sec" in d:
            delay["max_sec"] = _coerce_int(d.get("max_sec"), 0, hi=600)
        if delay:
            out["delay"] = delay
    return out


def load() -> Dict[str, Any]:
    """读 JSON 覆盖设置（带缓存）。文件不存在 / 损坏 → {}。"""
    global _cache
    with _lock:
        if _cache is not None:
            return dict(_cache)
        try:
            raw = _store_path().read_text(encoding="utf-8")
            data = json.loads(raw)
            _cache = data if isinstance(data, dict) else {}
        except FileNotFoundError:
            _cache = {}
        except Exception:
            logger.debug("[autoreply-settings] 读取失败，回落空", exc_info=True)
            _cache = {}
        return dict(_cache)


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base or {})
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def save(patch: Dict[str, Any]) -> Dict[str, Any]:
    """深合并 patch 到现有设置并落盘。返回合并后的完整设置。"""
    global _cache
    clean = sanitize(patch)
    with _lock:
        current = dict(_cache) if _cache is not None else _load_unlocked()
        merged = _deep_merge(current, clean)
        p = _store_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        _cache = merged
        return dict(merged)


def _load_unlocked() -> Dict[str, Any]:
    try:
        raw = _store_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    """公开的深合并（over 覆盖 base）。"""
    return _deep_merge(base, over)


def sanitize_override(patch: Dict[str, Any]) -> Dict[str, Any]:
    """账号级覆盖白名单：复用 sanitize，但移除 ``enabled``
    （账号开关用 registry meta.auto_reply，不在覆盖内）。"""
    out = sanitize(patch)
    out.pop("enabled", None)
    return out


def merge_account_override(
    global_pa: Optional[Dict[str, Any]], override: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """全局有效 protocol_autoreply + 账号覆盖（覆盖优先）。"""
    return _deep_merge(dict(global_pa or {}), dict(override or {}))


def effective_settings(base_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """config.yaml 的 protocol_autoreply 基底 + JSON 覆盖（覆盖优先）。"""
    base = dict((base_cfg or {}).get("protocol_autoreply") or {})
    return _deep_merge(base, load())


def _flatten(d: Optional[Dict[str, Any]], prefix: str = "") -> Dict[str, Any]:
    """嵌套 dict → 点号扁平键（用于配置变更 diff）。"""
    out: Dict[str, Any] = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def diff_settings(
    before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]],
) -> list:
    """返回 [{key, old, new}]（仅列出有变化的扁平键，含新增/删除）。"""
    fb = _flatten(before)
    fa = _flatten(after)
    changes = []
    for key in sorted(set(fb) | set(fa)):
        ov = fb.get(key)
        nv = fa.get(key)
        if ov != nv:
            changes.append({"key": key, "old": ov, "new": nv})
    return changes


def cfg_with_settings(base_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """返回浅拷贝 cfg，其 protocol_autoreply 替换为合并后的有效设置。"""
    out = dict(base_cfg or {})
    out["protocol_autoreply"] = effective_settings(base_cfg)
    return out
