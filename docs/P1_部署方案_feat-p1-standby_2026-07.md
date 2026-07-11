# feat-p1-standby 部署方案与冲突面分析（2026-07-12）

> 建档 2026-07-12 00:xx。目的：把本轮攒的 P0 全集成 + P1 能力（AI 值守 / 桌面首启向导 /
> 试用额度 / 资产养号包装 / 翻译置信度 / send-gate 持久化 / canary 接线 / 发送安全视图 /
> flaky 修复）从隔离分支 `feat-p1-standby` 落到**正在自动发送的线上活树**。
>
> **核心结论（实测得出，修正了先前「直接合并分支」的判断）**：
> **禁止 `git merge feat-p1-standby → main` 式整批部署。** 线上活树 `d:/workspace/telegram-mtproto-ai`
> 处于 `main(daddb72) + 162 个未提交文件`（87 改 + 75 新）的状态,而分支基于**旧基线 daddb72**
> 构建,不含这 162 个在途改动。整批合并/checkout 会冲突或**清掉团队在途工作**（不可逆,且线上在真发）。

---

## 1. 实测冲突面（分支 vs 活树,2026-07-12）

分支相对 daddb72 改 **136** 文件;活树脏 **162**（87 tracked + 75 untracked）。交叉分类：

| 类别 | 数量 | 处置 |
|---|---|---|
| **已同步**（本会话已热拷贝到活树,live==分支） | 5 | ✅ 已上线：`send_guard.py`/`protocol_autoreply_limits.py`/`send_health.py`/`scripts/send_health_report.py`/`tests/test_workspace_merge.py` |
| **新增文件**（活树不存在,零冲突可加） | 30 | 可安全新增,但多数**运行时依赖**下方冲突文件,单独加=悬空代码 |
| **干净覆盖**（活树==daddb72,分支从基线改） | 37 | 可安全覆盖,但同样多数依赖冲突文件才生效 |
| **真冲突**（分支改 & 活树也脏,需三方合并） | 64（5 已同步除外） | ⚠️ 其中 ~14 核心 src/test + ~50 website |

### 1.1 两个「枢纽冲突文件」——一切 P1 UI/i18n 都过它俩

| 文件 | 活树vs分支差异 | 承载 |
|---|---|---|
| `src/web/web_i18n.py` | +208 / -208 | AI 值守键×26 + 首启向导 `setup.ai` 键×30 + 试用/养号/翻译置信度等**所有新前端键** |
| `src/web/templates/unified_inbox.html` | +200 / -382 | AI 值守三档 UI（`setStandbyMode`/`standby-bar` 共 10 处）+ 翻译置信度徽标 + 语音一键 + composer 改动 |

**含义**：桌面向导、AI 值守、翻译置信度、试用额度 UI 等**没有一个**能绕开这两个文件。两者与活树在途改动都冲突 → **必须先三方reconcile 这两个文件**,否则任何 P1 前端功能落地即残缺（`window.T` 裸键 / 哑按钮 / 门禁红）。

### 1.2 其余核心冲突（各需三方合并,均有活树在途改动叠加）

`translation_service.py`(+83/-15) · `store.py`(+22/-32) · `ops_overview.py`(+7/-90) ·
`ops_overview_routes.py` · `ops_overview.html`(+6/-328) · `unified_inbox_account_routes.py`(+25/-103) ·
`unified_inbox_send_routes.py`(+70/-11) · `unified_inbox_workspace_pages_routes.py` ·
`tests/test_admin_route_inventory.py` · `tests/test_ops_overview.py` · `tests/test_send_path_audit.py`

### 1.3 website（~50 文件冲突）——单独处理,勿强推

分支的 website 来自 agent D 导入的**活树在途 website 快照**（commit `15999af`）+ asset-safe/nurture
两页。活树 website 之后可能又动过（`landingContent.ts` 差 292 行）。**这是最纠结的一块,应由 website
负责人单独合并 2 个新落地页 + metrics JSON,不要用分支覆盖活树 website。**

---

