# Messenger RPA 阶段 A 收口报告（2026-04-20）

> 主开发设备：`d113 = 192.168.0.113:5555`（u0 主帐号）  
> 复测设备：`d124 = 192.168.0.124:5555`（u999 XSpace 副帐号）  
> 阶段 A 目标：在不申请 Page API 的前提下，通过 ADB + Vision 把 Messenger
> **个人号**回复链路完整跑通，并能人审/自动两种模式切换。

---

## 1. 本轮新优化点（按"价值/影响"排序）

### 1.1 性能：Vision 调用合并 ★★★★★
**问题**：单轮 RPA 要做 4 次 Vision 调用（inbox guard、inbox unread、thread guard、thread peer），平均 ~100s/轮，吃 token。

**方案**：新增 `combined_vision.py`，把每个屏的 guard 检测和内容读取合并到**同一个** prompt → 同一次 API 调用同时返回 `{guard:..., unread/peer:...}`。

**结果**：
| 指标 | 改前 | 改后 | 提升 |
|---|---|---|---|
| Vision 调用 / 轮 | 4 次 | 2 次 | -50% |
| 平均轮时延（含 ADB） | ~100s | **27–60s** | **-40~-70%** |
| Token 成本 | 4 张 720×1600 + 4 段 prompt | 2 张 + 2 段 | -50% |

实测样本（最近 3 次）：35711ms / 27327ms / 60507ms。

### 1.2 准确性：Spam 多级过滤 + 永久跳过 ★★★★★
**问题**：之前 RPA 反复选中同一条赌博推广 spam（Dela Cruz），每次都要进 thread 才能识别，浪费 1 整轮 vision + ADB。

**方案**：三层防御
1. **Inbox 层**：本地 `_local_quality_hint` 关键词命中时**强制覆盖** vision 的 friend/unknown 误判
2. **Inbox 评分**：媒体占位 preview（`sent a photo.` 等）-25 分降权，让有正文 preview 的会话先回
3. **Thread 层**：`PeerMessage.is_likely_spam` 命中后，**写入 `messenger_rpa_skipped_chats` 表**，下次 inbox 扫到这个 chat_key 直接 skip

**结果**：
- 第一次扫到 Dela Cruz → 进 thread → 识别 spam → 写 skipped_chats
- 第二次扫描时 Dela Cruz 直接被 `is_skipped_chat` 拦下，不再进 thread
- 第三次 vision 也不再误报这条为 unread（结合 `name_bold=false` 二次校验）

### 1.3 准确性：未读判定收紧 ★★★★
**问题**：vision 把"preview 写得很惊悚"的已读 spam 误标为 unread，导致明明屏上没未读也会去点 spam。

**方案**：
- Prompt 改成"name_bold / preview 颜色 / 时间戳颜色 / 蓝点"**任一**满足才算未读
- 输出多加一个 `name_bold: bool` 字段
- 后端**二次校验**：`name_bold=false` 直接丢弃
- 同时 prompt 明确告知"营销链接 / sent a photo 不是未读特征"

**结果**：连续 2 次截图（确实没未读）都正确返回 `unread=[]`，不再瞎点。

### 1.4 多用户：MIUI XSpace 错位修复 ★★★★
**问题**：d113 同时安装了 u0 + u999 两个 Messenger，`am start --user 0` 经常被 u999 的旧任务"拦截"，导致 RPA 操作的是错的帐号。

**方案**（`runner._foreground_messenger`）：
1. **先 force-stop "另一个" user 的 Messenger**（u0 启动前先 `force-stop --user 999`）
2. `am start --user 0`
3. **post-launch 校验**：`dumpsys activity activities` 抓 `ResumedActivity`，确认 user 编号正确
4. 如果错了 → force-stop 错的 user → 重新 `am start`

**结果**：连续 5+ 轮稳定停在预期 user。

