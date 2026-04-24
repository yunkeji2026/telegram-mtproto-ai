"""Messenger 1v1 会话页对方最后一条消息识别（Vision 优先）。

输入：当前停在 Messenger Thread view 的设备截图
输出：PeerMessage（role/kind/content/desc）
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Vision Prompt（已在 d113 上实测正确识别 link/text 类消息） ──
THREAD_VISION_PROMPT = (
    "你在分析一张 Facebook Messenger 1v1 私聊会话截图（Android, 720x1600 标定）。\n"
    "\n"
    "信息分类：\n"
    "A) 对方消息（role=peer）候选：\n"
    "   - 左侧深灰色圆角气泡（深色模式 #2A2D2F 灰）内的文字\n"
    "   - 左侧 Meta 贴纸（无气泡框，浮在背景）\n"
    "   - 左侧图片缩略图（带 link preview 卡片或纯图）\n"
    "   - 左侧 link preview 卡片（蓝色边框 + 标题/域名）\n"
    "   - 左侧语音条 / 文件\n"
    "B) 己方消息（role=self）：右侧蓝色气泡 / 右侧贴纸 / 右侧图片，带头像在右下角\n"
    "C) 必须忽略：\n"
    "   - 顶部联系人栏（头像 + 名字 + Active 时间 + 通话/视频按钮 + i 信息按钮）\n"
    "   - E2EE 提示「Messages and calls are secured with end-to-end encryption」\n"
    "   - 中间日期分隔符（'SAT AT 3:30 AM' / 'YESTERDAY' / 'Unread messages' 等）\n"
    "   - 底部输入栏（+, 相机, 图片, 麦克风, Message 输入框, 表情, 👍）\n"
    "   - Bloks onboarding modal（'Note reactions...', 'Previews are on'）\n"
    "   - 系统状态栏（时间、电池、信号）\n"
    "   - 屏幕底部三键导航\n"
    "\n"
    "步骤：\n"
    "1. 在内心列出截图里所有可见的 A/B 类消息\n"
    "2. 选出**垂直坐标最大（最靠近底部输入框）那一条**\n"
    "3. 按其 role 和 kind 输出\n"
    "\n"
    "严格输出一行合法 JSON（不要 markdown 包裹）：\n"
    '{"role":"peer|self|none","kind":"text|image|sticker|voice|file|link|other",'
    '"content":"...","desc":"..."}\n'
    "\n"
    "字段规则：\n"
    "- role=peer → 最底部对方；role=self → 最底部己方；role=none → 找不到\n"
    "- kind=text：content 填原文（保留 emoji、换行、URL），desc 留空\n"
    "- kind=link：content 填 URL，desc ≤15 中文字描述卡片标题/域名\n"
    "- kind=image：content 留空，desc ≤20 中文字描述图片内容（如 '虚假赌博平台付款截图'）\n"
    "- kind=sticker/voice/file：content 留空，desc 简述\n"
)


@dataclass(frozen=True)
class PeerMessage:
    """vision 解析后的对方/己方最后一条消息。"""

    role: str  # peer | self | none
    kind: str  # text | image | sticker | voice | file | link | other
    content: str
    desc: str
    raw: str  # 原始 vision 返回，便于审计

    @property
    def is_peer_text(self) -> bool:
        return self.role == "peer" and self.kind in ("text", "link")

    @property
    def is_peer_anything(self) -> bool:
        return self.role == "peer"

    def to_text_for_ai(self) -> str:
        """转成单行字符串供下游 SkillManager.process_message 消费。"""
        if self.kind == "text":
            return self.content.strip()
        if self.kind == "link":
            url = self.content.strip()
            return f"[链接] {self.desc} {url}".strip()
        if self.kind == "image":
            return f"[图片] {self.desc}".strip()
        if self.kind == "sticker":
            return f"[贴纸] {self.desc}".strip()
        if self.kind == "voice":
            return f"[语音] {self.desc}".strip()
        if self.kind == "file":
            return f"[文件] {self.desc}".strip()
        return f"[{self.kind}] {self.desc or self.content}".strip()

    @property
    def is_likely_spam(self) -> bool:
        """对消息正文做一次廉价的关键词扫描，判定是否营销/赌博/诈骗。

        本地兜底，命中即建议**直接跳过回复**，不耗 AI tokens。
        """
        s = (self.content + " " + self.desc).lower()
        if not s.strip():
            return False
        spam_keywords = [
            "win ", "win!", "bonus", "payout", "click my", "check my",
            "free credit", "free play", "free spin", "promo code",
            "register now", "claim your", "limited offer",
            "投注", "彩票", "博彩", "赌博", "代付", "代收",
            "fc8win", "betway", "1xbet", "sportingbet",
            "888.com", ".cc/?id=", ".cc/?promo=", "?ref=",
            "?promo=", "?id=4", "?id=5",
            "https://t.me/", "wa.me/",
        ]
        for kw in spam_keywords:
            if kw in s:
                return True
        return False


def fingerprint(msg: PeerMessage) -> str:
    """生成 per-message 指纹用于 per-chat 去重（避免重复回复同一消息）。"""
    h = hashlib.sha256()
    h.update(msg.role.encode("utf-8"))
    h.update(b"|")
    h.update(msg.kind.encode("utf-8"))
    h.update(b"|")
    h.update((msg.content or "")[:500].encode("utf-8"))
    h.update(b"|")
    h.update((msg.desc or "")[:200].encode("utf-8"))
    return h.hexdigest()[:16]


def _parse_thread_json(raw: str) -> Optional[Dict[str, Any]]:
    s = (raw or "").strip()
    if s.startswith("```"):
        lines = [ln for ln in s.splitlines() if not ln.strip().startswith("```")]
        s = "\n".join(lines).strip()
    try:
        return json.loads(s)
    except Exception as ex:
        logger.warning("thread vision JSON 解析失败: %s | raw=%r", ex, s[:200])
        return None


async def read_peer_message_vision(
    image_path: str,
    *,
    vision_cfg: Dict[str, Any],
    global_vision: Dict[str, Any],
    prompt_override: Optional[str] = None,
) -> Tuple[Optional[PeerMessage], str]:
    """读取 1v1 会话页中"最底部"那条消息。

    永不抛异常；失败返回 (None, 'error:...').
    """
    try:
        from src.vision_client import VisionClient
    except Exception as ex:
        return None, f"error:vision_import_failed:{ex}"

    prompt = prompt_override or THREAD_VISION_PROMPT
    try:
        text, tag = await VisionClient.describe_image_with_ollama_zhipu_fallback(
            merged_config=vision_cfg,
            global_vision=global_vision,
            image_path=image_path,
            prompt=prompt,
        )
    except Exception as ex:
        return None, f"error:vision_call_failed:{ex}"

    if not text:
        return None, f"empty:{tag}"

    parsed = _parse_thread_json(text)
    if not parsed:
        return None, f"parse_failed:{tag}"

    role = str(parsed.get("role") or "none").lower().strip()
    kind = str(parsed.get("kind") or "other").lower().strip()
    if role not in ("peer", "self", "none"):
        role = "none"
    if kind not in ("text", "image", "sticker", "voice", "file", "link", "other"):
        kind = "other"

    msg = PeerMessage(
        role=role,
        kind=kind,
        content=str(parsed.get("content") or "").strip(),
        desc=str(parsed.get("desc") or "").strip(),
        raw=text,
    )
    return msg, tag
