# 多主机部署指南（W03 / W175 / 主控）

> 本指南描述如何在多台机器上各自运行 `telegram-mtproto-ai`，各机独立管理本机连接的手机。

## 架构概述

```
┌────────────────────────────────────────────────────────────────┐
│                     共享 DeviceRegistryDB                       │
│         (openclaw.db — 可 NFS/SMB 共享 或 SQLite 副本同步)      │
└───────┬──────────────────────┬──────────────────────┬──────────┘
        │                      │                      │
   ┌────▼─────┐          ┌────▼─────┐          ┌────▼─────┐
   │  主控机   │          │   W03    │          │   W175   │
   │ host_name │          │ host_name │          │ host_name │
   │ = "主控"  │          │ = "W03"  │          │ = "W175" │
   │           │          │           │          │           │
   │ 手机05    │          │ 手机10    │          │ 手机15    │
   │ 手机07    │          │ 手机11    │          │ 手机16    │
   │ 手机08    │          │ ...       │          │ ...       │
   │ 手机09    │          │           │          │           │
   └───────────┘          └───────────┘          └───────────┘
```

每台主机只管理 `group_name` 匹配本机 `host_name` 的设备。

## 快速开始

### 1. 克隆代码到目标机器

```bash
git clone https://github.com/victor2025PH/telegram-mtproto-ai.git
cd telegram-mtproto-ai
pip install -r requirements.txt
```

### 2. 配置 `config/config.yaml`

关键差异化配置：

```yaml
# ── 热插拔设备管理 ──────────────────────────────────
hotplug_watcher:
  enabled: true
  host_name: "W03"              # ← 每台机器不同！
  scan_interval_sec: 15
  offline_timeout_sec: 30

# ── Web 后台端口（每台机器不同避免冲突，或用不同 IP）──
web:
  port: 18787                   # 主控 18787, W03 18788, W175 18789

# ── 设备注册表 DB 路径 ──────────────────────────────
device_registry:
  db_path: "D:/workspace/mobile-auto0423/data/openclaw.db"
  # 远程机器选项 A：SMB/NFS 共享
  # db_path: "\\\\192.168.8.100\\shared\\openclaw.db"
  # 远程机器选项 B：本地副本 + 定时同步
  # db_path: "./data/openclaw.db"
```

### 3. 设备注册

在 **任一** 机器的 Web 面板 → 📱 设备管理 → 注册新设备：

| 字段 | 说明 |
|------|------|
| serial | ADB serial（自动检测，未注册设备会显示黄色卡片） |
| label | 简码（如 `VWN`、`Q4N`） |
| group_name | **必须** 填写本机 host_name（如 `W03`）|
| number | 用户自定义编号 |
| platform_* | 平台 account_id |

### 4. 启动

```bash
python main.py
```

系统启动后 HotPlug Watcher 会：
1. 每 15s 扫描 `adb devices`
2. 查 registry DB，仅纳管 `group_name == host_name` 的设备
3. 动态创建 `DeviceCoordinator` 管理对应平台

## DB 共享策略

### 方案 A：网络共享（推荐小规模）

所有机器挂载同一 SMB/NFS 路径。SQLite WAL 模式支持多读单写。

**注意**：SQLite 在网络文件系统上可能有锁问题。如果频繁写入（>1 次/秒），建议方案 B。

### 方案 B：HTTP API 同步（推荐无 NFS 环境）

使用内置 `tools/sync_registry.py` 工具从主控 API 拉取 registry 到从机本地 DB。

```bash
# 一次性拉取
python tools/sync_registry.py pull --url http://192.168.8.100:18787/api/registry/export

# 守护模式：每 30 秒自动拉取
python tools/sync_registry.py pull --url http://192.168.8.100:18787/api/registry/export --loop 30

# 带认证 token
python tools/sync_registry.py pull --url http://192.168.8.100:18787/api/registry/export --token YOUR_TOKEN --loop 30
```

也可手工导出/导入 JSON：
```bash
# 在主控机导出
python tools/sync_registry.py export --out registry.json

# 在从机导入（upsert 策略，不删除本地独有设备）
python tools/sync_registry.py import --file registry.json
```

**合并策略**：远程数据覆盖本地已有设备字段；本地独有设备不会被删除。

### 方案 C：PostgreSQL 替换（大规模）

未来如果 >5 台主机，可将 registry 迁移到 PostgreSQL。`DeviceRegistryDB` 接口层已抽象，替换只需实现相同方法。

## 故障排除

| 现象 | 原因 | 解决 |
|------|------|------|
| 设备在线但不纳管 | `group_name` 不匹配 `host_name` | 检查 registry DB 中设备的 group_name |
| 设备频繁上下线 | USB 接触不良 / hub 供电不足 | 换 USB 线，用有源 HUB |
| "设备未注册" 黄卡 | 新设备未录入 registry | Web 面板直接注册 |
| DB 锁错误 | 网络共享 + 并发写入 | 改用方案 B 或 C |

## 批量设备管理

Web 面板支持勾选多台设备后批量分配平台：
1. 在 📱 设备管理 tab 勾选目标设备的复选框
2. 顶部出现批量操作栏，填写 platform account_id（输入 `-` 清空该平台）
3. 点击「应用」→ 一次性写入 registry + 热重建所有 Coordinator

对应 API：`POST /api/registry/batch`
```json
{
  "serials": ["SER1", "SER2"],
  "fields": {"platform_messenger": "msg_xxx", "platform_line": ""}
}
```

## 健康告警

启用 `webhook` 后，以下事件会自动推送到 Telegram / Slack：
- `device_coordinator.circuit_open` — 设备某平台连续失败触发熔断
- `messenger_rpa.device_unhealthy` — Messenger 设备连续不健康

配置见 `config.example.yaml` 的 `webhook` 段。

## 验证检查清单

- [ ] `python main.py` 启动无报错
- [ ] Web 面板 → 📱 设备管理 tab 显示设备卡片
- [ ] 设备卡片显示 "运行中" + 平台 tag
- [ ] 不同机器只显示自己管的设备（远程设备灰色 "由其他主机管理"）
- [ ] 注册新设备后 ≤15s 自动纳管
- [ ] 点击 ✏️ 编辑平台 → 保存 → toast 显示 "Coordinator 已热重建"
- [ ] 批量勾选设备 → 应用平台分配 → 全部热重建成功
- [ ] `python tools/sync_registry.py export --out test.json` 导出成功
- [ ] 配置 webhook 后触发熔断 → 收到 Telegram/Slack 通知
