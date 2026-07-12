"""AIChatAssistant 启动期辅助任务(Stage4,从 main.py 整方法原样迁出,仅 self->assistant)。

主动关怀/复活循环/延迟发件箱/嵌入预热/商业化初始化/情节回填——均由 start() 调度,
side-effect 式装配到 assistant 上,失败各自 try/except 兜底,绝不挡主启动。
"""
from __future__ import annotations

import asyncio
from pathlib import Path


async def maybe_start_proactive_care(assistant, web_app=None) -> None:
    """Phase O：主动关怀引擎（默认关，companion.proactive_care.enabled 开）。

    捕获：入站新消息回调 → 抽取约定入 care_schedule（gated）。
    派发：到期由 CareDispatcher 经 messenger deferred 队列发出（复用 reactivation 护栏）。
    """
    try:
        cfg = ((assistant.config.config.get("companion") or {}).get("proactive_care") or {})
        if not cfg.get("enabled", False):
            assistant.logger.info("proactive_care 未启用（companion.proactive_care.enabled=false）")
            return
        from src.contacts.care_schedule import get_care_schedule_store

        _cfg_dir = Path(assistant.config.config_path).parent
        care_store = get_care_schedule_store(_cfg_dir / "care_schedule.db")
        if web_app is not None:
            web_app.state.care_schedule_store = care_store
        # 启动时清理逾期太久的待办（错过时机不补发）
        try:
            care_store.expire_overdue(grace_days=float(cfg.get("grace_days", 1)))
        except Exception:
            pass

        # 捕获接线：入站新消息 → 抽取入库（gated，复用 inbox 既有回调钩子）
        if assistant.inbox_store is not None and cfg.get("capture", True):
            try:
                from src.contacts.care_capture import make_care_inbound_cb
                assistant.inbox_store.register_new_inbound_cb(
                    make_care_inbound_cb(care_store, assistant.config))
                assistant.logger.info("✅ proactive_care 入站捕获已接线")
            except Exception:
                assistant.logger.warning("proactive_care 捕获接线跳过", exc_info=True)

        # 派发循环：需 messenger deferred 队列（与 reactivation 同款发送）
        if assistant.messenger_rpa_service is None or assistant.ai_client is None:
            assistant.logger.info("proactive_care 派发循环跳过（messenger_rpa/ai 未就绪），仅捕获")
            return
        from src.contacts.care_dispatcher import CareDispatcher

        async def _care_send(channel, account_id, chat_name, reply, defer_until,
                             reason, staleness_sec, extra):
            if channel != "messenger":
                # 非 messenger → 多平台 deferred 队列（关/不可用则返回 0，零破坏）
                return assistant._enqueue_deferred_outbox(
                    channel, account_id, chat_name, reply, defer_until,
                    reason, staleness_sec, extra)
            return await assistant.messenger_rpa_service.enqueue_reactivation_deferred(
                account_id=account_id, chat_name=chat_name, reply_text=reply,
                defer_until=defer_until, defer_reason=reason,
                staleness_sec=staleness_sec, extra=extra)

        def _care_context(contact_key: str) -> str:
            # 最近若干条消息文本作 prompt 可引用要点（best-effort）
            try:
                msgs = assistant.inbox_store.list_messages(contact_key, limit=8) \
                    if assistant.inbox_store else []
                lines = [str(m.get("text") or "").strip() for m in (msgs or [])]
                return "\n".join(t for t in lines if t)[:800]
            except Exception:
                return ""

        ai_name = "她"
        try:
            ai_name = str((assistant.config.get_ai_config() or {}).get("ai_name") or "她")
        except Exception:
            ai_name = "她"

        # K2b：变现配额门控回调（仅当变现 gate 开启才注入；否则 None=不拦，零破坏）
        proactive_paywall = assistant._build_care_paywall(care_store)

        dispatcher = CareDispatcher(
            store=care_store, ai_client=assistant.ai_client, send_callback=_care_send,
            context_provider=_care_context, proactive_allowed=proactive_paywall,
            ai_name=ai_name,
            max_per_tick=int(cfg.get("max_per_tick", 3)),
            interval_sec=float(cfg.get("interval_sec", 600)),
            skip_if_no_context=bool(cfg.get("skip_if_no_context", True)),
            quiet_start_hour=float(cfg.get("quiet_start_hour", 23)),
            quiet_end_hour=float(cfg.get("quiet_end_hour", 8)),
            dry_run=bool(cfg.get("dry_run", False)),
        )
        await dispatcher.start()
        assistant._care_dispatcher = dispatcher
        assistant.logger.info("✅ proactive_care 派发循环已启动（interval=%ss）",
                         cfg.get("interval_sec", 600))
    except Exception as ex:
        assistant.logger.warning("proactive_care 启动跳过: %s", ex)
        assistant.logger.debug("proactive_care 启动异常", exc_info=True)


