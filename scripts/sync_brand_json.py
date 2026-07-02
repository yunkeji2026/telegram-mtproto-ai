"""品牌 JSON 同步：Python 常量 ↔ website/lib/brand.ts ↔ static/brand/brand.json。

默认（``python -m scripts.sync_brand_json``）从 ``branding.brand_catalog()`` 写 JSON。

加 ``--from-ts`` 时从 ``website/lib/brand.ts`` 抽取公司/智聊/五产品清单，
合并进 JSON（不直接改 branding.py，避免 TS 半解析误写源码）。"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List

from src.utils.branding import (
    DEFAULT_COMPANY_NAME,
    DEFAULT_COMPANY_NAME_EN,
    DEFAULT_PRODUCT_NAME,
    DEFAULT_PRODUCT_NAME_EN,
    DEFAULT_SITE_NAME,
    brand_catalog,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "web" / "static" / "brand" / "brand.json"
TS = ROOT / "website" / "lib" / "brand.ts"

_PRODUCT_KEYS = ("facex", "voicex", "livex", "lingox", "chatx")


def _re1(pattern: str, text: str, group: int = 1) -> str:
    m = re.search(pattern, text, re.S)
    return m.group(group).strip() if m else ""


def parse_brand_ts(path: Path = TS) -> Dict[str, Any]:
    """轻量 regex 解析 brand.ts（足够覆盖公司/智聊/五产品展示名）。"""
    if not path.is_file():
        raise FileNotFoundError(path)
    text = path.read_text(encoding="utf-8")
    cm = re.search(r'company:\s*\{\s*zh:\s*"([^"]+)"\s*,\s*en:\s*"([^"]+)"', text)
    company_zh = cm.group(1) if cm else DEFAULT_COMPANY_NAME
    company_en = cm.group(2) if cm else DEFAULT_COMPANY_NAME_EN
    tm = re.search(r'tagline:\s*\{\s*zh:\s*"([^"]+)"\s*,\s*en:\s*"([^"]+)"', text)
    tagline_zh = tm.group(1) if tm else "让沟通，无界"
    tagline_en = tm.group(2) if tm else "Communication, Boundless."
    products: List[Dict[str, str]] = []
    for key in _PRODUCT_KEYS:
        pos = text.find(f"{key}:")
        if pos < 0:
            continue
        chunk = text[pos : pos + 900]
        zm = re.search(r'zh:\s*"([^"]+)"', chunk)
        em = re.search(r'en:\s*"([^"]+)"', chunk)
        emoji_m = re.search(r'emoji:\s*"([^"]+)"', chunk)
        if not zm:
            continue
        products.append({
            "key": key,
            "zh": zm.group(1),
            "en": em.group(1) if em else "",
            "emoji": emoji_m.group(1) if emoji_m else "",
        })
    chatx = next((p for p in products if p["key"] == "chatx"), {})
    return {
        "company": {"zh": company_zh, "en": company_en},
        "product": {
            "zh": chatx.get("zh") or DEFAULT_PRODUCT_NAME,
            "en": chatx.get("en") or DEFAULT_PRODUCT_NAME_EN,
        },
        "site_name": f"{company_zh} · {chatx.get('zh') or DEFAULT_PRODUCT_NAME}",
        "tagline": {"zh": tagline_zh, "en": tagline_en},
        "products": products,
    }


def build_catalog(*, from_ts: bool = False) -> Dict[str, Any]:
    base = brand_catalog()
    if from_ts:
        ts = parse_brand_ts()
        base["company"] = ts["company"]
        base["product"] = ts["product"]
        base["site_name"] = ts["site_name"]
        base["tagline"] = ts["tagline"]
        base["products"] = ts.get("products") or []
    else:
        base["products"] = [
            {"key": "facex", "zh": "幻颜", "en": "FaceX", "emoji": "🎭"},
            {"key": "voicex", "zh": "幻声", "en": "VoiceX", "emoji": "🎙"},
            {"key": "livex", "zh": "幻影", "en": "LiveX", "emoji": "🎬"},
            {"key": "lingox", "zh": "通译", "en": "LingoX", "emoji": "🌐"},
            {"key": "chatx", "zh": "智聊", "en": "ChatX", "emoji": "💬"},
        ]
    base.setdefault("links", {})
    base["links"].setdefault("website", "https://usdt2026.cc")
    base["links"].setdefault("brand_path", "/brand")
    return base


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync brand.json")
    ap.add_argument("--from-ts", action="store_true", help="merge fields parsed from website/lib/brand.ts")
    args = ap.parse_args()
    data = build_catalog(from_ts=args.from_ts)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)} ({'from-ts' if args.from_ts else 'from-py'})")


if __name__ == "__main__":
    main()
