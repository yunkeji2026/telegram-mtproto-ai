# 知识库「直接输出」增强规范（reply_direct_spec）

## 适用条件

- 条目 `reply_mode` = `direct`
- 字段 `reply_direct_spec` 为 JSON 字符串，且包含 **`"version": 1`**

未配置或无效 JSON → 行为与旧版一致：仅使用 `example_reply_zh`，`\n---\n` 分隔多段随机。

## 通道数据来源

默认读取 `config/exchange_rates.yaml` 中的 `channels`（可通过 `channels_file` 覆盖为同目录下其他文件名）。

用户消息中会 **按通道别名匹配**（`names`、`display_name`、通道 key）；未匹配时使用 `default_channel_key`；再无则占位符填 `—`。

## 占位符（填槽）

模板中可使用与 `exchange_rates.yaml` 一致的字段，例如：

- `{channel_display_name}` `{channel_fee_rate}` `{channel_status}`
- `{channel_status_description}` `{channel_limits}` `{channel_processing_time}`
- `{channel_success_rate}` `{channel_notes}` `{channel_key}`

支持 `{{同名}}` 写法。

## 三种能力对应字段

### A — 单模板 + 占位符

只配置 `version`，不配 `branches`，则主文案仍来自 `example_reply_zh`，仅做占位符替换（及可选 `snippets`）。

### B — 按状态分支

`branches` 键名建议：`normal` / `volatile` / `maintenance` / `unknown`（可选）。

系统根据匹配通道的 `status` 文本映射：

- 含「维护」「暂停」「停用」→ `maintenance`
- 含「波动」「不稳定」→ `volatile`
- 含「正常」「稳定」→ `normal`
- 否则 → `unknown`（可用 `default_branch` 指定回落键）

### C — 条件片段

`snippets` 为数组，示例：

```json
{
  "id": "high_sr",
  "if": {"success_rate_gte": 80},
  "append": "\n成功率表现较好。"
}
```

支持 `if.status_in`、`if.success_rate_gte`、`if.success_rate_lte`。

## 受控 AI 路由（可选）

```json
"router": {
  "enabled": true,
  "min_confidence": 0.6
}
```

当 `branches` 多于 1 条且启用时，由模型 **仅输出 JSON** 选择分支键；置信度低于阈值或解析失败 → 使用 `fallback` 字段或退回 `example_reply_zh`。

## 完整示例

```json
{
  "version": 1,
  "default_channel_key": "jc",
  "default_branch": "normal",
  "branches": {
    "normal": "{channel_display_name} 当前{channel_status}，费率 {channel_fee_rate}，额度 {channel_limits}。",
    "volatile": "{channel_display_name} 当前波动，{channel_status_description}。",
    "maintenance": "该通道维护中，请稍后再试或换其他通道。"
  },
  "fallback": "请稍后再询或联系人工。",
  "snippets": [
    {
      "id": "warn_volatile",
      "if": {"status_in": ["波动"]},
      "append": "\n（波动期间到账时间可能延长）"
    }
  ],
  "router": {"enabled": false}
}
```

## 可观测

运行时在项目目录 `logs/kb_direct_trace.jsonl` 追加 JSON 行（路径、分支、路由、通道 key 等）。

## 管理端

当前可通过 **API/SQL** 写入 `reply_direct_spec`；Web 表单若需可视化编辑，可在后续迭代增加多行文本框。
