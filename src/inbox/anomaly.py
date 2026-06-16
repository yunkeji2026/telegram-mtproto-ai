"""
S2 — 异常检测预警引擎

功能：
  - 基于历史数据建立滑动基线（7天），检测当前指标是否偏离正常范围
  - 比统计 sigma 更实用的方法：滑动窗口均值 + 绝对偏差中位数(MAD)替代标准差
    （MAD 对少量异常值更鲁棒，适合 CSAT 这类偏态分布数据）
  - 三档灵敏度：cautious(1.5) / standard(2.0) / aggressive(2.5) sigma
  - 支持 4 大指标：csat_avg / l3l4_rate / autosend_rate / resolve_time_avg
  - 检测结果通过 EventBus 发布 anomaly_alert，复用 WebhookNotifier 推送

数学细节：
  MAD = median(|xi - median(X)|)
  score = |current - median(X)| / (1.4826 * MAD)   ← 1.4826 使 MAD ~ sigma
  异常条件：score > sensitivity AND 偏差方向符合预警方向

配置（config.yaml）：
  report:
    anomaly_detection:
      enabled: true
      sensitivity: 2.0          # sigma 阈值（1.5/2.0/2.5）
      baseline_days: 7          # 基线窗口天数
      metrics:                  # 监控哪些指标
        - csat_avg
        - l3l4_rate
        - autosend_rate
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 指标元数据（用于告警文案）──────────────────────────────────
_METRIC_META: Dict[str, Dict[str, Any]] = {
    "csat_avg": {
        "label":     "平均 CSAT",
        "direction": "down",    # 下降是异常
        "format":    lambda v: f"{v:.2f}⭐",
        "good_range": (4.0, 5.0),
    },
    "l3l4_rate": {
        "label":     "高风险草稿率",
        "direction": "up",      # 上升是异常
        "format":    lambda v: f"{v:.1f}%",
        "good_range": (0, 20),
    },
    "autosend_rate": {
        "label":     "自动发送率",
        "direction": "both",    # 大幅波动都是异常（可能系统问题）
        "format":    lambda v: f"{v:.1f}%",
        "good_range": (0, 100),
    },
    "resolve_time_avg": {
        "label":     "平均处置时长",
        "direction": "up",      # 变长是异常（响应慢）
        "format":    lambda v: f"{v:.0f}s",
        "good_range": (0, 3600),
    },
}


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    return (s[n // 2] + s[(n - 1) // 2]) / 2


def _mad(values: List[float]) -> float:
    """绝对偏差中位数（MAD）。"""
    if len(values) < 2:
        return 0.0
    med = _median(values)
    devs = [abs(v - med) for v in values]
    return _median(devs)


class AnomalyResult:
    def __init__(
        self,
        metric: str,
        current: float,
        baseline_median: float,
        baseline_mad: float,
        score: float,
        is_anomaly: bool,
        direction: str,
        sensitivity: float,
    ) -> None:
        self.metric = metric
        self.current = current
        self.baseline_median = baseline_median
        self.baseline_mad = baseline_mad
        self.score = round(score, 2)
        self.is_anomaly = is_anomaly
        self.direction = direction
        self.sensitivity = sensitivity

    def to_dict(self) -> Dict[str, Any]:
        meta = _METRIC_META.get(self.metric, {})
        fmt = meta.get("format", lambda v: f"{v:.2f}")
        return {
            "metric":           self.metric,
            "label":            meta.get("label", self.metric),
            "current":          self.current,
            "current_fmt":      fmt(self.current),
            "baseline_median":  self.baseline_median,
            "baseline_fmt":     fmt(self.baseline_median),
            "deviation_score":  self.score,
            "is_anomaly":       self.is_anomaly,
            "direction":        self.direction,
            "sensitivity":      self.sensitivity,
        }

    def __repr__(self) -> str:
        return (
            f"<AnomalyResult metric={self.metric} current={self.current:.2f} "
            f"median={self.baseline_median:.2f} score={self.score:.2f} "
            f"anomaly={self.is_anomaly}>"
        )


class AnomalyDetector:
    """S2：基于滑动基线的指标异常检测器。

    使用 MAD（绝对偏差中位数）替代标准差，对异常值鲁棒，适合小样本数据。
    """

    def __init__(
        self,
        store: Any,  # InboxStore
        cfg: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._store = store
        self._cfg = cfg or {}

    def _anomaly_cfg(self) -> Dict[str, Any]:
        return (self._cfg.get("report") or {}).get("anomaly_detection") or {}

    def is_enabled(self) -> bool:
        return bool(self._anomaly_cfg().get("enabled", False))

    def _sensitivity(self) -> float:
        return float(self._anomaly_cfg().get("sensitivity", 2.0))

    def _baseline_days(self) -> int:
        return int(self._anomaly_cfg().get("baseline_days", 7))

    def _monitored_metrics(self) -> List[str]:
        default = ["csat_avg", "l3l4_rate"]
        return list(self._anomaly_cfg().get("metrics") or default)

    def _get_historical_values(self, metric: str, window_days: int) -> List[float]:
        """从 InboxStore 获取过去 window_days 天的每日指标值（最多取 window_days 个点）。"""
        now = time.time()
        values = []

        if metric == "csat_avg":
            try:
                trend = self._store.get_csat_trend(
                    days=window_days, bucket="day"
                )
                values = [float(r["avg_csat"]) for r in (trend or []) if r.get("avg_csat") is not None]
            except Exception:
                pass

        elif metric == "l3l4_rate":
            try:
                trend = self._store.get_draft_level_trend(
                    days=window_days, bucket="day"
                )
                values = [float(r["l3l4_pct"]) for r in (trend or []) if r.get("l3l4_pct") is not None]
            except Exception:
                pass

        elif metric == "autosend_rate":
            try:
                # 从 draft_audit_log 统计近 N 天每日 autosend 占比
                since = now - window_days * 86400
                with self._store._lock:
                    rows = self._store._conn.execute(
                        """SELECT date(datetime(ts,'unixepoch')) as d,
                                  COUNT(*) as total,
                                  SUM(CASE WHEN action='autosend' THEN 1 ELSE 0 END) as autosent
                           FROM draft_audit_log WHERE ts >= ?
                           GROUP BY d ORDER BY d""",
                        (since,),
                    ).fetchall()
                for row in rows:
                    total = int(row[1] or 0)
                    autosent = int(row[2] or 0)
                    if total > 0:
                        values.append(autosent / total * 100)
            except Exception:
                pass

        elif metric == "resolve_time_avg":
            try:
                since = now - window_days * 86400
                with self._store._lock:
                    rows = self._store._conn.execute(
                        """SELECT date(datetime(decided_at,'unixepoch')) as d,
                                  AVG(decided_at - created_at) as avg_time
                           FROM reply_drafts
                           WHERE decided_at > 0 AND decided_at >= ?
                           GROUP BY d ORDER BY d""",
                        (since,),
                    ).fetchall()
                for row in rows:
                    if row[1] is not None:
                        values.append(float(row[1]))
            except Exception:
                pass

        return values

    def detect_one(
        self,
        metric: str,
        current_value: float,
        *,
        sensitivity: Optional[float] = None,
        window_days: Optional[int] = None,
    ) -> AnomalyResult:
        """检测单个指标是否异常。"""
        sigma = sensitivity if sensitivity is not None else self._sensitivity()
        days = window_days if window_days is not None else self._baseline_days()

        history = self._get_historical_values(metric, days)
        # 去掉最后一个点（可能是今天的，避免自我比较）
        if len(history) > 1:
            history = history[:-1]

        if len(history) < 3:
            # 历史数据不足，不触发告警
            return AnomalyResult(
                metric=metric, current=current_value,
                baseline_median=current_value, baseline_mad=0.0,
                score=0.0, is_anomaly=False, direction="none",
                sensitivity=sigma,
            )

        med = _median(history)
        mad_val = _mad(history)

        if mad_val < 1e-6:
            # 历史数据几乎无波动（所有天相同），用均值 ± 10% 作为触发阈值
            deviation = abs(current_value - med)
            threshold = max(med * 0.1, 0.01)
            score = deviation / threshold if threshold > 0 else 0.0
        else:
            score = abs(current_value - med) / (1.4826 * mad_val)

        # 判断偏差方向
        meta = _METRIC_META.get(metric, {})
        alert_dir = meta.get("direction", "both")
        if alert_dir == "down":
            directional_anomaly = current_value < med and score > sigma
        elif alert_dir == "up":
            directional_anomaly = current_value > med and score > sigma
        else:
            directional_anomaly = score > sigma

        direction = "down" if current_value < med else ("up" if current_value > med else "flat")

        return AnomalyResult(
            metric=metric,
            current=current_value,
            baseline_median=round(med, 3),
            baseline_mad=round(mad_val, 3),
            score=score,
            is_anomaly=directional_anomaly,
            direction=direction,
            sensitivity=sigma,
        )

    def run_full_check(
        self,
        current_metrics: Optional[Dict[str, float]] = None,
    ) -> List[AnomalyResult]:
        """对所有配置的指标运行检测，返回异常结果列表。

        current_metrics：可从外部传入当前指标值（由 ScheduledReporter 提供）；
        为 None 时自动从 store 查最新数据。
        """
        if not self.is_enabled():
            return []

        results = []
        for metric in self._monitored_metrics():
            try:
                current = self._get_current_value(metric, current_metrics)
                if current is None:
                    continue
                r = self.detect_one(metric, current)
                results.append(r)
                if r.is_anomaly:
                    logger.warning(
                        "S2 Anomaly detected: metric=%s current=%.3f median=%.3f score=%.2f",
                        metric, current, r.baseline_median, r.score,
                    )
            except Exception:
                logger.debug("S2 AnomalyDetector.detect_one 失败: %s", metric, exc_info=True)

        return results

    def _get_current_value(
        self,
        metric: str,
        current_metrics: Optional[Dict[str, float]],
    ) -> Optional[float]:
        """获取当前指标值（优先用传入的 current_metrics）。"""
        if current_metrics and metric in current_metrics:
            return float(current_metrics[metric])

        now = time.time()
        since_today = now - 86400

        try:
            if metric == "csat_avg":
                with self._store._lock:
                    row = self._store._conn.execute(
                        "SELECT AVG(csat_score) FROM conversation_meta WHERE csat_score>=0 AND updated_at>=?",
                        (since_today,),
                    ).fetchone()
                return float(row[0]) if row and row[0] is not None else None

            elif metric == "l3l4_rate":
                with self._store._lock:
                    total = self._store._conn.execute(
                        "SELECT COUNT(*) FROM reply_drafts WHERE created_at>=?", (since_today,)
                    ).fetchone()[0]
                    high = self._store._conn.execute(
                        "SELECT COUNT(*) FROM reply_drafts WHERE autopilot_level IN ('L3','L4','review','manual') AND created_at>=?",
                        (since_today,)
                    ).fetchone()[0]
                return float(high) / float(total) * 100 if total > 0 else None

            elif metric == "autosend_rate":
                with self._store._lock:
                    total = self._store._conn.execute(
                        "SELECT COUNT(*) FROM draft_audit_log WHERE ts>=?", (since_today,)
                    ).fetchone()[0]
                    auto = self._store._conn.execute(
                        "SELECT COUNT(*) FROM draft_audit_log WHERE action='autosend' AND ts>=?", (since_today,)
                    ).fetchone()[0]
                return float(auto) / float(total) * 100 if total > 0 else None

        except Exception:
            return None

        return None


def build_anomaly_alert_payload(
    results: List[AnomalyResult],
    *,
    detector_cfg: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """将检测结果打包为 anomaly_alert 事件 payload（供 EventBus 发布）。

    若无异常则返回 None。
    """
    anomalies = [r.to_dict() for r in results if r.is_anomaly]
    if not anomalies:
        return None

    return {
        "anomaly_count": len(anomalies),
        "anomalies":     anomalies,
        "all_metrics":   [r.to_dict() for r in results],
        "ts":            time.time(),
        "sensitivity":   (detector_cfg or {}).get("sensitivity", 2.0),
    }