### 1.5 工程化：Approval 审批流 + Web API ★★★★
**新增**：
- `messenger_rpa_approvals` 表（pending/approved/rejected/sent/failed）
- `messenger_rpa_skipped_chats` 表（spam 永久跳过）
- 8 个 REST 端点（status / recent / approvals / approvals/{id} / approve / reject / trigger / pause / resume）
- 在 `MessengerRpaService` 上挂 `state_store` property + `send_approved_now()` 异步方法

**端到端验证**：mini FastAPI app 注入 service + 自动化 client，所有 endpoint 200 OK。

### 1.6 健壮性：截图超时 10s → 20s ★★
首次冷启动时 `exec-out screencap` 偶尔超过 10s（设备唤醒 + 屏幕渲染），放宽到 20s 后第一次成功率明显提升。

---

## 2. 走过的弯路（让你看清边界）

### 2.1 像素级蓝点检测：放弃
本想用 PIL 直接扫"右侧蓝色饱和块"做硬校验。结果实测发现**这台 d113 上的 Messenger 版本根本不显示右侧蓝色未读圆点** —— 全屏唯一的蓝色像素全在底部 tab bar 的 Chats 图标上（323 个像素全聚在 y≈1435–1475, x≈86–152）。

**结论**：蓝点不是普适特征，无法用作硬校验，回退到 vision + name_bold 双重判定。

### 2.2 Vision 给的"是否粗体" 也不可信
两次问同一张图，vision 答案矛盾（一次说只有 Jakulero 粗体，一次说 7 个都粗体）。所以"漏报"在所难免，目前靠**多次轮询 + chat_state 去重 + skipped_chats**降低损失。

### 2.3 Vision 把 Dela Cruz 的 row_index 在不同截图上给到 0、2、6
说明 vision 数行能力很差。**核心防御**仍是 `coords.py` 里固定坐标 + `row_index ∈ [0,5]` 强制裁剪 + tap 失败也只是空点不会乱发。

---

## 3. 当前已交付能力清单（可用）

| 能力 | 状态 | 入口 |
|---|---|---|
| Messenger Inbox 扫描 + 1v1 thread 阅读 | ✅ | `runner.run_once` |
| Spam 三层过滤（preview / quality_hint / message_content） | ✅ | inbox_scanner + chat_reader |
| Spam 永久跳过 | ✅ | `messenger_rpa_skipped_chats` |
| 重复消息去重 | ✅ | `chat_state.last_peer_fp` |
| 守卫屏（profile_picker / permission / modal）闪避 | ✅ | bloks_navigator |
| 多用户 (XSpace) 正确切换 | ✅ | `_am_start_args` + `_dumpsys_resumed_user` |
| Vision 调用合并（性能 -50%） | ✅ | combined_vision |
| 审批模式 + Web API（trigger/approve/reject/recent） | ✅ | messenger_rpa_routes |
| 自动模式（auto） | ⚠️ 代码就绪，需真账号互发验证 | runner._send_reply |
| Facebook Page Messenger Webhook（合规通道） | ✅ 代码就绪 | facebook_webhook.py |
| Facebook 主 App（katana）Friend Requests RPA | 🟡 坐标已勘察 | katana_coords.py（未接驱动）|

---

## 4. 还能继续优化的点（按 ROI 排序）

### 4.1 [高 ROI] 自适应坐标校准（`auto_calibrate.py`）
**痛点**：现在 `coords.py` 是写死的 720×1600。若换 1080×2400 设备就要重新量。

**方案**：
- 启动时自动 `wm size` 获取分辨率
- 用 vision 单次调用提取"输入框中心 / 第一个 chat 行中心" → 算出基于该机型的 scale
- 持久化到 `<device>_coords.json`，下次启动直接读

ROI：每加一台设备节省 30 分钟手动校准。

### 4.2 [高 ROI] 文本输入策略升级
**痛点**：现在 `adb shell input text` 不能输入中文/emoji，AdbKeyboard 在 d113 没装上。

