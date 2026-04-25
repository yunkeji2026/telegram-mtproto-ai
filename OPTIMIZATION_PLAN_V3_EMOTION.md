# Telegram 客服 AI 优化方案 - 情绪感知 + 高效回复

> **状态更新 (2026-04-25 净化扫描)**：
> - `src/context/context_manager.py` ✅ **已实施**
> - `src/skills/emotion_enhancer.py` ✅ **已实施且接入** `src/client/telegram_client.py:1259/1294`
> - 原文档 model ID `claude-4.6-oups-high V3` 是**虚构占位**——本 repo 实际无 Anthropic Claude 客户端 (`src/ai/ai_client.py` 仅支持 Gemini + OpenAI 兼容 + Ollama)，已替换为项目实际 provider (DeepSeek-V3 via OpenAI 兼容)。
> - **当前阶段不是新建模块**，是参数调优 + 配置完善（emotion 触发阈值、emoji 映射演化、context 窗口大小、reply_decision 优先级）。
> - 想把整个项目升级到 Anthropic Claude 是另一项工作（需要新建 Anthropic SDK provider 分支 + 接入 ai_client.py），**本文档不覆盖**。

## 📋 优化目标
基于用户要求，将当前AI客服系统优化为：
1. **更智能的模型**: 升级到 `deepseek-chat`（DeepSeek-V3，主对话）/ `deepseek-reasoner`（强推理任务时） — 都通过项目现有 OpenAI 兼容 provider 调用
2. **情绪感知能力**: 理解用户情绪并相应回复
3. **表情符号系统**: 使用Telegram表情符号和表情包
4. **上下文理解**: 分析前后10条消息上下文
5. **高效回复机制**: 智能预测回复必要性
6. **性格包装**: 塑造独特的客服人格

## 🏗️ 系统架构优化

### **1. AI 模型升级** (`config/config.yaml`)

> 项目当前 ai 配置已是 DeepSeek-V3（`deepseek-chat`）。本节主要是**参数调优**（temperature / max_tokens / timeout / system_prompt），不是切 provider。
> 备选 provider（不替换当前 deepseek，按需配 alt）：`deepseek-reasoner`（DeepSeek-R1 推理）/ `gpt-4o-mini`（OpenAI 兼容）/ `glm-4-plus`（智谱）/ Ollama 本地。

> **⚠️ system_prompt 警告 (2026-04-25 审查)**：下方 yaml 示例里的 Camille 客服 prompt 是 **`config.example.yaml` 的公开模板**——`config/config.yaml` 的 production system_prompt 通常按业务定制（如 `conversion` 域已替换为更长的角色化营销 prompt）。
>
> **修改 production system_prompt 前必须先读 `config/config.yaml::ai.system_prompt` 看实际内容**——否则会误覆盖业务侧已调优的人格策略。本文档不在 git 里展开 production 全文（含敏感行业策略）。
>
> 三层 prompt 装载顺序（见 `src/ai/ai_client.py::_primary_system_prompt_text` + `set_domain_pack`）：
> 1. **`ai.system_prompt`** in `config/config.yaml`（最高优先级，业务自定义）
> 2. **域包 system_prompt**（按 `domains/{ecommerce, conversion, payment, ...}` 路由 fallback；当 config.yaml 留空时启用）
> 3. **persona runtime**（Web 后台「默认人设」+ `persona_runtime.yaml`）—— 与上面两层**叠加**，由 `persona_block_detail` 控制展开/精简/不注入

**参数 demo**（可复制到 `config/config.yaml::ai` 直接用，仅 provider/参数部分；system_prompt 见下方）：

```yaml
ai:
  provider: "openai_compatible"      # 项目实际 provider，无需改
  api_key: "${AI_API_KEY}"           # 从环境变量注入，不要写死
  base_url: "https://api.deepseek.com/v1"
  model: "deepseek-chat"             # ✅ DeepSeek-V3 主对话；推理任务可换 deepseek-reasoner
  temperature: 0.75                  # ✅ 提高创造力，保持专业性
  max_tokens: 768                    # ✅ 允许更详细的回复
  timeout: 60                        # ✅ 延长超时以适应深度对话

  # ⛔ system_prompt: 故意不在本文档展开
  # ─ production 真值: config/config.yaml::ai.system_prompt
  #   (业务定制角色化 prompt, 含敏感行业策略, git 中明文存储)
  # ─ 起新部署用的公开模板: config/config.example.yaml::ai.system_prompt
  #   (Camille 客服助手, 通用基础结构示例)
  # ─ 修改 production 前必读上方 §1 warning + reference_system_prompt_layers memory
  # ─ 三层装载顺序: ai.system_prompt > 域包 fallback > persona runtime 叠加
  #   (实现位置: src/ai/ai_client.py::_primary_system_prompt_text)
  system_prompt: "<see config/config.yaml or config.example.yaml>"
```

