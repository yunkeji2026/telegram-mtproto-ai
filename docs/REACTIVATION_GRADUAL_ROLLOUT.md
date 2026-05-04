# Reactivation 灰度启用 SOP

> **目的**：把 W2 阶段已经做好的"主动唤醒"功能从 enabled=false 安全推到真发。
> **风险**：reactivation 是 AI 自动选人 + LLM 自动写词 + RPA 自动发出去。任何环节出错都直接打到真用户脸上。
> **原则**：永远先 dry_run，永远先小流量，永远先看 metrics。

---

## 前置确认

```bash
# 1) 跑一次 intimacy migration（W3-D2.1 修复 bug 后必跑一次）
python scripts/migrate_intimacy_v1.py --dry-run    # 看会改什么
python scripts/migrate_intimacy_v1.py              # 真写

# 2) contacts.db 里有几个候选？少于 3 个不必启用
# 注意陪护语义已经把 ENGAGED 也算候选（W2-D7.6）
sqlite3 config/contacts.db "
  SELECT COUNT(*) FROM journeys
  WHERE funnel_stage IN ('ENGAGED','LINE_ENGAGED','BONDED','LINE_ACCEPTED')
    AND intimacy_score >= 40
    AND updated_at < strftime('%s','now') - 3*86400"

# 3) episodic memory 覆盖率（reactivation 默认 skip_if_no_episodic=true，
#    没 portrait 的 journey 不会发主动消息）
sqlite3 config/contacts.db "
  SELECT COUNT(*) FROM journeys
  WHERE COALESCE(LENGTH(context_snapshot_json),0) > 50"

# 4) reactivation 配置确认（默认 enabled=false / dry_run=true）
grep -A 10 "^reactivation:" config/config.yaml
```

**候选 0 怎么办**：
- 数据太新（updated_at 都在 3 天内）→ 等
- intimacy 都 < 40 → 调 contacts.min_intimacy_for_reactivation 到 30 临时验证
- portrait 都缺 → 让 messenger RPA 多跑几天累计 inbound（≥ 2 条触发首次抽）

候选 0 时不要启动 — 没数据看不出 LLM 效果，浪费 token 也得不到结论。

---

## 30 分钟快速验证（开 dry_run 看 loop 是不是真跑）

如果只是想验证整个链路工作，**不打算长期灰度**，做这个：

```yaml
# config/config.yaml
reactivation:
  enabled: true
  dry_run: true                    # 只生成 + log + metrics，不真发
  interval_sec: 60                 # ★ 临时 60 秒一次（默认 600）
  max_per_tick: 1                  # ★ 临时 1 条（默认 3）
  first_run_grace_minutes: 0       # ★ 不要宽限，立即跑
  skip_if_no_episodic: false       # ★ 临时关掉，让没 portrait 的也生成
```

重启程序，等 5-10 分钟。

**期望**：
1. `tail -f logs/app.log | grep "reactivation"` 看到调度日志
2. 浏览器 http://localhost:18787 → 滚到 dashboard 底部 "待审核话术" 面板 → 看到 LLM 生成的话术
3. `curl -H "Authorization: Bearer admin" http://127.0.0.1:18787/api/bot-metrics | jq .reactivation` → `last_run_ts > 0` + `dry_run_1h > 0`

**验证完后**改回保守值（interval_sec=600 / max_per_tick=3 / skip_if_no_episodic=true）再进阶段 1。

---

## 阶段 1：dry_run 观察（48 小时）

**改 `config/config.yaml`**：
```yaml
reactivation:
  enabled: true            # ← 改 true
  dry_run: true            # ← 保持 true
  interval_sec: 600
  max_per_tick: 3
  first_run_grace_minutes: 60
  first_run_max_per_tick: 1
```

重启程序：
```powershell
# 找 PID 杀掉
Stop-Process -Id (Get-NetTCPConnection -LocalPort 18787).OwningProcess -Force
python main.py
```

**观察点**：
1. 启动日志看到：`✅ reactivation_loop 已启动（interval=600s max_per_tick=3）`
2. 30 分钟后看一次：
   ```bash
   curl -s -H "Authorization: Bearer admin" \
     http://127.0.0.1:18787/api/reactivation/dry-run-samples | jq
   ```
3. dashboard 看 `bm-companion` 卡片：dry_run_1h 应该 > 0；候选数有显示

