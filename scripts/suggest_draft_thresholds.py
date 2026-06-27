"""草稿质量/记忆漂移告警阈值校准助手（运营工具）。

config/config.yaml 里 ``inbox.auto_draft.quality_alert`` / ``key_drift_alert`` 的阈值
初始为**经验值**。本脚本读取**运行中后端**的实时草稿指标（经 authed HTTP API，因指标
驻留在进程内存、无法离线读），按观测分布给出推荐阈值，便于用真实流量收敛参数。

指标驻留进程内存，且每次重启清零 → 须在后端**持续运行、累计足够样本**后再跑本脚本；
样本不足时如实提示，不硬给数字。

用法::

    python -m scripts.suggest_draft_thresholds                       # 默认 127.0.0.1:18799
    python -m scripts.suggest_draft_thresholds --token <token>       # 显式令牌
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


def _get(url: str, token: str, timeout: float = 8.0) -> Optional[Dict[str, Any]]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 拉取失败 {url}: {e}")
        return None


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


def _fmt(cur: Any, rec: Any) -> str:
    flag = "" if cur == rec else "  ← 建议调整"
    return f"现={cur}  推荐={rec}{flag}"


def suggest(base_url: str, token: str) -> int:
    base = base_url.rstrip("/")
    cur = _load_current_thresholds()
    qa_cur = cur["quality_alert"]

    snap = _get(f"{base}/api/drafts/autosend-status", token)
    dp = (((snap or {}).get("worker") or {}).get("draft_pipeline")) or {}
    total = dp.get("total") or {}
    rates = dp.get("rates_vs_generated") or {}
    latency = dp.get("latency") or {}
    gen = int(total.get("generated") or 0)

    print(f"== 草稿质量阈值校准（观测窗口 generated={gen}）==")
    if not snap or not dp:
        # autosend-status 是「主管会话」权限端点；纯 token 到不了 → 改从 dashboard 读。
        print("草稿指标不可达（该端点需主管会话权限，非 API token）。"
              "请在 workspace dashboard 的 draft_pipeline 面板查看 memory_hit / p95 / "
              "fast_path 分布，再据如下经验规则手调 config（inbox.auto_draft.quality_alert）：")
        print("  memory_hit_min ← 常态命中率×0.7   memory_hit_severe ← ×0.4")
        print("  p95_ms_max     ← 常态 p95×1.5      p95_ms_severe     ← ×2.5")
        print("  fast_path_ratio_max ← 常态快路占比 + 0.10")
    elif gen < _MIN_SAMPLES:
        print(f"样本不足（<{_MIN_SAMPLES}）：让后端在真实流量下多跑一段再来。"
              "当前不足以稳定推断分布，沿用经验值即可。")
    else:
        mem = float(rates.get("memory_hit") or 0.0)
        fast = float(rates.get("fast_path") or 0.0)
        p95 = int(latency.get("p95_ms") or 0)
        # 推荐：阈值设在"常态的安全下/上界"——命中率跌到常态 70% 报黄、40% 报红；
        # p95 超常态 1.5x 报黄、2.5x 报红；快路占比常态 +0.1 为上界（防过宽）。
        rec_mem_min = round(max(0.2, mem * 0.7), 2)
        rec_mem_sev = round(max(0.1, mem * 0.4), 2)
        rec_p95_max = int(p95 * 1.5) if p95 else qa_cur.get("p95_ms_max", 8000)
        rec_p95_sev = int(p95 * 2.5) if p95 else qa_cur.get("p95_ms_severe", 16000)
        rec_fp_max = round(min(0.99, fast + 0.10), 2)
        print(f"观测：memory_hit={mem:.0%}  fast_path={fast:.0%}  p95={p95}ms")
        print("  memory_hit_min     " + _fmt(qa_cur.get("memory_hit_min", 0.30), rec_mem_min))
        print("  memory_hit_severe  " + _fmt(qa_cur.get("memory_hit_severe", 0.15), rec_mem_sev))
        print("  p95_ms_max         " + _fmt(qa_cur.get("p95_ms_max", 8000), rec_p95_max))
        print("  p95_ms_severe      " + _fmt(qa_cur.get("p95_ms_severe", 16000), rec_p95_sev))
        print("  fast_path_ratio_max" + _fmt(qa_cur.get("fast_path_ratio_max", 0.98), rec_fp_max))

    # 记忆 key 健康（漂移）一并体检
    kh = _get(f"{base}/api/episodic-memory/key-health", token)
    if kh and kh.get("enabled"):
        bare = int(kh.get("bare_keys") or 0)
        print(f"\n== 记忆 key 健康 ==\n  裸 key={bare}  canonical={kh.get('canonical_keys')}  "
              f"失联事实={kh.get('bare_facts')}")
        if bare:
            print("  ⚠ 存在裸 key → 到运营总览一键并入，或 "
                  "python -m src.utils.episodic_key_migration --db config/bot.db "
                  "--platform telegram --apply")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="草稿质量/记忆漂移告警阈值校准助手")
    ap.add_argument("--base-url", default="http://127.0.0.1:18799")
    ap.add_argument("--token", default=os.environ.get("AITR_WEB_TOKEN", "admin"))
    args = ap.parse_args(argv)
    return suggest(args.base_url, args.token)


if __name__ == "__main__":
    raise SystemExit(main())
