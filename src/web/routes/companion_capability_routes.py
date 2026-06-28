"""陪伴能力就绪度看板 API（只读，无副作用）。

端点：
  GET /api/companion/capabilities  —— 全部陪伴能力的开/关/灰度/blocked 状态 +
      分阶段开启阶梯 + 行动建议。服务「看板→校准→分阶段开启」北极星第一步：先看清楚。

纯聚合在 ``src.companion.capability_status.collect_capability_status``；本层只把
config（``config_manager.config``）与运行时子系统挂载信号（``app.state``）喂进去。
子系统未就绪时对应能力判 blocked 而非报错——与本仓「软降级」约定一致。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from fastapi import Depends, Request

logger = logging.getLogger(__name__)

# capability runtime_dep 键 → app.state 属性名（存在即视为子系统已挂载）
_RUNTIME_STATE_ATTRS = {
    "translation_service": "translation_service",
    "quality_trend_store": "quality_trend_store",
    "autosend_worker": "autosend_worker",
    "companion_proactive_preview": "companion_proactive_preview",
    "care_schedule_store": "care_schedule_store",
    "deferred_outbox_store": "deferred_outbox_store",
}


def _audit_path(cm):
    base = getattr(cm, "config_path", None)
    return (Path(base).parent / "companion_capability_audit.jsonl") if base else None


def _snapshot_path(cm):
    base = getattr(cm, "config_path", None)
    return (Path(base).parent / "companion_capability_last_snapshot.json") if base else None


def _audit_toggle(cm, *, actor, key, field, value, path, reason="") -> None:
    """把开关变更追加到 config 目录下 companion_capability_audit.jsonl（best-effort）。"""
    try:
        p = _audit_path(cm)
        if p is None:
            return
        rec = {"ts": round(time.time(), 3), "actor": actor, "key": key,
               "field": field, "value": value, "path": path, "reason": reason}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("写陪伴能力开关审计失败（忽略）", exc_info=True)


def _read_audit(path, limit) -> list:
    """读审计 JSONL，返回最新在前的最多 limit 条（坏行跳过）。"""
    if not path or not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    cap = max(1, min(int(limit or 50), 500))
    out = []
    for ln in reversed(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
        if len(out) >= cap:
            break
    return out


def _apply_one(cm, config, modes, key, field, value, *, actor, reason) -> dict:
    """单条意图过护栏 + 写 overlay + 审计，返回结果项（不抛）。"""
    from src.companion.capability_toggle import check_toggle

    item = {"key": key, "field": field, "value": value}
    chk = check_toggle(config, modes, key, field, value)
    if not chk.get("allowed"):
        item["status"] = "blocked"
        item["reason"] = chk.get("reason") or "护栏拒绝"
        return item
    path = chk.get("flag_path") or ""
    ok, msg = cm.set_overlay_flag(path, value)
    if not ok:
        item["status"] = "error"
        item["reason"] = msg
        return item
    _audit_toggle(cm, actor=actor, key=key, field=field, value=value,
                  path=path, reason=reason)
    item["status"] = "applied"
    item["path"] = path
    if chk.get("warn"):
        item["warn"] = chk.get("reason")
    return item


def _apply_plan(cm, config, modes, plan, *, actor, reason) -> dict:
    """逐条应用意图列表（config 被 set_overlay_flag 就地更新，后条能看到前条结果）。"""
    applied, blocked, warned = [], [], []
    for it in plan or []:
        res = _apply_one(cm, config, modes, it["key"], it["field"],
                         bool(it["value"]), actor=actor, reason=reason)
        if res["status"] == "applied":
            applied.append(res)
            if res.get("warn"):
                warned.append(res)
        else:
            blocked.append(res)
    return {"applied": applied, "blocked": blocked, "warned": warned}


def _collect_status(state, config):
    from src.companion.capability_status import collect_capability_status
    runtime = {dep: (getattr(state, attr, None) is not None)
               for dep, attr in _RUNTIME_STATE_ATTRS.items()}
    return collect_capability_status(config, runtime=runtime)


def _auto_ai_count(state):
    store = getattr(state, "inbox_store", None)
    if store is None:
        return None
    try:
        return sum(1 for v in store.all_automation_modes().values() if v == "auto_ai")
    except Exception:
        logger.debug("auto_ai 计数失败", exc_info=True)
        return None


def _gather_readiness(state, window_hours):
    """从各运营数据源 best-effort 取数 → readiness_signals（signals/advice 共用）。"""
    from src.companion.readiness_signals import readiness_signals

    win = max(1.0, min(float(window_hours or 168), 720))
    since = time.time() - win * 3600.0

    text_quality = None
    store = getattr(state, "inbox_store", None)
    if store is not None:
        try:
            text_quality = store.get_quality_stats(since_ts=since)
        except Exception:
            logger.debug("get_quality_stats 失败", exc_info=True)

    proactive_quality = None
    try:
        from src.monitoring.metrics_store import get_metrics_store
        proactive_quality = get_metrics_store().companion_quality_overview(
            window_sec=win * 3600.0)
    except Exception:
        logger.debug("companion_quality_overview 失败", exc_info=True)

    sent = failed = None
    svc = getattr(state, "draft_service", None)
    if svc is not None:
        try:
            rows = svc.list_audit(since_ts=since, limit=2000)
            sent = sum(1 for r in rows if str(r.get("action")) == "autosend")
            failed = sum(1 for r in rows if str(r.get("action")) == "autosend_failed")
        except Exception:
            logger.debug("list_audit 计数失败", exc_info=True)

    return readiness_signals(
        text_quality=text_quality, proactive_quality=proactive_quality,
        delivery={"autosend": sent, "autosend_failed": failed})


def gather_companion_advice(state, config, window_hours: float = 168):
    """组合 能力档 × 信号 → build_advice（advice 端点与 ops-overview 共用，单一事实源）。"""
    from src.companion.capability_advisor import build_advice
    from src.companion.embedding_readiness import embedding_source_configured
    status = _collect_status(state, config)
    signals = _gather_readiness(state, window_hours)
    embed_ready = embedding_source_configured(config)
    return build_advice(status, signals, auto_ai=_auto_ai_count(state),
                        embed_ready=embed_ready)


def register_companion_capability_routes(app, *, api_auth) -> None:
    @app.get("/api/companion/capabilities")
    async def api_companion_capabilities(request: Request, _=Depends(api_auth)):
        """陪伴栈能力就绪度看板。"""
        from src.companion.capability_status import collect_capability_status

        state = request.app.state
        cm = getattr(state, "config_manager", None)
        config = getattr(cm, "config", None) if cm is not None else None
        if not isinstance(config, dict):
            return {"ok": False, "available": False,
                    "message": "config 未就绪", "capabilities": []}

        runtime = {dep: (getattr(state, attr, None) is not None)
                   for dep, attr in _RUNTIME_STATE_ATTRS.items()}
        try:
            data = collect_capability_status(config, runtime=runtime)
        except Exception:
            logger.warning("companion capability status 计算失败", exc_info=True)
            return {"ok": False, "available": True, "capabilities": [],
                    "message": "聚合计算失败"}
        return {"ok": True, "available": True, **data}

    @app.get("/api/companion/capabilities/delivery-calibration")
    async def api_companion_delivery_calibration(
        request: Request, window_hours: float = 24, _=Depends(api_auth),
    ):
        """全自动「真发」主开关开闸前校准：auto_ai 会话分布 + 三开关 verdict + 近期真发/失败。"""
        from src.companion.delivery_calibration import delivery_calibration

        state = request.app.state
        cm = getattr(state, "config_manager", None)
        config = getattr(cm, "config", None) if cm is not None else None
        if not isinstance(config, dict):
            return {"ok": False, "available": False, "message": "config 未就绪"}

        # 会话档位分布（auto_ai 决定 deliver 是否对人真发）
        modes = None
        store = getattr(state, "inbox_store", None)
        if store is not None:
            try:
                modes = store.all_automation_modes()
            except Exception:
                logger.debug("all_automation_modes 失败", exc_info=True)

        # 近窗口真发/失败计数（best-effort；审计不可用则 None=未知）
        sent = failed = None
        svc = getattr(state, "draft_service", None)
        if svc is not None:
            try:
                win = max(1.0, min(float(window_hours or 24), 720))
                rows = svc.list_audit(since_ts=time.time() - win * 3600.0, limit=1000)
                sent = sum(1 for r in rows if str(r.get("action")) == "autosend")
                failed = sum(1 for r in rows
                             if str(r.get("action")) == "autosend_failed")
            except Exception:
                logger.debug("list_audit 计数失败", exc_info=True)

        try:
            data = delivery_calibration(
                config, modes, recent_autosend=sent, recent_autosend_failed=failed)
        except Exception:
            logger.warning("delivery calibration 计算失败", exc_info=True)
            return {"ok": False, "available": True, "message": "校准计算失败"}
        return {"ok": True, "available": True, "window_hours": window_hours, **data}

    @app.post("/api/companion/capabilities/toggle")
    async def api_companion_capability_toggle(request: Request, _=Depends(api_auth)):
        """分阶段开启「开」一步：带服务端护栏地开/关单个能力（写 config overlay + 审计）。

        body: {key, field?("enabled"|"dry_run"), value:bool, actor?}。
        护栏拒绝时返回 ``{ok:False, blocked:True, message}``；放行但有风险时 ``warn`` 带提示。
        """
        from src.companion.capability_status import collect_capability_status
        from src.companion.capability_toggle import check_toggle

        state = request.app.state
        cm = getattr(state, "config_manager", None)
        config = getattr(cm, "config", None) if cm is not None else None
        if cm is None or not isinstance(config, dict):
            return {"ok": False, "available": False, "message": "config 未就绪"}
        if not hasattr(cm, "set_overlay_flag"):
            return {"ok": False, "message": "配置写入能力不可用（请升级 ConfigManager）"}

        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        key = str(body.get("key") or "").strip()
        field = str(body.get("field") or "enabled").strip()
        value = bool(body.get("value"))
        actor = (str(body.get("actor") or "").strip() or "web-admin")
        if not key:
            return {"ok": False, "message": "缺少 key"}

        modes = None
        store = getattr(state, "inbox_store", None)
        if store is not None:
            try:
                modes = store.all_automation_modes()
            except Exception:
                logger.debug("all_automation_modes 失败", exc_info=True)

        chk = check_toggle(config, modes, key, field, value)
        if not chk.get("allowed"):
            return {"ok": False, "blocked": True,
                    "message": chk.get("reason") or "护栏拒绝此操作"}

        path = chk.get("flag_path") or ""
        ok, msg = cm.set_overlay_flag(path, value)
        if not ok:
            return {"ok": False, "message": msg}
        _audit_toggle(cm, actor=actor, key=key, field=field, value=value, path=path)

        # 回算该能力最新状态 + 概要，供前端就地刷新
        runtime = {dep: (getattr(state, attr, None) is not None)
                   for dep, attr in _RUNTIME_STATE_ATTRS.items()}
        capability = summary = None
        try:
            data = collect_capability_status(config, runtime=runtime)
            capability = next((c for c in data.get("capabilities", [])
                               if c.get("key") == key), None)
            summary = data.get("summary")
        except Exception:
            logger.debug("回算能力状态失败", exc_info=True)
        resp = {"ok": True, "message": "已生效", "path": path, "field": field,
                "value": value, "capability": capability, "summary": summary}
        if chk.get("warn"):
            resp["warn"] = chk.get("reason")
        return resp

    @app.get("/api/companion/capabilities/toggle-audit")
    async def api_companion_toggle_audit(
        request: Request, limit: int = 50, _=Depends(api_auth),
    ):
        """最近的能力开关变更（谁/何时/改了啥/上下文 reason），最新在前。"""
        cm = getattr(request.app.state, "config_manager", None)
        if cm is None:
            return {"ok": False, "available": False, "entries": []}
        entries = _read_audit(_audit_path(cm), limit)
        return {"ok": True, "available": True,
                "count": len(entries), "entries": entries}

    @app.post("/api/companion/capabilities/preset")
    async def api_companion_capability_preset(request: Request, _=Depends(api_auth)):
        """一键预设档：按风险阶梯整档切换（每条仍逐项过护栏）；切换前自动存快照供回滚。

        body: {name: safe_default|dry_run_trial|full_auto, actor?}。
        """
        from src.companion.capability_presets import (
            PRESETS, build_preset_plan, capture_snapshot,
        )
        from src.companion.capability_status import collect_capability_status

        state = request.app.state
        cm = getattr(state, "config_manager", None)
        config = getattr(cm, "config", None) if cm is not None else None
        if cm is None or not isinstance(config, dict) or not hasattr(cm, "set_overlay_flag"):
            return {"ok": False, "available": False, "message": "config 未就绪"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        name = str(body.get("name") or "").strip()
        actor = (str(body.get("actor") or "").strip() or "web-admin")
        plan = build_preset_plan(name)
        if plan is None:
            return {"ok": False, "message": f"未知预设: {name}",
                    "presets": {k: v["label"] for k, v in PRESETS.items()}}

        modes = None
        store = getattr(state, "inbox_store", None)
        if store is not None:
            try:
                modes = store.all_automation_modes()
            except Exception:
                logger.debug("all_automation_modes 失败", exc_info=True)

        # 切换前存快照（best-effort，供 rollback）
        try:
            sp = _snapshot_path(cm)
            if sp is not None:
                snap = {"ts": round(time.time(), 3), "actor": actor,
                        "applied_preset": name, "snapshot": capture_snapshot(config)}
                sp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        except Exception:
            logger.debug("存快照失败（忽略）", exc_info=True)

        result = _apply_plan(cm, config, modes, plan, actor=actor, reason=f"preset:{name}")
        summary = None
        try:
            runtime = {dep: (getattr(state, attr, None) is not None)
                       for dep, attr in _RUNTIME_STATE_ATTRS.items()}
            summary = collect_capability_status(config, runtime=runtime).get("summary")
        except Exception:
            logger.debug("回算概要失败", exc_info=True)
        return {"ok": True, "preset": name, "label": PRESETS[name]["label"],
                "summary": summary, **result}

    @app.post("/api/companion/capabilities/rollback")
    async def api_companion_capability_rollback(request: Request, _=Depends(api_auth)):
        """回滚到上一次预设切换前的快照（逐项过护栏；条件变了的项会被如实拦下）。"""
        from src.companion.capability_presets import snapshot_to_plan
        from src.companion.capability_status import collect_capability_status

        state = request.app.state
        cm = getattr(state, "config_manager", None)
        config = getattr(cm, "config", None) if cm is not None else None
        if cm is None or not isinstance(config, dict) or not hasattr(cm, "set_overlay_flag"):
            return {"ok": False, "available": False, "message": "config 未就绪"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        actor = (str((body or {}).get("actor") or "").strip() or "web-admin")

        sp = _snapshot_path(cm)
        if sp is None or not sp.exists():
            return {"ok": False, "message": "无可回滚的快照（尚未做过一键切换）"}
        try:
            blob = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            return {"ok": False, "message": "快照损坏，无法回滚"}
        plan = snapshot_to_plan((blob or {}).get("snapshot") or {})
        if not plan:
            return {"ok": False, "message": "快照为空"}

        modes = None
        store = getattr(state, "inbox_store", None)
        if store is not None:
            try:
                modes = store.all_automation_modes()
            except Exception:
                logger.debug("all_automation_modes 失败", exc_info=True)

        result = _apply_plan(cm, config, modes, plan, actor=actor, reason="rollback")
        summary = None
        try:
            runtime = {dep: (getattr(state, attr, None) is not None)
                       for dep, attr in _RUNTIME_STATE_ATTRS.items()}
            summary = collect_capability_status(config, runtime=runtime).get("summary")
        except Exception:
            logger.debug("回算概要失败", exc_info=True)
        return {"ok": True, "restored_from": (blob or {}).get("ts"),
                "undid_preset": (blob or {}).get("applied_preset"),
                "summary": summary, **result}

    @app.get("/api/companion/capabilities/signals")
    async def api_companion_capability_signals(
        request: Request, window_hours: float = 168, _=Depends(api_auth),
    ):
        """决策信号：把真实运营指标翻成「该不该往上爬一档」的数据驱动建议。

        三路：文本自动回复质量(get_quality_stats) + 主动触达好评(companion_quality_overview)
        + 真发投递失败率(近窗口审计)。每路给 verdict + 一句下一步建议。
        """
        data = _gather_readiness(request.app.state, window_hours)
        return {"ok": True, "available": True, "window_hours": window_hours, **data}

    @app.get("/api/companion/capabilities/advice")
    async def api_companion_capability_advice(
        request: Request, window_hours: float = 168, _=Depends(api_auth),
    ):
        """能力档 × 决策信号 联动建议 + 配置一致性体检（闭环纠偏）。

        把每档当前状态与对应运营信号对齐成一条可执行建议（含一键 target），
        并查开关自洽性（真发开但 worker 关 / 无 auto_ai / 裸奔 / 语音孤悬 / blocked）。
        """
        state = request.app.state
        cm = getattr(state, "config_manager", None)
        config = getattr(cm, "config", None) if cm is not None else None
        if not isinstance(config, dict):
            return {"ok": False, "available": False, "message": "config 未就绪"}
        try:
            advice = gather_companion_advice(state, config, window_hours)
        except Exception:
            logger.warning("companion advice 聚合失败", exc_info=True)
            return {"ok": False, "available": True, "message": "建议聚合失败"}
        return {"ok": True, "available": True, "window_hours": window_hours, **advice}


__all__ = ["register_companion_capability_routes"]
