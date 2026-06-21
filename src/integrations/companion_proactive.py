"""P2 主动话题调度：沉默检测 + 冷却 → 选 P1 话题 → 经回调发出。

陪伴型 AI 的核心差异点：用户久未说话时，**主动**自然回到对方在意的事
（"上次你说在备考，后来怎么样？"），而不是被动等消息。本模块负责"何时发"，
话题种子由 P1 ``select_proactive_topic`` 产出（经 ``opener_fn`` 注入），真实文案
由回复生成层产出（经 ``send_fn`` 注入）。

设计（与 reactivation_loop / care_dispatcher 同范式）：
- ``plan_proactive_sends`` 是**确定性纯函数**：给定会话快照 + 冷却表 + 时钟，
  决定该给哪些会话主动开场、用什么指令。零 IO、可单测。
- ``CompanionProactiveLoop`` 是**薄异步循环**：now/sleep/send/cooldown 全可注入，
  单测用假时钟 + 假发送确定性驱动，无需真账号、无需长 sleep。
- **默认关**：上层 ``companion.proactive_topic.enabled`` 控；本模块只是机制，不自启。

护栏：
- 沉默不足不打扰（min_silent_hours）。
- 冷却：同一会话两次主动开场至少间隔 cooldown_hours，避免骚扰。
- 安静时段（quiet_start..quiet_end，默认 23–8）不发，错过则下个 tick 再看。
- **只在我方说完后冷场才主动**：若最后一条是对方消息（last_direction=="in"），
  那是"我欠回复"（SLA 范畴），不在此主动开场，交给坐席/自动回复处理。
- 归档会话不打扰。
- **与 proactive_care(Phase O) 去重**：若某会话已被"记忆驱动关怀"队列排了待发项
  （has_pending_care 返回 True），则本沉默话题让路——避免同一个人被两套主动系统
  同时打扰（care 引用具体约定/事件，优先级更高、更不像"机器到点打卡"）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def _in_quiet_hours(hour: int, start: int, end: int) -> bool:
    """当前小时是否落在安静时段。start>end 表示跨午夜（如 23..8）。"""
    start %= 24
    end %= 24
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def plan_proactive_sends(
    conversations: List[Dict[str, Any]],
    *,
    cooldown_map: Dict[str, float],
    opener_fn: Callable[..., Dict[str, Any]],
    now: Optional[float] = None,
    min_silent_hours: float = 24.0,
    cooldown_hours: float = 72.0,
    max_per_tick: int = 3,
    quiet_start_hour: float = 23.0,
    quiet_end_hour: float = 8.0,
    has_pending_care: Optional[Callable[[str], bool]] = None,
    on_crisis_block: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> List[Dict[str, Any]]:
    """决定本轮该主动开场的会话清单（确定性纯函数）。

    Args:
        conversations: 会话快照列表，每项含 ``conversation_id/platform/account_id/
            chat_key/last_ts/last_direction/archived`` 及给 opener 的 ``memory_key/
            stage/intimacy``。
        cooldown_map: ``{conversation_id: 上次主动开场时间戳}``。
        opener_fn: ``opener_fn(memory_key=, silent_hours=, stage=, intimacy=) ->
            {mode, directive, fact, context_facts, ...}``（即 build_proactive_opener）。
        has_pending_care: 可选谓词 ``(conversation_id) -> bool``——返回 True 表示该会话
            已被 proactive_care(Phase O) 排了待发关怀，本主动话题让路跳过（去重）。
        on_crisis_block: 可选回调 ``(conversation_snapshot) -> None``——当 opener 因近期
            severe 危机被护栏拦下（``blocked == "crisis_severe"``）时调用，让派发层把该
            用户排进 care 队列（危机关怀升级：把"静默"变"接住"）。IO 留在回调里、纯函数
            不落库；失败吞掉、绝不影响其余会话的计划。
        now: 注入"现在"（测试用）。

    Returns:
        计划列表 ``[{conversation_id, platform, account_id, chat_key, mode,
        directive, fact, context_facts, silent_hours}]``，按沉默时长降序，
        截断到 max_per_tick。
    """
    now = now if now is not None else time.time()
    local_hour = time.localtime(now).tm_hour
    if _in_quiet_hours(local_hour, int(quiet_start_hour), int(quiet_end_hour)):
        return []  # 安静时段不打扰

    plans: List[Dict[str, Any]] = []
    for c in conversations or []:
        if not isinstance(c, dict) or c.get("archived"):
            continue
        cid = str(c.get("conversation_id") or "")
        if not cid:
            continue
        # 对方最后发言 = 我欠回复，不在此主动开场
        if str(c.get("last_direction") or "") == "in":
            continue
        # 与 proactive_care 去重：已排关怀的会话让路（care 引用具体约定，优先）
        if has_pending_care is not None:
            try:
                if has_pending_care(cid):
                    continue
            except Exception:
                logger.debug("[proactive] has_pending_care 失败 cid=%s", cid, exc_info=True)
        try:
            last_ts = float(c.get("last_ts") or 0)
        except (TypeError, ValueError):
            last_ts = 0.0
        if last_ts <= 0:
            continue
        silent_hours = (now - last_ts) / 3600.0
        if silent_hours < float(min_silent_hours):
            continue
        try:
            last_pro = float(cooldown_map.get(cid, 0) or 0)
        except (TypeError, ValueError):
            last_pro = 0.0
        if last_pro and (now - last_pro) < float(cooldown_hours) * 3600.0:
            continue
        try:
            opener = opener_fn(
                memory_key=str(c.get("memory_key") or ""),
                silent_hours=silent_hours,
                stage=str(c.get("stage") or ""),
                intimacy=float(c.get("intimacy") or 0.0),
            ) or {}
        except Exception:
            logger.debug("[proactive] opener_fn 失败 cid=%s", cid, exc_info=True)
            continue
        # 危机关怀升级：被情绪护栏拦下的 severe 会话 → 排进 care 队列（best-effort），
        # 再正常跳过（mode 为空，不会作普通主动文案发出）。
        if str(opener.get("blocked") or "") == "crisis_severe" and on_crisis_block is not None:
            try:
                on_crisis_block(c)
            except Exception:
                logger.debug("[proactive] on_crisis_block 失败 cid=%s", cid, exc_info=True)
        mode = str(opener.get("mode") or "")
        directive = str(opener.get("directive") or "")
        if not mode or not directive:
            continue
        plans.append({
            "conversation_id": cid,
            "platform": str(c.get("platform") or ""),
            "account_id": str(c.get("account_id") or ""),
            "chat_key": str(c.get("chat_key") or ""),
            "mode": mode,
            "directive": directive,
            "fact": str(opener.get("fact") or ""),
            "context_facts": [
                str(f).strip()
                for f in (opener.get("context_facts") or [])
                if str(f).strip()
            ],
            "silent_hours": round(silent_hours, 1),
        })

    plans.sort(key=lambda p: p["silent_hours"], reverse=True)
    return plans[: max(0, int(max_per_tick))]


class JsonCooldownStore:
    """极简文件持久冷却表（best-effort）：``{conversation_id: 上次主动开场 ts}``。"""

    def __init__(self, path: Any) -> None:
        self.path = Path(path)
        self._data: Dict[str, float] = {}
        try:
            if self.path.exists():
                self._data = {
                    str(k): float(v)
                    for k, v in (json.loads(self.path.read_text("utf-8")) or {}).items()
                }
        except Exception:
            self._data = {}

    def snapshot(self) -> Dict[str, float]:
        return dict(self._data)

    def mark(self, conversation_id: str, ts: float) -> None:
        self._data[str(conversation_id)] = float(ts)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, ensure_ascii=False), "utf-8")
        except Exception:
            logger.debug("[proactive] 冷却表落盘失败", exc_info=True)


class CompanionProactiveLoop:
    """陪伴主动话题派发循环（薄监督；机制与时钟解耦，可单测）。"""

    def __init__(
        self,
        *,
        conversations_provider: Callable[[], List[Dict[str, Any]]],
        opener_fn: Callable[..., Dict[str, Any]],
        send_fn: Callable[[Dict[str, Any]], Awaitable[bool]],
        cooldown_store: Any,
        interval_sec: float = 900.0,
        min_silent_hours: float = 24.0,
        cooldown_hours: float = 72.0,
        max_per_tick: int = 3,
        quiet_start_hour: float = 23.0,
        quiet_end_hour: float = 8.0,
        dry_run: bool = False,
        has_pending_care: Optional[Callable[[str], bool]] = None,
        on_crisis_block: Optional[Callable[[Dict[str, Any]], None]] = None,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._conversations_provider = conversations_provider
        self._opener_fn = opener_fn
        self._send_fn = send_fn
        self._cooldown = cooldown_store
        self._has_pending_care = has_pending_care
        self._on_crisis_block = on_crisis_block
        self._interval = float(interval_sec)
        self._min_silent_hours = float(min_silent_hours)
        self._cooldown_hours = float(cooldown_hours)
        self._max_per_tick = int(max_per_tick)
        self._quiet_start = float(quiet_start_hour)
        self._quiet_end = float(quiet_end_hour)
        self._dry_run = bool(dry_run)
        self._now = now
        self._sleep = sleep
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def run_once(self) -> Dict[str, int]:
        """一次幂等派发步：扫描 → 计划 → 发送 → 记冷却。返回 {planned, sent}。"""
        try:
            convs = list(self._conversations_provider() or [])
        except Exception:
            logger.debug("[proactive] 会话快照获取失败", exc_info=True)
            return {"planned": 0, "sent": 0}
        plans = plan_proactive_sends(
            convs,
            cooldown_map=(self._cooldown.snapshot() if self._cooldown else {}),
            opener_fn=self._opener_fn,
            now=self._now(),
            min_silent_hours=self._min_silent_hours,
            cooldown_hours=self._cooldown_hours,
            max_per_tick=self._max_per_tick,
            quiet_start_hour=self._quiet_start,
            quiet_end_hour=self._quiet_end,
            has_pending_care=self._has_pending_care,
            on_crisis_block=self._on_crisis_block,
        )
        sent = 0
        for p in plans:
            ok = False
            try:
                ok = True if self._dry_run else bool(await self._send_fn(p))
            except Exception:
                logger.debug("[proactive] send_fn 失败 cid=%s",
                             p.get("conversation_id"), exc_info=True)
                ok = False
            if ok:
                if self._cooldown:
                    self._cooldown.mark(p["conversation_id"], self._now())
                sent += 1
        if plans:
            logger.info("[proactive] tick: planned=%d sent=%d dry_run=%s",
                        len(plans), sent, self._dry_run)
        return {"planned": len(plans), "sent": sent}

    async def _loop(self) -> None:
        try:
            while self._running:
                try:
                    await self.run_once()
                except Exception:
                    logger.exception("[proactive] run_once 异常")
                await self._sleep(self._interval)
        except asyncio.CancelledError:
            logger.info("[proactive] loop cancelled")
        except Exception:
            logger.exception("[proactive] loop 退出")

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="companion_proactive_loop")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None


__all__ = [
    "plan_proactive_sends",
    "JsonCooldownStore",
    "CompanionProactiveLoop",
]
