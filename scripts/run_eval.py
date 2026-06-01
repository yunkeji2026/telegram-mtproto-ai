"""评测 CLI：跑意图识别准确率（规则基线，可扩 LLM）。

用法：
  python -m scripts.run_eval                      # 用内置种子集 + 规则预测器
  python -m scripts.run_eval --dataset path.yaml  # 自定义标注集
  python -m scripts.run_eval --json               # 输出 JSON（接 CI/看板）
  python -m scripts.run_eval --threshold 0.85     # 自定义 PASS 阈值

退出码：PASS=0 / FAIL=1（便于接 CI 门禁）。
"""

from __future__ import annotations

import argparse
import json
import sys

from src.eval.dataset import load_intent_samples
from src.eval.intent_eval import evaluate_intent, format_report
from src.eval.predictors import rule_intent_predictor


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="意图识别评测")
    ap.add_argument("--dataset", default="", help="标注集路径(YAML/JSONL)；空=内置种子")
    ap.add_argument("--threshold", type=float, default=0.85, help="PASS 阈值（默认 0.85）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    samples = load_intent_samples(args.dataset or None)
    report = evaluate_intent(rule_intent_predictor(), samples,
                             threshold=args.threshold)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
