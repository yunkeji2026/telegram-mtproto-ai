"""WhatsApp RPA 后台服务 — 主进程托管的长期运行循环。

职责：
- 按配置自动拉起轮询（可开关）
- 暴露 start/stop/pause/resume/trigger_once/status 控制面
- 复用 SkillManager / AIClient（共享人设/KB）
- 自适应轮询：有消息则快轮，连续空轮则指数退避
- daily_cap：UTC 0 点重置计数
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.integrations.line_rpa.adb_helpers import get_device_lock
from src.integrations.whatsapp_rpa.runner import WhatsAppRpaRunner
from src.integrations.whatsapp_rpa.state_store import (
    WaRpaStateStore,
    default_state_db_path,
)
from src.integrations.whatsapp_rpa.proactive_templates import (
    ProactiveTemplatePool,
    create_pool,
)

logger = logging.getLogger(__name__)


class WhatsAppRpaService:
    """长期后台服务；由 main.py 生命周期管理。"""

    def __init__(
        self,
        *,
        config_manager: Any,
        skill_manager: Any,
        wa_cfg: Optional[Dict[str, Any]] = None,
        account_id: Optional[str] = None,
    ) -> None:
        self._cm = config_manager
        self._sm = skill_manager
        self._cfg: Dict[str, Any] = dict(wa_cfg or {})
        self.account_id: str = account_id or self._cfg.get("account_id") or "default"
        self._merged_cfg: Dict[str, Any] = self._merged()

        if self.account_id and self.account_id != "default":
            db_path = Path(self._cm.config_path).parent / f"wa_rpa_state_{self.account_id}.db"
        else:
            db_path = default_state_db_path(self._cm.config_path)
        self._state = WaRpaStateStore(db_path)
        self._runner = WhatsAppRpaRunner(
            config_manager=config_manager,
            skill_manager=skill_manager,
            wa_cfg=self._merged_cfg,
            state_store=self._state,
        )

        self._task: Optional[asyncio.Task] = None
        self._stop_evt = asyncio.Event()
        self._trigger_evt = asyncio.Event()
        self._pause_until: float = 0.0
        self._started_at: float = 0.0
        self._last_run: Dict[str, Any] = {}
        self._consecutive_fail: int = 0
        self._last_had_peer_ts: float = 0.0
        self._last_tick_ts: float = 0.0
        self._next_auto_accept_ts: float = 0.0
        self._last_pending_ttl_check: float = 0.0  # P12-A
        # P15-b: 主动续聊计数与冷却（内存级，避免 DB migration）
        self._proactive_day: int = int(time.time() // 86400)
        self._proactive_sent_today: int = 0
        self._proactive_last_ts: Dict[str, float] = {}  # chat_key -> ts
        # P15-e: 递进式防骚扰 - 首次命中静默，二次命中黑名单
        self._stop_contact_first_hit: Dict[str, float] = {}  # chat_key -> first_hit_ts
        # P15-g: 主动续聊话题模板池（多样化 + A/B）
        _tpl_cfg = self._merged_cfg.get("proactive_templates") or {}
        self._template_pool: ProactiveTemplatePool = create_pool(_tpl_cfg)
        # P15-h: 加载历史模板回复数据（用于加权选择）
        self._load_template_history()

    # ── 默认配置 ──────────────────────────────────────────────────────────

    def _defaults(self) -> Dict[str, Any]:
        return {
            "enabled": False,
            "wa_package": "com.whatsapp",
            "use_business_app": False,
            "dump_remote_path": "/sdcard/wa_rpa_dump.xml",
            "default_reply_lang": "zh",
            "daily_cap": 0,
            "reply_mode": "auto",
            # P15-d: 停止联系保护（关键词→静默/黑名单）
            "stop_contact_quiet_minutes": 1440,
            "stop_contact_blacklist": True,
            "stop_contact_keywords": [
                "stop", "unsubscribe", "do not contact",
                "别联系", "停止联系", "不要再发",
            ],
            # P15-e: 递进式防骚扰 - 首次命中静默，二次命中黑名单
            "stop_contact_escalation_hours": 72,  # 首次命中后多少小时内二次命中才升黑名单
            # P15-f: 轻量意图检测器配置（替代纯关键词匹配）
            "stop_contact_strong_threshold": 0.85,
            "stop_contact_weak_threshold": 0.70,
            "stop_contact_enable_negative_check": True,
            # P15-b: 主动续聊（沉默唤醒）
            "proactive": {
                "enabled": False,
                "silence_minutes": 60,           # 距上次 peer 消息多久触发
                "per_chat_cooldown_minutes": 240, # 同一联系人冷却
                "daily_cap": 20,                  # 每账号每日上限（续聊）
                "max_per_tick": 1,                # 单轮最多触发数
                "window_start": "08:30",        # 本地时间窗
                "window_end": "22:30",
            },
            # P15-g: 主动续聊话题模板池（多样化 + A/B）
            # P15-h: 默认使用加权选择策略（根据使用频率自动优化）
            "proactive_templates": {
                "rotation_strategy": "weighted",  # round_robin / random / weighted
                "ab_test_enabled": True,
                "weighted_min_weight": 1.0,       # 基础权重
                "weighted_sent_boost": 0.2,     # 每次使用增加的权重
            },
            "auto_accept": {
                "enabled": False,
                "max_per_run": 5,
                "check_interval_sec": 120.0,
            },
            "after_launch_sleep_sec": 1.5,
            "service": {
                "interval_sec": 15.0,
                "fast_interval_sec": 4.0,
                "slow_interval_sec": 30.0,
                "slow_after_empty": 6,
                "backoff_max_sec": 120.0,
            },
            "human_pacing": {
                "enabled": True,
                "split_strategy": "sentence",
                "split_max_chars": 80,
                "split_max_parts": 5,
                "read_pause_ms": [800, 2000],
                "inter_msg_ms": [700, 1800],
            },
            # P15-j: 表情回复控制默认配置
            "emoticons": {
                "naturalization": {
                    "enabled": True,
                    "skip_emoticon_pass_probability": 0.32,
                    "ignore_context_suggestions_probability": 0.28,
                    "max_consecutive_decorated": 2,
                    "forbidden_emoticons": ["👉", "📝"],
                },
            },
        }

    def _merged(self) -> Dict[str, Any]:
        import copy
        d = self._defaults()
        for k, v in self._cfg.items():
            if isinstance(v, dict) and isinstance(d.get(k), dict):
                merged_sub = dict(d[k])
                merged_sub.update(v)
                d[k] = merged_sub
            else:
                d[k] = v
        return d

    def reconfigure(self, new_cfg: Dict[str, Any]) -> None:
        self._cfg = dict(new_cfg)
        self._merged_cfg = self._merged()
        self._runner.reconfigure(self._merged_cfg)

    def effective_config(self) -> Dict[str, Any]:
        return dict(self._merged_cfg)

    def set_contact_hooks(self, hooks: Any) -> None:
        """main.py 在 contacts 子系统 bootstrap 后调用，把 ContactHooks 注入 runner。"""
        self._runner.set_contact_hooks(hooks)

    # ── 启停控制 ──────────────────────────────────────────────────────────

    async def start(self) -> bool:
        if self._task and not self._task.done():
            return False
        if not self._merged_cfg.get("enabled"):
            logger.info("WhatsApp RPA disabled（enabled=false），跳过启动")
            return False
        svc_cfg = self._merged_cfg.get("service") or {}
        if not svc_cfg.get("autostart", True):
            logger.info("WhatsApp RPA autostart=false，等待外部触发")
            return False
        return await self._do_start()

    async def force_start(self) -> bool:
        """Web 控制面板调用：绕过 autostart 检查，只要 enabled 就拉起 loop。"""
        if self._task and not self._task.done():
            return True  # 已在运行
        if not self._merged_cfg.get("enabled"):
            logger.info("WhatsApp RPA disabled（enabled=false），跳过 force_start")
            return False
        return await self._do_start()

    async def _do_start(self) -> bool:
        self._stop_evt.clear()
        self._trigger_evt.clear()
        self._started_at = time.time()
        self._task = asyncio.create_task(
            self._loop(), name="whatsapp_rpa_service_loop"
        )
        logger.info("WhatsAppRpaService 已启动")
        return True

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._task and not self._task.done():
            self._task.cancel()

    def pause_for(self, seconds: float) -> None:
        self._pause_until = time.time() + max(0.0, seconds)

    def resume(self) -> None:
        self._pause_until = 0.0

    def trigger_once(self) -> None:
        self._trigger_evt.set()

    @property
    def is_running(self) -> bool:
        return bool(self._task and not self._task.done())

    # ── 主循环 ────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        svc_cfg = self._merged_cfg.get("service") or {}
        empty_streak = 0

        while not self._stop_evt.is_set():
            # 暂停判断
            pause_rem = self._pause_until - time.time()
            if pause_rem > 0:
                try:
                    await asyncio.wait_for(self._stop_evt.wait(), timeout=pause_rem)
                except asyncio.TimeoutError:
                    pass
                if self._stop_evt.is_set():
                    break
                continue

            # daily_cap 守门
            _cap = int(self._merged_cfg.get("daily_cap") or 0)
            if _cap > 0:
                _stats = self._state.run_stats(24.0)
                _today_sent = int((_stats or {}).get("sent") or 0)
                if _today_sent >= _cap:
                    _secs = 86400 - (int(time.time()) % 86400)
                    logger.info(
                        "WA daily_cap=%d reached (sent=%d), sleeping %ds",
                        _cap, _today_sent, _secs,
                    )
                    try:
                        await asyncio.wait_for(self._stop_evt.wait(), timeout=float(_secs))
                    except asyncio.TimeoutError:
                        pass
                    continue

            self._last_tick_ts = time.time()

            # 执行单轮（设备锁：同一物理设备上的 LINE/Messenger/WA 三者串行，防止互踢前台）
            _serial = self._merged_cfg.get("adb_serial") or ""
            try:
                async with get_device_lock(_serial):
                    result = await self._runner.run_once()
                self._last_run = result
                step = result.get("step", "")
                had_peer = bool(result.get("peer_text"))
                if had_peer:
                    self._last_had_peer_ts = time.time()
                    empty_streak = 0
                    self._consecutive_fail = 0
                elif step in {"no_unread", "already_replied", "no_peer_text"}:
                    empty_streak += 1
                if not result.get("ok") and step not in {
                    "no_unread", "already_replied", "no_peer_text",
                    "dry_run_done", "reply_mode_off", "pending_queued",
                    "empty_reply",
                }:
                    self._consecutive_fail += 1
                else:
                    self._consecutive_fail = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("WA run_once 异常: %s", e, exc_info=True)
                self._consecutive_fail += 1
                empty_streak += 1

            # 自动接受联系人申请
            try:
                await self._maybe_run_auto_accept()
            except Exception:
                logger.debug("wa auto_accept 失败", exc_info=True)

            # P15-b: 主动续聊（沉默唤醒）
            try:
                await self._maybe_enqueue_proactive()
            except Exception:
                logger.debug("wa proactive enqueue 失败", exc_info=True)

            # P12-A: pending 自动过期（每小时最多一次）
            try:
                _ttl = float(svc_cfg.get("pending_ttl_sec") or 86400)
                _now = time.time()
                if _ttl > 0 and _now - self._last_pending_ttl_check >= 3600:
                    self._last_pending_ttl_check = _now
                    _ss = getattr(self._runner, "_state_store", None) or getattr(self, "_state_store", None)
                    if _ss is not None:
                        _cancelled = _ss.cancel_pending_by_ttl(ttl_sec=_ttl)
                        if _cancelled:
                            logger.info("P12-A wa pending_ttl: %d 条已自动取消", len(_cancelled))
            except Exception:
                logger.debug("wa cancel_pending_by_ttl 失败", exc_info=True)

            # 自适应间隔
            interval = self._compute_next_interval(empty_streak, svc_cfg)
            try:
                tg_task = asyncio.create_task(self._trigger_evt.wait())
                st_task = asyncio.create_task(self._stop_evt.wait())
                done, pending_ = await asyncio.wait(
                    [tg_task, st_task], timeout=interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending_:
                    t.cancel()
                if self._trigger_evt.is_set():
                    self._trigger_evt.clear()
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(interval)

    def _compute_next_interval(
        self, empty_streak: int, svc_cfg: Dict[str, Any]
    ) -> float:
        base = float(svc_cfg.get("interval_sec", 15.0) or 15.0)
        fast = float(svc_cfg.get("fast_interval_sec", 4.0) or 4.0)
        slow = float(svc_cfg.get("slow_interval_sec", 30.0) or 30.0)
        slow_after = int(svc_cfg.get("slow_after_empty", 6) or 6)
        backoff_max = float(svc_cfg.get("backoff_max_sec", 120.0) or 120.0)

        since_peer = time.time() - self._last_had_peer_ts
        if since_peer < 30.0:
            return fast

        if self._consecutive_fail > 0:
            backoff = min(backoff_max, base * (2 ** min(self._consecutive_fail - 1, 4)))
            return backoff

        if empty_streak >= slow_after:
            _vi = self._merged_cfg.get("voice_input") or {}
            if isinstance(_vi, dict) and _vi.get("enabled"):
                return min(slow, base)
            return slow

        return base

    # ── 自动接受联系人申请 ────────────────────────────────────────────────

    async def _maybe_run_auto_accept(self) -> None:
        aa = self._merged_cfg.get("auto_accept") or {}
        if not isinstance(aa, dict) or not aa.get("enabled"):
            return
        interval = float(aa.get("check_interval_sec", 120.0) or 120.0)
        now = time.time()
        if now < self._next_auto_accept_ts:
            return
        self._next_auto_accept_ts = now + interval
        res = await self._runner.maybe_auto_accept_contacts(
            max_accept=int(aa.get("max_per_run", 5) or 5)
        )
        if res.get("tapped"):
            logger.info("wa auto_accept: tapped=%d", res["tapped"])

    # ── 状态查询 ──────────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        stats_24 = self._state.run_stats(24.0)
        stats_1 = self._state.run_stats(1.0)
        conv_stats = self._state.conversation_stats(24.0)
        return {
            "enabled": bool(self._merged_cfg.get("enabled")),
            "running": self.is_running,
            "paused": self._pause_until > time.time(),
            "pause_remaining_sec": max(0.0, self._pause_until - time.time()),
            "started_at": self._started_at or None,
            "last_tick_ts": self._last_tick_ts or None,
            "last_run": self._last_run,
            "stats_24h": stats_24,
            "stats_1h": stats_1,
            "conv_stats_24h": conv_stats,
            "unacked_alerts": self._state.alerts_count_unacked(),
            "pending_count": (self._state.pending_stats() or {}).get("pending", 0),
            "daily_cap": int(self._merged_cfg.get("daily_cap") or 0),
            "daily_sent": int((stats_24 or {}).get("sent") or 0),
            "reply_mode": str(self._merged_cfg.get("reply_mode") or "auto"),
        }

    # ── P15-b: 主动续聊（沉默唤醒） ───────────────────────────────────────

    def _reset_proactive_counter_if_needed(self) -> None:
        today = int(time.time() // 86400)
        if today != self._proactive_day:
            self._proactive_day = today
            self._proactive_sent_today = 0
            self._proactive_last_ts.clear()

    @staticmethod
    def _hhmm_to_minutes(hhmm: str) -> int:
        try:
            h, m = hhmm.split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return 0

    def _within_time_window(self, cfg: Dict[str, Any]) -> bool:
        now = time.localtime()
        now_min = now.tm_hour * 60 + now.tm_min
        start_min = self._hhmm_to_minutes(str(cfg.get("window_start", "00:00")))
        end_min = self._hhmm_to_minutes(str(cfg.get("window_end", "23:59")))
        if start_min <= end_min:
            return start_min <= now_min <= end_min
        return now_min >= start_min or now_min <= end_min  # 跨午夜

    async def _maybe_enqueue_proactive(self) -> None:
        cfg = self._merged_cfg.get("proactive") or {}
        if not cfg.get("enabled"):
            return

        self._reset_proactive_counter_if_needed()

        daily_cap = int(cfg.get("daily_cap") or 0)
        if daily_cap > 0 and self._proactive_sent_today >= daily_cap:
            return

        if not self._within_time_window(cfg):
            return

        silence_min = float(cfg.get("silence_minutes") or 60.0)
        per_chat_cooldown = float(cfg.get("per_chat_cooldown_minutes") or 240.0)
        max_per_tick = int(cfg.get("max_per_tick") or 1)

        try:
            rows = self._state.recent_conversations(limit=50, hours=96.0)
        except Exception:
            rows = []

        now_ts = time.time()
        candidates: List[Dict[str, Any]] = []
        for row in rows:
            chat_key = str(row.get("chat_key") or "").strip()
            if not chat_key:
                continue
            peer_name = chat_key.split(":")[-1]
            last_peer_ts = float(row.get("state_last_peer_ts") or row.get("last_peer_ts") or 0)
            last_reply_ts = float(row.get("state_last_reply_ts") or 0)
            state_quiet_until = float(row.get("state_quiet_until") or 0)
            state_blacklist = int(row.get("state_blacklist") or 0)

            if state_blacklist:
                continue
            if state_quiet_until > 0 and now_ts < state_quiet_until:
                continue

            last_interact_ts = max(last_peer_ts, last_reply_ts)
            if last_interact_ts <= 0:
                continue
            silence_secs = now_ts - last_interact_ts
            if silence_secs < silence_min * 60:
                continue
            _last_pro_ts = float(self._proactive_last_ts.get(chat_key) or 0)
            if _last_pro_ts > 0 and (now_ts - _last_pro_ts) < per_chat_cooldown * 60:
                continue
            candidates.append({
                "chat_key": chat_key,
                "peer_name": peer_name,
                "last_peer_text": row.get("state_last_peer") or row.get("peer_text") or "",
                "last_peer_ts": last_peer_ts,
                "last_reply_ts": last_reply_ts,
                "silence_secs": silence_secs,
            })

        if not candidates:
            return

        candidates.sort(key=lambda r: r.get("silence_secs", 0), reverse=True)
        selected = candidates[:max_per_tick]

        for cand in selected:
            chat_key = cand["chat_key"]
            peer_name = cand["peer_name"]
            last_peer_text = (cand.get("last_peer_text") or "").strip()
            silence_m = int(cand.get("silence_secs", 0) // 60)

            # P15-g: 使用模板池生成多样化续聊内容
            tpl_ctx = {
                "last_peer_text": last_peer_text,
                "silence_minutes": silence_m,
            }
            base_msg, tpl_category, tpl_idx = self._template_pool.select_template(
                context=tpl_ctx
            )

            # 可选：用 LLM 润色/个性化（轻量级）
            try:
                ctx = {
                    "platform": "whatsapp",
                    "account_id": self.account_id,
                    "account_persona_id": "",
                    "proactive": True,
                    "silence_minutes": silence_m,
                    "base_template": base_msg,
                    "template_category": tpl_category,
                }
                # 轻量润色提示：保持原意，稍微个性化
                polish_prompt = (
                    f"请稍微润色这句话，保持自然口语化，不要改变原意：\"{base_msg}\""
                )
                polished = await self._sm.process_message(
                    polish_prompt, user_id=chat_key, context=ctx
                )
                reply_text = (polished or "").strip() if polished else base_msg
                if len(reply_text) < 5 or len(reply_text) > 200:
                    reply_text = base_msg  # 回退到模板
            except Exception as e:
                logger.debug("[wa_rpa][proactive] 润色失败，使用模板原文 chat=%s err=%s", chat_key, e)
                reply_text = base_msg

            if not reply_text:
                continue

            self._state.enqueue_send(chat_key, peer_name, reply_text)
            self._proactive_last_ts[chat_key] = now_ts
            self._proactive_sent_today += 1
            # P15-g: 记录使用的模板，用于后续回复追踪
            self._state.upsert_chat_state(
                chat_key,
                last_proactive_template=json.dumps({
                    "category": tpl_category,
                    "idx": tpl_idx,
                    "ts": now_ts,
                    "text": reply_text[:100],  # 记录前100字符用于核对
                })
            )
            logger.warning(
                "[wa_rpa][proactive] enqueued chat=%s silence=%dm cat=%s idx=%d text=%r",
                chat_key, silence_m, tpl_category, tpl_idx, reply_text[:60]
            )

            if daily_cap > 0 and self._proactive_sent_today >= daily_cap:
                break

    def proactive_status(self) -> Dict[str, Any]:
        cfg = self._merged_cfg.get("proactive") or {}
        self._reset_proactive_counter_if_needed()
        last_contacts = sorted(
            self._proactive_last_ts.items(), key=lambda kv: kv[1], reverse=True
        )[:5]
        stop_kws = self._merged_cfg.get("stop_contact_keywords") or []
        # 统计最近 24h stop_contact 触发次数
        stop_hits = 0
        stop_state = {"blacklist": 0, "quiet_active": 0}
        try:
            tl = self._state.timeline(minutes=24*60, limit=500)
            stop_hits = sum(1 for r in tl if r.get("kind") == "stop_contact")
            stop_state = self._state.stop_contact_stats()
        except Exception:
            stop_hits = 0
        # P15-g: 模板池 A/B 统计（内存级）
        tpl_stats = self._template_pool.get_stats()
        # P15-g: 从 timeline 读取 proactive 回复统计（持久化）
        proactive_replied = 0
        replied_by_category: Dict[str, int] = {}
        try:
            tl_48h = self._state.timeline(minutes=48*60, limit=1000)
            for rec in tl_48h:
                if rec.get("kind") == "proactive_replied":
                    proactive_replied += 1
                    detail = json.loads(rec.get("detail") or "{}")
                    cat = detail.get("template_category", "unknown")
                    replied_by_category[cat] = replied_by_category.get(cat, 0) + 1
        except Exception:
            pass
        return {
            "enabled": bool(cfg.get("enabled")),
            "cfg": cfg,
            "sent_today": self._proactive_sent_today,
            "day": self._proactive_day,
            "active_contacts": len(self._proactive_last_ts),
            "recent_contacts": [
                {"chat_key": ck, "ts": ts} for ck, ts in last_contacts
            ],
            "stop_contact_keywords": stop_kws,
            "stop_contact_hits_24h": stop_hits,
            "stop_contact_state": stop_state,
            "template_stats": tpl_stats,  # P15-g: 内存级 A/B 统计
            "proactive_replied_48h": proactive_replied,  # P15-g: 48h 回复数
            "replied_by_category": replied_by_category,  # P15-g: 按类别回复数
        }

    def _load_template_history(self) -> None:
        """P15-h: 从 timeline 加载历史模板回复数据到模板池（用于加权选择）。"""
        try:
            tl = self._state.timeline(minutes=7*24*60, limit=5000)  # 7天
            loaded = 0
            for rec in tl:
                if rec.get("kind") == "proactive_replied":
                    detail = json.loads(rec.get("detail") or "{}")
                    cat = detail.get("template_category")
                    idx = detail.get("template_idx")
                    if cat and idx is not None:
                        self._template_pool.record_reply(cat, idx)
                        loaded += 1
            if loaded > 0:
                logger.info("[wa_rpa][template] 加载历史回复数据: %d 条", loaded)
        except Exception as e:
            logger.debug("[wa_rpa][template] 加载历史数据失败: %s", e)

    # ── P15-c: 防骚扰/静默控制 ────────────────────────────────────────────

    def set_chat_quiet(self, chat_key: str, minutes: float) -> None:
        """为指定联系人设置静默（分钟）。minutes<=0 视为解除。"""
        if not chat_key:
            raise ValueError("chat_key required")
        until = 0.0
        if minutes > 0:
            until = time.time() + minutes * 60
        self._state.upsert_chat_state(chat_key, quiet_until=until)

    def set_chat_blacklist(self, chat_key: str, blacklist: bool = True) -> None:
        """设置/取消联系人黑名单。"""
        if not chat_key:
            raise ValueError("chat_key required")
        self._state.upsert_chat_state(chat_key, blacklist=1 if blacklist else 0)

    def recent_runs(self, limit: int = 50, only_with_peer: bool = False) -> list:
        return self._state.recent_runs(limit=limit, only_with_peer=only_with_peer)

    def recent_conversations(self, limit: int = 30, hours: float = 48.0) -> list:
        return self._state.recent_conversations(limit=limit, hours=hours)

    def chat_history(self, chat_key: str, limit: int = 10, offset: int = 0) -> list:
        return self._state.chat_history(chat_key=chat_key, limit=limit, offset=offset)

    def sessions_for_chat(self, chat_key: str) -> list:
        return self._state.sessions_for_chat(chat_key=chat_key)

    def total_turns_for_chat(self, chat_key: str) -> int:
        return self._state.total_turns_for_chat(chat_key=chat_key)

    def customer_profile(self, chat_key: str) -> dict:
        return self._state.customer_profile(chat_key=chat_key)

    def search_history(self, q: str, intent: str = "", days: int = 30, limit: int = 20) -> list:
        return self._state.search_history(q, intent=intent, days=days, limit=limit)

    def intent_stats(self, window_hours: float = 168.0) -> dict:
        return self._state.intent_stats(window_hours=window_hours)

    def match_chat_name(self, name: str) -> dict:
        """P12-A: 跨平台身份匹配。"""
        return self._state.match_chat_name(name=name)

    def list_alerts(self, *, only_unacked: bool = True, limit: int = 50) -> list:
        return self._state.list_alerts(only_unacked=only_unacked, limit=limit)

    def ack_alert(self, alert_id: int, by: str = "") -> Optional[Dict]:
        return self._state.ack_alert(alert_id, by=by)

    def ack_all_alerts(self, by: str = "") -> int:
        return self._state.ack_all_alerts(by=by)

    def alerts_count_unacked(self) -> int:
        return self._state.alerts_count_unacked()

    def timeline(self, *, minutes: int = 60, limit: int = 200) -> list:
        return self._state.timeline(minutes=minutes, limit=limit)

    def list_pending(self, *, status: Optional[str] = None, limit: int = 50) -> list:
        return self._state.list_pending(status=status, limit=limit)

    def resolve_pending(self, pending_id: int, action: str, by: str = "") -> Optional[Dict]:
        return self._state.resolve_pending(pending_id, action, by=by)

    def pending_stats(self) -> Dict:
        return self._state.pending_stats()

    # ── P4-B: 手动发送队列 ────────────────────────────────────────────────

    def enqueue_send(self, chat_key: str, peer_name: str, text: str) -> int:
        """入队一条主动发送任务并立即唤醒 runner 进入下一轮（避免等到下一个 interval）。"""
        item_id = self._state.enqueue_send(
            chat_key=chat_key, peer_name=peer_name, text=text)
        # 唤醒 runner 立刻 pop（与 LINE service 对称：入队即触发，降投递延迟）——
        # _loop 在 asyncio.wait([_trigger_evt.wait(), ...], timeout=interval) 上阻塞，
        # set() 令其立即返回并处理发送队列，而非空等一个自适应轮询间隔（最长数十秒）。
        try:
            self._trigger_evt.set()
        except Exception:
            logger.debug("wa enqueue_send 触发唤醒失败（已忽略）", exc_info=True)
        return item_id

    def list_send_queue(self, limit: int = 30, include_done: bool = False) -> list:
        return self._state.list_send_queue(limit=limit, include_done=include_done)

    def get_send_queue_item(self, item_id: int):
        """P15-C: 单条查询。"""
        return self._state.get_send_queue_item(int(item_id))

    def cancel_send_queue_item(self, item_id: int) -> None:
        self._state.mark_send_queue_item(item_id, "cancelled")
