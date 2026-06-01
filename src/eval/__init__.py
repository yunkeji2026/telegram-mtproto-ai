"""评测框架（eval harness）。

把蓝图验收里一直没量化的「意图识别准确率 ≥85% / FAQ 自动解决率 ≥50%」
变成可复现的数字：标注数据集 + 纯指标计算 + 预测器适配 + CLI 报告。

分层（均无副作用、可单测）：
- metrics.py     纯指标（多分类 accuracy/precision/recall/F1/混淆；二分类解决率）
- dataset.py     标注样本加载（YAML/JSONL）+ 内置种子
- intent_eval.py 用任意 predict_fn 跑意图评测 → 结构化报告
- predictors.py  predict_fn 适配（规则版意图基线，离线确定性，零 API）
"""

from .metrics import multiclass_metrics, resolve_rate
from .dataset import IntentSample, load_intent_samples
from .intent_eval import evaluate_intent, compare_predictors
from .predictors import (
    rule_intent_predictor,
    llm_intent_predictor,
    INTENT_LABELS,
    build_intent_classify_prompt,
    parse_intent_label,
)

__all__ = [
    "multiclass_metrics",
    "resolve_rate",
    "IntentSample",
    "load_intent_samples",
    "evaluate_intent",
    "compare_predictors",
    "rule_intent_predictor",
    "llm_intent_predictor",
    "INTENT_LABELS",
    "build_intent_classify_prompt",
    "parse_intent_label",
]