**为什么 system_prompt 不在本文档展示**：
- production 是 35+ 行业务定制角色化 prompt（含 7 天阶段化营销脚本 / 心理学驱动机制），属敏感商业内容，公开 docs 不复制
- demo 化会诱导未来开发者直接抄占位文本覆盖 production，**误删业务调优结果**
- 想看通用结构: 读 `config/config.example.yaml::ai.system_prompt`（~10 行 Camille 客服模板）

### **2. 表情符号系统** (`config/emoticons.yaml`)
```yaml
# 表情符号映射 - 根据情绪和场景使用
emoticons:
  # 情绪相关
  positive:
    - "😊"  # 微笑
    - "👍"  # 点赞
    - "🙏"  # 感谢
    - "🎉"  # 庆祝
    - "💪"  # 加油
  
  neutral:
    - "👉"  # 指向
    - "📝"  # 笔记
    - "🔍"  # 搜索
    - "⏰"  # 时间
    - "📊"  # 统计
  
  negative:
    - "😔"  # 难过
    - "🙁"  # 担忧
    - "😥"  # 失望
    - "💔"  # 心碎
    - "⚠️"   # 警告
  
  # 业务相关
  business:
    - "💰"  # 金钱/价格
    - "📦"  # 订单/包裹
    - "🔄"  # 流程/处理中
    - "✅"  # 完成/确认
    - "❌"  # 取消/拒绝
  
  # 客服专用
  customer_service:
    - "👋"  # 欢迎
    - "🙋"  # 举手/帮助
    - "💬"  # 对话
    - "📞"  # 联系
    - "🛠️"   # 支持

# 表情包配置 (可扩展为图片表情)
stickers:
  enabled: true
  folder: "./stickers"  # 表情包图片文件夹
  mapping:
    welcome: "welcome_sticker.png"
    success: "success_sticker.png"
    error: "error_sticker.png"
    processing: "processing_sticker.png"
```

