"""P42 一次性施工器：批量收口 inbox 余部 21 个中小路由文件（全 ratio=1.0）。

策略：一张**共享 curation**（body→spec）铺满全批——bodies 高度重复（`inbox_store 不可用`
横跨 ~10 文件），故一处定义、处处复用：
- 「inbox 不可用/未就绪」四措辞 → 统一 err.svc.inbox_not_ready；
- 单 token / ASCII 斜杠字段 → err.ws.field_required({field})；
- 含中文连接词的复合字段 → 专用 err.ws.*（防 {field} 把中文泄漏进英文）；
- 未列入 curation 的 body 回落 draft 的 match_key（自动复用 err.perm/req/msg_js_* 等）。

人工兜底：presence 的 `detail="seat_limit:…"` 保留机器前缀 `seat_limit:`，只本地化人读部分。
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

SHARED = {
    # 服务不可用 → 归一
    "inbox_store 不可用": "err.svc.inbox_not_ready",
    "inbox store 未就绪": "err.svc.inbox_not_ready",
    "统一收件箱持久层未启用": "err.ws.inbox_persistence_disabled",
    "config_manager 不可用": "err.svc.config_manager_not_ready",
    # {field} 参数化（单 token / ASCII 斜杠，无 EN 泄漏）
    "body 不能为空": (_REQ, {"field": '"body"'}),
    "conversation_id 不能为空": (_REQ, {"field": '"conversation_id"'}),
    "conversation_id 必填": (_REQ, {"field": '"conversation_id"'}),
    "tag 不能为空": (_REQ, {"field": '"tag"'}),
    "chat_key 不能为空": (_REQ, {"field": '"chat_key"'}),
    "conversation_ids 不能为空": (_REQ, {"field": '"conversation_ids"'}),
    "conversation_ids / agent_id 不能为空": (_REQ, {"field": '"conversation_ids / agent_id"'}),
    "conversation_id / to_agent_id 不能为空": (_REQ, {"field": '"conversation_id / to_agent_id"'}),
    # 复合（含中文连接词）→ 专用键
    "title 和 content 不能为空": "err.ws.title_content_required",
    "platform 和 chat_key 不能为空": "err.ws.platform_chatkey_required",
    "conversation_id 或 platform+chat_key 必填": "err.ws.conv_or_platform_required",
    "缺少 platform/chat_key 或 conversation_id": "err.ws.missing_platform_or_conv",
    # 领域专用
    "执行记录不存在": "err.ws.exec_record_not_found",
    "仅可取消运行中的工作链": "err.ws.only_cancel_running_chain",
    "缺少 chain_id": "err.ws.missing_chain_id",
    "该会话已有同链运行中": "err.ws.chain_already_running",
    "注解不存在": "err.ws.annotation_not_found",
    "模板库未启用（需 inbox_store）": "err.ws.template_lib_disabled",
    "模板不存在": "err.ws.template_not_found",
    "删除模板需要主管权限": "err.perm.supervisor_required",  # 复用（去前缀统一）
    "账号不存在": "err.ws.account_not_found",
    "未指定有效的 webhook（index 或 webhook）": "err.ws.no_valid_webhook",
    # f-string
    "不支持的自动化模式: {mode}": ("err.ws.unsupported_automation_mode", {"mode": "mode"}),
    "当前套餐（{_lic.plan}）未包含「{channel}」渠道，请升级套餐。":
        ("err.ws.plan_channel_not_included", {"plan": "_lic.plan", "channel": "channel"}),
}

_SEAT_BODY = "seat_limit:活跃坐席已达授权席位上限，请升级套餐或让其他坐席下线"
PRESENCE_MANUAL = {
    f'detail="{_SEAT_BODY}"': 'detail="seat_limit:" + tr(request, "err.ws.seat_limit_reached")',
}

FILES = [
    "unified_inbox_workflow_routes.py", "unified_inbox_collab_mention_routes.py",
    "unified_inbox_template_routes.py", "unified_inbox_batch_notif_routes.py",
    "unified_inbox_stored_read_routes.py", "unified_inbox_account_routes.py",
    "unified_inbox_queue_webhook_routes.py", "unified_inbox_workspace_presence_routes.py",
    "unified_inbox_workspace_tags_routes.py", "unified_inbox_qa_churn_routes.py",
    "unified_inbox_copilot_routes.py", "unified_inbox_intel_profile_routes.py",
    "unified_inbox_setup_routes.py", "unified_inbox_analyze_routes.py",
    "unified_inbox_aux_read_routes.py", "unified_inbox_auth.py",
    "unified_inbox_collab_context_routes.py", "unified_inbox_read_routes.py",
    "unified_inbox_realtime_routes.py", "unified_inbox_routing_search_routes.py",
    "unified_inbox_workspace_prefs_routes.py",
]
_MANUAL = {"unified_inbox_workspace_presence_routes.py": PRESENCE_MANUAL}
_SKIP = {"unified_inbox_workspace_presence_routes.py": {_SEAT_BODY}}


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
        mapping.update(_MANUAL.get(fname, {}))
        skip = _SKIP.get(fname, set())
        uncovered = [e["body"] for e in pending + fstrings if e["body"] not in skip]
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
