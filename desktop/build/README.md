# 桌面端打包（P0 本地自包含交付）

目标：让用户**双击安装、零 Python 依赖**即可运行——后端被打成 sidecar 二进制随包分发，
Electron 启动时自动拉起、退出时回收（见 `desktop/backend-launcher.js`）。

## 两步走

### ① 打后端 sidecar（PyInstaller）

在装好 `requirements.txt` 的同款 Python 环境、且**与目标 OS 一致**的机器上跑
（Windows 包在 Windows 上打，mac 包在 mac 上打；PyInstaller 不跨平台）：

```bash
cd desktop
pip install pyinstaller
npm run build:backend          # = python build/build_backend.py
```

产出：`desktop/build/backend-dist/backend(.exe)`（onedir，默认排除 whisper/torch 等重依赖控包体）。
需要本地 ASR/OCR 时加 `python build/build_backend.py --keep-heavy`。

### ② 打桌面安装包（electron-builder）

```bash
cd desktop
npm install
npm run dist:win        # 或 dist:mac / dist（当前平台）
```

`extraResources` 会把 `build/backend-dist/` 复制进安装包的 `resources/backend/`。
运行时 `backend-launcher.js` 在发布态优先用 `resources/backend/backend(.exe)`，
开发态回退系统 Python 跑仓库根 `main.py`。

## 运行时行为（生命周期）

- 启动：先探活 `backend.base_url/login`——**已在跑则复用、不重复拉起**（对「先手动起后端」零回归）；
  否则解析命令并 spawn，日志落 `userData/logs/backend.log`。
- 就绪门控：renderer 既有「正在连接后台→自动重连」遮罩自动衔接；
  `desktop:backend-spawn-status` 暴露细化状态（starting/ready/failed…）。
- 退出：`before-quit` 回收后端（Windows `taskkill /T /F`；posix 杀进程组）。
- 关闭自拉起：`config.json::backend.spawn.enabled=false`（完全由用户自管后端）。

## 可写数据目录（已实现）

发布态后端的 config + 运行数据**不写只读安装包**，而是落用户可写目录：

- launcher 在打包态把 `AITR_DATA_DIR=<userData>/data`、`AITR_CONFIG_PATH=<dataDir>/config/config.yaml`
  注入后端 env，并设后端 cwd=`<dataDir>`（使 `logs/`、`*.db`、tmp 等 cwd 相对路径也落可写区）。
- 后端 `ConfigManager` 识别这两个 env（见 `src/utils/config_manager.py`），**首次运行自播种**：
  桌面模式（`AITR_DESKTOP_MODE=1`）优先拷内置**最小种子** `config.desktop.min.yaml`
  （P0-1 A1：无 `YOUR_*` 占位、`translation.engines.order: ["ai"]`，只差首启向导补 AI Key），
  否则回落完整 `config.example.yaml`，落到 `<dataDir>/config/config.yaml`，无需 launcher 搬运。
- 开发态（无 env）行为完全不变（仍用仓库 `config/config.yaml`），零回归。
- PyInstaller bootloader 经 exe 路径定位 `_internal`，与 cwd 无关，故改 cwd 安全；
  `domains/` 等代码目录在打包态从可写区找不到时**优雅降级**（已有 `exists()` 守护）。

## 桌面可启动（无凭证开机，已实现）

纯桌面用户（只用统一收件箱 / 内嵌网页翻译，不用 Telegram 协议号）**无需任何凭证即可开机**：

- 真正的拦路是 `main.py` 旧版**无条件**初始化 config-Telegram 协议客户端——用 example 占位
  `api_id` 连接会失败/挂起，挡住整个进程。（`ConfigManager._validate_config` 的返回值在 `main.py`
  其实**未被用于 gate**，故非根因。）
- 现已门控：`main._telegram_configured()` 判定占位/缺省即「未配置」→ **跳过协议客户端初始化**；
  `create_app()` / `start()` / `stop()` 全部对 `telegram_client=None` 守护。
- 显式桌面模式：launcher 在打包态注入 `AITR_DESKTOP_MODE=1`（或 config `app.desktop_mode: true`），
  即便填了真凭证也强制跳过 config-Telegram，用于「纯收件箱/翻译」形态。
- **serve↔talk 强一致（否则连不上后端）**：随包 `config.example.yaml` 的 `web_admin.port`(18787)/
  占位令牌与桌面默认(18799/`admin`)不符，全新安装会「后端起了但 renderer 连不上」。故 launcher
  按桌面 `config.json::backend.{base_url,token}` 注入 `AITR_WEB_HOST/PORT/TOKEN`，`ConfigManager`
  据此覆盖 `web_admin.{host,port,auth_token}`；并在桌面模式**强制** `web_admin.enabled=true`
  （统一收件箱 / 翻译 / D1 选择器热更新 / D4 受控外发路由都挂在 web 后台下）。
- 不受影响：统一收件箱、内嵌网页翻译、LINE/Messenger/WhatsApp RPA（各自 `enabled` 门控）、
  **QR 扫码登录协议号**（走 orchestrator，不依赖 config 账号）。

## 待硬化（后续迭代）

1. **代码签名**：Windows Authenticode / mac notarization（否则 SmartScreen / Gatekeeper 拦截）。
2. **更新源**：`package.json::build.publish.url` 现为占位，需指向真实静态更新服务器。
3. **包体瘦身**：确认排除重依赖后体积可接受；如需 whisper/OCR 建议改走在线后端而非进包。
