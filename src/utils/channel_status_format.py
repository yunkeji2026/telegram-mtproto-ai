"""
将 exchange_rates.channels 格式化为注入 AI 的实时通道状态文本。
与 SkillManager._get_live_channel_status 策略一致，便于单测与复用。
支持 payin/payout 结构（代收/代付分开），同时兼容旧扁平结构。
"""

from typing import Any, Dict, Optional

DISABLED_STATUSES = frozenset({"禁用", "disabled", "停用"})

AMOUNT_TYPE_LABELS = {"hundred": "整百", "integer": "整数"}

_CUSTOMER_OMIT_KEYS = frozenset({"other"})


def _get_sub(ch: dict, direction: str, field: str, fallback_key: str = ""):
    """从 payin/payout 子结构取值，不存在时回退到通道顶层（兼容旧结构）。"""
    sub = ch.get(direction)
    if isinstance(sub, dict) and field in sub:
        return sub[field]
    fk = fallback_key or field
    return ch.get(fk)


def _sub_status(ch: dict, direction: str) -> str:
    return str(_get_sub(ch, direction, "status") or "正常").strip()


def is_channel_disabled(ch: dict) -> bool:
    """通道级禁用判定：如果存在 payin/payout 子结构，两者都禁用才算通道禁用；
    否则检查顶层 status。"""
    payin = ch.get("payin")
    payout = ch.get("payout")
    if isinstance(payin, dict) and isinstance(payout, dict):
        return (
            _sub_status(ch, "payin") in DISABLED_STATUSES
            and _sub_status(ch, "payout") in DISABLED_STATUSES
        )
    return (ch.get("status") or "").strip() in DISABLED_STATUSES


def is_direction_disabled(ch: dict, direction: str) -> bool:
    """单方向禁用判定。"""
    return _sub_status(ch, direction) in DISABLED_STATUSES


def customer_should_omit_channel(channel_key: str, ch: Optional[dict] = None) -> bool:
    if ch is None:
        ch = {}
    k = (channel_key or "").strip().lower()
    if k in _CUSTOMER_OMIT_KEYS:
        return True
    blob = f"{channel_key} {ch.get('display_name', '')}"
    for nm in ch.get("names") or []:
        if isinstance(nm, str):
            blob += " " + nm
    if "PIX" in blob.upper():
        return True
    return False


def _format_direction(ch: dict, direction: str, label: str, include_fee: bool) -> str:
    """格式化单个方向（代收/代付）的信息。"""
    st = _sub_status(ch, direction)
    parts = [f"{label}: 状态={st}"]
    sr = _get_sub(ch, direction, "success_rate")
    if sr is not None:
        parts.append(f"成功率={sr}%")
    if include_fee:
        fee = _get_sub(ch, direction, "fee_rate")
        if fee:
            parts.append(f"费率={fee}")
    lo = _get_sub(ch, direction, "minimum_amount")
    hi = _get_sub(ch, direction, "maximum_amount")
    if lo and hi:
        parts.append(f"限额={lo}-{hi}")
    pt = _get_sub(ch, direction, "processing_time")
    if pt:
        parts.append(f"处理时间={pt}")
    amt = _get_sub(ch, direction, "amount_type")
    if not amt:
        amt = ch.get("amount_type", "")
    amt_lbl = AMOUNT_TYPE_LABELS.get(amt, "")
    if amt_lbl:
        parts.append(f"金额类型={amt_lbl}")
    return ", ".join(parts)


def format_live_channel_status_text(channels: Dict[str, Any], *, include_fee: bool) -> str:
    """将 channels 映射格式化为多行文本；无可用通道时返回空串。
    支持 payin/payout 子结构，分别输出代收和代付信息。"""
    if not channels:
        return ""
    lines = []
    disabled_names = []
    for key, ch in channels.items():
        if not isinstance(ch, dict):
            continue
        if customer_should_omit_channel(str(key), ch):
            continue
        name = ch.get("display_name") or str(key).upper()
        if is_channel_disabled(ch):
            disabled_names.append(name)
            continue

        has_sub = isinstance(ch.get("payin"), dict) or isinstance(ch.get("payout"), dict)

        if has_sub:
            lines.append(name)
            if isinstance(ch.get("payin"), dict) and not is_direction_disabled(ch, "payin"):
                lines.append("  " + _format_direction(ch, "payin", "代收", include_fee))
            if isinstance(ch.get("payout"), dict) and not is_direction_disabled(ch, "payout"):
                lines.append("  " + _format_direction(ch, "payout", "代付", include_fee))
        else:
            status = ch.get("status", "正常")
            parts = [f"{name}: 状态={status}"]
            sr = ch.get("success_rate")
            if sr is not None:
                parts.append(f"成功率={sr}%")
            if include_fee:
                fee = ch.get("fee_rate")
                if fee:
                    parts.append(f"费率={fee}")
            lo = ch.get("minimum_amount", "")
            hi = ch.get("maximum_amount", "")
            if lo and hi:
                parts.append(f"单笔限额={lo}-{hi}")
            amt = ch.get("amount_type", "")
            amt_label = AMOUNT_TYPE_LABELS.get(amt, "")
            if amt_label:
                parts.append(f"金额类型={amt_label}")
            lines.append(", ".join(parts))

    if disabled_names:
        lines.append(f"已禁用通道（不可用，不接受订单）: {', '.join(disabled_names)}")
    return "\n".join(lines)
