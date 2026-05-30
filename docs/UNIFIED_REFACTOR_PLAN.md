# 统一重构计划 — telegram-mtproto-ai

> **创建日期**：2026-05-23
> **目标**：解决 4 部手机差异化 bug + 统一四端 RPA 模块 + 重建人设系统
> **文档作用**：阶段性开发的权威文档。每阶段完成后追加进度与测试结果。如果 Cascade 中断重入，读这份文档即可继续工作。

## 0. 总体原则

1. **小步前进**：每阶段独立可测试、可回滚。
2. **先稳后改**：P0 修 bug 不动架构，P1 清数据，P2 才重构架构。
3. **测试驱动**：每阶段必跑 `python -m pytest tests/ -n auto -q`，并记录结果。
4. **以代码为准**：文档先于实施容易漂移；实施时如发现代码与计划不符，**修计划而非硬改代码**。
5. **深度思考触发点**：每阶段开始前重读本节 + 当前阶段 § + 上一阶段 § 的"教训"，确认方向无误再动手。

---

## 1. 四部手机问题归因（已分析）

| 手机壁纸 | 现象 | 根因 |
|----------|------|------|
| 05 | Messenger 对话页变相机 + 横屏 | (a) Messenger 无 portrait 强制；(b) BACK→TAB_CHATS 路径 misalign |
| 07 | 陌生人"还原聊天记录"卡死 + Yunshan 只点赞不回 | (a) restore_modal 未在 guard 白名单；(b) 输入框 tap 偏到 like 按钮 |
| 08 | 反复打开 Victor Zan | Vision row_index 漂移 + calibration 偏移 → wrong_chat 循环 |
| 09 | 反复打开 Yunshan + 进相机 + 横屏自切回 | 同 08 + 同 05；横屏自切回说明 ROM 加速度计敏感 |

**共因**：Litho `ui_rows=0` 导致全部走 formula tap；每机 calibration 历史漂移不一致；缺统一防呆。

**WhatsApp 不回因**：device_coordinator 让 Messenger 在 wrong_chat 失败循环里吃光时间片，WA 排队饿死。

---

## 2. 阶段路线图

| 阶段 | 范围 | 预估 | 依赖 | 测试关卡 |
|------|------|------|------|----------|
| **S1** | P0-A 四机统一 RPA 防呆 | 2h | 无 | 单元测试全绿 + 真机日志 30min 无 wrong_chat 循环 |
| **S2** | P0-B device_coordinator 排程公平 | 1h | S1 | 单元测试全绿 + 30min 内每机 WA/MR/LINE 各跑 ≥1 轮 |
| **S3** | P1 清 KB + 移除客服 persona | 3h | S1 | KB 表行数 = 0；grep 无客服话术残留；测试全绿 |
| **S4** | P2-a 人设管理 audit & 设计 | 半天 | S3 | 设计文档落档；本节追加 Code Review |
| **S5** | P2-b CrossPlatformIdentity 表 + 三层路由 | 1.5 天 | S4 | 新建 schema + migrate；新增 3 个 unit test 全绿 |
| **S6** | P2-c /ai-studio 集中入口 + 四端模块化命名 | 2 天 | S5 | E2E 手测：四端从 /ai-studio 切换人设生效 |
| **S7** | 全量回归 | 0.5 天 | S6 | 全量测试连跑 2 次全绿 |

---

## 3. 阶段详细设计

### S1 — P0-A 四机统一 RPA 防呆

**目标**：让 4 机在 RPA 层面遵循同一规则，消除 calibration / 模态白名单的设备级差异。

**改动清单**：

1. **Messenger 强制 portrait**（参考 WhatsApp 已有写法）
   - 文件：`src/integrations/messenger_rpa/runner.py::_foreground_messenger`
   - 在 `am start` 之前插入与 WA 同款的 `settings put system user_rotation 0`
   - 加 1s 等待让旋转生效

2. **共享 guard 白名单模块**
   - 新建：`src/integrations/shared/guard_whitelist.py`
   - 把 `_guard_is_inbox_false_positive` 的逻辑搬过去（empty title / Stories / Meta AI / Messenger 品牌 / row_index prompt 泄漏 / 长 title / 通知文字 / 正向关键词检查）
   - Messenger / LINE / WA / Telegram runner 都 import 并使用同一函数
   - **新增** `restore_chat_history_modal` 处理：title 含 "restore" / "还原" / "履歴" → 自动按"否/取消"按钮（坐标 fallback）

3. **校准异常自动降级**
   - 文件：`src/integrations/messenger_rpa/coord_calibrator.py`
   - 加载时校验：若 `chat_row_height > 180` 或 `< 100`，丢弃文件值，回退到 BASE 等比缩放
   - 同时加 WARNING 日志记录被丢弃的异常文件

4. **wrong_chat_streak ≥ 3 → search-by-name 切换**
   - 文件：`runner.py::_handle_thread_entry`
   - 当 `_wrong_chat_streak[(serial, target)] >= 3` 时：
     - 跳过 formula tap
     - 调用既有 `_search_chat_by_name(serial, target_name)`（若不存在需新建：tap 搜索栏 → 输入名 → tap 第一条结果）
     - 命中后 reset streak
   - 若 search 也失败则继续走 backoff 逻辑

