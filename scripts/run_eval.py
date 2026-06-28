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

from src.eval.dataset import (
    load_faq_samples, load_intent_samples, load_translation_samples,
)
from src.eval.faq_eval import (
    build_kb_resolver, evaluate_faq, format_faq_report,
)
from src.eval.translation_eval import (
    build_deterministic_evaluator, evaluate_translation_quality,
    format_translation_report,
)
from src.eval.memory_eval import (
    build_real_embed_fn, compare_recall, evaluate_semantic_dedup,
    format_dedup_report, format_recall_report,
)
from src.eval.embedding_providers import describe_availability
from src.eval.memory_extract_eval import (
    build_llm_extract_fn, evaluate_fact_extraction, format_extract_report,
    heuristic_extract_fn,
)
from src.eval.persona_eval import (
    evaluate_persona_consistency, format_persona_report,
)
from src.eval.emotion_eval import (
    evaluate_crisis_detection, evaluate_emotion_dimension,
    format_crisis_report, format_emotion_report,
)
from src.eval.crisis_response_eval import (
    evaluate_crisis_response, format_crisis_response_report,
)
from src.eval.translation_confidence_eval import (
    evaluate_confidence, format_confidence_report,
)
from src.eval.proactive_guard_eval import (
    evaluate_proactive_guard, format_proactive_guard_report,
)
from src.eval.crisis_resource_eval import (
    evaluate_resource_assurance, format_resource_report,
)
from src.eval.emotion_intensity_eval import (
    evaluate_intensity_grading, format_intensity_report,
)
from src.eval.dataset import (
    load_confidence_samples, load_crisis_resource_scenarios,
    load_crisis_response_scenarios, load_crisis_samples,
    load_emotion_samples, load_extract_samples, load_intensity_orders,
    load_memory_scenarios, load_persona_samples, load_proactive_guard_scenarios,
)
from src.eval.intent_eval import (
    compare_predictors, evaluate_intent, format_compare, format_report,
)
from src.eval.predictors import llm_intent_predictor, rule_intent_predictor

logger = logging.getLogger("run_eval")


