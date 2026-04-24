"""Facebook 主 App (com.facebook.katana) 关键 UI 坐标。

★ 与 messenger_rpa.coords 共用 BASE_WIDTH/BASE_HEIGHT 标定 (720×1600 物理)
★ Katana 一样不能 uiautomator dump（OOM），必须走 vision + 坐标
★ 只覆盖最核心的入口；细分页面（评论区/帖子详情）下一阶段再补

布局基线：通过 d113 物理屏 (720×1600) 实测校准。
"""

from __future__ import annotations

from src.integrations.messenger_rpa.coords import BASE_WIDTH, BASE_HEIGHT, Coord  # noqa: F401

# ── Katana News Feed 顶部导航 ──────────────────────
NF_HAMBURGER = Coord(50, 110, "左上汉堡菜单 (≡)")
NF_LOGO = Coord(160, 110, "facebook logo")
NF_NEW_POST_BTN = Coord(530, 110, "+ 新建帖子")
NF_SEARCH = Coord(605, 110, "搜索 🔍")
NF_MESSENGER_BTN = Coord(671, 110, "Messenger ↻ 入口 (右上)")

# 顶部 Tabs (6 个)
TAB_HOME = Coord(58, 203, "Home tab")
TAB_REELS = Coord(176, 203, "Reels tab")
TAB_FRIENDS = Coord(300, 203, "Friends tab")
TAB_MARKETPLACE = Coord(415, 203, "Marketplace tab")
TAB_NOTIFICATIONS = Coord(543, 203, "Notifications tab")
TAB_PROFILE = Coord(664, 203, "Profile tab")

# News Feed 上方的发帖入口
NF_POST_BOX = Coord(375, 308, "What's on your mind? 发帖框")
NF_POST_PHOTO = Coord(656, 308, "发图片入口")

# Stories 行（圆头像中心 Y≈594）
STORY_ROW_Y = 594
STORY_CREATE_X = 115
STORY_FIRST_X = 297
STORY_GAP_X = 175

# PYMK 区块（People you may know）
PYMK_TITLE_Y = 1010                       # "People you may know" 标题 Y
PYMK_FIRST_CARD_CENTER = Coord(175, 1300, "PYMK 第一张卡片 (头像)")
PYMK_FIRST_NAME_Y = 1500                  # 第一张卡片名字 Y
PYMK_DISMISS_X = 432                      # PYMK 区块右上 X 关闭

# ── 底部导航（部分版本有）── 通常 katana 是顶部 tabs，没底部 nav

# ── 评论区（点开帖子→Comments）──
# d113 当前是 Home Feed，没打开评论；待手动打开后再校准
# COMMENT_INPUT = Coord(?, ?, "评论输入框")
# COMMENT_SEND = Coord(?, ?, "评论发送")


def story_at(index: int) -> Coord:
    """Stories 行第 index 个头像（index=0 是 Create）。"""
    if index == 0:
        x = STORY_CREATE_X
    else:
        x = STORY_FIRST_X + (index - 1) * STORY_GAP_X
    return Coord(x, STORY_ROW_Y, f"katana story #{index}")


def pymk_card(index: int) -> Coord:
    """PYMK 第 index 张卡片中心（横向滚动）。"""
    if index == 0:
        return PYMK_FIRST_CARD_CENTER
    # 横向 gap ~285 px
    x = 175 + index * 285
    return Coord(x, PYMK_FIRST_CARD_CENTER.y, f"PYMK card #{index}")


# ── Friends 页（点 Friends tab 后） ──
FRIENDS_TAB_HEADER_HEIGHT = 109
FRIENDS_TITLE = Coord(164, 203, "Friends 标题")
FRIENDS_FILTER_ONLINE = Coord(140, 281, "X online filter")
FRIENDS_FILTER_SUGGEST = Coord(382, 281, "Suggestions filter")
FRIENDS_REQUESTS_TITLE = Coord(140, 562, "Friend requests 标题")
FRIENDS_REQUESTS_SEE_ALL = Coord(635, 562, "See all 链接")

# Friend Requests 列表 —— 第一行 Confirm/Delete 按钮中心
FRIENDS_REQ_FIRST_CONFIRM = Coord(343, 781, "第 0 行 Confirm")
FRIENDS_REQ_FIRST_DELETE = Coord(578, 781, "第 0 行 Delete")
FRIENDS_REQ_ROW_HEIGHT = 234              # 每行约 234 物理像素


def friend_request_confirm(index: int) -> Coord:
    """第 index 个 friend request 的 Confirm 按钮中心。"""
    y = 781 + index * FRIENDS_REQ_ROW_HEIGHT
    return Coord(343, y, f"friend_request[{index}] Confirm")


def friend_request_delete(index: int) -> Coord:
    """第 index 个 friend request 的 Delete 按钮中心。"""
    y = 781 + index * FRIENDS_REQ_ROW_HEIGHT
    return Coord(578, y, f"friend_request[{index}] Delete")