async def maybe_start_reactivation_loop(assistant) -> None:
    """W2-D4.2/4.3：启动 reactivation 主动唤醒循环（陪护核心）。

    条件：contacts 子系统已启用 + messenger_rpa_service 已起 + 配置 reactivation.enabled
    """
    try:
        cfg_react = (assistant.config.config.get("reactivation") or {})
        if not cfg_react.get("enabled", False):
            assistant.logger.info("reactivation_loop 未启用（reactivation.enabled=false）")
            return
        if assistant.contacts is None or assistant.messenger_rpa_service is None:
            assistant.logger.info(
                "reactivation_loop 跳过（contacts=%s messenger_rpa=%s）",
                assistant.contacts is not None, assistant.messenger_rpa_service is not None,
            )
            return
        from src.contacts.reactivation_loop import ReactivationLoop

        # send_callback：把 reply 入 messenger 的 deferred 队列
        async def _send_to_messenger(channel, account_id, chat_name, reply,
                                     defer_until, reason, staleness_sec, extra):
            if channel != "messenger":
                # 非 messenger → 多平台 deferred 队列（关/不可用则返回 0，零破坏）
                return assistant._enqueue_deferred_outbox(
                    channel, account_id, chat_name, reply, defer_until,
                    reason, staleness_sec, extra)
            return await assistant.messenger_rpa_service.enqueue_reactivation_deferred(
                account_id=account_id,
                chat_name=chat_name,
                reply_text=reply,
                defer_until=defer_until,
                defer_reason=reason,
                staleness_sec=staleness_sec,
                extra=extra,
            )

        # episodic_provider：拿 journey 对象 → 渲染画像 block 给 reactivation prompt
        def _episodic_provider(journey) -> str:
            try:
                if journey is None:
                    return ""
                snap = (getattr(journey, "context_snapshot_json", "") or "").strip()
                if snap:
                    from src.contacts.portrait_extractor import render_block
                    return render_block(snap) or ""
                return ""
            except Exception:
                return ""

        ai_name = ""
        try:
            ai_name = str((assistant.config.get_ai_config() or {}).get("ai_name") or "她")
        except Exception:
            ai_name = "她"

        loop = ReactivationLoop(
            scheduler=assistant.contacts.reactivation,
            store=assistant.contacts.store,
            ai_client=assistant.ai_client,
            send_callback=_send_to_messenger,
            episodic_provider=_episodic_provider,
            ai_name=ai_name,
            max_per_tick=int(cfg_react.get("max_per_tick", 3)),
            interval_sec=float(cfg_react.get("interval_sec", 600)),
            skip_if_no_episodic=bool(cfg_react.get("skip_if_no_episodic", True)),
            dry_run=bool(cfg_react.get("dry_run", False)),
            first_run_grace_minutes=float(
                cfg_react.get("first_run_grace_minutes", 60),
            ),
            first_run_max_per_tick=int(
                cfg_react.get("first_run_max_per_tick", 1),
            ),
            platform_priority=(
                cfg_react.get("platform_priority")
                or ["messenger", "telegram", "line", "whatsapp"]
            ),
        )
        await loop.start()
        assistant._reactivation_loop = loop
        assistant.logger.info(
            "✅ reactivation_loop 已启动（interval=%ss max_per_tick=%s）",
            cfg_react.get("interval_sec", 600), cfg_react.get("max_per_tick", 3),
        )
    except Exception as ex:
        assistant.logger.warning("reactivation_loop 启动跳过: %s", ex)
        assistant.logger.debug("reactivation_loop 启动异常", exc_info=True)


