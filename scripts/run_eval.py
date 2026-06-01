"""评测 CLI：跑意图识别准确率（规则基线，可对比 LLM）。

用法：
  python -m scripts.run_eval                                  # 内置种子集 + 规则预测器
  python -m scripts.run_eval --dataset config/eval/intent_samples.yaml
  python -m scripts.run_eval --json                           # JSON（接 CI/看板）
  python -m scripts.run_eval --threshold 0.85                 # 自定义 PASS 阈值
  python -m scripts.run_eval --compare                        # rule vs LLM 对比表
                                                              # （LLM 需 EVAL_LLM=1 且配好 ai）

退出码：PASS=0 / FAIL=1（便于接 CI 门禁）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from src.eval.dataset import load_faq_samples, load_intent_samples
from src.eval.faq_eval import evaluate_faq, format_faq_report, kb_search_resolver
from src.eval.intent_eval import (
    compare_predictors, evaluate_intent, format_compare, format_report,
)
from src.eval.predictors import llm_intent_predictor, rule_intent_predictor

logger = logging.getLogger("run_eval")


def _try_build_kb_resolver(kb_db: str, score_threshold: float):
    """构造 KB 解决判定器；KB 不存在/构造失败返回 None。"""
    from pathlib import Path
    candidates = [kb_db] if kb_db else [
        "config/knowledge_base.db", "data/knowledge_base.db",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            try:
                from src.utils.kb_store import KnowledgeBaseStore
                store = KnowledgeBaseStore(Path(c))
                return kb_search_resolver(store, score_threshold=score_threshold)
            except Exception as ex:
                logger.warning("KB resolver 构造失败 %s: %s", c, ex)
                return None
    return None


def _try_build_llm_generate_fn():
    """尝试构造同步 LLM generate_fn；不满足条件返回 None（默认离线安全）。

    仅在 EVAL_LLM=1 时启用，避免脚本默认触发 API 调用/联网。
    """
    if os.environ.get("EVAL_LLM") != "1":
        return None
    try:
        import asyncio
        import yaml
        from src.ai.ai_client import AIClient

        with open("config/config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        class _Cfg:
            config = cfg
            config_path = "config/config.yaml"

            def get_ai_config(self):
                return cfg.get("ai", {})

        client = AIClient(_Cfg())

        def _gen(prompt: str) -> str:
            async def _run():
                if hasattr(client, "initialize"):
                    try:
                        await client.initialize()
                    except Exception:
                        pass
                return await client.generate_reply(
                    prompt, {"reply_lang": "zh", "request_id": "eval"},
                    _skip_quality_check=True,
                ) or ""
            return asyncio.run(_run())

        return _gen
    except Exception as ex:
        logger.warning("LLM generate_fn 构造失败，跳过 LLM 对比: %s", ex)
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="意图识别评测")
    ap.add_argument("--dataset", default="", help="标注集路径(YAML/JSONL)；空=内置种子")
    ap.add_argument("--threshold", type=float, default=0.85, help="PASS 阈值（默认 0.85）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--compare", action="store_true", help="rule vs LLM 对比")
    ap.add_argument("--faq", action="store_true", help="FAQ 自动解决率评测")
    ap.add_argument("--kb-db", default="", help="KB sqlite 路径(--faq 用)")
    ap.add_argument("--faq-pass", type=float, default=0.50, help="FAQ 解决率 PASS 阈值")
    ap.add_argument("--score-threshold", type=float, default=1.0,
                    help="KB 命中分数阈值(--faq 判定解决)")
    args = ap.parse_args(argv)

    if args.faq:
        samples = load_faq_samples(args.dataset or None)
        resolver = _try_build_kb_resolver(args.kb_db, args.score_threshold)
        if resolver is None:
            print("[note] FAQ 评测需 KB：用 --kb-db 指定 sqlite，或确保 "
                  "config/knowledge_base.db 存在。当前 KB 不可用，跳过。")
            return 0
        report = evaluate_faq(resolver, samples, threshold=args.faq_pass)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_faq_report(report))
        return 0 if report["passed"] else 1

    samples = load_intent_samples(args.dataset or None)

    if args.compare:
        preds = {"rule": rule_intent_predictor()}
        llm_fn = _try_build_llm_generate_fn()
        if llm_fn is not None:
            preds["llm"] = llm_intent_predictor(llm_fn)
        else:
            print("[note] LLM 预测器未启用（需 EVAL_LLM=1 且配好 ai）；仅展示规则版。")
        results = compare_predictors(preds, samples, threshold=args.threshold)
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            print(format_compare(results))
        return 0 if all(r["passed"] for r in results.values()) else 1

    report = evaluate_intent(rule_intent_predictor(), samples,
                             threshold=args.threshold)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
