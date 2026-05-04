# Messenger RPA 优化方案 — 交接文档

> 最后更新：2026-05-02 23:00 UTC+8
> 设备序列号：`IJ8HZLORS485PJWW`（720×1600 分辨率）

---

## 一、项目概述

Messenger RPA 是一个基于 ADB 的 Facebook Messenger 自动回复系统。核心流程：

```
扫描 Inbox 截图 → Vision OCR 识别未读聊天 → 点击聊天行进入线程
→ 读取对方消息 → AI 生成回复 → 发送
```

**核心文件一览：**

| 文件 | 职责 |
|------|------|
| `src/integrations/messenger_rpa/runner.py` (~7482 行) | 主循环 `run_once()`，包含所有流程控制 |
| `src/integrations/messenger_rpa/ui_scraper.py` (~822 行) | UI XML 解析，提取聊天行/线程标题/发送按钮 |
| `src/integrations/messenger_rpa/thread_actions.py` (~820 行) | ADB 操作层，dump XML/注入文字/点击发送 |
| `src/integrations/messenger_rpa/bubble_detector.py` (126 行) | **[新建]** 像素级气泡归属检测 |
| `src/integrations/messenger_rpa/combined_vision.py` | Vision AI 管线，截图分析+OCR |
| `src/integrations/messenger_rpa/thread_title_vision.py` | Vision 方式读取线程标题 |
| `src/integrations/messenger_rpa/ui_inbox_scraper.py` | UI XML dump → InboxRow 列表 |

---

## 二、已完成的工作

### Phase 1：精准定位 + 错误回滚 ✅

**1. UI XML 匹配重写** (`runner.py` `_tap_chat_row` ~L3791-3856)
- 移除了有 bug 的 `name_match` + `stories-aware` 偏移 + `row0_heuristic`
- 新的 3 步匹配：`preview_match` → `row_index_direct` → `proximity_match`（校准 Y 坐标）
- 新增动态校准：当 XML 行数 ≥3 时自动更新 `first_y` 和 `row_h`

**2. Wrong-chat 回滚** (`runner.py` ~L1166-1191)
- 进入线程后对比 `actual_title` vs Vision name
- 仅对**公式坐标** tap 触发（UI XML tap 是像素级精确的，不会点错）
- 检测到错误 → 退出线程 + 设置 60s 冷却

**3. 冷却分层** (`runner.py` 多处)
- `self_skip_cooldown_sec` = 30s（测试模式）
- `spam_cooldown_sec` = 3600s
- `stale_peer_cooldown_sec` = 90s
- `wrong_chat_cooldown_sec` = 60s
- `post_send_cooldown_sec` = 120s **[新增]**

**4. Vision api_key 修复** (`runner.py` `_try_describe_peer_image` ~L4719)
- 原来用 `self._cfg.get("vision")` 返回空 dict → 改为 `self._vision_cfg()`

**5. OCR 归一化优化** (`runner.py` `_self_skip_norm_key` L172-183)
- CJK 名字前缀从 4→2 字符（只保留姓氏）
- 原因：Vision OCR 对日文汉字极不稳定（神沢颯人/風人/城人/飯人/凪人）

### Phase 2：去重修复 + 气泡检测 ✅（部分）

**6. Title 修正拆分** (`runner.py` ~L1192-1239) ✅
- **关键修复**：把 wrong-chat 检测（仅公式 tap）和 title 修正（所有 tap）拆分
- **修复前**：UI XML tap 时 `chat_key` 从不被修正 → OCR 每次读不同名字 → `fingerprint` 去重和 `reply_cooldown` 全部失效 → **多次重复回复**
- **修复后**：无论 tap 来源，只要 XML `actual_title` 与 Vision name 不同，就用 XML 结果修正 `target` 和 `chat_key`
- 新增日志：`[messenger_rpa] title 修正: vision=%r → xml=%r chat_key=%s`