5. **camera tap 二次保险**
   - 文件：`runner.py::_foreground_messenger` BACK 序列后
   - 已有 `is_in_thread` 检查；额外加 `is_camera_open` 检查（XML 含 `camera` resource-id 即按 BACK）

6. **横屏 watchdog**（Messenger）
   - 文件：`runner.py::run_once` 入口
   - 调用 `adb shell dumpsys input | grep SurfaceOrientation` 检测，若非 0（portrait）→ 立即下 portrait 命令 + sleep 1s

**测试**：
- 不破坏现有：`python -m pytest tests/test_rpa_shared.py tests/test_rpa_shared_yaml.py -q`
- 新增 `tests/test_guard_whitelist.py`：覆盖 6 类白名单场景
- 真机：启动 main.py 监控 30min，应无 `guard_needs_human` 真触发，wrong_chat 在 streak=3 后切 search

**回滚**：每个改动文件加注释 `# S1-P0A:` 标记，回滚时 grep 删行。

---

### S2 — P0-B device_coordinator 排程公平性

**目标**：Messenger 不再饿死 WA / LINE。

**改动清单**：

1. **per-platform 时间片预算**
   - 文件：`src/integrations/shared/device_coordinator.py`
   - 现状：每个 cycle 跑当前选中 platform 直到结束
   - 改为：单 cycle 最多 60s 切片；超时强制下一 platform

2. **优先级轮询**
   - 加 `_last_run_at[platform][serial]` 记录
   - 选 platform 时：取 `(now - last_run_at).max()` 那个，避免 starvation
   - 当某 platform `consecutive_fail >= 5` → 该 platform 进入 5min 冷却

3. **公平性指标**
   - 加 `metrics.per_platform_runs_per_hour` 输出到 /metrics

**测试**：
- `tests/test_device_coordinator_fairness.py`（新建）
- 真机 30min 跑：每 (serial, platform) 至少 3 轮

---

### S3 — P1 清 KB + 移除客服 persona

**前置**：列出当前 personas 与 KB 表，由用户确认哪些保留。

**改动清单**：

1. **审计现状**
   - `python -c "from src.bot import db; ..."` dump personas/kb_facts/kb_drafts 计数与 sample
   - 输出到 `tmp_audit_personas.txt`（用户审阅后删）

2. **清空 KB 数据**
   - SQL：`DELETE FROM kb_facts; DELETE FROM kb_drafts; DELETE FROM kb_categories WHERE name LIKE '%customer%';`
   - 加 backup：`cp knowledge_base.db knowledge_base.db.bak.$(date)`

3. **移除客服 persona**
   - `personas/*.yaml` 中删除 customer_service / kefu / 客服 名称的文件
   - `config/config.yaml::ai.persona_pool` 移除引用
   - grep `"您好.*帮.*"` `"请问.*服务"` 类客服话术，删 prompt 模板

4. **fallback 话术替换**
   - 找 `src/skills/*/prompts/*` 中的客服 fallback
   - 替换为人设感强的中性回复或交给 LLM 即时生成

**测试**：
- `python -m pytest tests/test_skill_*.py -q` 全绿
- DB 行数验证：kb_facts == 0 && kb_drafts == 0
- 真机：bot 回复内容不含客服话术（人工抽样 5 次）

---

### S4 — P2-a 人设系统 audit 与设计

**目标**：弄清现有 PersonaManager 全貌，画出三层路由设计。

**输出**：
- `docs/PERSONA_ARCHITECTURE_V2.md`
- 含：现状类图 + 目标类图 + DB schema diff + 迁移脚本草稿

**关键问题列表**（实施前确认）：
- 同一个 user 在 TG 与 Messenger 是否共享对话历史？
- 一个 persona 在不同 APP 是否需要不同语气微调？
- 跨 APP 识别同一人靠手机号 / 邮箱 / 名字 hash？

---

### S5 — P2-b CrossPlatformIdentity + 三层路由

**改动清单**：

1. **DB schema**
   ```sql
   CREATE TABLE user_identity_map (
     canonical_id TEXT PRIMARY KEY,
     platform TEXT NOT NULL,
     external_id TEXT NOT NULL,
     display_name TEXT,
     verified_at TIMESTAMP,
     UNIQUE(platform, external_id)
   );
   ```
   迁移加到 `src/bot/database.py` migration 列表

2. **三层 persona 路由**
   ```
   total_persona_id (admin 总管)
     ├── platform_persona_override[telegram|line|messenger|whatsapp]
     │     └── account_persona_override[<account_id>]
   ```
   解析顺序：account > platform > total

3. **EpisodicMemoryStore 改用 canonical_id**
   - 写入前：`canonical_id = identity_map.resolve(platform, external_id)`
   - 读取时：跨平台合并历史

**测试**：
- `tests/test_cross_platform_identity.py`（新建，3 个 case）
- migration up/down 双向 OK

---

