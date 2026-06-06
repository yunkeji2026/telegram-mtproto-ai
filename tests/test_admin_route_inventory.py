"""admin.py 路由清单快照（Phase E1 重构安全网）。

冻结基线：admin app 在重构期间必须保持「所有基线端点仍注册」。
- 任何拆分/搬迁导致端点丢失 → 本测试失败。
- 新增端点不会破坏（只断言基线是实际清单的子集）。

基线由 2026-06-01 admin.py（6819 行，拆分前）抓取，total=457。
若刻意删除/改名端点，请同步更新本基线并在 PR 说明。
"""

from starlette.routing import Route

# 基线端点（path<TAB>methods，methods 为逗号分隔且不含 HEAD/OPTIONS）。
_BASELINE = """
/	GET
/admin/tts-dashboard	GET
/ai-studio	GET
/analytics	GET
/api/ab-tests/evaluate	GET
/api/ab-tests/{intent}	PUT
/api/activity-stats	GET
/api/admin/tts-cleanup	POST
/api/admin/tts-stats	GET
/api/ai-studio/summary	GET
/api/ai/quality	GET
/api/alert-status	GET
/api/analytics	GET
/api/apply-param-suggestion	POST
/api/audit	GET
/api/audit/activity	GET
/api/autopilot	PUT
/api/autopilot-status	GET
/api/batch-channels	POST
/api/batch-strategies	POST
/api/batch-templates	POST
/api/bot-metrics	GET
/api/cases/active	GET
/api/cases/{case_id}/close	POST
/api/cases/{case_id}/note	POST
/api/change-password	POST
/api/channels	GET
/api/channels/{channel}	PUT
/api/chat/test	POST
/api/chat/test/correct	POST
/api/config/summary	GET
/api/conversations/active	GET
/api/copilot/query	POST
/api/data-purge	POST
/api/episodic-memory	GET
/api/episodic-memory/backfill	POST
/api/episodic-memory/{row_id}	DELETE
/api/events	GET
/api/export-strategy-events	GET
/api/health-check	GET
/api/human-escalation/mention-round-robin	GET
/api/human-escalation/schedule-status	GET
/api/human-escalation/shift	GET
/api/human-escalation/shift	POST
/api/human-escalation/verify	GET
/api/identity	GET
/api/identity/link	POST
/api/identity/unlink	POST
/api/kb/accept-suggestion	POST
/api/kb/ai-generate	POST
/api/kb/auto-suggestions	GET
/api/kb/backup	POST
/api/kb/backups	GET
/api/kb/category-stats	GET
/api/kb/check-channel-conflict	POST
/api/kb/check-conflict	POST
/api/kb/check-trigger-overlaps	POST
/api/kb/duplicates	GET
/api/kb/embed-all	POST
/api/kb/embed-coverage	GET
/api/kb/embed-progress	GET
/api/kb/embed-stats	GET
/api/kb/entries	GET
/api/kb/entries	POST
/api/kb/entries/batch-update	POST
/api/kb/entries/bulk-disable	POST
/api/kb/entries/{entry_id}	DELETE
/api/kb/entries/{entry_id}	GET
/api/kb/entries/{entry_id}	PUT
/api/kb/entries/{entry_id}/auto-translate	POST
/api/kb/entries/{entry_id}/embed	POST
/api/kb/entries/{entry_id}/images	GET
/api/kb/entries/{entry_id}/images	POST
/api/kb/entries/{entry_id}/translate	POST
/api/kb/entries/{entry_id}/versions	GET
/api/kb/error-codes	GET
/api/kb/error-codes	POST
/api/kb/error-codes/{ec_id}	DELETE
/api/kb/error-codes/{ec_id}	PUT
/api/kb/evolve-sweep	POST
/api/kb/examples	GET
/api/kb/examples	POST
/api/kb/examples/{ex_id}	DELETE
/api/kb/export	GET
/api/kb/export-csv	GET
/api/kb/export-markdown	GET
/api/kb/feedback	GET
/api/kb/feedback	POST
/api/kb/feedback/{fb_id}/promote	POST
/api/kb/health-stats	GET
/api/kb/images/{img_id}	DELETE
/api/kb/implicit-feedback	POST
/api/kb/import	POST
/api/kb/import-csv	POST
/api/kb/import/save	POST
/api/kb/maintenance-advice	GET
/api/kb/miss-log	DELETE
/api/kb/miss-log	POST
/api/kb/miss-to-entry	POST
/api/kb/query-analytics	GET
/api/kb/reply-quality	GET
/api/kb/report	GET
/api/kb/restore/{filename}	POST
/api/kb/rules	GET
/api/kb/rules	POST
/api/kb/rules/{rule_id}	DELETE
/api/kb/sandbox	POST
/api/kb/sandbox/ai-reply	POST
/api/kb/sandbox/save-example	POST
/api/kb/seed	POST
/api/kb/self-heal	POST
/api/kb/stale	GET
/api/kb/stats	GET
/api/kb/today-hit-rate	GET
/api/kb/translate-all	POST
/api/kb/translate-progress	GET
/api/kb/translate-sweep	POST
/api/kb/translation-gaps	GET
/api/kb/translations/pending	GET
/api/kb/translations/{trans_id}	PUT
/api/kb/translations/{trans_id}/confirm	POST
/api/kb/translations/{trans_id}/retranslate	POST
/api/kb/usage-ranking	GET
/api/kb/versions/{version_id}	GET
/api/kb/versions/{version_id}/restore	POST
/api/learner/drafts	GET
/api/learner/drafts/approve-all	POST
/api/learner/drafts/batch-action	POST
/api/learner/drafts/{draft_id}	GET
/api/learner/drafts/{draft_id}	PUT
/api/learner/drafts/{draft_id}/approve	POST
/api/learner/drafts/{draft_id}/recheck-dup	POST
/api/learner/drafts/{draft_id}/reject	POST
/api/learner/run	POST
/api/learner/stats	GET
/api/line-rpa/accept-friends	POST
/api/line-rpa/alerts	GET
/api/line-rpa/alerts/ack_all	POST
/api/line-rpa/alerts/{alert_id}/ack	POST
/api/line-rpa/audit	GET
/api/line-rpa/chat-history/{chat_key:path}	GET
/api/line-rpa/chat-lang-lock	POST
/api/line-rpa/chats	GET
/api/line-rpa/config	GET
/api/line-rpa/config	PUT
/api/line-rpa/customer-profile/{chat_key:path}	GET
/api/line-rpa/device-screenshot	GET
/api/line-rpa/intent-stats	GET
/api/line-rpa/log-tail	GET
/api/line-rpa/metrics	GET
/api/line-rpa/notifications	GET
/api/line-rpa/pause	POST
/api/line-rpa/pending	GET
/api/line-rpa/pending-tts	GET
/api/line-rpa/pending/cancel-all	POST
/api/line-rpa/pending/{pending_id}/resolve	POST
/api/line-rpa/pending/{pending_id}/retry-tts	POST
/api/line-rpa/recent	GET
/api/line-rpa/resume	POST
/api/line-rpa/screenshot/{name}	GET
/api/line-rpa/search	GET
/api/line-rpa/send-manual	POST
/api/line-rpa/send-queue	GET
/api/line-rpa/send-queue/{item_id}	GET
/api/line-rpa/send-queue/{item_id}/cancel	POST
/api/line-rpa/sessions/{chat_key:path}	GET
/api/line-rpa/status	GET
/api/line-rpa/timeline	GET
/api/line-rpa/trigger	POST
/api/messenger-rpa/accounts	GET
/api/messenger-rpa/accounts/health	GET
/api/messenger-rpa/accounts/{account_id}/chats/emergency_stop	DELETE
/api/messenger-rpa/accounts/{account_id}/chats/emergency_stop	POST
/api/messenger-rpa/accounts/{account_id}/chats/skipped	GET
/api/messenger-rpa/accounts/{account_id}/clear-unsafe	POST
/api/messenger-rpa/accounts/{account_id}/pause	POST
/api/messenger-rpa/accounts/{account_id}/resume	POST
/api/messenger-rpa/accounts/{account_id}/send-to	POST
/api/messenger-rpa/accounts/{account_id}/trigger	POST
/api/messenger-rpa/approvals	GET
/api/messenger-rpa/approvals/batch	POST
/api/messenger-rpa/approvals/{approval_id}	GET
/api/messenger-rpa/approvals/{approval_id}/approve	POST
/api/messenger-rpa/approvals/{approval_id}/reject	POST
/api/messenger-rpa/approvals/{approval_id}/suggest	POST
/api/messenger-rpa/approvals/{approval_id}/update	POST
/api/messenger-rpa/bindings	GET
/api/messenger-rpa/bindings	PUT
/api/messenger-rpa/calibrate	POST
/api/messenger-rpa/chat-history/{chat_key:path}	GET
/api/messenger-rpa/chat-lang-lock	POST
/api/messenger-rpa/chat-persona-bindings	GET
/api/messenger-rpa/chat-persona-bindings/batch	POST
/api/messenger-rpa/chat-persona-bindings/{chat_name}	DELETE
/api/messenger-rpa/chat-persona-bindings/{chat_name}	PUT
/api/messenger-rpa/chat/history	GET
/api/messenger-rpa/config	GET
/api/messenger-rpa/config	PUT
/api/messenger-rpa/coordinator	GET
/api/messenger-rpa/credits	GET
/api/messenger-rpa/credits/{chat_key}/reset	POST
/api/messenger-rpa/customer-profile/{chat_key:path}	GET
/api/messenger-rpa/devices	GET
/api/messenger-rpa/funnel	GET
/api/messenger-rpa/hint-metrics	GET
/api/messenger-rpa/install-adbkeyboard	POST
/api/messenger-rpa/intent-stats	GET
/api/messenger-rpa/leads	GET
/api/messenger-rpa/leads/{chat_key:path}	GET
/api/messenger-rpa/leads/{chat_key:path}/handoff	PUT
/api/messenger-rpa/llm-cost	GET
/api/messenger-rpa/media	GET
/api/messenger-rpa/media	PUT
/api/messenger-rpa/media/asr-test	POST
/api/messenger-rpa/media/tts-test	POST
/api/messenger-rpa/metrics	GET
/api/messenger-rpa/mobile-auto/cluster/devices/{device_id}/screenshot	GET
/api/messenger-rpa/mobile-auto/devices/{device_id}/action	POST
/api/messenger-rpa/mobile-auto/devices/{device_id}/screenshot	GET
/api/messenger-rpa/mobile-auto/status	GET
/api/messenger-rpa/pause	POST
/api/messenger-rpa/personas	GET
/api/messenger-rpa/personas	PUT
/api/messenger-rpa/recent	GET
/api/messenger-rpa/replays	GET
/api/messenger-rpa/replays/rerun	POST
/api/messenger-rpa/resume	POST
/api/messenger-rpa/search	GET
/api/messenger-rpa/send-manual	POST
/api/messenger-rpa/send-queue	GET
/api/messenger-rpa/send-queue/{item_id}	GET
/api/messenger-rpa/send-queue/{item_id}/cancel	POST
/api/messenger-rpa/sessions/{chat_key:path}	GET
/api/messenger-rpa/status	GET
/api/messenger-rpa/strategy/accounts/{account_id}	PATCH
/api/messenger-rpa/strategy/audit/{audit_id}/rollback	POST
/api/messenger-rpa/strategy/conversations/{customer_id:path}	PATCH
/api/messenger-rpa/strategy/jobs/{job_id}/{action}	POST
/api/messenger-rpa/strategy/personas	POST
/api/messenger-rpa/strategy/personas/{persona_id}	PATCH
/api/messenger-rpa/strategy/personas/{persona_id}/{action}	POST
/api/messenger-rpa/strategy/runtime	GET
/api/messenger-rpa/strategy/simulate	POST
/api/messenger-rpa/templates	GET
/api/messenger-rpa/trigger	POST
/api/messenger-rpa/variants/stats	GET
/api/migrate	POST
/api/model-summary	GET
/api/notifications	GET
/api/persona	GET
/api/persona/bind	POST
/api/persona/bindings	GET
/api/persona/global-rules	GET
/api/persona/global-rules	PUT
/api/persona/global-rules/backups	GET
/api/persona/global-rules/preview	POST
/api/persona/global-rules/restore/{slot}	POST
/api/persona/preview-prompt	GET
/api/persona/unbind	POST
/api/persona/update-default	POST
/api/personas/bulk-bind	POST
/api/personas/list	GET
/api/personas/profiles	GET
/api/personas/profiles/export	GET
/api/personas/profiles/import	POST
/api/personas/profiles/reload	POST
/api/personas/profiles/{profile_id}	DELETE
/api/personas/profiles/{profile_id}	GET
/api/personas/profiles/{profile_id}	PUT
/api/personas/profiles/{profile_id}/bindings	GET
/api/personas/profiles/{profile_id}/diff-canonical	GET
/api/personas/profiles/{profile_id}/history	GET
/api/personas/profiles/{profile_id}/promote	POST
/api/personas/profiles/{profile_id}/prompt-preview	GET
/api/personas/profiles/{profile_id}/revert	POST
/api/personas/status	GET
/api/personas/sync-to-config	POST
/api/personas/wa-account/{account_id}/assign-profile	POST
/api/reactivation/dry-run-feedback	POST
/api/reactivation/dry-run-samples	GET
/api/registry/apply-template	POST
/api/registry/batch	POST
/api/registry/export	GET
/api/registry/templates	GET
/api/reply-logic	GET
/api/reply-logic	POST
/api/report/daily	GET
/api/report/weekly	GET
/api/rollback	POST
/api/rpa-overview/alerts	GET
/api/rpa-overview/control	POST
/api/rpa-overview/device-stats	GET
/api/rpa-overview/device-stats/{serial}	GET
/api/rpa-overview/devices	GET
/api/rpa-overview/events	GET
/api/rpa-overview/lang-dist	GET
/api/rpa-overview/lang-dist-version	GET
/api/rpa-overview/lang-trend	GET
/api/rpa-overview/load-balance	GET
/api/rpa-overview/pending	GET
/api/rpa-overview/registry	GET
/api/rpa-overview/registry	POST
/api/rpa-overview/registry/{serial}/auto-detect	POST
/api/rpa-overview/status	GET
/api/rpa/cross-platform-profile	GET
/api/rpa/global-search	GET
/api/rpa/intent-tags	GET
/api/rpa/intent-tags	POST
/api/rpa/intent-tags/backups	GET
/api/rpa/intent-tags/diff	POST
/api/rpa/intent-tags/raw	GET
/api/rpa/intent-tags/reload	POST
/api/rpa/intent-tags/restore	POST
/api/rpa/metrics	GET
/api/session-stats	GET
/api/sessions	GET
/api/sessions/revoke-all	POST
/api/sessions/{jti}/revoke	POST
/api/settings/intent-keywords	GET
/api/settings/intent-keywords	PUT
/api/settings/save	POST
/api/settings/test-intent	POST
/api/settings/test-webhook	GET
/api/setup	POST
/api/setup/test-ai	POST
/api/snapshots	GET
/api/strategies	GET
/api/strategies/mapping	PUT
/api/strategies/{strategy_id}	PUT
/api/strategy-analytics	GET
/api/strategy-analytics/compare	GET
/api/strategy-analytics/{strategy_id}/hourly	GET
/api/strategy-history/{strategy_id}	GET
/api/system-info	GET
/api/telegram/account-info	GET
/api/telegram/config-export	GET
/api/telegram/config-restore	POST
/api/telegram/config-snapshots	GET
/api/telegram/health	GET
/api/telegram/log-stream	GET
/api/telegram/log-tail	GET
/api/telegram/recent-contacts	GET
/api/telegram/settings	GET
/api/telegram/settings/reply-logic	PUT
/api/telegram/settings/voice-asr	PUT
/api/telegram/settings/voice-reply	PUT
/api/telegram/upload-voice	POST
/api/telegram/voice-files	GET
/api/telegram/voice-quality	GET
/api/telegram/voice-sample/{filename}	GET
/api/templates	GET
/api/templates/{key}	PUT
/api/trigger-decisions	GET
/api/unified-inbox/analyze	POST
/api/unified-inbox/automation	GET
/api/unified-inbox/automation	POST
/api/unified-inbox/chats	GET
/api/unified-inbox/history	GET
/api/unified-inbox/kb-search	GET
/api/unified-inbox/profile	GET
/api/unified-inbox/templates	GET
/api/unified-inbox/send	POST
/api/unified-inbox/stored-chats	GET
/api/unified-inbox/thread	GET
/api/unified-inbox/translate	POST
/api/workspace/claim	POST
/api/workspace/claim/release	POST
/api/workspace/claim/renew	POST
/api/workspace/claims	GET
/api/workspace/contact/{contact_id}	GET
/api/workspace/contact/{contact_id}/crm	POST
/api/workspace/contact/{contact_id}/follow-up	POST
/api/workspace/contact/{contact_id}/tasks	GET
/api/workspace/contacts/export.csv	GET
/api/workspace/contacts/list	GET
/api/workspace/contacts/merge	POST
/api/workspace/contacts/merge-contact	POST
/api/workspace/contacts/overview	GET
/api/workspace/contacts/search	GET
/api/workspace/contacts/split	POST
/api/workspace/agent-frt-detail	GET
/api/workspace/daily-report.csv	GET
/api/workspace/dashboard	GET
/api/workspace/agent-perf	GET
/api/workspace/agent-perf/timeline	GET
/api/workspace/escalation-log	GET
/api/workspace/escalation/{esc_id}/assign	POST
/api/workspace/escalations	GET
/api/workspace/escalations/mine	GET
/api/workspace/follow-up/{task_id}/assign	POST
/api/workspace/follow-up/{task_id}/done	POST
/api/workspace/follow-up/{task_id}/snooze	POST
/api/workspace/follow-ups	GET
/api/workspace/heartbeat	POST
/api/workspace/me	GET
/api/workspace/my-tasks	GET
/api/workspace/merge-reviews	GET
/api/workspace/merge-reviews/{review_id}	POST
/api/workspace/metrics/web-funnel	GET
/api/workspace/presence	GET
/api/workspace/presence	POST
/api/workspace/prefs	GET
/api/workspace/prefs	POST
/api/workspace/sla-alerts	GET
/api/workspace/sla-detail	GET
/api/workspace/sla/create-task	POST
/api/workspace/stream	GET
/api/workspace/tag-library	GET
/api/workspace/tag-library	POST
/api/workspace/tag-library/{tag}	DELETE
/api/workspace/tags	GET
/api/user-segments	GET
/api/users/at-risk	GET
/api/vision-stats	GET
/api/voice/tts-file/{filename}	GET
/api/voice/tts-test	POST
/api/voice/tts-test/{filename}	GET
/api/webhook-settings	GET
/api/webhook-settings	PUT
/api/webhook-test	POST
/api/whatsapp-rpa/accept-contacts	POST
/api/whatsapp-rpa/alerts	GET
/api/whatsapp-rpa/alerts/ack_all	POST
/api/whatsapp-rpa/alerts/{alert_id}/ack	POST
/api/whatsapp-rpa/chat-blacklist	POST
/api/whatsapp-rpa/chat-history	GET
/api/whatsapp-rpa/chat-history/{chat_key:path}	GET
/api/whatsapp-rpa/chat-lang-lock	POST
/api/whatsapp-rpa/chat-quiet	POST
/api/whatsapp-rpa/config	GET
/api/whatsapp-rpa/config	PUT
/api/whatsapp-rpa/conversations	GET
/api/whatsapp-rpa/customer-profile/{chat_key:path}	GET
/api/whatsapp-rpa/device-screenshot	GET
/api/whatsapp-rpa/intent-stats	GET
/api/whatsapp-rpa/log-tail	GET
/api/whatsapp-rpa/media-metrics	GET
/api/whatsapp-rpa/pause	POST
/api/whatsapp-rpa/pending	GET
/api/whatsapp-rpa/pending-tts	GET
/api/whatsapp-rpa/pending/cancel-all	POST
/api/whatsapp-rpa/pending/{pending_id}/resolve	POST
/api/whatsapp-rpa/pending/{pending_id}/retry-tts	POST
/api/whatsapp-rpa/pipeline-metrics	GET
/api/whatsapp-rpa/proactive-metrics	GET
/api/whatsapp-rpa/proactive-stats	GET
/api/whatsapp-rpa/recent	GET
/api/whatsapp-rpa/reset-circuit-breaker	POST
/api/whatsapp-rpa/resume	POST
/api/whatsapp-rpa/search	GET
/api/whatsapp-rpa/send-manual	POST
/api/whatsapp-rpa/send-queue	GET
/api/whatsapp-rpa/send-queue/{item_id}	GET
/api/whatsapp-rpa/send-queue/{item_id}/cancel	POST
/api/whatsapp-rpa/sessions/{chat_key:path}	GET
/api/whatsapp-rpa/status	GET
/api/whatsapp-rpa/template-analytics	GET
/api/whatsapp-rpa/timeline	GET
/api/whatsapp-rpa/trigger	POST
/api/whatsapp-rpa/tts-test	POST
/api/whatsapp-rpa/voice-metrics	GET
/audit	GET
/audit/export	GET
/cases	GET
/channels	GET
/channels/update	POST
/developer	GET
/developer/auth	POST
/developer/logout	POST
/diff	GET
/episodic-memory	GET
/episodic_memory	GET
/export	GET
/health	GET
/help	GET
/import	GET
/import	POST
/kb-images/{filename}	GET
/knowledge	GET
/learner	GET
/line-rpa	GET
/login	GET
/login	POST
/logout	GET
/logs	GET
/logs/stream	GET
/messenger-rpa	GET
/openapi.json	GET
/personas	GET
/rpa-overview	GET
/set_lang	GET
/set_ui_mode	GET
/settings	GET
/setup	GET
/strategies	GET
/strategy-analytics	GET
/telegram	GET
/templates	GET
/templates/update	POST
/training	GET
/unified-inbox	GET
/workspace	GET
/workspace/agent-perf	GET
/workspace/contact/{contact_id}	GET
/workspace/contacts	GET
/workspace/dash	GET
/api/reply-templates	GET
/api/workspace/export	GET
/api/workspace/metrics	GET
/api/workspace/report	GET
/api/workspace/broadcast	POST
/api/workspace/leaderboard	GET
/api/reply-templates	POST
/api/reply-templates/{template_id}	DELETE
/api/reply-templates/{template_id}	PUT
/api/reply-templates/{template_id}/use	POST
/api/unified-inbox/conv-meta	GET
/api/unified-inbox/contact-profile	GET
/workspace/draft-audit	GET
/workspace/drafts	GET
/workspace/templates	GET
/workspace/escalations	GET
/workspace/tasks	GET
/users	GET
/users/create	POST
/users/delete/{user_id}	POST
/users/update/{user_id}	POST
/whatsapp-rpa	GET
"""


