"""
情绪增强器 - 根据用户情绪和上下文调整回复
添加表情符号和语气调整，语言感知（非中文回复跳过中文语气词）
"""

import random
import re
from typing import Dict, List, Optional, Tuple


def _reply_is_chinese(text: str) -> bool:
    """判断回复文本主要语言是否为中文（含少量英文通道名等）"""
    if not text:
        return True
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk == 0:
        return False
    if latin == 0:
        return True
    return cjk / (cjk + latin) > 0.3


class EmotionEnhancer:
    """根据情绪增强回复内容"""

    _domain_skip_phrases: List[str] = []
    _domain_skip_patterns: List[Tuple[List[str], List[str]]] = []

    @classmethod
    def set_domain_skip_rules(cls, skip_phrases: List[str] = None,
                              skip_patterns: List[Tuple[List[str], List[str]]] = None):
        """Configure domain-specific skip rules.
        skip_phrases: if any phrase is found in the reply, skip enhancement.
        skip_patterns: list of (all_of, any_of) tuples — skip if ALL of all_of
                       and ANY of any_of are present in the reply.
        """
        cls._domain_skip_phrases = skip_phrases or []
        cls._domain_skip_patterns = skip_patterns or []

    def __init__(self, config):
        self.config = config
        
        try:
            emoticons_config = config.get('emoticons', {})
            self.emoticons = emoticons_config.get('emoticons', {})
            self.rules = emoticons_config.get('rules', {})
            raw_nat = emoticons_config.get('naturalization')
            _nat_defaults = {
                "enabled": True,
                "skip_emoticon_pass_probability": 0.32,
                "ignore_context_suggestions_probability": 0.28,
                "max_consecutive_decorated": 2,
                "forbidden_emoticons": ["👉", "📝"],
            }
            if raw_nat is None or raw_nat == {}:
                self._naturalization = dict(_nat_defaults)
            else:
                merged = dict(_nat_defaults)
                merged.update(raw_nat)
                self._naturalization = merged
        except Exception:
            self.emoticons = {}
            self.rules = {}
            self._naturalization = {
                "enabled": True,
                "skip_emoticon_pass_probability": 0.32,
                "ignore_context_suggestions_probability": 0.28,
                "max_consecutive_decorated": 2,
                "forbidden_emoticons": ["👉", "📝"],
            }
        
        if not self.emoticons:
            self.emoticons = {
                'positive': ['😊', '👍', '🙏', '🎉'],
                'neutral': ['💭', '✨', '🤗', '⏰'],
                'negative': ['😔', '🙁', '😥', '⚠️'],
                'business': ['💰', '📦', '🔄', '✅'],
                'customer_service': ['👋', '🙋', '💬', '📞']
            }
        
        self.keyword_emoticon_map = self._build_keyword_emoticon_map()
        # 会话级：连续「带装饰」条数、用于冷却（chat_id -> {dec_streak: int}）
        self._chat_decor_state: Dict[str, Dict[str, int]] = {}
        
        self.tone_adjustments = {
            'positive': {
                '好的': '好的呢',
                '可以': '可以的呀',
                '明白': '明白啦',
                '收到': '收到啦',
                '谢谢': '不用啦',
            },
            'negative': {
                '明白': '感受到了',
                '收到': '收到了，我知道了'
            },
            'neutral': {
                '你好！': '你好呀～',
                '在啊！': '在呢～',
            }
        }  # S3: 移除客服腔短语（您好/请问有什么/欢迎咨询/订单状态等）
        self.tone_adjustments_en = {
            'positive': {
                'Hello': 'Hi there',
                'Thank you': 'Thanks a lot',
            },
            'negative': {},
            'neutral': {},
        }
    
    def _build_keyword_emoticon_map(self) -> Dict[str, List[str]]:
        """构建关键词到表情符号的映射"""
        keyword_triggers = self.rules.get('keyword_triggers', [])
        mapping = {}
        
        for trigger in keyword_triggers:
            keywords = trigger.get('keywords', [])
            emoticons = trigger.get('emoticons', [])
            
            for keyword in keywords:
                mapping[keyword] = emoticons
        
        # 添加默认映射
        default_mappings = {
            '价格': ['💰'],
            '订单': ['📦'],
            '谢谢': ['🙏'],
            '投诉': ['😔'],
            '帮助': ['🙋'],
            '客服': ['💬'],
            '紧急': ['⚠️'],
            '重要': ['📍']
        }
        
        for keyword, emoticons in default_mappings.items():
            if keyword not in mapping:
                mapping[keyword] = emoticons
        
        return mapping
    
    def enhance_reply(
        self,
        original_reply: str,
        emotion: str,
        context_analysis: Dict,
        message_text: str = "",
        *,
        chat_id: Optional[str] = None,
    ) -> str:
        """
        根据情绪和上下文增强回复。
        自动检测回复语言，非中文回复跳过中文语气词替换。
        chat_id: 用于会话级表情冷却与随机疏密（P0 自然化）。
        """
        if not original_reply:
            return original_reply
        r = original_reply.strip()
        nat = getattr(self, "_naturalization", {}) or {}
        if self._should_skip_enhancement(r):
            return self._strip_forbidden_watermark_chars(original_reply, nat)

        is_zh = _reply_is_chinese(r)
        enhanced_reply = original_reply

        enhanced_reply = self._adjust_tone(enhanced_reply, emotion, is_zh=is_zh)

        nat_on = bool(nat.get("enabled", True))
        state_key = str(chat_id) if chat_id is not None else "_default"
        st = self._chat_decor_state.setdefault(state_key, {"dec_streak": 0})

        ca_in = dict(context_analysis or {})
        if nat_on and random.random() < float(
            nat.get("ignore_context_suggestions_probability", 0.28)
        ):
            ca_in["suggested_emoticons"] = []

        emoticons_to_add: List[str] = []
        if nat_on:
            max_streak = int(nat.get("max_consecutive_decorated", 2))
            if int(st.get("dec_streak", 0)) >= max_streak:
                emoticons_to_add = []
                st["dec_streak"] = 0
            elif random.random() < float(nat.get("skip_emoticon_pass_probability", 0.32)):
                emoticons_to_add = []
                st["dec_streak"] = 0
            else:
                emoticons_to_add = self._select_emoticons(
                    enhanced_reply, emotion, ca_in, message_text
                )
                emoticons_to_add = self._filter_forbidden_emoticons(
                    emoticons_to_add, nat
                )
        else:
            emoticons_to_add = self._select_emoticons(
                enhanced_reply, emotion, ca_in, message_text
            )

        will_add = bool(emoticons_to_add) and self._should_add_emoticons(
            enhanced_reply, emoticons_to_add, emotion
        )
        enhanced_reply = self._add_emoticons(enhanced_reply, emoticons_to_add, emotion)
        enhanced_reply = self._cleanup_format(enhanced_reply, is_zh=is_zh)

        if nat_on:
            if will_add and emoticons_to_add:
                st["dec_streak"] = int(st.get("dec_streak", 0)) + 1
            else:
                st["dec_streak"] = 0
            if len(self._chat_decor_state) > 5000:
                self._chat_decor_state.clear()

        # 模型原文或历史逻辑可能在句首/句末带 👉📝；统一剥除（与是否追加新表情无关）
        enhanced_reply = self._strip_forbidden_watermark_chars(
            enhanced_reply, getattr(self, "_naturalization", {}) or {}
        )

        return enhanced_reply

    def _strip_forbidden_watermark_chars(self, text: str, nat: Dict) -> str:
        """去掉句首/句末配置的「水印」类 emoji（多轮剥离以处理 👉 📝 组合）。"""
        fb = list(nat.get("forbidden_emoticons") or ["👉", "📝"])
        if not fb or not (text or "").strip():
            return text
        t = text
        for _ in range(8):
            s = t.lstrip()
            if not s:
                break
            hit = False
            for fe in fb:
                if fe and s.startswith(fe):
                    t = s[len(fe) :]
                    hit = True
                    break
            if not hit:
                break
        for _ in range(8):
            s = t.rstrip()
            if not s:
                break
            hit = False
            for fe in fb:
                if fe and s.endswith(fe):
                    t = s[: -len(fe)]
                    hit = True
                    break
            if not hit:
                break
        return t.strip()

    def _filter_forbidden_emoticons(self, emoticons: List[str], nat: Dict) -> List[str]:
        forbidden = nat.get("forbidden_emoticons") or ["👉", "📝"]
        fb = set(str(x) for x in forbidden)
        return [e for e in emoticons if e not in fb]
    
    def _should_skip_enhancement(self, reply: str) -> bool:
        """Check if enhancement should be skipped based on domain skip rules."""
        for phrase in self._domain_skip_phrases:
            if phrase in reply:
                return True
        for all_of, any_of in self._domain_skip_patterns:
            if all(term in reply for term in all_of) and any(term in reply for term in any_of):
                return True
        return False

    def _adjust_tone(self, reply: str, emotion: str, *, is_zh: bool = True) -> str:
        """根据情绪调整语气，非中文回复跳过中文替换规则"""
        if not is_zh:
            adj_en = self.tone_adjustments_en.get(emotion, {})
            for orig, repl in adj_en.items():
                if reply.startswith(orig):
                    reply = reply.replace(orig, repl, 1)
            if emotion == 'negative':
                apology_en = ['sorry', 'apolog', 'regret']
                if not any(kw in reply.lower() for kw in apology_en):
                    reply = f"We're sorry for the inconvenience. {reply}"
            return reply

        if emotion not in self.tone_adjustments:
            return reply
        
        adjustments = self.tone_adjustments[emotion]
        
        for original, replacement in adjustments.items():
            if original in reply:
                if reply.startswith(original):
                    reply = reply.replace(original, replacement, 1)
                elif f" {original}" in reply:
                    if emotion == 'negative':
                        reply = reply.replace(f" {original}", f" {replacement}")
        
        if emotion == 'negative' and '抱歉' not in reply and '对不起' not in reply:
            apology_keywords = ['抱歉', '对不起', '不好意思', '请谅解']
            if not any(keyword in reply for keyword in apology_keywords):
                reply = f"非常抱歉给您带来不便，{reply}"
        
        return reply
    
    def _select_emoticons(
        self, 
        reply: str, 
        emotion: str, 
        context_analysis: Dict,
        message_text: str
    ) -> List[str]:
        """选择要添加的表情符号"""
        selected_emoticons = []
        
        # 1. 从上下文分析中获取建议的表情
        suggested = context_analysis.get('suggested_emoticons', [])
        if suggested:
            selected_emoticons.extend(suggested[:2])
        
        # 2. 根据情绪添加表情
        emotion_emoticons = self.emoticons.get(emotion, [])
        if emotion_emoticons and len(selected_emoticons) < 3:
            # 从情绪表情中随机选择，避免总是用同一个
            available = [e for e in emotion_emoticons if e not in selected_emoticons]
            if available:
                selected_emoticons.append(random.choice(available))
        
        # 3. 根据关键词添加表情
        keyword_emoticons = self._get_emoticons_for_keywords(message_text, reply)
        for emoticon in keyword_emoticons:
            if emoticon not in selected_emoticons and len(selected_emoticons) < 3:
                selected_emoticons.append(emoticon)
        
        # 4. 根据业务主题添加表情
        topic = context_analysis.get('conversation_topic', '')
        if topic:
            business_emoticons = self.emoticons.get('business', [])
            if business_emoticons and topic in ['order', 'price', 'service', 'problem']:
                # 为业务主题选择相关表情
                topic_emoticon_map = {
                    'order': '📦',
                    'price': '💰',
                    'service': '🙋',
                    'problem': '🔧'
                }
                if topic in topic_emoticon_map:
                    emoticon = topic_emoticon_map[topic]
                    if emoticon not in selected_emoticons and len(selected_emoticons) < 3:
                        selected_emoticons.append(emoticon)
        
        # 5. 确保不超过最大数量
        max_emoticons = self.rules.get('max_emoticons_per_message', 3)
        selected_emoticons = selected_emoticons[:max_emoticons]
        
        # 6. 去重
        unique_emoticons = []
        for emoticon in selected_emoticons:
            if emoticon not in unique_emoticons:
                unique_emoticons.append(emoticon)
        
        return unique_emoticons
    
    def _get_emoticons_for_keywords(self, message_text: str, reply_text: str) -> List[str]:
        """根据关键词获取表情符号"""
        emoticons = []
        all_text = f"{message_text} {reply_text}".lower()
        
        for keyword, keyword_emoticons in self.keyword_emoticon_map.items():
            if keyword in all_text:
                emoticons.extend(keyword_emoticons)
        
        # 去重
        return list(set(emoticons))
    
    def _add_emoticons(self, reply: str, emoticons: List[str], emotion: str) -> str:
        """将表情符号添加到回复中"""
        if not emoticons:
            return reply
        
        # 检查是否应该添加表情
        if not self._should_add_emoticons(reply, emoticons, emotion):
            return reply
        
        # 决定添加位置
        placement = self._determine_emoticon_placement(reply, emotion)
        
        if placement == 'beginning':
            # 在开头添加1-2个表情
            emoticons_to_add = emoticons[:2]
            reply = f"{' '.join(emoticons_to_add)} {reply}"
        elif placement == 'end':
            # 在结尾添加1-2个表情
            emoticons_to_add = emoticons[:2]
            reply = f"{reply} {' '.join(emoticons_to_add)}"
        elif placement == 'both':
            # 开头和结尾各添加一个
            if len(emoticons) >= 2:
                reply = f"{emoticons[0]} {reply} {emoticons[1]}"
            else:
                reply = f"{emoticons[0]} {reply} {emoticons[0]}"
        else:  # 'none' or unknown
            return reply
        
        return reply
    
    def _should_add_emoticons(self, reply: str, emoticons: List[str], emotion: str) -> bool:
        """判断是否应该添加表情符号"""
        # 检查回复长度
        min_length = self.rules.get('min_message_length_for_emoticon', 10)
        if len(reply.strip()) < min_length:
            return False
        
        # 检查是否在避免使用表情的场景中
        avoid_scenarios = self.rules.get('avoid_emoticons_in', [])
        
        serious_keywords = [
            '投诉', '严重', '紧急', '重要', '警告', '法律', '律师', '起诉',
            'complaint', 'legal', 'lawyer', 'lawsuit', 'fraud', 'scam', 'police',
        ]
        if any(keyword in reply.lower() for keyword in serious_keywords):
            return False
        
        # 检查回复中是否已经有太多表情
        existing_emoticons = self._count_existing_emoticons(reply)
        max_allowed = self.rules.get('max_emoticons_per_message', 3)
        if existing_emoticons >= max_allowed:
            return False
        
        # 根据情绪决定
        if emotion == 'negative' and '😔' not in emoticons and '🙁' not in emoticons:
            # 消极情绪只使用特定表情
            return False
        
        return True
    
    _EMOJI_RE = re.compile(
        "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U0001F251"
        "\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
        "\U00002600-\U000026FF\U0000FE00-\U0000FE0F\U0000200D]+",
        re.UNICODE,
    )

    def _count_existing_emoticons(self, text: str) -> int:
        """计算文本中已有的 emoji 数量（不误计中文/阿拉伯/印地文等）"""
        return len(self._EMOJI_RE.findall(text))
    
    def _determine_emoticon_placement(self, reply: str, emotion: str) -> str:
        """决定表情符号的放置位置"""
        # 根据情绪和回复内容决定
        if emotion == 'positive':
            # 积极情绪，可以放在开头或结尾
            return random.choice(['beginning', 'end', 'both'])
        elif emotion == 'negative':
            # 消极情绪，谨慎放置，通常在结尾
            return 'end'
        else:  # neutral
            # 中性情绪，随机选择
            options = ['beginning', 'end', 'none']
            weights = [0.3, 0.3, 0.4]  # 40%概率不添加
            return random.choices(options, weights=weights, k=1)[0]
    
    def _cleanup_format(self, reply: str, *, is_zh: bool = True) -> str:
        """清理回复格式：仅合并多余空白与标点后空格，不做 emoji 范围替换（避免误伤中文）。"""
        reply = re.sub(r'\s+', ' ', reply.strip())
        if is_zh:
            reply = re.sub(r'([。！？])\s*', r'\1', reply)
        return reply
    
    def analyze_message_emotion(self, message_text: str) -> Dict[str, any]:
        """分析消息情绪（简化版），支持中英文关键词"""
        text_lower = message_text.lower()
        
        positive_keywords = [
            '谢谢', '感谢', '很好', '不错', '满意', '开心', '高兴',
            'thank', 'thanks', 'great', 'good', 'nice', 'happy', 'excellent', 'perfect',
        ]
        positive_count = sum(1 for word in positive_keywords if word in text_lower)
        
        negative_keywords = [
            '生气', '愤怒', '不满意', '糟糕', '差劲', '失望', '投诉',
            'angry', 'upset', 'terrible', 'worst', 'disappointed', 'complain', 'fraud', 'scam',
        ]
        negative_count = sum(1 for word in negative_keywords if word in text_lower)
        
        if positive_count > negative_count:
            emotion = 'positive'
            confidence = positive_count / (positive_count + negative_count + 1)
        elif negative_count > positive_count:
            emotion = 'negative'
            confidence = negative_count / (positive_count + negative_count + 1)
        else:
            emotion = 'neutral'
            confidence = 0.5
        
        urgent_keywords = [
            '紧急', '立刻', '马上', '快点', '着急',
            'urgent', 'asap', 'immediately', 'hurry',
        ]
        is_urgent = any(word in text_lower for word in urgent_keywords)
        
        return {
            'emotion': emotion,
            'confidence': round(confidence, 2),
            'positive_count': positive_count,
            'negative_count': negative_count,
            'is_urgent': is_urgent
        }