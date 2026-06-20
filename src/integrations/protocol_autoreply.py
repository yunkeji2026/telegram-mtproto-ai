"""协议账号 7×24 自动回复（Phase 3）。

protocol 模式 worker（Telegram pyrogram / WhatsApp Baileys）收到入站消息后,
可在服务端**无人值守**生成并发送回复——这是 RPA/桌面 webview 之外、真正能挂大量
账号、跨会话全程托管的路径。

安全设计（双闸门 + 风控 + 冷却）：
  - 全局闸门 ``config.protocol_autoreply.enabled``（默认 False）
  - 账号闸门 registry ``meta.auto_reply``（默认 False）——两者皆开才会自动发
  - 高风险（支付/密码/账号安全，复用 keyword_risk_level）→ 不发,转人工
  - 每会话冷却 + 同条入站去重 → 防刷屏、防回环

生成复用生产级入口 ``SkillManager.process_message``（与真 bot/RPA 同一条产线,
带人设/意图/策略/KB）；发送复用 ``AccountOrchestrator.send``（并回写收件箱线程）。

本模块核心 ``run_autoreply`` 全程依赖注入（registry/generate/send/risk_fn）,
不依赖 FastAPI/pyrogram,可纯单测。``build_reply_hook(app)`` 在 web 层接线生产依赖。
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# 每会话最近一次自动回复：key=platform:account_id:chat_key → (last_inbound_text, ts)
_last_reply: Dict[str, tuple] = {}
AUTO_COOLDOWN_SEC = 5.0  # 同会话两次自动发的最小间隔，防刷屏/回环


def is_autoreply_enabled(cfg: Dict[str, Any], account_row: Dict[str, Any]) -> bool:
    """双闸门：全局 protocol_autoreply.enabled 且 账号 meta.auto_reply 皆为真。"""
    try:
        if not ((cfg or {}).get("protocol_autoreply") or {}).get("enabled", False):
            return False
    except Exception:
        return False
    meta = (account_row or {}).get("meta") or {}
    return bool(meta.get("auto_reply"))


def _account_effective_pa(cfg: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    """账号级有效 protocol_autoreply：全局有效设置 ⊕ 账号 meta.autoreply_override。"""
    glob = (cfg or {}).get("protocol_autoreply") or {}
    override = (row.get("meta") or {}).get("autoreply_override") or {}
    if not override:
        return dict(glob)
    try:
        from src.integrations.protocol_autoreply_settings import merge_account_override
        return merge_account_override(glob, override)
    except Exception:
        return dict(glob)


def _default_risk(text: str) -> str:
    try:
        from src.inbox.drafts import keyword_risk_level
        return keyword_risk_level(text) or "low"
    except Exception:
        return "low"


# 触发「转人工」的原因（自动回复未能安全送出 → 需要坐席接管）
HANDOFF_REASONS = frozenset({
    "high_risk", "empty_reply", "generate_error", "send_error",
    "quota_hour", "quota_day", "circuit_open", "off_hours",
})


def _parse_hhmm(s: str, default_min: int) -> int:
    """'09:30' → 570（一天内的分钟数）。解析失败回 default_min。"""
    try:
        hh, mm = str(s or "").split(":", 1)
        return (int(hh) % 24) * 60 + (int(mm) % 60)
    except Exception:
        return default_min


def within_business_hours(cfg: Dict[str, Any], now: Optional[float] = None) -> bool:
    """是否在「营业时段」内。未配置 / 未启用 → 恒 True（7×24）。

    config.protocol_autoreply.hours = {enabled, start:'HH:MM', end:'HH:MM', tz_offset}
    支持跨夜窗口（start>end，如 22:00→06:00）。
    """
    h = ((cfg or {}).get("protocol_autoreply") or {}).get("hours") or {}
    if not h.get("enabled"):
        return True
    now = now if now is not None else time.time()
    tz_offset = float(h.get("tz_offset", 8))
    local = now + tz_offset * 3600.0
    minutes = int(local // 60) % 1440
    start = _parse_hhmm(h.get("start", "09:00"), 540)
    end = _parse_hhmm(h.get("end", "23:00"), 1380)
    if start <= end:
        return start <= minutes < end
    return minutes >= start or minutes < end  # 跨夜


def pick_delay(cfg: Dict[str, Any]) -> float:
    """按 config.protocol_autoreply.delay = {min_sec, max_sec} 取随机拟人延迟（秒）。

    未配置 / 非法 → 0（不延迟）。"""
    d = ((cfg or {}).get("protocol_autoreply") or {}).get("delay") or {}
    try:
        lo = float(d.get("min_sec", 0) or 0)
        hi = float(d.get("max_sec", 0) or 0)
    except Exception:
        return 0.0
    if hi <= 0 or hi < lo:
        return 0.0
    return random.uniform(max(0.0, lo), hi)
# 值得落审计的原因（过滤掉门控/冷却/去重等噪声）
AUDIT_REASONS = frozenset({"ok"}) | HANDOFF_REASONS


def _result(reason: str, *, sent: bool = False, **extra: Any) -> Dict[str, Any]:
    """统一结果结构：decision/reason 恒在；兼容旧键 sent/skipped。"""
    r: Dict[str, Any] = {
        "decision": "sent" if sent else "skipped", "reason": reason,
    }
    if sent:
        r["sent"] = True
    else:
        r["skipped"] = reason
    r.update(extra)
    return r


async def run_autoreply(
    payload: Dict[str, Any],
    *,
    registry: Any,
    cfg: Dict[str, Any],
    generate: Callable[..., Awaitable[Optional[str]]],
    send: Callable[..., Awaitable[Any]],
    risk_fn: Callable[[str], str] = _default_risk,
    now: Optional[float] = None,
    limiter: Any = None,
    sleep: Optional[Callable[[float], Awaitable[Any]]] = None,
) -> Dict[str, Any]:
    """核心：判断并执行一次自动回复。返回结构化结果（便于测试/观测）。

    依赖注入：registry.get(platform, account_id) / generate(...) / send(...)。
    ``limiter`` 可选（按账号限速 + 熔断）；None 表示不限流（保持纯逻辑可测）。
    ``sleep`` 可选（拟人化发送延迟，生产传 asyncio.sleep）；None 表示不延迟。
    """
    if (payload or {}).get("direction", "in") != "in":
        return _result("not_inbound")
    platform = str(payload.get("platform") or "")
    account_id = str(payload.get("account_id") or "")
    chat_key = str(payload.get("chat_key") or "")
    text = str(payload.get("text") or "").strip()
    if not (platform and account_id and chat_key and text):
        return _result("incomplete")

    try:
        row = registry.get(platform, account_id) or {}
    except Exception:
        row = {}
    if not is_autoreply_enabled(cfg, row):
        return _result("disabled")

    # G1 全局 Kill-Switch：紧急冻结时在决策期就早退（不生成、不发、不浪费 token）；
    # 与预热闸门正交（无视 companion_send_gate.enabled）。入站仍由收件箱 ingest 收录，
    # 故不另打人工标签（避免全局停发时人工队列被瞬时灌爆），等同 disabled 抑制。
    try:
        from src.ops.kill_switch import is_blocked as _ks_blocked
        _ks_on, _ks_scope, _ks_reason = _ks_blocked(platform, account_id)
    except Exception:
        _ks_on, _ks_scope = False, ""
    if _ks_on:
        logger.warning("[kill-switch] 冻结发送 %s:%s（scope=%s）", platform, account_id, _ks_scope)
        return _result("kill_switch", inbound=text)

    # G3 金丝雀放量：启用且本号不在 cohort → 决策期早退（不生成、不发；与 disabled 同抑制，
    # 不打人工标签避免放量期人工队列被灌爆）。未启用→零破坏。
    try:
        from src.ops.canary import is_held as _canary_held
        _ch_on, _ = _canary_held(platform, account_id, cfg)
    except Exception:
        _ch_on = False
    if _ch_on:
        return _result("canary_hold", inbound=text)

    ts = now if now is not None else time.time()
    # 账号级有效设置 = 全局有效 protocol_autoreply ⊕ 账号 meta.autoreply_override
    acct_pa = _account_effective_pa(cfg, row)
    acct_cfg = {"protocol_autoreply": acct_pa}
    # 营业时段外：不自动发，转人工（坐席上班后处理）
    if not within_business_hours(acct_cfg, ts):
        return _result("off_hours", inbound=text)

    # 账号级闸门：限速 / 熔断（区别于下方会话级去重/冷却）
    account_key = f"{platform}:{account_id}"
    ov_rate = acct_pa.get("rate") or {}
    if limiter is not None:
        allowed, why = limiter.allow(
            account_key, ts,
            hourly=ov_rate.get("hourly"), daily=ov_rate.get("daily"))
        if not allowed:
            return _result(why, inbound=text)

    key = f"{platform}:{account_id}:{chat_key}"
    last = _last_reply.get(key)
    if last is not None:
        if last[0] == text:
            return _result("duplicate")
        if ts - last[1] < AUTO_COOLDOWN_SEC:
            return _result("cooldown")
    # 先占位（含本条入站文本），避免生成期间同条消息重复触发
    _last_reply[key] = (text, ts)

    persona_id = str((row.get("meta") or {}).get("persona_id") or "")
    try:
        reply = await generate(
            text=text, platform=platform, account_id=account_id,
            chat_key=chat_key, persona_id=persona_id,
        )
    except Exception:
        logger.warning("[protocol-autoreply] 生成失败 %s", key, exc_info=True)
        opened = limiter.record_failure(account_key) if limiter is not None else False
        return _result("generate_error", inbound=text, breaker_opened=opened)
    reply = str(reply or "").strip()
    if not reply:
        return _result("empty_reply", inbound=text)

    risk = "low"
    try:
        risk = (risk_fn(reply) if risk_fn else "low") or "low"
    except Exception:
        risk = "low"
    if risk == "high":
        logger.warning("[protocol-autoreply] 命中高风险，转人工不自动发：%s", key)
        return _result("high_risk", text=reply, inbound=text, risk=risk)

    # 拟人化发送延迟（模拟打字；只在确定要发时才等，跳过的不浪费时间）
    if sleep is not None:
        d = pick_delay(acct_cfg)
        if d > 0:
            try:
                await sleep(d)
            except Exception:
                pass

    try:
        await send(platform=platform, account_id=account_id,
                   chat_key=chat_key, text=reply)
    except Exception:
        logger.warning("[protocol-autoreply] 发送失败 %s", key, exc_info=True)
        opened = limiter.record_failure(account_key) if limiter is not None else False
        return _result("send_error", text=reply, inbound=text, risk=risk,
                       breaker_opened=opened)
    # 发送成功后用「发送时刻」刷新冷却基准 + 记账号配额/闭合熔断
    send_ts = ts if now is not None else time.time()
    _last_reply[key] = (text, send_ts)
    if limiter is not None:
        limiter.record_sent(account_key, send_ts)
        limiter.record_success(account_key)
    return _result("ok", sent=True, text=reply, inbound=text, risk=risk)


# 自动回复未能安全送出时，给会话打的标签（在统一收件箱里高亮，供坐席接管）
HANDOFF_TAG = "需人工"


def record_decision_audit(audit: Any, payload: Dict[str, Any],
                          res: Dict[str, Any]) -> bool:
    """把一次有意义的决策落审计（门控/冷却/去重等噪声不记）。返回是否记录。"""
    reason = str((res or {}).get("reason") or "")
    if reason not in AUDIT_REASONS:
        return False
    from src.inbox.normalizer import conv_id
    platform = str(payload.get("platform") or "")
    account_id = str(payload.get("account_id") or "")
    chat_key = str(payload.get("chat_key") or "")
    audit.record(
        platform=platform, account_id=account_id, chat_key=chat_key,
        conversation_id=conv_id(platform, account_id, chat_key),
        inbound=str(payload.get("text") or ""),
        reply=str(res.get("text") or ""),
        risk=str(res.get("risk") or ""),
        decision=str(res.get("decision") or ""),
        reason=reason,
    )
    return True


def needs_handoff(res: Dict[str, Any]) -> bool:
    return str((res or {}).get("reason") or "") in HANDOFF_REASONS


# 告警防抖：同账号同类告警 30 分钟最多发一次（避免配额耗尽时刷屏）
_alert_seen: Dict[str, float] = {}
_ALERT_DEBOUNCE_SEC = 1800.0


def publish_alert(kind: str, payload: Dict[str, Any], detail: str = "",
                  now: Optional[float] = None) -> bool:
    """熔断 / 配额耗尽等运维告警 → EventBus（WebhookNotifier 转钉钉/飞书/企微）。

    防抖：同 (kind, platform, account) 30 分钟一次。返回是否真的发了。
    """
    platform = str(payload.get("platform") or "")
    account_id = str(payload.get("account_id") or "")
    key = f"{kind}:{platform}:{account_id}"
    ts = now if now is not None else time.time()
    last = _alert_seen.get(key)
    if last is not None and ts - last < _ALERT_DEBOUNCE_SEC:
        return False
    _alert_seen[key] = ts
    try:
        from src.integrations.shared.event_bus import get_event_bus
        get_event_bus().publish("autoreply_alert", {
            "kind": kind, "platform": platform, "account_id": account_id,
            "detail": detail,
        })
        return True
    except Exception:
        logger.debug("[protocol-autoreply] 告警发布失败", exc_info=True)
        return False


def clear_needs_human(store: Any, conversation_id: str) -> bool:
    """坐席接管（人工发出消息）后清除 HANDOFF_TAG。返回是否真的清除了。"""
    if store is None or not conversation_id:
        return False
    try:
        tags = list(store.get_conv_tags(conversation_id) or [])
    except Exception:
        return False
    if HANDOFF_TAG not in tags:
        return False
    try:
        store.set_conv_tags(conversation_id, [t for t in tags if t != HANDOFF_TAG])
        return True
    except Exception:
        return False


def tag_needs_human(store: Any, payload: Dict[str, Any]) -> bool:
    """给会话打 HANDOFF_TAG（已存在则跳过）。store 需提供 get/set_conv_tags。"""
    if store is None:
        return False
    from src.inbox.normalizer import conv_id
    cid = conv_id(
        str(payload.get("platform") or ""),
        str(payload.get("account_id") or ""),
        str(payload.get("chat_key") or ""),
    )
    try:
        tags = list(store.get_conv_tags(cid) or [])
    except Exception:
        tags = []
    if HANDOFF_TAG in tags:
        return False
    tags.append(HANDOFF_TAG)
    try:
        store.set_conv_tags(cid, tags)
        return True
    except Exception:
        return False


def build_reply_hook(app: Any) -> Callable[[Dict[str, Any]], Awaitable[None]]:
    """生产接线：从 app.state 取 skill_manager/config/orchestrator，返回异步 hook。"""

    def _cfg() -> Dict[str, Any]:
        cm = getattr(app.state, "config_manager", None)
        base = (getattr(cm, "config", None) or {}) if cm is not None else {}
        try:
            from src.integrations.protocol_autoreply_settings import (
                cfg_with_settings,
            )
            return cfg_with_settings(base)
        except Exception:
            return base

    async def _generate(*, text, platform, account_id, chat_key, persona_id):
        sm = getattr(app.state, "skill_manager", None)
        if sm is None:
            tc = getattr(app.state, "telegram_client", None)
            sm = getattr(tc, "skill_manager", None) if tc is not None else None
        if sm is None or not hasattr(sm, "process_message"):
            return None
        # N 线 核心1：复用共享 companion_context 装配标准上下文（与 A 线同一套）。
        # 记忆/情绪由 skill_manager 内部按 platform+user_id+chat_id 注入；
        # 此处保证平台/会话标识 + 人设一致（协议线默认私聊）。
        from src.utils.companion_context import build_companion_context
        _emo = getattr(app.state, "emotion_enhancer", None)
        if _emo is None:
            _tc = getattr(app.state, "telegram_client", None)
            _emo = getattr(_tc, "emotion_enhancer", None) if _tc is not None else None
        ctx = build_companion_context(
            platform=platform,
            chat_id=chat_key,
            text=text,
            chat_type="private",
            persona_id=persona_id,
            emotion_enhancer=_emo,
            extra={"channel": "protocol"},
        )
        return await sm.process_message(
            text, user_id=f"{platform}:{account_id}:{chat_key}", context=ctx
        )

    async def _send(*, platform, account_id, chat_key, text):
        from src.integrations.account_orchestrator import get_orchestrator
        cfg = _cfg()
        # G1 Kill-Switch（防御兜底）：无视预热闸门是否开，直达发送边界的硬停——
        # 任何绕过 run_autoreply 决策直接调 _send 的路径（如编排器/手动）也被冻结覆盖。
        try:
            from src.ops.kill_switch import is_blocked as _ks_blocked
            _ks_on, _ks_scope, _ = _ks_blocked(platform, account_id)
        except Exception:
            _ks_on, _ks_scope = False, ""
        if _ks_on:
            raise RuntimeError(f"kill_switch_blocked:{_ks_scope}")
        # N 线 核心3：发送前反封号闸门（A/B 两线共用 companion_send_gate；默认关→零破坏）。
        # 拦截 → 抛错，交由 run_autoreply 既有熔断/转人工处理。
        from src.skills.companion_send_gate import evaluate, gate_enabled
        if gate_enabled(cfg):
            try:
                from src.integrations.account_registry import get_account_registry
                from src.integrations.protocol_autoreply_limits import (
                    get_autoreply_limiter,
                )
                from src.skills.account_signals import build_account_signals
                # N3 信号接线：真 sends_today(限额计数) + age_days/proxy/banned(注册表)
                sig = build_account_signals(
                    platform, account_id,
                    registry=get_account_registry(),
                    limiter=get_autoreply_limiter(cfg),
                )
                dec = evaluate(sig, cfg)
            except Exception:
                logger.debug("[send_gate] 信号装配失败，放行", exc_info=True)
                dec = {"allowed": True}
            if not dec.get("allowed", True):
                raise RuntimeError(f"send_gate_blocked:{dec.get('reason')}")
        orch = get_orchestrator(cfg)
        try:
            return await orch.send(platform, account_id, chat_key, text)
        except Exception as _send_exc:
            # G2 封号信号自动急停：风控错误 → 分级处置（退避/暂停/封禁），再抛回既有熔断
            try:
                from src.ops.ban_signal import handle_send_exception as _g2
                from src.integrations.account_registry import get_account_registry
                _g2(platform, account_id, _send_exc,
                    registry=get_account_registry(), alert=publish_alert)
            except Exception:
                pass
            raise

    async def hook(payload: Dict[str, Any]) -> None:
        from src.integrations.account_registry import get_account_registry
        from src.integrations.protocol_autoreply_limits import (
            get_autoreply_limiter,
        )
        import asyncio
        cfg = _cfg()
        res = await run_autoreply(
            payload, registry=get_account_registry(), cfg=cfg,
            generate=_generate, send=_send,
            limiter=get_autoreply_limiter(cfg), sleep=asyncio.sleep,
        )
        # Phase 5：熔断刚触发 → 告警（断路器自身已在冷却期拦截后续）
        if res.get("breaker_opened"):
            logger.warning(
                "[protocol-autoreply] 账号 %s:%s 连续失败触发熔断，冷却期内暂停自动回复",
                payload.get("platform"), payload.get("account_id"))
            publish_alert("circuit_open", payload, "连续失败触发熔断，已暂停自动回复")
        # Phase 8：配额耗尽 → 告警（防抖，避免刷屏）
        elif res.get("reason") in ("quota_hour", "quota_day"):
            publish_alert(res["reason"], payload, "自动回复配额已用尽，转人工")
        # Phase 4：审计 + 转人工（best-effort，绝不影响主流程）
        try:
            from src.integrations.protocol_autoreply_audit import (
                get_autoreply_audit,
            )
            record_decision_audit(get_autoreply_audit(), payload, res)
        except Exception:
            logger.debug("[protocol-autoreply] 审计写入失败", exc_info=True)
        try:
            if needs_handoff(res):
                tag_needs_human(getattr(app.state, "inbox_store", None), payload)
        except Exception:
            logger.debug("[protocol-autoreply] 转人工打标失败", exc_info=True)

    return hook