def _parse_baseline():
    expected = set()
    for line in _BASELINE.strip().splitlines():
        if "\t" not in line:
            continue
        path, methods = line.split("\t", 1)
        for m in methods.split(","):
            m = m.strip()
            if m:
                expected.add((path.strip(), m))
    return expected


EXPECTED_ROUTES = _parse_baseline()


def _live_routes(app):
    live = set()
    for r in app.routes:
        path = getattr(r, "path", None)
        if not path:
            continue
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((path, m))
    return live


def test_baseline_parsed_nontrivial():
    # 防止基线被误清空
    assert len(EXPECTED_ROUTES) >= 450


def test_all_baseline_routes_still_registered(app):
    live = _live_routes(app)
    missing = sorted(EXPECTED_ROUTES - live)
    assert not missing, (
        f"重构丢失了 {len(missing)} 个端点（admin.py 拆分安全网）：\n"
        + "\n".join(f"  {p} [{m}]" for p, m in missing)
    )


def test_no_unexpected_extra_routes(app):
    """精确相等：除基线外不应出现新端点（拆分期间应只搬迁、不新增/改名）。"""
    live = _live_routes(app)
    extra = sorted(live - EXPECTED_ROUTES)
    assert not extra, (
        f"出现 {len(extra)} 个基线外端点（拆分应只搬迁；如确为有意新增，请更新基线）：\n"
        + "\n".join(f"  {p} [{m}]" for p, m in extra)
    )


