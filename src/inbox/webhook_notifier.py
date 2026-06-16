"""WebhookNotifier — L2 Webhook 通知（面向海外：Telegram / WhatsApp / Messenger）。

架构思路（优于"在 SLAWatcher 内直接 HTTP"方案）：
  - 订阅全局 EventBus，与 SLAWatcher / DraftService 完全解耦
  - 任何发布到 EventBus 的事件都可被拦截，无需改动上游代码
  - asyncio 原生 HTTP（在 executor 中），不阻塞事件循环
  - 内置速率限制：同一 (event_type, key) 每小时最多通知一次
  - 运行时可 reload()，配合 config/notify_webhooks.json 覆盖层免重启增删

支持事件：见 ``_EVENT_ALIASES``（含 autoreply_alert 自动回复熔断/配额告警）

支持格式（推荐海外渠道在前）：
  telegram  — Telegram Bot sendMessage（token + chat_id，海外首选，免企业认证）
  whatsapp  — WhatsApp Cloud API（Graph messages 端点 + Bearer token + 收件号）
  messenger — Messenger Send API（Graph me/messages + page access_token + PSID）
  json      — 原始 JSON body（万能：Zapier / n8n / 自建服务）
  dingtalk / feishu / wecom — 国内企业 IM（保留兼容，海外部署不用）

配置（config.yaml::notify.webhooks，或后台「告警渠道」面板 → notify_webhooks.json）：
  webhooks:
    - name: "tg-ops"
      format: "telegram"
      token: "123456:ABC..."        # Bot token（或直接给完整 url）
      target: "-1001234567890"       # chat_id（群/频道/个人）
      events: ["autoreply_alert", "sla_breach"]
    - name: "wa-ops"
      format: "whatsapp"
      url: "https://graph.facebook.com/v19.0/<PHONE_ID>/messages"
      token: "EAAB..."               # 永久/临时 access token（Bearer）
      target: "8613800000000"        # 收件人号码（含国家码，无 +）
      events: ["autoreply_alert"]
    - name: "msgr-ops"
      format: "messenger"
      token: "<PAGE_ACCESS_TOKEN>"   # 主页 access token（或完整 url）
      target: "<PSID>"               # 收件人 page-scoped id
      events: ["autoreply_alert"]
    - name: "my-server"
      url: "https://myserver.com/hook"
      format: "json"
      events: ["all"]                # "all" 匹配所有事件
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ─── 事件别名 → 实际 EventBus event_type + 条件 ───────────────────────────

_EVENT_ALIASES: Dict[str, Dict[str, Any]] = {
    "all": {"types": None},                               # 所有事件
    "L2_created": {"types": {"draft_created"}, "levels": {"L2"}},
    "L3_created": {"types": {"draft_created"}, "levels": {"L3"}},
    "L4_created": {"types": {"draft_created"}, "levels": {"L4"}},
    "draft_created": {"types": {"draft_created"}, "levels": None},
    "sla_breach": {"types": {"draft_sla_breach"}, "levels": None},
    "reassigned": {"types": {"draft_reassigned"}, "levels": None},
    "escalation": {"types": {"escalation"}, "levels": None},
    "reply_risk": {"types": {"human_reply_risk"}, "levels": None},  # M3
    "report": {"types": {"report"}, "levels": None},                # M2 简报推送
    "csat_alert": {"types": {"csat_alert"}, "levels": None},        # O2 智能预警
    "crm_sync":      {"types": {"draft_resolved"}, "levels": None},   # P1 外部 CRM 同步
    "anomaly":       {"types": {"anomaly_alert"}, "levels": None},   # S2 统计异常预警
    # P28：会话生命周期事件
    "new_message":   {"types": {"inbox_message"}, "levels": None},   # 新入站消息
    "conv_archived": {"types": {"conv_archived"}, "levels": None},   # 会话归档
    "conv_tagged":   {"types": {"conv_tagged"}, "levels": None},     # 标签变更
    "conv_note":     {"types": {"conv_note"}, "levels": None},       # 坐席注解（@提及）
    "queue_alert":   {"types": {"queue_alert"}, "levels": None},     # P29 队列告警
    "autoreply_alert": {"types": {"autoreply_alert"}, "levels": None},  # 协议自动回复熔断/配额
    "health_alert":  {"types": {"health_alert"}, "levels": None},    # D3 运行时健康告警
    "billing_alert": {"types": {"billing_alert"}, "levels": None},   # E3 计费异常（超席位/超额）
}

# ─── 速率限制 ────────────────────────────────────────────────────────────────

_RATE_WINDOW_SEC: float = 3600.0   # 同一 key 每小时最多一次


class _RateLimiter:
    def __init__(self, window_sec: float = _RATE_WINDOW_SEC) -> None:
        self._window = window_sec
        self._seen: Dict[str, float] = {}   # key → last_sent_ts

    def allow(self, key: str) -> bool:
        now = time.time()
        last = self._seen.get(key, 0.0)
        if now - last < self._window:
            return False
        self._seen[key] = now
        return True

    def cleanup(self) -> None:
        now = time.time()
        self._seen = {k: v for k, v in self._seen.items() if now - v < self._window * 2}


# ─── 格式化器 ────────────────────────────────────────────────────────────────

def _fmt_json(title: str, text: str, data: Dict[str, Any]) -> bytes:
    return json.dumps({"title": title, "text": text, "data": data}, ensure_ascii=False).encode()


def _fmt_dingtalk(title: str, text: str, data: Dict[str, Any]) -> bytes:
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": f"### {title}\n\n{text}",
        },
    }
    return json.dumps(payload, ensure_ascii=False).encode()


def _fmt_feishu(title: str, text: str, data: Dict[str, Any]) -> bytes:
    payload = {
        "msg_type": "text",
        "content": {"text": f"{title}\n{text}"},
    }
    return json.dumps(payload, ensure_ascii=False).encode()


def _fmt_wecom(title: str, text: str, data: Dict[str, Any]) -> bytes:
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": f"**{title}**\n{text}"},
    }
    return json.dumps(payload, ensure_ascii=False).encode()


_FORMATTERS = {
    "json": _fmt_json,
    "dingtalk": _fmt_dingtalk,
    "feishu": _fmt_feishu,
    "wecom": _fmt_wecom,
}

# ─── 海外即时通讯渠道（Telegram / WhatsApp / Messenger）────────────────────────

CHAT_FORMATS = {"telegram", "whatsapp", "messenger"}

_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")


def _plainify(text: str, base_url: str = "") -> str:
    """把内部 Markdown（**粗体** / [文字](链接) / ### 标题）转成 IM 纯文本。
    相对链接（/workspace/...）默认只保留文字；给了 base_url 则拼成绝对地址。"""
    def _link(m: "re.Match") -> str:
        label, href = m.group(1), m.group(2)
        if href.startswith("http"):
            return f"{label}: {href}"
        if base_url and href.startswith("/"):
            return f"{label}: {base_url.rstrip('/')}{href}"
        return label
    s = _MD_LINK.sub(_link, text or "")
    s = _MD_BOLD.sub(r"\1", s)
    s = s.replace("### ", "")
    return s.strip()


def _resolve_chat_endpoint(fmt: str, url: str, token: str) -> str:
    """渠道端点解析：给了完整 url 直接用；否则按 token 推断标准端点。
    whatsapp 需要 phone-id，无法仅凭 token 推断，必须显式给 url。"""
    url = (url or "").strip()
    if url:
        return url
    token = (token or "").strip()
    if fmt == "telegram" and token:
        return f"https://api.telegram.org/bot{token}/sendMessage"
    if fmt == "messenger" and token:
        return ("https://graph.facebook.com/v19.0/me/messages"
                f"?access_token={urllib.parse.quote(token)}")
    return ""


def _build_chat_body(
    fmt: str, msg: str, target: str, token: str,
) -> Tuple[bytes, Dict[str, str]]:
    """构造海外 IM 渠道的请求体与请求头（whatsapp 走 Bearer 鉴权）。"""
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if fmt == "telegram":
        payload: Dict[str, Any] = {
            "chat_id": target, "text": msg,
            "disable_web_page_preview": True,
        }
    elif fmt == "whatsapp":
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = {
            "messaging_product": "whatsapp", "to": target,
            "type": "text", "text": {"body": msg, "preview_url": False},
        }
    elif fmt == "messenger":
        payload = {
            "recipient": {"id": target},
            "messaging_type": "UPDATE",
            "message": {"text": msg},
        }
    else:
        payload = {"text": msg}
    return json.dumps(payload, ensure_ascii=False).encode(), headers


# ─── 钉钉签名 ────────────────────────────────────────────────────────────────

def _dingtalk_sign(secret: str) -> tuple:
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return ts, sign


# ─── 消息构建 ────────────────────────────────────────────────────────────────

def _build_message(event_type: str, data: Dict[str, Any]) -> tuple[str, str]:
    """根据 event_type + data 构造 (title, markdown_text)。"""
    if event_type == "draft_created":
        lv = data.get("autopilot_level", "?")
        plat = data.get("platform", "?")
        peer = str(data.get("peer_text") or data.get("peer_text_preview") or "")[:60]
        title = f"{'🚨' if lv=='L4' else '⚠️'} 新草稿 [{lv}]"
        text = (
            f"**平台**: {plat}\n"
            f"**风险等级**: {data.get('risk_level', '?')}\n"
            f"**客户消息**: {peer or '（无）'}\n"
            "[📋 前往审批](/workspace/drafts)"
        )

    elif event_type == "draft_sla_breach":
        lv = data.get("autopilot_level", "?")
        wait = data.get("wait_min", "?")
        peer = str(data.get("peer_text_preview") or "")[:60]
        title = f"⏰ 草稿 SLA 超时 [{lv}]"
        text = (
            f"**已等待**: {wait} 分钟（SLA={data.get('sla_hours','?')}h）\n"
            f"**客户消息**: {peer or '（无）'}\n"
            f"**平台**: {data.get('platform', '?')}\n"
            "[📋 前往审批](/workspace/drafts)"
        )

    elif event_type == "draft_reassigned":
        title = "🔄 草稿已自动再分配"
        text = (
            f"**来自坐席**: {data.get('from_agent', '?')}\n"
            f"**转给主管**: {data.get('to_agent_name') or data.get('to_agent', '?')}\n"
            f"**原因**: {data.get('reason', 'agent_offline')}\n"
            "[📋 前往审批](/workspace/drafts)"
        )

    elif event_type == "human_reply_risk":
        title = f"⚠️ 坐席回复质量警告 [{data.get('risk_level', '?')}]"
        text = (
            f"**坐席**: {data.get('agent_id', '?')}\n"
            f"**风险等级**: {data.get('risk_level', '?')}\n"
            f"**原因**: {', '.join(data.get('risk_reasons') or []) or '—'}\n"
            f"**回复预览**: {data.get('text_preview', '')[:60]}\n"
            "[📋 查看审计](/workspace/agent-perf)"
        )

    elif event_type == "report":
        period = data.get("period", "daily")
        title = f"📊 {'今日' if period == 'daily' else '本周'}工作简报"
        text = str(data.get("text") or "")

    elif event_type == "anomaly_alert":
        # S2: 统计异常预警
        anomalies = data.get("anomalies") or []
        count = data.get("anomaly_count", 0)
        title = f"🔔 统计异常预警：{count} 项指标偏离基线"
        lines = []
        for a in anomalies:
            direction_icon = "📉" if a.get("direction") == "down" else "📈"
            lines.append(
                f"{direction_icon} **{a.get('label', a.get('metric'))}**: "
                f"当前 {a.get('current_fmt', '?')} vs 基线 {a.get('baseline_fmt', '?')} "
                f"（偏离 {a.get('deviation_score', 0):.1f}σ）"
            )
        text = "\n".join(lines) if lines else "异常详情见系统日志"

    elif event_type == "draft_resolved":
        # P1: CRM 同步用简洁摘要格式
        intent_s = data.get("intent") or "—"
        emotion_s = data.get("emotion") or "—"
        csat_s = f"{data['csat']:.1f}⭐" if data.get("csat") is not None else "待评"
        title = f"✅ 对话已处置 [{data.get('platform', '?')}] intent={intent_s}"
        text = (
            f"**草稿**: {data.get('draft_id', '?')}\n"
            f"**会话**: {data.get('conversation_id', '?')}\n"
            f"**坐席**: {data.get('agent_id', '?')} · {data.get('action', '?')}\n"
            f"**意图**: {intent_s} · **情绪**: {emotion_s}\n"
            f"**CSAT**: {csat_s} · **风险**: {data.get('risk_level', '?')}\n"
            f"**回复预览**: {data.get('text_preview', '')}"
        )

    elif event_type == "csat_alert":
        title = f"⚠️ 服务质量预警 [{data.get('condition', '?')}]"
        text = (
            f"**预警条件**: {data.get('condition', '?')}\n"
            f"**消息**: {data.get('message', '')}\n"
            f"**阈值**: {data.get('threshold', '?')}\n"
            "[📊 查看简报](/workspace/dashboard)"
        )

    elif event_type == "autoreply_alert":
        kind = str(data.get("kind") or "")
        kind_label = {
            "circuit_open": "熔断（连续失败）",
            "quota_hour": "小时配额耗尽",
            "quota_day": "每日配额耗尽",
        }.get(kind, kind)
        title = f"🚨 自动回复告警：{kind_label}"
        text = (
            f"**平台**: {data.get('platform', '?')}\n"
            f"**账号**: {data.get('account_id', '?')}\n"
            f"**详情**: {data.get('detail', '')}\n"
            "[👥 前往账号管理](/workspace/unified-inbox)"
        )

    elif event_type == "health_alert":
        light = str(data.get("light") or "")
        icon = {"red": "🔴", "yellow": "🟡"}.get(light, "🩺")
        if data.get("recovered"):
            title = "✅ 系统已恢复健康"
            text = (
                f"**状态**: 全部组件恢复正常\n"
                "[🩺 查看健康面板](/dashboard)"
            )
        else:
            probs = data.get("problems") or []
            lines = [f"- {p.get('name','?')}：{p.get('detail','')}" for p in probs[:8]]
            title = f"{icon} 系统健康告警（{light or '异常'}）"
            text = (
                f"**异常组件**: {len(probs)} 项\n"
                + "\n".join(lines) + "\n"
                "[🩺 查看健康面板](/dashboard)"
            )

    elif event_type == "billing_alert":
        anomalies = data.get("anomalies") or []
        if data.get("recovered"):
            title = "✅ 计费异常已解除"
            text = "**状态**: 席位/用量已回到授权额度内\n[💸 查看运营总览](/admin/ops)"
        else:
            lines = [f"- {a.get('message','')}" for a in anomalies[:8]]
            title = "💸 计费异常告警"
            text = (
                f"**异常**: {len(anomalies)} 项\n"
                + "\n".join(lines) + "\n"
                "[💸 查看运营总览](/admin/ops)"
            )

    elif event_type == "escalation":
        title = "🔔 升级告警"
        text = (
            f"**客户**: {data.get('name', '?')}\n"
            f"**等待**: {int((data.get('wait_sec') or 0)//60)} 分钟\n"
            f"**原因**: {data.get('reason', '?')}"
        )

    else:
        title = f"[{event_type}] 事件"
        text = json.dumps(data, ensure_ascii=False)[:300]

    return title, text


# ─── 主类 ────────────────────────────────────────────────────────────────────

class WebhookNotifier:
    """L2：订阅 EventBus，对匹配事件发送企业 IM Webhook 通知。

    Usage::

        notifier = WebhookNotifier(config=cfg_dict)
        asyncio.ensure_future(notifier.run())
        notifier.stop()
    """

    def __init__(self, config: Optional[List[Dict[str, Any]]] = None) -> None:
        self._webhooks: List[Dict[str, Any]] = list(config or [])
        self._rate_limiter = _RateLimiter()
        self._stop_evt = asyncio.Event()
        self._running = False
        self.total_sent: int = 0
        self.total_errors: int = 0

        # 预处理：每条 webhook 配置展开事件匹配规则
        self._matchers: List[Dict[str, Any]] = []
        self._build_matchers()

    def _build_matchers(self) -> None:
        """（重）构建事件匹配规则；供 __init__ 与 reload() 复用。"""
        matchers: List[Dict[str, Any]] = []
        for wh in self._webhooks:
            if wh.get("enabled") is False:
                continue
            events = list(wh.get("events") or ["all"])
            for alias in events:
                rule = _EVENT_ALIASES.get(alias)
                if rule is None:
                    logger.warning("未知 webhook 事件别名: %s", alias)
                    continue
                matchers.append({
                    "url": str(wh.get("url") or ""),
                    "fmt": str(wh.get("format") or "json").lower(),
                    "secret": str(wh.get("secret") or ""),
                    "token": str(wh.get("token") or ""),
                    "target": str(wh.get("target") or wh.get("chat_id") or ""),
                    "name": str(wh.get("name") or "webhook"),
                    "types": rule["types"],          # None → 全部
                    "levels": rule.get("levels"),    # None → 全部
                })
        self._matchers = matchers

    def reload(self, config: Optional[List[Dict[str, Any]]] = None) -> None:
        """运行时热更 webhook 列表（配合后台「告警渠道」面板免重启增删）。"""
        self._webhooks = list(config or [])
        self._build_matchers()
        logger.info("WebhookNotifier 已热更（%d 个 webhook / %d 匹配规则）",
                    len(self._webhooks), len(self._matchers))

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        from src.integrations.shared.event_bus import get_event_bus
        self._running = True
        self._stop_evt.clear()
        bus = get_event_bus()
        queue = bus.subscribe()
        logger.info("WebhookNotifier 已启动（%d 个 webhook 端点）", len(self._webhooks))
        try:
            while not self._stop_evt.is_set():
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=2.0)
                    await self._dispatch(evt)
                except asyncio.TimeoutError:
                    pass  # 正常超时，继续检查 stop_evt
                except Exception:
                    logger.debug("WebhookNotifier event 处理异常（已忽略）", exc_info=True)
        finally:
            bus.unsubscribe(queue)
            self._running = False
            logger.info("WebhookNotifier 已停止")

    def stop(self) -> None:
        self._stop_evt.set()

    # ── 事件分发 ─────────────────────────────────────────────────────────

    async def _dispatch(self, evt: Dict[str, Any]) -> None:
        etype = str(evt.get("type") or "")
        data = dict(evt.get("data") or {})
        level = str(data.get("autopilot_level") or "")

        for m in self._matchers:
            # 匹配 event type
            if m["types"] is not None and etype not in m["types"]:
                continue
            # 匹配 autopilot_level
            if m["levels"] is not None and level not in m["levels"]:
                continue
            # 速率限制 key = (name, event_type, draft_id 或空)
            rate_key = f"{m['name']}:{etype}:{data.get('draft_id','')}"
            if not self._rate_limiter.allow(rate_key):
                logger.debug("Webhook 速率限制跳过: %s", rate_key)
                continue

            await self._send(m, etype, data)

        # 定期清理速率限制记录
        self._rate_limiter.cleanup()

    async def _send(self, matcher: Dict[str, Any], etype: str, data: Dict[str, Any]) -> None:
        fmt = matcher["fmt"]
        secret = matcher["secret"]
        token = matcher.get("token") or ""
        target = matcher.get("target") or ""
        title, text = _build_message(etype, data)

        if fmt in CHAT_FORMATS:
            url = _resolve_chat_endpoint(fmt, matcher["url"], token)
            if not url:
                self.total_errors += 1
                logger.warning("Webhook 跳过 [%s]：%s 渠道缺少 url/token",
                               matcher["name"], fmt)
                return
            if not target:
                self.total_errors += 1
                logger.warning("Webhook 跳过 [%s]：%s 渠道缺少 target",
                               matcher["name"], fmt)
                return
            msg = f"{title}\n{_plainify(text)}".strip()
            body, headers = _build_chat_body(fmt, msg, target, token)
        else:
            url = matcher["url"]
            if not url:
                return
            formatter = _FORMATTERS.get(fmt, _fmt_json)
            body = formatter(title, text, data)
            headers = {"Content-Type": "application/json; charset=utf-8"}
            # 钉钉签名（可选，保留兼容）
            if fmt == "dingtalk" and secret:
                ts, sign = _dingtalk_sign(secret)
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}timestamp={ts}&sign={sign}"

        try:
            await asyncio.get_event_loop().run_in_executor(
                None, self._http_post, url, body, headers
            )
            self.total_sent += 1
            logger.info("Webhook 发送成功 [%s] %s", matcher["name"], etype)
        except Exception as exc:
            self.total_errors += 1
            logger.warning("Webhook 发送失败 [%s]: %s", matcher["name"], exc)

    @staticmethod
    def _http_post(url: str, body: bytes, headers: Optional[Dict[str, str]] = None) -> None:
        """同步 HTTP POST（在 executor 中运行，不阻塞事件循环）。"""
        req = urllib.request.Request(
            url,
            data=body,
            headers=headers or {"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()

    async def send_test(self, webhook: Dict[str, Any], etype: str = "autoreply_alert",
                        data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """对单条 webhook 配置即时发一条测试消息（不经速率限制），返回结果。"""
        rule = _EVENT_ALIASES.get((webhook.get("events") or ["all"])[0]) \
            or _EVENT_ALIASES["all"]
        m = {
            "url": str(webhook.get("url") or ""),
            "fmt": str(webhook.get("format") or "json").lower(),
            "secret": str(webhook.get("secret") or ""),
            "token": str(webhook.get("token") or ""),
            "target": str(webhook.get("target") or webhook.get("chat_id") or ""),
            "name": str(webhook.get("name") or "test"),
            "types": rule["types"], "levels": rule.get("levels"),
        }
        payload = data or {
            "kind": "circuit_open", "platform": "telegram",
            "account_id": "test-account", "detail": "这是一条测试告警（连通性检查）",
        }
        before_err = self.total_errors
        await self._send(m, etype, payload)
        ok = self.total_errors == before_err
        return {"ok": ok, "name": m["name"], "format": m["fmt"]}

    # ── 状态快照 ──────────────────────────────────────────────────────────

    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "webhooks": len(self._webhooks),
            "matchers": len(self._matchers),
            "total_sent": self.total_sent,
            "total_errors": self.total_errors,
        }