def ensure_deferred_outbox(assistant):
    """惰性建/起多平台 deferred 队列（非 messenger 主动消息走此队列）。

    返回 dispatcher（已 start），或 None（功能关/不可用）。幂等：重复调用复用同一实例。
    sender 用编排器 `orch.send(platform,account,chat_key,text)` 统一投递（编排器已
    路由到对应平台 worker 并回写收件箱出站镜像）；worker 未就绪 → 抛 NotReady 推后重试。
    messenger 不走此队列（保留既有 runner deferred 路径）。
    """
    if assistant._deferred_outbox_dispatcher is not None:
        return assistant._deferred_outbox_dispatcher
    try:
        comp = (assistant.config.config.get("companion") or {})
        cfg = (comp.get("multiplatform_deferred") or {})
        if not cfg.get("enabled", False):
            return None
        from src.integrations.shared.deferred_outbox import (
            DeferredDispatcher, DeferredOutboxStore, DeferredSenderNotReady,
        )

        _cfg_dir = Path(assistant.config.config_path).parent
        store = DeferredOutboxStore(_cfg_dir / "deferred_outbox.db")

        async def _universal_send(account_id, chat_key, text, *, platform):
            # 出站自动翻译（主动触达：care/reactivation 等经 deferred 队列的非 messenger 主动消息）。
            # care 默认按 zh 生成 → 真译成客户语言；reactivation 本就按客户语言生成 → 检测护栏
            # 自动跳过（no-op）。绝不阻塞投递（异常回落原文）。统一受 translate.enabled 开关控。
            text = await assistant._maybe_translate_outbound(
                platform, account_id, chat_key, text)
            # 1) 编排器受管 worker（telegram/whatsapp/line… 任一暴露 send 的）
            try:
                from src.integrations.account_orchestrator import get_orchestrator
                orch = get_orchestrator(assistant.config.config or {})
                if orch.owns(platform, account_id):
                    res = await orch.send(platform, account_id, chat_key, text)
                    return bool((res or {}).get("delivered", True))
            except DeferredSenderNotReady:
                raise
            except Exception:
                assistant.logger.debug("[deferred_outbox] 编排器发送异常 %s:%s",
                                  platform, account_id, exc_info=True)
            # 2) 回落：主 A 线客户端（仅 telegram default）
            if platform == "telegram" and assistant.telegram_client is not None:
                try:
                    target = int(chat_key)
                except (TypeError, ValueError):
                    target = chat_key
                try:
                    return bool(await assistant.telegram_client.send_message(target, text))
                except Exception:
                    assistant.logger.debug("[deferred_outbox] 主客户端发送失败", exc_info=True)
                    return False
            # 3) 该账号此刻无可用 worker → 暂态，推后重试（不丢、不标失败）
            raise DeferredSenderNotReady(f"no worker for {platform}:{account_id}")

        def _make_sender(platform):
            async def _s(account_id, chat_key, text):
                return await _universal_send(account_id, chat_key, text,
                                             platform=platform)
            return _s

        dispatcher = DeferredDispatcher(
            store=store,
            quiet_start_hour=float(cfg.get("quiet_start_hour", 23)),
            quiet_end_hour=float(cfg.get("quiet_end_hour", 8)),
            min_gap_sec=float(cfg.get("min_gap_sec", 45)),
            max_per_tick=int(cfg.get("max_per_tick", 3)),
            interval_sec=float(cfg.get("interval_sec", 120)),
        )
        platforms = cfg.get("platforms") or [
            "telegram", "line", "whatsapp", "instagram", "zalo",
        ]
        for p in platforms:
            dispatcher.register_sender(str(p), _make_sender(str(p)))

        assistant._deferred_outbox_dispatcher = dispatcher
        if assistant._web_app is not None:
            assistant._web_app.state.deferred_outbox_store = store
            assistant._web_app.state.deferred_outbox_dispatcher = dispatcher
        assistant.logger.info(
            "✅ 多平台 deferred 队列已就绪（platforms=%s interval=%ss）",
            platforms, cfg.get("interval_sec", 120))
        return dispatcher
    except Exception:
        assistant.logger.warning("多平台 deferred 队列初始化失败（非 messenger 主动消息将被丢弃）",
                             exc_info=True)
        return None