### **3. 上下文缓存系统** (`src/context/context_manager.py`)
```python
"""
上下文管理器 - 存储和分析对话上下文
"""

import asyncio
from typing import Dict, List, Optional, Tuple
from collections import deque
import time

class ContextManager:
    """管理对话上下文，支持前后消息分析"""
    
    def __init__(self, max_context_size: int = 20):
        """
        初始化上下文管理器
        
        Args:
            max_context_size: 最大上下文消息数
        """
        self.contexts: Dict[str, deque] = {}  # chat_id -> 消息队列
        self.max_context_size = max_context_size
        self.logger = logging.getLogger(__name__)
    
    def add_message(self, chat_id: str, message_data: Dict[str, any]):
        """
        添加消息到上下文
        
        Args:
            chat_id: 聊天ID
            message_data: 消息数据 {text, user_id, username, timestamp}
        """
        if chat_id not in self.contexts:
            self.contexts[chat_id] = deque(maxlen=self.max_context_size)
        
        self.contexts[chat_id].append(message_data)
        self.logger.debug(f"添加上下文消息: {chat_id} - {message_data['text'][:50]}...")
    
    def get_context(self, chat_id: str, look_back: int = 10, look_forward: int = 0) -> List[Dict[str, any]]:
        """
        获取上下文消息
        
        Args:
            chat_id: 聊天ID
            look_back: 向后查看的消息数
            look_forward: 向前查看的消息数（当前未实现）
            
        Returns:
            上下文消息列表
        """
        if chat_id not in self.contexts:
            return []
        
        context_list = list(self.contexts[chat_id])
        
        # 获取最近的消息
        if len(context_list) <= look_back:
            return context_list
        else:
            return context_list[-look_back:]
    
    def analyze_context(self, chat_id: str, current_message: str) -> Dict[str, any]:
        """
        分析上下文
        
        Returns:
            分析结果 {
                'should_reply': bool,          # 是否需要回复
                'context_summary': str,        # 上下文摘要
                'user_emotion': str,           # 用户情绪
                'conversation_topic': str,     # 对话主题
                'needs_followup': bool,        # 是否需要跟进
            }
        """
        context_messages = self.get_context(chat_id, look_back=10)
        
        if not context_messages:
            return {
                'should_reply': True,  # 没有上下文，默认回复
                'context_summary': '',
                'user_emotion': 'neutral',
                'conversation_topic': '',
                'needs_followup': False
            }
        
        # 分析对话主题（简单实现）
        topics = []
        for msg in context_messages:
            text = msg['text'].lower()
            if any(word in text for word in ['订单', '下单', '查单']):
                topics.append('order')
            elif any(word in text for word in ['价格', '费率', '多少钱']):
                topics.append('price')
            elif any(word in text for word in ['客服', '帮助', '支持']):
                topics.append('support')
            elif any(word in text for word in ['问题', '故障', '错误']):
                topics.append('problem')
        
        # 确定主要主题
        conversation_topic = max(set(topics), key=topics.count) if topics else ''
        
        # 情绪分析（简单关键词匹配）
        user_emotion = self._analyze_emotion(current_message)
        
        # 决定是否需要回复
        should_reply = self._should_reply_based_on_context(context_messages, current_message)
        
        return {
            'should_reply': should_reply,
            'context_summary': f"最近{len(context_messages)}条消息，主题: {conversation_topic}",
            'user_emotion': user_emotion,
            'conversation_topic': conversation_topic,
            'needs_followup': len(context_messages) > 0 and should_reply
        }
    
    def _analyze_emotion(self, text: str) -> str:
        """简单情绪分析"""
        text_lower = text.lower()
        
        # 积极情绪关键词
        positive_words = ['谢谢', '感谢', '很好', '不错', '满意', '开心', '高兴']
        # 消极情绪关键词  
        negative_words = ['生气', '愤怒', '不满意', '糟糕', '差劲', '失望', '投诉']
        
        if any(word in text_lower for word in positive_words):
            return 'positive'
        elif any(word in text_lower for word in negative_words):
            return 'negative'
        else:
            return 'neutral'
    
    def _should_reply_based_on_context(self, context_messages: List, current_message: str) -> bool:
        """基于上下文决定是否需要回复"""
        # 检查最近消息中是否有未回复的@提及
        recent_messages = context_messages[-5:] if len(context_messages) >= 5 else context_messages
        
        for msg in recent_messages:
            if '@ai_zkw' in msg['text'] or 'Camille' in msg['text']:
                # 最近有@提及，可能需要回复
                return True
        
        # 检查对话是否在进行中
        if len(context_messages) >= 3:
            # 如果有连续的用户消息而没有AI回复，可能需要回复
            last_user_ids = [msg['user_id'] for msg in context_messages[-3:]]
            if len(set(last_user_ids)) == 1:  # 同一用户连续发送
                return True
        
        return True  # 默认回复
    
    def clear_context(self, chat_id: str):
        """清空指定聊天的上下文"""
        if chat_id in self.contexts:
            del self.contexts[chat_id]
            self.logger.info(f"已清空上下文: {chat_id}")
```

### **4. 情绪感知回复增强** (`src/skills/emotion_enhancer.py`)
```python
"""
情绪增强器 - 根据用户情绪调整回复
"""

import random
from typing import Dict, Optional

class EmotionEnhancer:
    """根据情绪增强回复内容"""
    
    def __init__(self, config):
        self.config = config
        self.emoticons_config = config.get('emoticons', {})
    
    def enhance_reply(self, original_reply: str, emotion: str, context: Dict) -> str:
        """
        根据情绪增强回复
        
        Args:
            original_reply: 原始AI回复
            emotion: 用户情绪 (positive/neutral/negative)
            context: 上下文信息
            
        Returns:
            增强后的回复
        """
        # 选择适当的表情符号
        emoticon = self._select_emoticon(emotion, context)
        
        # 调整回复语气
        enhanced_reply = self._adjust_tone(original_reply, emotion)
        
        # 添加表情符号（如果合适）
        if emoticon and self._should_add_emoticon(enhanced_reply):
            # 在适当位置添加表情符号
            if emotion == 'positive':
                # 积极情绪，在开头或结尾添加
                if random.random() > 0.5:
                    enhanced_reply = f"{emoticon} {enhanced_reply}"
                else:
                    enhanced_reply = f"{enhanced_reply} {emoticon}"
            elif emotion == 'negative':
                # 消极情绪，谨慎添加
                if "抱歉" in enhanced_reply or "对不起" in enhanced_reply:
                    enhanced_reply = f"{enhanced_reply} {emoticon}"
        
        return enhanced_reply
    
    def _select_emoticon(self, emotion: str, context: Dict) -> Optional[str]:
        """选择表情符号"""
        emotion_emoticons = self.emoticons_config.get(emotion, [])
        business_emoticons = self.emoticons_config.get('business', [])
        cs_emoticons = self.emoticons_config.get('customer_service', [])
        
        all_emoticons = emotion_emoticons + business_emoticons + cs_emoticons
        
        if all_emoticons:
            return random.choice(all_emoticons)
        return None
    
    def _adjust_tone(self, reply: str, emotion: str) -> str:
        """根据情绪调整语气"""
        if emotion == 'positive':
            # 积极情绪，可以更热情
            if reply.startswith('您好'):
                reply = reply.replace('您好', '您好呀', 1)
            elif reply.startswith('好的'):
                reply = reply.replace('好的', '好的呢', 1)
        elif emotion == 'negative':
            # 消极情绪，更委婉和同情
            if '抱歉' not in reply and '对不起' not in reply:
                reply = f"非常抱歉给您带来不便，{reply}"
        
        return reply
    
    def _should_add_emoticon(self, reply: str) -> bool:
        """判断是否应该添加表情符号"""
        # 避免在严肃内容中添加表情
        serious_keywords = ['投诉', '严重', '紧急', '重要', '警告']
        if any(keyword in reply for keyword in serious_keywords):
            return False
        
        # 避免过度使用表情
        existing_emoticons = sum(1 for char in reply if ord(char) > 10000)  # 粗略判断表情符号
        return existing_emoticons < 2
```

