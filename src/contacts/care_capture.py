"""Phase O4：主动关怀入站捕获接线（gated）。

把 O1 抽取 + O2 入库接到统一收件箱既有的入站新消息回调 `register_new_inbound_cb`
（参数 `cb(conv_dict, text)`）。**默认关**：仅 `companion.proactive_care.enabled`
且 `capture` 为真时才捕获。绝不抛（best-effort，异常不影响 ingest）。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)


def _care_cfg(config_manager: Any) -> Dict[str, Any]:
    try:
        full = getattr(config_manager, "config", None) or {}
        return dict((full.get("companion") or {}).get("proactive_care") or {})
    except Exception:
        return {}


def make_care_inbound_cb(store: Any, config_manager: Any) -> Callable[[Dict[str, Any], str], None]:
    """构造 `cb(conv_dict, text)`：gated 捕获入站约定入 care_schedule。

    `conv_dict` 形如 ingest 提供的 {conversation_id, platform, account_id, chat_key, display_name}。
    contact_key 用稳定的 conversation_id。
    """
    def _cb(conv_dict: Dict[str, Any], text: str) -> None:
        try:
            cfg = _care_cfg(config_manager)
            if not cfg.get("enabled", False) or not cfg.get("capture", True):
                return
            t = (text or "").strip()
            if not t:
                return
            contact_key = str(conv_dict.get("conversation_id") or "")
            if not contact_key:
                return
            ids = store.add_from_text(
                t,
                contact_key=contact_key,
                platform=str(conv_dict.get("platform") or ""),
                account_id=str(conv_dict.get("account_id") or "default"),
                chat_key=str(conv_dict.get("chat_key") or ""),
                min_confidence=float(cfg.get("min_confidence", 0.6)),
                dedup_window_days=float(cfg.get("dedup_window_days", 3)),
            )
            if ids:
                logger.info("[care] 捕获 %d 条关怀约定 contact=%s", len(ids), contact_key)
        except Exception:
            logger.debug("[care] 入站捕获失败（忽略）", exc_info=True)

    return _cb


__all__ = ["make_care_inbound_cb"]
