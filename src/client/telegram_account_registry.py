"""Telegram multi-account registry.

Mirrors the Messenger RPA ``AccountRegistry`` pattern — reads
``telegram.accounts`` from config and produces ``TelegramAccountContext``
objects, one per enabled account.

Backward-compatibility guarantee
---------------------------------
When ``telegram.accounts`` is absent or empty the registry falls back to a
single "default" context built from the flat ``telegram.*`` fields.  Callers
that only reference ``self.telegram_client`` in ``main.py`` see zero change.

Typical config layout (multi-account)::

    telegram:
      accounts:
        - id: acc_a
          label: "号 A"
          api_id: 12345
          api_hash: abc
          phone_number: "+8613800000000"
          session_name: camille_a
          persona_ids: [warm_companion]
          enabled: true
        - id: acc_b
          label: "号 B"
          api_id: 67890
          api_hash: def
          phone_number: "+8613811111111"
          session_name: camille_b
          persona_ids: [professional_support]
          enabled: true
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TelegramAccountContext:
    """Static config for one Telegram account.

    ``account_cfg`` is the overlay dict passed to ``TelegramClient`` so it can
    override the credentials read from the global ``telegram.*`` config block.
    """
    account_id: str
    label: str = ""
    api_id: Optional[int] = None
    api_hash: str = ""
    phone_number: str = ""
    session_name: str = "camille_bot"
    persona_ids: List[str] = field(default_factory=list)
    status: str = "active"
    # N 线 核心2：绑定 proxy_pool 条目 → 每号独立出口 IP（反封号）
    proxy_id: str = ""

    def account_cfg(self) -> Dict[str, Any]:
        """Return the overlay dict to pass to ``TelegramClient(account_cfg=...)``."""
        cfg: Dict[str, Any] = {
            "account_id": self.account_id,
            "account_label": self.label,
        }
        if self.api_id is not None:
            cfg["api_id"] = self.api_id
        if self.api_hash:
            cfg["api_hash"] = self.api_hash
        if self.phone_number:
            cfg["phone_number"] = self.phone_number
        if self.session_name:
            cfg["session_name"] = self.session_name
        if self.persona_ids:
            cfg["persona_ids"] = list(self.persona_ids)
        if self.proxy_id:
            cfg["proxy_id"] = self.proxy_id
        return cfg

    @property
    def is_default(self) -> bool:
        return self.account_id == "default"


class TelegramAccountRegistry:
    """Read-only registry: config → list of TelegramAccountContext.

    Does not manage TelegramClient lifetimes; that is ``main.py``'s job.
    """

    def __init__(self, contexts: List[TelegramAccountContext]) -> None:
        self._ctx: Dict[str, TelegramAccountContext] = {
            c.account_id: c for c in contexts
        }

    # ── Factory ──────────────────────────────────────────────────────────────

    @classmethod
    def from_config(
        cls,
        telegram_cfg: Dict[str, Any],
    ) -> "TelegramAccountRegistry":
        """Build a registry from the ``telegram`` config section.

        Args:
            telegram_cfg: value of ``config["telegram"]``
        """
        raw_accounts = telegram_cfg.get("accounts")
        contexts: List[TelegramAccountContext] = []

        if isinstance(raw_accounts, list) and raw_accounts:
            for entry in raw_accounts:
                if not isinstance(entry, dict):
                    continue
                if entry.get("enabled") is False:
                    logger.info(
                        "[tg_registry] 跳过 disabled account: %s",
                        entry.get("id") or entry.get("account_id"),
                    )
                    continue
                aid = str(
                    entry.get("id") or entry.get("account_id") or ""
                ).strip()
                if not aid:
                    logger.warning("[tg_registry] 跳过无 id 的 account 条目: %s", entry)
                    continue
                api_id_raw = entry.get("api_id")
                api_id = int(api_id_raw) if api_id_raw else None
                persona_ids_raw = entry.get("persona_ids") or []
                if isinstance(persona_ids_raw, str):
                    persona_ids_raw = [
                        x.strip()
                        for x in persona_ids_raw.split(",")
                        if x.strip()
                    ]
                contexts.append(
                    TelegramAccountContext(
                        account_id=aid,
                        label=str(entry.get("label") or aid),
                        api_id=api_id,
                        api_hash=str(entry.get("api_hash") or "").strip(),
                        phone_number=str(
                            entry.get("phone_number")
                            or entry.get("phone")
                            or ""
                        ).strip(),
                        session_name=str(
                            entry.get("session_name")
                            or entry.get("session")
                            or f"camille_{aid}"
                        ).strip(),
                        persona_ids=[str(p) for p in persona_ids_raw if p],
                        status=str(entry.get("status") or "active"),
                        proxy_id=str(entry.get("proxy_id") or "").strip(),
                    )
                )
            if contexts:
                logger.info(
                    "[tg_registry] 多账号模式：%d 个账号 (%s)",
                    len(contexts),
                    ", ".join(c.account_id for c in contexts),
                )
            else:
                logger.warning(
                    "[tg_registry] accounts 列表解析后为空，回退单账号"
                )

        if not contexts:
            # Single-account fallback — use flat telegram.* fields
            api_id_raw = telegram_cfg.get("api_id")
            contexts.append(
                TelegramAccountContext(
                    account_id="default",
                    label="default",
                    api_id=int(api_id_raw) if api_id_raw else None,
                    api_hash=str(telegram_cfg.get("api_hash") or "").strip(),
                    phone_number=str(
                        telegram_cfg.get("phone_number") or ""
                    ).strip(),
                    session_name=str(
                        telegram_cfg.get("session_name") or "camille_bot"
                    ).strip(),
                    # 单账号也支持扁平 telegram.persona_ids（Web 人设工作室指定人设后写入）
                    persona_ids=[
                        str(p)
                        for p in (telegram_cfg.get("persona_ids") or [])
                        if p
                    ],
                    proxy_id=str(telegram_cfg.get("proxy_id") or "").strip(),
                )
            )
            logger.info("[tg_registry] 单账号模式（default）")

        return cls(contexts)

    # ── Query ─────────────────────────────────────────────────────────────────

    def all_contexts(self) -> List[TelegramAccountContext]:
        return list(self._ctx.values())

    def get(self, account_id: str) -> Optional[TelegramAccountContext]:
        return self._ctx.get(account_id)

    def primary(self) -> TelegramAccountContext:
        """Return the first context (used as the legacy ``telegram_client``)."""
        return next(iter(self._ctx.values()))

    def size(self) -> int:
        return len(self._ctx)

    def is_multi_account(self) -> bool:
        return self.size() > 1 or not self.primary().is_default

    # ── N5: 登录注册统一 ───────────────────────────────────────────────────────

    def sync_to_account_registry(
        self,
        registry: Any,
        *,
        platform: str = "telegram",
        include_default: bool = True,
    ) -> List[str]:
        """N5：把 A 线 config 账号（phone+code）并入 B 线持久注册表（QR 共用）。

        让"配置定义"与"扫码登录"两类账号汇入**同一张 platform_accounts 表**，
        编排器/舰队健康视图无需区分来源即可看全。幂等且**不破坏既有登录态**：

        - **不覆盖会话凭据**：合并而非替换 meta——读出既有 meta，仅叠加 config 派生的
          静态身份字段（session_name/phone/persona_ids），绝不丢 QR 登录写入的
          ``session_string``。
        - **不打翻在线态/登录模式**：已存在的账号保留其 ``status`` 与 ``mode``（如某号
          已 QR online，本同步不会把它打回 pending）；仅新账号写 mode=protocol/status=pending。
        - **config 为静态属性源**：``label``/``proxy_id`` 以 config 为准刷新（这两项本就
          由配置管理），其余运行态字段不动。

        Args:
            registry: B 线 ``AccountRegistry`` 实例（duck-typed：需 get/upsert）。
            platform: 注册表平台键，默认 ``telegram``。
            include_default: 是否纳入单账号回退的 ``default`` 上下文。

        Returns:
            已同步的 account_id 列表。
        """
        synced: List[str] = []
        if registry is None:
            return synced
        for ctx in self.all_contexts():
            if ctx.account_id == "default" and not include_default:
                continue
            try:
                existing = registry.get(platform, ctx.account_id)
            except Exception:
                existing = None
            # 合并 meta：保留既有（含 QR 的 session_string），叠加 config 静态身份
            meta: Dict[str, Any] = dict((existing or {}).get("meta") or {})
            if ctx.session_name:
                meta["session_name"] = ctx.session_name
            if ctx.phone_number:
                meta["phone_number"] = ctx.phone_number
            if ctx.persona_ids:
                meta["persona_ids"] = list(ctx.persona_ids)
                # 数据侧自愈：同写单数 meta.persona_id（=首个），让直接读 persona_id
                # 的消费方（protocol_autoreply 生成 / voice 灰度解析等）无需各自懂
                # 复数→单数回退。用 persona_id_auto 标记区分「本同步自动补的」与
                # 「人工/QR 显式绑定的」：前者随 config 首个刷新，后者绝不覆盖——
                # 否则陈旧单数会压过刷新后的复数（resolver 单数优先）造成回退。
                _first_pid = str(ctx.persona_ids[0] or "").strip()
                _cur_pid = str(meta.get("persona_id") or "").strip()
                if _first_pid and (not _cur_pid or meta.get("persona_id_auto")):
                    meta["persona_id"] = _first_pid
                    meta["persona_id_auto"] = True
            meta["config_synced"] = True  # 标记来源含 config 同步
            if existing:
                # 既有账号：保留 mode/status/会话凭据，仅刷新 config 拥有的静态属性
                kwargs: Dict[str, Any] = {"meta": meta}
                if ctx.label:
                    kwargs["label"] = ctx.label
                # proxy_id 以 config 为准（含清空：config 删了代理则同步清空）
                kwargs["proxy_id"] = ctx.proxy_id or ""
                try:
                    registry.upsert(platform, ctx.account_id, **kwargs)
                except Exception:
                    continue
            else:
                # 新账号：phone+code = protocol 模式，待编排器拉起（pending）
                try:
                    registry.upsert(
                        platform, ctx.account_id,
                        mode="protocol",
                        label=ctx.label or "",
                        proxy_id=ctx.proxy_id or "",
                        status="pending",
                        meta=meta,
                    )
                except Exception:
                    continue
            synced.append(ctx.account_id)
        return synced

    def stats(self) -> Dict[str, Any]:
        return {
            "total": self.size(),
            "multi_account": self.is_multi_account(),
            "accounts": [
                {
                    "account_id": c.account_id,
                    "label": c.label,
                    "session_name": c.session_name,
                    "persona_ids": c.persona_ids,
                    "status": c.status,
                    "proxy_id": c.proxy_id,
                }
                for c in self.all_contexts()
            ],
        }


__all__ = ["TelegramAccountContext", "TelegramAccountRegistry"]
