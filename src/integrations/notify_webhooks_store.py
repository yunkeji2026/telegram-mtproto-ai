"""告警 Webhook 列表存储（Phase 11）。

让运营**不碰 YAML** 也能增删自动回复告警渠道（Telegram / WhatsApp / Messenger /
通用 JSON）：列表写入独立 JSON（``config/notify_webhooks.json``），运行时覆盖
``config.yaml::notify.webhooks``（覆盖层存在则整段取代），并热更
``app.state.webhook_notifier``，无需改 YAML、无需重启。

设计：纯文件 + 内存缓存 + 线程锁；白名单校验（只接受已知字段、合法 format/events）。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: Optional[List[Dict[str, Any]]] = None
_path: Path = Path("config/notify_webhooks.json")

_ALLOWED_FORMATS = {
    "telegram", "whatsapp", "messenger", "json",
    "dingtalk", "feishu", "wecom",
}


def _store_path() -> Path:
    return _path


def set_store_path(p: Path) -> None:
    """测试辅助：切换存储路径并清缓存。"""
    global _path, _cache
    with _lock:
        _path = Path(p)
        _cache = None


def _known_event_aliases() -> set:
    try:
        from src.inbox.webhook_notifier import _EVENT_ALIASES
        return set(_EVENT_ALIASES.keys())
    except Exception:
        return {"all"}


def sanitize_webhook(item: Dict[str, Any]) -> Dict[str, Any]:
    """单条 webhook 白名单校验 + 类型规整。非法 format → json；非法事件别名剔除。"""
    if not isinstance(item, dict):
        return {}
    fmt = str(item.get("format") or "json").strip().lower()
    if fmt not in _ALLOWED_FORMATS:
        fmt = "json"
    aliases = _known_event_aliases()
    events = [str(e).strip() for e in (item.get("events") or []) if str(e).strip()]
    events = [e for e in events if e in aliases] or ["autoreply_alert"]
    out: Dict[str, Any] = {
        "name": str(item.get("name") or "webhook").strip()[:64],
        "format": fmt,
        "url": str(item.get("url") or "").strip()[:1000],
        "token": str(item.get("token") or "").strip()[:2000],
        "target": str(item.get("target") or item.get("chat_id") or "").strip()[:128],
        "secret": str(item.get("secret") or "").strip()[:512],
        "events": events,
        "enabled": item.get("enabled", True) is not False,
    }
    return out


def sanitize_list(items: Any) -> List[Dict[str, Any]]:
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for it in items[:50]:  # 上限保护
        clean = sanitize_webhook(it)
        if clean:
            out.append(clean)
    return out


def load() -> Optional[List[Dict[str, Any]]]:
    """读覆盖层；文件不存在 → None（表示"未覆盖"，沿用 config.yaml）。"""
    global _cache
    with _lock:
        if _cache is not None:
            return list(_cache)
        p = _store_path()
        if not p.exists():
            return None
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            _cache = sanitize_list(raw if isinstance(raw, list)
                                   else raw.get("webhooks"))
            return list(_cache)
        except Exception:
            logger.warning("notify_webhooks.json 解析失败，忽略覆盖", exc_info=True)
            return None


def save_list(items: Any) -> List[Dict[str, Any]]:
    """整段覆盖写盘（运营面板的权威来源）。返回规整后的列表。"""
    global _cache
    clean = sanitize_list(items)
    with _lock:
        p = _store_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(clean, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        _cache = clean
    return list(clean)


def effective_webhooks(base_cfg: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """有效 webhook 列表：覆盖层存在则用覆盖层，否则用 config.yaml::notify.webhooks。"""
    ov = load()
    if ov is not None:
        return ov
    base = ((base_cfg or {}).get("notify") or {}).get("webhooks") or []
    return list(base)


def mask(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给前端展示用：脱敏 token/secret，只暴露是否已设置。"""
    out: List[Dict[str, Any]] = []
    for w in items:
        m = dict(w)
        for k in ("token", "secret"):
            v = str(m.get(k) or "")
            m[k] = (v[:3] + "***") if v else ""
            m[f"{k}_set"] = bool(v)
        out.append(m)
    return out
