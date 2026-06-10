# AI 客服桌面客户端（多平台）

**Electron 桌面壳**，两条并行能力：

1. **统一收件箱（默认首屏，📥）**：内嵌后台 `/workspace`，与网页后台**同源同款**——
   Telegram / WhatsApp / Messenger / LINE / Web 全部聚合在一个工作台里（会话列表、AI 草稿、
   copilot、翻译都和后台完全一致）。**这是 WhatsApp/Messenger/LINE 聊天的入口。**
2. **内嵌官方网页（按账号 Tab）**：把各平台**官方 web 客户端**嵌进 `<webview>` + 注入
   「点击翻译 / 智能回复」+ 右侧 copilot 边栏。目前 Telegram 最完整，WhatsApp 为 beta。

翻译与 AI 全部走本仓库已有的 FastAPI 后端。

## 为什么是桌面壳（而非浏览器后台）

浏览器里无法 iframe + 注入 `web.telegram.org`（X-Frame-Options + 同源策略）。
桌面 Electron 的 `<webview>` + preload 可以注入官方页面，且「官方网页端所有功能」白嫖——
跑的就是官方客户端本体，封号风险最低。

## 架构

```
rail 标签：
  [📥 统一收件箱]  ── webview ── 后台 /workspace（session cookie 鉴权，token 自动登录）
                                  └─ 多平台聚合工作台（=网页后台，全功能）
  [✈️ Telegram 账号] ── webview ── web.telegram.org
       │                           └─ preload: inject/tg-inject.js（点击翻译/智能回复 + 注入状态条）
  [➕ 新增]          ── 运行时新增内嵌账号（选平台→起独立 partition webview→标签内扫码），持久化+重启自动重建
       │ ipcRenderer.invoke
       ▼
  main.js (Node 主进程)             ← 规避 webview 跨域/混合内容
        │ fetch (Bearer token)
        ▼
  本仓库 FastAPI 后端
    GET  /workspace                      （统一收件箱页，内嵌）
    POST /login (auth_token / user+pass)  （收件箱 webview 凭据链自动登录）
    GET  /login                          （主进程健康探针：可达即自动重连）
    POST /api/unified-inbox/translate            （文本翻译）
    POST /api/unified-inbox/translate-image|voice （图片 OCR / 语音转写 → 翻译）
    POST /api/desktop/smart-reply                （上下文智能回复）
```

- **统一收件箱**：固定 id `__inbox__`，独立分区 `persist:backend-workspace`。落到 `/login`
  时在页面内 POST 凭据自动登录回跳——**凭据链**优先 `backend.token`，回退 `backend.user`/`backend.pass`
  （token 为空/失效时自动接力，全部失败才露出登录页人工处理）。带 loading/error 遮罩；
  **后端未起会自动重连**（主进程 `desktop:backend-health` 探活，可达即自动重载，先开桌面后开后端也能自愈）。
  开关：`config.json::unified_inbox.enabled`（默认 `true`，可改 `label`/`path`）；
  `unified_inbox.lang`（如 `zh`/`en`，空=跟随后台）会以 `?lang=` 注入并贯穿登录回跳，**坐席界面语言对齐**。
- **内嵌官方网页多账号**：每个平台/账号一个 Electron `partition`（独立 cookie/storage），
  可在 `config.json` 的 `accounts[].proxy` 配独立代理（防关联）。除 `config.json` 静态配置外，
  rail 底部「➕新增」可**运行时新增**（免改配置免重启），账号持久化到 `localStorage`、重启自动重建。

## 运行

1. 先确保本仓库后端在跑：仓库根目录 `python main.py`（Web 在 `127.0.0.1:18787`）。
2. 配 `config.json`：`backend.base_url` / `backend.token`（默认与 `config.yaml::web_admin.auth_token` 一致，当前为 `admin`）。
3. 装依赖并启动：

```bash
cd desktop
npm install
npm start        # 或 npm run dev 打开 DevTools
```

4. 启动后默认停在 **📥 统一收件箱**（=后台 `/workspace`），WhatsApp/Messenger/LINE/Telegram
   聊天都在这里，和网页后台完全一致。后端未启动时会显示「重试连接」。