async def warmup_embeddings(assistant):
    """后台批量向量化无 embedding 的知识库条目"""
    try:
        await asyncio.sleep(5)
        if not assistant.ai_client or not assistant.ai_client.client:
            return
        cfg_dir = (Path(assistant.config.config_path).parent if hasattr(assistant.config, "config_path") else Path("config")).resolve()
        kb_path = (cfg_dir / "knowledge_base.db").resolve()
        if not kb_path.exists():
            assistant.logger.info("向量预热: 知识库文件不存在，跳过 (%s)", kb_path)
            return
        from src.utils.kb_store import KnowledgeBaseStore
        kb = KnowledgeBaseStore(kb_path)
        pending = kb.get_entries_without_embedding()
        if not pending:
            assistant.logger.info("向量预热: 所有条目已向量化 (%d 条)", kb._vindex.count())
            return
        assistant.logger.info("向量预热: 发现 %d 条待向量化条目，开始批量处理...", len(pending))
        batch_size = 20
        done = 0
        for i in range(0, len(pending), batch_size):
            if not assistant.running:
                break
            batch = pending[i:i + batch_size]
            texts = []
            for e in batch:
                parts = [e.get("title", "")]
                trigs = e.get("triggers", "")
                if trigs:
                    try:
                        import json as _j
                        tl = _j.loads(trigs) if isinstance(trigs, str) else trigs
                        if isinstance(tl, list):
                            parts.append(" ".join(tl))
                    except Exception:
                        pass
                for f in ("scenario", "steps", "principles"):
                    if e.get(f):
                        parts.append(e[f][:200])
                texts.append(" ".join(parts)[:500])
            try:
                vecs = await assistant.ai_client.embed_with_fallback(texts)
                if vecs and len(vecs) == len(batch):
                    n_ok = 0
                    for entry, vec in zip(batch, vecs):
                        if not vec:
                            continue
                        kb.set_single_embedding(entry["id"], vec)
                        n_ok += 1
                    done += n_ok
                    assistant.logger.debug("向量预热: 已处理 %d/%d (本批成功 %d)", done, len(pending), n_ok)
                else:
                    assistant.logger.warning(
                        "向量预热: 批次返回数量仍不匹配 (%s vs %s)",
                        len(vecs) if vecs else 0, len(batch),
                    )
            except Exception as e:
                assistant.logger.warning("向量预热: 批次失败: %s", e)
            await asyncio.sleep(1.5)
        cov = kb.embedding_coverage()
        assistant.logger.info("向量预热完成: %d 条新增向量化, 总覆盖率 %s%%", done, cov.get("pct", 0))
    except Exception:
        assistant.logger.exception("向量预热异常")


