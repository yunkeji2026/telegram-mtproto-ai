"""
知识库「直接输出」增强：通道占位符、按状态分支模板、条件片段、受控 AI 路由。
reply_direct_spec 为 JSON，存于 kb_entries.reply_direct_spec。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.utils.channel_status_format import customer_should_omit_channel, is_channel_disabled

_logger = logging.getLogger(__name__)

# 对客话术：不在对话中展示后台配置里的手续费具体数值/比例，由业务主管或人工客服对接咨询
_CUSTOMER_FEE_PLACEHOLDER = "请咨询业务主管或人工客服"

_domain_config_dir: Optional[Path] = None


def set_domain_config_dir(path: Path):
    """Set the domain pack's config directory as fallback for data file loading."""
    global _domain_config_dir
    _domain_config_dir = path


def _normalize_branch_from_status(status: str) -> str:
    """与 branches 键名对齐：normal / volatile / maintenance / unknown"""
    s = (status or "").strip()
    if any(x in s for x in ("维护", "暂停", "停用")):
        return "maintenance"
    if any(x in s for x in ("波动", "不稳定")):
        return "volatile"
    if any(x in s for x in ("正常", "稳定")):
        return "normal"
    return "unknown"


def _load_channels_yaml(cfg_dir: Path) -> Dict[str, Any]:
    path = cfg_dir / "exchange_rates.yaml"
    if not path.exists() and _domain_config_dir:
        path = _domain_config_dir / "exchange_rates.yaml"
    if not path.exists():
        return {}
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("channels") or {}
    except Exception as e:
        _logger.warning("加载 exchange_rates.yaml 失败: %s", e)
        return {}


def _flatten_channel(ch_key: str, ch: Dict[str, Any]) -> Dict[str, str]:
    """兼容 payin/payout 子结构和旧扁平结构。"""
    lim = ch.get("limits") or {}
    if isinstance(lim, dict):
        lim_desc = lim.get("description") or lim.get("default") or ""
    else:
        lim_desc = str(lim)

    from src.utils.channel_status_format import _get_sub, _sub_status

    payin_sr = _get_sub(ch, "payin", "success_rate")
    payout_sr = _get_sub(ch, "payout", "success_rate")
    avg_sr = ""
    if payin_sr is not None and payout_sr is not None:
        avg_sr = str(round((float(payin_sr) + float(payout_sr)) / 2, 1))
    elif payin_sr is not None:
        avg_sr = str(payin_sr)
    elif payout_sr is not None:
        avg_sr = str(payout_sr)

    pi_st = _sub_status(ch, "payin")
    po_st = _sub_status(ch, "payout")
    combined_status = pi_st if pi_st == po_st else f"代收{pi_st}/代付{po_st}"

    return {
        "channel_key": ch_key,
        "channel_display_name": str(ch.get("display_name") or ch_key),
        "channel_fee_rate": _CUSTOMER_FEE_PLACEHOLDER,
        "channel_fee_description": _CUSTOMER_FEE_PLACEHOLDER,
        "channel_status": combined_status,
        "channel_status_description": combined_status,
        "channel_limits": lim_desc,
        "channel_processing_time": str(
            _get_sub(ch, "payin", "processing_time") or ch.get("processing_time") or ""
        ),
        "channel_success_rate": avg_sr,
        "channel_notes": str(ch.get("notes") or ""),
    }


def _match_channel_key(user_text: str, channels: Dict[str, Any]) -> Optional[str]:
    if not user_text or not channels:
        return None
    t = user_text.lower()
    best_k = None
    best_len = 0
    for ck, cfg in channels.items():
        if not isinstance(cfg, dict):
            continue
        if customer_should_omit_channel(str(ck), cfg):
            continue
        names = list(cfg.get("names") or [])
        dn = cfg.get("display_name")
        if dn:
            names.append(dn)
        names.append(ck)
        for n in names:
            if not n:
                continue
            nl = n.lower().strip()
            if len(nl) < 2:
                continue
            if nl in t or nl.upper() in user_text.upper():
                if len(nl) > best_len:
                    best_len = len(nl)
                    best_k = ck
    return best_k