# 已知的历史重复注册（pre-existing，非拆分引入）。Starlette 用首个匹配，
# 后注册的被遮蔽。列入白名单，使本测试能抓出「新增」的重复注册。
_KNOWN_DUPLICATE_ROUTES = {
    # /api/kb/import 同 path 注册两次但语义不同：kb_routes(export-dump 导入，生效) 与
    # admin.py inline(KBImporter 文档导入，被遮蔽)。属待产品决策的遗留 bug（需改名才能并存），
    # 暂保留白名单。详见 admin.py「KB Import API」处注释。
    ("/api/kb/import", "POST"),
    # 注：persona 6 端点的 inline 重复已在 Phase E1 清理（删 inline 死代码，
    # 统一由 register_persona_routes 提供），不再是重复注册。
}


def test_no_new_duplicate_route_registrations(app):
    """检测重复注册的 (path, method)。已知历史重复白名单豁免，新增重复则失败。"""
    from collections import Counter

    counts = Counter()
    for r in app.routes:
        path = getattr(r, "path", None)
        if not path:
            continue
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            counts[(path, m)] += 1
    dups = {k for k, c in counts.items() if c > 1}
    new_dups = sorted(dups - _KNOWN_DUPLICATE_ROUTES)
    assert not new_dups, (
        f"出现 {len(new_dups)} 个新的重复注册端点：\n"
        + "\n".join(f"  {p} [{m}]" for p, m in new_dups)
    )