def _try_build_kb_resolver(kb_db: str, score_threshold: float):
    """构造 KB 解决判定器；KB 不存在/构造失败返回 None（复用 faq_eval 定位逻辑）。"""
    resolver, _store = build_kb_resolver(kb_db, score_threshold=score_threshold)
    return resolver


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
    ap.add_argument("--translation", action="store_true",
                    help="翻译回译质量评测（需配 DeepL/Google 确定性引擎）")
    ap.add_argument("--xlate-sample-threshold", type=float, default=0.5,
                    help="单样本合格相似度阈(--translation)")
    ap.add_argument("--xlate-pass", type=float, default=0.6,
                    help="回译合格率 PASS 阈值(--translation)")
    ap.add_argument("--memory", action="store_true",
                    help="记忆召回对比评测（关键词 vs 向量，需 ai_client embed）")
    ap.add_argument("--mem-topk", type=int, default=3,
                    help="召回 top-k(--memory)；须 < 每场景事实数才鉴别向量增益")
    ap.add_argument("--semantic-dedup", action="store_true",
                    help="记忆语义去重评测（需真实嵌入：配 embedding 端点或 AITR_EMBED_LOCAL=1）")
    ap.add_argument("--dedup-threshold", type=float, default=0.7,
                    help="近义并簇余弦阈值(--semantic-dedup)")
    ap.add_argument("--memory-extract", action="store_true",
                    help="记忆抽取质量评测（启发式常驻；--extract-llm 切 LLM 抽取器）")
    ap.add_argument("--extract-llm", action="store_true",
                    help="用 ai_client.extract_memory_bullets 抽取(--memory-extract)")
    ap.add_argument("--extract-recall", type=float, default=0.8,
                    help="抽取召回率 PASS 阈值(--memory-extract)")
    ap.add_argument("--extract-max-fp", type=int, default=0,
                    help="允许的最大误抽数(--memory-extract)")
    ap.add_argument("--persona", action="store_true",
                    help="人设一致性评测（persona_guard 违规召回 + 误伤）")
    ap.add_argument("--emotion", action="store_true",
                    help="情绪维度准确率评测（analyze_emotion）")
    ap.add_argument("--emotion-acc", type=float, default=0.8,
                    help="情绪维度准确率 PASS 阈值(--emotion)")
    ap.add_argument("--crisis", action="store_true",
                    help="危机识别评测（detect_crisis 安全红线）")
    ap.add_argument("--crisis-response", action="store_true",
                    help="危机响应闭环评测（识别→处置端到端安全）")
    ap.add_argument("--xlate-confidence", action="store_true",
                    help="译文置信度评测（引擎智能切换 scorer）")
    ap.add_argument("--conf-threshold", type=float, default=0.5,
                    help="置信度二分阈值（--xlate-confidence 用）")
    ap.add_argument("--proactive-guard", action="store_true",
                    help="主动护栏闭环评测（危机/低落→主动触达抑制）")
    ap.add_argument("--emotion-intensity", action="store_true",
                    help="情绪强度分级评测（程度副词单调性）")
    ap.add_argument("--crisis-resource", action="store_true",
                    help="危机资源保障评测（severe 补热线不重复）")
    ap.add_argument("--crisis-overview", action="store_true",
                    help="危机安全总览（L/O 主动抑制 + J 响应闭环 + Q 资源保障 串联回归）")
    args = ap.parse_args(argv)

    if args.crisis_overview:
        from src.eval.crisis_safety_overview import (
            evaluate_crisis_safety_overview, format_crisis_safety_overview,
        )
        report = evaluate_crisis_safety_overview()
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_crisis_safety_overview(report))
        return 0 if report["passed"] else 1

    if args.crisis_resource:
        scenarios = load_crisis_resource_scenarios(
            args.dataset or "config/eval/crisis_resource_samples.yaml")
        report = evaluate_resource_assurance(scenarios)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_resource_report(report))
        return 0 if report["passed"] else 1

    if args.proactive_guard:
        scenarios = load_proactive_guard_scenarios(
            args.dataset or "config/eval/proactive_guard_samples.yaml")
        report = evaluate_proactive_guard(scenarios)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_proactive_guard_report(report))
        return 0 if report["passed"] else 1

    if args.emotion_intensity:
        orders = load_intensity_orders(
            args.dataset or "config/eval/emotion_intensity_samples.yaml")
        report = evaluate_intensity_grading(orders)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_intensity_report(report))
        return 0 if report["passed"] else 1

    if args.crisis_response:
        scenarios = load_crisis_response_scenarios(
            args.dataset or "config/eval/crisis_response_samples.yaml")
        report = evaluate_crisis_response(scenarios)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_crisis_response_report(report))
        return 0 if report["passed"] else 1

    if args.xlate_confidence:
        samples = load_confidence_samples(
            args.dataset or "config/eval/translation_confidence_samples.yaml")
        report = evaluate_confidence(samples, threshold=args.conf_threshold)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_confidence_report(report))
        return 0 if report["passed"] else 1

    if args.persona:
        samples = load_persona_samples(args.dataset or "config/eval/persona_samples.yaml")
        report = evaluate_persona_consistency(samples)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_persona_report(report))
        return 0 if report["passed"] else 1

    if args.emotion:
        samples = load_emotion_samples(args.dataset or "config/eval/emotion_samples.yaml")
        report = evaluate_emotion_dimension(samples, threshold=args.emotion_acc)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_emotion_report(report))
        return 0 if report["passed"] else 1

    if args.crisis:
        samples = load_crisis_samples(args.dataset or "config/eval/crisis_samples.yaml")
        report = evaluate_crisis_detection(samples)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_crisis_report(report))
        return 0 if report["passed"] else 1

    if args.memory_extract:
        samples = load_extract_samples(
            args.dataset or "config/eval/memory_extract_samples.yaml")
        if args.extract_llm:
            extract_fn = build_llm_extract_fn()
            if extract_fn is None:
                print("[note] LLM 抽取评测需 ai_client.extract_memory_bullets（配好 ai + key）。"
                      "当前不可用，跳过。")
                return 0
        else:
            extract_fn = heuristic_extract_fn
        report = evaluate_fact_extraction(
            extract_fn, samples,
            recall_target=args.extract_recall, max_false_positive=args.extract_max_fp)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_extract_report(report))
        return 0 if report["passed"] else 1

    if args.semantic_dedup:
        print(f"[info] {describe_availability()}")
        embed_fn = build_real_embed_fn()
        if embed_fn is None:
            print("[note] 语义去重评测需真实嵌入：配 ai.embedding_base_url/AITR_EMBED_BASE_URL，"
                  "或装 sentence-transformers 并设 AITR_EMBED_LOCAL=1。当前不可用，跳过。")
            return 0
        report = evaluate_semantic_dedup(embed_fn=embed_fn, threshold=args.dedup_threshold)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_dedup_report(report))
        return 0 if report["passed"] else 1

    if args.memory:
        print(f"[info] {describe_availability()}")
        embed_fn = build_real_embed_fn()
        if embed_fn is None:
            print("[note] 记忆召回评测需真实嵌入（配 embedding endpoint 或 AITR_EMBED_LOCAL=1）。"
                  "当前不可用，跳过。")
            return 0
        scenarios = load_memory_scenarios(args.dataset or "config/eval/memory_samples.yaml")
        cmp = compare_recall(scenarios, embed_fn=embed_fn, top_k=args.mem_topk)
        if args.json:
            print(json.dumps(cmp, ensure_ascii=False, indent=2))
        else:
            print(format_recall_report(cmp))
        # 对比模式不设硬门禁：delta<0（向量反而更差）才视为失败信号
        return 1 if cmp["delta_recall"] < 0 else 0

    if args.translation:
        import asyncio
        ev = build_deterministic_evaluator()
        if ev is None:
            print("[note] 翻译评测需确定性引擎：在 config.yaml 的 translation.engines.order "
                  "里加 deepl/google 并配 api_key。当前无可用确定性引擎，跳过。")
            return 0
        translate_fn, detect_fn = ev
        samples = load_translation_samples(args.dataset or "config/eval/translation_samples.yaml")
        report = asyncio.run(evaluate_translation_quality(
            translate_fn, samples, detect_fn=detect_fn,
            per_sample_threshold=args.xlate_sample_threshold, pass_target=args.xlate_pass))
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_translation_report(report))
        return 0 if report["passed"] else 1

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
