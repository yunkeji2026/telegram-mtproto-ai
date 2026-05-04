"""P5-1：多账号隔离与并发控制。

设计原则（深度思考版）：
1. **向后兼容优先**：当 config.accounts 为空/未配置 → 回到单账号 "default"
   路径，零迁移；cfg.adb_serial 自动映射为 default 账号的 serial。
2. **状态隔离**：每个 account_id 持有独立的 MessengerRpaStateStore（独立
   SQLite 文件）；chat_key 通过 prefix 隔离避免跨账号串扰。
3. **并发保护**：
   - **账号内串行**：同一 account 在同一时刻只能有一个 runner 在跑
     `adb shell input` 类操作（防 IME 输入错位）→ per-account asyncio.Lock
   - **账号间并发**：由外部 Semaphore(max_parallel) 控制；默认 2
4. **无状态注册表**：AccountRegistry 只负责"读 config + 构造 context"；
   真正的 Runner/Service 实例由上层持有，不在这里管理生命周期。

用法：

    from src.integrations.messenger_rpa.account_pool import AccountRegistry

    reg = AccountRegistry.from_config(cfg, config_path)
    for ctx in reg.all_contexts():
        store = ctx.state_store()          # lazy init
        async with reg.pool.acquire(ctx.account_id):
            # 这里可以安全跑 adb shell input / ime set / input text 等
            ...
"""
from __future__ import annotations

import asyncio
import copy
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.integrations.messenger_rpa.state_store import (
    MessengerRpaStateStore,
    default_state_db_path,
)

logger = logging.getLogger(__name__)


@dataclass
class AccountContext:
    """某个 account 的全部静态配置 + 懒加载状态。

    - ``account_id``：字符串 id（用于日志、chat_key 前缀、state db 文件名）
    - ``adb_serial``：该 account 绑定的设备 serial（IP:port 或 USB serial）
    - ``chat_key_prefix``：插到 chat_key 前的命名空间前缀（默认 "acc_{id}"）
    - ``config_overlay``：对全局 config 的 overlay（例如 specific reply_mode）
    - ``state_db_path``：绝对路径；None 时走 default_state_db_path(...)
    """
    account_id: str
    adb_serial: str = ""
    chat_key_prefix: str = ""
    config_overlay: Dict[str, Any] = field(default_factory=dict)
    state_db_path: Optional[Path] = None
    label: str = ""  # 人读标签，运维展示用
    reply_profile_id: str = ""
    mobile_device_id: str = ""
    device_number: str = ""
    device_alias: str = ""
    login_account: str = ""
    line_id: str = ""
    supported_languages: List[str] = field(default_factory=list)
    supported_customer_types: List[str] = field(default_factory=list)
    persona_ids: List[str] = field(default_factory=list)
    status: str = "active"
    health_score: float = 100.0
    current_load: int = 0
    max_daily_send: int = 200

    _state_store: Optional[MessengerRpaStateStore] = None
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def state_store(self) -> MessengerRpaStateStore:
        """懒加载 StateStore；多线程安全。"""
        with self._lock:
            if self._state_store is None:
                db_path = self.state_db_path
                if db_path is None:
                    raise RuntimeError(
                        f"AccountContext({self.account_id}) state_db_path 未设置"
                    )
                self._state_store = MessengerRpaStateStore(
                    db_path, account_id=self.account_id,
                )
            return self._state_store

    def prefix_chat_key(self, raw_chat_key: str) -> str:
        """给 chat_key 打上 account 命名空间。

        - default 账号 + 未配置 prefix → 不变（旧行为）
        - 非 default 或配置了 prefix → `{prefix}:{raw}`
        """
        prefix = (self.chat_key_prefix or "").strip()
        if not prefix:
            if self.account_id == "default":
                return raw_chat_key
            prefix = f"acc_{self.account_id}"
        if raw_chat_key.startswith(prefix + ":"):
            return raw_chat_key
        return f"{prefix}:{raw_chat_key}"

    def merged_config(self, base_cfg: Dict[str, Any]) -> Dict[str, Any]:
        """把 config_overlay 合并到 base_cfg，返回新字典（不改原对象）。

        overlay 优先；浅合并（不递归）。adb_serial 字段**强制**被 overlay 指定值
        覆盖，即便 base 已配置。

        **P6-1 关键强化**：非 default 账号自动注入 ``chat_key_prefix`` —— 即便
        用户忘了在 overlay 里配，也不会让两个账号的 chat_key 在 ContextStore
        和 state DB 层互相污染。
        """
        merged = copy.deepcopy(base_cfg or {})
        merged.update(self.config_overlay or {})
        if self.adb_serial:
            merged["adb_serial"] = self.adb_serial
        merged["account_id"] = self.account_id
        merged["account_label"] = self.label
        if self.reply_profile_id:
            merged["account_reply_profile_id"] = self.reply_profile_id
        if self.mobile_device_id:
            merged["mobile_device_id"] = self.mobile_device_id
        if self.device_number:
            merged["device_number"] = self.device_number
        if self.device_alias:
            merged["device_alias"] = self.device_alias
        if self.login_account:
            merged["login_account"] = self.login_account
        if self.line_id:
            lq = merged.get("lead_qualification")
            if not isinstance(lq, dict):
                lq = {}
            else:
                lq = copy.deepcopy(lq)
            handoff = lq.get("handoff")
            if not isinstance(handoff, dict):
                handoff = {}
            else:
                handoff = copy.deepcopy(handoff)
            handoff["line_id"] = self.line_id
            lq["handoff"] = handoff
            merged["lead_qualification"] = lq
        # P6-1：非 default 账号默认启用命名空间前缀
        has_overlay_prefix = (
            "chat_key_prefix" in (self.config_overlay or {})
        )
        if not has_overlay_prefix and self.account_id != "default":
            effective = (self.chat_key_prefix or "").strip() or (
                f"acc_{self.account_id}"
            )
            merged["chat_key_prefix"] = effective
        return merged


