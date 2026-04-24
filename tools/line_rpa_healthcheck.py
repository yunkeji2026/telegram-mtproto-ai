#!/usr/bin/env python3
"""
LINE RPA 环境自检：adb、Tesseract、Python 依赖、可选设备。

  python tools/line_rpa_healthcheck.py
  python tools/line_rpa_healthcheck.py --lenient
      # 仅 adb + Pillow 失败时非零；Tesseract/OCR/智谱 缺失仍退出 0
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="LINE RPA healthcheck")
    ap.add_argument(
        "--lenient",
        action="store_true",
        help="仅强制项失败时退出 1（OCR/Tesseract/智谱 为可选）",
    )
    args = ap.parse_args()

    out: dict = {"checks": [], "lenient": args.lenient}

    def chk(name: str, detail: str, pass_: bool, *, tier: str) -> None:
        out["checks"].append(
            {"name": name, "ok": pass_, "detail": detail, "tier": tier}
        )

    adb = shutil.which("adb")
    chk("adb_in_path", adb or "not found", bool(adb), tier="required")
    if adb:
        r = subprocess.run(
            [adb, "devices", "-l"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        chk("adb_devices", (r.stdout or "")[:800], r.returncode == 0, tier="required")

    tess = shutil.which("tesseract")
    chk(
        "tesseract_in_path",
        tess or "not found (screenshot OCR will fail)",
        bool(tess),
        tier="optional",
    )

    try:
        import pytesseract  # noqa: F401

        chk("import_pytesseract", "ok", True, tier="optional")
    except Exception as e:
        chk("import_pytesseract", str(e), False, tier="optional")

    try:
        from PIL import Image  # noqa: F401

        chk("import_pillow", "ok", True, tier="required")
    except Exception as e:
        chk("import_pillow", str(e), False, tier="required")

    try:
        from zhipuai import ZhipuAI  # noqa: F401

        chk("import_zhipuai", "ok", True, tier="optional")
    except Exception as e:
        chk("import_zhipuai", str(e), False, tier="optional")

    print(json.dumps(out, ensure_ascii=False, indent=2))

    if args.lenient:
        bad = [c for c in out["checks"] if not c["ok"] and c.get("tier") == "required"]
    else:
        bad = [c for c in out["checks"] if not c["ok"]]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
