"""Messenger Inbox 未读会话扫描（Vision 优先，UI dump 降级）。

输入：当前停在 Messenger Chats 主页的设备截图
输出：UnreadChat 列表（name, preview, time, quality_hint, row_index）

quality_hint 用于下游 score 排序，避免 PoC 阶段总选到第一条 spam。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


INBOX_VISION_PROMPT = (
    "你在分析一张 Facebook Messenger 安卓 App 的 Chats 主页（720x1600 标定）。\n"
    "\n"
    "页面布局（从上到下）：\n"
    "1. 顶部 'messenger' 标题 + 编辑按钮 + Facebook 头像\n"
    "2. 'Ask Meta AI or Search' 搜索栏\n"
    "3. 顶部 banner（'Pin your favorite chats'/'Try Meta AI' 等，可能没有）\n"
    "4. Stories 横向行（'Create story' + 几个圆形头像，可能带 Active 绿点）\n"
    "5. 会话列表（每行：左圆头像 + 名字 + 预览/n new messages + 时间）\n"
    "6. 'People you may know' 区块\n"
    "7. 底部 tab：Chats / Stories / Menu\n"
    "\n"
    "===== 任务 =====\n"
    "识别**未读会话**（preview 里有 'X new messages' / 'X new message' 字样，"
    "或预览文本明显加粗）。\n"
    "对每条未读还要给出：\n"
    "  - y_percent: 该行**头像中心**距离屏幕顶部的位置百分比（0-100，整数；"
    "**这个非常关键**，下游会用 y_percent × 屏幕高 来计算点击坐标）\n"
    "  - quality_hint: 内容质量\n"
    "      friend = 真人朋友日常聊天（短句、emoji、问候、问问题）\n"
    "      unknown = 陌生人主动来消息但内容看着像正常聊天\n"
    "      spam = 明显营销/诈骗/赌博推广（含 'win'/'bonus'/'payout'/'check my'/"
    "赌博/博彩/付款链接/可疑短链）\n"
    "      channel = 公众号/官方号自动推送\n"
    "      group = 群聊（preview 含 'X: ...' 形式发件人前缀）\n"
    "      unsure = 看不出\n"
    "\n"
    "===== 关于 y_percent =====\n"
    "图像顶部=0，图像底部=100。如会话列表第一行头像中心大约在图像高度的 37%，"
    "就填 37。**直接以你看到的图像总高度做分母估算**，不要用其他参考。\n"
    "请给整数百分比。错了下游会点错行，所以请尽量精确。\n"
    "\n"
    "严格输出一行合法 JSON（不要 markdown 包裹）：\n"
    '{"unread":[{"name":"...","preview":"...","time":"...",'
    '"y_percent":N,"quality_hint":"friend|unknown|spam|channel|group|unsure"}],'
    '"total_unread":N}\n'
    "\n"
    "字段规则：\n"
    "- name: 会话对方姓名\n"
    "- preview: 消息预览（保留 emoji）\n"
    "- time: 右侧时间戳\n"
    "- y_percent: **必填**，头像中心 Y 占图像高的百分比（0-100 整数）\n"
    "- 没有未读时返回 {\"unread\":[],\"total_unread\":0}\n"
    "- 顺序按从上到下\n"
    "- 已读会话请勿包含\n"
    "- Stories 行/顶部 banner/'People you may know' 区块**绝不要**当成未读\n"
)


# 本地兜底：vision 没标 quality_hint 时，用关键词扫描自动判
_SPAM_KEYWORDS = [
    "win ", "win!", "bonus", "payout", "click my", "check my",
    "free credit", "free play", "free spin", "promo code", "sign up to",
    "register now", "投注", "彩票", "博彩", "赌博", "代付", "代收",
    "fc8win", "betway", ".cc/", ".cn/?id=", "?promo=", "?ref=",
    "claim your", "limited offer",
]
_FRIEND_KEYWORDS = [
    "hey", "hi", "hello", "你好", "嗨", "hola", "ola",
    "?", "？", "在吗", "在不", "在么", "你在吗",
]


def _local_quality_hint(name: str, preview: str) -> str:
    """本地启发式：preview 看起来像 spam/friend 时给个兜底标签。

    P2-A：优先检测系统事件（"You can now message..." 等），因为它们既不是
    spam 也不是真实 friend 消息，应单独分类。
    """
    p = (preview or "").lower()
    if not p:
        return "unsure"
    # P2-A: 系统事件前置判断（避免被 friend 关键字"hi"误中）
    try:
        from src.integrations.messenger_rpa.chat_reader import (
            is_system_event_text,
        )
        if is_system_event_text(preview):
            return "system_event"
    except Exception:
        pass
    for kw in _SPAM_KEYWORDS:
        if kw.lower() in p:
            return "spam"
    for kw in _FRIEND_KEYWORDS:
        if kw.lower() in p:
            return "friend"
    return "unsure"


@dataclass(frozen=True)
class UnreadChat:
    """一条未读会话条目。"""

    name: str
    preview: str
    time: str
    row_index: int                # 在可见列表里的 0-based 顺序（vision 给）
    y_percent: float = 0.0        # 头像中心相对屏幕高的百分比（vision 给，最关键）
    quality_hint: str = "unsure"  # friend | unknown | spam | channel | group | unsure
    score: float = 0.0            # 下游打分用，越大越优先
    # True：已通过搜索等方式进入会话，勿再对 inbox 行做 tap
    skip_inbox_tap: bool = False
    # ── P23 (2026-05-04) vision 三信号 ──
    # vision INBOX_COMBINED 输出的未读视觉特征。三个全 False 时大概率是
    # vision 误把已读列入 unread[]（特别是 lowmemkill 后 inbox 缓存陈旧）。
    # 默认值（True / False / False）与"老 prompt 只返 name_bold"行为一致，
    # 兼容老 vision 输出。下游 runner 用 unread_signals_count() 决策。
    name_bold: bool = True
    preview_bold: bool = False
    blue_dot: bool = False

    @property
    def unread_signals_count(self) -> int:
        """三信号合计（0~3）。0 = vision 模型偷懒 / 误把已读列入 unread。"""
        return int(self.name_bold) + int(self.preview_bold) + int(self.blue_dot)

    @property
    def is_spam(self) -> bool:
        return self.quality_hint == "spam"

    def click_y(
        self,
        screen_height: int,
        first_row_y_base: int,
        row_height_base: int,
        base_height: int,
    ) -> int:
        """物理 Y = 基准坐标 × (设备高 / 基准高)。

        ★ 经验：vision 给的 y_percent 系统性偏低 ~7%，**不可信**，
        所以本算法**只用 row_index + 标定行高**，忽略 y_percent。
        如果未来 vision 改进了，可以考虑加权融合。
        """
        scale = float(screen_height) / float(base_height)
        y_base = first_row_y_base + self.row_index * row_height_base
        return int(round(y_base * scale))


# 各 quality_hint 的基础分（手动 tunable）
_QUALITY_BASE_SCORE = {
    "friend": 100.0,
    "unknown": 50.0,
    "channel": 20.0,
    "group": 30.0,
    "unsure": 40.0,
    "spam": -50.0,
    # P2-A：系统事件行（"You can now message..."、E2EE 通知等）降到 spam 以下。
    # 会排到最后，若有别的未读会先处理；若整屏只有系统事件则会被 pick 出来，
    # 但下游 chat_reader 里会把 peer 降为 role=none，run_once 自然走
    # no_peer_message 分支退出，不会生成 AI 回复。
    "system_event": -60.0,
}


_MEDIA_ONLY_PREVIEW_PATTERNS = (
    "sent a photo",
    "sent a video",
    "sent an attachment",
    "sent a file",
    "sent a voice",
    "sent a sticker",
    "shared a story",
    "started a call",
    "ended a call",
    "missed call",
    "📷 photo",
    "📹 video",
)


def _is_media_only_preview(preview: str) -> bool:
    """判断 preview 是否仅是媒体占位符（无法看到正文）。"""
    p = (preview or "").strip().lower()
    if not p:
        return False
    return any(pat in p for pat in _MEDIA_ONLY_PREVIEW_PATTERNS)


def _score_chat(
    row_index: int, quality_hint: str, preview: str = ""
) -> float:
    """综合打分：基础分 - 位置惩罚（越靠下越次要）。

    ★ 媒体占位预览（'sent a photo' / 'sent a sticker' …）→ 额外 -25 分，
       因为 inbox 看不到正文，无法做 spam 检测；先让有正文 preview 的会话先回。
       开 thread 后还能用 msg_level_spam_skip 兜底。
    """
    base = _QUALITY_BASE_SCORE.get(quality_hint, 0.0)
    pos_penalty = float(row_index) * 2.0
    media_penalty = 25.0 if _is_media_only_preview(preview) else 0.0
    return base - pos_penalty - media_penalty


def _parse_inbox_json(raw: str) -> Optional[Dict[str, Any]]:
    """解析 vision 返回的 JSON；失败返回 None。

    与 combined_vision 共用 ``parse_vision_json_loose``（截断/未闭合引号修复 + 失败落盘）。
    """
    from src.integrations.messenger_rpa.vision_json_repair import (
        parse_vision_json_loose,
    )

    return parse_vision_json_loose(raw, dump_label="inbox_scanner", write_dump=True)


async def scan_inbox_vision(
    image_path: str,
    *,
    vision_cfg: Dict[str, Any],
    global_vision: Dict[str, Any],
    prompt_override: Optional[str] = None,
    skip_spam: bool = True,
) -> Tuple[List[UnreadChat], str]:
    """用 vision 扫一张 Inbox 截图，返回**按 score 排序**的未读列表 + 后端 tag。

    永不抛异常，失败返回 ([], 'error:...').
    skip_spam=True 时直接过滤掉 quality_hint=spam 的会话。
    """
    try:
        from src.vision_client import VisionClient
    except Exception as ex:
        return [], f"error:vision_import_failed:{ex}"

    prompt = prompt_override or INBOX_VISION_PROMPT
    try:
        text, tag = await VisionClient.describe_image_with_ollama_zhipu_fallback(
            merged_config=vision_cfg,
            global_vision=global_vision,
            image_path=image_path,
            prompt=prompt,
        )
    except Exception as ex:
        return [], f"error:vision_call_failed:{ex}"

    if not text:
        return [], f"empty:{tag}"

    parsed = _parse_inbox_json(text)
    if not parsed:
        return [], f"parse_failed:{tag}"

    valid_hints = {
        "friend", "unknown", "spam", "channel", "group", "unsure",
        "system_event",  # P2-A
    }

    raw_rows: List[UnreadChat] = []
    for i, raw in enumerate(parsed.get("unread") or []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        preview = str(raw.get("preview") or "").strip()
        time_s = str(raw.get("time") or "").strip()
        hint = str(raw.get("quality_hint") or "").strip().lower()
        if hint not in valid_hints:
            hint = _local_quality_hint(name, preview)
        # ★ 本地 spam 关键词命中时**强制覆盖** vision 的 friend/unknown 误判
        # （PoC 期间见过 Rodel 的 "fc8win.com" 链接 vision 标 friend，需要兜底）
        local_hint = _local_quality_hint(name, preview)
        if local_hint == "spam" and hint in ("friend", "unknown", "unsure"):
            hint = "spam"
        # P2-A：vision 容易把 "You can now message and call each other" 这类
        # 系统事件误当正常 friend 消息，用本地关键字扫描强制覆盖
        if local_hint == "system_event" and hint in (
            "friend", "unknown", "unsure",
        ):
            hint = "system_event"
        try:
            y_pct = float(raw.get("y_percent") or 0)
        except (TypeError, ValueError):
            y_pct = 0.0
        if not name:
            continue
        raw_rows.append(
            UnreadChat(
                name=name,
                preview=preview,
                time=time_s,
                row_index=i,
                y_percent=y_pct,
                quality_hint=hint,
                score=_score_chat(i, hint, preview),
            )
        )

    # 过滤 spam（可选）
    if skip_spam:
        before = len(raw_rows)
        raw_rows = [r for r in raw_rows if r.quality_hint != "spam"]
        if before != len(raw_rows):
            logger.info(
                "[inbox_scanner] 过滤 spam: %d -> %d", before, len(raw_rows)
            )

    # 按 score 排序
    raw_rows.sort(key=lambda r: r.score, reverse=True)
    return raw_rows, tag