class AccountPool:
    """进程内账号并发控制。

    - per-account `asyncio.Lock`：同 account 串行执行 adb 敏感操作
    - 全局 `asyncio.Semaphore(max_parallel)`：控制同时"活跃"账号上限
    """

    def __init__(self, max_parallel: int = 2) -> None:
        self._max_parallel = max(1, int(max_parallel or 2))
        self._locks: Dict[str, asyncio.Lock] = {}
        self._sem: Optional[asyncio.Semaphore] = None
        self._create_lock = threading.Lock()

    def _ensure_semaphore(self) -> asyncio.Semaphore:
        """延迟创建 semaphore，必须在事件循环内调用。"""
        if self._sem is not None:
            try:
                running = asyncio.get_running_loop()
                bound = getattr(self._sem, "_loop", None)
                if bound is not None and bound is not running:
                    self._sem = None  # event loop 不匹配，重建
            except RuntimeError:
                pass
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_parallel)
        return self._sem

    def _lock_for(self, account_id: str) -> asyncio.Lock:
        with self._create_lock:
            lk = self._locks.get(account_id)
            if lk is not None:
                # Python 3.10+ 锁绑定 event loop；若 loop 不匹配则重建
                try:
                    running = asyncio.get_running_loop()
                    bound = getattr(lk, "_loop", None)
                    if bound is not None and bound is not running:
                        lk = None  # 强制重建
                except RuntimeError:
                    pass  # 不在 async 上下文——不重建
            if lk is None:
                lk = asyncio.Lock()
                self._locks[account_id] = lk
            return lk

    class _AcquireCtx:
        """异步上下文：先抢全局并发额，再抢账号内锁。顺序不能颠倒（否则
        某个 account 持有自己 lock 且全局 sem 满 → 跨 account 死锁）。"""

        def __init__(self, pool: "AccountPool", account_id: str,
                     timeout: Optional[float]) -> None:
            self._pool = pool
            self._aid = account_id
            self._timeout = timeout
            self._sem_acquired = False
            self._lock: Optional[asyncio.Lock] = None

        async def __aenter__(self) -> "AccountPool._AcquireCtx":
            sem = self._pool._ensure_semaphore()
            if self._timeout is not None:
                await asyncio.wait_for(sem.acquire(), timeout=self._timeout)
            else:
                await sem.acquire()
            self._sem_acquired = True
            self._lock = self._pool._lock_for(self._aid)
            try:
                if self._timeout is not None:
                    await asyncio.wait_for(
                        self._lock.acquire(), timeout=self._timeout,
                    )
                else:
                    await self._lock.acquire()
            except Exception:
                # lock 拿不到 → 释放 sem，避免泄漏
                if self._sem_acquired:
                    sem.release()
                    self._sem_acquired = False
                raise
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            if self._lock is not None and self._lock.locked():
                try:
                    self._lock.release()
                except RuntimeError:
                    pass
            if self._sem_acquired and self._pool._sem is not None:
                self._pool._sem.release()
                self._sem_acquired = False

    def acquire(
        self, account_id: str, *, timeout: Optional[float] = None,
    ) -> "AccountPool._AcquireCtx":
        """使用：``async with pool.acquire('A'): ...``"""
        return AccountPool._AcquireCtx(self, account_id, timeout)

    def stats(self) -> Dict[str, Any]:
        """返回当前 lock 占用情况（调试/运维用）。"""
        return {
            "max_parallel": self._max_parallel,
            "accounts_with_lock": list(self._locks.keys()),
            "locked_now": [
                aid for aid, lk in self._locks.items() if lk.locked()
            ],
        }


