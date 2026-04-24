# 回复决策与多语言查单

## 1. 识别所有对话进行分析、决定要不要回复

### 当前机制

- **主路径**：群消息是否回复由 `_should_reply_to_group_message()` 决定。
- **四层触发**（`config/trigger` 开启时）：
  - **L1**：关键词、图片+文字、订单号格式、@提及 等。
  - **L2**：语义（是否与订单/通道/客服相关）。
  - **L3**：上下文（连续用户消息、@提及、问题/负面情绪等）。
- **补充路径**：若四层均未触发，会再做**上下文推理**，判断是否为「机器人回复后的用户追问」。

### 上下文推理（追问也回复）

目的：避免像「什么时候回调出来」「Order inquiry」这类本该回复的消息被漏掉。

逻辑（`_should_reply_by_follow_up_context()`）：

1. 取当前群最近几条消息（`get_chat_history`）。
2. 若**上一条消息是机器人（本账号）发的**，且当前用户消息：
   - 含疑问语气（如 `?`、`吗`、`呢` 等），或
   - 含追问/查单相关词（如「什么时候」「多久」「回调」「到账」「查到了吗」或英文 "when" "order" "inquiry" "status" "check" 等），  
则**判定为追问，应回复**。

这样即使用户没带 L1 关键词，只要是在我们刚回复之后的追问，也会被识别并回复。

### 配置要点

- **L1 关键词**：`config/trigger_rules.yaml` → `high_frequency_keywords.all_keywords`。已加入追问/查单相关中英文（如 回调、可用、什么时候、order、inquiry、check、status、payment、channel 等）。
- **Legacy 关键词**：`config/config.yaml` → `telegram.group_reply.keywords`。已与上述多语言/追问词对齐，保证非四层模式下也能触发。

---

## 2. 多语言查单与「客户用什么语言就用什么语言回复」

### 查单相关多语言触发（L1）

在 `trigger_rules.yaml` 的 `all_keywords` 中已包含例如：

- 中文：订单、查、查询、回调、可用、什么时候、多久、到账 等。
- 英文：order、inquiry、check、status、payment、channel 等。

客户用中文或英文发「查单/订单/状态」类消息，都能触发 L1，进入正常查单流程。

### 回复语言与客户一致

在 **系统提示词**（`config/config.yaml` → `ai.system_prompt`）中已约定：

- **多语言回复**：客户用什么语言发消息，就用同一语言回复；查单、订单状态、通道说明等与客户语言一致，不混用。需识别的常用语言包括：
  - **中东**：阿拉伯语(العربية)、土耳其语(Türkçe)、乌尔都语(اردو)等；
  - **巴西**：葡萄牙语(Português)；
  - **欧美**：英语、西班牙语(Español)、法语(Français)、德语(Deutsch)、意大利语(Italiano)、俄语(Русский)等。

**L1 触发词**（`trigger_rules.yaml` → `all_keywords`）已加入上述地区的查单/订单/状态相关词，例如：

- 阿拉伯语：طلب、استفسار、حالة、دفع、قناة
- 土耳其语：sipariş、sorgu、durum、ödeme、kanal
- 葡萄牙语：pedido、consulta、pagamento、canal
- 西班牙语：pedido、consulta、estado、pago
- 法语：commande、enquête、statut、paiement
- 德语：Bestellung、Anfrage、Zahlung
- 意大利语：ordine、richiesta、stato、pagamento、canale
- 俄语：заказ、запрос、статус、оплата、канал

因此：

- 客户用英文问 "Order inquiry" → 用英文回复。
- 客户用中文问「什么时候回调」→ 用中文回复。
- 客户用阿拉伯语/葡萄牙语/西/法/德/意/俄等发查单相关消息 → 触发回复并用对应语言回复。

### 相关文件

- 回复决策与追问逻辑：`src/client/telegram_client.py`（`_should_reply_to_group_message`、`_should_reply_by_follow_up_context`）。
- L1 关键词与多语言词：`config/trigger_rules.yaml`。
- 群回复关键词（legacy）：`config/config.yaml` → `telegram.group_reply.keywords`。
- 多语言回复约定：`config/config.yaml` → `ai.system_prompt`。
