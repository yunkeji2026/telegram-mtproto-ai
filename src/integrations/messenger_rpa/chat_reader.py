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
    "★★★ 颜色判定是【绝对硬规则】，优先级高于位置／长度／内容 ★★★\n"
    "  - 蓝色气泡（Messenger 主蓝 #0084FF 或同色系亮蓝/紫）→ 必定 role=self；\n"
    "    无论它在屏幕左/中/右，无论一行还是多行，无论内容像不像对方在说话。\n"
    "  - 灰色气泡（#E4E6EB 或同色系亮灰/米白；深色模式约 #2A2D2F）→ 必定 role=peer。\n"
    "  - 反例：蓝色长气泡占了大半屏宽、左边界距屏幕左侧只剩一点 → 仍是 self；\n"
    "    蓝色气泡里是问候 / 道歉 / 提问 'I am heading home for now' 等容易看着像\n"
    "    对方话术 → 颜色蓝就是 self，不要因内容像 peer 就反转。\n"
    "\n"
    "信息分类：\n"
    "A) 对方消息（role=peer）候选：\n"
    "   - 灰色圆角气泡（通常居左；深色模式 #2A2D2F 灰）内的文字\n"
    "   - 左侧 Meta 贴纸（无气泡框，浮在背景）\n"
    "   - 左侧图片缩略图（带 link preview 卡片或纯图）\n"
    "   - 左侧 link preview 卡片（蓝色边框 + 标题/域名）\n"
    "   - 左侧语音条 / 文件。语音条通常有播放三角、波形/进度条、时长（如 0:06），"
    "如果它最靠近底部输入框，必须返回 kind=voice\n"
    "B) 己方消息（role=self）：蓝色气泡（通常居右） / 蓝色贴纸 / 自己头像旁的图片；"
    "如果可访问性文本含 'You:' / '你:' / 'Me:' 前缀，也必须判为 self\n"
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
    "2. 选出**垂直坐标最大（最靠近底部输入框）那一条**；"
    "若底部是语音条，不要回头选择上方旧文字\n"
    "3. 按其 role 和 kind 输出\n"
    "\n"
    "严格输出一行合法 JSON（不要 markdown 包裹）：\n"
    '{"role":"peer|self|none",'
    '"kind":"text|image|video|gif|sticker|animated_sticker|voice|file|link|other",'
    '"content":"...","desc":"..."}\n'
    "\n"
    "字段规则：\n"
    "- role=peer → 最底部对方；role=self → 最底部己方；role=none → 找不到\n"
    "- 如果最底部是 role=self，不要向上寻找旧的 peer 消息来回复\n"
    "- kind=text：content 填原文（保留 emoji、换行、URL），desc 留空\n"
    "- kind=link：content 填 URL，desc ≤15 中文字描述卡片标题/域名\n"
    "- kind=image：静态照片/截图。content 留空，desc ≤20 中文字描述图片内容（如 '虚假赌博平台付款截图'）\n"
    "  ⚠️ 若消息本体是照片/截图，即使图里有 OCR 文字，也仍返回 kind=image；"
    "不要把图片里的文字当成 content。\n"
    "- kind=video：视频（缩略图覆盖**播放三角**或时长标 0:08 等）。content 留空，desc 描述画面+若可见的时长\n"
    "- kind=gif：GIF 动图（角标 'GIF' 字样 / 自动播放循环动画）。content 留空，desc 简述动作\n"
    "- kind=sticker：静态贴纸（无气泡浮在背景）。content 留空，desc 简述\n"
    "- kind=animated_sticker：会动的贴纸（角标小播放标志或动画感图层）。content 留空，desc 简述动作\n"
    "- kind=voice/file：content 留空，desc 简述\n"
    "\n"
    "★ 媒体 desc 兜底原则（image/video/gif/sticker/animated_sticker/voice/file）：\n"
    "  desc 永远不能为空字符串。即使主体很模糊：宁可粗糙也别空。最低标准：\n"
    "  - 至少给出主体类型（人 / 自拍 / 风景 / 食物 / 截图 / 文档 / 表情 / 动物 / 卡通角色等）\n"
    "  - 看不清细节就只写主体类型一个词（例：'自拍' / '截图' / '动物' / 'meme 表情包'）\n"
    "  - 完全没有视觉信息也至少写 '看不清的图片'，不要返回空 desc。\n"
)