def _safe_format(template: str, mapping: Dict[str, str]) -> str:
    """仅替换已知占位符，缺失键保留原占位符。"""
    out = template
    for k, v in mapping.items():
        out = out.replace("{" + k + "}", str(v))
        out = out.replace("{{" + k + "}}", str(v))
    return out


def _snippet_matches(flat: Dict[str, str], cond: Dict[str, Any]) -> bool:
    if not cond:
        return False
    if "status_in" in cond:
        st = flat.get("channel_status") or ""
        want = cond["status_in"]
        if isinstance(want, str):
            want = [want]
        return any(w in st for w in want)
    if "success_rate_gte" in cond:
        try:
            sr = float(flat.get("channel_success_rate") or 0)
            return sr >= float(cond["success_rate_gte"])
        except (TypeError, ValueError):
            return False
    if "success_rate_lte" in cond:
        try:
            sr = float(flat.get("channel_success_rate") or 0)
            return sr <= float(cond["success_rate_lte"])
        except (TypeError, ValueError):
            return False
    return False


def _apply_snippets(
    text: str,
    snippets: List[Dict[str, Any]],
    flat: Dict[str, str],
) -> Tuple[str, List[str]]:
    applied: List[str] = []
    for sn in snippets:
        if not isinstance(sn, dict):
            continue
        cond = sn.get("if") or sn.get("when")
        if isinstance(cond, dict) and not _snippet_matches(flat, cond):
            continue
        part = (sn.get("append") or sn.get("text") or "").strip()
        if not part:
            continue
        sid = str(sn.get("id") or "snippet")
        pos = (sn.get("position") or "after").lower()
        if pos == "before":
            text = part + "\n" + text
        else:
            text = text.rstrip() + "\n" + part
        applied.append(sid)
    return text, applied


def _parse_json_loose(raw: str) -> Optional[Dict[str, Any]]:
    raw = raw.strip()
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


async def _router_choose(
    ai_client,
    user_message: str,
    branches: Dict[str, str],
    min_confidence: float,
) -> Tuple[str, float, str]:
    """返回 (chosen_key, confidence, raw_note)"""
    if len(branches) <= 1:
        k = next(iter(branches.keys()))
        return k, 1.0, "single_candidate"
    keys = list(branches.keys())
    desc_lines = [f"- {k}: {(branches[k] or '')[:120]}" for k in keys]
    sys_p = (
        "你只输出一个 JSON 对象，不要 markdown，不要解释。"
        '格式：{"chosen":"<候选键之一>","confidence":0.0到1.0}'
    )
    user_p = (
        "用户消息：\n" + user_message[:800] + "\n\n"
        "请从下列候选键中选最匹配的一个：\n" + "\n".join(desc_lines)
    )
    try:
        reply = await ai_client.generate_reply(
            user_p,
            context={"request_id": "kb_direct_router"},
            strategy_overrides={"temperature": 0.1, "max_tokens": 200},
        )
        if not reply:
            return keys[0], 0.0, "empty_ai"
        obj = _parse_json_loose(reply)
        if not obj:
            return keys[0], 0.0, "parse_fail"
        ch = str(obj.get("chosen") or "").strip()
        conf = float(obj.get("confidence") or 0)
        if ch not in branches:
            return keys[0], conf, "invalid_key"
        if conf < min_confidence:
            return keys[0], conf, "low_conf"
        return ch, conf, "ok"
    except Exception as e:
        _logger.warning("KB direct router AI 失败: %s", e)
        return keys[0], 0.0, "error"


