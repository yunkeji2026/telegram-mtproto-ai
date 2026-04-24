"""
Payment domain web routes — channel management pages and APIs.

Extracted from core admin.py to keep domain-specific logic in the domain pack.
"""

import copy
import logging
from pathlib import Path

import yaml
from fastapi import Request, Depends, HTTPException
from fastapi.responses import HTMLResponse

logger = logging.getLogger("PaymentWebRoutes")


def register_routes(app, ctx):
    """Register channel management routes for the payment domain."""

    config_manager = ctx.config_manager
    audit_store = ctx.audit_store
    templates = ctx.templates
    _page_auth = ctx.page_auth
    _api_auth = ctx.api_auth
    _api_write = ctx.api_write_factory
    _auto_snapshot = ctx.auto_snapshot
    _sync_domain_exchange_rates = ctx.sync_domain_exchange_rates

    def _build_channel_list(rates_data=None):
        """将通道 dict 转为含 name 字段的 list，支持 payin/payout 子结构"""
        if rates_data is None:
            rates_data = config_manager.get_exchange_rates_config() or {}
        raw = rates_data.get("channels", {})
        result = []
        for key, cfg in raw.items():
            item = dict(cfg)
            item["name"] = key
            item.setdefault("alert_threshold", 80)
            fallback_amt = cfg.get("amount_type", "integer")
            payin = cfg.get("payin") if isinstance(cfg.get("payin"), dict) else {}
            payout = cfg.get("payout") if isinstance(cfg.get("payout"), dict) else {}
            fallback_pt = cfg.get("processing_time", "")
            if payin or payout:
                for d, sub in [("payin", payin), ("payout", payout)]:
                    sr = sub.get("success_rate")
                    item[f"{d}_success_rate"] = float(sr) if sr is not None else None
                    item[f"{d}_fee_rate"] = sub.get("fee_rate", "")
                    item[f"{d}_minimum_amount"] = str(sub.get("minimum_amount", "100"))
                    item[f"{d}_maximum_amount"] = str(sub.get("maximum_amount", "100000"))
                    item[f"{d}_status"] = sub.get("status", "正常")
                    item[f"{d}_processing_time"] = sub.get("processing_time", "") or fallback_pt
                    item[f"{d}_amount_type"] = sub.get("amount_type", "") or fallback_amt
                item["success_rate"] = item.get("payin_success_rate")
            else:
                sr = cfg.get("success_rate")
                item["success_rate"] = float(sr) if sr is not None else None
                for d in ("payin", "payout"):
                    item[f"{d}_success_rate"] = item["success_rate"]
                    item[f"{d}_fee_rate"] = cfg.get("fee_rate", "")
                    item[f"{d}_minimum_amount"] = str(cfg.get("minimum_amount", "100"))
                    item[f"{d}_maximum_amount"] = str(cfg.get("maximum_amount", "100000"))
                    item[f"{d}_status"] = cfg.get("status", "正常")
                    item[f"{d}_processing_time"] = fallback_pt
                    item[f"{d}_amount_type"] = fallback_amt
            result.append(item)
        return result

    def _sync_rates(rates: dict):
        """Sync main exchange_rates to domain pack config."""
        try:
            main_channels = rates.get("channels", {})
            base = Path(config_manager.config_path).resolve().parent.parent
            domain_file = base / "domains" / "payment" / "config" / "exchange_rates.yaml"
            if not domain_file.exists():
                return
            with open(domain_file, "r", encoding="utf-8") as f:
                domain_data = yaml.safe_load(f) or {}
            domain_channels = domain_data.get("channels", {})
            changed = False
            for key, main_ch in main_channels.items():
                if not isinstance(main_ch, dict):
                    continue
                if key not in domain_channels:
                    domain_channels[key] = dict(main_ch)
                    changed = True
                else:
                    old_ser = yaml.dump(domain_channels[key], default_flow_style=False)
                    domain_channels[key] = copy.deepcopy(main_ch)
                    new_ser = yaml.dump(domain_channels[key], default_flow_style=False)
                    if old_ser != new_ser:
                        changed = True
            if changed:
                domain_data["channels"] = domain_channels
                with open(domain_file, "w", encoding="utf-8") as f:
                    yaml.dump(domain_data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
                logger.info("domain exchange_rates.yaml fully synced from main config")
        except Exception as e:
            logger.warning("domain config sync failed: %s", e)

    # ── Page routes ─────────────────────────────────────────

    @app.get("/channels", response_class=HTMLResponse)
    async def channels_page(request: Request, _=Depends(_page_auth)):
        channels = _build_channel_list()
        alerts_cfg = config_manager.config.get("channel_alerts", {})
        return templates.TemplateResponse(request, "channels.html", {
            "channels": channels, "msg": "",
            "alert_threshold": alerts_cfg.get("success_rate_threshold", 80),
        })

    @app.post("/channels/update")
    async def channels_update(request: Request, _=Depends(_page_auth)):
        form = await request.form()
        channel = form.get("channel", "")
        if not channel:
            raise HTTPException(400, "缺少 channel 参数")
        rates = config_manager.get_exchange_rates_config() or {}
        ch = rates.get("channels", {}).get(channel)
        if not ch:
            raise HTTPException(404, f"通道 {channel} 不存在")
        snap_content = yaml.dump(rates, allow_unicode=True, default_flow_style=False)

        amount_type_top = form.get("amount_type", "")
        if amount_type_top in ("integer", "hundred"):
            ch["amount_type"] = amount_type_top

        status_val = form.get("status", "")
        alert_threshold = form.get("alert_threshold", "")
        if alert_threshold:
            ch["alert_threshold"] = int(alert_threshold)

        if status_val:
            for d in ("payin", "payout"):
                if isinstance(ch.get(d), dict):
                    ch[d]["status"] = status_val
                else:
                    ch["status"] = status_val

        for direction in ("payin", "payout"):
            sub = ch.get(direction)
            if not isinstance(sub, dict):
                sub = {}
                ch[direction] = sub
            prefix = f"{direction}_"
            fee = form.get(f"{prefix}fee_rate", "")
            if fee:
                sub["fee_rate"] = fee
            sr = form.get(f"{prefix}success_rate", "")
            if sr:
                sub["success_rate"] = round(float(sr), 1)
            lo = form.get(f"{prefix}minimum_amount", "")
            if lo:
                sub["minimum_amount"] = lo
            hi = form.get(f"{prefix}maximum_amount", "")
            if hi:
                sub["maximum_amount"] = hi
            d_status = form.get(f"{prefix}status", "")
            if d_status:
                sub["status"] = d_status
            pt = form.get(f"{prefix}processing_time", "")
            if pt is not None and pt != "":
                sub["processing_time"] = pt
            d_amt = form.get(f"{prefix}amount_type", "")
            if d_amt in ("integer", "hundred"):
                sub["amount_type"] = d_amt

        ok, msg = config_manager.save_exchange_rates(rates)
        if ok:
            config_manager.invalidate_exchange_rates_cache()
            _sync_rates(rates)
            actor = request.session.get("username", "web_admin")
            _auto_snapshot("exchange_rates", snap_content, actor)
        channels = _build_channel_list()
        alerts_cfg = config_manager.config.get("channel_alerts", {})
        return templates.TemplateResponse(request, "channels.html", {
            "channels": channels,
            "msg": f"已更新 {channel}" if ok else f"更新失败: {msg}",
            "alert_threshold": alerts_cfg.get("success_rate_threshold", 80),
        })

    # ── API routes ──────────────────────────────────────────

    @app.post("/api/batch-channels")
    async def api_batch_channels(request: Request, _=Depends(_api_write("edit_channel"))):
        """批量更新多个通道状态"""
        body = await request.json()
        names: list = body.get("names", [])
        status: str = body.get("status", "")
        if not names or not status:
            raise HTTPException(400, "names 和 status 不能为空")
        valid_statuses = {"正常", "维护中", "波动", "暂停", "禁用"}
        if status not in valid_statuses:
            raise HTTPException(400, f"status 必须是 {valid_statuses}")
        rates = config_manager.get_exchange_rates_config() or {}
        channels_cfg = rates.get("channels", {})
        updated, not_found = [], []
        for name in names:
            ch_cfg = channels_cfg.get(name)
            if ch_cfg is None:
                not_found.append(name)
            else:
                for d in ("payin", "payout"):
                    if isinstance(ch_cfg.get(d), dict):
                        ch_cfg[d]["status"] = status
                if not isinstance(ch_cfg.get("payin"), dict) and not isinstance(ch_cfg.get("payout"), dict):
                    ch_cfg["status"] = status
                updated.append(name)
        if not updated:
            raise HTTPException(404, f"未找到任何通道: {not_found}")
        snap_content = yaml.dump(rates, allow_unicode=True, default_flow_style=False)
        ok, msg = config_manager.save_exchange_rates(rates)
        if not ok:
            raise HTTPException(500, msg)
        config_manager.invalidate_exchange_rates_cache()
        _sync_rates(rates)
        actor = request.session.get("username", "api")
        _auto_snapshot("exchange_rates", snap_content, actor)
        if audit_store:
            audit_store.log(actor, "batch_channel_status", ",".join(updated), "", status)
        return {"ok": True, "updated": updated, "not_found": not_found, "status": status}

    @app.get("/api/channels")
    async def api_get_channels(request: Request, _=Depends(_api_auth)):
        rates = config_manager.get_exchange_rates_config() or {}
        return rates.get("channels", {})

    @app.put("/api/channels/{channel}")
    async def api_update_channel(channel: str, request: Request, _=Depends(_api_write("edit_channel"))):
        body = await request.json()
        rates = config_manager.get_exchange_rates_config() or {}
        ch = rates.get("channels", {}).get(channel)
        if not ch:
            raise HTTPException(404, f"Channel '{channel}' not found")
        for field in ("fee_rate", "status", "processing_time", "notes"):
            if field in body:
                ch[field] = body[field]
        if "success_rate" in body:
            ch["success_rate"] = round(float(body["success_rate"]), 1)
        ok, msg = config_manager.save_exchange_rates(rates)
        if not ok:
            raise HTTPException(500, msg)
        config_manager.invalidate_exchange_rates_cache()
        _sync_rates(rates)
        if audit_store:
            audit_store.log("api", "update_channel", channel, "", str(body)[:100])
        return {"ok": True, "channel": channel}

    # ── KB conflict checker registration ──────────────────

    _CHANNEL_DATA_KEYWORDS = (
        "成功率", "费率", "手续费", "限额", "额度", "代收", "代付",
        "通道状态", "处理时间", "payin", "payout", "success_rate",
        "fee_rate", "channel_status",
    )

    def _check_channel_data_conflict(data: dict) -> list:
        """检测 KB 条目是否与通道实时数据冲突（支付域专用）。"""
        import re
        warnings = []
        blob = " ".join(str(v) for v in [
            data.get("title", ""), data.get("category", ""),
            data.get("triggers", ""), data.get("scenario", ""),
            data.get("example_reply_zh", ""), data.get("steps", ""),
        ]).lower()
        matched = [kw for kw in _CHANNEL_DATA_KEYWORDS if kw.lower() in blob]
        if matched:
            warnings.append(
                f"检测到通道数据相关关键词：{'、'.join(matched)}。"
                "通道的成功率、费率、限额、状态等数据由【通道管理】面板实时控制，"
                "KB 中的相关数值不会被使用。请确认此条目仅包含话术风格，不含具体数据。"
            )
        if data.get("category") == "通道状态":
            warnings.append(
                "分类为「通道状态」的条目已由系统程序化回复接管，"
                "KB 条目不会生效。建议改用其他分类或移除。"
            )
        example = data.get("example_reply_zh", "")
        if re.search(r'\d+[\.\d]*\s*%', example):
            warnings.append(
                "示例回复中包含百分比数值，这可能与通道实时数据冲突。"
                "建议移除具体数字，改用「参考实时数据」等占位表述。"
            )
        return warnings

    if hasattr(app.state, "kb_conflict_checkers"):
        app.state.kb_conflict_checkers.append(_check_channel_data_conflict)

    if hasattr(app.state, "intent_display_names_extra"):
        app.state.intent_display_names_extra.update({
            "order_query": "订单查询",
            "order_query_with_number": "带单号查单",
            "price_check": "价格咨询",
            "status_check": "状态查询",
            "gxp_command": "GXP 命令",
            "channel_info": "通道信息",
            "enhanced_quota_config": "增强配额配置",
            "quota_config": "配额配置",
        })

    # Legacy compatibility: /api/kb/check-channel-conflict
    @app.post("/api/kb/check-channel-conflict")
    async def api_kb_check_channel_conflict_legacy(request: Request):
        from fastapi import Depends as _Dep
        data = await request.json()
        warnings = _check_channel_data_conflict(data)
        return {"has_conflict": bool(warnings), "warnings": warnings}

    logger.info("Payment channel routes registered: /channels, /api/channels, /api/batch-channels, kb-conflict-checker")