## 2. 根因：真正的部署阻塞不是分支,是「活树 162 未提交」

- 活树没有干净基线 → git 无法区分「团队在途工作」与「我部署的改动」→ **回滚不可行**（出问题无法一键还原,而线上在真发）。
- 分支基线 daddb72 已被活树在途工作甩在身后 → 分支是「旧基线上加 P1」,与活树「新基线」错位。
- reconcile 冲突文件时,活树在途工作可能仍在变 → **移动靶,合完即过期**。

**结论**：先让活树 162 未提交工作**提交/收口**（团队负责人动作,非本 agent 可代劳）,确立干净基线,才谈得上安全的结构化部署 + 回滚。

---

## 3. 修正后的部署策略（分层,安全优先）

> 原则：已上线的安全件保持;新功能等干净基线后分层落;绝不在 162 未提交态上做大 checkout/merge。

### 阶段 0 —— 已完成（本轮热补,零冲突,已在线上跑）
- send-gate 计数持久化 + canary 接线 send_blocked + 发送安全视图 CLI + 3 flaky 测试修复。
- overlay `config.local.yaml`：`companion_send_gate.target_cap=80` + `ops.canary`(armed-off)。
- 现状：`git status` 活树多这 5 文件 + gitignored overlay/db,线上 PID 运行中,近 24h 零失败。

### 阶段 1 —— 前置（团队负责人,非本 agent）：**收口活树在途工作**
1. Review 活树 87 改 + 75 新,分主题提交到 main（或专门分支），确立干净基线 `main'`。
2. website 在途工作单独提交。

### 阶段 2 —— 分支 rebase 到干净基线 + reconcile 枢纽文件
1. `git rebase main'` （或新建 `feat-p1-standby-2` 从 `main'` cherry-pick 分支非冲突提交）。
2. 手工三方合并 `web_i18n.py` + `unified_inbox.html`（枢纽,最先做,合完全量回归 + 前端门禁）。
3. 依次 reconcile §1.2 的 ~12 核心文件,每个合完跑对应子集回归。
4. website 交负责人合 asset-safe/nurture + metrics JSON。

### 阶段 3 —— 低峰窗口正式部署 + 验证
1. 全量回归 `scripts/regression.ps1` 全绿 + 前端门禁全绿。
2. 低峰窗口（真发少）`restart_main_keep_tts.ps1` 重启。
3. 冒烟：web 端口探活 / TelegramClient 轮询 / `send_health_report` 队列级别 / 值守三档可切 / golive 绿。
4. 盯 30 分钟 autosend 失败率 + kill-switch 无触发。

### 回滚预案
- 有干净基线后：`git reset --hard main'` + 重启即回滚（阶段 1 的价值正在于此——**没有干净基线就没有回滚**）。
- overlay/db 层：`config.local.yaml` 可单独 revert `target_cap`;`account_sends.db` 删除即回退到无持久化（自动降级内存,不阻断发送）。

---

## 4. 立即可做 vs 必须等基线

| 能力 | 依赖枢纽冲突文件? | 现在能否安全上线 |
|---|---|---|
| send-gate 持久化 / canary / 安全视图 CLI | 否（已热补） | ✅ 已上线 |
| `gen-trust-metrics.py`（独立脚本） | 否 | ✅ 可单独加（新文件） |
| AI 值守三档 UI | 是（unified_inbox.html + web_i18n） | ❌ 等基线 reconcile |
| 桌面首启向导 | 是（web_i18n `setup.ai` 键） | ❌ 等基线 |
| 试用额度 / 翻译置信度 UI / 资产养号页 | 是 | ❌ 等基线 |

**判断**：除已上线的安全件与个别独立脚本外,其余 P1 前端功能**没有安全的部分部署路径**——强行分半落只会制造悬空代码 + 门禁红。价值兑现的前提是阶段 1（收口活树）。

---

*实测基于 2026-07-12 00:xx 的 `git diff`。活树状态随团队工作变动,执行阶段 2 前请重跑 §1 的交叉分析核对最新冲突面。*
