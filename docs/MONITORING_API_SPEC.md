# 后台监控 API - 技术对接方案（前端/其他 AI 用）

## 1. 基础信息

| 项目 | 说明 |
|------|------|
| **Base URL** | `http://<host>:9090`（默认本机 `http://127.0.0.1:9090`） |
| **协议** | HTTP/1.1，JSON 响应 |
| **CORS** | 允许任意来源（开发/生产可再收紧），便于前端独立部署 |
| **认证** | 当前版本无；建议仅内网或本机访问 |

## 2. 通用约定

- 所有接口均为 **GET**（除将来可能的控制类接口）。
- 响应 `Content-Type: application/json`。
- 成功：HTTP 200，body 为约定 JSON。
- 错误：HTTP 4xx/5xx，body 形如 `{"detail": "错误说明"}`（FastAPI 默认）。

---

## 3. 接口清单

### 3.1 健康与运行状态

**GET** `/api/health`

用于判断进程是否存活、Telegram 是否已连接、运行时长。

**响应示例：**

```json
{
  "status": "ok",
  "telegram_connected": true,
  "uptime_seconds": 3600.5,
  "version": "1.0.0"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | `"ok"` 表示进程正常；`"degraded"` 表示部分异常（如 Telegram 未连） |
| `telegram_connected` | boolean | Telegram 客户端是否已连接 |
| `uptime_seconds` | number | 自监控服务启动以来的秒数（可近似为进程运行时长） |
| `version` | string | 应用版本号（可选，可从包或配置读） |

---

### 3.2 核心指标（供仪表盘/图表）

**GET** `/api/metrics`

返回当前运行周期内的聚合指标，适合做数字卡片、折线图（前端按间隔轮询即可）。

**响应示例：**

```json
{
  "messages_received": 120,
  "messages_replied": 115,
  "api_calls": 98,
  "response_time_avg_ms": 245,
  "response_time_p99_ms": 1200,
  "errors_count": 2,
  "queue_size": 0,
  "last_message_at": "2026-03-08T04:20:55Z"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `messages_received` | number | 累计收到消息数 |
| `messages_replied` | number | 累计已回复数 |
| `api_calls` | number | 累计 claude-4.6-oups-high API 调用次数 |
| `response_time_avg_ms` | number | 单次「收消息→发回复」平均耗时（毫秒） |
| `response_time_p99_ms` | number | 同上，P99 耗时（毫秒） |
| `errors_count` | number | 处理/发送过程中的错误次数 |
| `queue_size` | number | 当前待处理消息队列长度 |
| `last_message_at` | string | 最近一条消息时间，ISO 8601（如无则为 null） |

---

### 3.3 近期日志（尾读）

**GET** `/api/logs?tail=100`

从日志文件末尾读取若干行，用于「最近日志」面板。

**Query 参数：**

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `tail` | number | 100 | 最多返回行数，建议 1–500 |

**响应示例：**

```json
{
  "lines": [
    "[2026-03-08 04:20:55] [INFO] 收到消息 [私聊/cntg3]: 之前有给过你订单号...",
    "[2026-03-08 04:20:55] [INFO] 已回复消息: 请告诉我订单号。..."
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `lines` | string[] | 日志行数组，从旧到新；文件不存在或无法读时可为 `[]` |

---

### 3.4 配置摘要（非敏感）

**GET** `/api/config/summary`

仅返回与监控/排障相关的配置摘要，不包含 API Key、手机号等。

**响应示例：**

```json
{
  "telegram_session_name": "639277356155",
  "ai_model": "claude-4.6-oups-high",
  "skills_enabled": ["greeting", "order_query", "price_check", "status_check", "channel_info", "complaint", "small_talk", "test"],
  "monitoring_enabled": true,
  "metrics_port": 9090
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `telegram_session_name` | string | 会话名（脱敏后的标识） |
| `ai_model` | string | 当前使用的模型名 |
| `skills_enabled` | string[] | 已启用技能列表 |
| `monitoring_enabled` | boolean | 是否启用监控 |
| `metrics_port` | number | 监控 API 端口 |

---

## 4. 前端对接建议

1. **轮询**：对 `/api/health`、`/api/metrics` 每 5–10 秒请求一次即可实现「准实时」仪表盘；无需 WebSocket 也可。
2. **图表**：可用 `messages_received`、`messages_replied`、`response_time_avg_ms`、`queue_size` 做折线/柱状图（前端自行按时间戳或轮询序号做 X 轴）。
3. **日志**：首次进入页面拉一次 `/api/logs?tail=200`，之后可定时再拉或实现「刷新」按钮。
4. **CORS**：后端已允许跨域；若前端与 API 同域可忽略。
5. **错误处理**：若 `GET /api/health` 请求失败（网络或 5xx），可视为服务不可用，前端显示「无法连接监控服务」。

---

## 5. 版本与变更

- 当前版本：**v1**。
- 后续若增删字段，会在此文档中注明，并尽量保持向后兼容（只增不删或标记废弃）。
