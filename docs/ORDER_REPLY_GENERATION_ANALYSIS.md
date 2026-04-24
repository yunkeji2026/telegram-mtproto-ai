# 「订单」话术生成路径分析

## 现象

用户发「看到发的订单了吗。」（或带图）后，机器人回复一大段具体订单内容，例如：

- 「看到了看到了！您刚才发的订单信息我已经查到了，是今天下午3点42分下的单对吧？😊 订单里有一件春季新款连衣裙和两双袜子，收货地址是上海市浦东新区那个。目前状态显示"待发货」……」

这些**具体内容（时间、商品、地址）与用户发的图无关**，是模型根据「订单」语境**自由生成**的。

---

## 1. 整体流程（从收到消息到发出回复）

```
用户发: "看到发的订单了吗。"
    ↓
TelegramClient: 四层触发（关键词「订单」）→ 决定回复
    ↓
TelegramClient: 上下文分析 → 主题 order
    ↓
SkillManager.process_message(text="看到发的订单了吗。", context={ chat_id, context_analysis })
    ↓
SkillManager: 意图识别 → order_query
    ↓
OrderQuerySkill.execute(text, user_id, context)
    ↓
claude-4.6-oups-highClient.generate_reply_with_intent(
    user_message="看到发的订单了吗。",
    intent="order_query",
    user_context={ user_id, intent, last_message, last_reply, ... }
)
    ↓
claude-4.6-oups-high API 返回一段自然语言 → 原样作为回复发出
```

**结论：整段话术 100% 由 claude-4.6-oups-high 根据「用户一句文字 + 意图 order_query」生成，没有读取任何图片内容。**

---

## 2. 各环节代码位置与输入

### 2.1 触发与入参（只传文字）

- **文件**: `src/client/telegram_client.py`
- 入队时只传 **文本**：
  - 若是「带图+说明」：只取 `message.caption` 或 `message.text`（即「看到发的订单了吗。」）
  - 若是纯图：会走 OCR，文本变成 `[图片识别] xxx`，但**当前这条消息**你看到的是「看到发的订单了吗。」，说明这条是**带说明或纯文字**，没有把「图里内容」拼进本条 `text`
- 传给 Skill 的只有：`text`、`user_id`、`context`（含 `context_analysis`，无图片、无 OCR 结果）。

所以：**发图与否都不影响本条请求里有没有「图的内容」；模型拿到的只有这句文字。**

### 2.2 意图与技能

- **文件**: `src/skills/skill_manager.py`
  - 意图识别：含「订单」等词 → `order_query`（见 `config.yaml` 里 `order_query` 关键词）
  - `OrderQuerySkill.execute()` 被调用
- **OrderQuerySkill**（同文件）：
  - 先尝试 **AI 生成**，再才用模板
  - 调用：`ai_client.generate_reply_with_intent(user_message=text, intent='order_query', user_context=context)`

这里没有任何「把图片或 OCR 结果塞进 user_message / user_context」的逻辑，所以话术来源只能是下面的 claude-4.6-oups-high。

### 2.3 发给 claude-4.6-oups-high 的内容

- **文件**: `src/ai/claude-4.6-oups-high_client.py`
  - `generate_reply_with_intent()` 只把 `user_message`、`intent`、`user_context` 传给 `generate_reply()`，没有图片或 OCR 字段。
  - `_build_messages()` 构建的是**纯文本**对话：
    - **system**: `config.yaml` 里的 `ai.system_prompt`（Camille 客服、真人化、业务场景等）
    - **context 段**（若有）：`_build_context_prompt(context)`，可能包含：
      - 用户意图: order_query
      - 用户上一条消息、上次回复、对话阶段等
    - **user**: 当前用户消息，即「看到发的订单了吗。」

也就是说：**API 里没有任何「订单截图」或「OCR 出来的订单内容」，只有「看到发的订单了吗。」这句话 + 意图 + 上下文。**

### 2.4 系统提示词（不包含真实订单数据）

- **文件**: `config/config.yaml` → `ai.system_prompt`
  - 只规定了**人设和风格**（真人化、不承认是 AI、业务场景处理等）
  - **没有**提供任何真实订单数据，也**没有**「根据用户发的图/订单截图来回复」的设定

所以模型既看不到图，也没有被指示「只根据给定订单信息回答」。

---

## 3. 话术为什么和「发的图」无关？

| 环节           | 是否使用图片/OCR | 说明 |
|----------------|------------------|------|
| 消息入队       | 否               | 本条只传文字「看到发的订单了吗。」；若上一条是图，其 OCR 也不会自动拼进本条 |
| 四层触发       | 否               | 只看文本是否含「订单」等词 |
| 意图识别       | 否               | 只看文本和关键词 |
| OrderQuerySkill| 否               | 只把 `text` 和 `context` 给 AI，无图无 OCR |
| claude-4.6-oups-high 调用  | 否               | 请求里只有文本消息，没有 image 或 OCR 结果 |

因此：**整段「订单详情」话术（时间、商品、地址、待发货等）都是 claude-4.6-oups-high 根据「订单」语境自由生成的，和用户发的图没有关系。**

---

## 4. 已实现：让回复与发的图、群内机器人信息一致

当前实现方式：

1. **OCR 交给模型**
   - 带说明的图（图+说明）：同样会对图片做 OCR，结果写入 context 的 `image_ocr_text`，随上下文一起发给 claude-4.6-oups-high（`用户刚发的图片/截图内容（OCR）`）。
   - 纯图：继续用「图片识别」结果作为本条消息内容，并在需要时一并作为 OCR 上下文。

2. **群内机器人/通知消息**
   - 在群聊中回复前，会拉取近期该群内指定机器人（如 `gxp_notify_bot`）的消息，写入 context 的 `recent_bot_messages`，在提示中以「近期群内机器人/通知消息（可参考）」形式提供给模型。

3. **系统提示禁止编造**
   - 在 `config.yaml` 的 `ai.system_prompt` 中已增加「订单/截图/付款凭证（严禁编造）」规则：无真实 OCR 或机器人通知内容时不得编造订单详情，应引导用户发截图或订单号。

配置入口：
- 图片识别：`image_recognition`（已有）。
- 机器人消息来源：`context.bot_sources`（`enabled`、`usernames`、`include_any_bot`、`limit`）。

---

## 5. 相关代码索引

| 步骤         | 文件: 位置 |
|--------------|------------|
| 消息入队文本 | `telegram_client.py`: 取 `message.text` / `message.caption`，图片仅走 OCR 写回 `text`，不自动合并到下一条 |
| 四层触发     | `telegram_client.py`: `_should_reply_with_four_layer_trigger` |
| 调用 Skill   | `telegram_client.py`: `_process_message_async` → `skill_manager.process_message(...)` |
| 意图与执行   | `skill_manager.py`: `process_message` → `_select_skill` → `OrderQuerySkill.execute` |
| AI 生成      | `skill_manager.py`: `OrderQuerySkill` 内 `generate_reply_with_intent(..., intent='order_query')` |
| 请求构建     | `claude-4.6-oups-high_client.py`: `generate_reply_with_intent` → `generate_reply` → `_build_messages` / `_build_context_prompt` |
| 系统提示     | `config/config.yaml`: `ai.system_prompt` |

---

**总结**：当前「订单」相关的那段长话术，完全由 **claude-4.6-oups-high 根据一句「看到发的订单了吗。」+ 意图 order_query** 生成，**没有使用任何图片内容**；若要跟「发的图」一致，需要把图片/OCR 接入请求并约束模型不编造订单详情。
