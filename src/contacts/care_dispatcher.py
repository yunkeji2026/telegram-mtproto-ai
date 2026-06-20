"""Phase O3：主动关怀到期派发器。

读 `CareScheduleStore.list_due(now)` → 对每条拉上下文 + topic 构造 LLM prompt（强制引用
具体事，无上下文就 skip 不发空话）→ 经**注入的 send_callback（reactivation 同款 deferred
队列）**发送（自动享 gate / pacing / quiet_hours / kill-switch / staleness）→ 成功 mark_sent。

设计（与 reactivation_loop 同范式、可注入、可单测）：
- 不做平台身份查找——`care_schedule` 入库时已存 platform/account_id/chat_key，直接用。
- O3 改进①：派发前可选 `already_discussed(contact_key, topic)` 复查，近期已聊过该事 → skip
  （`mark_skipped`，防"机器到点打卡"）。
- O3 改进②：发送时刻命中 quiet_hours → **顺延到安静时段结束**（而非跳过，关怀该送只是择时）。
- dry_run：只生成 + log，不真 enqueue、不 mark_sent（灰度第一阶段看 LLM 质量）。

默认关：上层 `companion.proactive_care.enabled` 控；本类只是机制，不自启。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from src.contacts.care_schedule import CareScheduleStore

logger = logging.getLogger(__name__)

# send_callback：(channel, account_id, chat_name, reply, defer_until, reason, staleness, extra) -> row_id
SendCallback = Callable[[str, str, str, str, float, str, float, dict], Awaitable[int]]
# context_provider：(contact_key) -> 最近对话/episodic 要点文本（供 prompt 引用）
ContextProvider = Callable[[str], str]
# already_discussed：(contact_key, topic) -> bool（近期是否已主动聊过该事）
AlreadyDiscussed = Callable[[str, str], bool]
# proactive_allowed：(contact_key) -> bool（变现配额门控；False=免费用户超额不主动）
ProactiveAllowed = Callable[[str], bool]

_IDENTITY_LEAK = ("作为AI", "作为一个AI", "AI助手", "as an AI", "i'm an ai", "i am an ai")

_CARE_PROMPT = """你是「{ai_name}」，正在和对方私聊。对方之前提到过 **{topic}**（{when_desc}），
现在是主动关心这件事的好时机。

【对方当时的原话】
{source_text}

【你们最近的对话要点（可自然引用）】
{context_block}

请用对方习惯的语言（{lang}）发**一条**主动关心的消息：
- 像朋友一直惦记着这件事（"你之前说的{topic}…怎么样啦？"）
- **紧扣「{topic}」这件具体事**，不要泛泛的"在吗 / 最近好吗"
- 短小亲切 1-2 句，可含一个 emoji
- 不要说"我是AI"或身份相关的话，不发链接、不索要联系方式

