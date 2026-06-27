"""scripts.suggest_draft_thresholds 推荐逻辑单测（纯函数，无网络）。

此前推荐数学内联在 suggest() 里、零测试覆盖。重构为纯函数后在此固化行为：
  - 样本不足 → insufficient_samples，不硬给数字
  - 足量样本 → 按策略给推荐 + changed 标记
  - p95 无延迟样本 → 回退当前/默认，不归零
  - observed_from_pipeline 优先 rates、缺键回退 total/generated（跨后端版本兼容）
"""

from __future__ import annotations

from scripts.suggest_draft_thresholds import (
    observed_from_pipeline,
    recommend_quality_thresholds,
    _QA_DEFAULTS,
)


class TestObservedFromPipeline:
    def test_reads_rates_vs_generated(self):
        dp = {
            "total": {"generated": 100},
            "rates_vs_generated": {"memory_hit": 0.6, "fast_path": 0.8, "empty": 0.05},
            "latency": {"count": 100, "p95_ms": 4200},
        }
        obs = observed_from_pipeline(dp)
        assert obs["generated"] == 100
        assert obs["memory_hit"] == 0.6
        assert obs["fast_path"] == 0.8
        assert obs["p95_ms"] == 4200

    def test_falls_back_to_total_when_rates_missing(self):
        """旧后端 rates 不含 fast_path → 用 total/generated 现算。"""
        dp = {
            "total": {"generated": 50, "fast_path": 30, "memory_hit": 20},
            "rates_vs_generated": {},  # 旧版本缺
            "latency": {},
        }
        obs = observed_from_pipeline(dp)
        assert obs["fast_path"] == 0.6
        assert obs["memory_hit"] == 0.4
        assert obs["p95_ms"] == 0

    def test_zero_generated_safe(self):
        obs = observed_from_pipeline({"total": {}, "rates_vs_generated": {}, "latency": {}})
        assert obs["generated"] == 0
        assert obs["fast_path"] == 0.0

    def test_window_rates_aligned_with_watchdog_basis(self):
        """窗口口径 = window[name]/window.generated（与 watchdog 触发口径一致）。"""
        dp = {
            "total": {"generated": 1000},
            "rates_vs_generated": {"memory_hit": 0.9, "fast_path": 0.9},
            "latency": {},
            "window": {"generated": 40, "memory_hit": 10, "fast_path": 36},
            "window_sec": 3600,
        }
        obs = observed_from_pipeline(dp)
        assert obs["window_generated"] == 40
        assert obs["window_sec"] == 3600
        assert obs["window_memory_hit"] == 0.25   # 10/40，明显有别于稳态 0.9
        assert obs["window_fast_path"] == 0.9      # 36/40
        # 稳态口径不受窗口影响
        assert obs["memory_hit"] == 0.9


class TestRecommendQualityThresholds:
    def test_insufficient_samples(self):
        obs = {"generated": 10, "memory_hit": 0.9, "fast_path": 0.5, "p95_ms": 1000}
        out = recommend_quality_thresholds(obs, {}, min_samples=50)
        assert out["status"] == "insufficient_samples"
        assert out["recommendations"] == {}
        assert out["generated"] == 10

    def test_ok_path_values(self):
        obs = {"generated": 200, "memory_hit": 0.80, "fast_path": 0.70,
               "p95_ms": 4000, "empty": 0.0}
        out = recommend_quality_thresholds(obs, {}, min_samples=50)
        assert out["status"] == "ok"
        rec = out["recommendations"]
        assert rec["memory_hit_min"]["recommended"] == 0.56     # 0.80*0.7
        assert rec["memory_hit_severe"]["recommended"] == 0.32  # 0.80*0.4
        assert rec["p95_ms_max"]["recommended"] == 6000         # 4000*1.5
        assert rec["p95_ms_severe"]["recommended"] == 10000     # 4000*2.5
        assert rec["fast_path_ratio_max"]["recommended"] == 0.80  # 0.70+0.10

    def test_p95_fallback_when_no_latency(self):
        """无延迟样本（p95=0）→ p95 阈值沿用当前配置/默认，不归零。"""
        obs = {"generated": 100, "memory_hit": 0.5, "fast_path": 0.5, "p95_ms": 0}
        current = {"p95_ms_max": 9000}
        out = recommend_quality_thresholds(obs, current, min_samples=50)
        rec = out["recommendations"]
        assert rec["p95_ms_max"]["recommended"] == 9000
        # current 缺 severe → 回退默认
        assert rec["p95_ms_severe"]["recommended"] == _QA_DEFAULTS["p95_ms_severe"]

    def test_changed_flag_reflects_diff(self):
        obs = {"generated": 100, "memory_hit": 0.50, "fast_path": 0.50, "p95_ms": 0}
        # 当前 memory_hit_min 恰等于推荐 0.35 → changed False
        current = {"memory_hit_min": 0.35}
        out = recommend_quality_thresholds(obs, current, min_samples=50)
        assert out["recommendations"]["memory_hit_min"]["changed"] is False

    def test_lower_bounds_enforced(self):
        """命中率极低时，推荐不低于下限（0.20 / 0.10）。"""
        obs = {"generated": 100, "memory_hit": 0.05, "fast_path": 0.0, "p95_ms": 0}
        out = recommend_quality_thresholds(obs, {}, min_samples=50)
        rec = out["recommendations"]
        assert rec["memory_hit_min"]["recommended"] == 0.20
        assert rec["memory_hit_severe"]["recommended"] == 0.10
