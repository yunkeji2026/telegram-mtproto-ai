"""FAQ 自动解决率 CI 门禁（Tier2 #8：对外唯一可信「自助解决」硬指标）。

口径：FAQ 样本（``config/eval/faq_samples.yaml``）经 KB 检索，top 命中分 ≥ 阈值即「可自动
解决」；解决率 = 解决数 / 样本数，目标 ≥ 50%（蓝图）。

门禁策略（务实、不误伤）：
  - **无 KB sqlite** → skip（dev/CI 未备库，等价 run_eval 的「缺库优雅跳过」）。
  - **KB 夹生**（启用条目 < 下限，疑为占位/测试库）→ skip，避免空库把 CI 刷红。
  - **KB 真备货**（启用条目 ≥ 下限）→ 强制解决率 ≥ 目标，未达即 fail 并打印未解决清单。

可调环境变量（BM25 分未归一化，阈值需按各 KB 校准）：
  - ``AITR_FAQ_SCORE_THRESHOLD``（默认 1.0）KB top 命中分判定阈
  - ``AITR_FAQ_RESOLVE_TARGET``（默认 0.50）解决率 PASS 目标
  - ``AITR_FAQ_MIN_ENTRIES``（默认 10）判「真备货」的最少启用条目数
"""

from __future__ import annotations

import os

import pytest

from src.eval.dataset import load_faq_samples
from src.eval.faq_eval import (
    build_kb_resolver, evaluate_faq, format_faq_report, kb_enabled_count,
    locate_kb_db,
)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def test_faq_resolution_rate_gate():
    """真实 KB 备货时强制 FAQ 解决率 ≥ 目标；缺库/夹生库优雅跳过。"""
    score_threshold = _env_float("AITR_FAQ_SCORE_THRESHOLD", 1.0)
    target = _env_float("AITR_FAQ_RESOLVE_TARGET", 0.50)
    min_entries = _env_int("AITR_FAQ_MIN_ENTRIES", 10)

    resolver, store = build_kb_resolver(score_threshold=score_threshold)
    if resolver is None:
        pytest.skip("无 KB sqlite（config/knowledge_base.db 等）；FAQ 门禁跳过")

    enabled = kb_enabled_count(store)
    if enabled < min_entries:
        pytest.skip(
            f"KB 仅 {enabled} 条启用条目(<{min_entries})，疑为占位/测试库；FAQ 门禁跳过")

    samples = load_faq_samples("config/eval/faq_samples.yaml")
    report = evaluate_faq(resolver, samples, threshold=target)
    assert report["passed"], (
        "\nFAQ 自动解决率未达门禁——请补 KB 内容或校准 "
        "AITR_FAQ_SCORE_THRESHOLD：\n" + format_faq_report(report))


# ── 门禁基建确定性自测（与是否有真库无关，保证本文件有可执行覆盖）──────────────

def test_locate_kb_db_prefers_existing_param(tmp_path):
    db = tmp_path / "kb.db"
    db.write_text("x", encoding="utf-8")
    assert locate_kb_db(str(db)) == str(db)


def test_locate_kb_db_missing_param_falls_through(tmp_path, monkeypatch):
    # 入参不存在 + 无环境 + 无默认库 → None（用不存在的 cwd 隔离默认候选）
    monkeypatch.delenv("AITR_KB_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    assert locate_kb_db(str(tmp_path / "nope.db")) is None


def test_build_kb_resolver_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("AITR_KB_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    resolver, store = build_kb_resolver(str(tmp_path / "nope.db"))
    assert resolver is None and store is None


def test_kb_enabled_count_robust():
    class _Real:                                   # KnowledgeBaseStore 真实键名
        def stats(self):
            return {"enabled_entries": 12, "total_entries": 30}

    class _Alias:                                  # 兼容 enabled 别名
        def stats(self):
            return {"enabled": 7}

    class _Boom:
        def stats(self):
            raise RuntimeError("db locked")

    assert kb_enabled_count(_Real()) == 12
    assert kb_enabled_count(_Alias()) == 7
    assert kb_enabled_count(_Boom()) == 0          # 异常保守 0
    assert kb_enabled_count(object()) == 0         # 无 stats 方法
