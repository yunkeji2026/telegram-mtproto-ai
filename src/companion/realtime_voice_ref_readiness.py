"""实时语音参考音就绪度（纯函数）。

把各 persona 的 ``reference_audio_meta`` 聚合成一张「克隆素材够不够格」的摘要，
供 readiness 信号 / 开闸校准 / ops 卡共用。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

_GRADE_RANK = {"green": 0, "yellow": 1, "red": 2, "unknown": 3, "none": 4}


def summarize_voice_ref_rows(rows: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """聚合人设参考音体检结果。

    ``rows`` 元素形如 ``{persona_id, name?, has_reference, health?: {grade, issues, ...}}``。
    """
    items = list(rows or [])
    with_ref = [r for r in items if r.get("has_reference")]
    grades: List[str] = []
    sample_issues: List[str] = []
    for r in with_ref:
        h = r.get("health") if isinstance(r.get("health"), dict) else {}
        g = str(h.get("grade") or "unknown")
        grades.append(g)
        for iss in (h.get("issues") or [])[:2]:
            s = str(iss or "").strip()
            if s and s not in sample_issues:
                sample_issues.append(s)
    worst = "none"
    if grades:
        worst = max(grades, key=lambda g: _GRADE_RANK.get(g, 3))
    grade_counts: Dict[str, int] = {}
    for g in grades:
        grade_counts[g] = grade_counts.get(g, 0) + 1
    return {
        "persona_count": len(items),
        "with_reference": len(with_ref),
        "without_reference": max(0, len(items) - len(with_ref)),
        "worst_grade": worst,
        "grade_counts": grade_counts,
        "sample_issues": sample_issues[:3],
    }


def apply_ref_to_rtv_verdict(
    verdict: str,
    advice: str,
    ref_summary: Optional[Dict[str, Any]],
) -> tuple[str, str]:
    """在 realtime_voice 运营信号上叠加参考音维度（不单独拆信号，避免看板过碎）。"""
    if not ref_summary:
        return verdict, advice
    pc = int(ref_summary.get("persona_count") or 0)
    wr = int(ref_summary.get("with_reference") or 0)
    worst = str(ref_summary.get("worst_grade") or "none")
    extra = ""
    new_verdict = verdict
    if pc > 0 and wr == 0:
        extra = "尚无参考音（通话降级内置音色），建议在试拨页上传 6–15 秒真人声"
        if new_verdict == "healthy":
            new_verdict = "caution"
    elif worst == "red":
        iss = (ref_summary.get("sample_issues") or ["参考音体检红灯"])[0]
        extra = f"参考音体检红灯（{iss}），重录后再扩量"
        if new_verdict in ("healthy", "insufficient"):
            new_verdict = "caution"
    elif worst == "yellow":
        extra = "部分参考音可优化（体检黄灯），按提示重录克隆会更像"
        if new_verdict == "healthy":
            new_verdict = "caution"
    elif wr > 0 and worst == "green":
        extra = f"{wr} 人设参考音体检通过"
    if not extra:
        return verdict, advice
    sep = "；" if advice else ""
    return new_verdict, f"{advice}{sep}{extra}"


__all__ = ["summarize_voice_ref_rows", "apply_ref_to_rtv_verdict"]
