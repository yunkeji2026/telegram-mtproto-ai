"""RelationshipStager — map intimacy_score (0-100) → PersonaManager profile_id.

Connects IntimacyEngine's continuous score to PersonaManager's profile store so
the AI persona gets progressively warmer as the relationship deepens.

Stage bands mirror companion_relationship.INTIMACY_BAND_DEFAULTS:
  initial   0-25
  warming  25-55
  intimate 55-80
  steady   80+

Config keys (in messenger_rpa.* or per-account overlay):
  stage_persona_ids:          {initial: pid, warming: pid, intimate: pid, steady: pid}
  stage_persona_bands:        {to_warming: 25, to_intimate: 55, to_steady: 80}
  stage_persona_enabled:      true   (auto-true when stage_persona_ids is non-empty)
  stage_persona_fallback_up:  false  (if true: score=15 with no "initial" map tries "warming" etc.)

Design rules:
  1. Operator's explicit PM chat-level binding ALWAYS wins — caller must check
     pm.has_chat_binding() before calling resolve() and skip if True.
  2. resolve() is pure: no DB calls, no side effects.
  3. Missing stage in map → tries adjacent lower stage if fallback_up=False,
     adjacent higher stage if fallback_up=True (configurable).
  4. Gracefully degrades to None on any error so callers can ignore it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Stage ordering (ascending intimacy)
_STAGE_ORDER: List[str] = ["initial", "warming", "intimate", "steady"]

# Mirrors companion_relationship.INTIMACY_BAND_DEFAULTS — kept in sync manually.
# Import at runtime to avoid hard import cycle; fallback to these defaults.
_FALLBACK_BANDS: Dict[str, float] = {
    "to_warming": 25.0,
    "to_intimate": 55.0,
    "to_steady": 80.0,
}


def _load_bands() -> Dict[str, float]:
    """Load thresholds from companion_relationship (authoritative source)."""
    try:
        from src.utils.companion_relationship import INTIMACY_BAND_DEFAULTS
        return dict(INTIMACY_BAND_DEFAULTS)
    except Exception:
        return dict(_FALLBACK_BANDS)


class RelationshipStager:
    """Resolves a PersonaManager profile_id from an intimacy score.

    Usage::

        stager = RelationshipStager.from_config(merged_account_cfg)
        pid = stager.resolve(ctx.get("intimacy_score"))
        if pid and pm.get_persona_by_id(pid):
            ctx["account_persona_id"] = pid
            ctx["relationship_stage"] = stager.score_to_stage(ctx["intimacy_score"])
    """

    def __init__(
        self,
        stage_persona_ids: Dict[str, str],
        *,
        bands: Optional[Dict[str, float]] = None,
        enabled: bool = True,
        fallback_up: bool = False,
    ) -> None:
        self._map: Dict[str, str] = {
            k: str(v).strip()
            for k, v in (stage_persona_ids or {}).items()
            if str(v).strip()
        }
        _b = _load_bands()
        if isinstance(bands, dict):
            for k in ("to_warming", "to_intimate", "to_steady"):
                try:
                    if k in bands:
                        _b[k] = float(bands[k])
                except (TypeError, ValueError):
                    pass
        self._bands = _b
        self.enabled = bool(enabled)
        self._fallback_up = bool(fallback_up)

    # ── factory ──────────────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "RelationshipStager":
        """Build from merged account config (or global messenger_rpa cfg).

        Reads keys:
          stage_persona_ids, stage_persona_bands, stage_persona_enabled,
          stage_persona_fallback_up
        """
        if not isinstance(cfg, dict):
            return cls({}, enabled=False)
        raw = cfg.get("stage_persona_ids") or {}
        if not isinstance(raw, dict):
            raw = {}
        bands = cfg.get("stage_persona_bands") or {}
        if not isinstance(bands, dict):
            bands = {}
        # Auto-enable when stage_persona_ids is non-empty unless explicitly disabled
        auto = bool(raw)
        enabled = bool(cfg.get("stage_persona_enabled", auto))
        fallback_up = bool(cfg.get("stage_persona_fallback_up", False))
        return cls(raw, bands=bands, enabled=enabled, fallback_up=fallback_up)

    # ── public API ────────────────────────────────────────────────────────────

    def score_to_stage(self, score: float) -> str:
        """Map 0-100 intimacy_score → stage name.  Always returns a valid stage."""
        try:
            s = float(score)
        except (TypeError, ValueError):
            return "initial"
        if s >= self._bands.get("to_steady", 80.0):
            return "steady"
        if s >= self._bands.get("to_intimate", 55.0):
            return "intimate"
        if s >= self._bands.get("to_warming", 25.0):
            return "warming"
        return "initial"

    def resolve(self, intimacy_score: Optional[float]) -> Optional[str]:
        """Resolve profile_id for a given intimacy_score.

        Returns:
          profile_id string if a mapping was found,
          None  if disabled / score unavailable / no mapping configured.

        The caller is responsible for checking pm.has_chat_binding() first
        and for verifying pm.get_persona_by_id(returned_pid) is not None.
        """
        if not self.enabled or not self._map:
            return None
        if intimacy_score is None:
            return None
        try:
            stage = self.score_to_stage(float(intimacy_score))
        except (TypeError, ValueError):
            return None

        # Exact match
        pid = self._map.get(stage, "")
        if pid:
            return pid

        # Adjacent-stage fallback
        try:
            idx = _STAGE_ORDER.index(stage)
        except ValueError:
            return None

        if self._fallback_up:
            # Try progressively higher stages (more intimate)
            for i in range(idx + 1, len(_STAGE_ORDER)):
                pid = self._map.get(_STAGE_ORDER[i], "")
                if pid:
                    return pid
        else:
            # Try progressively lower stages (safer/cooler)
            for i in range(idx - 1, -1, -1):
                pid = self._map.get(_STAGE_ORDER[i], "")
                if pid:
                    return pid
        return None

    def summary(self) -> Dict[str, Any]:
        """Return a diagnostic dict for logging / API status."""
        return {
            "enabled": self.enabled,
            "stage_map": dict(self._map),
            "bands": dict(self._bands),
            "fallback_up": self._fallback_up,
        }