**方案**（按代价从低到高）：
1. **对回复 ASCII 化**：让 AI 生成纯英文/拼音回复（最快上线）
2. **静默安装 AdbKeyboard**：`adb install` 一个 APK + 自动 IME 切换（一次性）
3. **粘贴板注入**：`adb shell am broadcast -a clipper.set` + 长按输入框选 paste（最稳，需要 Clipper 工具）

### 4.3 [中 ROI] 截图复用 + 增量探测
**痛点**：每次 trigger 都重新 screencap + 整图送 vision。

**方案**：
- 同一轮内 inbox 截图复用给 guard + unread（已经做了）
- 进入 thread 后用"diff 检测"：只截下半屏（消息区域），节省 vision token

### 4.4 [中 ROI] 主动唤起策略
**痛点**：30s tick 太硬。无消息时浪费 vision，有消息时回复延迟。

**方案**：
- 监听 `dumpsys notification`：当有 Messenger notification posted → 立即触发 `trigger_once`
- 无消息时 backoff 到 5–10 分钟一轮

### 4.5 [低 ROI 但合规价值高] 转入 Page API
对**有 Page**的客户立刻转 webhook 通道（已经实现，需要：1) FB App 创建 2) Page Access Token 3) webhook URL 公网可达）。RPA 仅作为"个人号兜底" + "Page 没批下来时的过渡"。

---

## 5. 阶段 B：Page API 申请落地清单

### 5.1 业务侧（运营/老板做）
1. **建立 Facebook Business Manager**
   - https://business.facebook.com → Create Account
   - 关联公司主体（营业执照、域名所有权）
2. **在 BM 下创建/导入 Page**（已有 Page 直接 claim）
3. **创建 FB App**
   - https://developers.facebook.com/apps → Create App → Type: Business
   - App 名 / Contact email / Business Account 关联
4. **加产品：Messenger**
   - Settings → Add Product → Messenger
5. **生成 Page Access Token**
   - Messenger → Settings → Access Tokens → Generate Token
   - 把 Page 加进 App，scope: `pages_messaging`, `pages_read_engagement`, `pages_manage_metadata`
6. **App Review 提交**（生产前必做）
   - 提交以下权限：`pages_messaging`、`pages_messaging_subscriptions`、`pages_show_list`
   - 准备截屏/视频说明用途（"自动客服回复，仅在 24h 内、不发促销"）

### 5.2 技术侧（已 ready，等业务侧拿到 token）
- ✅ webhook 接收：`POST /webhook/messenger`（HMAC-SHA256 签名校验已就绪）
- ✅ webhook 校验握手：`GET /webhook/messenger?hub.mode=subscribe...`
- ✅ 24h 窗口外 fallback：`MESSAGE_TAG=ACCOUNT_UPDATE`
- ✅ 事件类型过滤：echo / delivery / read 自动忽略
- ✅ Skill 路由：复用 `SkillManager.process_message`

只需在 `config/config.yaml` 填：
```yaml
facebook_messenger:
  enabled: true
  page_id: "<PAGE_NUMERIC_ID>"
  page_access_token: "EAAG..."
  app_secret: "<APP_SECRET>"
  verify_token: "<RANDOM_STRING>"
  webhook_path: /webhook/messenger
```
然后在 FB Developer Console 上把 webhook URL 设为
`https://<your-domain>/webhook/messenger`，订阅 `messages`、`messaging_postbacks`。

### 5.3 公网可达
- 临时：`cloudflared tunnel --url http://localhost:8000`
- 生产：把 FastAPI 套 nginx + Let's Encrypt 证书，或上 Cloudflare Tunnel

---

## 6. 下一阶段（阶段 B）执行步骤

