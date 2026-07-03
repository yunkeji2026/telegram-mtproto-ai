"""P43b 一次性施工器：收口 messenger_rpa_routes.py（147 处，全 ratio=1.0）。

沿用 P43a 施工器结构。最大化复用：
- {dep}/{field}/{key} 参数化键（dep_not_injected 一键吞 34 处 X未注入）；
- 复用既有 err.rpa.op_failed（ascii op）/service_not_started({platform})/lang_unsupported/
  write_failed/chat_key_required/disallowed_fields/queue_item_*/limit_must_be_int/err.set.text_required。
fmt 值语义同 P43a：字符串字面量带引号（'"x"'），f-string 表达式原样（"expr"）。
施工恒开 `convert_file(scope_check=True)`（P43d）——事前 AST scope 校验，此 driver 为新路由族收口参考模板。
"""
from __future__ import annotations

from pathlib import Path

from scripts.i18n_routeconv import build_draft_map, convert_file, draft_map
from src.web.web_i18n import get_translations

FILE = Path("src/web/routes/messenger_rpa_routes.py")
ZH = get_translations("zh")
_WS = "err.ws.field_required"


def _lit(v: str) -> str:
    return f'"{v}"'


def _f(field: str):
    return (_WS, {"field": _lit(field)})


def _rpa_f(field: str):
    return ("err.rpa.field_required", {"field": _lit(field)})


def _arr(field: str):
    return ("err.rpa.must_be_array", {"field": _lit(field)})


def _obj(field):
    return ("err.rpa.must_be_object", {"field": field})


def _inv(field: str):
    return ("err.rpa.invalid_field", {"field": _lit(field)})


def _dep(dep: str):
    return ("err.rpa.dep_not_injected", {"dep": _lit(dep)})


def _op(op: str, err: str = "ex"):
    return ("err.rpa.op_failed", {"op": _lit(op), "err": err})