def maybe_init_monetization(assistant, web_app=None) -> None:
    """Phase K2：C 端变现（默认关，monetization.enabled 开）。

    开启时建 EntitlementStore 单例（落 config/entitlements.db）→ 挂 app.state 供路由用，
    并按 catalog 注入价目；启动可选清理过期订阅。关时不建库（路由会按需懒建只读单例）。
    """
    try:
        mon = (assistant.config.config.get("monetization") or {})
        if not mon.get("enabled", False):
            assistant.logger.info("C 端变现未启用（monetization.enabled=false）")
            return
        from src.utils.entitlement_store import get_entitlement_store
        from src.utils.monetization import merge_catalog

        catalog = merge_catalog(mon.get("catalog"))
        _cfg_dir = Path(assistant.config.config_path).parent
        store = get_entitlement_store(_cfg_dir / "entitlements.db", catalog=catalog)
        if web_app is not None:
            web_app.state.entitlement_store = store
        # Stage 1：把真实权益接进对话路径——注册进程级 resolver，让付费剧情闸
        # （story_engine.require_unlock）据端用户真实拥有判准入。仅在变现就绪时注册，
        # 故未启用时 resolve_entitlement 恒 None → 付费场景仍对所有人锁（零回归）。
        try:
            from src.utils.companion_context import set_relationship_providers
            set_relationship_providers(
                entitlement_resolver=lambda ck: store.get_entitlement(ck))
            assistant.logger.info("✅ 对话剧情付费闸已接入真实权益（entitlement resolver 已注册）")
        except Exception:
            assistant.logger.debug("entitlement resolver 注册失败", exc_info=True)
        if mon.get("expire_on_startup", True):
            try:
                store.expire_subscriptions()
            except Exception:
                pass
        # Stage 3：付费预告转化漏斗埋点库（teaser 发出 → tx_ledger 归因）。
        try:
            from src.utils.companion_funnel_store import (
                get_companion_funnel_store,
            )
            funnel = get_companion_funnel_store(_cfg_dir / "companion_funnel.db")
            assistant._companion_funnel_store = funnel
            if web_app is not None:
                web_app.state.companion_funnel_store = funnel
            assistant.logger.info("✅ 付费预告转化漏斗埋点已就绪")
        except Exception:
            assistant.logger.debug("companion funnel store 初始化跳过", exc_info=True)
        assistant.logger.info("✅ C 端变现已就绪（EntitlementStore 已挂载）")
    except Exception:
        assistant.logger.warning("C 端变现初始化跳过", exc_info=True)


async def episodic_backfill_periodic(assistant):
    """可选：按间隔补全情景记忆缺失向量（memory.vector.backfill_periodic）。"""
    try:
        mcfg = (assistant.config.config or {}).get("memory") or {}
        vcfg = (mcfg.get("vector") or {})
        pcfg = vcfg.get("backfill_periodic") or {}
        if not pcfg.get("enabled", False):
            return
        init_delay = float(pcfg.get("initial_delay_seconds", 1800))
        await asyncio.sleep(max(0.0, init_delay))
    except Exception:
        assistant.logger.exception("情景记忆周期补全初始化失败")
        return

    while assistant.running:
        try:
            mcfg = (assistant.config.config or {}).get("memory") or {}
            vcfg = (mcfg.get("vector") or {})
            pcfg = vcfg.get("backfill_periodic") or {}
            if not pcfg.get("enabled", False):
                await asyncio.sleep(3600)
                continue
            if not vcfg.get("enabled", False):
                await asyncio.sleep(min(3600.0, float(pcfg.get("interval_hours", 6)) * 3600.0))
                continue
            limit = max(1, min(int(pcfg.get("limit", 20)), 100))
            sm = assistant.skill_manager
            if sm:
                out = await sm.episodic_backfill_embeddings(limit)
                if int(out.get("updated") or 0) > 0:
                    assistant.logger.info("情景记忆周期补全: %s", out)
                else:
                    assistant.logger.debug("情景记忆周期补全: %s", out)
        except Exception:
            assistant.logger.exception("情景记忆周期补全失败")
        try:
            hrs = float(
                ((assistant.config.config or {}).get("memory") or {})
                .get("vector", {})
                .get("backfill_periodic", {})
                .get("interval_hours", 6)
            )
        except (TypeError, ValueError):
            hrs = 6.0
        await asyncio.sleep(max(60.0, hrs * 3600.0))
