"""Bloks/RN 守卫屏检测与自动闪避。

Messenger 经常在用户打开会话时弹出 onboarding modal：
- 'Note reactions will no longer be sent as messages'（点 OK 关闭）
- 'Previews are on'（点右上 X 或 BACK 关闭）
- 'Profile picker'（多账户选号；不能自动跳过，必须等用户手动选）
- 'Send Like / Send a heart'（首次发送的引导）

Vision 识别这些 modal 的存在并给出建议动作。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# 已知守卫屏类型
ACTION_TAP_OK = "tap_ok"
ACTION_TAP_CLOSE_X = "tap_close_x"
ACTION_PRESS_BACK = "press_back"
ACTION_NEED_HUMAN = "need_human"  # 多账户选号等
ACTION_NONE = "none"  # 没有 modal


GUARD_VISION_PROMPT = (
    "你在分析一张 Facebook Messenger 安卓 App 截图。\n"
    "\n"
    "唯一任务：判断屏幕上是否有 **真正的悬浮遮挡式 modal/dialog/bottom-sheet**。\n"
    "\n"
    "===== 真 modal 的 4 个必要视觉特征（必须**同时**出现至少 3 个）=====\n"
    "  M1) 背景内容被半透明黑色遮罩覆盖（背后图层明显发暗 / 模糊）\n"
    "  M2) 浮层有明显的圆角白卡 / 圆角深灰卡，边缘脱离屏幕（非满屏）\n"
    "  M3) 浮层中央或下方有显眼大按钮（'OK', 'Continue', 'Allow', 'Don't Allow'）\n"
    "  M4) 浮层右上角有 X 关闭图标 或 顶部有 drag handle 横线\n"
    "\n"
    "===== 假阳性陷阱（这些都不是 modal，必须 type=none）=====\n"
    "  ❌ Chats 顶部的 'Pin your favorite chats' / 'Try Meta AI' / 'Stories'"
    "      banner 提示条 —— 它们是 inline 横幅，没有遮罩，背景没变暗\n"
    "  ❌ 会话页里的 'Unread messages' 分隔符\n"
    "  ❌ 'Messages and calls are secured with end-to-end encryption' E2EE 提示\n"
    "  ❌ 时间戳分隔符 'SAT AT 3:30 AM' / 'Today' / 'Yesterday'\n"
    "  ❌ link preview 卡片（蓝边卡片，是消息内容的一部分）\n"
    "  ❌ Story Note 视图（黑屏 + 圆形头像 + 文字气泡 + 表情反应行）\n"
    "      —— 它是全屏专用页面，不是 modal；如果是这个，type=none, action=press_back\n"
    "  ❌ '今天的活跃' / 'People you may know' 区块\n"
    "  ❌ 系统通知抽屉（屏幕顶部下拉的）—— 不是 Messenger modal\n"
    "\n"
    "===== 关键判断口诀 =====\n"
    "  • 看到 Message 输入框 + 表情 + 👍 在底部 → **正常会话页**，type=none\n"
    "  • 看到 Chats / Stories / Menu 三个 tab 在底部 → **正常 Inbox**，type=none\n"
    "  • 没有遮罩、没有圆角浮卡 → **不是 modal**，type=none\n"
    "\n"
    "===== 已知 modal 类型与处置 =====\n"
    "  - note_reactions：'Note reactions will no longer be sent as messages' "
    "弹出层 + 底部 OK 蓝按钮 → action=tap_ok\n"
    "  - previews_on：'Previews are on' 弹出层，右上 X → action=press_back\n"
    "  - profile_picker：整屏账号选择（'Messenger' logo + 多账号头像 + 'Use another profile'）"
    "→ action=need_human\n"
    "  - send_first_like：首次发送 like 的解释弹窗 → action=tap_ok\n"
    "  - permission_dialog：Android 系统对话框 'Allow Messenger to...' → action=press_back\n"
    "  - other_modal：明显遮罩 + 浮卡但不是上面任何一种 → action=press_back\n"
    "  - none：以上都不是 → action=none\n"
    "\n"
    "===== 输出 =====\n"
    "严格输出一行合法 JSON（不要 markdown 包裹）：\n"
    '{"type":"note_reactions|previews_on|profile_picker|send_first_like|'
    'permission_dialog|other_modal|none","action":"tap_ok|tap_close_x|press_back|'
    'need_human|none","title":"...","confidence":"high|medium|low"}\n'
    "字段规则：\n"
    "- title：modal 标题文字；none 时填空串\n"
    "- confidence：你对此判断的自信度。看到的视觉证据越接近 M1-M4 全满，"
    "越是 high；只是猜测就 low\n"
)


@dataclass(frozen=True)
class GuardScreen:
    type: str
    action: str
    title: str
    confidence: str  # high | medium | low
    raw: str

    @property
    def is_clear(self) -> bool:
        return self.type == "none"

    @property
    def needs_human(self) -> bool:
        return self.action == ACTION_NEED_HUMAN

    @property
    def is_trustworthy(self) -> bool:
        """高/中置信度才允许执行闪避动作；低置信度时把它当 none。"""
        return self.confidence in ("high", "medium")


def _parse_guard_json(raw: str) -> Optional[Dict[str, Any]]:
    s = (raw or "").strip()
    if s.startswith("```"):
        lines = [ln for ln in s.splitlines() if not ln.strip().startswith("```")]
        s = "\n".join(lines).strip()
    try:
        return json.loads(s)
    except Exception as ex:
        logger.warning("guard vision JSON 解析失败: %s | raw=%r", ex, s[:200])
        return None


async def detect_guard_screen(
    image_path: str,
    *,
    vision_cfg: Dict[str, Any],
    global_vision: Dict[str, Any],
) -> Tuple[GuardScreen, str]:
    """识别当前屏是否被守卫弹窗遮挡，返回应执行的动作。"""
    try:
        from src.vision_client import VisionClient
    except Exception as ex:
        return (
            GuardScreen(type="none", action=ACTION_NONE, title="", raw=""),
            f"error:vision_import_failed:{ex}",
        )

    try:
        text, tag = await VisionClient.describe_image_with_ollama_zhipu_fallback(
            merged_config=vision_cfg,
            global_vision=global_vision,
            image_path=image_path,
            prompt=GUARD_VISION_PROMPT,
        )
    except Exception as ex:
        return (
            GuardScreen(type="none", action=ACTION_NONE, title="", raw=""),
            f"error:vision_call_failed:{ex}",
        )

    if not text:
        return (
            GuardScreen(
                type="none", action=ACTION_NONE, title="", confidence="low", raw=""
            ),
            f"empty:{tag}",
        )

    parsed = _parse_guard_json(text)
    if not parsed:
        return (
            GuardScreen(
                type="none",
                action=ACTION_NONE,
                title="",
                confidence="low",
                raw=text,
            ),
            f"parse_failed:{tag}",
        )

    typ = str(parsed.get("type") or "none").strip().lower()
    action = str(parsed.get("action") or ACTION_NONE).strip().lower()
    title = str(parsed.get("title") or "").strip()
    conf = str(parsed.get("confidence") or "medium").strip().lower()

    valid_types = {
        "note_reactions",
        "previews_on",
        "profile_picker",
        "send_first_like",
        "permission_dialog",
        "other_modal",
        "none",
    }
    if typ not in valid_types:
        typ = "other_modal"

    valid_actions = {
        ACTION_TAP_OK,
        ACTION_TAP_CLOSE_X,
        ACTION_PRESS_BACK,
        ACTION_NEED_HUMAN,
        ACTION_NONE,
    }
    if action not in valid_actions:
        action = ACTION_PRESS_BACK if typ != "none" else ACTION_NONE

    if conf not in ("high", "medium", "low"):
        conf = "medium"

    # 保守化：低置信度 + 通用 other_modal → 视为 none，避免误闪避
    # （已知特定类型如 note_reactions 可信赖即使 low）
    if typ == "other_modal" and conf == "low":
        typ = "none"
        action = ACTION_NONE

    return (
        GuardScreen(
            type=typ, action=action, title=title, confidence=conf, raw=text
        ),
        tag,
    )
