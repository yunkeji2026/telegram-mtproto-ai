"""通道健康度评分 — 综合成功率/告警/配置完整性/更新频率"""

import re
import time
from typing import Dict, List, Optional

from src.utils.channel_status_format import is_channel_disabled


def compute_health_scores(channels: dict, event_tracker=None) -> List[Dict]:
    results = []
    alert_counts: Dict[str, int] = {}
    if event_tracker:
        try:
            events = event_tracker.command_stats(hours=24)
            for e in events:
                if e["type"] == "alert_success_rate":
                    alert_counts["_global"] = alert_counts.get("_global", 0) + e["count"]
        except Exception:
            pass

    for key, cfg in channels.items():
        if not isinstance(cfg, dict):
            continue
        score = 0.0
        details = {}

        from src.utils.channel_status_format import _get_sub, _sub_status
        payin = cfg.get("payin") if isinstance(cfg.get("payin"), dict) else {}
        payout = cfg.get("payout") if isinstance(cfg.get("payout"), dict) else {}
        rate_str = _get_sub(cfg, "payin", "fee_rate") or cfg.get("fee_rate", "")
        pi_st = _sub_status(cfg, "payin")
        po_st = _sub_status(cfg, "payout")
        status = pi_st if pi_st == po_st else f"{pi_st}/{po_st}"
        last_updated = cfg.get("last_updated", "")
        display = cfg.get("display_name", key)

        if is_channel_disabled(cfg):
            results.append({
                "key": key,
                "display_name": display,
                "score": -1,
                "grade": "disabled",
                "status": status,
                "fee_rate": rate_str,
                "details": {"note": "通道已禁用，不参与健康度评分"},
            })
            continue

        config_completeness = 0
        has_pt = bool(
            (payin and payin.get("processing_time"))
            or (payout and payout.get("processing_time"))
            or cfg.get("processing_time")
        )
        required_checks = [bool(cfg.get("display_name")), has_pt]
        present = sum(required_checks)
        if payin:
            present += sum(1 for f in ["fee_rate", "status"] if payin.get(f))
        else:
            present += sum(1 for f in ["fee_rate", "status"] if cfg.get(f))
        config_completeness = present / 4 * 100
        details["config_completeness"] = round(config_completeness)

        _normal = {"active", "正常", "启用"}
        _maint  = {"maintenance", "维护中", "波动"}
        status_score = max(
            100 if pi_st in _normal else (50 if pi_st in _maint else 0),
            100 if po_st in _normal else (50 if po_st in _maint else 0),
        )
        details["status_score"] = status_score

        alert_penalty = min(alert_counts.get("_global", 0) * 10, 50)
        details["alert_penalty"] = alert_penalty

        freshness = 100
        if last_updated:
            try:
                parts = last_updated.split("-")
                if len(parts) == 3:
                    days_ago = (time.time() - time.mktime(time.strptime(last_updated, "%Y-%m-%d"))) / 86400
                    if days_ago > 30:
                        freshness = max(0, 100 - (days_ago - 30) * 2)
            except Exception:
                freshness = 50
        else:
            freshness = 30
        details["freshness"] = round(freshness)

        score = (
            status_score * 0.50 +
            config_completeness * 0.15 +
            freshness * 0.15 +
            max(0, 100 - alert_penalty) * 0.20
        )
        score = round(min(100, max(0, score)))

        if score >= 80:
            grade = "healthy"
        elif score >= 50:
            grade = "warning"
        else:
            grade = "critical"

        results.append({
            "key": key,
            "display_name": display,
            "score": score,
            "grade": grade,
            "status": status,
            "fee_rate": rate_str,
            "details": details,
        })

    results.sort(key=lambda x: x["score"])
    return results
