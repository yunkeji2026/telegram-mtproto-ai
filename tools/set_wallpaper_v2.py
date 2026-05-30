"""通过 MediaStore content URI 打开图片并设为壁纸（MIUI Android 7+）"""
import re, subprocess, time, sys

PHONES = [
    {"serial": "IJ8HZLORS485PJWW", "label": "IJ8",  "phone_no": "?"},
    {"serial": "Q4N7AM7HMZGU4LZD", "label": "Q4N",  "phone_no": "?"},
    {"serial": "VWNJFUNRV4LF4XTS", "label": "VWN",  "phone_no": "?"},
    {"serial": "XW8TQKEQIVJRQO69", "label": "XW8",  "phone_no": "?"},
]


def adb(serial, *args, timeout=20):
    r = subprocess.run(["adb", "-s", serial] + list(args),
                       capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def tap(serial, x, y):
    adb(serial, "shell", "input", "tap", str(x), str(y))
    time.sleep(0.8)


def dump_xml(serial, tag="x"):
    remote = f"/sdcard/tmp_dump_{tag}.xml"
    adb(serial, "shell", "uiautomator", "dump", remote, timeout=15)
    rc, out, _ = adb(serial, "shell", "cat", remote, timeout=10)
    return out if rc == 0 else ""


def find_xy(xml, *texts):
    for text in texts:
        for attr in ("text", "content-desc"):
            for pat in [
                rf'{attr}="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*{attr}="{re.escape(text)}"',
            ]:
                m = re.search(pat, xml)
                if m:
                    x = (int(m.group(1)) + int(m.group(3))) // 2
                    y = (int(m.group(2)) + int(m.group(4))) // 2
                    print(f"    ✓ 找到 [{text}] → ({x},{y})")
                    return x, y
    return None


def get_content_uri(serial, remote_path):
    """从 MediaStore 查询 content:// URI"""
    # 先触发媒体扫描
    adb(serial, "shell", "am", "broadcast",
        "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
        "-d", f"file://{remote_path}")
    time.sleep(1.5)
    rc, out, _ = adb(serial, "shell",
        "content", "query",
        "--uri", "content://media/external/images/media",
        "--projection", "_id",
        "--where", f"_data='{remote_path}'",
        timeout=10)
    m = re.search(r"_id=(\d+)", out)
    if m:
        return f"content://media/external/images/media/{m.group(1)}"
    return None


def process_one(serial, label, phone_no):
    remote = f"/sdcard/Pictures/phone_label_{label}.png"
    print(f"\n── [{label}] serial={serial[:8]}... phone_no={phone_no} ──")

    # 确认图片存在
    rc, _, _ = adb(serial, "shell", "ls", remote)
    if rc != 0:
        print("  推送图片...")
        adb(serial, "push", f"tmp_wallpapers/wallpaper_{label}.png", remote)
        time.sleep(0.5)

    # 获取 content URI
    content_uri = get_content_uri(serial, remote)
    print(f"  content URI: {content_uri}")

    # 先回主屏
    adb(serial, "shell", "input", "keyevent", "KEYCODE_HOME")
    time.sleep(0.5)

    if content_uri:
        # 用 content URI 打开图片
        adb(serial, "shell", "am", "start",
            "-a", "android.intent.action.VIEW",
            "-d", content_uri,
            "-t", "image/png",
            "--activity-new-task")
    else:
        # 直接打开 MIUI Gallery
        adb(serial, "shell", "am", "start",
            "-n", "com.miui.gallery/.activity.GalleryDetailActivity",
            "--es", "filePath", remote,
            "--activity-new-task")
    time.sleep(3)

    xml = dump_xml(serial, label)
    if not xml:
        print("  ✗ XML 获取失败")
        return

    # 输出当前页面关键文字（调试）
    texts = re.findall(r'(?:text|content-desc)="([^"]{2,40})"', xml)
    uniq = list(dict.fromkeys(texts))[:15]
    print(f"  当前UI: {uniq}")

    # 找更多选项菜单
    menu_xy = find_xy(xml, "更多选项", "More options", "更多", "菜单")
    if menu_xy:
        tap(serial, *menu_xy)
        time.sleep(1)
        xml = dump_xml(serial, label + "2")

    # 找「设为壁纸」
    wp_xy = find_xy(xml, "设为壁纸", "用作壁纸", "设置壁纸",
                    "Set as wallpaper", "Use as wallpaper")
    if not wp_xy:
        # 尝试右上角固定坐标
        _, wh_out, _ = adb(serial, "shell", "wm", "size")
        m = re.search(r"(\d+)x(\d+)", wh_out)
        w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 1920)
        print(f"  尝试右上角 ({int(w*0.95)}, {int(h*0.04)})")
        tap(serial, int(w * 0.95), int(h * 0.04))
        time.sleep(1)
        xml = dump_xml(serial, label + "3")
        texts2 = re.findall(r'(?:text|content-desc)="([^"]{2,40})"', xml)
        print(f"  菜单UI: {list(dict.fromkeys(texts2))[:12]}")
        wp_xy = find_xy(xml, "设为壁纸", "用作壁纸", "设置壁纸",
                        "Set as wallpaper", "Use as wallpaper")

    if not wp_xy:
        print("  ✗ 未找到壁纸选项，图片已推送到相册，请手动长按图片→设为壁纸")
        return

    tap(serial, *wp_xy)
    time.sleep(1.5)
    xml = dump_xml(serial, label + "4")

    # 选 主屏幕+锁屏
    sc_xy = find_xy(xml,
        "主屏幕和锁屏", "两者", "Home screen and lock screen",
        "主屏幕", "Home screen", "Both", "确定", "Apply")
    if sc_xy:
        tap(serial, *sc_xy)
        time.sleep(1.5)
        xml = dump_xml(serial, label + "5")

    # 确认
    ok_xy = find_xy(xml, "应用", "完成", "设置", "确定", "Apply", "Set", "Done")
    if ok_xy:
        tap(serial, *ok_xy)
        time.sleep(1)

    print(f"  ✓ [{label}] 壁纸流程完成")


if __name__ == "__main__":
    # 支持命令行指定编号 e.g.: python set_wallpaper_v2.py IJ8=09 Q4N=07 VWN=05 XW8=08
    mapping = {}
    for arg in sys.argv[1:]:
        if "=" in arg:
            k, v = arg.split("=", 1)
            mapping[k.upper()] = v
    for p in PHONES:
        if p["label"] in mapping:
            p["phone_no"] = mapping[p["label"]]

    for p in PHONES:
        try:
            process_one(p["serial"], p["label"], p["phone_no"])
        except Exception as e:
            print(f"  ✗ [{p['label']}] 异常: {e}")

    print("\n完成！如有未成功的手机，图片已在相册，手动长按→设为壁纸即可。")
