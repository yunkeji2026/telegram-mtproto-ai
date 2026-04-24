"""Webhook 通知器 — 配置变更/审计事件推送到外部系统"""

import asyncio
import json
import logging
import time
from typing import Dict, List, Optional, Callable

logger = logging.getLogger("Webhook")


class WebhookNotifier:

    def __init__(self, config: dict):
        self._endpoints: List[dict] = config.get("webhooks", [])
        self._enabled = config.get("enabled", False) and bool(self._endpoints)
        self._timeout = config.get("timeout", 10)
        self._retry = config.get("retry", 1)
        if self._enabled:
            logger.info("Webhook 通知已启用，%d 个端点", len(self._endpoints))

    @property
    def enabled(self) -> bool:
        return self._enabled

    def notify(self, event_type: str, data: dict):
        if not self._enabled:
            return
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.create_task(self._dispatch(event_type, data))
            else:
                loop.run_until_complete(self._dispatch(event_type, data))
        except RuntimeError:
            pass

    async def _dispatch(self, event_type: str, data: dict):
        payload = {
            "event": event_type,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "data": data,
        }
        for ep in self._endpoints:
            url = ep.get("url", "")
            fmt = ep.get("format", "json")
            if not url:
                continue
            body = self._format_payload(payload, fmt, ep)
            await self._send(url, body, ep.get("headers", {}))

    def _format_payload(self, payload: dict, fmt: str, ep: dict) -> dict:
        if fmt == "slack":
            text = self._to_text(payload)
            return {"text": text}
        if fmt == "wechat":
            text = self._to_text(payload)
            return {"msgtype": "text", "text": {"content": text}}
        if fmt == "telegram":
            chat_id = ep.get("chat_id", "")
            text = self._to_text(payload)
            return {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        return payload

    @staticmethod
    def _to_text(payload: dict) -> str:
        d = payload.get("data", {})
        lines = [
            f"[Bot 配置变更] {payload.get('event', '')}",
            f"时间: {payload.get('timestamp', '')}",
        ]
        if d.get("action"):
            lines.append(f"操作: {d['action']}")
        if d.get("target"):
            lines.append(f"目标: {d['target']}")
        if d.get("user_id"):
            lines.append(f"操作人: {d['user_id']}")
        if d.get("new_val"):
            lines.append(f"新值: {d['new_val'][:100]}")
        return "\n".join(lines)

    async def _send(self, url: str, body: dict, headers: dict):
        import aiohttp
        merged_headers = {"Content-Type": "application/json"}
        merged_headers.update(headers)
        for attempt in range(self._retry + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, json=body, headers=merged_headers,
                        timeout=aiohttp.ClientTimeout(total=self._timeout),
                    ) as resp:
                        if resp.status < 300:
                            logger.debug("Webhook 发送成功: %s → %d", url[:50], resp.status)
                            return
                        logger.warning("Webhook 响应异常: %s → %d", url[:50], resp.status)
            except ImportError:
                try:
                    import urllib.request
                    req = urllib.request.Request(
                        url, data=json.dumps(body).encode(),
                        headers=merged_headers, method="POST",
                    )
                    urllib.request.urlopen(req, timeout=self._timeout)
                    return
                except Exception as e2:
                    logger.warning("Webhook fallback 发送失败: %s", e2)
                    return
            except Exception as e:
                if attempt < self._retry:
                    await asyncio.sleep(2 ** attempt)
                else:
                    logger.warning("Webhook 发送失败 (%d 次重试): %s → %s", self._retry, url[:50], e)
