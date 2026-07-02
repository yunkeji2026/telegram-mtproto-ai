"""P43a 一次性施工器：批量收口 13 个非 inbox 中小路由文件（全 ratio=1.0）。

沿用 P42 施工器 + 共享 curation 模式：未列入 curation 的 body 回落 draft 的 match_key
（自动复用 err.inbox.empty_file / err.svc.config_manager_not_ready / err.kb.question_empty /
err.epi.bot_not_ready_sm / tg_js_021）。f-string 用 (key, fmt) 组装；服务不可用类归 err.svc.*，
RPA 平台/设备类归 err.rpa.*，语音归 err.voice.*，字段必填归 err.ws.field_required。
内建 AST 校验（复用 P40）：含 tr(request,…) 的处理器须有 request 形参。
"""
from __future__ import annotations

from pathlib import Path

from scripts._p40_apply import _verify_request_scope
from scripts.i18n_routeconv import build_draft_map, convert_file, draft_map
from src.web.web_i18n import get_translations

ROUTES = Path("src/web/routes")
ZH = get_translations("zh")
_REQ = "err.ws.field_required"


def _f(field):
    return (_REQ, {"field": f'"{field}"'})


SHARED = {
    # rpa_overview
    "action 须为 {'/'.join(sorted(valid_actions))}，收到: {action}":
        ("err.rpa.action_must_be", {"actions": "'/'.join(sorted(valid_actions))", "got": "action"}),
    "Telegram 走 MTProto 直连，不支持控制": "err.rpa.telegram_no_control",
    "未知平台: {platform}": ("err.rpa.unknown_platform", {"platform": "platform"}),
    "{platform} RPA 服务未构建或未启用": ("err.rpa.service_not_built", {"platform": "platform"}),
    "serial 不能为空": _f("serial"),
    "设备 {serial} 未注册": ("err.rpa.device_not_registered", {"serial": "serial"}),
    "ADB 检测失败: {exc}": ("err.rpa.adb_check_failed", {"err": "exc"}),
    "serials 不能为空": _f("serials"),
    "fields 不能为空": _f("fields"),
    "无有效字段": "err.rpa.no_valid_fields",
    "模板 '{template_name}' 不存在": ("err.rpa.template_not_found", {"template_name": "template_name"}),
    # voice
    "file（参考音频）必填": "err.voice.ref_file_required",
    "persona_id 必填": _f("persona_id"),
    "preferred_name 必填": _f("preferred_name"),
    "人设 {persona_id} 不存在": ("err.voice.persona_not_found", {"persona_id": "persona_id"}),
    "参考音频过大（上限 15MB）": "err.voice.ref_too_large",
    "参考音频落盘失败: {ex}": ("err.voice.ref_save_failed", {"err": "ex"}),
    "from_persona_id 与 to_persona_id 均必填": "err.voice.from_to_persona_required",
    "源与目标人设相同": "err.voice.src_dst_same",
    "源人设 {src_id} 不存在": ("err.voice.src_persona_not_found", {"src_id": "src_id"}),
    "目标人设 {dst_id} 不存在": ("err.voice.dst_persona_not_found", {"dst_id": "dst_id"}),
    "voice 必填": _f("voice"),
    # persona
    "只读账号无法修改人设配置": "err.persona.readonly_no_edit",
    "该操作仅主帐号可执行": "err.perm.master_only",
    "ConfigManager.save_personas 不可用": "err.persona.save_unavailable",
    # cases
    "上下文存储不可用": "err.svc.context_store_not_ready",
    "Case {case_id} 不存在": ("err.case.not_found", {"case_id": "case_id"}),
    # ecommerce
    "电商工具未启用": "err.ec.tools_disabled",
    "缺少 order_no 或可识别的订单号": "err.ec.no_order_no",
    "缺少 tracking_no 或可识别的物流单号": "err.ec.no_tracking_no",
    "未能从文本识别订单号或物流单号": "err.ec.no_id_recognized",
    # telegram
    "保存配置失败": "err.tg.save_config_failed",
    "文件过大（最大 20MB）": "err.tg.file_too_large_20mb",
    # chat_test
    "message 不能为空": _f("message"),
    "SkillManager 未初始化（Bot 未运行）": "err.svc.skill_manager_not_ready",
    # copilot
    "user_message 和 correct_reply 不能为空": "err.cp.user_msg_reply_required",
    # crisis_audit
    "事件不存在或危机审计未启用": "err.ca.event_not_found",
    # human_escalation
    "人工转接存储未初始化": "err.svc.handoff_store_not_ready",
    # page
    "培训演示文件未找到，请联系管理员部署 docs/training/": "err.page.training_not_found",
    # strategy
    "策略追踪器未就绪": "err.svc.strategy_tracker_not_ready",
}

FILES = [
    "rpa_overview_routes.py", "voice_routes.py", "persona_routes.py", "cases_routes.py",
    "ecommerce_tools_routes.py", "telegram_routes.py", "chat_test_routes.py",
    "copilot_routes.py", "crisis_audit_routes.py", "branding_routes.py",
    "human_escalation_routes.py", "page_routes.py", "strategy_routes.py",
]


def _key_for(e):
    spec = SHARED.get(e["body"])
    if spec is not None:
        return spec
    return e["match_key"] if not e["is_fstring"] else None


def main() -> int:
    problems = 0
    for fname in FILES:
        path = ROUTES / fname
        text = path.read_text(encoding="utf-8")
        ents = draft_map(text, ZH)
        mapping, pending, fstrings = build_draft_map(ents, _key_for)
        uncovered = [e["body"] for e in pending + fstrings]
        res = convert_file(path, mapping)
        scope_bad = _verify_request_scope(path.read_text(encoding="utf-8"))
        status = "OK" if not (res["unmatched"] or uncovered or scope_bad) else "!!"
        print(f"[{status}] {fname}: replaced={res['total_replaced']} "
              f"unmatched={len(res['unmatched'])} uncovered={uncovered} scope_bad={scope_bad}")
        if res["unmatched"] or uncovered or scope_bad:
            problems += 1
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
