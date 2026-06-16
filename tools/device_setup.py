# -*- coding: utf-8 -*-
"""
设备注册 + 壁纸部署一体化工具
用法示例:
  # 注册并部署所有当前连接的手机
  python tools/device_setup.py register-all

  # 只部署壁纸（不修改注册信息）
  python tools/device_setup.py wallpaper

  # 显示设备清单
  python tools/device_setup.py list

  # 单台注册：serial label number group
  python tools/device_setup.py register Q4N7AM7HMZGU4LZD Q4N 7 主控

壁纸内容：
  ┌─────────────────────────────┐
  │           07                │  ← 编号（大字）
  │          Q4N                │  ← 系统简码（中）
  │      主控-手机07             │  ← 完整位置标签（小）
  └─────────────────────────────┘
"""

from __future__ import annotations

import colorsys
import hashlib
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ── 路径常量 ─────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
_MOBILE_ROOT = _PROJECT_ROOT.parent / "mobile-auto0423"
_JAR_LOCAL = _MOBILE_ROOT / "tools" / "wallpaper_helper" / "openclaw_wp.jar"
_REMOTE_JAR = "/data/local/tmp/openclaw_wp.jar"
_REMOTE_WP = "/sdcard/Download/openclaw_wallpaper.png"

# 初始化项目 sys.path
sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
log = logging.getLogger("device_setup")


# ═════════════════════════════════════════════════════════════════════════
#  ADB 工具
# ═════════════════════════════════════════════════════════════════════════

def _adb(serial: str, *args, timeout: int = 20) -> tuple[bool, str]:
    cmd = ["adb", "-s", serial] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode == 0, out.strip()
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


def _connected_serials() -> list[str]:
    r = subprocess.run(["adb", "devices"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    serials = []
    for line in r.stdout.splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) == 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def _get_prop(serial: str, prop: str) -> str:
    ok, out = _adb(serial, "shell", "getprop", prop)
    return out.strip() if ok else ""


def _get_wifi_ip(serial: str) -> str:
    ok, out = _adb(serial, "shell", "ip", "addr", "show", "wlan0")
    if ok:
        import re
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    return ""


# ═════════════════════════════════════════════════════════════════════════
#  壁纸图片生成
# ═════════════════════════════════════════════════════════════════════════

def _number_color(number: int):
    """黄金角色相 → 独特颜色，与 mobile-auto0423 保持一致。"""
    hue = (number * 137.508) % 360
    r, g, b = colorsys.hls_to_rgb(hue / 360.0, 0.15, 0.55)
    accent_r, accent_g, accent_b = colorsys.hls_to_rgb(hue / 360.0, 0.55, 0.75)
    return (
        (int(r * 255), int(g * 255), int(b * 255)),
        (int(accent_r * 255), int(accent_g * 255), int(accent_b * 255)),
    )


def _get_font(size: int):
    from PIL import ImageFont
    for path in [
        "C:/Windows/Fonts/msyh.ttc",    # 微软雅黑（支持中文）
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def generate_wallpaper(number: int, label: str, location: str,
                       width: int = 1080, height: int = 1920,
                       output_dir: str = "") -> str:
    """
    生成包含 编号 + 简码 + 位置 的壁纸图片。
    返回本地 PNG 路径。
    """
    from PIL import Image, ImageDraw

    bg_color, accent_color = _number_color(number)
    out_dir = output_dir or os.path.join(tempfile.gettempdir(), "tg_wallpapers")
    os.makedirs(out_dir, exist_ok=True)

    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)

    # 渐变背景
    for y in range(height):
        ratio = y / height
        r = int(bg_color[0] * (1 - ratio * 0.3))
        g = int(bg_color[1] * (1 - ratio * 0.3))
        b = int(bg_color[2] * (1 - ratio * 0.3))
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    cx = width // 2

    # 光晕
    glow_r = int(width * 0.38)
    cy_glow = int(height * 0.38)
    for offset in range(glow_r, 0, -3):
        ratio = offset / glow_r
        rc = int(accent_color[0] * (1 - ratio * 0.8))
        gc = int(accent_color[1] * (1 - ratio * 0.8))
        bc = int(accent_color[2] * (1 - ratio * 0.8))
        draw.ellipse([cx - offset, cy_glow - offset, cx + offset, cy_glow + offset],
                     fill=(rc, gc, bc))

    # 圆环
    ring_r = int(width * 0.28)
    for dr in range(5):
        draw.ellipse(
            [cx - ring_r - dr, cy_glow - ring_r - dr,
             cx + ring_r + dr, cy_glow + ring_r + dr],
            outline=accent_color,
        )

    def draw_center(text, font, y, color=(255, 255, 255), shadow=False):
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        x = (width - tw) // 2
        if shadow:
            for dx, dy in [(3, 3), (-2, -2), (4, 4)]:
                draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0))
        draw.text((x, y), text, font=font, fill=color)

    # ── 大字编号
    font_num = _get_font(int(width * 0.52))
    draw_center(f"{number:02d}", font_num, int(height * 0.22), shadow=True)

    # ── "号机" 小标
    font_sub = _get_font(int(width * 0.06))
    draw_center("号 机", font_sub, int(height * 0.5),
                color=accent_color)

    # ── 系统简码
    font_label = _get_font(int(width * 0.075))
    draw_center(label, font_label, int(height * 0.58),
                color=(200, 220, 255))

    # ── 分隔线
    line_y = int(height * 0.68)
    margin = int(width * 0.2)
    draw.line([(margin, line_y), (width - margin, line_y)],
              fill=tuple(max(0, c - 60) for c in accent_color), width=2)

    # ── 位置标签（中文）
    font_loc = _get_font(int(width * 0.055))
    draw_center(location, font_loc, int(height * 0.71),
                color=(148, 163, 184))

    # ── 底部 brand
    font_brand = _get_font(int(width * 0.035))
    draw_center("华灵 Engine · AI RPA", font_brand, int(height * 0.88),
                color=(80, 100, 120))

    out_path = os.path.join(out_dir, f"wp_{number:02d}_{label}.png")
    img.save(out_path, format="PNG", optimize=True)
    log.info("壁纸生成 → %s", out_path)
    return out_path