class AccountRegistry:
    """静态读 config → 构造 AccountContext 列表。不持有 runtime 资源。"""

    def __init__(
        self,
        contexts: List[AccountContext],
        pool: AccountPool,
    ) -> None:
        self._ctx_by_id: Dict[str, AccountContext] = {
            c.account_id: c for c in contexts
        }
        self.pool = pool

    @classmethod
    def from_config(
        cls,
        cfg: Dict[str, Any],
        config_path: Path | str,
    ) -> "AccountRegistry":
        """支持 2 种配置形态：

        A) 未配 ``accounts``：回退到单账号 "default"，serial 来自 cfg.adb_serial
        B) 配 ``accounts: [{id, adb_serial, prefix?, overrides?}, ...]``
        """
        config_path = Path(config_path)
        raw_accounts = cfg.get("accounts")
        max_parallel = int(
            (cfg.get("max_parallel") or cfg.get("account_max_parallel") or 2)
        )

        contexts: List[AccountContext] = []
        def _as_list(value: Any) -> List[str]:
            if isinstance(value, str):
                return [x.strip() for x in value.split(",") if x.strip()]
            if isinstance(value, list):
                return [str(x).strip() for x in value if str(x).strip()]
            return []

        if isinstance(raw_accounts, list) and raw_accounts:
            for entry in raw_accounts:
                if not isinstance(entry, dict):
                    continue
                if entry.get("enabled") is False:
                    logger.info(
                        "[account_registry] 跳过 disabled account: %s",
                        entry.get("id") or entry.get("account_id"),
                    )
                    continue
                aid = str(entry.get("id") or entry.get("account_id") or "").strip()
                if not aid:
                    logger.warning(
                        "[account_registry] 跳过无 id 的 account: %s", entry,
                    )
                    continue
                serial = str(entry.get("adb_serial") or "").strip()
                prefix = str(entry.get("chat_key_prefix") or "").strip()
                overlay = entry.get("overrides") or entry.get("config_overlay") or {}
                if not isinstance(overlay, dict):
                    overlay = {}
                label = str(entry.get("label") or aid)
                reply_profile_id = str(
                    entry.get("reply_profile_id")
                    or entry.get("persona_id")
                    or (overlay.get("account_reply_profile_id") if isinstance(overlay, dict) else "")
                    or ""
                ).strip()
                db_override = entry.get("state_db_path")
                db_path: Optional[Path] = (
                    Path(db_override).expanduser().resolve()
                    if db_override else
                    default_state_db_path(config_path, account_id=aid)
                )
                contexts.append(AccountContext(
                    account_id=aid,
                    adb_serial=serial,
                    chat_key_prefix=prefix,
                    config_overlay=dict(overlay),
                    state_db_path=db_path,
                    label=label,
                    reply_profile_id=reply_profile_id,
                    mobile_device_id=str(entry.get("mobile_device_id") or "").strip(),
                    device_number=str(entry.get("device_number") or "").strip(),
                    device_alias=str(entry.get("device_alias") or "").strip(),
                    login_account=str(
                        entry.get("login_account")
                        or entry.get("messenger_login")
                        or ""
                    ).strip(),
                    line_id=str(entry.get("line_id") or "").strip(),
                    supported_languages=_as_list(
                        entry.get("supported_languages")
                        or entry.get("languages")
                        or []
                    ),
                    supported_customer_types=_as_list(
                        entry.get("supported_customer_types")
                        or entry.get("customer_types")
                        or []
                    ),
                    persona_ids=_as_list(
                        entry.get("persona_ids")
                        or ([reply_profile_id] if reply_profile_id else [])
                    ),
                    status=str(entry.get("status") or "active"),
                    health_score=float(entry.get("health_score") or 100),
                    current_load=int(entry.get("current_load") or 0),
                    max_daily_send=int(entry.get("max_daily_send") or 200),
                ))
        else:
            # 单账号兼容：account_id="default"，serial 来自 cfg.adb_serial
            contexts.append(AccountContext(
                account_id="default",
                adb_serial=str(cfg.get("adb_serial") or "").strip(),
                chat_key_prefix="",  # default 不加前缀 → 完全兼容旧 chat_key
                config_overlay={},
                state_db_path=default_state_db_path(
                    config_path, account_id="default",
                ),
                label="default",
                supported_languages=_as_list(cfg.get("supported_languages") or []),
                supported_customer_types=_as_list(
                    cfg.get("supported_customer_types") or []
                ),
                persona_ids=[],
            ))

        pool = AccountPool(max_parallel=max_parallel)
        return cls(contexts, pool)

    def get(self, account_id: str) -> Optional[AccountContext]:
        return self._ctx_by_id.get(account_id)

    def all_contexts(self) -> List[AccountContext]:
        return list(self._ctx_by_id.values())

    def account_ids(self) -> List[str]:
        return list(self._ctx_by_id.keys())

    def size(self) -> int:
        return len(self._ctx_by_id)

    def stats(self) -> Dict[str, Any]:
        """运维 API 用：列出每 account 的关键状态。"""
        out = []
        for ctx in self.all_contexts():
            row: Dict[str, Any] = {
                "account_id": ctx.account_id,
                "label": ctx.label,
                "adb_serial": ctx.adb_serial,
                "reply_profile_id": ctx.reply_profile_id,
                "mobile_device_id": ctx.mobile_device_id,
                "device_number": ctx.device_number,
                "device_alias": ctx.device_alias,
                "login_account": ctx.login_account,
                "line_id": ctx.line_id,
                "supported_languages": list(ctx.supported_languages or []),
                "supported_customer_types": list(
                    ctx.supported_customer_types or []
                ),
                "persona_ids": list(ctx.persona_ids or []),
                "status": ctx.status,
                "health_score": ctx.health_score,
                "current_load": ctx.current_load,
                "max_daily_send": ctx.max_daily_send,
                "chat_key_prefix": ctx.chat_key_prefix or (
                    f"acc_{ctx.account_id}" if ctx.account_id != "default" else ""
                ),
                "state_db_path": str(ctx.state_db_path) if ctx.state_db_path else "",
            }
            # 不强制初始化 state_store（避免运维调用 stats 时触发文件创建）
            try:
                if ctx._state_store is not None:
                    row["send_counters"] = ctx._state_store.get_send_stats()
                    row["risk"] = ctx._state_store.get_risk_state()
            except Exception:
                pass
            out.append(row)
        return {
            "total": len(out),
            "accounts": out,
            "pool": self.pool.stats(),
        }


__all__ = ["AccountContext", "AccountPool", "AccountRegistry"]