直接输出消息文本，不要前后缀、不要解释。"""


def shift_out_of_quiet_hours(ts: float, *, start_hour: float, end_hour: float) -> float:
    """命中安静时段则顺延到其结束时刻；否则原样返回。start==end 表示无安静窗。"""
    if start_hour == end_hour:
        return ts
    dt = datetime.fromtimestamp(ts)
    h = dt.hour + dt.minute / 60.0
    overnight = start_hour > end_hour
    in_quiet = (
        (not overnight and start_hour <= h < end_hour)
        or (overnight and (h >= start_hour or h < end_hour))
    )
    if not in_quiet:
        return ts
    target = dt.replace(hour=int(end_hour) % 24, minute=0, second=0, microsecond=0)
    if overnight and h >= start_hour:
        target = target + timedelta(days=1)  # 深夜 → 次日早晨结束点
    if target.timestamp() <= ts:
        target = target + timedelta(days=1)
    return target.timestamp()


def _when_desc(event_at: float, now: float) -> str:
    """事件相对 now 的口语化描述（供 prompt：今天/昨天/这两天/即将）。"""
    try:
        ev_day = datetime.fromtimestamp(event_at).date()
        now_day = datetime.fromtimestamp(now).date()
    except Exception:
        return "最近"
    delta = (ev_day - now_day).days
    if delta == 0:
        return "就在今天"
    if delta == -1:
        return "昨天"
    if delta < -1:
        return "前几天"
    if delta == 1:
        return "明天"
    return "这几天"


class CareDispatcher:
    def __init__(
        self,
        *,
        store: CareScheduleStore,
        ai_client: Any,
        send_callback: SendCallback,
        context_provider: Optional[ContextProvider] = None,
        already_discussed: Optional[AlreadyDiscussed] = None,
        proactive_allowed: Optional[ProactiveAllowed] = None,
        ai_name: str = "她",
        default_lang: str = "zh",
        max_per_tick: int = 3,
        interval_sec: float = 600.0,
        skip_if_no_context: bool = True,
        quiet_start_hour: float = 23.0,
        quiet_end_hour: float = 8.0,
        send_jitter_sec: tuple = (60.0, 1200.0),
        staleness_sec: float = 86400.0,
        dry_run: bool = False,
        expire_grace_days: float = 1.0,
    ) -> None:
        self._store = store
        self._ai = ai_client
        self._send = send_callback
        self._context_provider = context_provider
        self._already_discussed = already_discussed
        self._proactive_allowed = proactive_allowed
        self._ai_name = ai_name or "她"
        self._default_lang = default_lang or "zh"
        self._max_per_tick = max(1, int(max_per_tick))
        self._interval = max(60.0, float(interval_sec))
        self._skip_if_no_context = bool(skip_if_no_context)
        self._quiet_start = float(quiet_start_hour)
        self._quiet_end = float(quiet_end_hour)
        self._jitter = send_jitter_sec
        self._staleness = float(staleness_sec)
        self._dry_run = bool(dry_run)
        self._expire_grace_days = float(expire_grace_days)
        self._stop_evt: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_evt = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="care_dispatcher")

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
                    n = await self.run_once()
                    if n:
                        logger.info("[care_dispatcher] tick: scheduled %d care msgs", n)
                except Exception:
                    logger.exception("care_dispatcher run_once 异常")
                try:
                    if self._stop_evt:
                        await asyncio.wait_for(self._stop_evt.wait(), timeout=self._interval)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("care_dispatcher 退出")

    async def run_once(self, *, now: Optional[float] = None) -> int:
        """一次派发：返回成功 enqueue（或 dry_run 计数）的条数。"""
        n = float(now if now is not None else time.time())
        # 每轮先清理逾期太久仍 pending 的待办（错过关怀时机不补发），best-effort
        try:
            expired = self._store.expire_overdue(now=n, grace_days=self._expire_grace_days)
            if expired:
                logger.info("[care_dispatcher] 清理逾期待办 %d 条", expired)
        except Exception:
            logger.debug("care_dispatcher expire_overdue 异常", exc_info=True)
        due = self._store.list_due(now=n, limit=self._max_per_tick * 4)
        if not due:
            return 0
        scheduled = 0
        for item in due:
            if scheduled >= self._max_per_tick:
                break
            try:
                if await self._dispatch_one(item, n):
                    scheduled += 1
            except Exception:
                logger.debug("care dispatch_one 异常 id=%s", item.get("id"), exc_info=True)
        return scheduled

    def _mark_skipped(self, sid: int, reason: str) -> None:
        """store.mark_skipped + 记 metrics skip 原因（O·P 联动质量看板用）。"""
        self._store.mark_skipped(sid, note=reason)
        try:
            from src.monitoring.metrics_store import get_metrics_store
            get_metrics_store().record_care_skipped(reason)
        except Exception:
            pass

    async def _dispatch_one(self, item: dict, now: float) -> bool:
        sid = int(item["id"])
        contact_key = str(item.get("contact_key") or "")
        topic = str(item.get("topic") or "").strip()
        chat_key = str(item.get("chat_key") or "")
        platform = str(item.get("platform") or "")
        account_id = str(item.get("account_id") or "default") or "default"

        if not chat_key or not platform:
            self._mark_skipped(sid, "missing platform/chat_key")
            return False

        # K2b：变现配额门控——免费用户主动关怀超额 → 跳过（gate 关时回调返 True 不拦）。
        # 放在 LLM 之前，超额时不白耗 token。
        if self._proactive_allowed is not None:
            try:
                if not self._proactive_allowed(contact_key):
                    self._mark_skipped(sid, "paywall_quota")
                    return False
            except Exception:
                logger.debug("proactive_allowed 异常（忽略放行）", exc_info=True)

        # O3 改进①：近期已主动聊过该事 → 跳过（防到点打卡）
        if self._already_discussed is not None:
            try:
                if self._already_discussed(contact_key, topic):
                    self._mark_skipped(sid, "already_discussed")
                    return False
            except Exception:
                logger.debug("already_discussed 异常（忽略）", exc_info=True)

        context_block = ""
        if self._context_provider is not None:
            try:
                context_block = (self._context_provider(contact_key) or "").strip()
            except Exception:
                logger.debug("context_provider 异常", exc_info=True)
        if self._skip_if_no_context and not context_block:
            self._mark_skipped(sid, "no_context")
            return False

        prompt = _CARE_PROMPT.format(
            ai_name=self._ai_name,
            topic=topic or "那件事",
            when_desc=_when_desc(float(item.get("event_at") or now), now),
            source_text=(str(item.get("source_text") or "") or "(无)")[:200],
            context_block=context_block or "(无具体要点)",
            lang=self._default_lang,
        )
        try:
            reply = (await self._ai.chat(prompt) or "").strip()
        except Exception:
            logger.warning("care LLM 失败 id=%s", sid, exc_info=True)
            return False  # 留 pending，下个 tick 重试
        if not reply or len(reply) < 4:
            self._mark_skipped(sid, "llm_empty")
            return False
        low = reply.lower()
        if any(b.lower() in low for b in _IDENTITY_LEAK):
            self._mark_skipped(sid, "identity_leak")
            return False

        # Phase O 质量闭环：与运营 dislike 黑名单话术相似 → 重生成一次，仍相似则跳过
        # （复用 reactivation 同一黑名单：被标记的雷同话术在 care 里同样该避免）
        reply = await self._avoid_disliked(prompt, reply, sid)
        if not reply:
            return False

        # O3 改进②：发送时刻命中 quiet_hours → 顺延到结束（而非跳过）
        defer_until = now + random.uniform(self._jitter[0], self._jitter[1])
        defer_until = shift_out_of_quiet_hours(
            defer_until, start_hour=self._quiet_start, end_hour=self._quiet_end)

        if self._dry_run:
            logger.info("[care DRY] id=%s contact=%s topic=%s reply=%r",
                        sid, contact_key, topic, reply[:120])
            try:
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().record_care_dry_run(sample={
                    "care_id": sid,
                    "contact_key": contact_key,
                    "topic": topic,
                    "platform": platform,
                    "account_id": account_id,
                    "chat_key": chat_key,
                    "reply_text": reply,
                    "lang": self._default_lang,
                    "would_send_in_min": int(max(0.0, defer_until - now) / 60),
                })
            except Exception:
                logger.debug("record_care_dry_run 异常", exc_info=True)
            self._store.mark_sent(sid, note="dry_run")
            return True

        try:
            row_id = await self._send(
                platform, account_id, chat_key, reply, defer_until,
                f"care:{topic[:24]}", self._staleness,
                {"care": True, "care_id": sid, "contact_key": contact_key, "topic": topic},
            )
        except Exception:
            logger.warning("care send_callback 失败 id=%s", sid, exc_info=True)
            return False  # 留 pending 重试
        if not row_id:
            return False  # enqueue 失败（如 gate 拦）→ 留 pending
        self._store.mark_sent(sid, note=f"deferred:{int(row_id)}")
        return True

    async def _avoid_disliked(self, prompt: str, reply: str, sid: int) -> str:
        """Phase O 质量闭环：reply 命中 dislike 黑名单 → 重生成一次。

        返回最终可用 reply；若重生成仍相似/失败则 mark_skipped 并返回空串。
        复用 reactivation 的同一黑名单（被运营标记的雷同话术应全局避免）。
        """
        try:
            from src.monitoring.metrics_store import get_metrics_store
            ms = get_metrics_store()
        except Exception:
            return reply  # metrics 不可用不阻断派发
        try:
            is_sim, similar_to = ms.is_similar_to_disliked(reply, threshold=0.7)
        except Exception:
            return reply
        if not is_sim:
            return reply
        logger.info("[care] reply 与 dislike 黑名单相似 → 重生成 id=%s", sid)
        prompt2 = (
            prompt
            + "\n\n注意：和这条话术风格雷同的版本之前被运营标记为不合适，"
            + "请彻底换一种说法，不要复用以下结构或开头：\n"
            + (similar_to or "")[:200]
        )
        try:
            reply2 = (await self._ai.chat(prompt2) or "").strip()
        except Exception:
            reply2 = ""
        if reply2 and len(reply2) >= 4:
            try:
                is_sim2, _ = ms.is_similar_to_disliked(reply2, threshold=0.7)
            except Exception:
                is_sim2 = False
            low2 = reply2.lower()
            if not is_sim2 and not any(b.lower() in low2 for b in _IDENTITY_LEAK):
                logger.info("[care] 重生成成功 id=%s", sid)
                return reply2
        self._mark_skipped(sid, "disliked_similarity")
        return ""


__all__ = ["CareDispatcher", "shift_out_of_quiet_hours"]
