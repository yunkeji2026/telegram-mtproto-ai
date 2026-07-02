"""P39 一次性施工器：收口 auth_user_routes + settings_routes 的 CJK 响应文案。

由 `python -m scripts.i18n_routeconv --suggest <file>` 的键匹配建议 curate 而来：
- 密码/Token 类**跨族复用**现有键（su_js_003 / base.shell.pwd_min_len / token_error），零新增；
- settings 的「X 格式错误 / X 必须是 JSON …」四组 f-string 用 {field} **参数化收敛**到
  err.set.json_parse_error / json_must_be_array / json_must_be_object（15 site → 10 键）。

幂等：重跑不再命中（already tr(...)），report 全 0。
"""
from __future__ import annotations

from pathlib import Path

from scripts.i18n_routeconv import convert_file

_ROUTES = Path("src/web/routes")

# ── auth_user_routes.py ──（复用 token_error / su_js_003 / base.shell.pwd_min_len）
AUTH_MAP = {
    '"error": "用户名或密码错误"': '"error": tr(request, "err.auth.bad_credentials")',
    '"error": "Token 错误"': '"error": tr(request, "token_error")',
    'HTTPException(400, "系统已初始化，请通过用户管理页面操作")':
        'HTTPException(400, tr(request, "err.auth.already_initialized"))',
    'HTTPException(400, "用户名和密码不能为空")':
        'HTTPException(400, tr(request, "err.auth.user_pass_required"))',
    'HTTPException(400, "密码至少 6 位")':
        'HTTPException(400, tr(request, "su_js_003"))',
    'HTTPException(400, "两次密码不一致")':
        'HTTPException(400, tr(request, "err.auth.pwd_mismatch_signup"))',
    'HTTPException(500, "账户创建失败")':
        'HTTPException(500, tr(request, "err.auth.create_failed"))',
    'HTTPException(400, "API Key 不能为空")':
        'HTTPException(400, tr(request, "err.auth.api_key_required"))',
    'HTTPException(400, "Base URL 不能为空")':
        'HTTPException(400, tr(request, "err.auth.base_url_required"))',
    'HTTPException(400, "请输入旧密码和新密码")':
        'HTTPException(400, tr(request, "err.auth.old_new_pwd_required"))',
    'HTTPException(400, "新密码至少 6 位")':
        'HTTPException(400, tr(request, "base.shell.pwd_min_len"))',
    'HTTPException(400, "无法确定当前用户")':
        'HTTPException(400, tr(request, "err.auth.unknown_user"))',
    'HTTPException(400, "当前密码错误")':
        'HTTPException(400, tr(request, "err.auth.wrong_current_pwd"))',
    '"detail": "密码至少 6 位"': '"detail": tr(request, "su_js_003")',
    '"detail": f"用户名 \'{username}\' 已存在或角色无效"':
        '"detail": tr(request, "err.auth.user_exists_or_bad_role", username=username)',
    'HTTPException(400, "不能踢出自己的当前会话")':
        'HTTPException(400, tr(request, "err.auth.cannot_kick_self"))',
}

# ── settings_routes.py ──（f-string 参数化收敛）
SETTINGS_MAP = {
    'HTTPException(400, "section 和 fields 不能为空")':
        'HTTPException(400, tr(request, "err.set.section_fields_required"))',
    'HTTPException(400, f"不允许修改 section: {section}")':
        'HTTPException(400, tr(request, "err.set.section_forbidden", section=section))',
    'HTTPException(400, f"agents_json 格式错误: {e}")':
        'HTTPException(400, tr(request, "err.set.json_parse_error", field="agents_json", err=e))',
    'HTTPException(400, "agents_json 必须是 JSON 数组")':
        'HTTPException(400, tr(request, "err.set.json_must_be_array", field="agents_json"))',
    'HTTPException(400, f"work_hours_json 格式错误: {e}")':
        'HTTPException(400, tr(request, "err.set.json_parse_error", field="work_hours_json", err=e))',
    'HTTPException(400, "work_hours_json 必须是 JSON 对象")':
        'HTTPException(400, tr(request, "err.set.json_must_be_object", field="work_hours_json"))',
    'HTTPException(400, f"work_exceptions_json 格式错误: {e}")':
        'HTTPException(400, tr(request, "err.set.json_parse_error", field="work_exceptions_json", err=e))',
    'HTTPException(400, "work_exceptions_json 必须是 JSON 对象")':
        'HTTPException(400, tr(request, "err.set.json_must_be_object", field="work_exceptions_json"))',
    'HTTPException(400, f"agent_teams_json 格式错误: {e}")':
        'HTTPException(400, tr(request, "err.set.json_parse_error", field="agent_teams_json", err=e))',
    'HTTPException(400, "agent_teams_json 必须是 JSON 数组")':
        'HTTPException(400, tr(request, "err.set.json_must_be_array", field="agent_teams_json"))',
    'HTTPException(500, f"配置保存失败: {e}")':
        'HTTPException(500, tr(request, "err.set.save_config_failed", err=e))',
    'HTTPException(400, "keywords 必须是 {intent: [kw1, kw2, ...]} 格式")':
        'HTTPException(400, tr(request, "err.set.keywords_format"))',
    'HTTPException(500, f"保存失败: {e}")':
        'HTTPException(500, tr(request, "err.set.save_failed", err=e))',
    'HTTPException(400, "text 不能为空")':
        'HTTPException(400, tr(request, "err.set.text_required"))',
    'HTTPException(400, "未配置 Webhook URL")':
        'HTTPException(400, tr(request, "err.set.no_webhook_url"))',
}


def main() -> int:
    total_unmatched = 0
    for fname, mapping in (("auth_user_routes.py", AUTH_MAP),
                           ("settings_routes.py", SETTINGS_MAP)):
        res = convert_file(_ROUTES / fname, mapping)
        unmatched = res["unmatched"]
        total_unmatched += len(unmatched)
        print(f"[{fname}] replaced={res['total_replaced']} "
              f"import_added={res['import_added']} unmatched={len(unmatched)}")
        for u in unmatched:
            print(f"    MISS: {u}")
    return 1 if total_unmatched else 0


if __name__ == "__main__":
    raise SystemExit(main())
