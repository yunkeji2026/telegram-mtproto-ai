"""P2.1 sticker 素材生成 — emoji 渲染到 png 占位

用 Windows Segoe UI Emoji 字体渲染 8 类各 6 个 emoji 到 256x256 透明 png。
后续可用开源 sticker pack 替换（同目录结构）。

输出：config/stickers/{happy,love,sad,angry,cute,awkward,wink,thinking}/*.png

用法：python -X utf8 tools/gen_sticker_emoji.py
"""
from __future__ import annotations
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


CATEGORIES: dict[str, list[str]] = {
    "happy": ["\U0001F604", "\U0001F60A", "\U0001F606", "\U0001F923",
              "\U0001F601", "\U0001F63A"],
    "love": ["\U0001F60D", "\U0001F970", "\U0001F618", "❤️",
             "\U0001F496", "\U0001F63B"],
    "sad": ["\U0001F622", "\U0001F62D", "\U0001F97A", "\U0001F614",
            "\U0001F61E", "\U0001F494"],
    "angry": ["\U0001F620", "\U0001F621", "\U0001F92C", "\U0001F624",
              "\U0001F4A2", "\U0001F47F"],
    "cute": ["\U0001F970", "\U0001F431", "\U0001F436", "\U0001F338",
             "\U0001F370", "\U0001F98B"],
    "awkward": ["\U0001F605", "\U0001F972", "\U0001F62C", "\U0001F636",
                "\U0001F643", "\U0001F926"],
    "wink": ["\U0001F609", "\U0001F60F", "\U0001F61C", "\U0001F60B",
             "\U0001F92D", "\U0001F608"],
    "thinking": ["\U0001F914", "\U0001F9D0", "\U0001F4AD", "\U0001F636",
                 "❓", "\U0001F644"],
}


def render_emoji(text: str, out_path: Path, *, font_path: str = "seguiemj.ttf",
                 size: int = 256, font_size: int = 200) -> None:
    img = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, font_size)
    d.text((size // 2, size // 2), text,
           embedded_color=True, font=font, anchor="mm")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    out_root = repo / "config" / "stickers"
    total = 0
    for cat, emojis in CATEGORIES.items():
        for i, e in enumerate(emojis, 1):
            out = out_root / cat / f"{cat}_{i:02d}.png"
            try:
                render_emoji(e, out)
                total += 1
            except Exception as ex:
                print(f"  [!] {cat}/{out.name}: {ex}", file=sys.stderr)
    print(f"OK 生成 {total} 张 sticker -> {out_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
