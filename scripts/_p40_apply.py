"""P40 一次性施工器：收口 episodic_identity + line_rpa + whatsapp_rpa 三族 CJK 响应文案。

亮点（相较 P39 再进一步）：
- 全程走 `draft_map` → `build_draft_map` 施工器：**作用域安全**（只认 HTTPException/detail/error
  上下文，绝不裸字面量全局替换）+ 由施工器统一组装 `tr(...)` 调用；
- RPA 跨平台**参数化收敛**：`{platform} RPA 服务未启动`（LINE 10 + WhatsApp 13 = 23 site）→ 1 键；
  WhatsApp 的 `{op} 失败: {e}`（4 site）→ 1 键；line/whatsapp 共享 ADB/截屏/state_store 词表；
- **跨族复用**：`text 不能为空`→err.set.text_required、`配置保存失败`→err.set.save_config_failed；
- **文案统一**（记录在案）：`chat_key 必填`→统一「chat_key 不能为空」；WhatsApp `保存配置失败`→
  「配置保存失败」；WhatsApp「截屏返回空（ADB 设备未就绪）」→「…可能未就绪」。

curation 用 body→spec：``"key"`` 或 ``("key", {fmt_name: 源码表达式串})``。
内建 AST 校验：每个含 ``tr(request, …)`` 的处理器必须有 ``request`` 形参（防运行时 NameError）。
"""
from __future__ import annotations

import ast
from pathlib import Path

from scripts.i18n_routeconv import build_draft_map, convert_file, draft_map
from src.web.web_i18n import get_translations

ROUTES = Path("src/web/routes")
ZH = get_translations("zh")

EPI = {
    "Bot 未就绪或未注入 SkillManager": "err.epi.bot_not_ready_sm",
    "Bot 未就绪": "err.epi.bot_not_ready",
    "记录不存在或记忆未启用": "err.epi.record_not_found",
    "需要 platform": "err.epi.need_platform",
    "记录不存在、非 AI 推断或记忆未启用": "err.epi.record_not_ai_inferred",
    "情景记忆向量功能未启用（memory.vector.enabled）": "err.epi.vector_disabled",
    "本日情景记忆补全嵌入预算已用尽（memory.vector.daily_embed_budget）": "err.epi.embed_budget_exhausted",
    "情景记忆或 AI 客户端不可用": "err.epi.memory_or_ai_unavailable",
    "CrossPlatformIdentity 未就绪": "err.epi.identity_not_ready",
    "需要 platform_a/uid_a/platform_b/uid_b": "err.epi.need_ab_pairs",
    "需要 platform 和 uid": "err.epi.need_platform_uid",
}

LINE = {
    "LINE RPA 服务未启动": ("err.rpa.service_not_started", {"platform": '"LINE"'}),
    "未找到 ADB 设备（serial 未设置）": "err.rpa.no_adb_device",
    "截屏失败: {e}": ("err.rpa.screenshot_failed", {"err": "e"}),
    "截屏返回空（ADB 设备可能未就绪）": "err.rpa.screenshot_empty",
    "body 必须是对象": "err.rpa.body_must_be_object",
    "不允许的字段: {bad}": ("err.rpa.disallowed_fields", {"bad": "bad"}),
    "保存配置失败: {e}": ("err.set.save_config_failed", {"err": "e"}),
    "chat_key 必填": "err.rpa.chat_key_required",
    "text 不能为空": "err.set.text_required",
    "limit 必须为整数": "err.rpa.limit_must_be_int",
    "send_queue item {item_id} 不存在": ("err.rpa.queue_item_not_found", {"item_id": "item_id"}),
    "item {item_id} 不可取消（不存在或已非 queued 状态）":
        ("err.rpa.queue_item_not_cancelable", {"item_id": "item_id"}),
    "不支持的语言代码: {lang}。支持: {sorted(_VALID_LANGS)}":
        ("err.rpa.lang_unsupported", {"lang": "lang", "langs": "sorted(_VALID_LANGS)"}),
    "state_store 不可用": "err.rpa.state_store_unavailable",
    "写入失败: {e}": ("err.rpa.write_failed", {"err": "e"}),
}

WA = {
    "chat_key 不能为空": "err.rpa.chat_key_required",
    "WhatsApp RPA 服务未初始化": ("err.rpa.service_not_initialized", {"platform": '"WhatsApp"'}),
    "chat_key / peer_name / text 均不能为空": "err.rpa.chat_peer_text_required",
    "消息过长（最大 2000 字）": "err.rpa.message_too_long",
    "WhatsApp RPA 服务未启动": ("err.rpa.service_not_started", {"platform": '"WhatsApp"'}),
    "action 需为 approve/reject/send": "err.rpa.action_invalid",
    "DeviceCoordinatorService 未启动": "err.rpa.device_coordinator_not_started",
    "未找到 ADB 设备（serial 未设置）": "err.rpa.no_adb_device",
    "截屏失败: {e}": ("err.rpa.screenshot_failed", {"err": "e"}),
    "截屏返回空（ADB 设备未就绪）": "err.rpa.screenshot_empty",
    "不允许的字段: {bad}": ("err.rpa.disallowed_fields", {"bad": "bad"}),
    "配置保存失败: {e}": ("err.set.save_config_failed", {"err": "e"}),
    "proactive_stats 失败: {e}": ("err.rpa.op_failed", {"op": '"proactive_stats"', "err": "e"}),
    "proactive_metrics 失败: {e}": ("err.rpa.op_failed", {"op": '"proactive_metrics"', "err": "e"}),
    "set_chat_quiet 失败: {e}": ("err.rpa.op_failed", {"op": '"set_chat_quiet"', "err": "e"}),
    "set_chat_blacklist 失败: {e}": ("err.rpa.op_failed", {"op": '"set_chat_blacklist"', "err": "e"}),
    "不支持的语言代码: {lang}。支持: {sorted(XTTS_SUPPORTED)}":
        ("err.rpa.lang_unsupported", {"lang": "lang", "langs": "sorted(XTTS_SUPPORTED)"}),
    "state_store 不可用": "err.rpa.state_store_unavailable",
    "写入失败: {e}": ("err.rpa.write_failed", {"err": "e"}),
}

_JOBS = (
    ("episodic_identity_routes.py", EPI),
    ("line_rpa_routes.py", LINE),
    ("whatsapp_rpa_routes.py", WA),
)


def _verify_request_scope(text: str) -> list:
    """AST 校验：任一含 tr(request,…) 调用的函数，其（含外层）形参须有 request。"""
    tree = ast.parse(text)
    bad = []

    def _args_of(node):
        a = node.args
        return {x.arg for x in (a.posonlyargs + a.args + a.kwonlyargs)} | \
               ({a.vararg.arg} if a.vararg else set()) | \
               ({a.kwarg.arg} if a.kwarg else set())

    def _walk(node, scope_has_request):
        has_req = scope_has_request
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            has_req = scope_has_request or ("request" in _args_of(node))
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call):
                f = child.func
                if isinstance(f, ast.Name) and f.id == "tr" and child.args:
                    a0 = child.args[0]
                    if isinstance(a0, ast.Name) and a0.id == "request" and not has_req:
                        bad.append(getattr(child, "lineno", -1))
            _walk(child, has_req)

    _walk(tree, False)
    return bad


def main() -> int:
    problems = 0
    for fname, curation in _JOBS:
        path = ROUTES / fname
        text = path.read_text(encoding="utf-8")
        ents = draft_map(text, ZH)
        mapping, pending, fstrings = build_draft_map(ents, lambda e: curation.get(e["body"]))
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
