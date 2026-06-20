"""W2-D4.2/4.3：reactivation 真发循环。

scheduler 现在是纯查询工具（list_candidates）；这一层是发送闭环：

  loop tick →
    1. list_candidates(now)
    2. 对每个候选：找 messenger 上的 ChannelIdentity → external_id + account_id
    3. 拉 episodic memory（让 AI 引用具体事）
    4. 调 ai_client 生成 reactivation 消息
    5. 通过 send_callback 把 (reply, account_id, chat_name, defer_until) 入
       messenger 的 deferred 队列（reason='reactivation:...'，drain loop 自动发）
    6. mark_sent 落 reactivation_sent 事件防重复

不直接 send，因为：
- messenger 的 deferred 队列已经做了 gate / staleness / pause / pacing 一整套保险
- 主动发消息更不能在 quiet_hours / 超 daily_cap，必须复用同样保护

LLM 生成 prompt 强制引用 episodic：
- 拒绝输出"在吗" / "好久没聊"等空话
- 必须引用 episodic memory 里的某件具体事（"上次你说面试...", "之前你提过想吃寿司"）
- 找不到 episodic 内容就 skip（不发空话比发空话好）
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable, List, Optional, Protocol

from src.skills.reactivation_scheduler import (
    ReactivationCandidate,
    ReactivationScheduler,
)

logger = logging.getLogger(__name__)


class _AIClientProto(Protocol):
    async def chat(self, prompt: str, **kw) -> Optional[str]: ...


class _StoreProto(Protocol):
    def get_contact(self, contact_id: str) -> Optional[Any]: ...
    def list_channel_identities_of(self, contact_id: str) -> List[Any]: ...
    def get_journey_by_contact(self, contact_id: str) -> Optional[Any]: ...


# send_callback 签名：(channel, account_id, chat_name, reply_text, defer_until,
#                       reason, staleness_sec, extra) -> int (deferred_id) or 0 on fail
SendCallback = Callable[
    [str, str, str, str, float, str, float, dict],
    Awaitable[int],
]

# episodic_provider：拿 journey 对象 → episodic memory text（便于 prompt 注入）
# 接 journey 对象避免调用方再查一次
EpisodicProvider = Callable[[Any], str]


# 各渠道在 prompt 里的口语化平台名（让"在哪聊"自然，不写死 Messenger）
_PLATFORM_LABELS = {
    "messenger": "Facebook Messenger",
    "telegram": "Telegram",
    "line": "LINE",
    "whatsapp": "WhatsApp",
    "instagram": "Instagram",
    "zalo": "Zalo",
}

_REACTIVATION_PROMPT = """你是「{ai_name}」，正在和对方在 {platform_label} 上私聊。
对方上次互动是 **{silent_days:.0f} 天前**，需要你主动发一条消息把关系延续下去。

【画像 / 已知信息】
{portrait_block}

【最近对话要点（务必引用其中的某一件具体事）】
{episodic_block}

请用对方习惯的语言（{lang}）发**一条**主动联系的消息。要求：
- 像久违朋友自然发的消息，不要营销腔不要客服腔
- **必须引用上面 episodic memory 里的某件具体事**（"上次你说...", "你之前提过..."）
- 短小自然 1-2 句话，可以含一个 emoji
- 语气随意亲切，不要"在吗" / "好久没聊" 这种空话
- 不要主动索要联系方式 / 不发链接
- 不要在回复里说"我是 AI"或类似身份相关的话