### **5. 高效回复决策引擎** (`src/skills/reply_decision_engine.py`)
```python
"""
回复决策引擎 - 智能决定是否需要回复
"""

class ReplyDecisionEngine:
    """决定是否需要回复以及回复优先级"""
    
    def __init__(self, config):
        self.config = config
        self.group_reply_config = config.get('telegram', {}).get('group_reply', {})
    
    async def should_reply(
        self, 
        message_text: str,
        chat_type: str,
        context_analysis: Dict,
        trigger_conditions_met: bool
    ) -> Dict[str, any]:
        """
        综合决定是否需要回复
        
        Returns:
            {
                'reply': bool,           # 是否回复
                'priority': int,         # 回复优先级 (1-5)
                'reason': str,           # 决定原因
                'delay_seconds': float,  # 延迟回复秒数
            }
        """
        # 基础条件检查
        if chat_type == 'private':
            # 私聊总是回复（除非垃圾消息）
            return {
                'reply': True,
                'priority': 1,
                'reason': 'private_chat',
                'delay_seconds': 0
            }
        
        # 群聊决策逻辑
        if not trigger_conditions_met:
            # 未触发基础条件（@提及或关键词）
            return {
                'reply': False,
                'priority': 0,
                'reason': 'no_trigger',
                'delay_seconds': 0
            }
        
        # 获取上下文分析结果
        should_reply_by_context = context_analysis.get('should_reply', True)
        user_emotion = context_analysis.get('user_emotion', 'neutral')
        
        if not should_reply_by_context:
            # 上下文分析认为不需要回复
            return {
                'reply': False,
                'priority': 0,
                'reason': 'context_suggests_no_reply',
                'delay_seconds': 0
            }
        
        # 决定回复优先级
        priority = self._calculate_priority(message_text, user_emotion, context_analysis)
        
        # 决定延迟时间（避免刷屏）
        delay_seconds = self._calculate_delay(priority, chat_type)
        
        return {
            'reply': True,
            'priority': priority,
            'reason': 'triggered_and_context_appropriate',
            'delay_seconds': delay_seconds
        }
    
    def _calculate_priority(self, message_text: str, emotion: str, context: Dict) -> int:
        """计算回复优先级 (1-5, 1最高)"""
        priority = 3  # 默认优先级
        
        # 情绪因素
        if emotion == 'negative':
            priority = 1  # 消极情绪优先回复
        elif emotion == 'positive':
            priority = 4  # 积极情绪可稍后回复
        
        # 关键词重要性
        urgent_keywords = ['紧急', '立刻', '马上', '快点', '着急']
        important_keywords = ['重要', '关键', '必须', '需要']
        
        if any(keyword in message_text for keyword in urgent_keywords):
            priority = 1
        elif any(keyword in message_text for keyword in important_keywords):
            priority = 2
        
        # 业务相关优先级调整
        if context.get('conversation_topic') == 'problem':
            priority = min(priority, 2)  # 问题类提高优先级
        
        return priority
    
    def _calculate_delay(self, priority: int, chat_type: str) -> float:
        """计算延迟回复时间"""
        if chat_type == 'private':
            return 0  # 私聊立即回复
        
        # 群聊根据优先级延迟
        delays = {
            1: 0.5,    # 最高优先级：0.5秒
            2: 2.0,    # 高优先级：2秒
            3: 5.0,    # 中等优先级：5秒
            4: 10.0,   # 低优先级：10秒
            5: 15.0    # 最低优先级：15秒
        }
        
        return delays.get(priority, 5.0)
```

