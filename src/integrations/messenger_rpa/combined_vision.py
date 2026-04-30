"""Inbox/Thread 截图的"一次 vision 拿全部"合并 prompt。

为什么：v0.1 每次 run_once 要 4 次 vision（inbox guard + inbox content + thread guard
+ thread content），共 60-100 秒，是 RPA 单次延迟的最大瓶颈。

合并后每次 run_once 只剩 2 次 vision（inbox combined + thread combined），延迟降到 30-45 秒，
而且 token 总量不见得多很多（一张图 + 单 prompt vs 一张图 + 双 prompt，图费一样，prompt 只多几行）。

接口：
- analyze_inbox_combined(image_path, ...) -> InboxCombinedResult
    - guard: GuardScreen
    - rows: List[UnreadChat]
- analyze_thread_combined(image_path, ...) -> ThreadCombinedResult
    - guard: GuardScreen
    - peer: Optional[PeerMessage]

如果后续看 token 成本不划算，可以一键回退到分离调用（runner.use_combined_vision=False）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from src.integrations.messenger_rpa.bloks_navigator import (
    ACTION_NEED_HUMAN,
    ACTION_NONE,
    ACTION_PRESS_BACK,
    ACTION_TAP_CLOSE_X,
    ACTION_TAP_OK,
    GuardScreen,
)
from src.integrations.messenger_rpa.chat_reader import PeerMessage
from src.integrations.messenger_rpa.inbox_scanner import (
    UnreadChat,
    _local_quality_hint,  # 复用 spam 兜底
    _score_chat,  # 复用打分
)
from src.integrations.messenger_rpa.vision_json_repair import parse_vision_json_loose

logger = logging.getLogger(__name__)


# ── Prompt：一次拿 guard + inbox 内容 ─────────────────────────────
INBOX_COMBINED_PROMPT = (
    "你在分析一张 Facebook Messenger 安卓 App 的 Chats 主页（720x1600 标定）。\n"
    "\n"
    "请同时回答两个任务，整合到一行 JSON 输出。\n"
    "\n"
    "===== 任务 A：守卫屏识别 =====\n"
    "判断屏幕是否被**真正悬浮遮挡式 modal/dialog/bottom-sheet** 覆盖。\n"
    "真 modal 的 4 个必要视觉特征（至少同时出现 3 个）：\n"
    "  M1) 背景内容被半透明黑色遮罩覆盖（背后图层明显发暗 / 模糊）\n"
    "  M2) 浮层有明显的圆角白卡 / 圆角深灰卡，边缘脱离屏幕（非满屏）\n"
    "  M3) 浮层中央或下方有显眼大按钮（'OK', 'Continue', 'Allow', 'Don't Allow'）\n"
    "  M4) 浮层右上角有 X 关闭图标 或 顶部有 drag handle 横线\n"
    "\n"
    "假阳性陷阱（这些不是 modal，guard.type=none）：\n"
    "  ❌ Chats 顶部的 'Pin your favorite chats' / 'Try Meta AI' / 'Stories' banner\n"
    "  ❌ 普通会话列表项\n"
    "  ❌ 顶部搜索框 / 底部 tab bar\n"
    "\n"
    "===== 任务 B：未读会话扫描 =====\n"
    "实测此设备的 Messenger 版本**未必显示右侧蓝点**，请**综合**下列特征判断未读\n"
    "（满足 ≥1 条即视为可能未读，全部缺失 → 已读）：\n"
    "  U1) 联系人名字明显**粗体**（与同屏其他细字行对比能明显看出）\n"
    "  U2) preview 文字明显**深色 / 粗体**而非灰色细字\n"
    "  U3) 行右上角时间戳显示为**蓝色 / 高饱和**（已读时间戳是灰色）\n"
    "  U4) 行末有**蓝色实心圆点**（部分版本才有）\n"
    "  U5) preview 显示为 'X new messages' / 'X new message'\n"
    "★ 营销链接 / 'sent a photo.' / 表情符开头**本身不是未读特征**——还要 U1/U2/U3 之一\n"
    "★ preview 左侧/开头显示 'You:'、'Me:'、'Draft:' 的行是**我方已发送消息或草稿**，"
    "绝不是未读。不要去掉这个前缀后再把该行列入 unread。\n"
    "★ 下游已经有 chat_state 去重，宁多列也不要漏\n"
    "★ 如果你想报 0 条未读，但屏内有 ≥4 行字体明显粗于其他，**请重新核对**\n"
    "对每条未读：\n"
    "  - name: 联系人名\n"
    "  ★ **关键警告**：未读行的 preview **通常比 name 更黑更粗**——不要因此把 preview\n"
    "    当成 name。**name 永远在 preview 的上方**（即使 name 是正常字重、preview 是粗体）。\n"
    "    举例：如果看到上下两行\n"
    "        `さとう たかひろ`                  ← 上方，正常字重\n"
    "        `What time · 上午11:59`           ← 下方，粗体（因为未读）\n"
    "    正确：name=`さとう たかひろ`, preview=`What time`, time=`上午11:59`\n"
    "    错误：name=`What time`, preview=`上午11:59`, time=`(空)`\n"
    "  - preview: 最后一条消息预览（≤80 字）**须单行**；"
    "字符串里禁止未转义的双引号/换行（否则 JSON 会断）\n"
    "  - time: 时间显示（如 'Apr 8' / '5h' / '2d'）\n"
    "  - name_bold: true / false（联系人名是否粗体）\n"
    "  - preview_bold: true / false（**消息预览**是否粗体/深色——这是未读最可靠的信号）\n"
    "  - blue_dot: true / false（行末是否有蓝色实心圆点）\n"
    "  ★ 三个信号 name_bold / preview_bold / blue_dot 至少一个为 true 才视作未读。\n"
    "  - quality_hint: friend / unknown / spam / channel / group / unsure\n"
    "      friend = 真人朋友日常聊天\n"
    "      spam = 营销/诈骗/赌博推广（'win'/'bonus'/'payout'/'fc8win' 等）\n"
    "      channel = 公众号/官方号自动推送\n"
    "      group = 群聊\n"
    "  - row_index: 该会话在**会话列表**里从上往下的物理行序号（含已读，从 0 起算）。\n"
    "      参考标定：720×1600 屏上，第 0 行 Y≈600，第 1 行 Y≈765，第 2 行 Y≈930，\n"
    "      第 3 行 Y≈1095，第 4 行 Y≈1260，第 5 行 Y≈1425，第 6 行可能贴近底栏。\n"
    "      每行约 165 像素。\n"
    "      ★ 屏内最多 **7** 条可见会话行（row_index ∈ [0, 6]）；若 row_index≥7\n"
    "        说明已滚出屏外，**不要包含进 unread 列表**。\n"
    "      ★ 会话列表区**起点是 'Stories' 行的下方** —— 别把 'Create story' / 'Wen' / 'Arnel' \n"
    "        这些 Story 圆头像 当成 row_index=0。\n"
    "      ★ **row_index=0 是 Stories 行结束后紧挨着的那条「横向矩形会话行」**。\n"
    "        矩形会话行特征：占满全宽、左侧一个头像、右侧姓名 + preview + 时间戳。\n"
    "        Stories 下方紧接有这种行就**必须**作为 row_index=0 放进 unread，**不能跳过**。\n"
    "      ★ 自检：你输出的 unread[] 里 row_index 最小值是 0 吗？如果最小是 1 或更大，\n"
    "        但 Stories 下方紧挨着就有矩形会话行（带时间戳、不是 story 圆头），\n"
    "        说明你漏了 row_index=0，请补上再输出。\n"
    "      ★ 给错就会点屏外或点错人，请仔细数。\n"
    "\n"
    "===== 任务 C（P3-1）：Facebook 账号风控横幅检测 =====\n"
    "扫描**整张图**，判断是否存在 Facebook 风控相关的横幅/提示（account restriction banner）。\n"
    "命中信号（任一即可触发）：\n"
    "  R1) 顶部/中部红色或橙色横幅条，文字含 restrict / block / violat / unusual / "
    "not allowed / community standards / temporarily / disabled\n"
    "  R2) 对话框/弹层标题含 'Your account has been restricted' / 'Action Blocked' / "
    "'You can't send messages' / 'We limit how often' 等\n"
    "  R3) 底部 snackbar/toast 显示 'Your message wasn't sent' 或类似禁发提示\n"
    "  R4) 'Temporary restriction' / 'This feature has been restricted'\n"
    "⚠️ 不要把下列当风控（全部是正常 UI）：\n"
    "  ❌ 'Messages are now encrypted' / 'End-to-end encrypted'\n"
    "  ❌ 'Active now' / 'Typing...'\n"
    "  ❌ 'New messages' / 'Seen just now'\n"
    "  ❌ 普通权限弹窗（permission_dialog 走 guard）\n"
    "\n"
    "===== 输出格式（严格一行 JSON，无 markdown）=====\n"
    '{"guard":{"type":"note_reactions|previews_on|profile_picker|send_first_like|'
    'permission_dialog|other_modal|none","action":"tap_ok|tap_close_x|press_back|'
    'need_human|none","title":"...","confidence":"high|medium|low"},'
    '"unread":[{"name":"...","preview":"...","time":"...",'
    '"quality_hint":"friend|spam|...","row_index":N,'
    '"name_bold":true,"preview_bold":true,"blue_dot":false}],"total_unread":N,'
    '"risk":{"hit":false,"severity":"none|warn|block","reason":"..."}}\n'
    "\n"
    "字段规则：\n"
    "- guard.type=none + action=none：屏幕完全干净，下游可以直接读 inbox\n"
    "- 没有未读：unread=[], total_unread=0\n"
    "- 不要把 Stories/People you may know/Suggested 当未读\n"
    "- 无风控时：risk={\"hit\":false,\"severity\":\"none\",\"reason\":\"\"}\n"
    "- 有风控时：severity=warn（警告类，如 'unusual activity'）/ block（明确不可发送）；"
    "reason 写你看到的原文片段（≤80 字）\n"
)


# ── Prompt：一次拿 guard + thread 内容 ─────────────────────────────
THREAD_COMBINED_PROMPT = (
    "你在分析一张 Facebook Messenger 1v1 私聊会话截图（Android 720x1600）。\n"
    "\n"
    "请同时回答两个任务，整合到一行 JSON 输出。\n"
    "\n"
    "===== 任务 A：守卫屏识别 =====\n"
    "（同 Inbox combined：判断是否有真悬浮 modal）\n"
    "重点排除：\n"
    "  ❌ 顶部联系人栏（头像 + 名字 + 通话/视频按钮）\n"
    "  ❌ E2EE 提示「Messages and calls are secured...」\n"
    "  ❌ 中间日期分隔符 'YESTERDAY' 等\n"
    "  ❌ 普通蓝色 / 灰色聊天气泡\n"
    "  ❌ 底部输入栏（+, 相机, Message 输入框, 表情, 👍）\n"
    "\n"
    "===== 任务 B：找最底部那条消息（对方 or 己方）=====\n"
    "  - role=peer：左侧深灰气泡 / 左侧贴纸 / 左侧图片 / 左侧 link preview 卡片\n"
    "  - role=self：右侧蓝色气泡 / 右侧贴纸 / 右侧图片 / 右下角带本账号头像的消息\n"
    "  - role=none：屏幕完全没消息（如刚进会话还没加载）\n"
    "  - 如果最底部消息在右侧，或者 UI/可访问性文本含 'You:' / '你:' / 'Me:' 前缀，"
    "必须返回 role=self，不能把它当成 peer。\n"
    "  - ⚠️ **不要**因为消息很长、很怪、是 link preview / 假支付截图 / 赌博推广\n"
    "      就返回 role=none。这正是我们要识别的"
    "      恶意营销内容 —— 完整捕获 content + URL，下游会做 spam 兜底过滤。\n"
    "  - kind=text：content 填原文（保留 emoji + URL），desc 留空；"
    "**content/desc 须单行**，字符串内禁止未转义双引号与裸换行（避免 JSON 断裂）\n"
    "  - kind=link：content 填 URL（如 https://www.fc8win.com/?id=...），\n"
    "      desc ≤30 字描述卡片标题或截图内容（如 'fc8win 赌博推广 + 假 GCash 付款截图'）\n"
    "  - kind=image：content 留空，desc ≤30 字描述图片（如 '虚假 ₱500 GCash 收据'）\n"
    "  - kind=sticker/voice/file：content 留空，desc 简述\n"
    "  - 选**垂直坐标最大、紧贴输入框上方**那一条；如果同一条消息有图 + 文，合并为 kind=link 或 image\n"
    "\n"
    "===== 任务 C（新增）：对方连发识别 =====\n"
    "  - 若 peer.role=peer，继续**从最底向上**看紧邻的 peer 气泡：\n"
    "      · 只要**该条及往上连续全是左侧 peer 气泡、中间没被 self 气泡打断**，\n"
    "        就把它们作为 extra_peers 抽出来（从近到远，≤3 条，不含 peer 本条）\n"
    "      · 遇到第一条 self 气泡或日期分隔 / 屏顶即停止\n"
    "  - 如果最底部是 self，extra_peers 必须为空，不能继续向上找旧 peer 消息来回复。\n"
    "  - 每条 extra 也按 kind/content/desc 填写（规则同上）\n"
    "  - 没有连发就 extra_peers=[]（不要硬凑）\n"
    "\n"
    "===== 任务 D（P3-1）：Facebook 账号风控横幅检测 =====\n"
    "同 Inbox 任务 C：扫描整张 thread 图的风控红/橙 banner、'Message not sent'、\n"
    "'You can't send messages' 之类提示。命中就填 risk.hit=true。\n"
    "⚠️ 不要把 'E2EE / Messages secured' / '对方 Active now' 当风控。\n"
    "\n"
    "===== 输出格式（严格一行 JSON，无 markdown）=====\n"
    '{"guard":{"type":"...","action":"...","title":"...","confidence":"high|medium|low"},'
    '"peer":{"role":"peer|self|none","kind":"text|image|sticker|voice|file|link|other",'
    '"content":"...","desc":"..."},'
    '"extra_peers":[{"kind":"...","content":"...","desc":"..."}],'
    '"risk":{"hit":false,"severity":"none|warn|block","reason":"..."}}\n'
)


def _parse_combined(raw: str) -> Optional[Dict[str, Any]]:
    return parse_vision_json_loose(raw, dump_label="combined", write_dump=True)


def _parse_guard_dict(d: Any) -> GuardScreen:
    if not isinstance(d, dict):
        return GuardScreen(type="none", action=ACTION_NONE, title="", confidence="low", raw="")
    typ = str(d.get("type") or "none").strip().lower()
    action = str(d.get("action") or ACTION_NONE).strip().lower()
    title = str(d.get("title") or "").strip()
    conf = str(d.get("confidence") or "medium").strip().lower()

    valid_types = {
        "note_reactions", "previews_on", "profile_picker",
        "send_first_like", "permission_dialog", "other_modal", "none",
    }
    if typ not in valid_types:
        typ = "other_modal"

    valid_actions = {
        ACTION_TAP_OK, ACTION_TAP_CLOSE_X, ACTION_PRESS_BACK,
        ACTION_NEED_HUMAN, ACTION_NONE,
    }
    if action not in valid_actions:
        action = ACTION_PRESS_BACK if typ != "none" else ACTION_NONE

    valid_conf = {"high", "medium", "low"}
    if conf not in valid_conf:
        conf = "medium"

    if typ == "other_modal" and conf == "low":
        action = ACTION_NONE

    return GuardScreen(
        type=typ, action=action, title=title, confidence=conf, raw=str(d),
    )


def _parse_peer_dict(d: Any, raw_text: str) -> Optional[PeerMessage]:
    if not isinstance(d, dict):
        return None
    role = str(d.get("role") or "none").strip().lower()
    if role not in ("peer", "self", "none"):
        role = "none"
    kind = str(d.get("kind") or "other").strip().lower()
    valid_kinds = {"text", "image", "sticker", "voice", "file", "link", "other"}
    if kind not in valid_kinds:
        kind = "other"
    content = str(d.get("content") or "")
    desc = str(d.get("desc") or "")
    return PeerMessage(role=role, kind=kind, content=content, desc=desc, raw=raw_text)


@dataclass(frozen=True)
class RiskSignal:
    """P3-1：Facebook 风控横幅信号。"""
    hit: bool = False
    severity: str = "none"  # none | warn | block
    reason: str = ""

    @property
    def is_block(self) -> bool:
        return bool(self.hit) and self.severity == "block"


# 白名单：常被 LLM 错当风控的正常 UI 文本（大小写不敏感）
_RISK_FALSE_POSITIVE_PATTERNS = (
    "messages are now encrypted",
    "end-to-end encrypted",
    "end to end encrypted",
    "active now",
    "typing",
    "seen just now",
    "seen ",  # "Seen 12:34" 之类
    "sent just now",
    "new messages",
    "enable notifications",
    "turn on notifications",
)


def _parse_risk_dict(d: Any) -> RiskSignal:
    if not isinstance(d, dict):
        return RiskSignal()
    hit = bool(d.get("hit", False))
    sev = str(d.get("severity") or "none").strip().lower()
    if sev not in ("none", "warn", "block"):
        sev = "none"
    reason = str(d.get("reason") or "").strip()
    if not hit or sev == "none":
        return RiskSignal(hit=False, severity="none", reason="")
    # 白名单：LLM 误把 E2EE / typing 类文案当风控
    low = reason.lower()
    if any(p in low for p in _RISK_FALSE_POSITIVE_PATTERNS):
        return RiskSignal(hit=False, severity="none", reason="")
    # 强化：reason 太短或空说明 LLM 没依据
    if len(reason) < 6:
        return RiskSignal(hit=False, severity="none", reason="")
    return RiskSignal(hit=True, severity=sev, reason=reason[:200])


@dataclass(frozen=True)
class InboxCombinedResult:
    guard: GuardScreen
    rows: List[UnreadChat]
    raw: str
    risk: RiskSignal = RiskSignal()


# ── 单任务兜底 prompt：combined 报 0 未读时用 ────────────────
# 只问一件事：屏上有 "X new messages" 或明显粗体名字的行，逐行给 name。
UNREAD_ONLY_PROMPT = (
    "这是 Facebook Messenger Android Inbox 截图（720×1600）。\n"
    "**只回答一个问题**：屏上**从上到下**哪些会话**有未读消息**？\n"
    "\n"
    "判定规则（宁多勿漏）：\n"
    "  - preview 区出现 'X new messages' / 'X new message'（明确未读）\n"
    "  - 联系人名**明显粗体**（同屏其他细字对比很明显）\n"
    "  - 行末有**蓝色实心小圆点**（部分版本才有）\n"
    "  - 时间戳是**蓝色高饱和**（已读时间戳是灰色）\n"
    "  - 但 preview 左侧/开头显示 'You:'、'Me:'、'Draft:' 的行必须排除；"
    "这是我方消息或草稿，不是客户未读消息。\n"
    "\n"
    "**严格一行 JSON 输出（无 markdown）**：\n"
    '{"unread":[{"name":"...","row_index":N,"preview":"...≤60字","signal":'
    '"new_messages_text|bold_name|blue_dot|blue_time|multi"}],"total":N}\n'
    "\n"
    "其中 row_index 是**屏上从上到下**物理序号（Stories 行下方算 0）。"
    "屏内最多 **7** 行（0..6），滚屏外的不报。"
    "一条都没就 {\"unread\":[],\"total\":0}。"
)


def _is_noise_inbox_name(name: str) -> bool:
    """Meta AI / 系统入口等不应作为未读自动回复目标。"""
    n = (name or "").strip().lower()
    if not n:
        return True
    if n == "meta ai" or n.startswith("meta ai "):
        return True
    if "meta ai" in n and len(n) < 24:
        return True
    return False


_OUTBOUND_OR_DRAFT_PREVIEW_PREFIXES = (
    "you:",
    "me:",
    "draft:",
    "you：",
    "me：",
    "draft：",
)


def is_outbound_or_draft_preview(preview: str) -> bool:
    """Return True when an inbox preview is our own last message or a draft."""
    p = re.sub(r"\s+", " ", str(preview or "")).strip().lower()
    return any(p.startswith(prefix) for prefix in _OUTBOUND_OR_DRAFT_PREVIEW_PREFIXES)


async def analyze_unread_only(
    image_path: str,
    *,
    vision_cfg: Dict[str, Any],
    global_vision: Dict[str, Any],
    skip_spam: bool = True,
) -> Tuple[List[UnreadChat], str]:
    """combined 漏报时的单任务兜底。返回 UnreadChat 列表（不含 guard）。"""
    raw, tag = await _call_vision(
        image_path, UNREAD_ONLY_PROMPT,
        vision_cfg=vision_cfg, global_vision=global_vision,
    )
    if not raw:
        return [], f"empty:{tag}"

    parsed = _parse_combined(raw)
    if not parsed:
        return [], f"parse_failed:{tag}"

    rows: List[UnreadChat] = []
    for i, raw_row in enumerate(parsed.get("unread") or []):
        if not isinstance(raw_row, dict):
            continue
        name = str(raw_row.get("name") or "").strip()
        if not name:
            continue
        if _is_noise_inbox_name(name):
            continue
        preview = str(raw_row.get("preview") or "").strip()
        if is_outbound_or_draft_preview(preview):
            logger.info(
                "[combined_vision] skip outbound/draft preview name=%r preview=%r",
                name, preview[:80],
            )
            continue
        try:
            row_index = int(raw_row.get("row_index") or i)
        except (TypeError, ValueError):
            row_index = i
        if row_index < 0:
            row_index = 0
        if row_index > 6:
            continue
        hint = _local_quality_hint(name, preview)
        rows.append(
            UnreadChat(
                name=name,
                preview=preview,
                time="",
                row_index=row_index,
                y_percent=0.0,
                quality_hint=hint,
                score=_score_chat(row_index, hint, preview),
            )
        )
    rows.sort(key=lambda r: r.score, reverse=True)
    if skip_spam:
        before = len(rows)
        rows = [r for r in rows if r.quality_hint != "spam"]
        if before != len(rows):
            logger.info(
                "[combined_vision] unread_only 过滤 spam: %d -> %d",
                before,
                len(rows),
            )
    return rows, tag


@dataclass(frozen=True)
class ThreadCombinedResult:
    guard: GuardScreen
    peer: Optional[PeerMessage]
    raw: str
    # ★ P2-2：对方在 peer 正下方往上连续发出的 extra 消息（不含 peer 本条，
    # 从近到远 ≤3 条），允许上游把连发消息拼成一条文本喂给 AI
    extra_peers: Tuple[PeerMessage, ...] = ()
    # ★ P3-1：账号风控横幅检测结果
    risk: RiskSignal = RiskSignal()


async def _call_vision(
    image_path: str,
    prompt: str,
    *,
    vision_cfg: Dict[str, Any],
    global_vision: Dict[str, Any],
) -> Tuple[str, str]:
    try:
        from src.vision_client import VisionClient
    except Exception as ex:
        return "", f"error:vision_import:{ex}"
    try:
        text, tag = await VisionClient.describe_image_with_ollama_zhipu_fallback(
            merged_config=vision_cfg,
            global_vision=global_vision,
            image_path=image_path,
            prompt=prompt,
        )
        return (text or ""), tag
    except Exception as ex:
        return "", f"error:vision_call:{ex}"


async def analyze_inbox_combined(
    image_path: str,
    *,
    vision_cfg: Dict[str, Any],
    global_vision: Dict[str, Any],
    skip_spam: bool = True,
) -> Tuple[InboxCombinedResult, str]:
    raw, tag = await _call_vision(
        image_path, INBOX_COMBINED_PROMPT,
        vision_cfg=vision_cfg, global_vision=global_vision,
    )
    if not raw:
        return (
            InboxCombinedResult(
                guard=GuardScreen(type="none", action=ACTION_NONE, title="", confidence="low", raw=""),
                rows=[], raw="",
            ),
            f"empty:{tag}",
        )

    parsed = _parse_combined(raw)
    if not parsed:
        return (
            InboxCombinedResult(
                guard=GuardScreen(type="none", action=ACTION_NONE, title="", confidence="low", raw=raw),
                rows=[], raw=raw,
            ),
            f"parse_failed:{tag}",
        )

    guard = _parse_guard_dict(parsed.get("guard"))

    rows: List[UnreadChat] = []
    for i, raw_row in enumerate(parsed.get("unread") or []):
        if not isinstance(raw_row, dict):
            continue
        name = str(raw_row.get("name") or "").strip()
        if not name:
            continue
        preview = str(raw_row.get("preview") or "").strip()
        if is_outbound_or_draft_preview(preview):
            logger.info(
                "[combined_vision] skip outbound/draft preview name=%r preview=%r",
                name, preview[:80],
            )
            continue
        time_s = str(raw_row.get("time") or "").strip()
        hint = str(raw_row.get("quality_hint") or "").strip().lower()
        valid_hints = {"friend", "unknown", "spam", "channel", "group", "unsure"}
        if hint not in valid_hints:
            hint = _local_quality_hint(name, preview)
        local_hint = _local_quality_hint(name, preview)
        if local_hint == "spam" and hint in ("friend", "unknown", "unsure"):
            hint = "spam"

        try:
            row_index = int(raw_row.get("row_index") or i)
        except (TypeError, ValueError):
            row_index = i
        # 防越界：屏内最多 7 行可见（720x1600，行高 165；第 6 行可能贴底）。
        # row_index>6 视为滚出屏外
        if row_index < 0:
            row_index = 0
        if row_index > 6:
            logger.warning(
                "[combined_vision] row_index=%d 滚屏外，丢弃 name=%r",
                row_index, name,
            )
            continue

        # ★ 三信号合议：只要 name_bold / preview_bold / blue_dot 至少 1 个 True 即保留。
        # 任何字段缺失按默认值处理（向后兼容：老 prompt 只返 name_bold 时行为不变）。
        # 实测 GLM-4V 对 Messenger 的 name_bold 偶有错判（所有行都写 False），
        # 所以**必须**综合 preview_bold / blue_dot 才能避免误丢真未读。
        name_bold = bool(raw_row.get("name_bold", True))
        preview_bold = bool(raw_row.get("preview_bold", False))
        blue_dot = bool(raw_row.get("blue_dot", False))
        unread_signals = int(name_bold) + int(preview_bold) + int(blue_dot)
        if unread_signals == 0:
            # Vision 已把本行放进 unread[]，但三信号全 F（模型偷懒/误判）时
            # 仍保留 —— 否则会出现「有未读却 0 条」的假阴性（双机互发联调常见）。
            logger.warning(
                "[combined_vision] 三信号全 F 仍保留 name=%r row=%d "
                "（已在 unread 列表内）",
                name, row_index,
            )

        rows.append(
            UnreadChat(
                name=name,
                preview=preview,
                time=time_s,
                row_index=row_index,
                y_percent=0.0,
                quality_hint=hint,
                score=_score_chat(row_index, hint, preview),
            )
        )

    if skip_spam:
        before = len(rows)
        rows = [r for r in rows if r.quality_hint != "spam"]
        if before != len(rows):
            logger.info(
                "[combined_vision] inbox 过滤 spam: %d -> %d", before, len(rows)
            )

    rows.sort(key=lambda r: r.score, reverse=True)
    risk = _parse_risk_dict(parsed.get("risk"))
    if risk.hit:
        logger.warning(
            "[combined_vision] inbox risk detected severity=%s reason=%r",
            risk.severity, risk.reason,
        )
    return (
        InboxCombinedResult(guard=guard, rows=rows, raw=raw, risk=risk),
        tag,
    )


async def analyze_thread_combined(
    image_path: str,
    *,
    vision_cfg: Dict[str, Any],
    global_vision: Dict[str, Any],
) -> Tuple[ThreadCombinedResult, str]:
    raw, tag = await _call_vision(
        image_path, THREAD_COMBINED_PROMPT,
        vision_cfg=vision_cfg, global_vision=global_vision,
    )
    if not raw:
        return (
            ThreadCombinedResult(
                guard=GuardScreen(type="none", action=ACTION_NONE, title="", confidence="low", raw=""),
                peer=None, raw="",
            ),
            f"empty:{tag}",
        )

    parsed = _parse_combined(raw)
    if not parsed:
        return (
            ThreadCombinedResult(
                guard=GuardScreen(type="none", action=ACTION_NONE, title="", confidence="low", raw=raw),
                peer=None, raw=raw,
            ),
            f"parse_failed:{tag}",
        )

    guard = _parse_guard_dict(parsed.get("guard"))
    peer = _parse_peer_dict(parsed.get("peer"), raw)
    # ★ P2-2：解析 extra_peers（对方连发，从近到远，≤3 条）
    extra: list = []
    raw_extra = parsed.get("extra_peers") or []
    if isinstance(raw_extra, list):
        for it in raw_extra[:3]:
            if not isinstance(it, dict):
                continue
            # extra 天然是 peer 角色，强制 role=peer
            d = {"role": "peer", **it}
            pm = _parse_peer_dict(d, raw)
            if pm is not None and pm.role == "peer":
                extra.append(pm)
    # ★ P3-1：解析 risk
    risk = _parse_risk_dict(parsed.get("risk"))
    if risk.hit:
        logger.warning(
            "[combined_vision] thread risk detected severity=%s reason=%r",
            risk.severity, risk.reason,
        )
    return (
        ThreadCombinedResult(
            guard=guard, peer=peer, raw=raw,
            extra_peers=tuple(extra), risk=risk,
        ),
        tag,
    )


# ═══ P2-1：图片深度描述 ═══════════════════════════════════════════
# 当对方发图 (kind=image) 时，combined_vision 给的 desc 只有 ≤30 字；
# 用专门的 vision 调用拿到 1-2 句详细描述，让下游 AI 能真正看懂图
_IMAGE_DEEP_DESCRIBE_PROMPT_ZH = (
    "这是一张 Facebook Messenger 对方发来的消息截图。\n"
    "请聚焦在**左侧（对方一方）发出的图片气泡**，用 1-2 句自然中文描述图片内容。\n"
    "要求：\n"
    "  - 只描述图片本身（人、物、场景、动作、表情），不要描述对话气泡框或 UI\n"
    "  - 若是自拍/人像：描述性别/大致年龄/表情/背景/穿着，不要描述长相细节\n"
    "  - 若是截图/表情包/meme：描述内容大意即可\n"
    "  - 若是商品/产品图：简述品类 + 显著特征\n"
    "  - 若看不清或没有图片：返回 '无图片' 三个字\n"
    "  - 输出 ≤ 80 字，**纯文字**，不要 JSON / 不要 markdown / 不要前缀后缀\n"
)

_IMAGE_DEEP_DESCRIBE_PROMPT_EN = (
    "This is a Facebook Messenger screenshot. Focus on the image bubble sent "
    "by the OTHER party (left side). Describe the image content in 1-2 "
    "natural English sentences.\n"
    "Rules:\n"
    "  - Describe only the image itself (people, objects, scene, action, "
    "expression); DO NOT describe the chat bubble frame or any UI\n"
    "  - For selfies/portraits: gender / approximate age / expression / "
    "background / clothing; DO NOT describe detailed facial features\n"
    "  - For screenshots/memes: describe the gist\n"
    "  - For product photos: category + salient features\n"
    "  - If no image is visible: return exactly 'NO_IMAGE'\n"
    "  - Output <= 80 chars, plain text, NO JSON / NO markdown / NO prefix\n"
)


async def describe_peer_image_detail(
    image_path: str,
    *,
    vision_cfg: Dict[str, Any],
    global_vision: Dict[str, Any],
    language: str = "zh",
) -> Tuple[str, str]:
    """对 thread 截图做一次聚焦的图片内容描述。

    返回 (caption, tag)。失败或无图片时 caption 为空串。
    """
    prompt = (
        _IMAGE_DEEP_DESCRIBE_PROMPT_EN
        if str(language).lower().startswith("en")
        else _IMAGE_DEEP_DESCRIBE_PROMPT_ZH
    )
    raw, tag = await _call_vision(
        image_path, prompt,
        vision_cfg=vision_cfg, global_vision=global_vision,
    )
    text = (raw or "").strip()
    # 清掉常见的模型前缀
    for pref in (
        "描述：", "Description:", "图片描述：", "Caption:",
        "```", "图片内容：",
    ):
        if text.startswith(pref):
            text = text[len(pref):].strip()
    # 剥 markdown 引号
    if text.startswith(("「", '"', "“")):
        text = text[1:]
    if text.endswith(("」", '"', "”")):
        text = text[:-1]
    # 判 "无图片"
    if text in ("无图片", "NO_IMAGE", "no_image", "无") or not text:
        return "", tag
    return text[:200], tag