SHARED = {
    "保存 messenger_rpa 配置失败": "err.rpa.save_config_failed",
    "profiles 必须是数组": _arr("profiles"),
    "profile 必须是对象": _obj(_lit("profile")),
    "profile.id 不能为空": _f("profile.id"),
    "profile.id 重复: {pid}": ("err.rpa.profile_id_duplicate", {"pid": "pid"}),
    "default profile 不存在: {default_id}": ("err.rpa.default_profile_not_found", {"default_id": "default_id"}),
    "body 必须是对象": "err.rpa.body_must_be_object",
    "不允许的字段: {bad}": ("err.rpa.disallowed_fields", {"bad": "bad"}),
    "{k} 必须是对象": _obj("k"),
    "{k} 必须是字符串或数组": ("err.rpa.must_be_str_or_array", {"field": "k"}),
    "reply_profiles 必须是对象": _obj(_lit("reply_profiles")),
    "策略运行状态读取失败: {type(ex).__name__}": ("err.rpa.strategy_status_read_failed", {"err": "type(ex).__name__"}),
    "messenger_rpa state_store 未注入": _dep("messenger_rpa state_store"),
    "text 不能为空": "err.set.text_required",
    "策略模拟失败: {type(ex).__name__}: {ex}": ("err.rpa.strategy_sim_failed", {"err": 'f"{type(ex).__name__}: {ex}"'}),
    "status 不合法": _inv("status"),
    "id 不能为空": _f("id"),
    "persona 已存在: {new_id}": ("err.rpa.persona_exists", {"new_id": "new_id"}),
    "action 只能是 disable / enable / delete / set_default": "err.rpa.action_profile_ops",
    "action 只能是 retry 或 cancel": "err.rpa.action_retry_cancel",
    "该审计记录缺少可回滚的人设配置": "err.rpa.audit_no_persona_rollback",
    "该审计记录缺少账号回滚数据": "err.rpa.audit_no_account_rollback",
    "该审计记录缺少会话回滚数据": "err.rpa.audit_no_chat_rollback",
    "该审计记录缺少任务回滚数据": "err.rpa.audit_no_task_rollback",
    "暂不支持回滚任务状态: {status}": ("err.rpa.rollback_task_status_unsupported", {"status": "status"}),
    "暂不支持该审计类型回滚": "err.rpa.rollback_audit_type_unsupported",
    "mobile_auto.api_base 未配置": ("err.rpa.config_missing", {"name": _lit("mobile_auto.api_base")}),
    "不允许打开的 package: {package}": ("err.rpa.package_not_allowed", {"package": "package"}),
    "不支持的 mobile-auto 操作: {action}": ("err.rpa.mobile_auto_op_unsupported", {"action": "action"}),
    "accounts 必须是数组": _arr("accounts"),
    "未知 account: {aid}": ("err.rpa.unknown_account", {"account_id": "aid"}),
    "reply_profile_id 不存在: {persona}": ("err.rpa.reply_profile_not_found", {"persona": "persona"}),
    "path 必填": _rpa_f("path"),
    "音频文件不存在": "err.rpa.audio_file_not_found",
    "ASR 测试失败: {type(exc).__name__}: {exc}": ("err.rpa.asr_test_failed", {"err": 'f"{type(exc).__name__}: {exc}"'}),
    "text 必填": _rpa_f("text"),
    "试听文本最多 500 字": "err.rpa.preview_text_too_long",
    "TTS 试听失败: {type(exc).__name__}: {exc}": ("err.rpa.tts_preview_failed", {"err": 'f"{type(exc).__name__}: {exc}"'}),
    "state_store 未注入": _dep("state_store"),
    "chat_key 为空": "err.rpa.chat_key_required",
    "line_status 不合法": _inv("line_status"),
    "priority 不合法": _inv("priority"),
    "next_followup_at 必须是时间戳数字": "err.rpa.next_followup_timestamp",
    "state_store 不支持 handoff": "err.rpa.state_store_no_handoff",
    "reply_text 不能为空": _f("reply_text"),
    "仅 pending 审批支持 Suggest More": "err.rpa.suggest_more_pending_only",
    "SkillManager 未注入": _dep("SkillManager"),
    "Suggest More 超时 (>45s)": "err.rpa.suggest_more_timeout",
    "action 必须是 approve 或 reject": "err.rpa.action_approve_or_reject",
    "ids 解析结果为空（或过滤后无匹配）": "err.rpa.ids_empty",
    "llm_cost.dump 失败: {ex}": _op("llm_cost.dump"),
    "service 未注入": _dep("service"),
    "account_registry 未初始化": ("err.rpa.dep_not_initialized", {"dep": _lit("account_registry")}),
    "未知 account: {account_id}": ("err.rpa.unknown_account", {"account_id": "account_id"}),
    "trigger 失败: {ex}": _op("trigger"),
    "send-to 失败: {ex}": _op("send-to"),
    "body 必须是 JSON 对象": "err.rpa.body_must_be_json_object",
    "chat_name 必填": _rpa_f("chat_name"),
    "runner 初始化失败: {ex}": ("err.rpa.runner_init_failed", {"err": "ex"}),
    "list_skipped_chats 失败: {ex}": _op("list_skipped_chats"),
    "list_replays 失败: {ex}": _op("list_replays"),
    "zip 参数必填": _rpa_f("zip"),
    "service 未构建": ("err.rpa.dep_not_built", {"dep": _lit("service")}),
    "service.calibrate_now 不可用": "err.rpa.calibrate_not_available",
    "bot.db 不存在: {db}": ("err.rpa.bot_db_not_found", {"db": "db"}),
    "messenger_rpa.adb_serial 未配置": ("err.rpa.config_missing", {"name": _lit("messenger_rpa.adb_serial")}),
    "accounts_health 不可用": "err.rpa.accounts_health_unavailable",
    "health check 失败: {type(ex).__name__}:{ex}": _op("health check", 'f"{type(ex).__name__}:{ex}"'),
    "chat_names 必须是数组": _arr("chat_names"),
    "bindings 必须是数组": _arr("bindings"),
    "Messenger RPA 服务未启动": ("err.rpa.service_not_started", {"platform": _lit("Messenger")}),
    "chat_key 必填": "err.rpa.chat_key_required",
    "limit 必须为整数": "err.rpa.limit_must_be_int",
    "send_queue item {item_id} 不存在": ("err.rpa.queue_item_not_found", {"item_id": "item_id"}),
    "item {item_id} 不可取消（不存在或已非 queued 状态）": ("err.rpa.queue_item_not_cancelable", {"item_id": "item_id"}),
    "不支持的语言代码: {lang}。支持: {sorted(_VALID_LANGS)}":
        ("err.rpa.lang_unsupported", {"lang": "lang", "langs": "sorted(_VALID_LANGS)"}),
    "写入失败: {e}": ("err.rpa.write_failed", {"err": "e"}),
}


def _key_for(e):
    spec = SHARED.get(e["body"])
    if spec is not None:
        return spec
    return e["match_key"] if not e["is_fstring"] else None


def main() -> int:
    text = FILE.read_text(encoding="utf-8")
    ents = draft_map(text, ZH)
    mapping, pending, fstrings = build_draft_map(ents, _key_for)
    uncovered = [e["body"] for e in pending + fstrings]
    # P43d 起：施工恒开 scope_check——事前剔除落在无 request 作用域的映射（不动源码），
    # 从源头杜绝 tr(request,…) 写进无 request 的 helper。此 driver 为新路由族收口的参考模板。
    res = convert_file(FILE, mapping, scope_check=True)
    scope_skipped = res.get("scope_skipped", [])
    scope_bad = res.get("scope_bad", [])
    ok = not (res["unmatched"] or uncovered or scope_bad)
    print(f"[{'OK' if ok else '!!'}] {FILE.name}: replaced={res['total_replaced']} "
          f"unmatched={len(res['unmatched'])} scope_skipped={len(scope_skipped)} scope_bad={scope_bad}")
    if uncovered:
        print("  UNCOVERED:", uncovered)
    if res["unmatched"]:
        print("  UNMATCHED:", res["unmatched"])
    if scope_skipped:
        print("  SCOPE_SKIPPED (helper 缺 request，需先把 request 收进形参):", scope_skipped)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
