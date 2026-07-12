"""一次性清理：中文入站消息的「假译文」（2026-07 入站翻译修复配套）。

历史缺陷：protocol push 落库的消息 source_lang='unknown'，旧候选判定不重检正文语言，
把中文消息送去「译成中文」——LLM 对同语输入常自由发挥（「你这个是中文」→「嗯嗯，是的呀～」），
闲聊句被当译文写库，前端双行显示污染。源头已修（unknown 标签强制 detect_language 重检）；
本脚本清掉存量：**正文检出为 zh 且带 zh 译文** 的入站行，译文/target_lang 归零
（zh→zh 本就不该有译文；清后这些行也不会再被重译——语言重检直接跳过）。

用法：
    python -m scripts.cleanup_zh_fake_translations           # dry-run（只报数）
    python -m scripts.cleanup_zh_fake_translations --apply   # 实际清理
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ai.translation_service import detect_language  # noqa: E402

DB = Path(__file__).resolve().parent.parent / "config" / "inbox.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际执行（默认 dry-run）")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT message_id, text, translated_text FROM messages "
        "WHERE direction='in' AND target_lang='zh' AND translated_text != '' "
        "AND translated_text != text"
    ).fetchall()

    victims = [
        r["message_id"] for r in rows
        if detect_language(str(r["text"] or "")) == "zh"
    ]
    print(f"候选 {len(rows)} 行（in 向、target=zh、有译文），其中正文检出 zh 的假译文 {len(victims)} 行")
    if not victims:
        conn.close()
        return 0
    if not args.apply:
        for r in rows[:20]:
            if r["message_id"] in victims[:20]:
                print(f"  {str(r['text'])[:30]!r} -> {str(r['translated_text'])[:30]!r}")
        print("dry-run（加 --apply 执行清理）")
        conn.close()
        return 0
    with conn:
        conn.executemany(
            "UPDATE messages SET translated_text='', target_lang='' WHERE message_id=?",
            [(mid,) for mid in victims],
        )
    print(f"已清理 {len(victims)} 行")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
