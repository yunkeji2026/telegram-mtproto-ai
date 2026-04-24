"""Messenger 安卓 UI 关键坐标（基于 720×1600 物理像素标定，按设备分辨率等比缩放）。

⚠️ 关键约定 ⚠️
- BASE 分辨率 = 720 × 1600 = 设备物理像素
- 所有 Coord 的 (x, y) 都是 **物理像素坐标**（即 adb input tap 用的坐标）
- 不要写"图像显示坐标"——浏览器/截图查看器把 720×1600 缩到 461×1024 是显示层，
  跟 ADB 无关
- Coord.at(w, h) 在不同物理分辨率设备上等比换算

校准方法（每次新机型都要做）：
  1. 在设备物理分辨率上截图（adb shell wm size 看真实 W×H）
  2. 在原始截图（不缩放）里找控件的 Y 坐标
  3. 这个 Y 就是物理坐标，直接填进 Coord
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

# ── 基准分辨率（标定坐标用，请勿修改） ─────────────────
BASE_WIDTH = 720
BASE_HEIGHT = 1600


@dataclass(frozen=True)
class Coord:
    """一个标定点；用 .at(w, h) 换算到任意分辨率。"""

    x: int
    y: int
    label: str = ""

    def at(self, width: int, height: int) -> Tuple[int, int]:
        """按目标设备分辨率等比换算，返回 (x, y)。"""
        rx = float(width) / BASE_WIDTH
        ry = float(height) / BASE_HEIGHT
        return int(round(self.x * rx)), int(round(self.y * ry))


# ── Inbox 主页（Chats tab）—— 720×1600 物理坐标 ─────────────────────────
# 顶部状态栏 ~Y65，messenger 标题 ~Y125
INBOX_TOP_TITLE = Coord(180, 125, "messenger 标题")
INBOX_NEW_MSG_BTN = Coord(580, 125, "新建消息(笔)")
INBOX_FB_AVATAR = Coord(670, 125, "右上 Facebook 头像")
INBOX_SEARCH_BAR = Coord(360, 230, "Ask Meta AI / Search 搜索栏")

# 不同地区 / Meta AI 条高度下，搜索入口 Y 会漂移；``send_to_chat_name`` 搜索
# 重试时按序尝试这些点（相对 INBOX_SEARCH_BAR 的 delta_y，经 .at 等比换算）。
_INBOX_SEARCH_BAR_DELTA_Y: Tuple[int, ...] = (0, -42, 38, 78, -78)


def inbox_search_tap_candidates(width: int, height: int) -> List[Tuple[int, int]]:
    """Chats 页搜索/Ask 输入区的一组 tap 点（物理像素），优先主标定点再试 Y 偏移。"""
    out: List[Tuple[int, int]] = []
    for dy in _INBOX_SEARCH_BAR_DELTA_Y:
        x, y = Coord(INBOX_SEARCH_BAR.x, INBOX_SEARCH_BAR.y + dy, "search tap").at(
            width, height,
        )
        y = max(95, min(y, int(height * 0.42)))
        pair = (x, y)
        if pair not in out:
            out.append(pair)
    return out


# Stories 行（标题在 ~Y280，圆头像中心在 ~Y420）
STORIES_ROW_Y = 420
STORY_CREATE_X = 75
STORY_FIRST_X = 200
STORY_GAP_X = 175

# 会话列表第一行（Rodel 在 ~Y597）；每行高度约 175 px
# 注意：本次 d113 真实测量得到 Rodel(行0)≈Y597, Dela Cruz(行1)≈Y730, Madhasz(行2)≈Y905, Facebook user(行3)≈Y1075
CHAT_ROW_FIRST_Y = 600
CHAT_ROW_HEIGHT = 165
CHAT_ROW_TEXT_X = 280  # 名字 + 预览的水平中心

# 底部 tab（Y≈1490 是真实物理坐标，Chats 中心 X≈115）
TAB_CHATS = Coord(115, 1490, "Chats tab")
TAB_STORIES = Coord(360, 1490, "Stories tab")
TAB_MENU = Coord(605, 1490, "Menu tab")

# ── E2EE 会话页（Thread view）—— 720×1600 物理坐标 ──────────────────
THREAD_BACK = Coord(50, 140, "返回箭头")
THREAD_AVATAR = Coord(150, 140, "对方头像")
THREAD_TITLE = Coord(290, 140, "对方姓名")
THREAD_CALL = Coord(490, 140, "语音通话")
THREAD_VIDEO = Coord(575, 140, "视频通话")
THREAD_INFO = Coord(660, 140, "信息(i)")

# 底部输入区 —— 注意：Messenger 输入区位置随键盘状态变化
# 键盘**未弹出**态（默认进会话）：输入框 Y≈1450
# 键盘**已弹出**态（点过输入框）：输入框被键盘挤到 Y≈938
#
# 发送链路推荐顺序：
#   1) 点 INPUT_TEXT_FIELD_DOCKED (键盘未弹出态) → 唤起键盘
#   2) 等键盘动画 600ms
#   3) 输入文字 (input text 或 ADB Keyboard)
#   4) 点 SEND_BTN_KBD_OPEN (键盘已弹出态)
#
# 不点发送时（debug），停在 step 3 就行
INPUT_PLUS_DOCKED = Coord(60, 1450, "+ 附件 (键盘未弹)")
INPUT_CAMERA_DOCKED = Coord(140, 1450, "相机 (键盘未弹)")
INPUT_GALLERY_DOCKED = Coord(220, 1450, "相册 (键盘未弹)")
INPUT_MIC_DOCKED = Coord(300, 1450, "麦克风 (键盘未弹)")
INPUT_TEXT_FIELD = Coord(480, 1450, "Message 输入框 (键盘未弹，点击后唤起键盘)")
INPUT_EMOJI_DOCKED = Coord(640, 1450, "表情 (键盘未弹)")
INPUT_THUMB_UP_DOCKED = Coord(700, 1450, "👍 (键盘未弹)")

# 键盘弹出后输入框/发送按钮的位置 —— 通过 d113 物理屏实测校准
# 输入框中心在 X≈470 (居中靠右)，Y≈940
INPUT_TEXT_FIELD_KBD_OPEN = Coord(470, 940, "输入框 (键盘已弹)")
SEND_BTN = Coord(671, 940, "发送箭头 ➤ (键盘已弹)")

# 兼容老命名（不要新代码用）
INPUT_PLUS = INPUT_PLUS_DOCKED
INPUT_CAMERA = INPUT_CAMERA_DOCKED
INPUT_GALLERY = INPUT_GALLERY_DOCKED
INPUT_MIC = INPUT_MIC_DOCKED
INPUT_EMOJI = INPUT_EMOJI_DOCKED
INPUT_THUMB_UP = INPUT_THUMB_UP_DOCKED

# ── 常见 Bloks onboarding modal ──────────────────
# Note reactions / Previews are on / etc.
MODAL_OK_BTN = Coord(360, 1440, "OK 按钮 (蓝色长条)")
MODAL_CLOSE_X = Coord(670, 470, "modal 右上 X")

# ── Profile picker（多账户屏；存在多个账户时） ───
PICKER_FIRST_ACCOUNT = Coord(360, 670, "第一个账户")
PICKER_SECOND_ACCOUNT = Coord(360, 850, "第二个账户")
PICKER_USE_ANOTHER = Coord(360, 1010, "Use another profile")


def chat_row(index: int) -> Coord:
    """第 index 行会话条目的中心坐标（index 从 0 开始）。

    若 RPA runner 已为当前 device 注入 calibration，请改用 chat_row_for(serial, ...)。
    本函数仅返回 BASE 720×1600 上的坐标。
    """
    y = CHAT_ROW_FIRST_Y + index * CHAT_ROW_HEIGHT
    return Coord(CHAT_ROW_TEXT_X, y, f"chat row #{index}")


def chat_row_for(
    index: int,
    *,
    width: int,
    height: int,
    chat_row_first_y: Optional[int] = None,
    chat_row_height: Optional[int] = None,
    chat_row_text_x: Optional[int] = None,
) -> Tuple[int, int]:
    """带校准的 chat_row。如果 chat_row_first_y / chat_row_height 任一缺失，
    就用 BASE 等比缩放回退。返回**目标分辨率**下的物理坐标 (x, y)。
    """
    rx = float(width) / BASE_WIDTH
    ry = float(height) / BASE_HEIGHT
    fy = (
        chat_row_first_y
        if chat_row_first_y is not None
        else int(round(CHAT_ROW_FIRST_Y * ry))
    )
    h = (
        chat_row_height
        if chat_row_height is not None and chat_row_height > 80
        else int(round(CHAT_ROW_HEIGHT * ry))
    )
    x = (
        chat_row_text_x
        if chat_row_text_x is not None
        else int(round(CHAT_ROW_TEXT_X * rx))
    )
    return x, fy + index * h


def story_at(index: int) -> Coord:
    """Stories 行第 index 个头像（index 0 是 Create story，1+ 是真实 story）。"""
    if index == 0:
        x = STORY_CREATE_X
    else:
        x = STORY_FIRST_X + (index - 1) * STORY_GAP_X
    return Coord(x, STORIES_ROW_Y, f"story #{index}")
