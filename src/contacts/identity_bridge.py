"""Phase Q：跨域身份桥（contacts ↔ inbox/care 的只读 join 助手）。

把一个联系人（contact）的全部 `channel_identities` 反推成 inbox/care 域的
`conversation_id` 列表，用于让「关系健康卡」自动聚合 care 域的 `pending_care` 信号
（无需调用方手传 contact_key）。

设计纪律：
- **Q1 纯函数桥**：入参是已加载的 channel_identity 列表，不触 DB/网络、可单测。
- **Q 延伸写路径（可选，config 关）**：ingest 热路径回写 contact_id；默认仍走只读 join。
- `conversation_id` 格式 `{platform}:{account_id}:{chat_key}` 是 `src/inbox/normalizer.py::conv_id`
  的权威产物；这里**镜像**该格式（单行 f-string，由测试钉死防漂移），避免 contacts→inbox 反向依赖。

**已知局限（诚实标注）**：仅当 contacts 的 `external_id` 与 inbox 的 `chat_key` 同源时命中
（LINE / Telegram / web 高概率）；Messenger/WhatsApp 现状 `external_id`=裸 peer 名、
inbox `chat_key` 带前缀，会漏匹配 → 该类暂得 `pending_care=0`（不报错、不误算）。
彻底打通需 external_id 规范化层（中长期，单独立项）。

Q 延伸（ingest 回写）：``resolve_contact_id`` 在 ingest 热路径把已解析的 contact 写入
``conversations.contact_id`` + ``conversation_meta.contact_id``；读侧可用
``list_conversation_ids_for_contact`` 反查，补 CI 桥漏匹配场景。
"""
from __future__ import annotations

from typing import Any, Iterable, List


def _attr(ci: Any, key: str) -> str:
    """从 ChannelIdentity 对象或 dict 取字段，缺失 → 空串。"""
    if isinstance(ci, dict):
        return str(ci.get(key) or "")
    return str(getattr(ci, key, "") or "")


def conversation_ids_for_identities(channel_identities: Iterable[Any]) -> List[str]:
    """把 channel_identities 反推成候选 conversation_id 列表（去重、保序）。

    镜像 inbox.normalizer.conv_id：``{channel}:{account_id}:{external_id}``
    （account_id 缺省回落 'default'，与 inbox conversations 默认一致）。
    无 channel 或 external_id 的项跳过。
    """
    seen = set()
    out: List[str] = []
    for ci in channel_identities or []:
        ch = _attr(ci, "channel")
        ext = _attr(ci, "external_id")
        if not ch or not ext:
            continue
        acc = _attr(ci, "account_id") or "default"
        cid = f"{ch}:{acc}:{ext}"
        if cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def external_id_lookup_candidates(
    platform: str, account_id: str, chat_key: str,
) -> List[str]:
    """从 inbox chat_key 生成 contacts CI 查表候选 external_id（去重保序）。

    通用规则：原样 chat_key + ``:`` 后缀（``messenger_rpa:Bob`` → ``Bob``）。
    不动 RPA 写入，只在读侧扩命中。
    """
    ck = str(chat_key or "").strip()
    if not ck:
        return []
    seen: set = set()
    out: List[str] = []

    def _add(x: str) -> None:
        s = str(x or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    _add(ck)
    if ":" in ck:
        _add(ck.split(":", 1)[-1])
    return out


def resolve_contact_id(
    contacts_store: Any,
    *,
    platform: str,
    account_id: str,
    chat_key: str,
) -> str:
    """Q 延伸：从 inbox 会话键反查 contacts ``contact_id``（只读，best-effort）。

    对齐 ``unified_inbox_context._lookup_contacts_enrichment``：
    1) ``get_ci_by_external(channel, account_id, candidate)``
    2) 回落 ``channel + external_id``（忽略 account_id）
    """
    if contacts_store is None or not platform or not chat_key:
        return ""
    plat = str(platform).strip()
    acc = str(account_id or "default").strip() or "default"
    candidates = external_id_lookup_candidates(plat, acc, chat_key)
    for ext in candidates:
        try:
            ci = contacts_store.get_ci_by_external(plat, acc, ext)
            if ci is not None and getattr(ci, "contact_id", ""):
                return str(ci.contact_id)
        except Exception:
            continue
    for ext in candidates:
        try:
            with contacts_store._lock:  # noqa: SLF001
                row = contacts_store._conn.execute(  # noqa: SLF001
                    "SELECT contact_id FROM channel_identities "
                    "WHERE channel=? AND external_id=? "
                    "ORDER BY linked_at ASC LIMIT 1",
                    (plat, ext),
                ).fetchone()
            if row and row[0]:
                return str(row[0])
        except Exception:
            continue
    return ""


__all__ = [
    "conversation_ids_for_identities",
    "external_id_lookup_candidates",
    "resolve_contact_id",
]
