"""自动通过 UI 操作将图片设为壁纸（支持 MIUI）"""
import re
import subprocess
import time

PHONES = [
    {"serial": "IJ8HZLORS485PJWW", "label": "IJ8"},
    {"serial": "Q4N7AM7HMZGU4LZD", "label": "Q4N"},
    {"serial": "VWNJFUNRV4LF4XTS", "label": "VWN"},
    {"serial": "XW8TQKEQIVJRQO69", "label": "XW8"},
]


def adb(serial, *args, timeout=20):
    r = subprocess.run(["adb", "-s", serial] + list(args),
                       capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def tap(serial, x, y):
    adb(serial, "shell", "input", "tap", str(x), str(y))
    time.sleep(0.7)


def back(serial):
    adb(serial, "shell", "input", "keyevent", "KEYCODE_BACK")
    time.sleep(0.5)


def dump_xml(serial, label="x"):
    remote = f"/sdcard/tmp_ui_dump_{label}.xml"
    adb(serial, "shell", "uiautomator", "dump", remote, timeout=15)
    rc, out, _ = adb(serial, "shell", "cat", remote, timeout=10)
    return out if rc == 0 else ""


def find_bounds(xml, *texts):
    """按文本或 content-desc 查找元素中心坐标"""
    for text in texts:
        for attr in ("text", "content-desc"):
            pattern = rf'{attr}="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
            m = re.search(pattern, xml)
            if not m:
                # bounds 可能在前面
                pattern2 = rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*{attr}="{re.escape(text)}"'
                m = re.search(pattern2, xml)
            if m:
                x = (int(m.group(1)) + int(m.group(3))) // 2
                y = (int(m.group(2)) + int(m.group(4))) // 2
                print(f"    找到 {attr}={text!r} → ({x},{y})")
                return x, y
    return None


def set_wallpaper_one(serial, label):
    remote = f"/sdcard/Pictures/phone_label_{label}.png"
    print(f"\n── [{label}] serial={serial} ──")

    # Step 0: 确认文件存在
    rc, _, _ = adb(serial, "shell", "ls", remote)
    if rc != 0:
        print(f"  ✗ 文件不存在，先推送")
        img_path = f"tmp_wallpapers/wallpaper_{label}.png"
        adb(serial, "push", img_path, remote)
        adb(serial, "shell", "am", "broadcast",
            "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
            "-d", f"file://{remote}")
        time.sleep(1)

    # Step 1: 打开图片
    adb(serial, "shell", "am", "force-stop", "com.miui.gallery")
    time.sleep(0.5)
    adb(serial, "shell", "am", "start",
        "-a", "android.intent.action.VIEW",
        "-d", f"file://{remote}",
        "-t", "image/png",
        "--activity-new-task",
        "--activity-clear-task")
    time.sleep(3)

    xml = dump_xml(serial, label)
    if not xml:
        print("  ✗ XML dump 失败，尝试固定坐标点击菜单")
        # 固定坐标 fallback（MIUI 1080x1920 右上角三点菜单）
        tap(serial, 1040, 68)
        time.sleep(1)
        xml = dump_xml(serial, label) or ""

    # Step 2: 找 "更多选项" 或 "⋮" 菜单
    menu_xy = find_bounds(xml,
        "更多选项", "More options", "选项", "菜单",
        "更多", "其他选项")
    if menu_xy:
        tap(serial, *menu_xy)
        time.sleep(1)
        xml = dump_xml(serial, label)

    # Step 3: 点 "设为壁纸" 或 "用作壁纸"
    wp_xy = find_bounds(xml,
        "设为壁纸", "用作壁纸", "设置壁纸",
        "Set as wallpaper", "Use as wallpaper", "Set wallpaper")
    if not wp_xy:
        # MIUI Gallery 可能在底部 ActionBar，试试点右下角 ⋮
        print("  未找到菜单项，尝试点击底部右侧菜单")
        wh_rc, wh_out, _ = adb(serial, "shell", "wm", "size")
        w, h = 1080, 1920
        m = re.search(r"(\d+)x(\d+)", wh_out)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
        tap(serial, int(w * 0.93), int(h * 0.05))   # 右上角
        time.sleep(1)
        xml = dump_xml(serial, label)
        wp_xy = find_bounds(xml,
            "设为壁纸", "用作壁纸", "设置壁纸",
            "Set as wallpaper", "Use as wallpaper")

    if not wp_xy:
        print("  ✗ 未找到「设为壁纸」选项，输出当前 UI 文本:")
        texts = re.findall(r'(?:text|content-desc)="([^"]{2,})"', xml)
        print("   ", [t for t in dict.fromkeys(texts) if len(t) > 1][:20])
        return False

    tap(serial, *wp_xy)
    time.sleep(1.5)
    xml = dump_xml(serial, label)

    # Step 4: 选择 "主屏幕" 或 "主屏幕和锁屏"
    screen_xy = find_bounds(xml,
        "主屏幕", "桌面", "Home screen",
        "Home Screen and Lock Screen", "主屏幕和锁屏",
        "两者", "Both")
    if screen_xy:
        tap(serial, *screen_xy)
        time.sleep(1.5)
        xml = dump_xml(serial, label)

    # Step 5: 确认 "应用" / "设置"
    apply_xy = find_bounds(xml,
        "应用", "设置", "确定", "完成",
        "Apply", "Set", "Done", "OK")
    if apply_xy:
        tap(serial, *apply_xy)
        time.sleep(1)

    print(f"  ✓ [{label}] 壁纸设置流程完成")
    return True


if __name__ == "__main__":
    for p in PHONES:
        try:
            set_wallpaper_one(p["serial"], p["label"])
        except Exception as e:
            print(f"  ✗ [{p['label']}] 异常: {e}")
