"""
S3 — 全链路请求追踪

功能：
  - 为每条消息在 ingest 阶段生成/继承 trace_id（格式：trc_{16位hex}）
  - trace_id 随对话元数据持久化，随草稿生成传播
  - 时间线 API 重建：ingest → draft_created → audit → survey 完整链路
  - 轻量实现：无分布式追踪依赖，纯 SQLite 查询重建时间线

trace_id 格式：trc_{os.urandom(8).hex()} → e.g. trc_a3f92b1d4e6c0823
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional


def new_trace_id() -> str:
    """生成新 trace_id（16位 hex，加 trc_ 前缀便于日志过滤）。"""
    return "trc_" + os.urandom(8).hex()


def get_or_create_trace_id(existing: Optional[str] = None) -> str:
    """若已有 trace_id 则复用，否则新建（用于 ingest 继承已有对话 trace）。"""
    if existing and existing.startswith("trc_") and len(existing) >= 10:
        return existing
    return new_trace_id()


class TraceTimeline:
    """从 InboxStore 重建指定 trace_id 的完整时间线。"""

    def __init__(self, store: Any) -> None:
        self._s = store

    def build(self, trace_id: str) -> Dict[str, Any]:
        """重建 trace_id 的完整调用链时间线。

        返回结构：
        {
          "trace_id": "trc_xxxx",
          "conversation_id": "...",
          "events": [
            {"ts": float, "type": "ingest",   "detail": {...}},
            {"ts": float, "type": "draft",    "detail": {...}},
            {"ts": float, "type": "audit",    "detail": {...}},
            {"ts": float, "type": "survey",   "detail": {...}},
          ],
          "span_ms": float,   # 首尾时间跨度（毫秒）
          "total_events": int,
        }
        """
        if not trace_id or not trace_id.startswith("trc_"):
            return {"error": "invalid trace_id format", "trace_id": trace_id}

        events: List[Dict[str, Any]] = []

        # 1. 查对话 meta（ingest 事件）
        conv_id = ""
        with self._s._lock:
            row = self._s._conn.execute(
                "SELECT * FROM conversation_meta WHERE trace_id=? LIMIT 1",
                (trace_id,),
            ).fetchone()
        if row:
            d = dict(row)
            conv_id = d.get("conversation_id", "")
            events.append({
                "ts":   float(d.get("created_at") or d.get("updated_at") or time.time()),
                "type": "ingest",
                "detail": {
                    "conversation_id": conv_id,
                    "platform": d.get("platform", ""),
                    "msg_count": d.get("msg_count", 0),
                    "last_intent": d.get("last_intent", ""),
                    "last_emotion": d.get("last_emotion", ""),
                },
            })

        if not conv_id:
            # trace_id 不存在
            return {
                "trace_id": trace_id,
                "found": False,
                "events": [],
                "span_ms": 0,
                "total_events": 0,
            }

        # 2. 草稿（可能多个）
        with self._s._lock:
            draft_rows = self._s._conn.execute(
                "SELECT * FROM reply_drafts WHERE trace_id=? OR conversation_id=? ORDER BY created_at",
                (trace_id, conv_id),
            ).fetchall()
        for dr in draft_rows:
            d = dict(dr)
            events.append({
                "ts":   float(d.get("created_at") or 0),
                "type": "draft_created",
                "detail": {
                    "draft_id": d.get("id", ""),
                    "autopilot": d.get("autopilot_level", ""),
                    "risk": d.get("risk_level", ""),
                    "quality_score": d.get("quality_score", -1),
                    "text_len": len(str(d.get("draft_text") or "")),
                },
            })

        # 3. 审计日志
        with self._s._lock:
            audit_rows = self._s._conn.execute(
                "SELECT * FROM draft_audit_log WHERE conversation_id=? ORDER BY ts",
                (conv_id,),
            ).fetchall()
        for ar in audit_rows:
            d = dict(ar)
            events.append({
                "ts":   float(d.get("ts") or 0),
                "type": "audit",
                "detail": {
                    "draft_id": d.get("draft_id", ""),
                    "action": d.get("action", ""),
                    "agent_id": d.get("agent_id", ""),
                    "risk_level": d.get("risk_level", ""),
                },
            })

        # 4. CSAT 问卷
        with self._s._lock:
            survey_rows = self._s._conn.execute(
                "SELECT * FROM csat_surveys WHERE conversation_id=? ORDER BY created_at",
                (conv_id,),
            ).fetchall()
        for sr in survey_rows:
            d = dict(sr)
            events.append({
                "ts":   float(d.get("created_at") or 0),
                "type": "survey_scheduled",
                "detail": {
                    "survey_id": d.get("id", ""),
                    "sent": bool(d.get("sent")),
                    "response_score": d.get("response_score"),
                    "send_at": d.get("send_at"),
                },
            })

        # 按时间排序
        events.sort(key=lambda e: e["ts"])

        span_ms = 0.0
        if len(events) >= 2:
            span_ms = (events[-1]["ts"] - events[0]["ts"]) * 1000

        return {
            "trace_id": trace_id,
            "found": True,
            "conversation_id": conv_id,
            "events": events,
            "span_ms": round(span_ms, 1),
            "total_events": len(events),
        }
