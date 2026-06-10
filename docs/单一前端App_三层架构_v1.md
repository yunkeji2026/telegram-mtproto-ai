# 单一前端 App · 三层架构 v1 —「一处开发,处处运行」

> 决策(2026-06-09):**B 单一前端 App + 多薄壳** / 基座 **Web Components + Vite** / 先上 **桌面 dev 直连后端 URL**。
> 目标:桌面/网页(及未来移动端、更多平台)**功能与 UI 全共用**,加新端=加薄壳,加新聊天平台=加适配器,均不碰业务 UI。

## 1. 三层分离

```
宿主壳 Shell(每端一薄层,只做端特有的事)
  · 网页 = 浏览器
  · 桌面 = Electron + 内嵌真·官方聊天网页 + inject(桌面独有价值,留在壳)
  · 移动 = Tauri/Capacitor/webview(未来)
─────────────────────────────────────────
共享前端 App(唯一一份:全部业务 UI + 交互 + 布局)
  · 设计系统 tokens(双主题) + Web Components 组件库
  · 只依赖 HostBridge,不知道自己在哪端跑
─────────────────────────────────────────
HostBridge(每端实现一份)
  · 数据:web/桌面 iframe = 同源 fetch;移动 = native
  · 能力:剪贴板 / 通知 / 内嵌 webview / 实时会话消息源
─────────────────────────────────────────
后端 FastAPI(同一个大脑,已共享)
```

## 2. 关键优化:桌面用「同源 iframe」加载单一 App

桌面业务面板做成 `<iframe src="http://127.0.0.1:18787/copilot/app.html">`:
- iframe origin = 后端 → 内部 `fetch('/api/..')` **同源直达,无需 IPC、无 CSP 问题**;
- 桌面与网页因此跑**同一份 `WebCopilotClient`**;
- 桌面独有的"实时 DOM 会话"只需由外层壳 `postMessage({type:'cp-context', conversationId})` 喂进 iframe。

鉴权:
- 网页(浏览器开 app.html)→ 同源 **session cookie** 自动带;
- 桌面 iframe → 壳在 URL `#token=` 注入,`CopilotShared.setAuthToken()` 让同一 WebClient 附 `Authorization: Bearer`。

这样 **IPC 适配器变为遗留**,长期可退役;桌面壳只保留"内嵌聊天网页 + inject + 把会话 postMessage 给 app.html"。

## 3. 排序约束(避免回退,极重要)

桌面**真正切到 iframe URL** 必须等 `app.html` 的能力**追平现 `renderer.js`**(草拟/分析/知识库/快捷回复/人设/语言/护栏/关系阶段…),否则切过去就丢功能。

因此路径是**渐进、可独立上线**:
1. 立起 `app.html` 单一 App 壳(本次:挂关系阶段)。
2. 以后**所有新面板只写进 shared 组件 + 装进 app.html**;网页模板与 app.html 同引,**天然两端共用**。
3. app.html 追平后,桌面壳一键把业务区换成 iframe(`business_ui.dev_url` 配置开关)。
4. 需要移动端时加 Tauri/Capacitor 薄壳 + HostBridge.mobile。

## 4. 基座与构建

- 基座:**Web Components**(贴近现状、CSP 友好、零运行时依赖)。
- 构建:**Vite 暂缓**——当前组件少、直接 `<script>` 引入即可;待组件规模/打包/HMR 收益显现再引入 Vite(届时 `app.html` 改为 Vite entry,产物仍挂 `/copilot`)。**不在收益出现前堆构建工具。**
- 单一来源:`shared/copilot/`(repo 根)。网页 `/copilot/*`;桌面 `copy-shared` 同步(切 iframe 后连复制都免了)。

## 5. "更多 APP" 如何被自然容纳

- **更多聊天平台**:加 inject profile(桌面 `PROFILES`)或后端 RPA/协议适配器,**UI 不动**。
- **更多客户端**:加一个薄壳 + 一份 HostBridge 实现,业务 UI 直接复用。

## 6. 本次落地(P2.5-seed)

- `shared/copilot/app.html`:单一 App 壳种子。`?theme=light|dark`、`?cid=`、`#token=`、`postMessage({type:'cp-context',conversationId})`;首挂 `<cp-rel-stage>`。
- `copilot-client.js`:`CopilotShared.setAuthToken(t)` + `_get/_post` 附带 token(同一 WebClient 跨端)。
- 验证:`/copilot/app.html` HTTP 200,浏览器直接可看关系阶段。

## 7. 下一步(P2)

- 把 NBA/剧本/注解/草稿/人设/语言/护栏逐个做成 shared 组件并**装进 app.html**(网页模板同引)。抽 `CpPanelBase` 基类去重。
- app.html 接近 parity 后:桌面壳加 `business_ui.dev_url` 开关 + `postMessage` 会话桥,灰度切 iframe。
- HostBridge 能力补全(copy/notify/openExternal/getLiveMessages),IPC 适配器逐步退役。

## 8. 风险

- **鉴权**:桌面 iframe 的 token 注入要安全(仅本地回环、随会话失效)。
- **parity 缺口**:切 iframe 前务必逐项核对功能,留回退开关。
- **防漂移**:业务 UI 一律进 `shared/`,禁止 renderer.js/Jinja 再各写一份。
