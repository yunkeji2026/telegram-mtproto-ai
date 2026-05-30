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
                    persona_ids=[],
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
                }
                for c in self.all_contexts()
            ],
        }


__all__ = ["TelegramAccountContext", "TelegramAccountRegistry"]