**7. Post-send 冷却** (`runner.py` ~L2273-2288) ✅
- 发送成功后设置 `_self_skip_until`（默认 120s）
- 同时覆盖 XML 稳定名和 Vision 原始名
- 与 `companion_reply_cooldown_sec`（state_store 级）形成双保险

**8. Bubble 检测器** (`bubble_detector.py`) ✅
- 像素扫描线程截图，检测蓝色（自发）/灰色（对方）气泡
- 两轮扫描：先找蓝色（高可信度），再找灰色（更严格阈值）
- 已测试通过：对线程截图正确返回 `self`

**9. Bubble 检测器集成** (`runner.py` ~L1294-1339, ~L1395-1420) ✅
- 在 XML 自发守卫处加入交叉验证
- XML 说自发 + bubble 说自发 → 确认跳过
- XML 说自发 + bubble 说对方 → 信任 bubble，继续处理（XML 陈旧）
- 覆盖了两个检查点（pre-retry 和 post-retry）

**10. `_finish` 统一日志** (`runner.py` ~L7455-7463) ✅
- 所有 `run_once` 出口都经过 `_finish()`，现在统一输出 WARNING 级别日志
- 格式：`run_once 结束 step=X ok=Y chat=Z ms=N err=E`

---

## 三、🚨 当前阻塞问题

### `not_in_thread_after_tap` — 无法读取线程标题

**现象：**
```
step=not_in_thread_after_tap ok=True chat='神沢風人' ms=121670
err=could not verify thread title for '神沢風人'; skip reply
```

**截图确认系统确实已进入正确的线程**（标题 "神沢颯人" 可见），但 `_thread_title_from_xml()` 返回空。

**根本原因分析：**

`find_thread_title()` 在 `ui_scraper.py` L298-347 的匹配逻辑是：
1. 找 `class` 含 `Button` 的元素
2. `bounds.top < 260`（顶栏区域）
3. `content-desc` 以 `", Thread Details"` / `", 会話の詳細"` 等后缀结尾
4. 去尾后取第一个非状态段作为 peer name

**最可能的失败原因（需验证）：**
- Messenger Litho 渲染的顶栏可能**不暴露** `content-desc` 属性
- 或者 `content-desc` 的格式不匹配 `_PEER_TITLE_SUFFIXES` 中的任何后缀
- 或者按钮的 `class` 不含 `Button`
- 已有 XML dump 文件 `tmp_screenshots/thread_ui.xml` 可直接分析

**Vision fallback 也失败：**
- `_thread_title_from_vision()` 调用 `thread_title_vision.py` 的 `read_thread_title_via_vision()`
- 可能是 Vision API 超时或返回空

### 诊断步骤（下一个 AI 需要做的第一件事）

```python
# 1. 用已有的 XML dump 测试
import sys; sys.path.insert(0, '.')
xml = open('tmp_screenshots/thread_ui.xml', encoding='utf-8').read()

# 2. 查看 XML 中顶栏区域的所有元素
import re
from xml.etree import ElementTree as ET
root = ET.fromstring(xml)
for el in root.iter():
    bounds = el.get("bounds", "")
    # 找 top < 260 的元素
    m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
    if m and int(m.group(2)) < 260:
        cd = el.get("content-desc", "")
        cls = el.get("class", "")
        text = el.get("text", "")
        if cd or text:
            print(f'class={cls} top={m.group(2)} cd={cd[:80]} text={text[:40]}')

# 3. 专门找包含人名的元素
for el in root.iter():
    cd = el.get("content-desc", "")
    text = el.get("text", "")
    if "神沢" in cd or "神沢" in text or "颯人" in cd or "颯人" in text:
        print(f'FOUND: class={el.get("class")} bounds={el.get("bounds")} cd={cd} text={text}')
```

### 修复方向

1. **如果 `content-desc` 格式变了** → 更新 `_PEER_TITLE_SUFFIXES` 或放宽匹配逻辑
2. **如果 Litho 不暴露 `content-desc`** → 添加 `text` 属性匹配作为 fallback
3. **如果整个顶栏都不在 XML 里** → 只用 Vision fallback，或直接用 `target.name`（信任 UI XML tap 的精准度）
4. **最简方案**：对 UI XML tap（像素精确）的情况，放宽 `require_thread_title_before_reply` 限制，直接用 Vision 校准后的 `target.name`