def _trace_log(cfg_dir: Path, record: Dict[str, Any]) -> None:
    record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        logf = cfg_dir / "logs" / "kb_direct_trace.jsonl"
        logf.parent.mkdir(parents=True, exist_ok=True)
        with open(logf, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def legacy_direct_text(entry: Dict[str, Any]) -> str:
    raw = entry.get("example_reply_zh") or ""
    if not raw:
        return ""
    import random as _rnd

    variants = [v.strip() for v in raw.split("\n---\n") if v.strip()]
    return _rnd.choice(variants) if variants else raw


def parse_spec(raw: Optional[str]) -> Dict[str, Any]:
    if not raw or not str(raw).strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


async def render_kb_direct_reply(
    entry: Dict[str, Any],
    user_message: str,
    config_dir: Path,
    ai_client=None,
) -> Tuple[str, Dict[str, Any]]:
    """
    渲染 KB 直接输出。无 spec 时等价于 legacy 随机变体。
    返回 (text, meta) meta 含路径、分支、片段、路由信息。
    """
    meta: Dict[str, Any] = {
        "entry_id": entry.get("id"),
        "title": entry.get("title"),
        "path": ["legacy"],
        "branch": None,
        "channel_key": None,
        "snippets": [],
        "router": None,
    }
    spec = parse_spec(entry.get("reply_direct_spec"))
    if not spec or "version" not in spec:
        txt = legacy_direct_text(entry)
        meta["path"] = ["legacy", "no_spec"]
        _trace_log(config_dir, {**meta, "text_len": len(txt), "user_preview": user_message[:80]})
        return txt, meta

    channels = _load_channels_yaml(config_dir)
    ch_file = spec.get("channels_file") or "exchange_rates.yaml"
    if ch_file != "exchange_rates.yaml":
        alt = config_dir / ch_file
        if alt.exists():
            try:
                import yaml
                with open(alt, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                channels = data.get("channels") or {}
            except Exception:
                pass
    channels = {
        k: v for k, v in channels.items()
        if isinstance(v, dict) and not is_channel_disabled(v)
    }

    default_ck = spec.get("default_channel_key")
    matched = _match_channel_key(user_message, channels)
    ck = matched or default_ck
    meta["channel_key"] = ck
    meta["path"].append("channel_match" if matched else "default_or_none")

    flat: Dict[str, str] = {}
    if ck and ck in channels:
        flat = _flatten_channel(ck, channels[ck])
    else:
        flat = {k: "—" for k in _flatten_channel("x", {}).keys()}

    branch_key = _normalize_branch_from_status(flat.get("channel_status") or "")
    meta["branch_from_status"] = branch_key

    branches = spec.get("branches") or {}
    router = spec.get("router") or {}
    template = ""

    if router.get("enabled") and ai_client and isinstance(branches, dict) and len(branches) > 1:
        min_c = float(router.get("min_confidence") or router.get("confidence_threshold") or 0.6)
        chosen, conf, note = await _router_choose(
            ai_client, user_message, branches, min_c
        )
        template = branches.get(chosen) or ""
        meta["router"] = {"chosen": chosen, "confidence": conf, "note": note}
        meta["path"].append("ai_router")
        if note == "low_conf" or note == "parse_fail":
            fb = spec.get("fallback") or entry.get("example_reply_zh") or ""
            if fb:
                template = fb
                meta["path"].append("router_fallback")
    else:
        # 规则优先：按状态选分支
        bk = branch_key if branch_key != "unknown" else (spec.get("default_branch") or "normal")
        if bk not in branches and "unknown" in branches:
            bk = "unknown"
        if bk in branches:
            template = branches[bk]
            meta["branch"] = bk
            meta["path"].append("branch:" + bk)
        elif branches:
            # 取第一个非空
            for k, v in branches.items():
                if v:
                    template = v
                    meta["branch"] = k
                    meta["path"].append("branch_first:" + k)
                    break
        if not template:
            template = entry.get("example_reply_zh") or ""
            meta["path"].append("example_reply_zh")

    if not template:
        txt = legacy_direct_text(entry)
        meta["path"].append("legacy_empty_template")
        _trace_log(config_dir, {**meta, "text_len": len(txt)})
        return txt, meta

    text = _safe_format(template, flat)

    snippets = spec.get("snippets") or []
    if snippets:
        text, applied = _apply_snippets(text, snippets, flat)
        meta["snippets"] = applied
        meta["path"].append("snippets")

    # 仍支持多段随机（整段模板内 ---）
    if "\n---\n" in text:
        import random as _rnd

        variants = [v.strip() for v in text.split("\n---\n") if v.strip()]
        text = _rnd.choice(variants) if variants else text

    meta["text_len"] = len(text)
    _trace_log(config_dir, {**meta, "user_preview": user_message[:120]})
    return text, meta