@dataclass(frozen=True)
class PeerMessage:
    """vision 解析后的对方/己方最后一条消息。"""

    role: str  # peer | self | none
    # P1-A3 (2026-05-04): 扩展媒体细分 — video / gif / animated_sticker
    kind: str  # text | image | video | gif | sticker | animated_sticker | voice | file | link | other | system_event
    content: str
    desc: str
    raw: str  # 原始 vision 返回，便于审计

    @property
    def is_peer_text(self) -> bool:
        return self.role == "peer" and self.kind in ("text", "link")

    @property
    def is_peer_anything(self) -> bool:
        return self.role == "peer"

    @property
    def is_system_event(self) -> bool:
        """P2-A: 朋友接受 / 加好友提示等系统事件，不应触发 AI 回复。"""
        return self.kind == "system_event"

    def to_text_for_ai(
        self,
        *,
        caption: Optional[str] = None,
        sticker_category: Optional[str] = None,
        fusion_hint: Optional[str] = None,
    ) -> str:
        """转成单行字符串供下游 SkillManager.process_message 消费。

        P1-A1/A2/1.4 (2026-05-04) 增强：
          - caption: image/video/gif/sticker 的详细描述（80字 vision caption），优先于 desc
          - sticker_category: 5 类标签（happy/love/sad/angry/cute），拼到 [贴纸·tag] 让 LLM 知道情绪
          - fusion_hint: 上下文提示行（如 "上一句 peer 说'看看我的照片'"），帮 LLM 判断意图
        默认 None 时行为完全等价旧调用。
        """
        if self.kind == "text":
            return self.content.strip()
        if self.kind == "link":
            url = self.content.strip()
            return f"[链接] {self.desc} {url}".strip()
        # 媒体类：caption 优先于 desc
        body = (caption or self.desc or "").strip()
        prefix_map = {
            "image": "图片",
            "video": "视频",
            "gif": "GIF",
            "sticker": "贴纸",
            "animated_sticker": "动态贴纸",
            "voice": "语音",
            "file": "文件",
        }
        prefix = prefix_map.get(self.kind, self.kind)
        if self.kind in ("sticker", "animated_sticker") and sticker_category:
            line = f"[{prefix}·{sticker_category}] {body}".strip()
        else:
            line = f"[{prefix}] {body}".strip()
        if fusion_hint:
            line = f"{line}\n[上下文提示] {fusion_hint.strip()}"
        return line

    # ── HIGH-confidence keywords：一次命中即建议**永久 skip** ──
    # 赌博品牌/域名 + 中文赌博词 + IM 引流链接 + 明显引流参数
    _SPAM_KW_HIGH = (
        # 赌博品牌/域名
        "fc8win", "betway", "1xbet", "sportingbet", "888.com",
        # 中文赌博/支付通道（非客服业务话术，是营销/诈骗）
        "投注", "彩票", "博彩", "赌博",
        # IM 引流（用户主动让你换平台 = 通常诈骗或竞品引流）
        "https://t.me/", "wa.me/",
        # 引流参数片段
        ".cc/?id=", ".cc/?promo=",
        "?promo=", "?id=4", "?id=5",
    )

    # ── LOW-confidence keywords：单次跳过当前消息，**不入永久 skip 表** ──
    # 这些词在合法消息也会偶发（如客户说"check my order"），不该永久封杀
    _SPAM_KW_LOW = (
        "win ", "win!", "bonus", "payout", "click my", "check my",
        "free credit", "free play", "free spin", "promo code",
        "register now", "claim your", "limited offer",
        "代付", "代收",        # 业务上也可能是客户提问，不一刀切
        "?ref=",                # 可能是合法 referral
    )

    def spam_match(self) -> tuple[bool, str, str]:
        """三元组返回 (hit, level, keyword)。
        level: "high" → 永久 skip，"low" → 单次跳过，"" → 未命中。
        """
        s = (self.content + " " + self.desc).lower()
        if not s.strip():
            return (False, "", "")
        for kw in self._SPAM_KW_HIGH:
            if kw in s:
                return (True, "high", kw)
        for kw in self._SPAM_KW_LOW:
            if kw in s:
                return (True, "low", kw)
        return (False, "", "")

    @property
    def is_likely_spam(self) -> bool:
        """向后兼容：返 bool，命中（含 LOW）即 True。
        新代码用 `spam_match()` 拿分级信号。
        """
        hit, _level, _kw = self.spam_match()
        return hit


