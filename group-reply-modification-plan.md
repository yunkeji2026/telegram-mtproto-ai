# 群组回复控制修改方案

## 📋 需求说明
用户要求：让客服帐号在群里，只有被 @ 到的时候再回复，或者触发关键词再回复。

## 🔧 当前状态分析

### 现有消息处理逻辑
```python
@self.client.on_message(filters.group)
async def handle_group_message(client, message: Message):
    """处理群组消息"""
    try:
        # 处理文本、语音和图片消息
        if message.text or message.caption or message.voice or message.audio or message.photo or message.document:
            await self._process_message(message)  # ❌ 当前处理所有消息
        else:
            self.logger.debug(f"忽略非文本/语音/图片群组消息")
    except Exception as e:
        self.logger.error(f"处理群组消息失败: {e}")
```

**问题**：当前系统会回复群组中的所有消息，不符合客服场景需求。

## 🎯 修改目标

### 核心需求
1. **@提及检测**：只有消息中 @ 了客服账号时才回复
2. **关键词触发**：消息包含特定关键词时也回复
3. **配置灵活**：支持不同触发模式切换
4. **向后兼容**：不影响现有私聊功能

## 📐 技术方案设计

### 1. 配置扩展 (`config/config.yaml`)
在 `telegram` 配置节中添加群组回复控制：

```yaml
# Telegram API配置 (从 https://my.telegram.org 获取)
telegram:
  api_id: "36469541"
  api_hash: "30bf037aa581f4407ba2cdb7619549c3"
  phone_number: "+639277356155"
  session_name: "639277356155"
  
  # 新增：群组回复控制配置
  group_reply:
    mode: "mention_or_keyword"  # 回复模式: always|mention_only|keyword_only|mention_or_keyword
    keywords:                  # 触发关键词列表
      - "客服"
      - "帮助"
      - "camille"
      - "支持"
    mention_usernames:         # 需要检测的@用户名列表
      - "@ai_zkw"             # 当前客服账号
      - "Camille"             # 显示名称
    case_sensitive: false     # 是否大小写敏感
    require_exact_match: false # 是否需要精确匹配关键词
    
  # 现有配置保持不变...
```

### 2. 消息处理器改造 (`src/client/telegram_client.py`)

#### 2.1 添加检测函数
```python
def _should_reply_to_group_message(self, message: Message) -> bool:
    """
    判断是否应该回复群组消息
    
    Args:
        message: Telegram消息对象
        
    Returns:
        bool: 是否应该回复
    """
    # 获取配置
    group_config = self.config.get('telegram', {}).get('group_reply', {})
    mode = group_config.get('mode', 'always')
    
    # 模式1: 始终回复 (默认，向后兼容)
    if mode == 'always':
        return True
    
    # 获取消息文本
    text = message.text or message.caption or ""
    if not text:
        return False  # 无文本的消息不处理
    
    # 模式2: 仅@提及
    if mode == 'mention_only':
        return self._contains_mention(text, group_config)
    
    # 模式3: 仅关键词
    if mode == 'keyword_only':
        return self._contains_keyword(text, group_config)
    
    # 模式4: @提及或关键词
    if mode == 'mention_or_keyword':
        return (self._contains_mention(text, group_config) or 
                self._contains_keyword(text, group_config))
    
    return True  # 默认保持原有行为

def _contains_mention(self, text: str, group_config: dict) -> bool:
    """检测消息是否包含@提及"""
    usernames = group_config.get('mention_usernames', [])
    for username in usernames:
        # 移除@符号进行匹配
        clean_username = username.lstrip('@')
        if clean_username.lower() in text.lower():
            return True
        # 也检查原始用户名
        if username.lower() in text.lower():
            return True
    return False

def _contains_keyword(self, text: str, group_config: dict) -> bool:
    """检测消息是否包含关键词"""
    keywords = group_config.get('keywords', [])
    text_lower = text.lower() if not group_config.get('case_sensitive', False) else text
    
    for keyword in keywords:
        keyword_lower = keyword.lower() if not group_config.get('case_sensitive', False) else keyword
        
        if group_config.get('require_exact_match', False):
            # 精确匹配
            if text_lower == keyword_lower:
                return True
        else:
            # 包含匹配
            if keyword_lower in text_lower:
                return True
    return False
```

