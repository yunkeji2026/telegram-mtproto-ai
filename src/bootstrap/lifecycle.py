"""AIChatAssistant 生命周期(Stage4,从 main.py 整方法原样迁出,仅 self->assistant)。

start_assistant(assistant): 启动所有服务/后台循环;stop_assistant(assistant): 优雅停机。
"""
from __future__ import annotations

import asyncio


async def start_assistant(assistant):
    """启动AI聊天助手"""
    if not assistant.running:
        try:
            assistant.logger.info("🚀 启动AI聊天助手...")
            assistant.running = True

            # ★ 修复：telegram_client.start() 内部 await idle()，永不返回；
            # 若直接 await 会阻塞后续 LINE/Messenger RPA 的 start()，
            # 所以包装成后台 task，紧接着启动 RPA 服务，保持原日志语义不变。
            # 桌面/未配置协议号时 telegram_client 为 None，整段跳过。
            if assistant.telegram_client is not None:
                assistant._telegram_task = asyncio.create_task(
                    assistant.telegram_client.start(), name="telegram_client_start",
                )
                # 次要账号各自建独立 task
                for _i, _tc in enumerate(assistant.telegram_clients[1:], 2):
                    _t = asyncio.create_task(
                        _tc.start(),
                        name=f"telegram_client_start_{_tc.account_id}",
                    )
                    assistant._secondary_tg_tasks.append(_t)
                    assistant.logger.info(
                        "Telegram 账号 [%s] 已在后台启动", _tc.account_id
                    )
                # 给主 telegram 几秒完成登录
                try:
                    await asyncio.wait_for(
                        assistant._wait_until_telegram_ready(), timeout=15.0
                    )
                except asyncio.TimeoutError:
                    assistant.logger.warning(
                        "Telegram 客户端 15s 内未就绪，继续启动 RPA 服务（会在后台重试）"
                    )
            else:
                assistant.logger.info(
                    "无 Telegram 协议号（桌面/未配置），跳过 Telegram 启动；"
                    "Web 后台 / 收件箱 / RPA 正常启动"
                )

            # 设置信号处理
            assistant._setup_signal_handlers()

            assistant.logger.info("✅ AI聊天助手已启动，等待消息...")

            for _lsvc in assistant.line_rpa_services:
                try:
                    started = await _lsvc.start()
                    _aid = getattr(_lsvc, "account_id", "default")
                    if started:
                        assistant.logger.info("✅ LINE RPA [%s] 后台循环已启动", _aid)
                    else:
                        assistant.logger.info("LINE RPA [%s] 未自动启动（见配置）", _aid)
                except Exception as ex:
                    assistant.logger.warning("LINE RPA 启动跳过: %s", ex)

            for _wsvc in assistant.whatsapp_rpa_services:
                try:
                    started = await _wsvc.start()
                    _aid = getattr(_wsvc, "account_id", "default")
                    if started:
                        assistant.logger.info("✅ WhatsApp RPA [%s] 后台循环已启动", _aid)
                    else:
                        assistant.logger.info("WhatsApp RPA [%s] 未自动启动（见配置）", _aid)
                except Exception as ex:
                    assistant.logger.warning("WhatsApp RPA 启动跳过: %s", ex)

            if assistant.messenger_rpa_service is not None:
                try:
                    # ★ P2-4：注入 telegram_client 给 service → runner，
                    # 使人工转接能推送到 TG 管理员群
                    if assistant.telegram_client is not None and hasattr(
                        assistant.messenger_rpa_service, "bind_telegram_client"
                    ):
                        try:
                            assistant.messenger_rpa_service.bind_telegram_client(
                                assistant.telegram_client
                            )
                        except Exception:
                            assistant.logger.debug(
                                "bind_telegram_client 失败", exc_info=True
                            )
                    started = await assistant.messenger_rpa_service.start()
                    if started:
                        assistant.logger.info("✅ Messenger RPA 后台循环已启动")
                    else:
                        assistant.logger.info("Messenger RPA 后台循环未自动启动（见配置）")
                except Exception as ex:
                    assistant.logger.warning("Messenger RPA 启动跳过: %s", ex)

            if assistant.device_coordinator_service is not None:
                try:
                    await assistant.device_coordinator_service.start()
                    assistant.logger.info("✅ DeviceCoordinatorService 已启动")
                except Exception as ex:
                    assistant.logger.warning("DeviceCoordinatorService 启动跳过: %s", ex)

            if assistant.hotplug_watcher is not None:
                try:
                    await assistant.hotplug_watcher.start()
                    assistant.logger.info("✅ HotPlugWatcher 已启动")
                except Exception as ex:
                    assistant.logger.warning("HotPlugWatcher 启动跳过: %s", ex)

            asyncio.create_task(assistant._warmup_embeddings(), name="kb_warmup_embeddings")
            asyncio.create_task(
                assistant._episodic_backfill_on_startup(), name="episodic_backfill_startup"
            )
            asyncio.create_task(
                assistant._episodic_backfill_periodic(), name="episodic_backfill_periodic"
            )
            asyncio.create_task(assistant._periodic_self_heal(), name="kb_periodic_self_heal")
            asyncio.create_task(assistant._periodic_daily_learn(), name="daily_learner")

            # ★ W3-3G / W3-3K：启动 reunion 草稿成功率评估循环（DraftEvalScheduler）
            if assistant.contacts is not None and assistant.contacts.store is not None:
                from src.contacts.draft_eval import DraftEvalScheduler
                assistant.contacts.draft_eval_scheduler = DraftEvalScheduler(
                    assistant.contacts.store, eval_window_secs=86400,
                )
                asyncio.create_task(
                    assistant._periodic_draft_eval(), name="draft_success_evaluator",
                )

            # ★ W2-D4.2/4.3：启动 reactivation 主动唤醒循环
            # 必须在 contacts + messenger_rpa_service + ai_client 都就绪后启动
            await assistant._maybe_start_reactivation_loop()

            # ★ Phase K2：C 端变现（端用户订阅/解锁/打赏；默认关）
            # 先于 proactive_care，使变现门控开启时 EntitlementStore 已就绪
            assistant._maybe_init_monetization(assistant._web_app)

            # ★ Phase O：主动关怀引擎（记忆驱动的约定/事件跟进）
            await assistant._maybe_start_proactive_care(assistant._web_app)

            # ★ 多平台 deferred 队列（非 messenger 主动消息的发送闭环；默认关）
            await assistant._maybe_start_deferred_outbox()

            # ★ 质量趋势持久化（周期落地 companion_quality_overview；默认关）
            await assistant._maybe_start_quality_trend()

            # ★ P4-B：TTS 成本按日落库（供 ops 看板画近 N 天花费曲线；默认关）
            assistant._maybe_init_tts_cost_log()

            # ★ S：翻译置信度低置信率/切换率按日落库（供看板画 7 天 sparkline；默认关）
            assistant._maybe_init_translation_trend_log()
            assistant._maybe_init_realtime_voice_trend_log()
            # ★ P8：出站路由回落率按日落库（供看板画 7 天 sparkline；默认关）
            assistant._maybe_init_send_route_trend_log()
            # ★ F1：会话身份健康（入站 raw% / 头像 empty%）按日落库（默认关）
            assistant._maybe_init_identity_trend_log()

            # ★ 每人设「相册/媒体」注册表（图/视频 + 触发词；始终开启，供相册后台/回复链读写）
            assistant._init_persona_media_store()

            # ★ Q 延伸：ingest 回写 contact_id（默认关）
            assistant._maybe_wire_ingest_contact_writeback()

            # ★ Q 延伸·存量回填：给历史会话补 contact_id（默认关，一次性）
            asyncio.create_task(
                assistant._maybe_run_contact_id_backfill(),
                name="contact_id_backfill",
            )

            # ★ P2：陪伴主动话题调度（沉默检测 + 冷却 → P1 选题 → 主动开场）
            await assistant._maybe_start_companion_proactive()

            # 坐席工作台实时化（D5a）：后台轻量 ingest 轮询 → 新入站消息发 SSE 事件
            assistant._maybe_start_inbox_ingest_loop()

            # Mobile Bridge 轮询循环
            if assistant.mobile_bridge is not None:
                try:
                    await assistant.mobile_bridge.start()
                    assistant.logger.info("✅ Mobile Bridge 已启动")
                except Exception as ex:
                    assistant.logger.warning("Mobile Bridge 启动跳过: %s", ex)

            # 保持运行直到收到停止信号
            while assistant.running:
                await asyncio.sleep(1)

        except KeyboardInterrupt:
            assistant.logger.info("收到中断信号，正在关闭...")
        except Exception as e:
            assistant.logger.error(f"运行错误: {e}")
        finally:
            await assistant.stop()