# ── P2-A：系统事件识别（朋友请求接受通知等，不能作为回复目标）──
# Messenger 会在好友请求接受后插入系统行，它看起来像一条消息但不是对方主动发的。
# 这些字符串在多语言下的常见变体一并覆盖。
# ★ inbox preview 经常被截断为 "You can now message and c..."（约 25 字），
#   所以这里前缀短于完整短语，仍能命中。但是必须确保前缀不会误命中真正的
#   用户消息（如 "you can now stop pretending..."），所以前缀至少 18 字。
_SYSTEM_EVENT_PATTERNS = (
    # English（前缀 → 全文都能命中）
    "you can now message and",         # 截断 + 完整
    "you can now message,",            # 部分版本逗号分隔
    "you can now call each other",
    "say hi to ",
    "you're now connected on",
    "you are now connected on",
    "you are now friends",
    "you became friends",
    "started the chat",
    "messages and calls are secured",  # E2EE 通知
    "end-to-end encrypted",
    # 简体中文（前缀允许截断）
    "你们现在可以互相发",
    "你们已经成为好友",
    "你已成为好友",
    "消息和通话使用端到端加密",
    "消息和通话使用端到端",
    # 繁体中文
    "你們現在可以互傳",
    "你們現在是好友",
    # 日本語（更精确的系统事件搭配，避免误中真人聊天）
    "お互いにメッセージと通話ができ",  # "お互いにメッセージと通話ができるようになりました"
    "お互いにメッセージが送れる",
    "メッセージと通話はエンドツーエンド",
    "がメッセンジャーで友達になりました",  # "X さんがメッセンジャーで友達になりました"
    # 韓国語
    "이제 서로 메시지를 보내",
    "이제 친구가 되었습니다",
)


def is_system_event_text(text: str) -> bool:
    """判断一段文本是否是 Messenger 系统事件行（好友接受 / E2EE 通知等）。

    用于 inbox preview 与 thread 最底部消息两个位置的双重过滤。
    """
    low = (text or "").strip().lower()
    if not low:
        return False
    return any(pat in low for pat in _SYSTEM_EVENT_PATTERNS)


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
    if kind not in (
        "text", "image", "sticker", "voice", "file", "link", "other",
        "system_event",
    ):
        kind = "other"

    content = str(parsed.get("content") or "").strip()
    desc = str(parsed.get("desc") or "").strip()

    # ── P2-A：系统事件覆盖 ──
    # vision 有时把 "You can now message and call each other" 等系统行当成一条
    # peer text 返回（因为它居中显示、字号与普通消息接近）。这里兜底识别，
    # 强制把 role 降为 none 并标 kind='system_event'，下游自然走 no_peer_message
    # 分支跳过，避免对"加好友通知"生成 AI 回复。
    if role == "peer" and kind in ("text", "link", "other") and is_system_event_text(
        content + " " + desc,
    ):
        logger.info(
            "[chat_reader] system event at thread bottom detected: "
            "role=peer kind=%s content=%r → downgrade role=none kind=system_event",
            kind, content[:80],
        )
        role = "none"
        kind = "system_event"

    # ── P-VOICE-FIX (2026-05-04)：vision 把 voice bubble 的 a11y text
    # 误识为 text content（如 "音声メッセージを送ってください" /
    # "voice message" / "sent a voice" 等），导致 _maybe_media_ack 不命中
    # voice 分支。这里 keyword 兜底覆盖 kind=text → voice。
    _voice_a11y_markers = (
        "音声メッセージ", "ボイスメッセージ", "ボイスノート", "voice message",
        "voice note", "audio message", "sent a voice", "语音消息", "語音訊息",
        "送了一条语音", "sent an audio",
    )
    if role == "peer" and kind == "text":
        _combo = (content + " " + desc).lower()
        if any(m.lower() in _combo for m in _voice_a11y_markers):
            logger.info(
                "[chat_reader] vision misread voice bubble as text "
                "(content contained voice-marker keyword): %r → kind=voice",
                content[:80],
            )
            kind = "voice"
            # content 是 vision 看 a11y text 编出来的，对 LLM 无价值；放 desc
            desc = (desc or content)[:80]
            content = ""

    msg = PeerMessage(
        role=role,
        kind=kind,
        content=content,
        desc=desc,
        raw=text,
    )
    return msg, tag