## 🔧 实施步骤

### **阶段1: 基础配置更新** (5分钟)
1. **更新AI配置** (`config/config.yaml`)
   - 确认 `model: deepseek-chat`（DeepSeek-V3，已是项目当前默认）；如需更强推理能力可临时改 `deepseek-reasoner`
   - 设置超时为60秒
   - 更新系统提示词（**注意**：原文档示例的 Camille 人格 prompt 与本项目 customer_service 域可能不完全一致，调用前先 grep `system_prompt` 确认现状再合并）

2. **创建表情符号配置** (`config/emoticons.yaml`)
   - 定义情绪和业务表情映射
   - 创建stickers文件夹存放表情包图片

### **阶段2: 核心模块开发** (15分钟)
1. **上下文管理器** (`src/context/context_manager.py`)
   - 实现消息存储和检索
   - 添加简单情绪分析

2. **情绪增强器** (`src/skills/emotion_enhancer.py`)
   - 根据情绪调整回复语气
   - 智能添加表情符号

3. **回复决策引擎** (`src/skills/reply_decision_engine.py`)
   - 实现智能回复决策
   - 优先级和延迟计算

### **阶段3: 集成修改** (10分钟)
1. **修改Telegram客户端** (`src/client/telegram_client.py`)
   - 添加上下文管理器实例
   - 修改消息处理流程
   - 集成情绪增强和决策引擎

2. **更新Skill管理器** (`src/skills/skill_manager.py`)
   - 集成情绪感知
   - 优化回复生成流程

### **阶段4: 测试验证** (10分钟)
1. **功能测试**
   - 测试群组@提及触发
   - 验证上下文分析
   - 检查表情符号使用

2. **性能测试**
   - 验证响应时间
   - 测试并发处理
   - 检查内存使用

## 📊 预期效果

### **智能提升**
| 功能 | 优化前 | 优化后 |
|------|--------|--------|
| **模型智能度** | V2模型，基础理解 | V3模型，深度理解 |
| **情绪感知** | 无 | 基础情绪识别 |
| **上下文理解** | 单条消息 | 前后10条消息分析 |
| **回复个性化** | 模板化回复 | 情绪化、个性化回复 |

### **用户体验**
| 指标 | 优化前 | 优化后 |
|------|--------|--------|
| **回复相关性** | 60% | >85% |
| **响应自然度** | 机械 | 人性化，有情感 |
| **表情使用** | 无 | 适当表情符号 |
| **对话连贯性** | 差 | 良好 |

### **性能指标**
| 指标 | 目标值 |
|------|--------|
| **平均响应时间** | <8秒 |
| **AI调用成功率** | >95% |
| **上下文分析延迟** | <100ms |
| **系统资源占用** | <200MB内存 |

## ⚠️ 注意事项

### **技术风险**
1. **V3模型成本**: 可能比V2略高，需监控API使用
2. **响应时间**: 60秒超时可能增加用户等待感
3. **情绪分析准确性**: 简单关键词匹配可能不准确
4. **表情符号过度使用**: 需避免影响专业性

### **缓解措施**
1. **成本控制**: 添加API使用监控和限制
2. **超时优化**: 实际响应目标在5-10秒内
3. **情绪分析改进**: 可后续升级为AI情绪分析
4. **表情符号控制**: 添加使用频率限制

## 🚀 立即执行建议

### **优先实施**
1. ✅ 更新AI模型配置 (立即生效)
2. ✅ 添加上下文缓存基础功能
3. ✅ 实现简单情绪分析
4. ✅ 集成表情符号系统

### **后续优化**
1. 🔄 升级情绪分析为AI模型
2. 🔄 添加更复杂的上下文理解
3. 🔄 实现个性化学习
4. 🔄 优化性能监控

### **验证清单**
- [ ] AI配置更新生效
- [ ] 上下文分析正常工作
- [ ] 情绪感知正确识别
- [ ] 表情符号适当添加
- [ ] 群组触发机制正常
- [ ] 响应时间可接受
- [ ] 系统稳定性良好

## 📞 技术支持

如需实施帮助，请提供：
1. 当前系统状态确认
2. 测试环境准备
3. 具体问题描述

**优化方案已设计完成，等待您的确认后即可开始实施。**