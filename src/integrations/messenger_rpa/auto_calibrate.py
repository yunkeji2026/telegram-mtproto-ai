"""Messenger Inbox 行自动标定（像素级）。

每台设备 / 每个 Messenger 版本的会话列表 Y 坐标不尽相同：
  - 标准版本：CHAT_ROW_FIRST_Y=600, CHAT_ROW_HEIGHT=165
  - 某些 u999 XSpace：CHAT_ROW_FIRST_Y=609, CHAT_ROW_HEIGHT=150
  - 新版本可能把 Stories 行做高，挤压会话行

本模块基于一个关键观察：**每一行左侧都有圆形头像**。
通过扫描 x∈[50, 140] 垂直条带的灰度"非白密度"峰值，可以定位
所有"有头像"的 Y 行中心，然后：
  1. 排除最上面（logo/menu 行，y<200）和最下面（tab bar，y>1350）
  2. 排除 Stories 行（第一个"大块头像"，通常 height > 100px 且最靠上）
  3. 剩下的连续等距峰即是会话行

调用方：
    calib = calibrate_inbox_rows(png_path)
    if calib.ok:
        coords.CHAT_ROW_FIRST_Y = calib.first_y
        coords.CHAT_ROW_HEIGHT = calib.row_height
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class InboxCalibration:
    ok: bool
    first_y: int
    row_height: int
    visible_rows: int
    peaks_raw: List[int]
    device_wh: tuple
    reason: str = ""


# 扫描参数
_AVATAR_X_RANGE = (50, 140)   # 头像左列
_NON_WHITE_THRESHOLD = 230    # 灰度 < 该值视作非白
_MIN_DENSITY = 15             # 垂直条带每 y 的非白像素下限
_MIN_AVATAR_HEIGHT = 40       # 连续高密度段最小高度
_LOGO_MAX_Y = 250             # 这一行以上当作 logo/title
_TABBAR_MIN_Y = 1350          # 这一行以下当作 tab bar


def calibrate_inbox_rows(png_path: str) -> InboxCalibration:
    try:
        from PIL import Image
    except ImportError:
        logger.warning("[auto_calibrate] Pillow 未安装，跳过")
        return InboxCalibration(False, 600, 165, 0, [], (0, 0), "no_pillow")

    try:
        img = Image.open(png_path).convert("L")
    except Exception as ex:
        logger.warning("[auto_calibrate] 读图失败 err=%s", ex)
        return InboxCalibration(False, 600, 165, 0, [], (0, 0), f"open_failed:{ex}")

    w, h = img.size
    px = img.load()

    x0, x1 = _AVATAR_X_RANGE
    x0 = int(x0 * w / 720)
    x1 = int(x1 * w / 720)

    density = [0] * h
    for y in range(h):
        cnt = 0
        for x in range(x0, x1):
            if px[x, y] < _NON_WHITE_THRESHOLD:
                cnt += 1
        density[y] = cnt

    # 平滑
    N = 10
    smooth = []
    for i in range(h):
        lo = max(0, i - N)
        hi = min(h, i + N)
        smooth.append(sum(density[lo:hi]) / (hi - lo))

    # 找连续高密度段
    peaks: List[tuple] = []
    i = 0
    min_height = int(_MIN_AVATAR_HEIGHT * h / 1600)
    while i < h:
        if smooth[i] >= _MIN_DENSITY:
            start = i
            while i < h and smooth[i] >= _MIN_DENSITY:
                i += 1
            end = i - 1
            if end - start >= min_height:
                cy = (start + end) // 2
                peaks.append((cy, end - start))
        else:
            i += 1

    logger.info(
        "[auto_calibrate] image=%dx%d raw_peaks=%s",
        w, h, [p[0] for p in peaks]
    )

    # 过滤掉 logo（y<250）和 tab bar（y>1350），按比例缩放
    y_logo = int(_LOGO_MAX_Y * h / 1600)
    y_tabbar = int(_TABBAR_MIN_Y * h / 1600)
    chat_peaks = [
        (cy, pheight) for cy, pheight in peaks
        if y_logo < cy < y_tabbar
    ]

    if not chat_peaks:
        return InboxCalibration(
            False, 600, 165, 0, [p[0] for p in peaks], (w, h),
            "no_chat_peaks"
        )

    # 判断首行 = Stories 还是会话 1：
    # Stories 行通常 height >= 100px（大圆头像列）；若 height < 100 就认为已是会话第 1 行
    start_idx = 0
    if chat_peaks[0][1] >= int(100 * h / 1600):
        # Stories 行，跳过
        start_idx = 1

    chat_peaks = chat_peaks[start_idx:]
    if len(chat_peaks) < 2:
        return InboxCalibration(
            False, 600, 165, 0, [p[0] for p in peaks], (w, h),
            f"too_few_chat_peaks={len(chat_peaks)}"
        )

    # 映射回 720×1600 标准坐标
    ys_norm = [int(c[0] * 1600 / h) for c in chat_peaks]
    first_y = ys_norm[0]

    # row_height：取相邻差值中位数
    diffs = [ys_norm[i+1] - ys_norm[i] for i in range(len(ys_norm)-1)]
    diffs.sort()
    row_height = diffs[len(diffs) // 2]

    # 健全性检查
    if not (500 <= first_y <= 750):
        return InboxCalibration(
            False, 600, 165, 0, [p[0] for p in peaks], (w, h),
            f"first_y_out_of_range={first_y}"
        )
    if not (100 <= row_height <= 220):
        return InboxCalibration(
            False, 600, 165, 0, [p[0] for p in peaks], (w, h),
            f"row_height_out_of_range={row_height}"
        )

    return InboxCalibration(
        ok=True,
        first_y=first_y,
        row_height=row_height,
        visible_rows=len(chat_peaks),
        peaks_raw=[p[0] for p in peaks],
        device_wh=(w, h),
    )


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("usage: python -m src.integrations.messenger_rpa.auto_calibrate <png>")
        sys.exit(1)
    r = calibrate_inbox_rows(sys.argv[1])
    print(r)
