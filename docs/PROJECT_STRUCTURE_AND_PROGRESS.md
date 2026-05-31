# 项目结构、功能与进度

项目：`telegram-mtproto-ai`
定位：多平台 AI 客服、统一收件箱、RPA 承接、客户旅程与转人工主系统。

## 一、当前体积

清理后项目约 `470 MB`。主要体积来自：

```text
models/        354 MB   本地 Whisper/语音模型资产，不进 GitHub
config/         50 MB   本地运行数据库和 runtime 配置
src/            13 MB   源码
tests/          10 MB   自动化测试
docs/            4 MB   文档
```

已移出的大文件在：

```text
D:\workspace\_cleanup_quarantine_20260531\telegram-mtproto-ai
```

## 二、目录树

```text
telegram-mtproto-ai
├─ config/                  # 配置模板、规则、运行态 DB；真实运行文件不上传
├─ docker/                  # HA/容器化相关配置
├─ docs/                    # 架构、部署、功能、业务和运维文档
│  └─ training/             # 培训材料
├─ domains/                 # 多业务领域 persona、KB、prompt、hooks
│  ├─ community/
│  ├─ conversion/
│  ├─ crypto/
│  ├─ ecommerce/
│  ├─ education/
│  ├─ general/
│  ├─ it_helpdesk/
│  ├─ legal/
│  └─ payment/
├─ models/                  # 本地模型资产，建议不进 GitHub
├─ scripts/                 # 运维/生成/辅助脚本
├─ sessions/                # Telegram session 占位；真实 session 不上传
├─ src/
│  ├─ ai/                   # AI client、TTS、persona voice、faceswap 接口
│  ├─ client/               # Telegram client、sender、账号注册
│  ├─ contacts/             # Contact/Journey/Handoff/KPI/重逢提示
│  ├─ integrations/         # LINE/Messenger/WhatsApp/RPA shared
│  ├─ monitoring/           # 监控与指标
│  ├─ shared/               # 共享设备注册等基础模块
│  ├─ skills/               # 回复技能、情感增强、关系分层
│  ├─ utils/                # 配置、缓存、画像、上下文、策略工具
│  └─ web/                  # Web 后台路由与模板
├─ tests/                   # 回归测试和接口测试
└─ tools/                   # 设备、KB、语音、壁纸、账号等工具脚本
```

## 三、开发功能进度

| 模块 | 状态 | 说明 |
|---|---|---|
| Telegram MTProto 基础客户端 | 已实现 | `src/client/telegram_client.py`、`main.py` |
| LINE RPA | 已实现并扩展 | 发送队列、导航、状态存储、pending 审核 |
| Messenger RPA | 已实现并持续优化 | runner、视觉识别、线程操作、语音抓取、状态存储 |
| WhatsApp RPA | 新增/进行中 | `src/integrations/whatsapp_rpa/`，含语言识别、媒体视觉、语音发送 |
| 统一收件箱 | 新增/进行中 | `src/web/routes/unified_inbox_routes.py`、`unified_inbox.html` |
| RPA 总览后台 | 新增/进行中 | `rpa_overview_routes.py`、`rpa_overview.html` |
| Persona 管理 | 已实现并扩展 | 多层 persona、批量绑定、标签搜索、持久化测试 |
| 情感陪伴/关系分层 | 已实现并扩展 | intimacy、relationship stager、reunion prompts |
| 语音/TTS 工作流 | 新增/进行中 | persona voice、voice routes、admin tts dashboard |
| KPI/草稿评估 | 新增/进行中 | `draft_eval.py`、`kpi_alerting.py`、相关测试 |
| 多领域知识库 | 已实现 | `domains/` 按领域拆分，支持 KB seeds/prompts/hooks |
| 测试覆盖 | 较丰富 | 新增大量 `tests/test_*`，覆盖 RPA、persona、funnel、voice、KPI |

## 四、当前风险

1. `config/bindings_runtime.yaml`、`config/profiles_runtime.yaml` 是运行态配置，当前未提交，需确认是否要抽成 `.example`。
2. `models/` 仍占 354 MB；开发机可保留，仓库不要上传。
3. `config/` 内有本地 DB/WAL 文件，已被 `.gitignore` 忽略；不要手动 `git add -f`。
4. 全量测试较重，可能因 RPA/外设/服务依赖超时。提交前可优先跑模块级测试。

## 五、建议下一步

1. 把 `bindings_runtime.yaml`、`profiles_runtime.yaml` 拆成 `*.example.yaml`，真实文件继续本地保留。
2. 为 WhatsApp/统一收件箱写一份操作员手册。
3. 把 `models/` 移出仓库目录，用 `MODEL_CACHE_DIR` 之类环境变量定位。
4. 建立定期清理任务：清理 `tmp_*`、`logs/`、语音输出和截图回放。
