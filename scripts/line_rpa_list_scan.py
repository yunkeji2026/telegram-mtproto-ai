"""LINE RPA 聊天列表扫描 + 滑动探测（vision 驱动）。

这个脚本替代 uiautomator-based chat_list_scanner 的职责。

能力
----
* 截取聊天列表页 → 调 glm-4v-flash → 抽取每一行：name/preview/time/unread_badge
* 通过向上滑 scroll_to_top，再向下滑扫完整列表
* 每轮产生一个 unread_rows 汇总，按 y 顺序排好

用法
----
    python scripts/line_rpa_list_scan.py                 # 扫一屏
    python scripts/line_rpa_list_scan.py --multi-page    # 向下滑扫多屏
    python scripts/line_rpa_list_scan.py --serial xxx
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.line_rpa_live_test import (  # noqa: E402
    _strip_code_fence,
    load_cfg,
    pick_device,
    run_adb,
    screencap,
)

LIST_PROMPT = (
    "你在分析一张 LINE 手机的【聊天列表】页截图（不是聊天详情页）。\n"
    "典型布局：顶栏是『聊天』标题；往下是搜索框；往下是一行一行的聊天项，每一项从左到右依次是：\n"
    "  头像 | 对方昵称 + 最近消息预览 | 右侧的最近时间 + 可能的未读数字红点（圆形红底白字数字）\n"
    "如果某行右侧没有红底白字的数字角标，就视为**没有未读**（last_seen 已读）。\n"
    "有些行会显示绿色『N』角标（表示新功能而不是未读消息），也算**非未读**。\n"
    "LINE 系统账号（账号昵称是『LINE』或『Keep 备忘录』）永远算非未读，除非用户明确验证。\n"
    "\n"
    "请从上到下列出当前屏幕可见的**所有聊天行**，严格按下列 JSON 数组输出（只输出 JSON，"
    "不要 markdown 包裹、不要多余解释）：\n"
    '[{"name":"...","preview":"...","time":"...","unread":0,"is_system":false},'
    '{"name":"...","preview":"...","time":"...","unread":3,"is_system":false}]\n'
    "字段说明：\n"
    " - name：对方昵称或群名，原样\n"
    " - preview：最近一条消息预览；如果是『XX 发送了贴图/图片/语音』也照抄\n"
    " - time：右上角的时间（『上午10:17』『昨天』『4/18』等）\n"
    " - unread：该行右侧数字角标的数字；无角标填 0\n"
    " - is_system：LINE 官方帐号 / Keep 备忘录 填 true，普通对话填 false\n"
    "\n"
    "如果整屏没有任何聊天行，输出 []。按从上到下顺序。"
)


def _post_vision(cfg: dict, img_path: Path, prompt: str) -> str:
    vcfg = cfg.get("vision") or {}
    api_key = vcfg.get("api_key", "")
    model = vcfg.get("model", "glm-4v-flash")
    if not api_key:
        raise RuntimeError("vision.api_key 未配置")
    b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{b64}"
                }},
            ],
        }],
        "temperature": 0.1,
    }
    r = requests.post(
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=body, timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def crop_list_area(img_path: Path) -> Path:
    """裁掉顶部『聊天』标题 + 搜索框 + 底部 tab 栏，保留中间 list 区域。"""
    try:
        from PIL import Image
    except ImportError:
        return img_path
    img = Image.open(img_path).convert("RGB")
    W, H = img.size
    y0 = int(H * 0.11)   # 跳过顶部标题 + 搜索框
    y1 = int(H * 0.90)   # 跳过底部 tab 栏
    out = img_path.with_name(img_path.stem + "_list.png")
    img.crop((0, y0, W, y1)).save(out, format="PNG", optimize=True)
    return out


def parse_list(cfg: dict, img_path: Path) -> list[dict[str, Any]]:
    raw = _post_vision(cfg, crop_list_area(img_path), LIST_PROMPT)
    raw = _strip_code_fence(raw)
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return [{"_raw": raw}]


def swipe_down(serial: str) -> None:
    """从屏幕 80% 位置向上拖到 30% —— 等于让列表向下滚。"""
    run_adb(serial, "shell", "input", "swipe",
            "360", "1280", "360", "480", "450")
    time.sleep(0.8)


def swipe_up(serial: str) -> None:
    """等价 scroll to previous page。"""
    run_adb(serial, "shell", "input", "swipe",
            "360", "480", "360", "1280", "450")
    time.sleep(0.8)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", default="")
    parser.add_argument("--multi-page", action="store_true",
                        help="向下滑扫多屏（最多 5 屏）")
    parser.add_argument("--max-pages", type=int, default=5)
    args = parser.parse_args()

    serial = args.serial or pick_device()
    cfg = load_cfg()
    print(f"[*] 设备：{serial}\n")

    seen_signatures: set[tuple[str, str]] = set()
    total_rows: list[dict[str, Any]] = []
    unread_rows: list[dict[str, Any]] = []

    for page in range(1, args.max_pages + 1):
        screen = ROOT / f"tmp_list_p{page}.png"
        screencap(serial, screen)
        rows = parse_list(cfg, screen)

        print(f"===== Page {page} =====")
        if not rows:
            print("  (空)")
        for row in rows:
            if "_raw" in row:
                print(f"  [PARSE-ERR] {row['_raw'][:120]}")
                continue
            sig = (row.get("name", ""), row.get("time", ""))
            if sig in seen_signatures:
                print(f"  [dup] {row}")
                continue
            seen_signatures.add(sig)
            total_rows.append(row)
            marker = "🔴" if int(row.get("unread", 0) or 0) > 0 else "  "
            print(f"  {marker} name={row.get('name')!r}  "
                  f"time={row.get('time')!r}  "
                  f"unread={row.get('unread')}  "
                  f"preview={row.get('preview')!r}")
            if int(row.get("unread", 0) or 0) > 0 and not row.get("is_system"):
                unread_rows.append(row)
        print()

        if not args.multi_page:
            break
        if page < args.max_pages:
            swipe_down(serial)

    print("========== 汇总 ==========")
    print(f"扫到聊天行总数：{len(total_rows)}")
    print(f"有未读数字且非系统帐号：{len(unread_rows)}")
    for row in unread_rows:
        print(f"  · {row.get('name')}  未读={row.get('unread')}  "
              f"preview={row.get('preview')!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