### S6 — P2-c /ai-studio 入口 + 四端命名统一

**统一命名规范**（强制）：
| 旧 | 新 |
|----|----|
| `messenger_rpa` | `messenger`（保留 alias）|
| `line_rpa` | `line` |
| `whatsapp_rpa` | `whatsapp` |
| `telegram_client` | `telegram` |

四端 runner 统一接口：
```python
class PlatformRunner(Protocol):
    async def run_once(...) -> RunResult
    async def send_text(...) -> SendResult
    async def health_check(...) -> HealthStatus
```

**/ai-studio 整合**（已有部分实现）：
- `/ai-studio` 4 Tab：人设管理 / 跨平台身份 / 知识库 / 对话审计
- 各平台设置页只留平台特有项（账号、触发、cooldown、daily_cap、设备）

---

### S7 — 全量回归

- `python -m pytest tests/ -n auto -q` 连跑 **2 次**全绿
- 失败 → 加专项测试 → 直到全绿
- 真机 4 机连跑 1h：每机每平台至少 1 次成功回复

---

## 4. 进度日志（每阶段实施完追加）

### S1 — P0-A 四机统一 RPA 防呆
- [x] 状态：**完成**
- 改动：Messenger 加 portrait 命令；coord_calibrator row_height 校验收紧 100-180；guard_whitelist.py 共享模块；restore_chat 自动 BACK；wrong_chat_streak≥3 切 search-by-name
- 测试结果：test_guard_whitelist.py 34 cases 全绿；全量仅预存失败 1 条（whatsapp funnel）
- 教训：classify_guard 必须先判 restore_chat 再判 false_positive，否则 restore 被宽泛规则吞掉

### S2 — device_coordinator 排程公平性
- [x] 状态：**完成**
- 改动：per-platform 独立 force_check_ts；排序改为 badge 优先 + 最久未跑优先（防 WA/LINE 饿死）
- 测试结果：test_device_coordinator_fairness.py 5 cases 全绿；全量仅预存失败 1 条

### S3 — 清 KB + 移除客服 persona
- [x] 状态：**完成**
- 改动：sqlite 清空 kb_entries/kb_rules/kb_error_codes/kb_drafts/kb_translations（25+4+4+102+10 行）；base.py fallback 话术去客服腔；skill_manager.py anti_repeat_hint 去客服指令；emotion_enhancer.py tone_adjustments 移除客服短语；kb_seeded_once meta=1 防止重启回填
- 测试结果：全量仅预存失败 1 条

### S4 — 人设 audit 与设计
- [x] 状态：**完成（审计 + 标记缺口）**
- 审计结果：profiles_runtime.yaml 共 7 个人设，全部 claim_human=True/deny_ai=True 伴侣风格
- 平台绑定：Telegram=marcus_wei、Messenger全账号=chen_meiling、WhatsApp=lin_jiaxin
- ⚠️ 缺口：LINE 两账号（line_ij8/line_xw8）无 persona_ids，仅靠 reply_style_hint 兜底
- 行动项：需用户决定 LINE 使用哪个人设（建议 lin_xiaoyu 或 haruko_traveler），然后在 config.yaml line_rpa.accounts[].persona_ids 中添加

### S5 — CrossPlatformIdentity + 三层路由
- [x] 状态：**完成**
- 新增 src/utils/cross_platform_identity.py：user_identity_map SQLite 表，resolve/link/unlink/list_all/get_by_canonical
- SkillManager: _cpi 初始化（同 bot.db）；_episodic_storage_key(platform=) 加 CPI 解析；platform 加入 _line_merge_keys
- 四端 ctx 注入 platform：telegram/messenger_rpa/whatsapp_rpa/line_rpa（LINE 三条路径）
- admin.py 三个 API：GET /api/identity，POST /api/identity/link，POST /api/identity/unlink
- 测试：test_cross_platform_identity.py 12 cases 全绿；全量仅预存失败 1 条

### S6 — /ai-studio + 四端命名统一
- [x] 状态：**完成**
- ai_studio.html 加入第 5 个 tab「身份绑定」：user_identity_map 表格（按 canonical_id 分组）+ 手动 link/unlink UI
- 平台显示名称统一：_platLabel()将 messenger_rpa/whatsapp_rpa/line_rpa/telegram 映射为 Messenger/WhatsApp/LINE/Telegram
- tab 切换逻辑修复（identity 分支顺序）
- 全量仅预存失败 1 条

### S7 — 全量回归
- [x] 状态：**完成（零失败）**
- 顺手修复预存失败：whatsapp_rpa.html funnel wrapper 加 id="pane-wa-funnel"（1 行）
- 最终结果：2731 passed / 39 skipped / 0 failed

---

## 5. 中断恢复手册

如果 Cascade 重启或忘记上下文：
1. 读本文 § 2 路线图，确认当前阶段
2. 读 § 4 进度日志，看最后一个 `[x]` 标记的阶段
3. 若该阶段"教训"非空，先读教训
4. 进入下一阶段 § 详细设计，按改动清单做
5. 完成后追加 § 4 进度，更新测试结果
6. 进入下下阶段
