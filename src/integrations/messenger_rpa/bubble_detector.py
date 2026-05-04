"""像素级气泡位置检测器：判断 Messenger 线程截图中最新消息的发送方。

Messenger 聊天气泡特征：
  - 自己发的：蓝色/紫色渐变 (#0084FF ~ #9B59B6)，右对齐
  - 对方发的：浅灰色 (#E4E6EB)，左对齐
  - 系统消息：居中，无气泡背景

策略：
  从截图底部 30% 区域（排除键盘/输入框）逐行向上扫描，
  找到首个有显著彩色（蓝/紫）或灰色区域的行段，根据其 X 位置判断归属。
"""

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from PIL import Image
    _PIL_OK = True
except ImportError:
    _PIL_OK = False
    Image = None  # type: ignore


def _is_blue_purple(r: int, g: int, b: int) -> bool:
    """Messenger 自发气泡色域：蓝 (#0084FF) ~ 紫 (#9B59B6)。"""
    # 蓝色系：R < 80, G 50-180, B > 200
    if r < 80 and 40 < g < 200 and b > 180:
        return True
    # 紫色系：R 100-180, G < 100, B > 150
    if 80 < r < 200 and g < 120 and b > 140:
        return True
    # Messenger 主蓝色 #0084FF 附近
    if r < 40 and 100 < g < 160 and b > 220:
        return True
    return False


def _is_peer_gray(r: int, g: int, b: int) -> bool:
    """对方气泡灰色 (#E4E6EB 及近似)。

    排除纯白/近白背景 (>240) — 这不是气泡，是聊天页底色。
    """
    if 200 < r < 240 and 200 < g < 240 and 200 < b < 240:
        diff = max(r, g, b) - min(r, g, b)
        if diff < 20:
            return True
    return False


def detect_latest_sender(
    png_path: str,
    *,
    scan_top_pct: float = 0.45,
    scan_bottom_pct: float = 0.85,
    min_bubble_pixels: int = 40,
) -> Tuple[str, dict]:
    """检测线程截图中最新可见消息的发送方。

    Args:
        png_path: 线程截图路径
        scan_top_pct: 扫描区域上边界（屏幕高度百分比）
        scan_bottom_pct: 扫描区域下边界（排除键盘/输入框）
        min_bubble_pixels: 一行内最少彩色/灰色像素数才算有效气泡

    Returns:
        (sender, debug_info) — sender 为 "self" / "peer" / "unknown"
    """
    info: dict = {"method": "bubble_pixel"}
    if not _PIL_OK:
        return "unknown", {**info, "error": "pillow_not_installed"}

    try:
        img = Image.open(png_path).convert("RGB")
    except Exception as ex:
        return "unknown", {**info, "error": f"open_failed:{ex}"}

    w, h = img.size
    px = img.load()
    info["image_size"] = (w, h)

    y_start = int(h * scan_top_pct)
    y_end = int(h * scan_bottom_pct)
    # 从底部向上扫，找到第一个含有气泡色的行段
    x_mid = w // 2

    # 第一轮：从底向上找蓝色气泡（自发）—— 蓝色很独特，误报极低
    for y in range(y_end, y_start, -1):
        blue_right = 0
        blue_left = 0
        for x in range(10, w - 10, 2):
            r, g, b = px[x, y]
            if _is_blue_purple(r, g, b):
                if x >= x_mid:
                    blue_right += 1
                else:
                    blue_left += 1
        total_blue = blue_left + blue_right
        if total_blue >= min_bubble_pixels:
            info.update(y=y, blue_l=blue_left, blue_r=blue_right, pass_="blue")
            return "self", info

    # 第二轮：从底向上找灰色气泡（对方）—— 阈值更严格避免背景误判
    gray_threshold = max(min_bubble_pixels, 60)
    for y in range(y_end, y_start, -1):
        gray_left = 0
        gray_right = 0
        for x in range(10, w - 10, 2):
            r, g, b = px[x, y]
            if _is_peer_gray(r, g, b):
                if x < x_mid:
                    gray_left += 1
                else:
                    gray_right += 1
        total_gray = gray_left + gray_right
        if total_gray >= gray_threshold:
            # 灰色集中在左侧才是对方气泡
            if gray_left > gray_right * 1.3:
                info.update(y=y, gray_l=gray_left, gray_r=gray_right, pass_="gray")
                return "peer", info
            # 灰色均匀分布 → 背景，继续
            continue

    return "unknown", {**info, "note": "no_bubble_found"}