# ═════════════════════════════════════════════════════════════════════════
#  壁纸部署（优先 app_process + jar，零安装）
# ═════════════════════════════════════════════════════════════════════════

def deploy_wallpaper(serial: str, img_path: str) -> bool:
    """推送图片并通过 app_process jar 直接设置壁纸。"""
    # 1. push 壁纸
    ok, out = _adb(serial, "push", img_path, _REMOTE_WP, timeout=20)
    if not ok:
        log.warning("[%s] push 失败: %s", serial[:8], out[:100])
        return False

    # 2. push jar（如存在）
    if _JAR_LOCAL.exists():
        ok2, out2 = _adb(serial, "push", str(_JAR_LOCAL), _REMOTE_JAR, timeout=15)
        if ok2:
            cmd = (f"CLASSPATH={_REMOTE_JAR} "
                   f"app_process /system/bin "
                   f"com.openclaw.WallpaperSetter {_REMOTE_WP}")
            ok3, out3 = _adb(serial, "shell", cmd, timeout=25)
            if ok3 and "WP_SET_OK" in out3:
                log.info("[%s] ✓ 壁纸设置成功（app_process）", serial[:8])
                return True
            log.warning("[%s] app_process 失败: %s", serial[:8], out3[:200])
    else:
        log.warning("openclaw_wp.jar 不存在: %s", _JAR_LOCAL)

    # 3. fallback: 锁屏文字（至少能标识手机）
    log.info("[%s] fallback: 图片已推送到 %s，请手动设为壁纸", serial[:8], _REMOTE_WP)
    # 触发媒体库扫描，让相册能找到
    _adb(serial, "shell",
         "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE"
         f" -d file://{_REMOTE_WP}")
    return False


# ═════════════════════════════════════════════════════════════════════════
#  设备注册 + 壁纸一体化
# ═════════════════════════════════════════════════════════════════════════

