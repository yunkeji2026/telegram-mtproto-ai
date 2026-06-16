"""统一收件箱路由编排器 — 挂载各子域 register_*，自身不再定义 handler 或重导出符号。

历史巨石 ``register_unified_inbox_routes`` 经 slice 7–38b 拆分为子模块；
slice 39 起 helpers/services/auth/context/sla/aggregate 请直接从对应源模块 import；
slice 40 起文本+媒体翻译合并为 ``unified_inbox_translate_routes``（30 个子域 register）。
"""

from __future__ import annotations

from src.web.routes.unified_inbox_account_routes import register_account_routes
from src.web.routes.unified_inbox_analyze_routes import register_analyze_routes
from src.web.routes.unified_inbox_aux_read_routes import register_aux_read_routes
from src.web.routes.unified_inbox_batch_notif_routes import register_batch_notif_routes
from src.web.routes.unified_inbox_collab_context_routes import register_collab_context_routes
from src.web.routes.unified_inbox_collab_mention_routes import register_collab_mention_routes
from src.web.routes.unified_inbox_conversion_outreach_routes import (
    register_conversion_outreach_routes,
)
from src.web.routes.unified_inbox_copilot_routes import register_copilot_routes
from src.web.routes.unified_inbox_dashboard_routes import register_workspace_dashboard_routes
from src.web.routes.unified_inbox_desktop_routes import register_desktop_routes
from src.web.routes.unified_inbox_intel_profile_routes import register_intel_profile_routes
from src.web.routes.unified_inbox_login_routes import register_platform_login_routes
from src.web.routes.unified_inbox_proxy_routes import register_proxy_fingerprint_routes
from src.web.routes.unified_inbox_qa_churn_routes import register_qa_churn_routes
from src.web.routes.unified_inbox_quality_routes import register_quality_routes
from src.web.routes.unified_inbox_queue_webhook_routes import register_queue_webhook_routes
from src.web.routes.unified_inbox_read_routes import register_read_routes
from src.web.routes.unified_inbox_realtime_routes import register_realtime_routes
from src.web.routes.unified_inbox_relationship_routes import register_relationship_stage_routes
from src.web.routes.unified_inbox_roi import register_roi_routes
from src.web.routes.unified_inbox_routing_search_routes import register_routing_search_routes
from src.web.routes.unified_inbox_send_routes import register_send_routes
from src.web.routes.unified_inbox_setup_routes import register_setup_routes
from src.web.routes.unified_inbox_usage_routes import register_usage_routes
from src.web.routes.unified_inbox_stored_read_routes import register_stored_read_routes
from src.web.routes.unified_inbox_template_routes import register_template_routes
from src.web.routes.unified_inbox_translate_routes import register_translate_routes
from src.web.routes.unified_inbox_workflow_routes import register_workflow_routes
from src.web.routes.unified_inbox_workspace_contacts_routes import (
    register_workspace_contacts_routes,
)
from src.web.routes.unified_inbox_workspace_escalation_routes import (
    register_workspace_escalation_routes,
)
from src.web.routes.unified_inbox_workspace_pages_routes import register_workspace_pages_routes
from src.web.routes.unified_inbox_workspace_prefs_routes import register_workspace_prefs_routes
from src.web.routes.unified_inbox_workspace_presence_routes import (
    register_workspace_presence_routes,
)
from src.web.routes.unified_inbox_workspace_tags_routes import register_workspace_tags_routes


def register_unified_inbox_routes(
    app,
    *,
    page_auth,
    api_auth,
    templates,
    config_manager=None,
):
    """挂载统一收件箱全部子域路由（纯 orchestrator，无 inline handler）。

    slice 38b：按业务域插入分组注释；**调用顺序与历史巨石一致**（FastAPI 路由匹配 +
    account startup 钩子时序不变）。
    """

    # ── 1. 页面壳（slice 17 + 38a）──────────────────────────────────────
    register_workspace_pages_routes(
        app, page_auth=page_auth, templates=templates, config_manager=config_manager)

    # ── 2. 实时 + 主读路径（slice 36 / 37b）──────────────────────────────
    register_realtime_routes(app, api_auth=api_auth)
    register_read_routes(app, api_auth=api_auth, config_manager=config_manager)

    # ── 3. 账号 / 代理 / 登录（slice 8–10）──────────────────────────────
    register_platform_login_routes(app, api_auth=api_auth, config_manager=config_manager)
    register_setup_routes(app, api_auth=api_auth, config_manager=config_manager)
    register_proxy_fingerprint_routes(app, api_auth=api_auth)
    register_account_routes(app, api_auth=api_auth, config_manager=config_manager)

    # ── 4. 坐席工作台（slice 11–16）────────────────────────────────────
    register_workspace_presence_routes(
        app, api_auth=api_auth, config_manager=config_manager)
    register_workspace_contacts_routes(
        app, api_auth=api_auth, page_auth=page_auth,
        templates=templates, config_manager=config_manager)
    register_workspace_escalation_routes(app, api_auth=api_auth)
    register_workspace_prefs_routes(app, api_auth=api_auth)
    register_workspace_dashboard_routes(
        app, api_auth=api_auth, config_manager=config_manager)
    register_roi_routes(app, api_auth=api_auth, config_manager=config_manager)
    register_quality_routes(app, api_auth=api_auth)
    register_usage_routes(app, api_auth=api_auth)
    register_workspace_tags_routes(app, api_auth=api_auth)

    # ── 5. 辅助读 + 翻译 + 桌面 + 转化 + 分析（slice 37a / 40 / 33 / 32–34）──
    register_aux_read_routes(app, api_auth=api_auth, config_manager=config_manager)
    register_translate_routes(app, api_auth=api_auth)
    register_desktop_routes(app, api_auth=api_auth)
    register_conversion_outreach_routes(app, api_auth=api_auth)
    register_analyze_routes(app, api_auth=api_auth)

    # ── 6. 写路径 + store 读（slice 29–30）──────────────────────────────
    register_stored_read_routes(app, api_auth=api_auth)
    register_send_routes(app, api_auth=api_auth, page_auth=page_auth)

    # ── 7. 协作 / 智能 / 运营（slice 18–28 / 23–27 / 21–22）────────────
    register_intel_profile_routes(app, api_auth=api_auth)
    register_template_routes(app, api_auth=api_auth)
    register_batch_notif_routes(app, api_auth=api_auth)
    register_queue_webhook_routes(app, api_auth=api_auth)
    register_collab_mention_routes(app, api_auth=api_auth, config_manager=config_manager)
    register_collab_context_routes(app, api_auth=api_auth)
    register_relationship_stage_routes(app, api_auth=api_auth)
    register_copilot_routes(app, api_auth=api_auth)
    register_workflow_routes(app, api_auth=api_auth)
    register_routing_search_routes(app, api_auth=api_auth)
    register_qa_churn_routes(app, api_auth=api_auth)