5. 切到 **✈️ Telegram 账号** Tab 用手机扫码登录（官方流程）；每条消息下出现「点击翻译」，
   右下角「🤖 智能回复」读最近对话→草拟回复填进输入框，右侧 copilot 边栏提供草稿/洞察/客户档案。

## 端到端验收

**后端契约（自动化，CI 守护）**：`tests/test_desktop_integration_contract.py`
锁死桌面收件箱依赖的后端契约（健康探针 / token 自动登录 / 深链入口 / `?lang=` / chats 形状）。
改后端前后跑：`python -m pytest tests/test_desktop_integration_contract.py -q`。

**桌面纯函数（自动化）**：注入诊断映射 `deriveInjectState` 的单测，跑 `npm --prefix desktop test`。

**桌面 GUI（手动，前端 webview 无法 headless）**：

1. 后端未起就启动桌面 → 收件箱显示「正在等待后台启动并自动重连…」；再起后端 → **自动连上**（无需点重试）。
2. 收件箱首屏不出现 `/login` 闪屏，直接进 `/workspace`（token 自动登录生效）。
3. 左侧 rail：Telegram/WhatsApp 为内嵌 Tab；若配了 Messenger/LINE 账号，显示「↪收件箱」dim 入口，点击切到收件箱（不开死页）。
4. 切到 Telegram Tab 扫码登录 → 消息下出现「点击翻译」、右下角「🤖 智能回复」可用。
5. Telegram Tab 选中一个会话 → 右栏头部 📥「在收件箱打开」→ 切到收件箱并定位到**同一会话**。
6. rail 底部「➕新增」→ 选 Telegram/WhatsApp → 立刻出现新内嵌 Tab 并激活，可在其中扫码登录第二个号。
7. hover 运行时新增的 Tab 右上角「✕」→ 移除该 Tab；**重启桌面**后第 6 步新增的号仍在（且免重新扫码）。
8. 切换到任一内嵌平台 Tab：右上角出现注入状态药丸（绿=正常 / 黄=失配 / 灰=未登录）。
9. 会话里收到**图片** → 气泡出现「🖼 翻译图片」→ 点击 → 气泡内显示「[图片原文] …＋译文」
   （需后端 `config.vision.enabled`；未启用则按钮处回显原因）。语音同理「🎤 翻译语音」。
6. `config.json::backend.token` 改错、填对 `user/pass` → 仍能自动登录（凭据链回退）；两者都错 → 露出登录页可人工登录。

## 选择器调优

`inject/tg-inject.js` 顶部 `PROFILES` 按各平台当前 DOM 给的选择器档案（telegram / whatsapp）。
官方改版后类名会变，若按钮不出现/抓不到文本，F12 看真实 DOM 后改对应档案的
`bubble / bubbleText / composer / sendBtn` 即可（WhatsApp 已是多候选，可继续追加）。
新增可内嵌平台时：在档案里加 `supported:true`，并同步 `renderer.js::EMBEDDABLE`。

## 语音克隆 / 发送（与统一收件箱对齐）

- **📥 统一收件箱标签**：内嵌 `/workspace`，与浏览器后台**完全同款**（横向工具栏 + 录入面板，见截图）。
- **内嵌 Telegram/WhatsApp + 右侧业务助手**：「💬 回复」页新增 **🎙️ 语音克隆 / 发送**（共享组件 `cp-voice`，同源 API）。
  - 生成草稿后点「填入」→ 语音区自动带上文字 → 🎙️ 试听 → 📨 发送。
  - 🎚️ 录入 / 对账 / 改绑与后台一致；侧栏为纵向布局（空间较窄）。
  - 需要与后台**一模一样横向布局**时，用 rail 头 **📥** 或 copilot 头 **📥 在统一收件箱打开**。

## 路线（后续）

- 收件箱**深色主题**：`/workspace` 现为硬编码浅色（含大量硬编码色值，且与浏览器后台共用），
  整页深色化成本/风险高，暂缓；当前已统一为「深色 rail + 浅色内容」与壳一致，遮罩也已浅色化对齐
