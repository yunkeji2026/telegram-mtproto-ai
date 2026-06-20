"""Phase Q 延伸·存量回填：给历史 inbox 会话补 contact_id。

ingest 回写（§28）只对**新进消息**生效；存量会话仍 contact_id=''。本模块离线扫描
未归档会话，用 ``resolve_contact_id`` 反查并写回（支持 dry_run 预演）。

设计纪律：
- **best-effort、可中断**：单会话解析失败不影响其余；返回结构化统计供日志/审计。
- **dry_run**：只解析不写库，用于上线前评估命中率。
- **不触 RPA / 不改 schema**：复用 §28 已建的 resolver + store 写回方法。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# resolver 签名：(platform, account_id, chat_key) -> contact_id str
ContactResolver = Callable[[str, str, str], str]


@dataclass
class BackfillResult:
    scanned: int = 0
    resolved: int = 0
    written: int = 0
    dry_run: bool = False
    samples: List[Dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "scanned": self.scanned,
            "resolved": self.resolved,
            "written": self.written,
            "dry_run": self.dry_run,
            "hit_rate": round(self.resolved / self.scanned, 3) if self.scanned else 0.0,
            "samples": self.samples,
        }


def backfill_contact_ids(
    inbox_store: Any,
    resolver: Optional[ContactResolver],
    *,
    limit: int = 200,
    platform: str = "",
    dry_run: bool = False,
    max_samples: int = 5,
) -> BackfillResult:
    """扫描缺 contact_id 的会话，反查并（非 dry_run 时）写回。

    返回 ``BackfillResult``（scanned/resolved/written/hit_rate/samples）。
    """
    res = BackfillResult(dry_run=bool(dry_run))
    if inbox_store is None or resolver is None:
        return res
    try:
        rows = inbox_store.list_conversations_missing_contact_id(
            limit=limit, platform=platform)
    except Exception:
        logger.debug("backfill: list missing failed", exc_info=True)
        return res
    for row in rows or []:
        res.scanned += 1
        conv_id = str(row.get("conversation_id") or "")
        plat = str(row.get("platform") or "")
        acc = str(row.get("account_id") or "default")
        chat_key = str(row.get("chat_key") or "")
        if not conv_id or not plat or not chat_key:
            continue
        try:
            contact_id = str(resolver(plat, acc, chat_key) or "").strip()
        except Exception:
            logger.debug("backfill: resolve failed conv=%s", conv_id, exc_info=True)
            continue
        if not contact_id:
            continue
        res.resolved += 1
        if len(res.samples) < max_samples:
            res.samples.append({
                "conversation_id": conv_id, "contact_id": contact_id})
        if dry_run:
            continue
        try:
            if inbox_store.set_conversation_contact_id(conv_id, contact_id):
                res.written += 1
        except Exception:
            logger.debug("backfill: write failed conv=%s", conv_id, exc_info=True)
    return res


__all__ = ["BackfillResult", "backfill_contact_ids", "ContactResolver"]
