"""可信指标流水线（P0-5 D11）：把 run_eval 的硬门禁结果导出成营销站可引用的 JSON。

逐项调用 ``python -m scripts.run_eval <flag> --json``，解析报告，产出
``website/public/metrics/<key>.json`` + 汇总 ``index.json``。营销站由此引用
**真实评测数字**（营销可以强，数字必须真）。

设计要点：
- 缺资源的评测（翻译引擎无 key / KB 未备货 / 无真实嵌入）**优雅跳过**记为 skipped，
  绝不 fail —— 与 run_eval 自身的 skip 约定一致（exit 0 + [note] 文案、无 JSON）。
- 公开产物只保留**汇总数字**（passed / 召回率 / 准确率 / 计数等标量），剥掉逐样本
  明细（details/failures 列表），避免把内部评测语料原文发布到公网站点。
- 脚本默认 exit 0（产出即成功）；``--strict`` 时任一 fail/error 返回 1（接 CI 用）。

用法：
  python scripts/gen-trust-metrics.py                 # 全量（缺资源自动 skip）
  python scripts/gen-trust-metrics.py --only persona,crisis-overview
  python scripts/gen-trust-metrics.py --out website/public/metrics --strict
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

# key → (run_eval 追加参数, 站点展示标签 zh/en)。key 同时是输出文件名。
EVALS: List[Dict[str, Any]] = [
    {"key": "persona", "args": ["--persona"],
     "label": {"zh": "人设一致性（客服腔/AI 自曝零漏抓）", "en": "Persona consistency"}},
    {"key": "emotion", "args": ["--emotion"],
     "label": {"zh": "情绪维度识别准确率", "en": "Emotion dimension accuracy"}},
    {"key": "emotion-intensity", "args": ["--emotion-intensity"],
     "label": {"zh": "情绪强度分级单调性", "en": "Emotion intensity grading"}},
    {"key": "crisis", "args": ["--crisis"],
     "label": {"zh": "危机识别（severe 召回红线）", "en": "Crisis detection"}},
    {"key": "crisis-response", "args": ["--crisis-response"],
     "label": {"zh": "危机响应闭环（识别→处置）", "en": "Crisis response loop"}},
    {"key": "crisis-resource", "args": ["--crisis-resource"],
     "label": {"zh": "危机资源保障（热线补发）", "en": "Crisis resource assurance"}},
    {"key": "crisis-overview", "args": ["--crisis-overview"],
     "label": {"zh": "危机安全总览（全链合并门禁）", "en": "Crisis safety overview"}},
    {"key": "proactive-guard", "args": ["--proactive-guard"],
     "label": {"zh": "主动触达情绪护栏", "en": "Proactive outreach guard"}},
    {"key": "memory-extract", "args": ["--memory-extract"],
     "label": {"zh": "记忆抽取质量（启发式）", "en": "Memory extraction quality"}},
    {"key": "voice-language", "args": ["--voice-language"],
     "label": {"zh": "语音合成语言一致性", "en": "Voice language consistency"}},
    {"key": "xlate-confidence", "args": ["--xlate-confidence"],
     "label": {"zh": "译文置信度 scorer", "en": "Translation confidence scorer"}},
    {"key": "intent", "args": [],
     "label": {"zh": "意图识别准确率（规则基线）", "en": "Intent accuracy (rule baseline)"}},
    # ↓ 资源依赖型：缺 key/KB/嵌入时 run_eval 自身 exit 0 + 无 JSON → skipped
    {"key": "translation", "args": ["--translation"],
     "label": {"zh": "翻译回译质量（确定性引擎）", "en": "Back-translation quality"}},
    {"key": "faq", "args": ["--faq"],
     "label": {"zh": "FAQ 自解决率", "en": "FAQ self-resolution rate"}},
    {"key": "memory", "args": ["--memory"],
     "label": {"zh": "记忆召回（关键词 vs 向量）", "en": "Memory recall (kw vs vector)"}},
]


def _slim(value: Any) -> Any:
    """公开版报告瘦身：保留标量与嵌套 dict 的标量叶子，剥掉列表（逐样本明细）。"""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            sv = _slim(v)
            if sv is not None:
                out[k] = sv
        return out
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # list/tuple（details/failures/samples…）一律不进公开产物
    return None


def _parse_report(stdout: str) -> Optional[Dict[str, Any]]:
    """从混有 [info]/[note] 行的输出里剥出 JSON 报告（indent=2，起于行首 '{'）。"""
    idx = stdout.find("\n{")
    if idx >= 0:
        blob = stdout[idx + 1:]
    elif stdout.lstrip().startswith("{"):
        blob = stdout[stdout.find("{"):]
    else:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def run_one(spec: Dict[str, Any], *, timeout: float = 300.0) -> Dict[str, Any]:
    """跑单项评测：pass / fail / skipped / error 四态，永不抛。"""
    cmd = [sys.executable, "-m", "scripts.run_eval", *spec["args"], "--json"]
    entry: Dict[str, Any] = {
        "key": spec["key"],
        "label": spec["label"],
        "cmd": "python -m scripts.run_eval " + " ".join([*spec["args"], "--json"]),
    }
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        proc = subprocess.run(
            cmd, cwd=str(REPO_ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        entry.update(status="error", note=f"timeout after {timeout:.0f}s")
        return entry
    except Exception as exc:  # 启动失败等
        entry.update(status="error", note=str(exc)[:300])
        return entry

    report = _parse_report(proc.stdout or "")
    if report is None:
        note = " ".join((proc.stdout or "").split())[:300]
        if proc.returncode == 0:
            # run_eval 的资源缺失约定：exit 0 + [note] 说明 + 无 JSON
            entry.update(status="skipped", note=note or "resource unavailable")
        else:
            err = " ".join((proc.stderr or "").split())[-300:]
            entry.update(status="error", note=(note or err or "no report"))
        return entry

    passed = report.get("passed")
    if passed is None and "delta_recall" in report:   # --memory 对比模式
        passed = float(report.get("delta_recall") or 0) >= 0
    entry.update(
        status="pass" if (passed and proc.returncode == 0) else "fail",
        report=_slim(report),
    )
    return entry


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="可信指标流水线：run_eval → website/public/metrics/*.json")
    ap.add_argument("--out", default="website/public/metrics",
                    help="输出目录（默认 website/public/metrics）")
    ap.add_argument("--only", default="",
                    help="仅跑这些 key（逗号分隔，如 persona,crisis-overview）")
    ap.add_argument("--timeout", type=float, default=300.0, help="单项超时秒数")
    ap.add_argument("--strict", action="store_true",
                    help="任一 fail/error 时 exit 1（默认恒 0，缺资源 skip 不算失败）")
    ap.add_argument("--json", action="store_true", help="把汇总打到 stdout")
    args = ap.parse_args(argv)

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    specs = [s for s in EVALS if not only or s["key"] in only]
    if only and len(specs) != len(only):
        missing = only - {s["key"] for s in specs}
        print(f"[warn] 未知评测 key，忽略: {sorted(missing)}")

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = REPO_ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    generated_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    entries: List[Dict[str, Any]] = []
    for spec in specs:
        entry = run_one(spec, timeout=args.timeout)
        entry["generated_at"] = generated_at
        entries.append(entry)
        (out_dir / f"{spec['key']}.json").write_text(
            json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[{entry['status']:>7}] {spec['key']}"
              + (f" — {entry.get('note', '')}" if entry.get("note") else ""))

    counts = {st: sum(1 for e in entries if e["status"] == st)
              for st in ("pass", "fail", "skipped", "error")}
    index = {
        "generated_at": generated_at,
        "counts": counts,
        "evals": [{"key": e["key"], "label": e["label"], "status": e["status"],
                   "file": f"{e['key']}.json"} for e in entries],
    }
    (out_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[done] {len(entries)} evals → {out_dir}  "
          f"(pass={counts['pass']} fail={counts['fail']} "
          f"skipped={counts['skipped']} error={counts['error']})")
    if args.json:
        print(json.dumps(index, ensure_ascii=False, indent=2))
    if args.strict and (counts["fail"] or counts["error"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