#### 2.2 修改群组消息处理器
```python
@self.client.on_message(filters.group)
async def handle_group_message(client, message: Message):
    """处理群组消息"""
    try:
        # 检查是否需要处理此消息类型
        if not (message.text or message.caption or message.voice or message.audio or message.photo or message.document):
            self.logger.debug(f"忽略非文本/语音/图片群组消息: {message.chat.title}")
            return
        
        # 检查是否需要回复
        if not self._should_reply_to_group_message(message):
            self.logger.debug(f"忽略未触发条件的群组消息: {message.chat.title}")
            return
        
        # 满足条件，处理消息
        await self._process_message(message)
        
    except Exception as e:
        self.logger.error(f"处理群组消息失败: {e}")
```

### 3. 配置类更新 (`src/config/config.py`)
如果 Config 类需要支持新的配置结构，可能需要添加相应的 getter 方法。

## 🔄 实施步骤

### 阶段1：备份现有配置
1. 备份 `config/config.yaml`
2. 备份 `src/client/telegram_client.py`

### 阶段2：更新配置文件
1. 在 `telegram` 配置节中添加 `group_reply` 配置
2. 根据实际需求设置初始值

### 阶段3：修改代码
1. 在 `telegram_client.py` 中添加检测函数
2. 修改群组消息处理器
3. 添加必要的日志输出

### 阶段4：测试验证
1. 重启系统：`python main.py`
2. 测试不同场景：
   - 发送普通消息（不应回复）
   - 发送含关键词的消息（应回复）
   - 发送 @客服账号的消息（应回复）
   - 私聊消息（应正常回复）
3. 检查日志确认过滤逻辑

## ⚙️ 配置选项说明

### 回复模式 (`mode`)
- `always`：始终回复所有消息（现有行为，向后兼容）
- `mention_only`：仅当 @ 提及客服账号时回复
- `keyword_only`：仅当消息包含关键词时回复
- `mention_or_keyword`：@提及或关键词任一条件满足即回复

### 关键词列表 (`keywords`)
- 支持多个关键词
- 默认包含常见客服触发词
- 可自定义添加业务相关关键词

### @提及用户名 (`mention_usernames`)
- 支持带 @ 符号或不带
- 支持账号用户名和显示名称
- 可配置多个名称

### 匹配选项
- `case_sensitive`：大小写敏感，默认 false
- `require_exact_match`：精确匹配，默认 false（包含匹配）

## 📝 默认配置建议

```yaml
group_reply:
  mode: "mention_or_keyword"
  keywords:
    - "客服"
    - "帮助"
    - "camille"
    - "支持"
    - "有问题"
    - "怎么用"
    - "订单"
    - "价格"
  mention_usernames:
    - "@ai_zkw"
    - "Camille"
  case_sensitive: false
  require_exact_match: false
```

## 🧪 测试用例

| 测试场景 | 消息内容 | 预期结果 |
|---------|---------|---------|
| 普通消息 | "大家好" | 不回复 |
| 包含关键词 | "客服在吗" | 回复 |
| @提及 | "@ai_zkw 你好" | 回复 |
| 混合触发 | "Camille 帮我查订单" | 回复 |
| 私聊消息 | 任何内容 | 正常回复 |
| 语音消息含@ | 语音消息提到"ai_zkw" | 回复（转录后检测） |
| 图片消息 | 图片无文字 | 不回复 |

## ⚠️ 注意事项

1. **语音消息处理**：语音消息需要先转录为文字，然后再进行关键词/@检测
2. **性能影响**：新增检测逻辑对性能影响极小
3. **日志输出**：添加详细日志便于调试
4. **向后兼容**：默认配置保持现有行为，需要用户明确启用新功能

## 🚀 实施时间预估

- **方案设计**：已完成 (当前文档)
- **代码修改**：15-20分钟
- **测试验证**：10-15分钟
- **文档更新**：5分钟
- **总计**：约30-45分钟

## ✅ 成功标准

1. 群组中普通消息不再触发回复
2. @提及客服账号时正常回复
3. 包含关键词的消息正常回复
4. 私聊功能不受影响
5. 日志清晰记录过滤决策

## 📞 需要确认的事项

1. ✅ 您是否同意此修改方案？
2. ✅ 默认关键词列表是否需要调整？
3. ✅ 客服账号的用户名是否正确？（当前配置：@ai_zkw）
4. ✅ 是否还有其他触发条件需求？

## 🔜 下一步行动

1. **您确认方案**：回复同意或提出修改意见
2. **我执行修改**：按照方案实施代码更改
3. **您测试验证**：重启系统并进行功能测试
4. **反馈调整**：根据测试结果进行微调

---

**请确认以上方案，我将开始实施修改。**