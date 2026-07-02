"""P41 一次性施工器：收口 inbox 工作台四族 CJK 响应文案。

- 全程 draft_map → build_draft_map（作用域安全）；
- **参数化**：`{field} 不能为空` 归一到 err.ws.field_required（reason/assignee/agent_id/
  conversation_id/id/due_at/chat_key/platform-account/ci_id 等，单/斜杠 token，无 EN 泄漏）；
- **跨族复用**：inbox「不可用/存储不可用/store 未就绪」四种措辞统一到 err.svc.inbox_not_ready（9 site）；
- **人工兜底**：desktop 的字符串拼接 `"未知 action：" + action`（draft 覆盖率诚实点名的唯一 gap）
  走 MANUAL 映射，保留 `+ action`。
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

CONTACTS = {
    "ci_id 和 target_contact_id 必填": "err.ws.ci_target_required",
    "source_contact_id 和 target_contact_id 必填": "err.ws.source_target_required",
    "ci_id 必填": (_REQ, {"field": '"ci_id"'}),
    "action 必须是 approve / reject": "err.ws.action_approve_reject",
    "contact 不存在": "err.ws.contact_not_found",
    "tags 必须是数组": "err.ws.tags_must_be_array",
    "follow_up_at 必须是时间戳整数": "err.ws.follow_up_at_int",
    "due_at 必须是时间戳整数": "err.ws.due_at_int",
    "due_at 不能为空": (_REQ, {"field": '"due_at"'}),
    "assignee 不能为空": (_REQ, {"field": '"assignee"'}),
    "days/due_at 必须是整数": "err.ws.days_due_at_int",
    "需提供 days 或 due_at": "err.ws.days_or_due_at_required",
    "contacts 未启用": "err.ws.contacts_disabled",
}

RELATIONSHIP = {
    "inbox_store 不可用": "err.svc.inbox_not_ready",
    "无效目标阶段": "err.ws.invalid_target_stage",
    "目标阶段必须高于当前确认阶段": "err.ws.stage_must_be_higher",
    "reason 不能为空": (_REQ, {"field": '"reason"'}),
    "目标阶段必须低于当前确认阶段": "err.ws.stage_must_be_lower",
    "当前会话未检测到久别重逢信号": "err.ws.no_reunion_signal",
    "无可对齐的目标阶段": "err.ws.no_alignable_stage",
}

ESCALATION = {
    "inbox 存储不可用": "err.svc.inbox_not_ready",
    "agent_id 不能为空": (_REQ, {"field": '"agent_id"'}),
    "升级记录 {esc_id} 不存在": ("err.ws.escalation_not_found", {"esc_id": "esc_id"}),
    "conversation_id 不能为空": (_REQ, {"field": '"conversation_id"'}),
    "until_ts 非法": "err.ws.until_ts_invalid",
    "minutes 非法": "err.ws.minutes_invalid",
    "minutes 必须为正（或改传 until_ts）": "err.ws.minutes_must_be_positive",
}

DESKTOP = {
    "无可用对话上下文": "err.ws.no_conversation_context",
    "inbox store 未就绪": "err.svc.inbox_not_ready",
    "platform / account_id 不能为空": (_REQ, {"field": '"platform / account_id"'}),
    "chat_key 不能为空": (_REQ, {"field": '"chat_key"'}),
    "id 不能为空": (_REQ, {"field": '"id"'}),
    "命令不存在或已清理": "err.ws.command_not_found",
    "无客户会话上下文（inbox 未启用或该会话无入站消息），无法重写": "err.ws.no_customer_context",
}
DESKTOP_MANUAL = {
    'HTTPException(400, "未知 action：" + action)':
        'HTTPException(400, tr(request, "err.ws.unknown_action") + action)',
}

_JOBS = (
    ("unified_inbox_workspace_contacts_routes.py", CONTACTS, {}),
    ("unified_inbox_relationship_routes.py", RELATIONSHIP, {}),
    ("unified_inbox_workspace_escalation_routes.py", ESCALATION, {}),
    ("unified_inbox_desktop_routes.py", DESKTOP, DESKTOP_MANUAL),
)


def main() -> int:
    problems = 0
    for fname, curation, manual in _JOBS:
        path = ROUTES / fname
        text = path.read_text(encoding="utf-8")
        ents = draft_map(text, ZH)
        mapping, pending, fstrings = build_draft_map(ents, lambda e: curation.get(e["body"]))
        mapping.update(manual)
        uncovered = [e["body"] for e in pending] + [e["body"] for e in fstrings]
        res = convert_file(path, mapping)
        scope_bad = _verify_request_scope(path.read_text(encoding="utf-8"))
        print(f"[{fname}] entries={len(ents)} map={len(mapping)} "
              f"replaced={res['total_replaced']} unmatched={len(res['unmatched'])} "
              f"uncovered={uncovered} scope_bad={scope_bad}")
        if res["unmatched"] or uncovered or scope_bad:
            problems += 1
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