---

## 四、TODO 清单（优先级排序）

### P0 — 必须立即修复

- [ ] **修复 `find_thread_title()` 线程标题提取**
  - 分析 `tmp_screenshots/thread_ui.xml` 确认 XML 结构
  - 更新匹配逻辑或添加 fallback
  - 这个不修好，系统完全无法发送任何消息

### P1 — 高优先级

- [ ] **验证 post-send 冷却是否有效**
  - 需要 `find_thread_title()` 修好后才能验证完整流程
  - 监控日志关键词：`发送成功 chat=X → post-send cooldown`

- [ ] **验证 bubble 检测器在生产中的表现**
  - 监控日志关键词：`bubble_sender`, `bubble_override_xml_self`
  - 特别注意深色模式/特殊主题下的色值偏差

### P2 — 中优先级

- [ ] **Local/Cloud Vision 管线统一**
  - 当前 local（截图分析）和 cloud（ZhipuAI API）是分开调用的
  - 应统一为单一管线，按场景选择

- [ ] **操作指标（Operational Metrics）**
  - `metrics.py` 已有基础框架
  - 需要添加：成功率、延迟分布、chat_type 分布、错误分类

### P3 — 低优先级

- [ ] **Vision fallback 优化**
  - `_thread_title_from_vision()` 在失败时没有详细错误日志
  - 应记录 Vision API 返回的原始内容用于调试

---

## 五、关键技术细节

### 5.1 Messenger XML 的特殊性

- **Litho 渲染**：Messenger 大量使用 Litho（Facebook 的自定义渲染引擎），导致 `uiautomator dump` 输出的 XML 结构非标准
- **content-desc 极度陈旧**：Inbox 的 `content-desc`（如 "You sent a voice message"）可能滞后 30+ 分钟不更新
- **bounds 是可靠的**：虽然内容陈旧，但元素的像素坐标 (`bounds`) 是精确的

### 5.2 OCR 名字漂移

Vision OCR 对日文汉字名极不稳定：
```
真实名字: 神沢颯人
OCR 变体: 神沢風人, 神沢城人, 神沢飯人, 神沢凪人, 神戸風人
```

解决方案：`_self_skip_norm_key()` 只保留前 2 个字符（姓氏）做归一化：
```python
# 神沢颯人 → "神沢"
# 神沢風人 → "神沢"  ← 同一个 key
```

### 5.3 消息去重机制（双保险）

| 层级 | 机制 | key | 存储 |
|------|------|-----|------|
| 1 | `fingerprint(peer_msg)` + `is_duplicate(chat_key, fp)` | `chat_key` = `acc_bg_phone_2:神沢颯人` | state_store (持久化) |
| 2 | `_self_skip_until[norm_key]` | `norm_key` = `"神沢"` | 内存 (进程级) |
| 3 | `companion_reply_cooldown_sec` | `chat_key` | state_store |
| 4 | **[新]** `post_send_cooldown_sec` | `norm_key` | 内存 |

### 5.4 `run_once()` 关键流程 (runner.py)

```
L1035 ─ 截图 inbox
L1045 ─ Vision 分析未读列表 (_inbox_combined)
L1070 ─ 选择目标 chat
L1090 ─ _tap_chat_row() → 点击进入线程
L1093 ─ inbox_self_sent_skip 守卫
L1106 ─ 截图 thread
L1114 ─ self-heal: 还在 inbox? 清校准+重试
L1123 ─ _thread_title_from_xml()        ← 🚨 这里返回空
L1125 ─ _thread_title_from_vision()     ← 🚨 这里也返回空
L1128 ─ search_on_thread_title_missing  ← fallback 搜索
L1150 ─ not_in_thread_after_tap → 放弃 ← 🚨 当前卡在这里
L1166 ─ wrong-chat 检测 (仅公式 tap)
L1192 ─ title 修正 (所有 tap)
L1241 ─ pre_thread_self_xml_guard
L1253 ─ combined_vision (线程分析)
L1294 ─ bubble_detector 交叉验证
L1313 ─ XML self-sent guard + bubble 验证
L1341 ─ system_event_skip
L1353 ─ peer_retry
L1395 ─ post-retry XML guard + bubble 验证
L1508 ─ fingerprint 去重
L1527 ─ reply_cooldown_skip
L1580 ─ escalation
L2224 ─ 发送回复
L2264 ─ update_chat_state
L2273 ─ post-send 冷却
```

