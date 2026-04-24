# 日志分析 — 优化点汇总

基于 `logs/app.log` 的整理，便于按优先级落地修改。

---

## 一、必须修复（错误 / 漏回）

### 1. 「什么时候回 调出来」被跳过（漏回）

- **日志**：`什么时候回 调出来...` → 跳过未触发消息。
- **原因**：用户输入「回 调」中间有空格，L1 关键词是「回调」，整词匹配不到。
- **优化**：L1 匹配前对文本做**空格规范化**（如 `re.sub(r'\s+', '', text)` 或 `text.replace(' ', '')` 再匹配），或在高频词中增加「回 调」「什么时候」等变体；追问关键词已含「什么时候」，但若 L1 先未触发且追问也未命中（例如历史 K 条内没有我们），仍会漏。建议在 **trigger 的 L1 检查里对 message_text 做 normalize**（去掉空格后再匹配关键词）。

### 2. 「Order inquiry」被跳过（漏回）

- **日志**：`Order inquiry...` → 跳过未触发消息。
- **原因**：`trigger_rules.yaml` 的 all_keywords 已包含 "order" 和 "inquiry"，理论上应触发。可能原因：**群消息来源是机器人**（如 gxp_notify_bot）被当成普通用户消息处理但未触发；或某次会话里 message_text 被截断/编码问题。建议：确认 L1 收到的 `message_text` 是否为完整 "Order inquiry"；若为机器人消息，应不回复（见下条）。

### 3. 机器人通知消息触发回复（误回）

- **日志**：`收到消息 [甲方模拟回复群/gxp_notify_bot]: 🇵🇰 巴基斯坦通道最新通知...` → 四层触发决策: 回复 - L1触发: 高频关键词: 成功。
- **原因**：消息来自 **gxp_notify_bot**，内容含「成功」等关键词，被当成普通用户消息触发回复。
- **优化**：在**判断是否回复之前**，若 `message.from_user` 为 **bot**（`from_user.is_bot == True`），或 `from_user.username` 在配置的「仅拉取、不回复」的 bot 列表（如 gxp_notify_bot）内，则**直接不回复、不进入四层/追问/AI 上下文**，避免对机器人通知回复。

### 4. 'Photo' object is not iterable（崩溃/失败）

- **日志**：多次 `下载图片文件失败: 'Photo' object is not iterable`，群内最近图连续失败。
- **原因**：某处将 `message.photo` 当作 list 迭代（如 `for photo in message.photo`），而 Pyrogram 2.x 中 `message.photo` 可能为单个 `Photo` 对象。
- **优化**：全局搜 `message.photo` 及 `photo` 迭代，统一为「若为 list 则迭代，若为单对象则包装成单元素再取 file_id」；之前已在 `_download_image_file` 修过，需检查**拉取群内最近图**的那段（get_chat_history 遍历到的 msg.photo）是否也做了同样兼容。

### 5. 'utf-16-le' codec can't decode bytes（处理异常）

- **日志**：`处理消息失败: 'utf-16-le' codec can't decode bytes in position 100-101: unexpected end of data`（紧接在 gxp_notify_bot 通道通知之后）。
- **原因**：某处用 `utf-16-le` 解码或写文件（例如默认 encoding），遇到非 UTF-16 或截断数据时报错。
- **优化**：定位使用 `utf-16` / `utf-16-le` 的代码（如 open(..., encoding=...) 或 .decode()），改为 `utf-8` 并加 `errors='replace'` 或 `errors='ignore'`，避免因编码导致整条消息处理失败。

---

## 二、体验与策略优化

### 6. 情绪增强器长期关闭

- **日志**：多次 `情绪增强器已禁用（配置: emoticons.enabled: false）`，仅最后一次启动为「情绪增强器初始化成功」。
- **说明**：若当前配置已改为 `enabled: true` 且已修复 _cleanup_format，可保持开启；否则建议按 CHAT_STIFFNESS_ANALYSIS_AND_OPTIMIZATION.md 修复并开启，减轻回复呆板感。

### 7. 纯闲聊/追问被 L2 拦下

- **日志**：如「和我聊聊天。」「先别工作了。」「给我玩一会。」「听到了没。」「我投诉你啦。」→ L2置信度不足: 0.5，跳过。
- **说明**：当前策略是「非业务不回复」，避免刷屏。若希望对「投诉」等少量词必回，可在 L1 增加关键词「投诉」；若希望紧跟我们回复的追问（如「听到了没」）更多被回复，可依赖追问上下文或 AI 上下文，无需改 L2 阈值。

### 8. 回复内容与 SOP 不一致（识图后仍要订单号）

- **日志**：用户说「你按订单上的信息去查啊」「查到了吗」时，上下文中已有 Vision 解析的交易号 #41504924558，但回复仍出现「不过我这里暂时还没看到具体的订单信息呢」「您方便提供一下订单号吗」。
- **优化**：系统提示词已约定「识图有唯一依据则按已查询到处理、不再索要订单号」；需确认**上下文里是否把 image_ocr_text / 最近图解析结果**正确带入 AI，且模型未被长上下文干扰。可检查 skill_manager / claude-4.6-oups-high_client 的 context 构建是否包含「群内最近图 OCR」与「识图得到的交易号」并优先于泛化话术。

### 9. 通道通知问「通道什么时候可用」被复读订单话术

- **日志**：用户问「通道什么时候可用」，回复是「已根据您发的凭证（交易号 #41504924558）确认到订单... 关于通道可用性的具体时间，我们正在为您核实」。
- **说明**：意图偏「通道状态」，但上下文里订单信息过强，导致回复仍以订单为主。可考虑在上下文中区分「当前问的是通道还是订单」，或在提示词中强调「若用户明确问通道/额度，优先答通道/额度，不要强行扯回订单」。

---

## 三、建议实施顺序

1. **过滤机器人发送者**：from_user.is_bot 或 username 在 bot 名单 → 不回复。
2. **L1 关键词匹配规范化**：对 message_text 做空格规范化后再匹配，减少「回 调」漏触发。
3. **再次检查 Photo 迭代**：拉取群内最近图时对 msg.photo 做单对象/列表兼容，消除 'Photo' object is not iterable。
4. **定位并修正 utf-16-le**：全文搜 utf-16，改为 utf-8 + errors=replace/ignore。
5. **确认「Order inquiry」**：确认 L1 收到的文本和发送者；若为机器人则已被 1 过滤。
6. 其余为体验与 SOP 微调（情绪增强、识图后话术、通道优先回复），可按需排期。

---

## 四、涉及文件（便于修改）

| 优化项           | 可能涉及文件/位置 |
|------------------|-------------------|
| 过滤机器人       | `telegram_client.py` 群消息入口或 `_should_reply_to_group_message` 开头 |
| L1 空格规范化    | `trigger_rules.yaml` 或 `four_layer_trigger.py` 的 L1 关键词匹配处 |
| Photo 单对象     | `telegram_client.py` 中拉取群内最近图、遍历 msg 并下载图的部分 |
| utf-16-le        | 全文搜索 `utf-16` / `decode` / `encoding` |
| 识图后话术/SOP   | `config.yaml` 系统提示词、`claude-4.6-oups-high_client` 的 context 构建、skill 传入的 user_context |

上述内容可直接作为工单或迭代清单使用；修改完成后建议再跑一轮相同场景，对照本清单做回归。