**通过条件**（48h 内）：
- 至少 5-10 条 dry_run 样本
- 抽 10 条 reply_text 人工审核：
  - 是否引用了 episodic memory 里具体的事？（不是"在吗"）
  - 是否符合 messenger style_hint（不是客服腔）
  - 是否不出戏（不说"我是 AI"）
  - 是否符合对方的语言（ja → 日文回；不要中文回日本人）

**审核流程**：每条样本通过 API 标 like / dislike：
```bash
# like
curl -X POST -H "Authorization: Bearer admin" -H "Content-Type: application/json" \
  -d '{"sample_ts":1234567890.0,"verdict":"like"}' \
  http://127.0.0.1:18787/api/reactivation/dry-run-feedback

# dislike（话术不行 → 自动加入内存黑名单，下次类似的会重生成）
curl -X POST -H "Authorization: Bearer admin" -H "Content-Type: application/json" \
  -d '{"sample_ts":1234567890.0,"verdict":"dislike","reason":"too generic"}' \
  http://127.0.0.1:18787/api/reactivation/dry-run-feedback
```

**驳回条件**（任一即停）：
- 出戏率 > 10%（10 条里 > 1 条说"作为 AI"）
- 客服腔 > 20%（"请问有什么需要"这种）
- 语言错配 > 10%（日文用户回中文）
- 引用 episodic 不准确（"上次你说面试" 但实际从来没聊过面试）

驳回了改 prompt，回阶段 1 重跑，**不要直接进阶段 2**。

---

## 阶段 2：第一次真发（极保守，1-2 天）

通过阶段 1 后改 config：
```yaml
reactivation:
  enabled: true
  dry_run: false                     # ← 改 false 真发
  interval_sec: 600
  max_per_tick: 1                    # ← 临时收到 1
  first_run_grace_minutes: 120       # ← 宽限期 2h（不是 1h）
  first_run_max_per_tick: 1
```

重启。

**观察点**：
- dashboard `bm-companion` 卡片 → `主动唤醒` 行
  - `1h 调度` 数 > 0 但 < 5
  - `dry_run_1h` 应该归 0（已经不 dry 了）
- 24 小时后看 `24h 回复率`：
  ```
  re.response_stats.response_rate_pct >= 30%  → OK
  10-30%                                       → 观察，可能话术不够好
  < 10%                                        → 立即停（dry_run=true）调话术
  ```
- 看 `分层 高/中/低`：高 intimacy 应该回得多；如果三层差不多 → reactivation 可能没差异化效应

---

## 阶段 3：放量（5-7 天）

阶段 2 跑一周回复率稳定在 25%+ 后：
```yaml
reactivation:
  max_per_tick: 3                    # ← 恢复默认
  first_run_grace_minutes: 60
  # 其他不变
```

继续观察 7 天回复率趋势。如果连续 3 天 > 30% → 进阶段 4（话术 A/B）。

---

## 阶段 4（W3+）：话术 A/B 优化

W3 实施。基础设施已就位：
- `reactivation.persona_variants:` 配置多套话术
- 同 chat sticky 分配 variant
- `feedback_1h.like / dislike` 回报每个 variant 效果
- 自动 winner 选择（参考 messenger persona_experiment 设计）

---

## 紧急停止

```yaml
reactivation:
  enabled: false       # 立刻停
```

或者运行时 API（如果加了，W3 任务）。

---

## 常见问题

**Q: 启用后没看到 dry_run 样本**
检查：候选数 = 0？interval 太长？loop 报错？
```bash
grep -i "reactivation" logs/app.log | tail -30
```

**Q: 样本里 LLM 说"我是一个 AI 助手"**
出戏检测已经会拦截，但是个信号 — style_hint 不够强。修 `reactivation_loop.py:_REACTIVATION_PROMPT` 加强"不要说 AI / 助手 / 模型"约束。

**Q: 看到生成的 reply 引用了从来没聊过的事**
LLM 在编造 episodic。两种原因：
1. portrait_extractor 写错了 → 看 `journeys.context_snapshot_json` 内容
2. prompt 里 episodic 字段缺 → 检查 portrait_block 在不在

**Q: 24h 回复率持续 < 10%**
不是话术问题就是时机问题。试：
- 改 `delay = random.uniform(...)` 让发送在用户最活跃时段
- 缩小候选范围（`min_intimacy_for_reactivation` 调到 60）