### 5.5 配置项参考

配置文件路径（通常）：项目根目录的 `config.yaml`，`messenger_rpa` section。

```yaml
messenger_rpa:
  use_ui_hierarchy_tap: true          # 用 XML bounds 点击（推荐 true）
  require_thread_title_before_reply: true  # 必须确认线程标题才发送
  thread_title_vision_fallback: true  # XML 失败后用 Vision
  search_on_thread_title_missing: true  # Vision 也失败后用搜索
  bubble_detector_enabled: true       # 像素气泡检测
  pre_thread_self_xml_guard: true     # 进线程后 XML 自发检测
  calib_selfheal: true                # 校准自愈
  self_skip_cooldown_sec: 30          # 自发跳过冷却（测试模式）
  post_send_cooldown_sec: 120         # 发送后冷却
  wrong_chat_cooldown_sec: 60         # 错误聊天冷却
  spam_cooldown_sec: 3600             # 垃圾跳过冷却
  stale_peer_cooldown_sec: 90         # 陈旧对方冷却
  peer_retry_max: 2                   # peer 消息重试次数
  ui_dump_timeout_s: 6.0              # UI dump 超时
```

### 5.6 测试用的文件

| 文件 | 用途 |
|------|------|
| `tmp_screenshots/thread_ui.xml` | 线程页面的 UI XML dump（**用这个诊断 P0**） |
| `tmp_screenshots/test_thread.png` | 线程截图（已用于测试 bubble_detector） |
| `tmp_screenshots/current_live.png` | 最新截图（确认线程已打开） |

---

## 六、启动/重启系统的方法

```powershell
# 停止
Get-Process -Name python -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -match "main.py" } |
  Stop-Process -Force

# 清除缓存
Remove-Item "d:\workspace\telegram-mtproto-ai\src\integrations\messenger_rpa\__pycache__\runner*" -Force

# 启动（后台）
Start-Process -FilePath "python" -ArgumentList "main.py" `
  -WorkingDirectory "d:\workspace\telegram-mtproto-ai" -WindowStyle Hidden

# 编译检查
python -m py_compile "src\integrations\messenger_rpa\runner.py"
python -m py_compile "src\integrations\messenger_rpa\bubble_detector.py"
```

### 监控日志

```powershell
# 实时监控关键事件
Get-Content "logs\app.log" -Tail 50 -Wait |
  Select-String "run_once 结束|tap chat|title 修正|发送成功|bubble|未确认"

# 查看最近的 run_once 结果
Get-Content "logs\app.log" -Tail 100 |
  Select-String "run_once 结束" | Select-Object -Last 10
