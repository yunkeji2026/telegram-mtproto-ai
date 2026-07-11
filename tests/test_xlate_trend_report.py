"""翻译趋势周报渲染：纯函数单测（分组/弱语对排序/坏行容错）。"""

import json

from scripts.xlate_trend_report import (
    group_series,
    load_rows,
    render_report,
    worst_pairs,
)


def _row(ts, engine="ollama_mt", back=None, dataset="a.yaml", **kw):
    d = {
        "ts": ts, "engine": engine, "back_engine": back or engine,
        "dataset": f"config/eval/{dataset}", "total": 44,
        "pass_rate": 0.86, "mean_score": 0.71, "mean_semantic": 0.88,
    }
    d.update(kw)
    return d


def test_group_series_splits_by_dataset_engine_back():
    rows = [
        _row("2026-07-01"), _row("2026-07-08"),
        _row("2026-07-08", back="ai"),
        _row("2026-07-08", dataset="b.yaml"),
    ]
    groups = group_series(rows, last=8)
    assert set(groups) == {
        ("a.yaml", "ollama_mt", "ollama_mt"),
        ("a.yaml", "ollama_mt", "ai"),
        ("b.yaml", "ollama_mt", "ollama_mt"),
    }
    assert len(groups[("a.yaml", "ollama_mt", "ollama_mt")]) == 2


def test_group_series_keeps_only_last_n():
    rows = [_row(f"2026-07-{i:02d}") for i in range(1, 11)]
    series = group_series(rows, last=3)[("a.yaml", "ollama_mt", "ollama_mt")]
    assert [r["ts"] for r in series] == ["2026-07-08", "2026-07-09", "2026-07-10"]


def test_worst_pairs_sorted_by_sem_ascending():
    row = _row("2026-07-08", by_pair={
        "zh->en": {"n": 3, "passed": 3, "char_mean": 0.8, "sem_mean": 0.95},
        "zh->hi": {"n": 3, "passed": 2, "char_mean": 0.7, "sem_mean": 0.80},
        "zh->ar": {"n": 1, "passed": 1, "char_mean": 0.6, "sem_mean": 0.85},
        "en->zh": {"n": 2, "passed": 2, "char_mean": 0.75, "sem_mean": None},
    })
    wp = worst_pairs(row, k=3)
    # en->zh 无 sem → 按 char 0.75 排序;顺序: en->zh(0.75) < zh->hi(0.80) < zh->ar(0.85)
    assert [w["pair"] for w in wp] == ["en->zh", "zh->hi", "zh->ar"]


def test_worst_pairs_absent_returns_empty():
    assert worst_pairs(_row("2026-07-08")) == []
    assert worst_pairs({"by_pair": "garbage"}) == []


def test_render_report_marks_small_samples_and_back_engine():
    rows = [
        _row("2026-07-01"),
        _row("2026-07-08", back="ai", by_pair={
            "zh->ar": {"n": 1, "passed": 0, "char_mean": 0.4, "sem_mean": 0.5},
        }),
    ]
    txt = render_report(rows, last=8, worst=5)
    assert "back=ai" in txt and "(self-back)" in txt
    assert "zh->ar" in txt and "n<2" in txt
    assert "0.88" in txt  # mean_semantic 渲染


def test_load_rows_skips_broken_lines(tmp_path):
    p = tmp_path / "trend.jsonl"
    p.write_text(
        json.dumps(_row("2026-07-01")) + "\n"
        + "{broken json\n"
        + json.dumps(_row("2026-07-08")) + "\n",
        encoding="utf-8",
    )
    rows = load_rows(str(p))
    assert [r["ts"] for r in rows] == ["2026-07-01", "2026-07-08"]
    assert load_rows(str(tmp_path / "missing.jsonl")) == []


def test_render_report_empty():
    assert "empty" in render_report([])
