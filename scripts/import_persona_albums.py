"""把旧文件相册 (config/persona_albums/<key>/*) 导入新 persona_media 注册表。

旧机制（companion_selfie backend=album）只是从目录随机挑图，无触发词/命中统计。跑本脚本
把这些静态媒体导入新 DB 注册表（文件复制进 static/persona_albums/<pid>/，供 /static 直服 +
前端预览），之后即可在「人设工作室 → 相册/媒体」面板补触发词/配文、按命中优化。

幂等：按 (persona_id, sha256) 去重，可反复跑。**默认 dry-run**（只报会导入什么），加 --apply 才真导。

用法：
  python -m scripts.import_persona_albums                       # dry-run 全扫，看清单
  python -m scripts.import_persona_albums --apply               # 真导入（全部子目录）
  python -m scripts.import_persona_albums --persona lin --apply # 只导 lin（含根目录散图归 lin）
  python -m scripts.import_persona_albums --triggers "自拍,selfie" --apply   # 导入项带触发词
  python -m scripts.import_persona_albums --json                # JSON 输出（接脚本/CI）

退出码：0=正常（含 dry-run）；1=有导入错误（error>0）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description="导入旧文件相册到 persona_media 注册表")
    ap.add_argument("--src", default="config/persona_albums",
                    help="旧相册根目录（默认 config/persona_albums）")
    ap.add_argument("--db", default="config/persona_media.db",
                    help="persona_media 库路径（默认 config/persona_media.db）")
    ap.add_argument("--persona", default="",
                    help="只导入该人设子目录（并把根目录散图归给它）")
    ap.add_argument("--triggers", default="",
                    help="给所有导入项统一带上的触发词（逗号分隔，可选）")
    ap.add_argument("--apply", action="store_true",
                    help="真导入（缺省为 dry-run，只报清单）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    from src.companion.persona_media_store import configure_persona_media_store
    from src.companion.persona_media_import import import_albums

    store = configure_persona_media_store(args.db)
    if store is None:
        print("persona_media store 不可用（建库失败）", file=sys.stderr)
        return 1

    album_root = Path(__file__).resolve().parents[1] / "src" / "web" / "static" / "persona_albums"
    triggers = [t.strip() for t in args.triggers.split(",") if t.strip()]
    summary = import_albums(
        store, args.src, album_root,
        only_persona=(args.persona or None),
        triggers=triggers or None, apply=bool(args.apply))

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        mode = "APPLY" if summary["apply"] else "DRY-RUN"
        print(f"[{mode}] src={args.src} db={args.db}")
        if not summary["personas"]:
            print("  未发现可导入的相册（子目录/文件为空或类型不支持）")
        for pid, s in summary["personas"].items():
            print(f"  {pid}: files={s['files']} imported={s['imported']} "
                  f"dup={s['dup']} skip={s['skip']} error={s['error']}")
        print(f"合计：imported={summary['total_imported']} dup={summary['total_dup']} "
              f"skip={summary['total_skip']} error={summary['total_error']}")
        if not summary["apply"] and summary["total_imported"]:
            print("（这是 dry-run；加 --apply 才真导入）")

    return 1 if summary.get("total_error", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