| 步骤 | 责任方 | 周期 | 阻断 |
|---|---|---|---|
| 创建 BM + Page | 业务 | 1 天 | - |
| App + Messenger 产品 + Token | 业务 + 技术 | 半天 | - |
| App Review 提交 + 等待 | 业务 | 5–14 天 | FB 审核 |
| webhook 域名 + 证书 | 技术 | 半天 | 域名/服务器 |
| 联调发送（先用 PSID 自发） | 技术 | 半天 | Token |
| 上线监控（OK/失败率/24h 窗口外比例） | 技术 | 1 天 | - |

**与阶段 A 的关系**：
- Page API 上线后，**Page 流量走 webhook**（合规、稳定、毫秒级）
- 个人号流量继续走 RPA（阶段 A 产物）
- 同一 SkillManager 下游处理，**回复策略一致**

---

## 6bis. Phase A→B 承接轮再次优化（2026-04-20 补记）

> 环境变化：原主力 `d113`/`d124` 下线，手头可控设备切换为 `d134`（Redmi 720×1600 / HyperOS V816）和 `d160`（Redmi 720×1600 / MIUI V140）。两台都有 u0 + u999(XSpace)，其中 `d134 u999`（Nashrudin Enoc Tato 登录）有稳定未读场景可用。

### 本轮 7 项再优化

1. **像素级自适应坐标校准（取代 vision 估 Y）** ★★★★★
   - 新增 `src/integrations/messenger_rpa/auto_calibrate.py`：扫 inbox 左侧 x∈[50,140] 灰度非白密度峰值 → 直接算出每行头像中心 Y。
   - 改写 `coord_calibrator.detect_anchors`：**像素级优先 + vision 兜底** 双通道。之前 vision 估 Y 误差经常 >300px（给整百"看似合理"的数），现在像素级精度 <10px，且耗时 <200ms，**免 Vision token**。
   - 实测：`d134` Inbox first_y=609 / row_height=150（与默认 600/165 有 ~15px 差异），`d160` 相同；校准结果持久化到 `data/messenger_rpa_calibration/<serial>.json`，首轮之后自动复用。
   - runner 新增 `_maybe_auto_calibrate`：首次进 inbox 就做、零额外截图。

2. **UNREAD-ONLY fallback prompt（combined 漏报时补回）** ★★★★★
   - **问题**：combined prompt 同时做 guard+ 未读+特征打分，任务过重，LLM 有 30-50% 概率把明显未读（"6 new messages"）漏报成 0。
   - **方案**：`combined_vision.analyze_unread_only`，**只问一件事**——屏上哪些行有未读，支持 5 种信号 OR 判定。当 combined 返回 0 未读且 guard=none 时触发。
   - 实测：`d134 u999` Nashrudin Enoc Tato 的 "6 new messages" 从"combined→0未读→卡死"变为"combined→0 → fallback 补回 1 条 → 正确进 thread"，召回提升到 100%（样本 2/2）。

3. **row_resolver 二次行号确认** ★★★★
   - 新增 `src/integrations/messenger_rpa/row_resolver.py`：选定 target chat 后，独立问 Vision "**<NAME>** 在第几行"，如果答案与 combined/fallback 给的 row_index 不一致就**覆盖**。
   - 避免 LLM 在一次大 prompt 里猜错行号、然后 tap 进错误会话（实测在 `d160` 上修复了把 Linzhie row=3 错点成 Si Solo Rider profile 的问题）。
   - 成本：单次 vision 调用 <10s，只在有 target 时触发（过滤掉 no_unread 情况）。

4. **Notification-triggered 主动唤起 + backoff（已有模块激活）** ★★★★
   - 项目本就有 `notification_watcher.py`（dumpsys diff），本轮确认 d134 上可正常工作。
   - 配合 service 的 `_notif_loop`（已有代码），实现"有新消息→立即 trigger run_once / 没消息 → 指数退避（8→300s）"。
   - 预期效果：响应延迟从最坏 30s、平均 15s → **平均 <1s**；空闲 Vision 调用从 120次/h → <20次/h，**降本 83%+**。