# 已知设备表（serial → 配置）
# 用户可在此处直接维护，或通过 register 子命令动态添加
KNOWN_DEVICES = {
    "IJ8HZLORS485PJWW": {
        "number": 5, "label": "IJ8", "group_name": "主控",
        "platform_line": "line_ij8",
        "platform_messenger": "msg_ij8",
    },
    "Q4N7AM7HMZGU4LZD": {
        "number": 7, "label": "Q4N", "group_name": "主控",
        "platform_whatsapp": "wa_q4n",
        "platform_messenger": "msg_q4n",
    },
    "VWNJFUNRV4LF4XTS": {
        "number": 8, "label": "VWN", "group_name": "主控",
        "platform_messenger": "msg_vwn",
    },
    "XW8TQKEQIVJRQO69": {
        "number": 9, "label": "XW8", "group_name": "主控",
        "platform_line": "line_xw8",
        "platform_messenger": "msg_xw8",
    },
}


def register_and_deploy(serial: str, cfg: dict, deploy: bool = True) -> dict:
    """注册单台设备并（可选）部署壁纸，返回 registry 记录。"""
    from src.shared.device_registry import get_device_registry
    db = get_device_registry()

    # 从设备读取硬件信息
    hw_serial = _get_prop(serial, "ro.serialno") or serial
    android_id_ok, android_id = _adb(serial, "shell", "settings", "get",
                                     "secure", "android_id")
    android_id = android_id.strip() if android_id_ok else ""
    model = _get_prop(serial, "ro.product.model")
    wifi_ip = _get_wifi_ip(serial)

    number = int(cfg.get("number", 0))
    label = cfg.get("label", serial[:4])
    group = cfg.get("group_name", "主控")
    location = cfg.get("location") or f"{group}-手机{number:02d}"

    record = db.upsert(
        serial=serial,
        hw_serial=hw_serial,
        android_id=android_id,
        model=model,
        number=number,
        label=label,
        group_name=group,
        location=location,
        wifi_ip=wifi_ip,
        platform_messenger=cfg.get("platform_messenger", ""),
        platform_line=cfg.get("platform_line", ""),
        platform_whatsapp=cfg.get("platform_whatsapp", ""),
    )

    log.info("注册完成: %s → %s  IP=%s", serial[:8], location, wifi_ip)

    if deploy and number:
        img = generate_wallpaper(number, label, location)
        img_hash = hashlib.md5(open(img, "rb").read()).hexdigest()[:8]
        ok = deploy_wallpaper(serial, img)
        if ok:
            db.mark_wallpaper(serial, img_hash)

    return record


# ═════════════════════════════════════════════════════════════════════════
#  CLI 入口
# ═════════════════════════════════════════════════════════════════════════

def cmd_list():
    from src.shared.device_registry import get_device_registry
    db = get_device_registry()
    print(db.summary())


def cmd_register_all(deploy: bool = True):
    connected = _connected_serials()
    log.info("已连接设备: %s", connected)
    for serial in connected:
        cfg = KNOWN_DEVICES.get(serial)
        if cfg is None:
            log.warning("[%s] 未在 KNOWN_DEVICES 中，跳过", serial[:8])
            continue
        register_and_deploy(serial, cfg, deploy=deploy)
    cmd_list()


def cmd_wallpaper():
    """仅部署壁纸（不修改注册信息）。"""
    from src.shared.device_registry import get_device_registry
    db = get_device_registry()
    connected = _connected_serials()
    for serial in connected:
        record = db.get(serial)
        if not record or not record.get("number"):
            log.warning("[%s] 未注册或无编号，跳过", serial[:8])
            continue
        img = generate_wallpaper(
            record["number"], record["label"], record["location"]
        )
        img_hash = hashlib.md5(open(img, "rb").read()).hexdigest()[:8]
        ok = deploy_wallpaper(serial, img)
        if ok:
            db.mark_wallpaper(serial, img_hash)


def cmd_register_one(serial: str, label: str, number: int, group: str):
    cfg = KNOWN_DEVICES.get(serial, {})
    cfg.update({"label": label, "number": number, "group_name": group})
    register_and_deploy(serial, cfg)
    cmd_list()


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "register-all":
        cmd_register_all(deploy=True)
    elif args[0] == "register-only":
        cmd_register_all(deploy=False)
    elif args[0] == "wallpaper":
        cmd_wallpaper()
    elif args[0] == "list":
        cmd_list()
    elif args[0] == "register" and len(args) >= 5:
        # register <serial> <label> <number> <group>
        cmd_register_one(args[1], args[2], int(args[3]), args[4])
    else:
        print(__doc__)
