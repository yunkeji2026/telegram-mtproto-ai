"""
生成手机编号壁纸并通过 ADB 设置。
用法：python tools/set_phone_wallpaper.py
"""
import os
import subprocess
import sys
from PIL import Image, ImageDraw, ImageFont

# ── 手机信息表（serial → 显示标签） ────────────────────────────────
# 运行前请先核对 IP 与手机编号，修改此处
PHONES = [
    {"serial": "IJ8HZLORS485PJWW", "label": "IJ8",  "ip": "192.168.0.133", "phone_no": "?"},
    {"serial": "Q4N7AM7HMZGU4LZD", "label": "Q4N",  "ip": "192.168.0.164", "phone_no": "?"},
    {"serial": "VWNJFUNRV4LF4XTS", "label": "VWN",  "ip": "192.168.8.170", "phone_no": "?"},
    {"serial": "XW8TQKEQIVJRQO69", "label": "XW8",  "ip": "192.168.8.132", "phone_no": "?"},
]

# 每个标签用不同背景色，一眼区分
COLORS = {
    "IJ8": ("#1a3a6b", "#ffffff"),   # 深蓝底 白字
    "Q4N": ("#1a6b2a", "#ffffff"),   # 深绿底 白字
    "VWN": ("#6b1a1a", "#ffffff"),   # 深红底 白字
    "XW8": ("#4a1a6b", "#ffffff"),   # 深紫底 白字
}

W, H = 1080, 1920
OUT_DIR = "tmp_wallpapers"
os.makedirs(OUT_DIR, exist_ok=True)


def make_wallpaper(phone: dict) -> str:
    label   = phone["label"]
    ip      = phone["ip"]
    phone_no = phone["phone_no"]
    bg, fg  = COLORS.get(label, ("#222222", "#ffffff"))

    img  = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)

    # ── 居中大字：手机编号 ──
    try:
        font_big  = ImageFont.truetype("arial.ttf",  260)
        font_mid  = ImageFont.truetype("arial.ttf",  120)
        font_small = ImageFont.truetype("arial.ttf",  70)
    except Exception:
        font_big   = ImageFont.load_default()
        font_mid   = font_big
        font_small = font_big

    # 手机编号（最显眼，居中）
    no_text = f"Phone {phone_no}" if phone_no != "?" else label
    draw.text((W // 2, H // 2 - 180), no_text,
              fill=fg, font=font_big, anchor="mm")

    # 系统代码
    draw.text((W // 2, H // 2 + 160), label,
              fill=fg, font=font_mid, anchor="mm")

    # IP 地址
    draw.text((W // 2, H // 2 + 320), f"IP: {ip}",
              fill=fg, font=font_small, anchor="mm")

    # 顶部装饰条
    draw.rectangle([0, 0, W, 40], fill=fg)
    draw.rectangle([0, H - 40, W, H], fill=fg)

    path = os.path.join(OUT_DIR, f"wallpaper_{label}.png")
    img.save(path)
    print(f"  ✓ 生成 {path}")
    return path


def adb(serial, *args):
    cmd = ["adb", "-s", serial] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def push_and_set(phone: dict, img_path: str):
    serial = phone["serial"]
    label  = phone["label"]
    remote = f"/sdcard/Pictures/phone_label_{label}.png"

    # 1. push
    rc, out, err = adb(serial, "push", img_path, remote)
    if rc != 0:
        print(f"  ✗ push 失败: {err}")
        return

    # 2. 通知媒体库扫描（让图片出现在相册）
    adb(serial, "shell", "am", "broadcast",
        "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
        "-d", f"file://{remote}")

    # 3. 尝试通过 MIUI WallpaperManager 直接设置
    rc2, _, _ = adb(serial, "shell",
        "cmd", "wallpaper", "set-wallpaper",
        "--which", "both", remote)

    if rc2 == 0:
        print(f"  ✓ [{label}] 壁纸已自动设置")
    else:
        # MIUI fallback: 打开图片让用户手动设置（1次点击）
        adb(serial, "shell", "am", "start",
            "-a", "android.intent.action.VIEW",
            "-d", f"file://{remote}",
            "-t", "image/png",
            "--activity-clear-top")
        print(f"  ⚠ [{label}] 自动设置不支持 → 已在手机上打开图片，长按可设为壁纸")


if __name__ == "__main__":
    # ── 使用前先修改 PHONES 表里的 phone_no ──
    # 或通过命令行参数传入：python set_phone_wallpaper.py IJ8=09 Q4N=07 VWN=05 XW8=08
    mapping = {}
    for arg in sys.argv[1:]:
        if "=" in arg:
            k, v = arg.split("=", 1)
            mapping[k.upper()] = v

    if mapping:
        for p in PHONES:
            if p["label"] in mapping:
                p["phone_no"] = mapping[p["label"]]

    print("=== 生成壁纸 ===")
    paths = {}
    for p in PHONES:
        paths[p["label"]] = make_wallpaper(p)

    print("\n=== 推送到手机 ===")
    for p in PHONES:
        print(f"\n[{p['label']}] serial={p['serial']}")
        push_and_set(p, paths[p["label"]])

    print("\n完成！")
    print("壁纸图片也保存在 tmp_wallpapers/ 目录，可用于手动设置。")