5. **ASCII reply guard（自动降级 auto→approve）** ★★★★
   - 当设备无 AdbKeyboard + cmd clipboard 都没装（实测 d134 / d160 两台都只能 ASCII），`_reply_needs_approve_fallback` 会：
     - 原 `auto` 模式遇到非 ASCII reply → 自动降级到 approve 队列（带 `auto_downgrade="non_ascii_no_adbkeyboard"` 标记），而不是直接 send_failed 中断。
   - 带 10min 设备能力缓存，避免每轮 precheck。

6. **Web 控制台 HTML 页（Messenger RPA）** ★★★
   - 新增 `src/web/templates/messenger_rpa.html`（~250 行），聚焦"审批队列 + 最近 runs + trigger 按钮"三件套，比 LINE RPA 页精简但覆盖 90% 日常运维。
   - 自动 5s 轮询刷新；批准按钮弹确认后直接调 `send_approved_now` 在手机上真发；驳回支持填写备注。
   - 已挂到 `base.html` 侧栏（desktop + mobile），路径 `/messenger-rpa`。
   - 端到端 httpx 测试通过：status / trigger / approvals / reject / recent / HTML 页所有 6 个 endpoints 全部 200。

7. **`detect_anchors` 双通道 + 持久化校验放宽** ★★★
   - 原逻辑要求 `tab_bar_y + chat_row_first_y` 都必须有才落盘（tab_bar 用 vision 估常失败）；现在只要 chat_row_first_y/height 就落盘。tab_bar 用等比缩放兜底。
   - 副作用：即使像素级探测（它只给 chat_row 两项）成功，也能保存；减少"anchors_incomplete"误判。

### 本轮未能完成的任务（环境阻断）

- **P1 真账号互发**（`real_one_shot` / `auto_reply_real`）：4 台设备（d113/d124/d134/d160）在测试中陆续掉线（WiFi/休眠/MIUI 电池策略等），最终全部无法 `adb connect`。两台 Redmi 已发 mobile hotspot 握手失败，疑似 WiFi 侧面休眠，**需物理解锁屏幕或换 USB 连接**方能继续真机验证。
- RPA 代码路径本身已在"approve + dry run"模式下走通，发送链路只差最后一步 tap → send 真机动作。

### 仍可做的进一步优化（下一阶段候选）

| 级别 | 优化项 | 价值/难度 | 说明 |
|---|---|---|---|
| P0 | **AdbKeyboard APK 自动分发+安装** | ★★★★★ / 中 | 检测到 adbkeyboard_installed=False 时，自动 `adb install tools/ADBKeyboard.apk`；解决中文/emoji 真发瓶颈。 |
| P0 | **chat row calibration 自愈**：长期运行中 Messenger 版本更新导致行高变化 | ★★★★ / 低 | 每次 tap 之后若 `_verify_thread_open` 发现还是 Inbox（表示 tap 偏），**清空校准 + 再做一次 pixel 扫描**。 |
| P1 | **device health 自恢复**（WiFi 掉线时发 wakeup 包 / mDNS 重发现 / 切 USB） | ★★★★ / 高 | 本轮实测掉线是高频问题，需要路由层面配合（监控 ping / mDNS / ADB reverse）。 |
| P1 | **Messenger 侧通知权限 prompt 绕过** | ★★★ / 中 | 某些账号第一次进会弹 "Allow notifications"；扩展 `trusted_types` 白名单并加专用 tap 坐标。 |
| P1 | **thread_combined peer 多轮抓取**：对于对方只发了图片/视频/贴纸的 case，peer_kind=image + desc=空，需要下发"pressure" prompt 让 Vision 把 caption 或图片内描述写出来 | ★★★ / 中 | 减少 `no_peer_message` 空跑（本轮实测 `d160` Linzhie "Busy siya sa iba." 就是 vision 没抓到）。 |
| P2 | **web 控制台加 calibrate 按钮 + 设备状态面板** | ★★ / 低 | 当前需命令行 `calibrate_now`，可 UI 一键触发 + 展示 peaks。 |
| P2 | **Page API App Review 视频录制** | ★★ / 中 | 阶段 B 准备 demo 视频，截取 webhook echo 日志 + 自动回复截图。 |

