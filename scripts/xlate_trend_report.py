"""翻译评测趋势周报：把 logs/eval/translation_trend.jsonl 渲染成可一眼读的对比表。

服务「让数据说话」的周审流程：每周批(translation_eval_weekly.ps1)攒下的趋势行
按 (dataset, engine, back_engine) 分组，各取最近 N 次，输出
  - 总体趋势行（char/sem/pass 率随时间）
  - 最新一次的弱语对 Top-K（按 sem_mean 升序；n<2 样本过少会标注）
读数即可决策 per_lang_order 覆写/阈值调整，无需手工翻 JSONL。

用法：
    python -m scripts.xlate_trend_report [--file logs/eval/translation_trend.jsonl]
        [--last 8] [--worst 5] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


def load_rows(path: str) -> List[dict]:
    """读 JSONL（坏行跳过不炸——趋势文件由多进程追加，防半行）。"""
    p = Path(path)
    if not p.exists():
        return []
    rows: List[dict] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _series_key(row: dict) -> Tuple[str, str, str]:
    dataset = str(row.get("dataset") or row.get("samples_file") or "default")
    # 只留文件名，路径太长影响表格
    dataset = dataset.replace("\\", "/").rsplit("/", 1)[-1]
    engine = str(row.get("engine") or "?")
    back = str(row.get("back_engine") or engine)
    if back in ("same", engine):  # run_eval 自洽口径写 "same"
        back = engine
    return dataset, engine, back


def _fmt(v: Any, nd: int = 3) -> str:
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "-"


def group_series(rows: List[dict], last: int = 8) -> Dict[Tuple[str, str, str], List[dict]]:
    """按 (dataset, engine, back_engine) 分组，各取最近 last 行（保输入序=时间序）。"""
    groups: Dict[Tuple[str, str, str], List[dict]] = {}
    for r in rows:
        groups.setdefault(_series_key(r), []).append(r)
    return {k: v[-last:] for k, v in groups.items()}


def worst_pairs(row: dict, k: int = 5) -> List[dict]:
    """最新趋势行的 by_pair 弱语对 Top-K（sem_mean 升序，缺 sem 用 char_mean）。"""
    bp = row.get("by_pair")
    if not isinstance(bp, dict) or not bp:
        return []
    items: List[dict] = []
    for pair, st in bp.items():
        if not isinstance(st, dict):
            continue
        sem = st.get("sem_mean")
        char = st.get("char_mean")
        score = sem if isinstance(sem, (int, float)) else char
        items.append({
            "pair": str(pair),
            "n": int(st.get("n") or st.get("count") or 0),
            "passed": st.get("passed"),
            "char_mean": char,
            "sem_mean": sem,
            "_score": float(score) if isinstance(score, (int, float)) else 1.0,
        })
    items.sort(key=lambda x: x["_score"])
    for it in items:
        it.pop("_score", None)
    return items[:k]


def render_report(rows: List[dict], last: int = 8, worst: int = 5) -> str:
    """纯函数：趋势行 → 文本报告（无 IO，便于测试）。"""
    if not rows:
        return "(trend file empty — run scripts/translation_eval_weekly.ps1 first)"
    out: List[str] = []
    for (dataset, engine, back), series in sorted(group_series(rows, last=last).items()):
        self_back = " (self-back)" if back == engine else f" (back={back})"
        out.append(f"== {dataset} | engine={engine}{self_back} ==")
        out.append(f"  {'ts':<20} {'n':>4} {'pass':>6} {'char':>7} {'sem':>7}")
        for r in series:
            ts = str(r.get("ts") or r.get("time") or "?")[:19]
            n = r.get("total") or r.get("n") or "-"
            out.append(
                f"  {ts:<20} {str(n):>4} {_fmt(r.get('pass_rate'), 2):>6}"
                f" {_fmt(r.get('mean_score')):>7} {_fmt(r.get('mean_semantic')):>7}"
            )
        wp = worst_pairs(series[-1], k=worst)
        if wp:
            out.append(f"  -- weakest pairs (latest run, by sem asc, top{worst}) --")
            for it in wp:
                note = " (n<2:样本过少)" if it["n"] < 2 else ""
                out.append(
                    f"    {it['pair']:<10} n={it['n']:<3} char={_fmt(it['char_mean'])}"
                    f" sem={_fmt(it['sem_mean'])}{note}"
                )
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default="logs/eval/translation_trend.jsonl")
    ap.add_argument("--last", type=int, default=8)
    ap.add_argument("--worst", type=int, default=5)
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON 而非表格")
    args = ap.parse_args(argv)
    rows = load_rows(args.file)
    if args.json:
        payload = {
            "series": [
                {
                    "dataset": k[0], "engine": k[1], "back_engine": k[2],
                    "runs": v, "worst_pairs": worst_pairs(v[-1], k=args.worst) if v else [],
                }
                for k, v in sorted(group_series(rows, last=args.last).items())
            ]
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_report(rows, last=args.last, worst=args.worst))
    return 0


if __name__ == "__main__":
    sys.exit(main())