async def stop_assistant(assistant):
    """停止AI聊天助手"""
    if assistant.running:
        assistant.logger.info("正在停止AI聊天助手...")
        assistant.running = False

        # 本机 IndexTTS2 随主程序退出而关闭（仅当由本进程托管拉起且 stop_with_app）
        if assistant.local_tts is not None:
            try:
                await assistant.local_tts.stop()
            except Exception as ex:
                assistant.logger.warning("本机 TTS 关闭异常: %s", ex)

        # 排空消息队列（graceful drain）
        if assistant.telegram_client and hasattr(assistant.telegram_client, 'message_queue'):
            q = assistant.telegram_client.message_queue
            if not q.empty():
                assistant.logger.info("等待消息队列排空 (%d 条)...", q.qsize())
                try:
                    await asyncio.wait_for(q.join(), timeout=10)
                    assistant.logger.info("消息队列已排空")
                except asyncio.TimeoutError:
                    assistant.logger.warning("消息队列排空超时，放弃剩余 %d 条", q.qsize())

        # 持久化上下文快照
        if assistant.telegram_client and hasattr(assistant.telegram_client, 'context_manager'):
            cm = assistant.telegram_client.context_manager
            if cm and hasattr(cm, 'persist_snapshot'):
                try:
                    cm.persist_snapshot()
                    assistant.logger.info("上下文快照已保存")
                except Exception as e:
                    assistant.logger.warning("上下文快照保存失败: %s", e)

        for _lsvc in assistant.line_rpa_services:
            try:
                await _lsvc.stop()
                assistant.logger.info("LINE RPA [%s] 后台循环已停止", getattr(_lsvc, "account_id", "?"))
            except Exception as ex:
                assistant.logger.warning("LINE RPA 停止异常: %s", ex)

        if assistant.messenger_rpa_service is not None:
            try:
                await assistant.messenger_rpa_service.stop()
                assistant.logger.info("Messenger RPA 后台循环已停止")
            except Exception as ex:
                assistant.logger.warning("Messenger RPA 停止异常: %s", ex)

        for _wsvc in assistant.whatsapp_rpa_services:
            try:
                await _wsvc.stop()
                assistant.logger.info("WhatsApp RPA [%s] 后台循环已停止", getattr(_wsvc, "account_id", "?"))
            except Exception as ex:
                assistant.logger.warning("WhatsApp RPA 停止异常: %s", ex)

        # D5a：收件箱 ingest 轮询优雅停止
        if assistant._inbox_ingest_task is not None:
            try:
                assistant._inbox_ingest_task.cancel()
                assistant.logger.info("收件箱 ingest 轮询已停止")
            except Exception as ex:
                assistant.logger.warning("收件箱 ingest 轮询停止异常: %s", ex)

        # W2-D4.2：reactivation_loop 优雅停止
        if assistant._reactivation_loop is not None:
            try:
                await assistant._reactivation_loop.stop()
                assistant.logger.info("reactivation_loop 已停止")
            except Exception as ex:
                assistant.logger.warning("reactivation_loop 停止异常: %s", ex)

        # P2：companion 主动话题调度优雅停止
        if assistant._companion_proactive_loop is not None:
            try:
                await assistant._companion_proactive_loop.stop()
                assistant.logger.info("companion proactive_topic 调度已停止")
            except Exception as ex:
                assistant.logger.warning("companion proactive_topic 停止异常: %s", ex)

        # Phase O：care_dispatcher 优雅停止
        if assistant._care_dispatcher is not None:
            try:
                await assistant._care_dispatcher.stop()
                assistant.logger.info("care_dispatcher 已停止")
            except Exception as ex:
                assistant.logger.warning("care_dispatcher 停止异常: %s", ex)

        # 多平台 deferred 队列优雅停止
        if assistant._deferred_outbox_dispatcher is not None:
            try:
                await assistant._deferred_outbox_dispatcher.stop()
                assistant.logger.info("多平台 deferred 队列已停止")
            except Exception as ex:
                assistant.logger.warning("deferred_outbox 停止异常: %s", ex)

        # 质量趋势快照器优雅停止
        if assistant._quality_trend_snapshotter is not None:
            try:
                await assistant._quality_trend_snapshotter.stop()
                assistant.logger.info("质量趋势快照器已停止")
            except Exception as ex:
                assistant.logger.warning("quality_trend 停止异常: %s", ex)

        if assistant.mobile_bridge is not None:
            try:
                await assistant.mobile_bridge.stop()
            except Exception as ex:
                assistant.logger.warning("Mobile Bridge 停止异常: %s", ex)

        if assistant.hotplug_watcher is not None:
            try:
                await assistant.hotplug_watcher.stop()
                assistant.logger.info("HotPlugWatcher 已停止")
            except Exception as ex:
                assistant.logger.warning("HotPlugWatcher 停止异常: %s", ex)

        if assistant.telegram_client:
            await assistant.telegram_client.stop()

        if assistant.skill_manager:
            await assistant.skill_manager.cleanup()

        if assistant.ai_client:
            await assistant.ai_client.cleanup()

        # D: 关掉 web 独立线程（uvicorn server 设 should_exit + 等线程退出）
        try:
            if assistant._web_server is not None:
                assistant._web_server.should_exit = True
            if assistant._web_thread is not None and assistant._web_thread.is_alive():
                assistant._web_thread.join(timeout=5.0)
                if assistant._web_thread.is_alive():
                    assistant.logger.warning("Web 管理后台线程 5s 内未退出，跳过等待")
                else:
                    assistant.logger.info("Web 管理后台线程已停止")
        except Exception as ex:
            assistant.logger.warning("Web 管理后台停止异常: %s", ex)

        assistant.logger.info("✅ AI聊天助手已停止")
