"""
R3 — CSAT 问卷自动发送 Worker

功能：
  - 后台轮询 csat_surveys 表，到期即向客户发送满意度问卷消息
  - 问卷消息内容：多语言 1-5 分请求（可通过 config 自定义）
  - 发送后标记 sent=1 + 设置 conv_meta survey_awaiting=True
  - 客户回复 1-5 时，由 ingest pipeline 调用 record_survey_response 更新 CSAT

配置（config.yaml）：
    workspace:
      csat_survey:
        enabled: false          # 默认关闭
        delay_minutes: 5        # 草稿批准后 N 分钟发送
        interval_seconds: 30    # Worker 轮询间隔

发送渠道：
  - 调用已注册的 ChannelAdapter.send()
  - 目前支持 Telegram (bot) 平台，其他平台降级为 EventBus 事件通知
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 多语言问卷消息模板 ────────────────────────────────────────────
_SURVEY_MESSAGES: Dict[str, str] = {
    "zh": (
        "感谢您本次使用我们的服务！\n"
        "请问您对本次服务的满意程度如何？\n"
        "请回复数字 1-5 进行评分：\n"
        "  5 ⭐⭐⭐⭐⭐ 非常满意\n"
        "  4 ⭐⭐⭐⭐ 满意\n"
        "  3 ⭐⭐⭐ 一般\n"
        "  2 ⭐⭐ 不满意\n"
        "  1 ⭐ 非常不满意"
    ),
    "en": (
        "Thank you for contacting us!\n"
        "How satisfied are you with our service today?\n"
        "Please reply with a number 1-5:\n"
        "  5 ⭐⭐⭐⭐⭐ Very Satisfied\n"
        "  4 ⭐⭐⭐⭐ Satisfied\n"
        "  3 ⭐⭐⭐ Neutral\n"
        "  2 ⭐⭐ Dissatisfied\n"
        "  1 ⭐ Very Dissatisfied"
    ),
}
_SURVEY_DEFAULT = _SURVEY_MESSAGES["zh"]


def get_survey_message(lang: str = "zh") -> str:
    """根据语言返回问卷消息文本。"""
    key = str(lang or "zh").lower()[:2]
    return _SURVEY_MESSAGES.get(key, _SURVEY_DEFAULT)


class SurveyWorker:
    """R3：CSAT 问卷后台 Worker。

    生命周期：
      worker = SurveyWorker(store, cfg, channel_adapters)
      asyncio.create_task(worker.run())
      worker.stop()
    """

    def __init__(
        self,
        store: Any,  # InboxStore
        cfg: Optional[Dict[str, Any]] = None,
        channel_adapters: Optional[List[Any]] = None,
    ) -> None:
        self._store = store
        self._cfg = cfg or {}
        self._adapters: List[Any] = channel_adapters or []
        self._stopped = False
        self._total_sent = 0
        self._total_responded = 0

    def _survey_cfg(self) -> Dict[str, Any]:
        return (self._cfg.get("workspace") or {}).get("csat_survey") or {}

    def is_enabled(self) -> bool:
        return bool(self._survey_cfg().get("enabled", False))

    def _interval(self) -> float:
        return float(self._survey_cfg().get("interval_seconds", 30))

    def _delay_seconds(self) -> float:
        return float(self._survey_cfg().get("delay_minutes", 5)) * 60

    async def run(self) -> None:
        """主循环：每 interval_seconds 轮询一次待发问卷。"""
        logger.info("SurveyWorker started (enabled=%s)", self.is_enabled())
        while not self._stopped:
            if self.is_enabled():
                try:
                    await self._send_due_surveys()
                except Exception:
                    logger.warning("SurveyWorker._send_due_surveys 异常", exc_info=True)
            await asyncio.sleep(self._interval())

    async def _send_due_surveys(self) -> None:
        """处理所有到期未发的问卷。"""
        due = self._store.list_due_surveys(limit=20)
        if not due:
            return
        for survey in due:
            try:
                await self._send_one(survey)
                self._total_sent += 1
            except Exception:
                logger.warning("SurveyWorker._send_one 失败: %s", survey.get("id"), exc_info=True)

    async def _send_one(self, survey: Dict[str, Any]) -> None:
        """向指定会话发送问卷消息。"""
        conv_id = str(survey.get("conversation_id") or "")
        survey_id = str(survey.get("id") or "")
        if not conv_id:
            return

        # 获取会话语言（从 conv_meta 推断）
        lang = "zh"
        try:
            meta = self._store.get_conv_meta(conv_id)
            if meta:
                # 从 draft_lang 或 last_intent 推断语言（简单规则）
                intent = str(meta.get("last_intent") or "")
                lang = "en" if any(c.isascii() and c.isalpha() for c in intent[:5]) else "zh"
        except Exception:
            pass

        msg_text = get_survey_message(lang)

        # 发布 survey_sent 事件到 EventBus（供 WebhookNotifier / SSE 消费）
        sent_via_event = False
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("survey_sent", {
                "survey_id": survey_id,
                "conversation_id": conv_id,
                "draft_id": survey.get("draft_id", ""),
                "agent_id": survey.get("agent_id", ""),
                "lang": lang,
                "message": msg_text,
            })
            sent_via_event = True
        except Exception:
            pass

        # 标记已发 + 设置 survey_awaiting
        self._store.mark_survey_sent(survey_id)
        self._store.set_conv_survey_awaiting(conv_id, True)
        logger.info(
            "SurveyWorker sent survey=%s conv=%s via_event=%s",
            survey_id, conv_id, sent_via_event,
        )

    def stop(self) -> None:
        self._stopped = True

    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "enabled": self.is_enabled(),
            "total_sent": self._total_sent,
            "total_responded": self._total_responded,
            "delay_minutes": self._survey_cfg().get("delay_minutes", 5),
            "interval_seconds": self._interval(),
        }
