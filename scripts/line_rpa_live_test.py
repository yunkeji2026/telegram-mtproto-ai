"""LINE RPA 实机联调脚本（最小闭环，绕开 uiautomator）。

用途
----
这台设备（Redmi Pad，720x1600，Android 13）运行 uiautomator dump 时 OOM，
因此生产 runner 的 UI 解析链路不可用。本脚本按 **P2 screenshot_ocr + vision 回退**
思路的精简版，直接串联：

    截图 → 用 vision 模型（zhipu glm-4v-flash）读对方最后一句 →
    deepseek-chat 生成草稿 → （可选）AdbKeyBoard 输入 + 点发送按钮

默认 **dry-run**（只打印草稿 + 坐标，不真发送）。加 `--send` 才真发。

用法
----
    python scripts/line_rpa_live_test.py                   # 干跑
    python scripts/line_rpa_live_test.py --send            # 真发
    python scripts/line_rpa_live_test.py --peer "你好，在吗"  # 跳过 vision 直接用已知文本

前置条件
--------
* ADB 设备在线（脚本自动取第一台）。
* LINE 已打开在目标聊天页（ChatHistoryActivity）。
* AdbKeyBoard 已装 + 设为默认 IME（本会话已处理）。
* config/config.yaml 中 ai 与 vision 可用。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "config.yaml"


def run_adb(serial: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["adb", "-s", serial, *args]
    return subprocess.run(
        cmd, check=check, capture_output=True, text=True, timeout=30,
    )


def pick_device() -> str:
    r = subprocess.run(
        ["adb", "devices"], check=True, capture_output=True, text=True,
    )
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of"):
            continue
        if "\tdevice" in line:
            return line.split("\t", 1)[0]
    raise RuntimeError("没有可用 ADB 设备；请 `adb connect` 或接 USB")


def screencap(serial: str, local_path: Path) -> None:
    run_adb(serial, "shell", "screencap", "-p", "/sdcard/tmp_screen.png")
    run_adb(serial, "pull", "/sdcard/tmp_screen.png", str(local_path))


def load_cfg() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


VISION_PROMPT = (
    "你在分析一张 LINE 手机聊天截图（已裁掉顶部和底部输入栏）。\n"
    "\n"
    "**关键特征**：LINE 贴图是**卡通形象**（棕色熊、白色兔、黄色小鸡 Sally、青蛙"
    "Leonard 等），通常占据很大面积（通常 200-400 px 高），浮在聊天蓝色背景上，"
    "**没有白色气泡框**，卡通角色左上方会有一个小小的圆形头像。贴图就是对方消息。\n"
    "\n"
    "请**先在内心列出从上到下所有能看到的消息**（忽略日期胶囊和系统功能引导浮窗）"
    "，然后再选出最下面的那一条。\n"
    "\n"
    "\n"
    "信息分类（非常重要）：\n"
    "A) **对方消息**（要被当作 role=peer 的候选）：\n"
    "   - 左侧白色圆角气泡内的文字\n"
    "   - 左侧一张图片缩略图（有相框 / 矩形边缘的真实照片或截图）\n"
    "   - 左侧一张 LINE 贴图（卡通角色，例如 Brown 棕熊 / Cony 白兔 / Sally 小鸡，"
    "常常独立浮在聊天背景上，没有白色气泡框，但**依然算对方消息**）\n"
    "   - 左侧的语音条（蓝色音波图标）、文件卡片\n"
    "B) **己方消息**：右侧绿色气泡 / 右侧贴图 / 右侧图片，一律视为 self\n"
    "C) **必须忽略**：\n"
    "   - 屏幕中部居中的灰色胶囊（如「今天」「昨天」「2026/4/18」）\n"
    "   - 橙色 / 绿色浮窗带 × 关闭按钮的 LINE 功能引导（例如"
    "「点击即可将贴图添加至收藏夹。」「新功能」等），这不是任何一方发的消息\n"
    "   - 顶部标题栏、底部输入栏、键盘区\n"
    "\n"
    "任务：在 A + B 中找出**垂直坐标最大（最靠近屏幕下方输入框）的那一条**，"
    "如果它属于 A → role=peer；属于 B → role=self；完全没找到 → role=none。\n"
    "\n"
    "严格按下列 JSON 格式输出（只输出 JSON 一行，不要 markdown 包裹、不要注释）：\n"
    '{"role":"peer|self|none","kind":"text|image|sticker|voice|file|other",'
    '"content":"...","desc":"..."}\n'
    "字段说明：\n"
    "- kind=text：content 填原文（保留所有标点 emoji，不加引号），desc 空串\n"
    "- kind=image：content 空串，desc 用 15 字内中文描述图内容\n"
    "- kind=sticker：content 空串，desc 用 15 字内中文描述贴图形象和动作（例：'棕熊欢呼撒彩纸'）\n"
    "- kind=voice/file：content 空串，desc 描述时长或文件名\n"
    "\n"
    "最终只输出一行合法 JSON。"
)


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        s = "\n".join(lines).strip()
    return s


def crop_for_vision(img_path: Path) -> Path:
    """裁剪掉顶部标题栏（~7%）和底部输入+键盘区（~12%），
    保留中间聊天区，让 vision 模型注意力集中在"最新对方消息"上。
    """
    try:
        from PIL import Image
    except ImportError:
        return img_path
    img = Image.open(img_path).convert("RGB")
    W, H = img.size
    top = int(H * 0.07)
    bottom = int(H * 0.92)
    cropped = img.crop((0, top, W, bottom))
    out = img_path.with_name(img_path.stem + "_chat.png")
    cropped.save(out, format="PNG", optimize=True)
    return out


def call_vision(cfg: dict, img_path: Path) -> dict:
    """用 zhipu glm-4v-flash 读对方最后一条消息。

    返回 {"role","kind","content","desc","_raw"}
    """
    vcfg = cfg.get("vision") or {}
    api_key = vcfg.get("api_key", "")
    model = vcfg.get("model", "glm-4v-flash")
    if not api_key:
        raise RuntimeError("vision.api_key 未配置")

    # 裁掉顶栏和底部键盘区，提高识别准确率
    crop_path = crop_for_vision(img_path)
    b = crop_path.read_bytes()
    b64 = base64.b64encode(b).decode("ascii")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": VISION_PROMPT},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{b64}"
                }},
            ],
        }],
        "temperature": 0.1,
    }
    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    r = requests.post(url, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    data = r.json()
    raw = data["choices"][0]["message"]["content"].strip()
    raw = _strip_code_fence(raw)
    out: dict = {"_raw": raw}
    try:
        parsed = json.loads(raw)
        out.update({
            "role": str(parsed.get("role") or "none").lower(),
            "kind": str(parsed.get("kind") or "other").lower(),
            "content": str(parsed.get("content") or ""),
            "desc": str(parsed.get("desc") or ""),
        })
    except Exception:
        # 模型有时直接返回纯文本；尽量兜底
        out.update({
            "role": "peer" if raw and raw.upper() != "NONE" else "none",
            "kind": "text",
            "content": "" if raw.upper() == "NONE" else raw,
            "desc": "",
        })
    return out


def build_user_prompt(v: dict) -> str:
    """根据 vision 返回的 kind，给 AI 不同的输入提示。"""
    kind = v.get("kind", "text")
    content = v.get("content", "")
    desc = v.get("desc", "")
    if kind == "text":
        return content
    if kind == "image":
        return f"[对方发来一张图片] 内容：{desc or '看不清楚'}"
    if kind == "sticker":
        return f"[对方发来一张 LINE 贴图] 含义：{desc or '表情动作'}"
    if kind == "voice":
        return f"[对方发来一段语音] {desc}"
    if kind == "file":
        return f"[对方发来一个文件] {desc}"
    return f"[对方发来一条非文字消息] {desc}"


def call_ai_reply(cfg: dict, v: dict) -> str:
    """用 deepseek-chat 生成一条简短友好的中文回复。

    接收 vision 返回的结构化结果，支持 text / image / sticker 等多种场景。
    """
    ai = cfg.get("ai") or {}
    api_key = ai.get("api_key", "")
    base_url = ai.get("base_url", "https://api.deepseek.com/v1").rstrip("/")
    model = ai.get("model", "deepseek-chat")
    if not api_key:
        raise RuntimeError("ai.api_key 未配置")

    sys_prompt = (
        "你是一个友好、真诚的中国朋友，用简体中文回复 LINE 聊天。\n"
        "风格：口语化、简短（一般 20 字内，最多 30）、自然随意，不要客套，不要罗列。\n"
        "输入格式说明：\n"
        " - 纯文字：直接是对方原话（可能中文也可能英文）\n"
        " - [对方发来一张图片] / [LINE 贴图] / [语音]：括号内是情境描述\n"
        "回复规则：\n"
        " - 对方说英文时，你也可以中英混合回复，但以中文为主\n"
        " - 对方发图片时：自然地对图片内容作简短评论或追问（例：'这是啥？' '哇 好看!' '哈哈'）\n"
        " - 对方发贴图时：用 emoji 或简短表情词回应（例：'哈哈哈' '🤣' '可爱!'），不要干巴巴\n"
        " - 不要重复对方的话；不要用「您」；可适当用感叹号 / emoji\n"
        "只输出回复本身，不要任何前后缀、引号、标签。"
    )

    user_prompt = build_user_prompt(v)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.9,
        "max_tokens": 120,
    }
    r = requests.post(
        f"{base_url}/chat/completions",
        headers=headers, json=body, timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()


def adbkb_input(serial: str, text: str) -> None:
    """通过 AdbKeyBoard base64 广播输入中文。"""
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    run_adb(
        serial, "shell",
        "am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", b64,
    )


def adbkb_clear(serial: str) -> None:
    run_adb(serial, "shell", "am", "broadcast", "-a", "ADB_CLEAR_TEXT")


def tap(serial: str, x: int, y: int) -> None:
    run_adb(serial, "shell", "input", "tap", str(x), str(y))


def find_send_button_xy_from_image(img_path: Path) -> tuple[int, int] | None:
    """简易视觉法：在底部横条里找蓝色发送箭头 (#5A8DEE 附近) 的质心。

    分辨率假设 720 宽；底部横条 y ∈ [1420, 1560]，右侧四分之一区域 x ∈ [540, 720]。
    返回 (x, y)；找不到返回 None。
    """
    try:
        from PIL import Image
    except ImportError:
        return None

    img = Image.open(img_path).convert("RGB")
    W, H = img.size
    x0, y0, x1, y1 = int(W * 0.75), int(H * 0.885), W, int(H * 0.975)
    px = img.load()
    xs, ys, n = 0, 0, 0
    for yy in range(y0, y1, 2):
        for xx in range(x0, x1, 2):
            r, g, b = px[xx, yy]
            # 蓝色箭头：R<110, G 60-160, B>200
            if r < 120 and 60 <= g <= 180 and b > 200:
                xs += xx
                ys += yy
                n += 1
    if n < 15:
        return None
    return (xs // n, ys // n)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true",
                        help="实际发送（默认只打印草稿）")
    parser.add_argument("--peer", default="",
                        help="直接提供对方文本，跳过 vision 识别")
    parser.add_argument("--serial", default="",
                        help="指定 ADB 序列号（默认自动选第一台）")
    parser.add_argument("--input-x", type=int, default=300)
    parser.add_argument("--input-y", type=int, default=1478)
    parser.add_argument("--force-reply", default="",
                        help="直接用这段文本当回复，跳过 AI 生成")
    args = parser.parse_args()

    serial = args.serial or pick_device()
    print(f"[*] 使用设备: {serial}")

    cfg = load_cfg()

    # Step 1: 截图
    screen = ROOT / "tmp_screen.png"
    print("[1/5] 截屏…")
    screencap(serial, screen)

    # Step 2: 读对方消息
    if args.peer:
        v = {"role": "peer", "kind": "text",
             "content": args.peer, "desc": "", "_raw": ""}
        print(f"[2/5] 使用提供的对方文本: {args.peer!r}")
    else:
        print("[2/5] 调 vision 识别对方最新消息…")
        v = call_vision(cfg, screen)
        print(f"     vision 原始 → {v.get('_raw','')!r}")
        print(f"     解析 → role={v.get('role')} kind={v.get('kind')} "
              f"content={v.get('content')!r} desc={v.get('desc')!r}")
        if v.get("role") != "peer":
            print(f"!!! 最新那条不是对方发的（role={v.get('role')}），退出")
            return 1

    # Step 3: AI 生成草稿
    if args.force_reply:
        reply = args.force_reply
        print(f"[3/5] 使用强制回复: {reply!r}")
    else:
        print("[3/5] 调 AI 生成草稿…")
        reply = call_ai_reply(cfg, v)
        print(f"     AI 草稿 → {reply!r}")

    if not args.send:
        print("\n========== DRY-RUN：草稿如下，未发送 ==========")
        kind = v.get("kind", "text")
        if kind == "text":
            print(f"对方({kind}): {v.get('content','')}")
        else:
            print(f"对方({kind}): {v.get('desc','')}")
        print(f"回复: {reply}")
        print("==============================================")
        print("如需真实发送，重跑时加 --send")
        return 0

    # Step 4: 输入
    print("[4/5] 点输入框 + 清空 + 输入草稿…")
    tap(serial, args.input_x, args.input_y)
    time.sleep(0.7)
    adbkb_clear(serial)
    time.sleep(0.3)
    adbkb_input(serial, reply)
    time.sleep(1.0)

    # Step 5: 点发送按钮
    print("[5/5] 定位发送按钮…")
    screencap(serial, screen)
    send_xy = find_send_button_xy_from_image(screen)
    if not send_xy:
        print("!!! 找不到蓝色发送按钮，使用回车兜底")
        run_adb(serial, "shell", "input", "keyevent", "66")
    else:
        sx, sy = send_xy
        print(f"     发送按钮: ({sx}, {sy})")
        tap(serial, sx, sy)
    time.sleep(1.5)

    # 最后截图佐证
    screencap(serial, screen)
    print("[OK] 已发送。最终截图保存到 tmp_screen.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
