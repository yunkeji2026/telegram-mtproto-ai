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
/admin/ops	GET
/admin/tts-dashboard	GET
/ai-studio	GET
/analytics	GET
/api/ab-tests/evaluate	GET
/api/ab-tests/{intent}	PUT
/api/activity-stats	GET
/api/admin/branding	GET
/api/admin/branding	POST
/api/admin/demo	GET
/api/admin/demo/clear	POST
/api/admin/demo/seed	POST
/api/admin/health	GET
/api/admin/incidents	GET
/api/admin/incidents/{incident_id}/ack	POST
/api/admin/health/recheck	POST
/api/admin/ops-overview	GET
/api/admin/ops-report	GET
/api/admin/workers/{worker_id}/reset-circuit	POST
/api/admin/reliability	GET
/api/admin/license	GET
/api/admin/license/reload	POST
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
/api/care/schedule	GET
/api/care/schedule	POST
/api/care/schedule/due	GET
/api/care/schedule/{sid}/cancel	POST
/api/care/schedule/{sid}/send-now	POST
/api/care/dry-run-samples	GET
/api/care/dry-run-feedback	POST
/api/deferred-outbox/status	GET
/api/deferred-outbox/retry	POST
/api/deferred-outbox/cancel	POST
/api/deferred-outbox/pause	POST
/api/deferred-outbox/resume	POST
/api/monetize/overview	GET
/api/monetize/catalog	GET
/api/monetize/entitlement	GET
/api/monetize/retention	GET
/api/monetize/teaser-funnel	GET
/api/monetize/feature-check	POST
/api/monetize/grant	POST
/api/monetize/webhook	POST
/api/monetize/checkout	POST
/api/monetize/webhook/stripe	POST
/api/monetize/webhook/telegram	POST
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
/api/crisis-events	GET
/api/crisis-events/{event_id}/handle	POST
/api/data-purge	POST
/api/episodic-memory	GET
/api/episodic-memory/backfill	POST
/api/episodic-memory/correction-stats	GET
/api/episodic-memory/{row_id}	DELETE
/api/episodic-memory/{row_id}/confirm	POST
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
/api/kb/cold-start	GET
/api/kb/seed-pack	POST
/api/kb/improvements	GET
/api/kb/improvements/convert	POST
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
/api/ops/kill-switch	GET
/api/ops/kill-switch	POST
/api/ops/kill-switch	DELETE
/api/ops/canary	GET
/api/ops/canary	POST
/api/ops/canary	DELETE
/api/personas/bulk-bind	POST
/api/personas/list	GET
/api/personas/mrpa-account/{account_id}/assign-profile	POST
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
/api/personas/tg-account/{account_id}/assign-profile	POST
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
/api/platforms/{platform}/modes	GET
/api/platforms/{platform}/login/start	POST
/api/platforms/{platform}/login/{login_id}/status	GET
/api/platforms/{platform}/login/{login_id}/cancel	POST
/api/proxies	GET
/api/proxies	POST
/api/proxies/{proxy_id}	DELETE
/api/proxies/{proxy_id}/test	POST
/api/fingerprints	GET
/api/fingerprints/generate	POST
/api/accounts	GET
/api/accounts/orchestrator	GET
/api/accounts/fleet-health	GET
/api/accounts/orchestrator/sync	POST
/api/accounts/{platform}/{account_id}/start	POST
/api/accounts/{platform}/{account_id}/stop	POST
/api/accounts/{platform}/{account_id}/restart	POST
/api/accounts/{platform}/{account_id}/label	POST
/api/accounts/{platform}/{account_id}/auto-reply	POST
/api/accounts/{platform}/{account_id}/auto-reply/override	POST
/api/accounts/auto-reply/audit	GET
/api/accounts/auto-reply/config	GET
/api/accounts/auto-reply/config	POST
/api/accounts/auto-reply/health	GET
/api/accounts/auto-reply/webhooks	GET
/api/accounts/auto-reply/webhooks	POST
/api/accounts/auto-reply/webhooks/test	POST
/api/accounts/auto-reply/stream	GET
/api/accounts/protocol/readiness	GET
/api/internal/protocol/ingest	POST
/api/unified-inbox/send-media	POST
/api/unified-inbox/send-voice	POST
/api/unified-inbox/send-caps	GET
/api/desktop/smart-reply	POST
/api/desktop/guard-check	POST
/api/desktop/ingest	POST
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
/api/unified-inbox/mark-conversion	POST
/api/unified-inbox/search-messages	GET
/api/unified-inbox/profile	GET
/api/unified-inbox/templates	GET
/api/unified-inbox/send	POST
/api/unified-inbox/stored-chats	GET
/api/unified-inbox/outreach/batch	GET
/api/unified-inbox/outreach/execute	POST
/api/unified-inbox/outreach/preview	POST
/api/unified-inbox/thread	GET
/api/unified-inbox/conv-engine	GET
/api/unified-inbox/conv-engine	POST
/api/unified-inbox/translate	POST
/api/unified-inbox/translate-compare	POST
/api/unified-inbox/translate-document	POST
/api/unified-inbox/translate-document-file	POST
/api/unified-inbox/translate-document-progress/{job_id}	GET
/api/unified-inbox/translated-file/{token}	GET
/api/unified-inbox/translate-image	POST
/api/unified-inbox/translate-message-media	POST
/api/unified-inbox/translate-voice	POST
/api/unified-inbox/translation-engines	GET
/api/workspace/claim	POST
/api/workspace/claim/release	POST
/api/workspace/claim/renew	POST
/api/workspace/claims	GET
/api/workspace/glossary	GET
/api/workspace/glossary	POST
/api/workspace/typing	POST
/api/workspace/contact/{contact_id}	GET
/api/workspace/contact/{contact_id}/crm	POST
/api/workspace/contact/{contact_id}/follow-up	POST
/api/workspace/contact/{contact_id}/tasks	GET
/api/workspace/contact/{contact_id}/timeline	GET
/api/workspace/conv/{conversation_id}/next-actions	GET
/api/workspace/conv/{conversation_id}/execute-action	POST
/api/workspace/conv/{conversation_id}/start-chain	POST
/api/workspace/workflow-actions	GET
/api/workspace/workflow-actions	POST
/api/workspace/workflow-actions/{action_id}	PUT
/api/workspace/workflow-actions/{action_id}	DELETE
/api/workspace/workflow-chains	GET
/api/workspace/workflow-chains	POST
/api/workspace/workflow-chains/{chain_id}	PUT
/api/workspace/workflow-chains/{chain_id}	DELETE
/api/workspace/routing-rules	GET
/api/workspace/routing-rules	POST
/api/workspace/routing-rules/{rule_id}	PUT
/api/workspace/routing-rules/{rule_id}	DELETE
/api/workspace/routing-rules/evaluate	POST
/api/workspace/search	GET
/api/workspace/conv/{conversation_id}/script-suggestions	GET
/api/workspace/script-topics	GET
/api/workspace/script-topics	POST
/api/workspace/script-topics/{topic_id}	PUT
/api/workspace/script-topics/{topic_id}	DELETE
/api/workspace/contact/{contact_id}/engagement	GET
/api/workspace/contact/{contact_id}/engagement	POST
/api/workspace/conv/{conversation_id}/reply-suggest	POST
/api/workspace/conv/{conversation_id}/copilot-prefill	GET
/api/workspace/conv/{conversation_id}/relationship-stage	GET
/api/workspace/conv/{conversation_id}/relationship-stage/confirm	POST
/api/workspace/conv/{conversation_id}/relationship-stage/downgrade	POST
/api/workspace/conv/{conversation_id}/relationship-stage/reunion	POST
/api/workspace/contact/{contact_id}/relationship-stage	GET
/api/workspace/contact/{contact_id}/relationship-stage/sync	POST
/api/workspace/contact/{contact_id}/stage-timeline	GET
/api/workspace/chain-executions	GET
/api/workspace/conv/{conversation_id}/chain-executions	GET
/api/workspace/chain-executions/{exec_id}/cancel	POST
/api/workspace/conv/{conversation_id}/mention-suggestions	GET
/api/workspace/conv/{conversation_id}/collab-context	GET
/api/workspace/contact/{contact_id}/collab-context	GET
/workspace/workflows	GET
/api/workspace/conv/{conversation_id}/qa-score	GET
/api/workspace/conv/{conversation_id}/qa-score	POST
/api/workspace/agent-qa-stats	GET
/api/workspace/churn-risks	GET
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
/api/workspace/roi	GET
/api/workspace/ai-quality	GET
/api/workspace/usage	GET
/api/workspace/billing	GET
/api/setup/channels	GET
/api/setup/channels/{channel}	POST
/api/setup/checklist	GET
/api/setup/companion-preflight	GET
/api/companion/proactive/preview	GET
/api/companion/quality-overview	GET
/api/companion/quality-trend	GET
/api/companion/proactive/sample	POST
/api/companion/proactive/sample/{sid}/rate	POST
/api/companion/proactive/samples	GET
/api/companion/proactive/tuning-advice	GET
/api/workspace/agent-perf	GET
/api/workspace/agent-perf/timeline	GET
/api/workspace/agent-copilot-stats	GET
/api/workspace/escalation-log	GET
/api/workspace/escalation/{esc_id}/assign	POST
/api/workspace/escalations	GET
/api/workspace/escalations/mine	GET
/api/workspace/handoff-brief	GET
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
/api/workspace/conv/{conversation_id}/archive	PATCH
/api/workspace/conv/{conversation_id}/summarize	POST
/api/workspace/conv/{conversation_id}/tags	GET
/api/workspace/conv/{conversation_id}/tags	PUT
/api/workspace/tag-library	GET
/api/workspace/tag-library	POST
/api/workspace/tag-library/{tag}	DELETE
/api/workspace/tag-stats	GET
/api/workspace/tags	GET
/api/workspace/batch/archive	POST
/api/workspace/batch/tags	POST
/api/workspace/batch/assign	POST
/api/workspace/notifications	GET
/api/workspace/notifications/read	POST
/api/workspace/conv/{conversation_id}/notes	GET
/api/workspace/conv/{conversation_id}/notes	POST
/api/workspace/conv/{conversation_id}/notes/{note_id}	PATCH
/api/workspace/conv/{conversation_id}/notes/{note_id}	DELETE
/api/workspace/activity-heatmap	GET
/api/workspace/queue-monitor	GET
/api/workspace/queue-monitor/reassign	POST
/api/workspace/webhook-outbound	GET
/api/workspace/webhook-outbound/test	POST
/api/user-segments	GET
/api/users/at-risk	GET
/api/vision-stats	GET
/api/voice/cloned	GET
/api/voice/enroll	POST
/api/voice/profiles	GET
/api/voice/profiles/{persona_id}	DELETE
/api/voice/purge	POST
/api/voice/purge-orphans	POST
/api/voice/rebind	POST
/api/voice/reconcile	GET
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
/care-schedule	GET
/crisis-audit	GET
/relations-health	GET
/monetization	GET
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
/workspace/queue	GET
/workspace/contact/{contact_id}	GET
/workspace/contacts	GET
/workspace/dash	GET
/api/reply-templates	GET
/api/workspace/export	GET
/api/workspace/metrics	GET
/api/workspace/report	GET
/api/workspace/broadcast	POST
/api/workspace/leaderboard	GET
/api/workspace/trend	GET
/api/workspace/my-perf	GET
/api/workspace/kb-archive	POST
/api/workspace/workspaces	GET
/api/workspace/workspaces	POST
/api/workspace/workspaces/{workspace_id}/stats	GET
/api/workspace/kb-stats	GET
/api/workspace/kb-click	POST
/api/workspace/quality-stats	GET
/api/workspace/workload	GET
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
/workspace/roi	GET
/workspace/setup	GET
/workspace/kb-start	GET
/workspace/golive	GET
/workspace/ai-quality	GET
/workspace/usage	GET
/workspace/tasks	GET
/users	GET
/users/create	POST
/users/delete/{user_id}	POST
/users/update/{user_id}	POST
/whatsapp-rpa	GET
/api/workspace/ab-tests	GET,POST
/api/workspace/ab-tests/{test_id}/results	GET
/api/workspace/ab-tests/{test_id}/stop	POST
/api/workspace/anomaly	GET
/api/workspace/trace	GET
/api/workspace/trace/{trace_id}	GET
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
