"""账号池编排器（M5）。

把「多账号 7×24 在线」真正跑起来的最后一块：进程启动时读 ``account_registry``，按每个账号
的 ``(platform, mode)`` 用对应 **worker** 拉起，并持续**健康监督**（失败指数退避重启），
绑定的 ``proxy`` / ``fingerprint`` 自动注入底层连接。

设计要点（多轮打磨后的取舍）：
- **worker 注册表**与登录 provider 同构（``register_worker(platform, mode, factory)``），
  protocol 等可挂载真实 worker，device/web 不在此编排（device 由既有 RPA runner 管，
  web 待 M6）。
- **监督与时钟解耦**：``tick()`` 是一次幂等监督步，``_now`` / ``_sleep`` 可注入 → 单测用
  假 worker + 假时钟**确定性**驱动启动/重启/退避/下线，无需真账号、无需长 sleep。
- **零副作用默认**：所有真实 worker 受各自 feature flag + ``orchestrator_enabled`` 门控，
  默认全关；关闭时编排器不持有任何连接，主进程行为不变。
- **WhatsApp 不双重监督**：Baileys（Node）自身在服务内重连保活，故 WA worker 是「确保 Node
  已恢复 + 读其状态」的薄监督，避免 Python/Node 两侧重复拉连接。
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.integrations.account_registry import get_account_registry
from src.integrations.shared.send_guard import send_blocked

logger = logging.getLogger(__name__)


def _record_line_identity(outcome: str) -> None:
    """记 LINE 私聊发送者显示名解析结果到 peer_identity 观测（best-effort，绝不影响主流程）。"""
    try:
        from src.web.peer_identity_stats import get_peer_identity_stats
        get_peer_identity_stats().record("line", outcome)
    except Exception:
        pass


# 仅这些 mode 由编排器接管（device 归既有 RPA runner）
# official = 官方 API 出站 worker（LINE/Messenger/WhatsApp Cloud，无状态 HTTP，G 延伸）
# web = 网页自动化 worker（Messenger 网页模式经隔离浏览器/Playwright Node 微服务；M6 落地）——
#   连接由 Node 微服务保活，Python 侧薄监督 + 路由出站（见 MessengerWebWorker）。
# 注：worker_supported 还要求对应 (platform, mode) 工厂已注册，故仅登记了工厂的 web 平台
#   （messenger）会被接管；未注册工厂的 web 账号（如遗留 telegram web 占位）自然不被拾取。
ORCHESTRATED_MODES = ("protocol", "official", "web")

# 监督参数
DEFAULT_INTERVAL = 15.0      # 监督步间隔（秒）
BACKOFF_BASE = 2.0           # 退避基数（秒）
BACKOFF_MAX = 120.0          # 退避上限（秒）
MAX_RESTARTS = 8             # 连续失败上限 → 熔断（标 error，停止重试直到人工/sync 重置）


def account_key(platform: str, account_id: str) -> str:
    return f"{str(platform).lower()}:{account_id}"


# ── worker 注册表 ────────────────────────────────────────────────────────────
# factory(account: dict, config: dict) -> Worker
#   Worker 需实现 async start()/stop()、async healthy()->bool、status()->dict
_WORKER_FACTORIES: Dict[str, Callable[..., Any]] = {}


def register_worker(platform: str, mode: str, factory: Callable[..., Any]) -> None:
    _WORKER_FACTORIES[f"{str(platform).lower()}:{str(mode).lower()}"] = factory


def get_worker_factory(platform: str, mode: str) -> Optional[Callable[..., Any]]:
    return _WORKER_FACTORIES.get(f"{str(platform).lower()}:{str(mode).lower()}")


def worker_supported(platform: str, mode: str) -> bool:
    return (mode in ORCHESTRATED_MODES) and get_worker_factory(platform, mode) is not None


# ── 被管理账号 ───────────────────────────────────────────────────────────────

@dataclass
class _Managed:
    key: str
    platform: str
    account_id: str
    mode: str
    worker: Any = None
    state: str = "stopped"      # stopped|starting|running|error|stopping
    restarts: int = 0
    last_error: str = ""
    backoff_until: float = 0.0
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        ws = {}
        if self.worker is not None and hasattr(self.worker, "status"):
            try:
                ws = self.worker.status() or {}
            except Exception:
                ws = {}
        return {
            "key": self.key, "platform": self.platform,
            "account_id": self.account_id, "mode": self.mode,
            "state": self.state, "restarts": self.restarts,
            "last_error": self.last_error, "worker": ws,
            "updated_at": self.updated_at,
        }


class AccountOrchestrator:
    def __init__(
        self,
        *,
        registry: Any = None,
        config: Optional[Dict[str, Any]] = None,
        interval: float = DEFAULT_INTERVAL,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._registry = registry if registry is not None else get_account_registry()
        self._config = config or {}
        self._interval = interval
        self._now = now
        self._sleep = sleep
        self._managed: Dict[str, _Managed] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()   # 串行化监督循环与手动 API，避免并发 start/stop 竞态

    # ── 期望状态 ─────────────────────────────────────────────────────────

    def desired_accounts(self) -> List[Dict[str, Any]]:
        out = []
        for a in self._registry.list():
            if a.get("status") == "removed":
                continue
            if worker_supported(a.get("platform", ""), a.get("mode", "")):
                out.append(a)
        return out

    # ── 单账号生命周期（公开方法加锁；内部 _* 无锁，仅供已持锁的监督步调用） ──

    async def start_account(self, account: Dict[str, Any]) -> bool:
        async with self._lock:
            return await self._start_account(account)

    async def stop_account(self, key: str) -> None:
        async with self._lock:
            await self._stop_account(key)

    async def restart_account(self, key: str) -> bool:
        async with self._lock:
            await self._stop_account(key)
            m = self._managed.get(key)
            if m is None:
                return False
            m.restarts = 0
            m.backoff_until = 0.0
            return await self._start_account({
                "platform": m.platform, "account_id": m.account_id, "mode": m.mode,
                **(self._registry.get(m.platform, m.account_id) or {}),
            })

    async def _start_account(self, account: Dict[str, Any]) -> bool:
        platform = str(account.get("platform") or "")
        account_id = str(account.get("account_id") or "")
        mode = str(account.get("mode") or "")
        key = account_key(platform, account_id)
        m = self._managed.get(key)
        if m is None:
            m = _Managed(key=key, platform=platform, account_id=account_id, mode=mode)
            self._managed[key] = m
        if m.state in ("running", "starting"):
            return True
        factory = get_worker_factory(platform, mode)
        if factory is None:
            m.state = "error"
            m.last_error = "no worker factory"
            return False
        m.state = "starting"
        m.updated_at = self._now_wall()
        try:
            if m.worker is None:
                m.worker = factory(account, self._config)
            await m.worker.start()
            m.state = "running"
            m.last_error = ""
            m.restarts = 0
            m.backoff_until = 0.0
            return True
        except Exception as ex:  # noqa: BLE001
            m.state = "error"
            m.last_error = str(ex)
            m.restarts += 1
            self._schedule_backoff(m)
            logger.debug("[orchestrator] 启动账号失败 %s", key, exc_info=True)
            return False

    async def _stop_account(self, key: str) -> None:
        m = self._managed.get(key)
        if m is None:
            return
        m.state = "stopping"
        try:
            if m.worker is not None and hasattr(m.worker, "stop"):
                await m.worker.stop()
        except Exception:
            logger.debug("[orchestrator] 停止账号失败 %s", key, exc_info=True)
        m.state = "stopped"
        m.worker = None
        m.updated_at = self._now_wall()

    # ── 监督 ─────────────────────────────────────────────────────────────

    async def sync(self) -> None:
        """对齐：拉起期望但未在管的；下线已移除/不再期望的。"""
        async with self._lock:
            desired = {account_key(a["platform"], a["account_id"]): a
                       for a in self.desired_accounts()}
            for key, acc in desired.items():
                m = self._managed.get(key)
                if m is None or m.state == "stopped":
                    await self._start_account(acc)
            for key in list(self._managed.keys()):
                if key not in desired and self._managed[key].state != "stopped":
                    await self._stop_account(key)

    async def tick(self) -> None:
        """一次监督步：健康检查 + 退避重启（幂等，可被测试直接驱动）。"""
        async with self._lock:
            now = self._now()
            for key, m in list(self._managed.items()):
                if m.state == "running":
                    healthy = await self._safe_healthy(m)
                    if not healthy:
                        m.state = "error"
                        m.last_error = m.last_error or "unhealthy"
                        m.restarts += 1
                        self._schedule_backoff(m)
                elif m.state == "error":
                    if m.restarts >= MAX_RESTARTS:
                        continue  # 熔断，等待人工/sync 重置
                    if now >= m.backoff_until:
                        acc = self._registry.get(m.platform, m.account_id) or {
                            "platform": m.platform, "account_id": m.account_id,
                            "mode": m.mode}
                        await self._start_account(acc)

    async def _safe_healthy(self, m: _Managed) -> bool:
        try:
            if m.worker is not None and hasattr(m.worker, "healthy"):
                return bool(await m.worker.healthy())
        except Exception:
            logger.debug("[orchestrator] healthy 检查异常 %s", m.key, exc_info=True)
            return False
        return True

    def _schedule_backoff(self, m: _Managed) -> None:
        delay = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** max(0, m.restarts - 1)))
        delay *= 0.8 + 0.4 * random.random()  # ±20% 抖动，避免雪崩
        m.backoff_until = self._now() + delay
        m.updated_at = self._now_wall()

    def _now_wall(self) -> float:
        return time.time()

    # ── 后台循环 ─────────────────────────────────────────────────────────

    async def start_loop(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info("[orchestrator] 监督循环已启动 (interval=%ss)", self._interval)

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.sync()
                await self.tick()
            except Exception:
                logger.debug("[orchestrator] 监督步异常", exc_info=True)
            await self._sleep(self._interval)

    async def stop_loop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        for key in list(self._managed.keys()):
            await self.stop_account(key)

    # ── 收发桥接（M6①：protocol 账号接入统一收件箱） ─────────────────────

    def owns(self, platform: str, account_id: str) -> bool:
        """该 (platform, account_id) 是否有正在运行、且可发送的受管 worker。"""
        m = self._managed.get(account_key(platform, account_id))
        return bool(
            m is not None and m.state == "running"
            and m.worker is not None and hasattr(m.worker, "send")
        )

    def worker_for(self, platform: str, account_id: str) -> Any:
        """返回该账号**正在运行**的受管 worker（供取 pyrogram client 做头像/身份解析）。

        多账号头像/补名的取数入口：非主账号的 pyrogram client 藏在其受管 worker 里
        （companion A 线 worker.client.client / protocol B 线 worker.client）。无运行中
        worker → None（调用方回落进程主 client）。
        """
        m = self._managed.get(account_key(platform, account_id))
        if m is not None and m.state == "running" and m.worker is not None:
            return m.worker
        return None

    def owns_media(self, platform: str, account_id: str) -> bool:
        """该账号是否有运行中、且支持发送媒体的 worker。"""
        m = self._managed.get(account_key(platform, account_id))
        return bool(
            m is not None and m.state == "running"
            and m.worker is not None and hasattr(m.worker, "send_media")
        )

    async def send_media(
        self, platform: str, account_id: str, chat_key: str, *,
        media_path: str, media_url: str, media_type: str, caption: str = "",
        inbox_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """经 worker 发送媒体，并把出站媒体消息回写收件箱线程（media_ref 用 /static URL）。

        ``inbox_text``：**仅**回写给收件箱（坐席台可读）的文本，不发给客户；为 None 时回落
        ``caption``（向后兼容）。语音出站用它把「念了什么」带进会话视图——坐席不播放也能读，
        客户那边仍是纯语音（caption 不变）。
        """
        # Stage M：编排器发送入口统一护栏（Kill-Switch + 反封号闸门）——富媒体与文本同守。
        _blk, _reason = send_blocked(
            platform, account_id, config=self._config, registry=self._registry)
        if _blk:
            logger.warning("[orchestrator] 媒体发送被护栏拦截 %s:%s (%s)",
                           platform, account_id, _reason)
            return {"delivered": False, "blocked": _reason}
        m = self._managed.get(account_key(platform, account_id))
        if not (m is not None and m.state == "running"
                and m.worker is not None and hasattr(m.worker, "send_media")):
            raise RuntimeError(f"无可用的运行中 worker(媒体): {platform}:{account_id}")
        # 透传 media_url 给支持的 worker（LINE/Messenger 官方通道需公网 URL 拉取）；
        # 旧 worker（telegram/wa-protocol/测试 fake）签名无此参 → 经签名探测跳过，零回归。
        _sm = m.worker.send_media
        _kw: Dict[str, Any] = dict(
            media_path=media_path, media_type=media_type, caption=caption)
        try:
            import inspect
            if "media_url" in inspect.signature(_sm).parameters:
                _kw["media_url"] = media_url
        except (ValueError, TypeError):
            pass
        res = await _sm(chat_key, **_kw)
        # P0-4：带回平台消息 id(wamid)，让出站回写与 worker 的 fromMe 回显同键去重
        _mid = str(res.get("message_id") or "") if isinstance(res, dict) else ""
        try:
            from src.integrations.protocol_bridge import emit_incoming, make_message
            _itext = inbox_text if inbox_text is not None else caption
            emit_incoming(make_message(
                platform=platform, account_id=account_id, chat_key=chat_key,
                text=_itext, direction="out", msg_id=_mid,
                media_type=media_type, media_ref=media_url,
            ))
        except Exception:
            logger.debug("[orchestrator] 出站媒体回写收件箱失败", exc_info=True)
        return res if isinstance(res, dict) else {"delivered": True}

    async def send(
        self, platform: str, account_id: str, chat_key: str, text: str,
        *, reply_to: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """经受管 worker 发送，并把出站消息回写收件箱线程。

        P4-5B：``reply_to`` 携带原生引用回复上下文——若 worker 的 send 支持该 kwarg
        （WhatsApp 协议 worker）则透传发原生引用；否则退回普通发送（TypeError 兜底）。
        引用摘要一并写进出站消息的 source.reply_to，使本端气泡也渲染引用条。
        """
        # Stage M：编排器发送入口统一护栏（Kill-Switch + 反封号闸门）——所有经编排器的
        # 外发（主动问候/唤醒/关怀/接管）都从这里走，旁路发送不再绕过急停与反封号。
        _blk, _reason = send_blocked(
            platform, account_id, config=self._config, registry=self._registry)
        if _blk:
            logger.warning("[orchestrator] 发送被护栏拦截 %s:%s (%s)",
                           platform, account_id, _reason)
            return {"delivered": False, "blocked": _reason}
        m = self._managed.get(account_key(platform, account_id))
        if not (m is not None and m.state == "running"
                and m.worker is not None and hasattr(m.worker, "send")):
            raise RuntimeError(f"无可用的运行中 worker: {platform}:{account_id}")
        if reply_to:
            try:
                res = await m.worker.send(chat_key, text, reply_to=reply_to)
            except TypeError:
                # worker 不支持 reply_to kwarg（非协议 worker）→ 退回普通发送
                res = await m.worker.send(chat_key, text)
        else:
            res = await m.worker.send(chat_key, text)
        # P0-4：带回平台消息 id(wamid)，让出站回写与 worker 的 fromMe 回显同键去重
        _mid = str(res.get("message_id") or "") if isinstance(res, dict) else ""
        try:
            from src.integrations.protocol_bridge import emit_incoming, make_message
            _src = None
            if reply_to and (reply_to.get("id") or reply_to.get("text")):
                _src = {"reply_to": {
                    "id": str(reply_to.get("id") or ""),
                    "text": str(reply_to.get("text") or ""),
                    "sender": str(reply_to.get("sender") or ""),
                }}
            emit_incoming(make_message(
                platform=platform, account_id=account_id, chat_key=chat_key,
                text=text, direction="out", msg_id=_mid, source=_src,
            ))
            # P4-4：Telegram 发送成功即置「已发送」（单勾）；对端读后由
            # UpdateReadHistoryOutbox 回执升级为「已读」（蓝色双勾）。
            if platform == "telegram" and _mid:
                from src.integrations.protocol_bridge import report_message_status
                report_message_status(platform, account_id, chat_key, _mid, "sent")
        except Exception:
            logger.debug("[orchestrator] 出站回写收件箱失败", exc_info=True)
        return res if isinstance(res, dict) else {"delivered": True}

    # ── 状态 ─────────────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        accts = [m.to_dict() for m in self._managed.values()]
        return {
            "running_loop": self._running,
            "interval": self._interval,
            "total": len(accts),
            "by_state": _count_by_state(accts),
            "accounts": accts,
        }


def _count_by_state(accts: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for a in accts:
        out[a["state"]] = out.get(a["state"], 0) + 1
    return out


# ── 内置 worker（懒注册，门控） ───────────────────────────────────────────────

def ensure_builtin_workers(config: Dict[str, Any]) -> None:
    """按需注册内置 worker（幂等、门控）。"""
    try:
        from src.integrations.telegram_protocol_login import (
            is_pyrogram_available, protocol_enabled as tg_enabled, resolve_credentials,
        )
        if (tg_enabled(config) and is_pyrogram_available()
                and resolve_credentials(config) is not None
                and get_worker_factory("telegram", "protocol") is None):
            # N 线 核心4：companion_runtime 开 → 协议号跑 A 线"有灵魂"client；否则用 B 线薄连接
            from src.integrations.telegram_companion_worker import (
                TelegramCompanionWorker, companion_runtime_enabled,
            )
            if companion_runtime_enabled(config):
                register_worker("telegram", "protocol",
                                lambda acc, cfg: TelegramCompanionWorker(acc, cfg))
                logger.info("[orchestrator] Telegram 协议号将使用 A 线统一运行时（companion_runtime）")
            else:
                register_worker("telegram", "protocol",
                                lambda acc, cfg: TelegramProtocolWorker(acc, cfg))
    except Exception:
        logger.debug("[orchestrator] 注册 telegram worker 失败", exc_info=True)
    try:
        from src.integrations.whatsapp_baileys_login import protocol_enabled as wa_enabled
        if wa_enabled(config) and get_worker_factory("whatsapp", "protocol") is None:
            register_worker("whatsapp", "protocol",
                            lambda acc, cfg: WhatsAppProtocolWorker(acc, cfg))
    except Exception:
        logger.debug("[orchestrator] 注册 whatsapp worker 失败", exc_info=True)
    try:
        from src.integrations.messenger_web_login import web_enabled as mg_web_enabled
        if mg_web_enabled(config) and get_worker_factory("messenger", "web") is None:
            register_worker("messenger", "web",
                            lambda acc, cfg: MessengerWebWorker(acc, cfg))
    except Exception:
        logger.debug("[orchestrator] 注册 messenger web worker 失败", exc_info=True)
    try:
        from src.integrations.line_protocol_login import (
            protocol_enabled as line_enabled, is_okline_available,
        )
        if (line_enabled(config) and is_okline_available()
                and get_worker_factory("line", "protocol") is None):
            register_worker("line", "protocol",
                            lambda acc, cfg: LineProtocolWorker(acc, cfg))
    except Exception:
        logger.debug("[orchestrator] 注册 line protocol worker 失败", exc_info=True)
    # 官方 API 出站 worker（LINE/Messenger/WhatsApp Cloud，mode=official；G 延伸）
    try:
        from src.integrations.official_api_worker import register_official_workers
        register_official_workers(config)
    except Exception:
        logger.debug("[orchestrator] 注册官方 worker 失败", exc_info=True)


class TelegramProtocolWorker:
    """保活一个 Telegram pyrogram 协议连接（从 M2 落地的 session 拉起）。"""

    def __init__(self, account: Dict[str, Any], config: Dict[str, Any]) -> None:
        self.account = account
        self.config = config
        self.account_id = str(account.get("account_id") or "")
        self.session_name = str((account.get("meta") or {}).get("session_name") or "")
        # N2/N4：优先 session_string 内存启动（抗文件 session SQLite 锁 / DC 迁移不稳）
        self.session_string = str((account.get("meta") or {}).get("session_string") or "")
        self.client: Any = None
        self.state = "stopped"
        self.detail = ""

    def _proxy(self) -> Optional[Dict[str, Any]]:
        pid = self.account.get("proxy_id") or ""
        if not pid:
            return None
        try:
            from src.integrations.proxy_pool import get_proxy_pool
            from src.integrations.telegram_protocol_login import _to_pyrogram_proxy
            return _to_pyrogram_proxy(get_proxy_pool().get(pid, mask=False))
        except Exception:
            return None

    async def start(self) -> None:
        from src.integrations.telegram_protocol_login import resolve_credentials
        creds = resolve_credentials(self.config)
        if creds is None or not (self.session_name or self.session_string):
            raise RuntimeError("缺少 api 凭据或 session_name/session_string")
        api_id, api_hash = creds
        # 重试前先清理可能残留的旧 client，避免连接泄漏
        if self.client is not None:
            try:
                await self.client.stop()
            except Exception:
                pass
            self.client = None
        from pyrogram import Client
        kwargs: Dict[str, Any] = dict(api_id=api_id, api_hash=api_hash)
        proxy = self._proxy()
        if proxy:
            kwargs["proxy"] = proxy
        if self.session_string:
            # N2/N4：内存会话启动——不碰 sessions/*.session 文件，规避扫码 client
            # 残留连接造成的 "database is locked"，也更抗 DC 迁移。
            name = self.session_name or f"mem_{self.account_id}"
            self.client = Client(name, session_string=self.session_string, **kwargs)
        else:
            kwargs["workdir"] = "sessions"
            self.client = Client(self.session_name, **kwargs)
        await self.client.start()
        self._wire_inbound()
        self._wire_receipts()
        self.state = "running"
        self.detail = ""
        await self._backfill()

    def _wire_inbound(self) -> None:
        """注册 pyrogram 消息处理器：收到消息 → 推入统一收件箱（best-effort）。"""
        try:
            from pyrogram.handlers import MessageHandler

            account_id = self.account_id

            async def _on_msg(_client: Any, message: Any) -> None:  # noqa: ANN401
                try:
                    from src.integrations.protocol_bridge import (
                        download_tg_media, emit_incoming, maybe_auto_reply,
                        tg_message_payload,
                    )
                    media_type, media_ref = await download_tg_media(message, account_id)
                    payload = tg_message_payload(
                        message, account_id,
                        media_type=media_type, media_ref=media_ref)
                    if payload is not None:
                        emit_incoming(payload)
                        await maybe_auto_reply(payload)
                except Exception:
                    logger.debug("[tg-worker] inbound 推送失败", exc_info=True)

            self.client.add_handler(MessageHandler(_on_msg))
        except Exception:
            logger.debug("[tg-worker] 注册消息处理器失败", exc_info=True)

    def _wire_receipts(self) -> None:
        """注册 pyrogram 原始更新处理器：对端读了我们发的消息（``UpdateReadHistoryOutbox``
        / 频道版 ``UpdateReadChannelOutbox``）→ 把该会话 ≤max_id 的出站消息升级为「已读」，
        前端出站气泡即显示蓝色双勾（best-effort，不影响主消息流）。"""
        try:
            from pyrogram.handlers import RawUpdateHandler
            from pyrogram import raw

            account_id = self.account_id

            async def _on_raw(_client: Any, update: Any, _users: Any, _chats: Any) -> None:  # noqa: ANN401
                try:
                    from src.integrations.protocol_bridge import (
                        report_read_upto, tg_peer_to_chat_key,
                    )
                    if isinstance(update, raw.types.UpdateReadHistoryOutbox):
                        ck = tg_peer_to_chat_key(getattr(update, "peer", None))
                        if ck:
                            report_read_upto("telegram", account_id, ck,
                                             getattr(update, "max_id", 0))
                    elif isinstance(update, raw.types.UpdateReadChannelOutbox):
                        chid = getattr(update, "channel_id", None)
                        if chid is not None:
                            report_read_upto("telegram", account_id, f"-100{int(chid)}",
                                             getattr(update, "max_id", 0))
                except Exception:
                    logger.debug("[tg-worker] 已读回执处理失败", exc_info=True)

            self.client.add_handler(RawUpdateHandler(_on_raw))
        except Exception:
            logger.debug("[tg-worker] 注册已读回执处理器失败", exc_info=True)

    def _backfill_limit(self) -> int:
        try:
            tg = ((self.config.get("platform_login") or {}).get("telegram") or {})
            return int(tg.get("backfill_dialogs", 20) or 0)
        except Exception:
            return 0

    async def _backfill(self) -> None:
        """首连历史回填（best-effort，不阻断启动）。"""
        try:
            from src.integrations.protocol_bridge import backfill_telegram
            limit = self._backfill_limit()
            if limit > 0:
                await backfill_telegram(self.client, self.account_id, limit)
        except Exception:
            logger.debug("[tg-worker] 历史回填失败", exc_info=True)

    async def send(self, chat_key: str, text: str) -> Dict[str, Any]:
        if self.client is None:
            raise RuntimeError("telegram client 未连接")
        target: Any = chat_key
        try:
            target = int(chat_key)
        except (TypeError, ValueError):
            target = chat_key
        msg = await self.client.send_message(target, text)
        return {"delivered": True, "message_id": str(getattr(msg, "id", "") or "")}

    async def send_media(self, chat_key: str, *, media_path: str,
                         media_type: str, caption: str = "") -> Dict[str, Any]:
        if self.client is None:
            raise RuntimeError("telegram client 未连接")
        target: Any = chat_key
        try:
            target = int(chat_key)
        except (TypeError, ValueError):
            target = chat_key
        kind = str(media_type or "").lower()
        if kind == "image":
            msg = await self.client.send_photo(target, media_path, caption=caption)
        elif kind == "voice":
            msg = await self.client.send_voice(target, media_path, caption=caption)
        elif kind == "video":
            msg = await self.client.send_video(target, media_path, caption=caption)
        else:
            msg = await self.client.send_document(target, media_path, caption=caption)
        return {"delivered": True, "message_id": str(getattr(msg, "id", "") or "")}

    async def stop(self) -> None:
        try:
            if self.client is not None:
                await self.client.stop()
        except Exception:
            pass
        self.client = None
        self.state = "stopped"

    async def healthy(self) -> bool:
        try:
            return bool(self.client is not None and self.client.is_connected)
        except Exception:
            return False

    def status(self) -> Dict[str, Any]:
        return {"type": "telegram_protocol", "session": self.session_name,
                "state": self.state, "detail": self.detail}


class WhatsAppProtocolWorker:
    """薄监督一个 WhatsApp(Baileys) 账号：确保 Node 已恢复 + 读其状态。"""

    def __init__(self, account: Dict[str, Any], config: Dict[str, Any]) -> None:
        self.account = account
        self.config = config
        self.account_id = str(account.get("account_id") or "")
        self.state = "stopped"
        self.detail = ""

    def _base(self) -> str:
        from src.integrations.whatsapp_baileys_login import service_base_url
        return service_base_url(self.config)

    async def start(self) -> None:
        from src.integrations.whatsapp_baileys_login import _post_json
        # 触发 Node 恢复所有持久化 session（幂等）；Node 自身也会在开机时恢复
        await _post_json(f"{self._base()}/accounts/restore", {})
        self.state = "running"
        self.detail = ""

    async def send(self, chat_key: str, text: str,
                   *, reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from src.integrations.whatsapp_baileys_login import _post_json
        payload: Dict[str, Any] = {"jid": chat_key, "text": text}
        # P4-5B 原生引用回复：把被引用消息 key + 文本摘要下发给 Baileys 的 quoted 选项
        if reply_to and reply_to.get("id"):
            payload["quoted"] = {
                "id": str(reply_to.get("id") or ""),
                "from_me": bool(reply_to.get("from_me")),
                "participant": str(reply_to.get("participant") or ""),
                "text": str(reply_to.get("text") or ""),
            }
        res = await _post_json(
            f"{self._base()}/accounts/{self.account_id}/send", payload,
        )
        return {"delivered": bool((res or {}).get("ok", True)),
                "message_id": str((res or {}).get("message_id") or "")}

    async def send_media(self, chat_key: str, *, media_path: str,
                         media_type: str, caption: str = "") -> Dict[str, Any]:
        from src.integrations.whatsapp_baileys_login import _post_json
        res = await _post_json(
            f"{self._base()}/accounts/{self.account_id}/send-media",
            {"jid": chat_key, "path": media_path,
             "media_type": media_type, "caption": caption},
        )
        return {"delivered": bool((res or {}).get("ok", True)),
                "message_id": str((res or {}).get("message_id") or "")}

    async def stop(self) -> None:
        # 不登出（Baileys 连接由 Node 保活）；仅停止 Python 侧监督
        self.state = "stopped"

    async def healthy(self) -> bool:
        from src.integrations.whatsapp_baileys_login import _get_json
        try:
            res = await _get_json(f"{self._base()}/accounts")
            ids = {str(a.get("account_id") or "") for a in (res.get("accounts") or [])}
            return self.account_id in ids
        except Exception:
            return False

    def status(self) -> Dict[str, Any]:
        return {"type": "whatsapp_protocol", "account_id": self.account_id,
                "state": self.state, "detail": self.detail}


class MessengerWebWorker:
    """薄监督一个 Messenger(网页模式) 账号：确保 Node/Playwright 微服务已恢复 + 读其状态。

    与 ``WhatsAppProtocolWorker`` 同构（连接由 Node 微服务保活，Python 侧只监督 + 路由出站）。
    """

    def __init__(self, account: Dict[str, Any], config: Dict[str, Any]) -> None:
        self.account = account
        self.config = config
        self.account_id = str(account.get("account_id") or "")
        self.state = "stopped"
        self.detail = ""

    def _base(self) -> str:
        from src.integrations.messenger_web_login import service_base_url
        return service_base_url(self.config)

    async def start(self) -> None:
        from src.integrations.messenger_web_login import _post_json
        # 触发 Node 恢复所有持久化 profile（幂等）；Node 自身也会在开机时恢复
        await _post_json(f"{self._base()}/accounts/restore", {})
        self.state = "running"
        self.detail = ""

    async def send(self, chat_key: str, text: str,
                   *, reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from src.integrations.messenger_web_login import _post_json
        res = await _post_json(
            f"{self._base()}/accounts/{self.account_id}/send",
            {"jid": chat_key, "text": text},
        )
        return {"delivered": bool((res or {}).get("ok", True)),
                "message_id": str((res or {}).get("message_id") or "")}

    async def stop(self) -> None:
        # 不登出（浏览器上下文由 Node 保活）；仅停止 Python 侧监督
        self.state = "stopped"

    async def healthy(self) -> bool:
        from src.integrations.messenger_web_login import _get_json
        try:
            res = await _get_json(f"{self._base()}/accounts")
            ids = {str(a.get("account_id") or "") for a in (res.get("accounts") or [])}
            return self.account_id in ids
        except Exception:
            return False

    def status(self) -> Dict[str, Any]:
        return {"type": "messenger_web", "account_id": self.account_id,
                "state": self.state, "detail": self.detail}


class LineProtocolWorker:
    """保活一个 LINE(okline 协议) 连接：从落库的 tokens 拉起 client + 后台 Bot 收消息。

    进程内 worker（仿 ``TelegramProtocolWorker``）：okline 是同步(requests)库，收消息用
    ``Bot.run`` 阻塞长轮询 → 放后台 daemon 线程；入站经 ``emit_incoming`` 同步落库，
    自动回复经 ``run_coroutine_threadsafe`` 调度回主事件循环。
    """

    def __init__(self, account: Dict[str, Any], config: Dict[str, Any]) -> None:
        self.account = account
        self.config = config
        self.account_id = str(account.get("account_id") or "")
        self.tokens_path = str((account.get("meta") or {}).get("tokens_path") or "")
        self.client: Any = None
        self.bot: Any = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.state = "stopped"
        self.detail = ""
        # peer mid → (显示名, 头像 URL) 缓存（含 ("","")=已查过无，避免每条消息重复打 getContactsV2）
        self._peer_ident_cache: Dict[str, tuple] = {}

    async def start(self) -> None:
        from src.integrations.line_protocol_login import (
            is_okline_available, tokens_path as _tp,
        )
        if not is_okline_available():
            raise RuntimeError("okline 未安装")
        path = self.tokens_path or _tp(self.config, self.account_id)
        if not path or not os.path.exists(path):
            raise RuntimeError(f"缺少 LINE session tokens: {path}")
        from okline import OkLine
        self.client = OkLine.from_tokens_file(path)
        self._loop = asyncio.get_running_loop()
        self._start_receiver()
        self.state = "running"
        self.detail = ""

    def _start_receiver(self) -> None:
        """后台 daemon 线程跑 okline Bot：收到消息 → 落库 + 自动回复（best-effort）。"""
        from okline import Bot
        from src.integrations.protocol_bridge import (
            emit_incoming, make_message, maybe_auto_reply,
        )
        account_id = self.account_id
        client = self.client
        loop = self._loop
        bot = Bot(client)

        @bot.on_message
        def _on_msg(ctx: Any) -> None:  # noqa: ANN401
            try:
                text = ctx.text or ""
                is_group = bool(ctx.is_group)
                chat_key = str((ctx.to if is_group else ctx.sender) or "")
                if not chat_key:
                    return
                # 私聊：按需向 LINE 拉发送者显示名+头像（getContactsV2 同一次调用免费取头像，
                # per-peer 缓存）——修「LINE 私聊只显示裸 mid + 无头像」。查的是**对方** mid，
                # 天然规避「误标成本账号名」。obs 直链稳定 → 直接落库 avatar_url 由前端渲染。
                peer_name, peer_avatar = ("", "") if is_group else self._resolve_peer_identity(chat_key)
                payload = make_message(
                    platform="line", account_id=account_id, chat_key=chat_key,
                    name=peer_name, avatar_url=peer_avatar, text=str(text),
                    msg_id=str((ctx.message or {}).get("id") or ""),
                    direction="in")
                if is_group:
                    payload["chat_type"] = "group"
                emit_incoming(payload)
                if not is_group and loop is not None:
                    asyncio.run_coroutine_threadsafe(maybe_auto_reply(payload), loop)
            except Exception:
                logger.debug("[line-worker] inbound 推送失败", exc_info=True)

        self.bot = bot

        def _run() -> None:
            try:
                bot.run(reconnect=True)
            except Exception:
                logger.debug("[line-worker] receiver 循环退出", exc_info=True)

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def _resolve_peer_identity(self, mid: str) -> tuple:
        """惰性解析 LINE 私聊发送者 ``(显示名, 头像 URL)``（备注名优先），per-peer 缓存、best-effort。

        在 okline 接收线程的 dispatch 内同步调用——此刻长轮询处于空闲（op 已收妥），故
        单发 ``getContactsV2`` 不与轮询争用连接，安全。查的是**对方 mid**（非本账号），故
        不会把「对方」误标成本账号名。取不到/异常 → ``("","")``（缓存以免逐条重打），交由
        no-clobber + 通讯录补名兜底。备注名 ``displayNameOverridden`` 优先（贴合账号主
        在客户端看到的称呼），否则回落公开 ``displayName``。**头像随同一次 get_contacts 免费
        取得**（零额外 API），``picturePath`` 经 ``line_picture_url`` 拼成稳定 obs 直链。
        """
        if not mid:
            return "", ""
        cached = self._peer_ident_cache.get(mid)
        if cached is not None:
            _record_line_identity("cache_hit")
            return cached
        name, avatar = "", ""
        try:
            from okline import Contact
            from src.integrations.line_protocol_login import line_picture_url
            res = self.client.get_contacts([mid])
            entry = ((res or {}).get("contacts") or {}).get(mid) if isinstance(res, dict) else None
            if entry is not None:
                contact = Contact.from_dict(entry)
                name = str(contact.display_name_overridden or contact.display_name or "").strip()
                pic = str(getattr(contact, "picture_path", "") or "")
                if not pic and isinstance(entry, dict):   # 回落原始 dict（Contact 未暴露该字段时）
                    inner = entry.get("contact") if isinstance(entry.get("contact"), dict) else entry
                    pic = str((inner or {}).get("picturePath") or "")
                avatar = line_picture_url(pic)
        except Exception:
            logger.debug("[line-worker] peer 身份解析失败 mid=%s", mid, exc_info=True)
            name, avatar = "", ""
        self._peer_ident_cache[mid] = (name, avatar)
        _record_line_identity("resolved" if name else "miss")
        return name, avatar

    def _resolve_peer_name(self, mid: str) -> str:
        """向后兼容薄封装：仅取显示名（内部走 ``_resolve_peer_identity``，头像一并缓存）。"""
        return self._resolve_peer_identity(mid)[0]

    async def send(self, chat_key: str, text: str,
                   *, reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.client is None:
            raise RuntimeError("line client 未连接")
        res = await asyncio.to_thread(self.client.send_text, chat_key, text)
        mid = ""
        try:
            if isinstance(res, dict):
                mid = str(res.get("id") or "")
        except Exception:
            mid = ""
        return {"delivered": True, "message_id": mid}

    async def stop(self) -> None:
        self.state = "stopped"
        try:
            if self.client is not None:
                self.client.close()
        except Exception:
            pass

    async def healthy(self) -> bool:
        return bool(self.client is not None and self._thread is not None
                    and self._thread.is_alive())

    def status(self) -> Dict[str, Any]:
        return {"type": "line_protocol", "account_id": self.account_id,
                "state": self.state, "detail": self.detail}


_orchestrator: Optional[AccountOrchestrator] = None


def get_orchestrator(config: Optional[Dict[str, Any]] = None) -> AccountOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = AccountOrchestrator(config=config or {})
    return _orchestrator


def get_orchestrator_if_running() -> Optional[AccountOrchestrator]:
    """返回**已创建**的编排器单例；不存在则 None，**绝不创建**。

    供只读取数（头像/身份解析按 account_id 取 worker client）——避免以空配置误建单例
    而遮蔽后续 app 以真实 config 建的实例。
    """
    return _orchestrator


def orchestrator_enabled(config: Dict[str, Any]) -> bool:
    pl = (config or {}).get("platform_login", {}) or {}
    return bool(pl.get("orchestrator_enabled", False))