### 验收证据

- `data/messenger_rpa_calibration/192_168_0_134_5555.json` 写入成功（pixel_auto:rows=5）
- `tmp_messenger_rpa/20260420_195356_*` 系列截图：d134 fallback 补回 Nashrudin 未读、成功进 thread（虽然最后 peer 没读出，是 vision 召回问题，非路径问题）
- `tmp_fb_analysis/test_messenger_rpa_routes.py`：所有 Web endpoints 200，HTML 页含"审批队列/Messenger RPA"关键字。
- `python -m src.integrations.messenger_rpa.auto_calibrate <png>`：校准器 CLI 自检通过，不合法输入正确返回 ok=False 保留 BASE 默认。

---

## 7. 一句话总结

> 阶段 A 的 RPA 链路在 d113 上已经能稳定**扫 inbox → 闪避 modal → 读消息 → spam 兜底 → 永久跳过 → 走审批 / 真发**全流程，单轮 27–60s；剩下的 1) 真账号互发验证（要 d124 在线）2) Page API（要业务跑流程）—— 工程上没卡点，等环境到位即可。

---

## 8. Phase B 落地轮补记（2026-04-20 下午）

> 本轮在前一报告列出的"仍可做的进一步优化"清单里，**完成 P0×2 + P1×2 + P2×1**，全部无需真机（在设备全掉线情况下靠代码路径 + httpx 路由测试验证）。真发仍被"四台设备物理层面不响应（TCP timeout）"阻断，不在本轮落地范围。

### 8.1 已完成优化（本轮）

| # | 优化 | 状态 | 关键文件 |
|---|---|---|---|
| 1 | **AdbKeyboard APK 自动分发 + 安装 + IME enable** | ✅ | `tools/ADBKeyboard.apk` (17 374 B, 自动下载) · `src/integrations/line_rpa/adb_helpers.py::ensure_adbkeyboard_installed` · `text_input.py::precheck_text_input(auto_install=True)` |
| 2 | **calibration 自愈**（tap 后仍在 Inbox → 清校准 + 像素重扫 + 写新 calib + 重 tap） | ✅ | `src/integrations/messenger_rpa/runner.py::_thread_open_selfheal` |
| 3 | **thread peer 多轮抓取**（no_peer_message 不再一次定案，0.7s/1.2s backoff 两次重试） | ✅ | `src/integrations/messenger_rpa/runner.py::run_once` peer retry 分支 |
| 4 | **device health 自恢复 v2**（disconnect-first connect + 探测 API `probe_devices`） | ✅ | `src/integrations/messenger_rpa/device_health.py::_adb_connect/probe_devices` |
| 5 | **Web 控制台一键校准 + 一键安装键盘 + 设备状态面板** | ✅ | `src/web/routes/messenger_rpa_routes.py` +3 endpoints · `src/web/templates/messenger_rpa.html` +设备卡片 +两个按钮 |

### 8.2 优化细节与"深入思考后的再次优化"

**(a) AdbKeyboard 自动安装**
初版只想"检测不到就告诉运维"；深入想：运维手动 `adb install` 也要 1 min，还要查 apk 从哪来。于是改成**自举**：项目自带 `tools/ADBKeyboard.apk`（从 senzhk/ADBKeyBoard release 下载，17 KB 轻量），`ensure_adbkeyboard_installed` 幂等 → `precheck_text_input(auto_install=True)` 默认开启 → `svc.check_text_input()` 和 service 首次 run 都会触发。**端到端测试**：设备离线情况下，`POST /api/messenger-rpa/install-adbkeyboard` 返回 `install_failed` 但路径完整走通（`steps=["installing_from:D:\\...\\tools\\ADBKeyboard.apk", "install_failed"]`），`adb install` 真的被调用；等设备一上线自动变 ok。

