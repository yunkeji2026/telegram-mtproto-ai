"""
上下文管理器 - 存储和分析对话上下文
支持前后消息分析、情绪感知和持久化恢复
"""

import json
import logging
import os
from typing import Dict, List, Optional, Any
from collections import deque
from pathlib import Path
import re
import time


class ContextManager:
    """管理对话上下文，支持前后消息分析"""
    
    def __init__(self, config, max_context_size: int = 20):
        self.config = config
        self.contexts: Dict[str, deque] = {}
        self.max_context_size = max_context_size
        self.logger = logging.getLogger(__name__)
        
        self.emotion_keywords = {
            'positive': ['谢谢', '感谢', '很好', '不错', '满意', '开心', '高兴', '棒', '优秀', '完美'],
            'negative': ['生气', '愤怒', '不满意', '糟糕', '差劲', '失望', '投诉', '垃圾', '骗人', '坑爹'],
            'urgent': ['紧急', '立刻', '马上', '快点', '着急', '急急急', '尽快', '立刻马上'],
            'important': ['重要', '关键', '必须', '需要', '务必', '一定', '千万']
        }
        
        self.business_keywords = {
            'order': ['订单', '下单', '查单', '单号', '订单号', '物流', '发货', '快递'],
            'price': ['价格', '价钱', '多少钱', '费率', '费用', '收费', '报价', '成本'],
            'service': ['客服', '帮助', '支持', '人工', '咨询', '联系', '电话', '微信'],
            'problem': ['问题', '故障', '错误', 'bug', '不能用', '打不开', '失效', '异常']
        }
        
        self._max_chats = max_context_size * 50
        self._last_gc = time.time()
        self._gc_interval = 600
        self._dirty = False

        # 持久化：JSON 快照
        self._persist_path: Optional[Path] = None
        self._persist_interval = 120
        self._last_persist = time.time()
        try:
            cfg_path = getattr(config, 'config_path', None)
            if cfg_path:
                self._persist_path = Path(cfg_path).parent / "context_snapshot.json"
                self._restore_snapshot()
        except Exception as e:
            self.logger.warning("上下文持久化路径初始化失败: %s", e)

        self.logger.info("上下文管理器初始化完成")

    # ── 持久化 ───────────────────────────────────────────────
    def _restore_snapshot(self):
        if not self._persist_path or not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
            cutoff = time.time() - 12 * 3600
            restored = 0
            for chat_id, msgs in data.items():
                valid = [m for m in msgs if m.get("timestamp", 0) > cutoff]
                if valid:
                    dq = deque(valid[-self.max_context_size:], maxlen=self.max_context_size)
                    self.contexts[chat_id] = dq
                    restored += 1
            self.logger.info("从快照恢复了 %d 个聊天上下文", restored)
        except Exception as e:
            self.logger.warning("恢复上下文快照失败: %s", e)

    def persist_snapshot(self):
        if not self._persist_path:
            return
        try:
            out = {}
            for chat_id, dq in self.contexts.items():
                out[chat_id] = list(dq)
            tmp = self._persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(out, ensure_ascii=False, default=str), encoding="utf-8")
            tmp.replace(self._persist_path)
            self._dirty = False
            self.logger.debug("上下文快照已保存 (%d chats)", len(out))
        except Exception as e:
            self.logger.warning("保存上下文快照失败: %s", e)

    def _maybe_persist(self):
        if not self._dirty or not self._persist_path:
            return
        now = time.time()
        if now - self._last_persist >= self._persist_interval:
            self._last_persist = now
            self.persist_snapshot()
    
    def add_message(self, chat_id: str, *args, **kwargs):
        """
        添加消息到上下文
        
        支持两种调用方式：
        1. add_message(chat_id, message_data_dict)
        2. add_message(chat_id, user_id, username, text, is_ai=False)
        
        Args:
            chat_id: 聊天ID (格式: "group_123456" 或 "private_123456")
            *args: 参数，可以是message_data字典或(user_id, username, text, is_ai)
            **kwargs: 关键字参数，用于message_data字段
        """
        if not chat_id:
            return
        
        # 构建消息数据
        message_data = {}
        
        if len(args) == 1 and isinstance(args[0], dict):
            # 方式1: 传递字典
            message_data = args[0]
        elif len(args) >= 3:
            # 方式2: 传递多个参数 (user_id, username, text, is_ai)
            message_data = {
                'user_id': str(args[0]) if len(args) > 0 else 'unknown',
                'username': str(args[1]) if len(args) > 1 else 'unknown',
                'text': str(args[2]) if len(args) > 2 else '',
                'is_bot': bool(args[3]) if len(args) > 3 else False
            }
        
        # 合并关键字参数
        message_data.update(kwargs)
        
        # 确保必要字段
        if 'text' not in message_data or not message_data['text']:
            self.logger.warning(f"添加上下文消息缺少text字段: {chat_id}")
            return
            
        if 'timestamp' not in message_data:
            message_data['timestamp'] = time.time()
        
        if 'user_id' not in message_data:
            message_data['user_id'] = 'unknown'
        
        if 'username' not in message_data:
            message_data['username'] = 'unknown'
        
        if 'is_bot' not in message_data:
            message_data['is_bot'] = False
        
        # 初始化聊天上下文
        if chat_id not in self.contexts:
            self.contexts[chat_id] = deque(maxlen=self.max_context_size)
        
        self.contexts[chat_id].append(message_data)
        self._dirty = True

        now = time.time()
        if now - self._last_gc > self._gc_interval or len(self.contexts) > self._max_chats:
            self._last_gc = now
            self.cleanup_old_contexts(max_age_hours=12)

        self._maybe_persist()
        
        text_preview = message_data['text'][:50] + '...' if len(message_data['text']) > 50 else message_data['text']
        self.logger.debug(f"添加上下文: {chat_id} - {message_data['username']}: {text_preview}")
    
    def get_context(self, chat_id: str, look_back: int = 10, look_forward: int = 0) -> List[Dict[str, any]]:
        """
        获取上下文消息
        
        Args:
            chat_id: 聊天ID
            look_back: 向后查看的消息数
            look_forward: 向前查看的消息数（当前未实现）
            
        Returns:
            上下文消息列表，按时间顺序排列（最早到最新）
        """
        if chat_id not in self.contexts:
            return []
        
        context_list = list(self.contexts[chat_id])
        
        # 获取最近的消息
        if len(context_list) <= look_back:
            return context_list
        else:
            return context_list[-look_back:]
    
    def analyze_context(self, chat_id: str, current_context: Any = None) -> Dict[str, any]:
        """
        分析上下文，决定回复策略
        
        Args:
            chat_id: 聊天ID
            current_context: 当前上下文，可以是字符串（消息文本）或用户ID
            
        Returns:
            分析结果 {
                'should_reply': bool,          # 是否需要回复
                'context_summary': str,        # 上下文摘要
                'user_emotion': str,           # 用户情绪: positive/neutral/negative
                'conversation_topic': str,     # 对话主题
                'needs_followup': bool,        # 是否需要跟进
                'priority': int,               # 优先级 1-5 (1最高)
                'suggested_emoticons': list,   # 建议使用的表情符号
            }
        """
        context_messages = self.get_context(chat_id, look_back=10)
        
        # 基础分析结果
        analysis = {
            'should_reply': True,
            'context_summary': '',
            'user_emotion': 'neutral',
            'conversation_topic': '',
            'needs_followup': False,
            'priority': 3,
            'suggested_emoticons': [],
            'context_message_count': len(context_messages)
        }
        
        if not context_messages:
            analysis['context_summary'] = '无上下文消息'
            return analysis
        
        # 获取当前消息文本（从上下文或参数）
        current_message = ""
        if isinstance(current_context, str) and current_context:
            # 如果传递的是字符串，假设是消息文本
            current_message = current_context
        elif context_messages:
            # 如果没有传递消息文本，使用最新的消息
            latest_message = context_messages[-1]
            current_message = latest_message.get('text', '')
        
        # 分析对话主题
        topics = self._analyze_topics(context_messages + [{'text': current_message}])
        main_topic = self._get_main_topic(topics)
        analysis['conversation_topic'] = main_topic
        
        # 分析用户情绪
        all_texts = [msg['text'] for msg in context_messages if 'text' in msg] + [current_message]
        emotion = self._analyze_emotion(' '.join(all_texts))
        analysis['user_emotion'] = emotion
        
        # 检查是否需要跟进
        needs_followup = self._check_needs_followup(context_messages, current_message)
        analysis['needs_followup'] = needs_followup
        
        # 决定是否需要回复
        should_reply = self._should_reply_based_on_context(context_messages, current_message, main_topic, emotion)
        analysis['should_reply'] = should_reply
        
        # 计算优先级
        priority = self._calculate_priority(current_message, emotion, main_topic, needs_followup)
        analysis['priority'] = priority
        
        # 生成上下文摘要
        summary = self._generate_context_summary(context_messages, main_topic, emotion)
        analysis['context_summary'] = summary
        
        # 建议表情符号
        suggested_emoticons = self._suggest_emoticons(emotion, main_topic, priority)
        analysis['suggested_emoticons'] = suggested_emoticons
        
        return analysis
    
    def _analyze_topics(self, messages: List[Dict]) -> Dict[str, int]:
        """分析对话主题"""
        topic_counts = {key: 0 for key in self.business_keywords.keys()}
        
        for msg in messages:
            text = msg.get('text', '').lower()
            for topic, keywords in self.business_keywords.items():
                for keyword in keywords:
                    if keyword in text:
                        topic_counts[topic] += 1
        
        return topic_counts
    
    def _get_main_topic(self, topic_counts: Dict[str, int]) -> str:
        """获取主要主题"""
        if not topic_counts:
            return 'general'
        
        # 找到计数最高的主题
        max_topic = max(topic_counts.items(), key=lambda x: x[1])
        
        if max_topic[1] > 0:
            return max_topic[0]
        return 'general'
    
    def _analyze_emotion(self, text: str) -> str:
        """分析文本情绪"""
        text_lower = text.lower()
        
        positive_count = sum(1 for word in self.emotion_keywords['positive'] if word in text_lower)
        negative_count = sum(1 for word in self.emotion_keywords['negative'] if word in text_lower)
        
        if positive_count > negative_count:
            return 'positive'
        elif negative_count > positive_count:
            return 'negative'
        else:
            return 'neutral'
    
    def _check_needs_followup(self, context_messages: List[Dict], current_message: str) -> bool:
        """检查是否需要跟进"""
        if len(context_messages) < 2:
            return False
        
        # 检查最近是否有bot回复
        recent_messages = context_messages[-3:] if len(context_messages) >= 3 else context_messages
        has_bot_reply = any(msg.get('is_bot', False) for msg in recent_messages)
        
        # 检查是否有未解决的问题
        last_bot_msg = None
        for msg in reversed(context_messages):
            if msg.get('is_bot', False):
                last_bot_msg = msg
                break
        
        if last_bot_msg:
            # 如果bot最后询问了问题，用户现在可能是在回答
            bot_text = last_bot_msg.get('text', '').lower()
            if any(word in bot_text for word in ['请提供', '请告诉我', '请问', '需要']):
                return True
        
        return False
    
    def _should_reply_based_on_context(
        self, 
        context_messages: List[Dict], 
        current_message: str,
        topic: str,
        emotion: str
    ) -> bool:
        """基于上下文决定是否需要回复"""
        # 如果有@提及，总是考虑回复
        if '@ai_zkw' in current_message or 'Camille' in current_message:
            return True
        
        # 检查最近消息中是否有未回复的@提及
        recent_messages = context_messages[-5:] if len(context_messages) >= 5 else context_messages
        for msg in recent_messages:
            if '@ai_zkw' in msg.get('text', '') or 'Camille' in msg.get('text', ''):
                return True
        
        # 消极情绪优先回复
        if emotion == 'negative':
            return True
        
        # 问题类主题优先回复
        if topic == 'problem':
            return True
        
        # 检查是否连续的用户消息（可能是在等待回复）
        if len(context_messages) >= 3:
            last_three = context_messages[-3:]
            # 如果连续3条都是用户消息（非bot），可能需要回复
            user_message_count = sum(1 for msg in last_three if not msg.get('is_bot', False))
            if user_message_count >= 3:
                return True
        
        # 默认回复（由触发条件决定）
        return True
    
    def _calculate_priority(self, message: str, emotion: str, topic: str, needs_followup: bool) -> int:
        """计算回复优先级 (1-5, 1最高)"""
        priority = 3  # 默认优先级
        
        # 情绪因素
        if emotion == 'negative':
            priority = 1
        elif emotion == 'positive':
            priority = 4
        
        # 紧急关键词
        for urgent_word in self.emotion_keywords['urgent']:
            if urgent_word in message.lower():
                priority = 1
                break
        
        # 重要关键词
        for important_word in self.emotion_keywords['important']:
            if important_word in message.lower():
                priority = min(priority, 2)
                break
        
        # 主题优先级
        if topic == 'problem':
            priority = min(priority, 2)
        elif topic == 'order':
            priority = min(priority, 3)
        
        # 跟进对话优先级更高
        if needs_followup:
            priority = min(priority, 2)
        
        return max(1, min(5, priority))  # 确保在1-5范围内
    
    def _generate_context_summary(self, context_messages: List[Dict], topic: str, emotion: str) -> str:
        """生成上下文摘要"""
        if not context_messages:
            return "无上下文"
        
        user_count = len(set(msg.get('username', '') for msg in context_messages))
        message_count = len(context_messages)
        
        summary_parts = []
        
        if message_count > 0:
            summary_parts.append(f"最近{message_count}条消息")
        
        if user_count > 1:
            summary_parts.append(f"{user_count}位用户参与")
        
        if topic != 'general':
            topic_names = {
                'order': '订单',
                'price': '价格',
                'service': '客服',
                'problem': '问题'
            }
            summary_parts.append(f"主题: {topic_names.get(topic, topic)}")
        
        if emotion != 'neutral':
            emotion_names = {
                'positive': '积极',
                'negative': '消极'
            }
            summary_parts.append(f"情绪: {emotion_names.get(emotion, emotion)}")
        
        return "，".join(summary_parts)
    
    def _suggest_emoticons(self, emotion: str, topic: str, priority: int) -> List[str]:
        """建议表情符号"""
        suggestions = []
        
        # 根据情绪添加表情
        # 与 config/emoticons.yaml 中性池对齐，避免固定「👉📝」水印感
        emotion_emoticons = {
            'positive': ['😊', '👍', '🙏'],
            'neutral': ['💭', '✨', '🤗'],
            'negative': ['😔', '🙁', '⚠️']
        }
        
        if emotion in emotion_emoticons:
            suggestions.extend(emotion_emoticons[emotion][:2])
        
        # 根据主题添加表情
        topic_emoticons = {
            'order': ['📦', '🔄'],
            'price': ['💰', '📊'],
            'service': ['🙋', '💬'],
            'problem': ['🔧', '🔄']
        }
        
        if topic in topic_emoticons:
            suggestions.extend(topic_emoticons[topic])
        
        # 根据优先级调整
        if priority <= 2:
            suggestions.append('⚠️' if emotion == 'negative' else '⏰')
        
        # 去重并限制数量
        unique_suggestions = []
        for emoticon in suggestions:
            if emoticon not in unique_suggestions:
                unique_suggestions.append(emoticon)
        
        return unique_suggestions[:3]  # 最多返回3个
    
    def clear_context(self, chat_id: str):
        """清空指定聊天的上下文"""
        if chat_id in self.contexts:
            del self.contexts[chat_id]
            self.logger.info(f"已清空上下文: {chat_id}")
    
    def cleanup_old_contexts(self, max_age_hours: int = 24):
        """清理过期的上下文；若超出上限则进一步 LRU 淘汰"""
        current_time = time.time()
        cutoff_time = current_time - (max_age_hours * 3600)

        chats_to_remove = []
        for chat_id, messages in self.contexts.items():
            if not messages:
                chats_to_remove.append(chat_id)
                continue
            latest_time = messages[-1].get('timestamp', 0)
            if latest_time < cutoff_time:
                chats_to_remove.append(chat_id)

        for chat_id in chats_to_remove:
            del self.contexts[chat_id]

        if len(self.contexts) > self._max_chats:
            ranked = sorted(
                self.contexts.items(),
                key=lambda kv: kv[1][-1].get('timestamp', 0) if kv[1] else 0,
            )
            excess = len(self.contexts) - self._max_chats
            for chat_id, _ in ranked[:excess]:
                del self.contexts[chat_id]
                chats_to_remove.append(chat_id)

        if chats_to_remove:
            self.logger.info("清理了 %d 个上下文 (剩余 %d)", len(chats_to_remove), len(self.contexts))
    
    def get_statistics(self) -> Dict[str, any]:
        """获取统计信息"""
        total_chats = len(self.contexts)
        total_messages = sum(len(messages) for messages in self.contexts.values())
        
        return {
            'total_chats': total_chats,
            'total_messages': total_messages,
            'avg_messages_per_chat': total_messages / total_chats if total_chats > 0 else 0
        }