```

---

## 七、总结

| 阶段 | 状态 | 说明 |
|------|------|------|
| Phase 1: 精准定位+回滚 | ✅ 完成 | UI XML 匹配重写、wrong-chat 回滚、冷却分层 |
| Phase 2.1: title 修正拆分 | ✅ 完成 | chat_key 去重修复 |
| Phase 2.2: post-send 冷却 | ✅ 完成 | 生产验证通过（日志 `→ post-send cooldown 120s`） |
| Phase 2.3: bubble 检测器 | ✅ 完成 | 单元测试通过，集成进 P16-B 联合判定 |
| Phase 2.4: `_finish` 日志 | ✅ 完成 | 所有出口可见 |
| Phase 2.5: 线程标题提取 | ✅ 完成 | `find_thread_title()` + title 修正链路跑通，日志可见 `vision='野木' → xml='野末'` 等 |
| Phase P15/P16: self_overlap | ✅ 完成 | 见下方 § 八 |
| Phase 3: Vision 管线统一 | ⏳ 未开始 | |
| Phase 3: 操作指标 | 🟡 部分 | `metrics.py` 守卫 reason 已加 |

---

## 八、P16 反空转三层守卫（2026-05-04）

### 8.1 解决问题

`vision_misread_self_as_peer overlap=1.00` 在单 chat 反复触发：14:29、14:32、14:34、14:45、14:46、14:50 全是 `Maipon Senda` 同样的内容（`'I am heading home for now'` / `'I like movies that are easygoing too...'` 等），每次浪费 100~200s。

### 8.2 根因

| 层级 | 缺陷 | 位置 |
|------|------|------|
| 检测语义 | `self_overlap_strict_window_sec` 默认 180s 与 `post_send_cooldown_sec=120s` 不一致，发送后 60s 必空转 | runner.py L2843 |
| 信号融合 | bubble_detector 在 L2569 已得 self/peer/unknown，但 L2841 的 overlap 决策完全没用 | runner.py L2802~2875 |
| 反空转 | 缺"同 chat 反复 skip 计数 + 长冷却"，单 chat 可无限轮空转 | runner.py L2862 退出处 |

### 8.3 修复（P16，runner.py 当前编号见 git diff）

```text
__init__ 新增三个状态字段：
  _skipped_peer_text_per_chat: Dict[chat_key → deque(maxlen=5) of str]
  _self_overlap_skip_streak:   Dict[chat_key → int]
  _chat_overlap_skip_until:    Dict[chat_key → monotonic_until]

`if peer_msg.kind == "text":` 内三层防御（按顺序）：
  D 层 — 短路：peer_msg.content 命中已 skip 文本指纹 → 立即 skip
  D 层 — chat 级冷却：_chat_overlap_skip_until 覆盖期内 → 立即 skip
  B 层 — 联合：bubble_sender == "self" + overlap ≥ 0.7 → skip 不试 promote
  C 层 — 反空转：self_message_skip 退出时 streak += 1，
                 streak ≥ self_overlap_streak_threshold (默认 3) →
                 _chat_overlap_skip_until = monotonic + self_overlap_long_cooldown_sec
                 (默认 600s)，并重置 streak

参数对齐：
  self_overlap_strict_window_sec 默认 180 → 120（与 post_send_cooldown_sec 一致）
```

### 8.4 新增配置项

```yaml
messenger_rpa:
  self_overlap_strict_window_sec: 120        # 默认从 180 降到 120
  self_overlap_streak_threshold: 3            # 连续 N 次 skip 触发长冷却
  self_overlap_long_cooldown_sec: 600         # chat 级长冷却时长
```

### 8.5 日志关键字（监控用）

```
skipped_peer_text_short_circuit         # D 层短路触发
chat_overlap_skip_cooldown              # chat 级冷却覆盖期
bubble_self_confirms_overlap            # B 层联合判定触发
chat_overlap_long_cooldown:600s:streak=3 # C 层长冷却设置
self_overlap_skip_streak                # result 字段，可看板聚合
```

### 8.6 测试

`tests/test_messenger_runner_self_overlap.py` 加 4 个 P16 单测：
- `test_p16_skipped_text_short_circuit_dedup`：D 层指纹去重
- `test_p16_streak_counter_triggers_long_cooldown`：C 层 streak → 长冷却
- `test_p16_runner_init_has_overlap_fields`：__init__ 字段就位
- `test_p16_bubble_self_strong_signal_skips_promote`：B 层信号

冒烟 `python -m pytest tests/test_messenger_runner_self_overlap.py -q` 应 11 通过。

---

**给接手 AI 的一句话**：P0 已修复，P15+P16 self_overlap 守卫已完成。当前重点已从"能不能发"转移到"vision 误读时反空转的成本控制"。下一阶段建议见 § 九（如有补充）。