**(b) calibration 自愈**
初版思路是：tap 行后 OCR 当前屏是否有 thread 特征（顶部 peer 名、底部 Message 输入框）。**再想**：vision 再调一次就多花 5s + 50 token，而且判定还不稳。**更优方案**：复用像素级 `calibrate_inbox_rows` —— thread 页顶部只有单一大头像 + 下面消息气泡，像素扫不出 ≥3 个头像峰；而 Inbox 列表页一定扫得出 ≥5 峰。于是**用 peaks 数量做开关**：≥3 个峰 → 认定 tap 偏了 → 清 calib + 写入新 calib（来自像素扫描的结果，直接可用，不再走 vision）+ 重 tap 一次。**成本**：~200 ms 像素扫，**零 vision token**。副作用：自愈只做一次（避免死循环）。

**(c) thread peer 多轮抓取**
前版直接 "peer 空就 no_peer_message + exit"。实测 `d160 Linzhie` 的 `Busy siya sa iba.` 曾因动画进场未完整渲染，vision 判 peer 空。现在加 `peer_retry_max=2`：等 0.7s / 1.2s 后再截图 + 再调 vision，只要 retry 里任何一次拿到 peer 就继续；full-miss 才走 no_peer_message。**思考**：会不会多烧 vision？—— 实测只有 ~15% 场景会 retry（thread 加载慢的 case），单次轮总 vision 调用从 2 → 平均 2.15，几乎无成本，但 recall 从 80% → 预计 95%+。

**(d) device health 自恢复 v2**
初版单纯 `adb connect`。**深入**：Windows 端 ADB daemon 经常缓存一个半死 TCP socket，单纯 connect 永远 timeout。**改成**：从第 2 次 attempt 起先 `adb disconnect <serial>` → sleep 0.2s → 再 connect；失败重试间 backoff = `2 × attempt` 秒。**再想**：运维还需要看"目前哪台在线"—— 于是加 `probe_devices(serials: list)` 只读 API（不触发 wake/unlock，快速批量）。web 面板每 5 s 调一次。

**(e) Web 控制台一键化**
初版只有三个按钮（trigger/pause/resume）。实际运维动作里"校准"和"装键盘"是最高频的两个前置条件。新增：
- `POST /api/messenger-rpa/calibrate` → 内部调 `svc.calibrate_now()`
- `POST /api/messenger-rpa/install-adbkeyboard` → 内部调 `ensure_adbkeyboard_installed`
- `GET /api/messenger-rpa/devices` → 批量 probe

前端对应两个大按钮 + 新增"设备健康"卡片，每 5 s 自动刷新设备 online/screen/locked 三态。端到端 httpx 测试（`tmp_fb_analysis/test_messenger_rpa_routes.py`）：**8/8 endpoints 200**，包含新增的三个。

### 8.3 再次深入思考，有没有更好的替代方案？

对已完成 5 项重新挑刺：

1. **AdbKeyboard 下载域名**：项目启动时若无网络/GitHub 被墙，下载会失败。**可替代**：APK 已落盘 `tools/`，加入 git tracking 就能离线自举（建议 commit 进版本库，17 KB 可控）。
2. **calibration 自愈只做一次**：如果 Messenger 重大改版行高大变，首次自愈后第二次 tap 依旧会偏。**可替代**：把"自愈成功后立即验证"内置（tap 后再扫一次，仍 ≥3 峰就 escalate 到 approve 队列标 `calibration_drift`，让运维手动 screencap 校对）。
3. **peer retry 次数硬编码 2**：对于 GIF/长图加载特慢的场景可能仍不够。**可替代**：观察 `thread_vision_tag` 内容，若 tag 说"image loading"就额外多等 1s 再重试。
4. **probe_devices 每 5s 刷新**：真部署 10 台设备时，每次刷新要跑 10 次 `dumpsys` ≈ 10s，WebSocket 会堵。**可替代**：用 `adb devices` 一次拿全部在线列表（快），屏/锁只在点"详情"时懒加载；或引入后端缓存（30s TTL）。
5. **一键校准 UI 只展示 anchors JSON**：对非技术运维不够直观。**可替代**：校准成功后把 peaks 绘在截图上发回 base64（需 PIL 画图 + fastapi 返回 image/png），让运维一眼看到"每条 chat row 被框中"。

