"""语音合成语言一致性评测（确定性，零网络/零 LLM）。

安全/体验不变量：克隆语音合成送给主机的 ``language`` 必须随**待合成文本的实际语种**，
而非固定配置默认——否则英文/他语回复被按中文音系发音（「中文声纹念英文」garble）。

复刻发声路径的真实决策 ``voice_clone_client.effective_clone_language``（autosend /
原生 voice_reply / 手动坐席三条链路共用的合成语言瓶颈），对每条样本断言
``effective_clone_language(text, default_lang) == expect_lang``。

指标：命中率（预测语言==期望）；``passed`` = 命中率 ≥ ``acc_target``（默认 1.0，
确定性不变量应全对）。纯函数常驻门禁。阈值可经 ``AITR_VOICE_LANG_ACC_TARGET`` 覆盖。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .dataset import VoiceLangSample, load_voice_lang_samples


def _default_acc_target() -> float:
    try:
        return float(os.environ.get("AITR_VOICE_LANG_ACC_TARGET", "1.0"))
    except (TypeError, ValueError):
        return 1.0


def evaluate_voice_language(
    samples: Optional[List[VoiceLangSample]] = None,
    *,
    acc_target: Optional[float] = None,
) -> Dict[str, Any]:
    """跑语音合成语言一致性评测；返回命中率 + 误判清单 + passed。

    passed = 命中率 ≥ acc_target（默认 1.0：确定性映射，任何一条不符即视为缺陷）。
    """
    from src.ai.voice_clone_client import effective_clone_language

    target = acc_target if acc_target is not None else _default_acc_target()
    rows = samples if samples is not None else load_voice_lang_samples()
    correct = 0
    errors: List[Dict[str, Any]] = []
    for s in rows:
        got = effective_clone_language(s.text, s.default_lang)
        if got == s.expect_lang:
            correct += 1
        else:
            errors.append({
                "text": s.text[:40], "default": s.default_lang,
                "expect": s.expect_lang, "got": got, "note": s.note})

    n = len(rows)
    accuracy = round(correct / n, 3) if n else 0.0
    passed = n > 0 and accuracy >= target
    return {
        "summary": {"total": n, "correct": correct, "accuracy": accuracy},
        "errors": errors,
        "acc_target": target,
        "passed": passed,
    }


def format_voice_language_report(report: Dict[str, Any]) -> str:
    m = report["summary"]
    lines = [
        "=== 语音合成语言一致性评测报告 ===",
        f"样本: {m['total']}  命中: {m['correct']}  命中率: {m['accuracy']:.0%}  "
        f"目标: ≥{report['acc_target']:.0%}  "
        f"{'[PASS]' if report['passed'] else '[FAIL]'}",
    ]
    if report["errors"]:
        lines.append(f"误判 {len(report['errors'])} 例（合成语言≠文本语种，会 garble）:")
        for e in report["errors"][:20]:
            lines.append(
                f"  - 「{e['text']}」default={e['default']} "
                f"期望={e['expect']} 实得={e['got']}  ({e['note']})")
    return "\n".join(lines)


__all__ = ["evaluate_voice_language", "format_voice_language_report"]