- 账号面板（后端注册表）登录的 protocol/web/device 号 → 走**服务端/真机**会话，统一进
  「统一收件箱」（无需桌面 webview）；与下方「内嵌网页账号」是两条独立登录链路
- 自动翻译开关（`config.translate.auto`，已预留；当前仅文本随 auto 自动翻，媒体按需点击）
- 指纹注入（UA/时区/WebGL，接 M4 指纹生成）

> 已落地（注入诊断条）：inject 经 `sendToHost("inject-status", …)` 周期上报
> `{supported, composer, bubbles, chatOpen}`；renderer 在平台 Tab 右上角显示状态药丸
> （注入正常/选择器失配/未登录），纯映射函数 `renderer/inject-status.js::deriveInjectState`
> 已抽成可单测单元，跑 `npm --prefix desktop test`（或 `cd desktop && npm test`）。

> 已落地（rail 入口治理）：可内嵌平台由 `renderer.js::EMBEDDABLE` 统一裁决（须与
> `inject/tg-inject.js::PROFILES[*].supported` 一致）。Messenger/LINE 无官方网页版聊天，
> rail 上以「↪收件箱」dim 入口呈现，点击切到统一收件箱，**不再开内嵌死页**。
> WhatsApp 注入选择器已做多候选/多语言加固（composer/sendBtn/text/peerName，只增不删）。

> **WhatsApp 内嵌浏览器兼容（UA 伪装）**：WhatsApp Web 会拒载含 `Electron` 的
> User-Agent（常误报「需要 Chrome 85+」）。桌面端对 WhatsApp webview 在 **partition 级 +
> webview 标签级** 伪装为与内置 Chromium 同版本的 Chrome UA（见 `desktop/webview-ua.js`）。
> 若仍见旧拒载页：移除该 WhatsApp 标签后重新「➕新增」（旧 `persist:` 分区可能缓存了失败页），
> 或菜单「强制重新加载」。WhatsApp 仍可能加强检测，内嵌方案不保证长期有效。

> 已落地（运行时新增内嵌账号 · 免改配置免重启）：rail 底部「➕新增」→ 选平台
> （Telegram/WhatsApp）→ 即时起一个独立 `partition=persist:<id>` 的内嵌 webview，
> 在标签内扫码登录即可多开同平台多号。运行时账号持久化到 `localStorage`，**重启后自动重建**
> （partition 不变 → 登录态续上）；hover 标签右上角「✕」可移除（仅运行时账号，config.json
> 定义的账号交配置管理）。纯逻辑 `renderer/account-utils.js`（merge/build/serialize）已抽成
> 可单测单元，覆盖于 `npm --prefix desktop test`。
>
> 说明：桌面「内嵌网页账号」（在 webview 内扫码）与后端账号面板登录（protocol/web/device →
> 统一收件箱）是**两条独立链路**——后端模式无一对应"桌面托管官方网页版"，故 3D 落在桌面侧自管，
> 而非把面板登录硬接成 webview（避免给协议号开出第二个空会话）。

> 已落地（媒体翻译）：内嵌气泡里**纯图片**显示「🖼 翻译图片」、**语音**显示「🎤 翻译语音」，
> 点击后 inject 取媒体字节（blob: 同源 fetch）→ 转 base64 → 主进程 POST
> `/api/unified-inbox/translate-image|voice`（Bearer）→ 气泡内双行渲染「原文(OCR/转写) + 译文」。
> 媒体识别按需点击触发（不随 `translate.auto` 自动跑，省算力）。结果归一逻辑
> `inject/media-format.js::formatMediaResult` 已抽成可单测单元（覆盖于 `npm --prefix desktop test`）。
> **后端前置**：图片需 `config.vision.enabled`（Ollama/智谱视觉后端），语音需
> `config.audio_pipeline.enabled`（faster-whisper/在线 ASR）；未启用时按钮处直接回显后端原因。
> 媒体选择器（`PROFILES[*].media`）为 best-effort：Telegram `.media-photo`/`audio[src]`、
> WhatsApp `img[blob:]`/`audio[src]`，官方改版后需按 F12 现场校准。