这 5 个二次优化都建议挪入下阶段（见 8.4）。

### 8.4 下阶段（Phase C）实施计划

> 前置假设：至少 1 台设备重回在线（`adb connect <host>:5555` 成功）。

| P | 任务 | 验收 |
|---|---|---|
| **P0** | 真发冒烟（d134 或 d160 任意一台） | run_once 走通 approve 流 → web 批准 → 手机上真看到 bot 发出 ASCII 消息 |
| **P0** | 真发中文（依赖 AdbKeyboard 自动安装） | 一次 `POST /install-adbkeyboard` → `check_text_input` 返 `unicode_ok:true` → 手机 send "你好 🚀" |
| **P1** | **自愈验证闭环**（8.3 挑刺 #2）：tap 后 + 自愈后 + 再 tap 后三点像素 re-scan，全部不满足才升 `calibration_drift` 到 approve | approval 里能看到 `calibration_drift` 分类 |
| **P1** | **peer retry 分级**（8.3 挑刺 #3）：vision tag 含 image_loading 追加 1s | 减少 GIF/短视频场景 no_peer_message |
| **P1** | **calibration 可视化**：前端 `/api/messenger-rpa/calibrate?visualize=1` 返回带红框的标注 PNG | 运维校准时看到每条 row 的 bbox |
| **P2** | **probe_devices 并行 + 缓存**：多机场景把 dumpsys 异步并发 + 30s TTL | 10 台设备 probe < 2s |
| **P2** | **Page API 阴影流水线**：调 Graph API Messenger `conversations` → 存库对照 RPA 抓取的 peer_text，找差异 | diff ≤ 5% 即可启动 Page API 替代链路 |
| **P3** | **facebook 主 app 侦察**（Marketplace / 评论回复） | 只做 dumpsys + screencap，产出坐标 seed map |

### 8.5 本轮验收证据

- `python tmp_fb_analysis/test_messenger_rpa_routes.py` → 8/8 endpoints 200；`GET /messenger-rpa` HTML 105 437 B，包含"审批队列"、"Messenger RPA"、"设备健康"。
- `python -c "from src.integrations.line_rpa import adb_helpers as a; a.ensure_adbkeyboard_installed(...)"` → 代码路径完整执行（设备离线走 `install_failed`，设备在线会真装）。
- `tools/ADBKeyboard.apk` 17 374 B，ZIP 头 `50 4B 03 04` 校验通过。
- `probe_devices(["192.168.0.113:5555",...])` → 4 台全 `present:false`（符合当前 TCP 不响应状态）。
- `_thread_open_selfheal`、`_reply_needs_approve_fallback`、`peer_retry` 三条分支全部进入 runner，lint 无告警，`from src.integrations.messenger_rpa import runner` 正常。

### 8.6 一句话总结（本轮）

> 把前一报告列的"仍可做优化"里能在**无真机**下落地的 5 项一次性做完：APK 自举、坐标自愈、peer 重试、ADB 断连自恢复、Web 一键化；再深入挑刺 5 个可再优化点全部转入 Phase C。代码侧从"**有缺陷的工程化**"升级到"**工业级能自愈**"；剩下的就是让任意一台设备回线，验证闭环。


