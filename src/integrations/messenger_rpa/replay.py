"""Messenger RPA 异常回放打包器（P3-7）。

当 run_once 失败时（result["error"] 非空）：
- 把 result + 截图 + vision raw 打包成一个 zip
- 放到 tmp_messenger_rpa/replays/
- 有速率限制（同类 error 30 分钟内最多 3 次）和保留上限（最近 7 天）

该模块不抛异常，打包失败时静默返回。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# 同类 error 30 分钟内最多打 N 次（进程内）
_RATE_LIMIT: Dict[str, list] = {}
_RATE_LOCK = threading.Lock()
_RATE_WINDOW_SEC = 1800
_RATE_MAX_PER_WINDOW = 3

# 保留天数
_RETENTION_DAYS = 7


def _rate_allow(error_class: str) -> bool:
    """True = 允许打包。"""
    now = time.time()
    with _RATE_LOCK:
        arr = _RATE_LIMIT.setdefault(error_class, [])
        # 淘汰老的
        arr[:] = [t for t in arr if (now - t) < _RATE_WINDOW_SEC]
        if len(arr) >= _RATE_MAX_PER_WINDOW:
            return False
        arr.append(now)
        return True


def _classify_error(err: str) -> str:
    """把 error 字符串归到大类，用于 rate limit key。"""
    s = str(err or "")[:200]
    if not s:
        return "unknown"
    # 常见模式
    for keyword in (
        "skill_error", "guard_needs_human", "screenshot_", "foreground_",
        "no_peer_message", "risk_blocked_until", "profile_picker",
        "media_approve_enqueue", "vision_import", "vision_call",
    ):
        if keyword in s:
            return keyword
    # 取第一段
    m = re.match(r"^([a-zA-Z_]+)", s)
    if m:
        return m.group(1)[:32]
    return "other"


def _cleanup_old(replays_dir: Path, retention_days: int = _RETENTION_DAYS) -> None:
    cutoff = time.time() - retention_days * 86400
    try:
        for z in replays_dir.glob("*.zip"):
            try:
                if z.stat().st_mtime < cutoff:
                    z.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


def _redact_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """脱敏 config 片段（api key / token）。"""
    if not isinstance(cfg, dict):
        return {}
    out: Dict[str, Any] = {}
    for k, v in cfg.items():
        kl = str(k).lower()
        if any(s in kl for s in ("key", "token", "secret", "password")):
            out[k] = "***REDACTED***"
        elif isinstance(v, dict):
            out[k] = _redact_cfg(v)
        else:
            out[k] = v
    return out


def maybe_pack_run(
    result: Dict[str, Any],
    cfg: Dict[str, Any],
    *,
    replays_subdir: str = "replays",
) -> Optional[str]:
    """若满足条件就把 run 打成 zip。返回 zip 路径或 None。"""
    if not isinstance(result, dict):
        return None
    err = str(result.get("error") or "")
    if not err:
        return None

    # 对 risk_blocked / duplicate 这类"不是 bug"的跳过
    err_low = err.lower()
    if any(k in err_low for k in ("risk_blocked_until", "duplicate_", "rate_limited_")):
        return None

    eclass = _classify_error(err)
    if not _rate_allow(eclass):
        logger.debug("[replay] rate limited for class=%s, skip pack", eclass)
        return None

    # 目标目录
    base = Path(cfg.get("debug_screenshot_dir") or "tmp_messenger_rpa").resolve()
    out_dir = base / replays_subdir
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    _cleanup_old(out_dir)

    ts = time.strftime("%Y%m%d_%H%M%S")
    run_id = str(result.get("run_id") or "noid")[:12]
    # 文件名清洗
    safe_eclass = re.sub(r"[^A-Za-z0-9_]+", "_", eclass)[:32]
    zip_name = f"{ts}_{run_id}_{safe_eclass}.zip"
    zip_path = out_dir / zip_name

    # 打包
    try:
        with zipfile.ZipFile(
            zip_path, "w", compression=zipfile.ZIP_DEFLATED,
        ) as zf:
            # 1) run_result.json（剥 _cap_task 这类 Task 对象）
            clean = {
                k: v for k, v in result.items()
                if not k.startswith("_") and not callable(v)
            }
            zf.writestr(
                "run_result.json",
                json.dumps(clean, ensure_ascii=False, indent=2, default=str),
            )
            # 2) 截图（若存在）
            ss = str(result.get("screenshot_path") or "").strip()
            if ss:
                p = Path(ss)
                if p.exists() and p.is_file():
                    try:
                        zf.write(str(p), arcname=f"screenshot/{p.name}")
                    except Exception:
                        logger.debug("[replay] copy screenshot 失败", exc_info=True)
            # 3) meta.json（含 cfg 摘要）
            meta = {
                "ts": time.time(),
                "error_class": eclass,
                "error": err[:500],
                "step": str(result.get("step") or ""),
                "cfg_keys": list(cfg.keys()) if isinstance(cfg, dict) else [],
                "cfg_redacted": _redact_cfg(cfg),
                "python_os": os.name,
            }
            zf.writestr(
                "meta.json",
                json.dumps(meta, ensure_ascii=False, indent=2, default=str),
            )
        logger.info(
            "[messenger_rpa] P3-7 run replay packed → %s (error_class=%s)",
            zip_path.name, eclass,
        )
        return str(zip_path)
    except Exception:
        logger.debug("[replay] pack 失败", exc_info=True)
        # 清理半成品
        try:
            if zip_path.exists():
                zip_path.unlink()
        except Exception:
            pass
        return None


def _resolve_zip(zip_arg: str, cfg: Dict[str, Any]) -> Path:
    """支持 basename 和绝对路径两种输入。"""
    p = Path(zip_arg)
    if p.is_absolute() and p.exists():
        return p
    base = Path(cfg.get("debug_screenshot_dir") or "tmp_messenger_rpa").resolve()
    out_dir = base / "replays"
    # 先按 basename 查
    cand = out_dir / Path(zip_arg).name
    if cand.exists():
        return cand
    # 再按相对路径查
    cand = out_dir / zip_arg
    if cand.exists():
        return cand
    raise FileNotFoundError(f"replay zip 不存在: {zip_arg}")


def _simple_diff(a: str, b: str) -> str:
    """极简 diff hint：长度变化 + 是否完全不同。"""
    a = (a or "").strip()
    b = (b or "").strip()
    if not a and not b:
        return "both empty"
    if a == b:
        return "identical"
    la, lb = len(a), len(b)
    # jaccard 词级
    sa, sb = set(a.split()), set(b.split())
    inter = len(sa & sb)
    union = max(1, len(sa | sb))
    jacc = inter / union
    return (
        f"len {la}→{lb} ({lb - la:+d}), word_jaccard={jacc:.2f}"
    )


async def rerun_from_zip(
    zip_arg: str,
    cfg: Dict[str, Any],
    app: Any,
    *,
    override_chat_key: Optional[str] = None,
) -> Dict[str, Any]:
    """读 zip 里的 run_result.json，取 peer_text + context，重新跑 LLM。

    完全不触设备；使用 app.state 里已经运行的 SkillManager。
    """
    zp = _resolve_zip(zip_arg, cfg)
    with zipfile.ZipFile(zp) as zf:
        names = zf.namelist()
        if "run_result.json" not in names:
            raise RuntimeError("zip 里没有 run_result.json")
        result = json.loads(zf.read("run_result.json").decode("utf-8"))

    sm = getattr(app.state, "skill_manager", None)
    if sm is None:
        tg = getattr(app.state, "telegram_client", None)
        sm = getattr(tg, "skill_manager", None) if tg else None
    if sm is None:
        raise RuntimeError("SkillManager 未注入，无法 rerun")

    peer_text = str(result.get("peer_text") or "").strip()
    old_reply = str(result.get("reply_text") or "")
    chat_key = override_chat_key or str(
        result.get("chat_key") or f"replay_{zp.stem}"
    )
    chat_name = str(result.get("chat_name") or "")
    peer_kind = str(result.get("peer_kind") or "text")
    caption = str(result.get("image_caption") or "")
    extra_peers = result.get("extra_peers") or []

    # 还原 text_for_ai（按 runner 的拼装规则）
    if peer_kind == "image":
        text_for_ai = f"[图片：{caption}]" if caption else "[图片]"
    else:
        text_for_ai = peer_text or "(no text)"
    if isinstance(extra_peers, list) and extra_peers:
        pieces = [f"(1) {text_for_ai}"]
        for i, p in enumerate(extra_peers[:3], start=2):
            k = str((p or {}).get("kind") or "text")
            c = str((p or {}).get("content") or "")
            d = str((p or {}).get("desc") or "")
            pieces.append(
                f"({i}) {c}" if k == "text" and c
                else f"({i}) [{k}：{d or c}]"
            )
        text_for_ai = "[对方连发]\n" + "\n".join(pieces)

    ctx: Dict[str, Any] = {
        "user_name": chat_name,
        "chat_title": chat_name,
        "chat_id": 0,
        "channel": "messenger_rpa_replay",
        "messenger_rpa_replay": True,
    }
    # 不污染真实 context_store：用独特 chat_key
    rerun_chat_key = f"__replay__{chat_key}_{zp.stem}"[:80]

    t0 = time.time()
    try:
        payload = await sm.process_message(text_for_ai, rerun_chat_key, context=ctx)
    except Exception as ex:
        raise RuntimeError(f"SkillManager.process_message 异常: {ex}")
    elapsed_ms = int((time.time() - t0) * 1000)

    new_reply = ""
    if isinstance(payload, dict):
        new_reply = str(payload.get("reply_text") or payload.get("text") or "")
    elif isinstance(payload, str):
        new_reply = payload

    # 清掉 rerun 写入的临时 chat 上下文（避免污染）
    try:
        cs = getattr(sm, "_context_store", None)
        if cs is not None:
            # 不调 delete（没公开方法），只 pop 缓存
            if hasattr(cs, "_cache"):
                cs._cache.pop(rerun_chat_key, None)
    except Exception:
        pass

    return {
        "zip": zp.name,
        "source_run_id": str(result.get("run_id") or ""),
        "chat_key": chat_key,
        "peer_text": peer_text[:200],
        "peer_kind": peer_kind,
        "text_for_ai": text_for_ai[:600],
        "old_reply": old_reply[:600],
        "new_reply": new_reply[:600],
        "diff_hint": _simple_diff(old_reply, new_reply),
        "elapsed_ms": elapsed_ms,
    }


def list_replays(cfg: Dict[str, Any], *, limit: int = 50) -> Tuple[list, Path]:
    """列出现有回放包（倒序，最新在前）。"""
    base = Path(cfg.get("debug_screenshot_dir") or "tmp_messenger_rpa").resolve()
    out_dir = base / "replays"
    items: list = []
    if not out_dir.exists():
        return items, out_dir
    for z in sorted(out_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            stat = z.stat()
            items.append({
                "name": z.name,
                "path": str(z),
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
            })
        except Exception:
            pass
    return items, out_dir
