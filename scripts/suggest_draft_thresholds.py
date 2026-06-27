"""草稿质量/记忆漂移告警阈值校准助手（运营工具）。

config/config.yaml 里 ``inbox.auto_draft.quality_alert`` / ``key_drift_alert`` 的阈值
初始为**经验值**。本脚本读取**运行中后端**的实时草稿指标（经 authed HTTP API，因指标
驻留在进程内存、无法离线读），按观测分布给出推荐阈值，便于用真实流量收敛参数。

指标驻留进程内存，且每次重启清零 → 须在后端**持续运行、累计足够样本**后再跑本脚本；
样本不足时如实提示，不硬给数字。

数据来源（按优先级）：
  1. ``/api/drafts/pipeline-metrics`` —— **只读聚合指标**，纯 API token 即可（推荐）。
  2. ``/api/drafts/autosend-status`` —— 旧端点，需**主管会话**；纯 token 会 403，自动回落。

用法::

    python -m scripts.suggest_draft_thresholds                       # 默认 127.0.0.1:18799
    python -m scripts.suggest_draft_thresholds --token <token>       # 显式令牌
    python -m scripts.suggest_draft_thresholds --json                # 机器可读（CI 用）
    AITR_WEB_TOKEN=xxx python -m scripts.suggest_draft_thresholds    # 经环境变量

令牌默认取 env ``AITR_WEB_TOKEN``（桌面壳默认 ``admin``）。
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from typing import Any, Dict, Optional

_MIN_SAMPLES = 50

# quality_alert 各阈值的出厂默认（与 health_watchdog._check_draft_quality 对齐）。
_QA_DEFAULTS: Dict[str, Any] = {
    "memory_hit_min": 0.30,
    "memory_hit_severe": 0.15,
    "p95_ms_max": 8000,
    "p95_ms_severe": 16000,
    "fast_path_ratio_max": 0.98,
}


def _get(url: str, token: str, timeout: float = 8.0) -> Optional[Dict[str, Any]]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)}


def _load_current_thresholds() -> Dict[str, Any]:
    try:
        import yaml
        with open("config/config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        ad = ((cfg.get("inbox") or {}).get("auto_draft") or {})
        return {"quality_alert": ad.get("quality_alert") or {},
                "key_drift_alert": ad.get("key_drift_alert") or {}}
    except Exception:
        return {"quality_alert": {}, "key_drift_alert": {}}


def observed_from_pipeline(dp: Dict[str, Any]) -> Dict[str, Any]:
    """从 ``draft_pipeline`` 快照抽取校准所需的观测量（纯函数，无 IO）。

    ``rates_vs_generated`` 已含 fast_path/empty（见 MetricsStore.get_inbox_draft_metrics）；
    若旧后端缺该键，回退用 total/generated 现算，保证跨版本可用。
    """
    total = dp.get("total") or {}
    rates = dp.get("rates_vs_generated") or {}
    latency = dp.get("latency") or {}
    gen = int(total.get("generated") or 0)

    def _rate(key: str) -> float:
        if key in rates:
            return float(rates.get(key) or 0.0)
        return round(int(total.get(key) or 0) / gen, 4) if gen > 0 else 0.0

    return {
        "generated": gen,
        "memory_hit": _rate("memory_hit"),
        "fast_path": _rate("fast_path"),
        "empty": _rate("empty"),
        "p95_ms": int(latency.get("p95_ms") or 0),
        "latency_count": int(latency.get("count") or 0),
    }


def recommend_quality_thresholds(
    observed: Dict[str, Any],
    current: Dict[str, Any],
    *,
    min_samples: int = _MIN_SAMPLES,
) -> Dict[str, Any]:
    """据观测分布给出 quality_alert 推荐阈值（**纯函数，无网络/无文件**，便于单测）。

    推荐策略：
      - memory_hit_min   ← 常态命中率 × 0.7（跌破报黄），下限 0.20
      - memory_hit_severe← 常态命中率 × 0.4（跌破报红），下限 0.10
      - p95_ms_max       ← 常态 p95 × 1.5；无延迟样本时沿用当前/默认
      - p95_ms_severe    ← 常态 p95 × 2.5；同上
      - fast_path_ratio_max ← 常态快路占比 + 0.10（上界，防风险分类过宽），上限 0.99

    返回 ``{status, generated, observed, recommendations:{k:{current,recommended,changed}}}``；
    样本不足时 ``status="insufficient_samples"``、recommendations 为空。
    """
    gen = int(observed.get("generated") or 0)
    if gen < int(min_samples):
        return {
            "status": "insufficient_samples",
            "generated": gen,
            "min_samples": int(min_samples),
            "recommendations": {},
        }

    mem = float(observed.get("memory_hit") or 0.0)
    fast = float(observed.get("fast_path") or 0.0)
    p95 = int(observed.get("p95_ms") or 0)

    def _cur(key: str) -> Any:
        return current.get(key, _QA_DEFAULTS[key])

    rec: Dict[str, Any] = {
        "memory_hit_min": round(max(0.20, mem * 0.7), 2),
        "memory_hit_severe": round(max(0.10, mem * 0.4), 2),
        "p95_ms_max": int(p95 * 1.5) if p95 else _cur("p95_ms_max"),
        "p95_ms_severe": int(p95 * 2.5) if p95 else _cur("p95_ms_severe"),
        "fast_path_ratio_max": round(min(0.99, fast + 0.10), 2),
    }
    recommendations = {
        k: {"current": _cur(k), "recommended": v, "changed": _cur(k) != v}
        for k, v in rec.items()
    }
    return {
        "status": "ok",
        "generated": gen,
        "observed": {"memory_hit": mem, "fast_path": fast, "p95_ms": p95,
                     "empty": float(observed.get("empty") or 0.0)},
        "recommendations": recommendations,
    }


def _fetch_pipeline(base: str, token: str) -> Dict[str, Any]:
    """优先打只读端点，403/失败回落主管会话端点；返回 ``{draft_pipeline, source}``。"""
    lite = _get(f"{base}/api/drafts/pipeline-metrics", token)
    if lite and lite.get("ok") and (lite.get("draft_pipeline") or {}):
        return {"draft_pipeline": lite["draft_pipeline"], "source": "pipeline-metrics"}
    snap = _get(f"{base}/api/drafts/autosend-status", token)
    dp = (((snap or {}).get("worker") or {}).get("draft_pipeline")) or {}
    if dp:
        return {"draft_pipeline": dp, "source": "autosend-status"}
    err = (lite or {}).get("_error") or (snap or {}).get("_error") or "unreachable"
    return {"draft_pipeline": {}, "source": "", "error": err}


def build_report(base_url: str, token: str) -> Dict[str, Any]:
    """组装完整校准报告（指标拉取 + 纯函数推荐 + key 健康），返回 dict（供 --json / 渲染共用）。"""
    base = base_url.rstrip("/")
    cur = _load_current_thresholds()
    fetched = _fetch_pipeline(base, token)
    dp = fetched["draft_pipeline"]

    report: Dict[str, Any] = {"source": fetched.get("source", "")}
    if not dp:
        report["quality"] = {"status": "unreachable", "error": fetched.get("error")}
    else:
        observed = observed_from_pipeline(dp)
        report["quality"] = recommend_quality_thresholds(observed, cur["quality_alert"])

    kh = _get(f"{base}/api/episodic-memory/key-health", token)
    if kh and kh.get("enabled"):
        report["key_health"] = {
            "bare_keys": int(kh.get("bare_keys") or 0),
            "canonical_keys": kh.get("canonical_keys"),
            "bare_facts": kh.get("bare_facts"),
        }
    elif kh and not kh.get("_error"):
        report["key_health"] = {"enabled": False}
    return report


def _print_report(report: Dict[str, Any]) -> None:
    q = report.get("quality") or {}
    status = q.get("status")
    src = report.get("source") or "?"
    if status == "unreachable":
        print("草稿指标不可达（pipeline-metrics 与 autosend-status 均未取到）。"
              f"\n  原因：{q.get('error')}\n  请确认后端在运行、token 正确、且已累计草稿样本。")
    elif status == "insufficient_samples":
        print(f"== 草稿质量阈值校准（来源 {src}）==")
        print(f"样本不足（generated={q.get('generated')} < {q.get('min_samples')}）："
              "让后端在真实流量下多跑一段再来，当前沿用经验值即可。")
    elif status == "ok":
        obs = q.get("observed") or {}
        print(f"== 草稿质量阈值校准（来源 {src}，generated={q.get('generated')}）==")
        print(f"观测：memory_hit={obs.get('memory_hit', 0):.0%}  "
              f"fast_path={obs.get('fast_path', 0):.0%}  "
              f"empty={obs.get('empty', 0):.0%}  p95={obs.get('p95_ms', 0)}ms")
        for k, info in (q.get("recommendations") or {}).items():
            flag = "  ← 建议调整" if info.get("changed") else ""
            print(f"  {k:<20} 现={info.get('current')}  推荐={info.get('recommended')}{flag}")

    kh = report.get("key_health")
    if kh and kh.get("enabled") is not False:
        bare = int(kh.get("bare_keys") or 0)
        print(f"\n== 记忆 key 健康 ==\n  裸 key={bare}  canonical={kh.get('canonical_keys')}  "
              f"失联事实={kh.get('bare_facts')}")
        if bare:
            print("  ⚠ 存在裸 key → 到运营总览一键并入，或 "
                  "python -m src.utils.episodic_key_migration --db config/bot.db "
                  "--platform telegram --apply")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="草稿质量/记忆漂移告警阈值校准助手")
    ap.add_argument("--base-url", default="http://127.0.0.1:18799")
    ap.add_argument("--token", default=os.environ.get("AITR_WEB_TOKEN", "admin"))
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON（CI 用）")
    args = ap.parse_args(argv)
    report = build_report(args.base_url, args.token)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