直接输出消息文本，不要前后缀，不要解释。"""


class ReactivationLoop:
    """W2-D4.2：reactivation 调度发送循环。

    设计为可注入：
    - scheduler：ReactivationScheduler（list_candidates）
    - store：ContactsStore（找 ChannelIdentity）
    - ai_client：生成消息
    - send_callback：把 reply 入 messenger deferred 队列（main.py 注入）
    - episodic_provider：拿 episodic memory（main.py 注入）
    """

    def __init__(
        self,
        *,
        scheduler: ReactivationScheduler,
        store: _StoreProto,
        ai_client: _AIClientProto,
        send_callback: SendCallback,
        episodic_provider: Optional[EpisodicProvider] = None,
        ai_name: str = "她",
        max_per_tick: int = 3,
        interval_sec: float = 600.0,
        skip_if_no_episodic: bool = True,
        dry_run: bool = False,
        first_run_grace_minutes: float = 60.0,
        first_run_max_per_tick: int = 1,
        platform_priority: Optional[List[str]] = None,
    ) -> None:
        self._scheduler = scheduler
        self._store = store
        self._ai = ai_client
        self._send = send_callback
        self._episodic_provider = episodic_provider
        self._ai_name = ai_name or "她"
        # 多平台：按优先级在该 contact 的 ChannelIdentity 里选一个渠道发。
        # 默认仅 messenger（零破坏既有行为）；main.py 可经 config 放宽到 telegram/line/…
        # 非 messenger 渠道经 send_callback 路由到多平台 deferred 队列（main.py 接线）。
        _pri = [str(p).strip() for p in (platform_priority or ["messenger"]) if str(p).strip()]
        self._platform_priority = _pri or ["messenger"]
        self._max_per_tick = max(1, int(max_per_tick))
        self._interval = max(60.0, float(interval_sec))
        self._skip_if_no_episodic = bool(skip_if_no_episodic)
        # ★ W2-D4.6：dry_run = 只生成消息+log，不真 enqueue 也不 mark_sent
        # 用于灰度第一阶段观察 LLM 输出质量
        self._dry_run = bool(dry_run)
        # ★ W2-D4.6：保守起步 — 启动后前 N 分钟 max_per_tick 强制限到 1，避免一波打挤
        self._first_run_grace_sec = max(0.0, float(first_run_grace_minutes) * 60.0)
        self._first_run_max_per_tick = max(1, int(first_run_max_per_tick))
        self._started_ts: float = 0.0
        self._stop_evt: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._started_ts = time.time()
        self._stop_evt = asyncio.Event()
        self._task = asyncio.create_task(
            self._loop(), name="reactivation_loop",
        )

    def _effective_max_per_tick(self) -> int:
        """W2-D4.6：启动后宽限期内强制收窄到 first_run_max_per_tick。"""
        if self._first_run_grace_sec <= 0:
            return self._max_per_tick
        if time.time() - self._started_ts < self._first_run_grace_sec:
            return min(self._max_per_tick, self._first_run_max_per_tick)
        return self._max_per_tick

    async def stop(self) -> None:
        if self._stop_evt:
            self._stop_evt.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                pass

    async def _loop(self) -> None:
        try:
            while not (self._stop_evt and self._stop_evt.is_set()):
                try:
                    sent = await self.run_once()
                    if sent:
                        logger.info(
                            "[reactivation_loop] tick: scheduled %d reactivations",
                            sent,
                        )
                except Exception:
                    logger.exception("reactivation_loop run_once 异常")
                # 等下一轮（可被 stop 信号中断）
                try:
                    if self._stop_evt:
                        await asyncio.wait_for(
                            self._stop_evt.wait(), timeout=self._interval,
                        )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            logger.info("reactivation_loop cancelled")
            raise
        except Exception:
            logger.exception("reactivation_loop 退出")

    async def run_once(self) -> int:
        """一次调度：返回成功 enqueue_deferred 的条数。"""
        cands = self._scheduler.list_candidates()
        try:
            from src.monitoring.metrics_store import get_metrics_store
            get_metrics_store().set_reactivation_run(len(cands))
        except Exception:
            pass
        # ★ W2-D6.1：tick 顺手算 24h 回复率（廉价 SQL，不影响候选处理）
        try:
            if hasattr(self._store, "compute_reactivation_response_stats"):
                stats = self._store.compute_reactivation_response_stats(
                    window_sec=86400, response_window_sec=86400,
                )
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().set_reactivation_response_stats(stats)
        except Exception:
            logger.debug("compute_reactivation_response_stats 异常", exc_info=True)
        if not cands:
            return 0
        random.shuffle(cands)  # 不要总按 updated_at 顺序，错开发送时间
        scheduled = 0
        eff_cap = self._effective_max_per_tick()
        for c in cands[:eff_cap]:
            try:
                if await self._schedule_one(c):
                    scheduled += 1
            except Exception:
                logger.debug("schedule_one 异常 contact=%s",
                             c.contact_id, exc_info=True)
        return scheduled

    def _pick_identity(self, identities):
        """按 platform_priority 在该 contact 的 ChannelIdentity 里选一个渠道。

        返回 (channel, identity)；都没命中 → (None, None)。同渠道多身份取首个。
        """
        by_channel = {}
        for i in identities or []:
            ch = str(getattr(i, "channel", "") or "")
            if ch and ch not in by_channel:
                by_channel[ch] = i
        for ch in self._platform_priority:
            if ch in by_channel:
                return ch, by_channel[ch]
        return None, None

    async def _schedule_one(self, cand: ReactivationCandidate) -> bool:
        # 按平台优先级选渠道（默认 messenger；config 放宽后可跨平台）
        try:
            identities = self._store.list_channel_identities_of(cand.contact_id)
        except Exception:
            return False
        channel, ident = self._pick_identity(identities)
        if ident is None:
            return False  # 这个 contact 不在任何受支持平台上 → 跳过
        chat_name = (getattr(ident, "external_id", "") or "").strip() \
            or (getattr(ident, "display_name", "") or "").strip()
        account_id = str(getattr(ident, "account_id", "default") or "default")
        if not chat_name:
            return False
        platform_label = _PLATFORM_LABELS.get(channel, channel)

        # 拿 portrait + episodic
        contact = None
        try:
            contact = self._store.get_contact(cand.contact_id)
        except Exception:
            pass
        portrait_block = self._render_portrait(contact, cand)
        episodic_block = ""
        journey_obj = None
        if self._episodic_provider is not None:
            try:
                journey_obj = self._store.get_journey_by_contact(cand.contact_id)
                if journey_obj is not None:
                    episodic_block = (self._episodic_provider(journey_obj) or "").strip()
            except Exception:
                logger.debug("episodic_provider 异常", exc_info=True)
        if self._skip_if_no_episodic and not episodic_block:
            logger.info(
                "[reactivation] skip contact=%s 无 episodic memory（避免空话）",
                cand.contact_id,
            )
            try:
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().record_reactivation_skipped("no_episodic")
            except Exception:
                pass
            return False

        lang = (getattr(contact, "language_hint", "") or "ja").strip() or "ja"
        prompt = _REACTIVATION_PROMPT.format(
            ai_name=self._ai_name,
            platform_label=platform_label,
            silent_days=cand.silent_days,
            portrait_block=portrait_block or "(无更多信息)",
            episodic_block=episodic_block or "(无具体事)",
            lang=lang,
        )

        # 调 LLM
        try:
            reply = await self._ai.chat(prompt)
        except Exception:
            logger.warning("reactivation LLM 失败 contact=%s", cand.contact_id, exc_info=True)
            return False
        reply = (reply or "").strip()
        if not reply or len(reply) < 4:
            logger.info(
                "[reactivation] skip contact=%s LLM 返回过短: %r",
                cand.contact_id, reply,
            )
            return False
        # 防身份泄露的简单 check
        for bad in ("作为AI", "作为一个AI", "AI助手", "as an AI", "I'm an AI"):
            if bad.lower() in reply.lower():
                logger.warning(
                    "[reactivation] LLM 出戏 contact=%s 丢弃: %r",
                    cand.contact_id, reply[:100],
                )
                return False

        # ★ W2-D7.5：和最近 dislike 的话术相似度 check（重生成一次）
        try:
            from src.monitoring.metrics_store import get_metrics_store
            ms = get_metrics_store()
            is_sim, similar_to = ms.is_similar_to_disliked(reply, threshold=0.7)
            if is_sim:
                logger.info(
                    "[reactivation] reply 与黑名单相似（dislike）→ 重生成 contact=%s",
                    cand.contact_id,
                )
                # 追加 anti-similar hint 重生成
                prompt2 = (
                    prompt
                    + "\n\n注意：和这条话术风格雷同的版本之前被运营标记为不合适，"
                    + "请彻底换一种说法，不要复用以下结构或开头：\n"
                    + similar_to[:200]
                )
                try:
                    reply2 = (await self._ai.chat(prompt2) or "").strip()
                except Exception:
                    reply2 = ""
                if reply2 and len(reply2) >= 4:
                    is_sim2, _ = ms.is_similar_to_disliked(reply2, threshold=0.7)
                    if not is_sim2:
                        reply = reply2
                        logger.info(
                            "[reactivation] 重生成成功 contact=%s",
                            cand.contact_id,
                        )
                    else:
                        # 重生成仍相似 → 跳过
                        logger.info(
                            "[reactivation] skip contact=%s 重生成仍相似黑名单",
                            cand.contact_id,
                        )
                        try:
                            ms.record_reactivation_skipped("disliked_similarity_2x")
                        except Exception:
                            pass
                        return False
                else:
                    # 重生成失败 → 跳过
                    try:
                        ms.record_reactivation_skipped("disliked_similarity_regen_fail")
                    except Exception:
                        pass
                    return False
        except Exception:
            logger.debug("similarity check 异常", exc_info=True)

        # 计算何时发：未来 15 min - 4 h 随机；让 drain loop 真发
        delay = random.uniform(15 * 60, 4 * 3600)
        defer_until = time.time() + delay

        # 复用 journey_obj（前面取过），避免再查一次
        if journey_obj is None:
            try:
                journey_obj = self._store.get_journey_by_contact(cand.contact_id)
            except Exception:
                journey_obj = None
        journey_id = getattr(journey_obj, "journey_id", "") if journey_obj else ""

        # ★ W2-D4.6：dry_run 模式 — 只生成 + log，不真 enqueue 也不 mark_sent
        if self._dry_run:
            logger.info(
                "[reactivation DRY] contact=%s chat=%s defer=+%dmin reply=%r",
                cand.contact_id, chat_name, int(delay / 60), reply[:120],
            )
            try:
                from src.monitoring.metrics_store import get_metrics_store
                # W2-D5.1：把样本存进 metrics（dashboard 可调）
                get_metrics_store().record_reactivation_dry_run(sample={
                    "contact_id": cand.contact_id,
                    "chat_name": chat_name,
                    "platform": channel,
                    "account_id": account_id,
                    "reply_text": reply,
                    "silent_days": cand.silent_days,
                    "intimacy": cand.intimacy_score,
                    "stage": cand.funnel_stage,
                    "lang": lang,
                    "would_send_in_min": int(delay / 60),
                })
            except Exception:
                pass
            return True

        try:
            row_id = await self._send(
                channel, account_id, chat_name, reply, defer_until,
                f"reactivation:silent_{int(cand.silent_days)}d",
                86400.0,  # staleness 24h
                {
                    "reactivation": True,
                    "contact_id": cand.contact_id,
                    "journey_id": journey_id,
                    "intimacy_score": cand.intimacy_score,
                },
            )
        except Exception:
            logger.warning(
                "reactivation send_callback 异常 contact=%s",
                cand.contact_id, exc_info=True,
            )
            return False
        if not row_id or row_id <= 0:
            try:
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().record_reactivation_failed("send_callback_returned_0")
            except Exception:
                pass
            return False

        try:
            from src.monitoring.metrics_store import get_metrics_store
            get_metrics_store().record_reactivation_scheduled(cand.contact_id)
        except Exception:
            pass

        # 落 reactivation_sent 事件防重复
        try:
            self._scheduler.mark_sent(
                journey_id=journey_id,
                note=f"reactivation_loop deferred_id={row_id}",
                trace_id=f"react-{row_id}",
            )
        except Exception:
            logger.debug("mark_sent 异常", exc_info=True)

        logger.info(
            "[reactivation] scheduled contact=%s chat=%s defer=+%dmin reply=%r",
            cand.contact_id, chat_name, int(delay / 60), reply[:80],
        )
        return True

    def _render_portrait(self, contact, cand: ReactivationCandidate) -> str:
        """把 contact + cand 简单渲染成一段画像注入 prompt。"""
        lines = []
        if contact:
            n = (getattr(contact, "primary_name", "") or "").strip()
            if n:
                lines.append(f"- 称呼：{n}")
            tz = (getattr(contact, "timezone_hint", "") or "").strip()
            if tz:
                lines.append(f"- 时区：{tz}")
        lines.append(f"- 沉默天数：{cand.silent_days:.1f} 天")
        lines.append(f"- 亲密度：{cand.intimacy_score:.0f}/100")
        lines.append(f"- 关系阶段：{cand.funnel_stage}")
        return "\n".join(lines)
