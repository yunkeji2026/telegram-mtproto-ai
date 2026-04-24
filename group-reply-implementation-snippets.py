# 群组回复控制 - 代码实现片段
# 仅用于参考，实际修改等待用户确认后执行

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
    
    # 获取消息文本（包括caption）
    text = message.text or message.caption or ""
    
    # 对于语音消息，转录后会设置text字段
    # 对于图片消息，OCR后会设置text字段
    
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
    case_sensitive = group_config.get('case_sensitive', False)
    require_exact = group_config.get('require_exact_match', False)
    
    if not case_sensitive:
        text = text.lower()
    
    for keyword in keywords:
        keyword_check = keyword if case_sensitive else keyword.lower()
        
        if require_exact:
            # 精确匹配
            if text == keyword_check:
                return True
        else:
            # 包含匹配
            if keyword_check in text:
                return True
    return False

# 群组消息处理器修改示例
async def handle_group_message_modified(client, message: Message):
    """处理群组消息（修改后版本）"""
    try:
        # 检查是否需要处理此消息类型
        if not (message.text or message.caption or message.voice or message.audio or message.photo or message.document):
            self.logger.debug(f"忽略非文本/语音/图片群组消息: {message.chat.title}")
            return
        
        # 检查是否需要回复
        if not self._should_reply_to_group_message(message):
            self.logger.debug(f"忽略未触发条件的群组消息: {message.chat.title} - 模式: {self.config.get('telegram', {}).get('group_reply', {}).get('mode', 'always')}")
            return
        
        # 满足条件，处理消息
        await self._process_message(message)
        
    except Exception as e:
        self.logger.error(f"处理群组消息失败: {e}")