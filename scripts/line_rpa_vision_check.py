"""LINE RPA Vision 多模态识别验证脚本。

把一张完整聊天截图分别裁成若干条带，每条带只包含 1 条消息，
依次喂给 vision，看对每种 kind（text / sticker / image / 英文）的识别准确率。

用法：
    python scripts/line_rpa_vision_check.py [--screen tmp_screen.png]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.line_rpa_live_test import call_vision, load_cfg  # noqa: E402

# (label, top_ratio, bottom_ratio)  — 相对于完整截图高度
# 根据 720x1600 的 Redmi Pad SE 布局手工校准
REGIONS = [
    ("text_cn(你好，在吗)", 0.08, 0.17),
    ("image(快捷键截图)", 0.20, 0.55),
    ("text_en(hello)", 0.55, 0.62),
    ("sticker(棕熊)", 0.63, 0.87),
]


def crop_region(src: Path, top: float, bottom: float, out: Path) -> Path:
    from PIL import Image
    img = Image.open(src).convert("RGB")
    W, H = img.size
    y0 = int(H * top)
    y1 = int(H * bottom)
    img.crop((0, y0, W, y1)).save(out, format="PNG", optimize=True)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screen", default="tmp_screen.png")
    args = parser.parse_args()
    src = (ROOT / args.screen).resolve()
    if not src.exists():
        print(f"找不到截图：{src}")
        return 1
    cfg = load_cfg()

    print(f"[*] 源图：{src}\n")
    for label, top, bottom in REGIONS:
        out = ROOT / f"tmp_region_{label.split('(')[0]}.png"
        crop_region(src, top, bottom, out)
        try:
            v = call_vision(cfg, out)
        except Exception as e:  # noqa: BLE001
            print(f"[{label}] ERROR: {e}\n")
            continue
        print(f"[{label}] crop={out.name}")
        print(f"  raw  : {v.get('_raw','')}")
        print(f"  parse: role={v.get('role')} kind={v.get('kind')} "
              f"content={v.get('content')!r} desc={v.get('desc')!r}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
