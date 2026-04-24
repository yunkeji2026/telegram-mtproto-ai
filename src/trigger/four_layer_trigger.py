"""
四层触发决策器 - 实现防误判率 < 3% 的智能触发机制
整合 L1规则触发 → L2语义触发 → L3上下文过滤 → L4人工兜底
"""

import time
import logging
import re
from typing import Dict, Any, Optional, Tuple, List
import yaml
import hashlib

# 尝试导入所需模块
try:
    from src.context.context_manager import ContextManager
    CONTEXT_MANAGER_AVAILABLE = True
except ImportError:
    CONTEXT_MANAGER_AVAILABLE = False

try:
    from src.ai.ai_client import AIClient
    AI_CLIENT_AVAILABLE = True
except ImportError:
    AI_CLIENT_AVAILABLE = False


class FourLayerTrigger:
    """四层触发决策器"""
    
    def __init__(self, config, context_manager=None, ai_client=None):
        """
        初始化四层触发决策器
        
        Args:
            config: 配置管理器
            context_manager: 上下文管理器实例（可选）
            ai_client: AI客户端实例（可选）
        """
        self.config = config
        self.context_manager = context_manager
        self.ai_client = ai_client
        self.logger = logging.getLogger(__name__)
        
        # 加载触发规则配置
        self.trigger_config = self._load_trigger_config()
        
        # 初始化各层处理器
        self.l1_processor = L1RuleProcessor(self.trigger_config)
        self.l3_processor = L3ContextProcessor(self.trigger_config, context_manager)
        
        # 缓存和状态
        self.decision_cache = {}
        self.user_behavior_cache = {}
        self.cooldown_tracker = {}
        
        # 统计信息
        self.stats = {
            'total_messages': 0,
            'l1_triggers': 0,
            'l2_triggers': 0,
            'l3_filtered': 0,
            'l4_silenced': 0,
            'false_positives': 0,
            'false_negatives': 0,
            'avg_processing_time': 0
        }

        # trigger_decisions 专用 RotatingFileHandler
        self._decision_logger = None
        try:
            from logging.handlers import RotatingFileHandler
            import os
            log_cfg = self.trigger_config.get('l4_human_fallback', {}).get('logging', {})
            if log_cfg.get('enabled', True):
                _log_path = log_cfg.get('file_path', 'logs/trigger_decisions.log')
                os.makedirs(os.path.dirname(_log_path), exist_ok=True)
                _dl = logging.getLogger("trigger_decisions")
                _dl.propagate = False
                if not _dl.handlers:
                    _fh = RotatingFileHandler(
                        _log_path, maxBytes=5 * 1024 * 1024, backupCount=3,
                        encoding='utf-8',
                    )
                    _fh.setFormatter(logging.Formatter("%(message)s"))
                    _dl.addHandler(_fh)
                    _dl.setLevel(logging.INFO)
                self._decision_logger = _dl
        except Exception:
            pass
        
        self.logger.info("四层触发决策器初始化完成")
    
    def _load_trigger_config(self) -> Dict[str, Any]:
        """加载触发规则配置，并自动合并 intent.keywords 到 L1 all_keywords"""
        try:
            config_file = self.config.get('trigger', {}).get('config_file', 'config/trigger_rules.yaml')
            with open(config_file, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f) or {}
        except Exception as e:
            self.logger.error(f"加载触发规则配置失败: {e}")
            return self._get_default_config()

        self._merge_intent_keywords_to_l1(cfg)
        return cfg

    def _merge_intent_keywords_to_l1(self, cfg: Dict[str, Any]) -> None:
        """将 config.yaml 中 intent.keywords 的所有关键词合并到 L1 all_keywords，
        避免后台添加了意图关键词但 L1 不触发的问题。"""
        try:
            intent_kw = self.config.get("intent", {}).get("keywords", {})
            if not isinstance(intent_kw, dict):
                return
            l1 = cfg.setdefault("l1_rule_trigger", {})
            hfk = l1.setdefault("high_frequency_keywords", {})
            existing = set(hfk.get("all_keywords") or [])
            added = 0
            for kw_list in intent_kw.values():
                if not isinstance(kw_list, list):
                    continue
                for kw in kw_list:
                    kw_s = str(kw).strip()
                    if kw_s and kw_s not in existing:
                        existing.add(kw_s)
                        added += 1
            if added:
                hfk["all_keywords"] = list(existing)
                self.logger.info("L1 自动合并 intent.keywords: 新增 %d 个关键词", added)
        except Exception as e:
            self.logger.debug("合并 intent.keywords 到 L1 异常: %s", e)
    
    def _get_default_config(self) -> Dict[str, Any]:
        """获取默认配置"""
        return {
            'l1_rule_trigger': {'enabled': True},
            'l2_semantic_trigger': {'enabled': True},
            'l3_context_filter': {'enabled': True},
            'l4_human_fallback': {'enabled': True},
            'global': {'enabled': True}
        }
    
    async def should_reply(
        self,
        message_text: str,
        chat_id: str,
        user_id: str,
        username: str,
        message_type: str = "text",
        has_image: bool = False,
        has_document: bool = False,
        bot_username: Optional[str] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        决定是否应该回复消息
        
        Args:
            message_text: 消息文本
            chat_id: 聊天ID
            user_id: 用户ID
            username: 用户名
            message_type: 消息类型 (text/voice/image)
            has_image: 是否有图片
            has_document: 是否有文档
            
        Returns:
            Tuple[是否应该回复, 决策详情]
        """
        start_time = time.time()
        self.stats['total_messages'] += 1
        
        # 📊 记录触发分析开始 - 新增
        self.logger.info(f"[触发分析] 开始分析消息: {message_text[:80]}...")
        self.logger.info(f"[触发分析] 来源: {username} @ {chat_id} (用户ID: {user_id})")
        self.logger.info(f"[触发分析] 消息类型: {message_type}, 图片: {has_image}, 文档: {has_document}")
        
        # 生成消息哈希用于缓存
        message_hash = self._hash_message(message_text, chat_id, user_id)
        
        # 检查缓存
        if message_hash in self.decision_cache:
            cached_decision = self.decision_cache[message_hash]
            processing_time = time.time() - start_time
            self._update_stats(processing_time)
            self.logger.info(f"[触发分析] 使用缓存决策: {cached_decision['should_reply']}")
            return cached_decision['should_reply'], cached_decision['details']
        
        # 决策详情记录
        decision_details = {
            'message_hash': message_hash,
            'chat_id': chat_id,
            'user_id': user_id,
            'username': username,
            'message_text_preview': message_text[:100],
            'message_type': message_type,
            'has_image': has_image,
            'has_document': has_document,
            'layers': {},
            'final_decision': False,
            'reason': '',
            'processing_time': 0
        }
        
        try:
            # L1 规则触发检查（传入 bot_username 时，@本账号 即 L1 触发）
            l1_result = self._check_l1_rule_trigger(
                message_text, has_image, has_document, decision_details, bot_username=bot_username
            )
            
            # 📊 记录L1分析结果 - 新增
            self.logger.info(f"[L1规则] 触发结果: {l1_result['triggered']}, 原因: {l1_result.get('trigger_reason', '未触发')}")
            if l1_result['triggered']:
                self.logger.info(f"[L1规则] 触发详情: {l1_result['trigger_reason']}")
            
            if l1_result['triggered']:
                # L1触发，立即返回应该回复
                decision_details['layers']['l1'] = l1_result
                decision_details['final_decision'] = True
                decision_details['reason'] = f"L1触发: {l1_result['trigger_reason']}"
                self.stats['l1_triggers'] += 1
                
                # 记录决策
                self._record_decision(message_hash, True, decision_details)
                processing_time = time.time() - start_time
                decision_details['processing_time'] = processing_time
                self._update_stats(processing_time)
                self.logger.info(f"[触发分析] 最终决策: 回复 (L1触发), 处理时间: {processing_time:.3f}s")
                return True, decision_details
            
            # L2 语义触发检查（如果L1未触发）
            l2_result = await self._check_l2_semantic_trigger(
                message_text, chat_id, user_id, decision_details
            )
            
            decision_details['layers']['l2'] = l2_result
            
            # 📊 记录L2分析结果 - 新增
            self.logger.info(f"[L2语义] 置信度: {l2_result.get('confidence', 0):.3f}, 是否继续: {l2_result.get('should_proceed', False)}")
            self.logger.info(f"[L2语义] 原因: {l2_result.get('reason', '未提供原因')}")
            
            if not l2_result['should_proceed']:
                # L2置信度不足，进入L4人工兜底
                decision_details['final_decision'] = False
                decision_details['reason'] = f"L2置信度不足: {l2_result['confidence']}"
                self.stats['l4_silenced'] += 1
                
                self._record_decision(message_hash, False, decision_details)
                processing_time = time.time() - start_time
                decision_details['processing_time'] = processing_time
                self._update_stats(processing_time)
                self.logger.info(f"[触发分析] 最终决策: 不回复 (L2置信度不足), 处理时间: {processing_time:.3f}s")
                return False, decision_details
            
            # L3 上下文过滤检查
            l3_result = self._check_l3_context_filter(
                message_text, chat_id, user_id, username, l2_result, decision_details
            )
            
            decision_details['layers']['l3'] = l3_result
            
            # 📊 记录L3分析结果 - 新增
            self.logger.info(f"[L3上下文] 是否回复: {l3_result.get('should_reply', False)}, 过滤原因: {l3_result.get('filter_reason', '无')}")
            
            if not l3_result['should_reply']:
                # L3过滤，不回复
                decision_details['final_decision'] = False
                decision_details['reason'] = f"L3过滤: {l3_result['filter_reason']}"
                self.stats['l3_filtered'] += 1
                
                self._record_decision(message_hash, False, decision_details)
                processing_time = time.time() - start_time
                decision_details['processing_time'] = processing_time
                self._update_stats(processing_time)
                self.logger.info(f"[触发分析] 最终决策: 不回复 (L3过滤), 处理时间: {processing_time:.3f}s")
                return False, decision_details
            
            # 所有检查通过，应该回复
            decision_details['final_decision'] = True
            decision_details['reason'] = "L2语义触发 + L3上下文通过"
            self.stats['l2_triggers'] += 1
            
            self._record_decision(message_hash, True, decision_details)
            processing_time = time.time() - start_time
            decision_details['processing_time'] = processing_time
            self._update_stats(processing_time)
            self.logger.info(f"[触发分析] 最终决策: 回复 (L2+L3通过), 处理时间: {processing_time:.3f}s")
            return True, decision_details
            
        except Exception as e:
            self.logger.error(f"触发决策过程中出错: {e}")
            # 出错时保守决策：不回复
            decision_details['final_decision'] = False
            decision_details['reason'] = f"决策出错: {str(e)}"
            decision_details['error'] = str(e)
            
            processing_time = time.time() - start_time
            decision_details['processing_time'] = processing_time
            self._update_stats(processing_time)
            self.logger.error(f"[触发分析] 决策异常: {e}, 处理时间: {processing_time:.3f}s")
            return False, decision_details
    
    def _check_l1_rule_trigger(
        self,
        message_text: str,
        has_image: bool,
        has_document: bool,
        decision_details: Dict[str, Any],
        bot_username: Optional[str] = None,
    ) -> Dict[str, Any]:
        """L1 规则触发检查。bot_username 不为空时，消息中 @本账号 即视为 L1 触发。"""
        result = {
            "triggered": False,
            "trigger_reason": "",
            "checks": {},
        }

        # 检查L1是否启用
        l1_config = self.trigger_config.get("l1_rule_trigger", {})
        if not l1_config.get("enabled", True):
            result["checks"]["enabled"] = False
            return result

        # 0. @本 bot 即触发（由 Telegram 层传入当前账号 username，避免仅配置客服/Camille 漏掉 @wookfaith 等）
        if bot_username:
            mention = ("@" + bot_username.strip().lstrip("@")).lower()
            if mention in (message_text or "").lower():
                result["triggered"] = True
                result["trigger_reason"] = "@本账号"
                result["checks"]["mention_bot"] = True
                return result

        # 0b. 单独「在」「在。」等（问客服在不在），与 greeting_lexicon.is_standalone_zai_query 一致。
        # 不可把裸「在」放进高频关键词列表：子串匹配会误伤「现在」「正在」等。
        try:
            from src.utils.greeting_lexicon import is_standalone_zai_query

            if is_standalone_zai_query(message_text or ""):
                result["triggered"] = True
                result["trigger_reason"] = "单字在(问客服是否在线)"
                result["checks"]["standalone_zai"] = True
                return result
        except Exception:
            pass

        # 1. 图片+文字触发
        if has_image or has_document:
            image_config = l1_config.get('image_with_text', {})
            if image_config.get('enabled', True):
                # 只要有图片/文档，并且有文字（哪怕只有1个字符）
                min_length = image_config.get('min_text_length', 1)
                if len(message_text.strip()) >= min_length:
                    result['triggered'] = True
                    result['trigger_reason'] = "图片/文档 + 文字"
                    result['checks']['image_with_text'] = True
                    return result
                else:
                    result['checks']['image_with_text'] = False
        
        # 2. 高频关键词触发（匹配前规范化：全角→半角、去空格、统一小写，避免「回　调」「Order  Inquiry」漏触发）
        keywords_config = l1_config.get('high_frequency_keywords', {})
        if keywords_config.get('enabled', True):
            keywords = keywords_config.get('all_keywords', [])
            case_sensitive = keywords_config.get('case_sensitive', False)
            raw = (message_text or "").replace("\u3000", " ")  # 全角空格→半角
            normalized = re.sub(r"\s+", "", raw)
            text_to_check = (normalized if case_sensitive else normalized.lower())
            for keyword in keywords:
                keyword_check = keyword if case_sensitive else keyword.lower()
                if keyword_check in text_to_check:
                    result['triggered'] = True
                    result['trigger_reason'] = f"高频关键词: {keyword}"
                    result['checks']['high_frequency_keyword'] = True
                    result['checks']['matched_keyword'] = keyword
                    return result
            
            result['checks']['high_frequency_keyword'] = False
        
        # 3. 订单号格式识别
        order_config = l1_config.get('order_number_patterns', {})
        if order_config.get('enabled', True):
            patterns = order_config.get('patterns', [])

            raw_text = (message_text or "").strip()
            stripped_text = re.sub(r'^[\u4e00-\u9fff]{1,2}', '', raw_text)

            for text_variant in (raw_text, stripped_text):
                if not text_variant:
                    continue
                for pattern in patterns:
                    if re.search(pattern, text_variant, re.IGNORECASE):
                        result['triggered'] = True
                        result['trigger_reason'] = f"订单号格式匹配: {pattern}"
                        result['checks']['order_number_pattern'] = True
                        result['checks']['matched_pattern'] = pattern
                        return result

            result['checks']['order_number_pattern'] = False
        
        # 4. @提及触发
        mention_config = l1_config.get('mention_trigger', {})
        if mention_config.get('enabled', True):
            usernames = mention_config.get('usernames', [])
            
            for username in usernames:
                clean_username = username.lstrip('@')
                if clean_username.lower() in message_text.lower():
                    result['triggered'] = True
                    result['trigger_reason'] = f"@提及: {username}"
                    result['checks']['mention_trigger'] = True
                    result['checks']['matched_username'] = username
                    return result
            
            result['checks']['mention_trigger'] = False
        
        return result
    
    async def _check_l2_semantic_trigger(
        self,
        message_text: str,
        chat_id: str,
        user_id: str,
        decision_details: Dict[str, Any]
    ) -> Dict[str, Any]:
        """L2 语义触发检查"""
        result = {
            'should_proceed': False,
            'confidence': 0.0,
            'reason': '',
            'ai_analysis': {}
        }
        
        # 检查L2是否启用
        l2_config = self.trigger_config.get('l2_semantic_trigger', {})
        if not l2_config.get('enabled', True):
            result['reason'] = "L2未启用"
            return result
        
        try:
            confidence_thresholds = l2_config.get('confidence_thresholds', {})
            reply_threshold = confidence_thresholds.get('reply_threshold', 0.75)
            # 有 AI 客户端时可用其做语义置信度；无则用规则置信度（便于 L2 兜底无依赖运行）
            confidence = await self._get_ai_confidence(message_text, chat_id, user_id)
            
            result['confidence'] = confidence
            result['ai_analysis']['raw_confidence'] = confidence
            
            if confidence >= reply_threshold:
                result['should_proceed'] = True
                result['reason'] = f"置信度达标: {confidence:.3f} ≥ {reply_threshold}"
            else:
                result['reason'] = f"置信度不足: {confidence:.3f} < {reply_threshold}"
            
            return result
            
        except Exception as e:
            self.logger.error(f"L2语义分析失败: {e}")
            result['reason'] = f"分析失败: {str(e)}"
            result['ai_analysis']['error'] = str(e)
            return result
    
    async def check_l2_only(self, message_text: str, chat_id: str, user_id: str) -> Tuple[bool, str]:
        """
        仅跑 L2 语义判定（用于会话窗口内 L2 兜底）。不跑 L1/L3。
        Returns:
            (should_proceed, reason)
        """
        decision_details: Dict[str, Any] = {}
        l2_result = await self._check_l2_semantic_trigger(
            message_text, chat_id, str(user_id), decision_details
        )
        return (l2_result.get('should_proceed', False), l2_result.get('reason', ''))
    
    async def _get_ai_confidence(self, message_text: str, chat_id: str, user_id: str) -> float:
        """获取AI置信度分数：有 ai_client 时调用 AI 判断，否则回退到规则"""
        if self.ai_client and AI_CLIENT_AVAILABLE:
            try:
                import asyncio
                l2_cfg = self.trigger_config.get('l2_semantic_trigger', {}).get('ai_model', {})
                timeout = l2_cfg.get('timeout', 10)
                prompt = (
                    "你是一个客服系统的触发判定器。判断以下消息是否为业务咨询（需要客服回复）。\n"
                    "业务咨询包括：订单查询、支付问题、通道状态、额度查询、成功率、代收代付等。\n"
                    "非业务消息包括：纯闲聊、表情、无意义文字、用户间私聊。\n"
                    f"消息内容：「{message_text[:200]}」\n"
                    "只回复一个 0 到 1 之间的数字表示业务相关的置信度，不要说其他任何话。"
                )
                resp = await asyncio.wait_for(
                    self.ai_client.chat(prompt, strategy_overrides={"temperature": 0.1, "max_tokens": 16}),
                    timeout=timeout,
                )
                import re as _re
                m = _re.search(r"(0?\.\d+|1\.0|[01])", (resp or "").strip())
                if m:
                    score = float(m.group(1))
                    if 0.0 <= score <= 1.0:
                        return score
            except Exception as e:
                self.logger.debug("L2 AI 置信度调用失败，回退规则: %s", e)
        text_lower = (message_text or "").lower()
        text_stripped = text_lower.strip()
        text_normalized = re.sub(r"\s+", "", text_lower)  # 去空格便于短语匹配

        # 纯数字 1~5（客服让选 1/2/3/4 或 1~5 时用户回复），一律高置信度，避免仅「4」触发而 123 不触发
        if re.match(r"^[1-5]\s*$", text_stripped) or text_stripped in ("1", "2", "3", "4", "5"):
            return 0.95

        # 代查/问能力短语（L1 未命中时的语义兜底，先匹配长短语）
        query_phrases = [
            "你能做什么", "能做什么", "可以做什么", "有什么功能", "服务什么", "有什么服务",
            "什么时候回调", "调出来", "回调出来", "多久回调",
            "查单", "查订单", "查看订单", "帮我查", "查一下", "查到了吗", "查新订单",
            "orderinquiry", "order inquiry",
        ]
        for phrase in query_phrases:
            phrase_norm = re.sub(r"\s+", "", phrase.lower())
            if phrase_norm in text_normalized or phrase in text_lower:
                return 0.95

        # 检查是否包含业务关键词（中英及多语言查单/订单/通道相关，便于 L2 兜底）
        business_keywords = [
            "订单", "支付", "问题", "帮助", "客服", "怎么", "为什么", "怎么办",
            "查", "回调", "到账", "没到账", "通道", "状态", "交易", "跑单", "波动", "稳定",
            "order", "inquiry", "payment", "channel",
            "status", "check", "pedido", "consulta", "sipariş", "sorgu", "commande", "statut",
            # gxp 斜杠命令（/cxye、/cxds 等），L1 未命中时 L2 兜底
            "/qhyy", "/tjcy", "/mnds", "/cxds", "/htds", "/cxdf", "/htdf", "/utr", "/hl", "/cxye", "/cgl",
        ]
        for keyword in business_keywords:
            if keyword in text_lower:
                return 0.95  # 高置信度

        # 身份/自我介绍类问题 — 直接问客服是谁，必须回复
        identity_phrases = [
            "你是谁", "你叫什么", "哪个客服", "什么客服", "谁在",
            "你是机器人", "你是ai", "你是人吗", "介绍一下你", "介绍下自己",
        ]
        for phrase in identity_phrases:
            if phrase in text_lower:
                return 0.95

        # 问句标志（？、吗、呢、谁、什么、怎么…）→ 用户在提问，高置信度通过
        _q_markers = ("？", "?", "吗", "呢", "谁", "什么", "怎么", "为什么", "哪", "多少", "几")
        if any(m in text_lower for m in _q_markers):
            return 0.90

        # 检查是否是简单问候
        greetings = ["你好", "您好", "hello", "hi", "在吗", "在？"]
        for greeting in greetings:
            if greeting in text_lower:
                return 0.80

        # 确认/应答类（客户收到回复后的确认，在会话中是合理回复）
        _ack_phrases = (
            "好的", "好", "ok", "嗯", "嗯嗯", "收到", "明白", "了解",
            "感谢", "谢谢", "谢了", "没问题", "是的", "对", "行",
            "可以了", "知道了", "懂了", "okey", "okay", "好嘞", "得嘞",
            "那行", "没事了", "解决了", "搞定了", "可以", "正常了",
        )
        if text_stripped in _ack_phrases or text_normalized in _ack_phrases:
            return 0.78

        # 意图预告（客户预告下一步行动，值得回应）
        _intent_signals = (
            "测试", "试试", "试一下", "等一下", "稍后", "晚点",
            "一会", "马上", "待会", "准备", "先这样", "我去",
        )
        if any(s in text_lower for s in _intent_signals):
            return 0.78

        # 带语气词的短文本（大概率是对话中的回应，非垃圾消息）
        _tone_particles = ("呀", "啦", "呢", "哟", "噢", "哦", "嘛", "吧", "呗", "哈")
        if len(text_stripped) <= 15 and any(p in text_lower for p in _tone_particles):
            return 0.76

        # 默认中等置信度（真正无法识别的消息）
        return 0.70
    
    def _check_l3_context_filter(
        self,
        message_text: str,
        chat_id: str,
        user_id: str,
        username: str,
        l2_result: Dict[str, Any],
        decision_details: Dict[str, Any]
    ) -> Dict[str, Any]:
        """L3 上下文过滤检查"""
        result = {
            'should_reply': True,
            'filter_reason': '',
            'checks': {}
        }
        
        # 检查L3是否启用
        l3_config = self.trigger_config.get('l3_context_filter', {})
        if not l3_config.get('enabled', True):
            result['checks']['enabled'] = False
            return result
        
        # 1. 冷却检查
        cooldown_config = l3_config.get('cooldown', {})
        if cooldown_config.get('enabled', True):
            cooldown_time = cooldown_config.get('default_cooldown', 90)
            
            # 检查用户冷却
            user_key = f"{chat_id}_{user_id}"
            current_time = time.time()
            
            if user_key in self.cooldown_tracker:
                last_reply_time = self.cooldown_tracker[user_key]
                if current_time - last_reply_time < cooldown_time:
                    result['should_reply'] = False
                    result['filter_reason'] = f"用户冷却中 ({cooldown_time}秒)"
                    result['checks']['cooldown'] = False
                    result['checks']['cooldown_remaining'] = cooldown_time - (current_time - last_reply_time)
                    return result
            
            result['checks']['cooldown'] = True
        
        # 2. 闲聊检测（如果有上下文管理器）
        if self.context_manager and CONTEXT_MANAGER_AVAILABLE:
            small_talk_config = l3_config.get('small_talk_detection', {})
            if small_talk_config.get('enabled', True):
                # 检查消息是否包含闲聊关键词
                small_talk_keywords = small_talk_config.get('small_talk_keywords', [])
                text_lower = message_text.lower()
                
                is_small_talk = False
                for keyword in small_talk_keywords:
                    if keyword in text_lower:
                        # 检查是否也包含业务关键词
                        business_keywords = small_talk_config.get('business_keywords', [])
                        has_business_keyword = any(bk in text_lower for bk in business_keywords)
                        
                        if not has_business_keyword:
                            is_small_talk = True
                            break
                
                if is_small_talk:
                    look_back = small_talk_config.get('look_back_messages', 10)
                    max_sequence = small_talk_config.get('max_small_talk_sequence', 3)

                    consecutive_small_talk = 1
                    try:
                        recent = self.context_manager.get_recent_messages(chat_id, limit=look_back) if self.context_manager else []
                        for prev_msg in reversed(recent):
                            prev_text = (prev_msg.get('text') or '').lower()
                            if any(kw in prev_text for kw in small_talk_keywords):
                                business_keywords = small_talk_config.get('business_keywords', [])
                                if not any(bk in prev_text for bk in business_keywords):
                                    consecutive_small_talk += 1
                                else:
                                    break
                            else:
                                break
                    except Exception:
                        pass

                    if consecutive_small_talk >= max_sequence:
                        result['should_reply'] = False
                        result['filter_reason'] = f"连续闲聊 ({consecutive_small_talk} 条)"
                        result['checks']['small_talk'] = False
                        return result
                    result['should_reply'] = False
                    result['filter_reason'] = "闲聊消息"
                    result['checks']['small_talk'] = False
                    return result
                
                result['checks']['small_talk'] = True
        
        # 3. 多人聊天判断
        multi_user_config = l3_config.get('multi_user_filter', {})
        if multi_user_config.get('enabled', True):
            min_users = multi_user_config.get('min_users_to_skip', 3)
            check_window = multi_user_config.get('check_window', 10)

            distinct_users = set()
            try:
                if self.context_manager:
                    recent = self.context_manager.get_recent_messages(chat_id, limit=check_window)
                    for msg in recent:
                        uid = msg.get('user_id') or msg.get('from_id')
                        if uid:
                            distinct_users.add(str(uid))
            except Exception:
                pass

            if len(distinct_users) >= min_users:
                text_lower = message_text.lower()
                exceptions = multi_user_config.get('exceptions', [])
                has_exception = False
                l1_keywords = self.trigger_config.get('l1_rule_trigger', {}).get('high_frequency_keywords', {}).get('all_keywords', [])
                mention_names = self.trigger_config.get('l1_rule_trigger', {}).get('mention_trigger', {}).get('usernames', [])

                if any(mn.lower() in text_lower for mn in mention_names):
                    has_exception = True
                elif any(kw.lower() in text_lower for kw in l1_keywords[:30]):
                    has_exception = True

                if not has_exception:
                    result['should_reply'] = False
                    result['filter_reason'] = f"多人聊天 ({len(distinct_users)}人)"
                    result['checks']['multi_user'] = False
                    return result

            result['checks']['multi_user'] = True
        
        # 所有检查通过
        result['checks']['all_passed'] = True
        return result
    
    def update_cooldown(self, chat_id: str, user_id: str):
        """更新用户冷却时间"""
        user_key = f"{chat_id}_{user_id}"
        self.cooldown_tracker[user_key] = time.time()
    
    def _hash_message(self, message_text: str, chat_id: str, user_id: str) -> str:
        """生成消息哈希"""
        content = f"{chat_id}_{user_id}_{message_text}"
        return hashlib.md5(content.encode()).hexdigest()[:12]
    
    def _record_decision(self, message_hash: str, should_reply: bool, details: Dict[str, Any]):
        """记录决策结果并更新全局指标"""
        try:
            from src.monitoring.metrics_store import get_metrics_store
            store = get_metrics_store()
            layer = details.get('triggered_layer', '')
            if not layer:
                reason = details.get('reason', '')
                if 'L1' in reason:
                    layer = 'l1'
                elif 'L2' in reason and should_reply:
                    layer = 'l2'
                elif 'L3' in reason:
                    layer = 'l3_filtered'
                elif 'L4' in reason or 'L2置信度不足' in reason:
                    layer = 'l4_silenced'
                else:
                    layer = 'l2' if should_reply else 'skipped'
            store.record_trigger_layer(layer)
        except Exception:
            pass

        self.decision_cache[message_hash] = {
            'should_reply': should_reply,
            'details': details,
            'timestamp': time.time()
        }
        
        # 限制缓存大小
        if len(self.decision_cache) > 1000:
            # 移除最旧的条目
            oldest_key = min(self.decision_cache.keys(), 
                           key=lambda k: self.decision_cache[k]['timestamp'])
            del self.decision_cache[oldest_key]
        
        try:
            if self._decision_logger:
                import json, datetime
                entry = {
                    'ts': datetime.datetime.now().isoformat(),
                    'hash': message_hash,
                    'reply': should_reply,
                    'reason': details.get('final_reason', ''),
                    'layer': details.get('triggered_layer', ''),
                }
                self._decision_logger.info(json.dumps(entry, ensure_ascii=False))
        except Exception:
            pass
    
    def _update_stats(self, processing_time: float):
        """更新统计信息"""
        total_messages = self.stats['total_messages']
        current_avg = self.stats['avg_processing_time']
        
        # 更新平均处理时间
        if total_messages == 1:
            self.stats['avg_processing_time'] = processing_time
        else:
            self.stats['avg_processing_time'] = (
                (current_avg * (total_messages - 1) + processing_time) / total_messages
            )
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = self.stats.copy()
        
        # 计算触发率
        if stats['total_messages'] > 0:
            stats['l1_trigger_rate'] = stats['l1_triggers'] / stats['total_messages']
            stats['l2_trigger_rate'] = stats['l2_triggers'] / stats['total_messages']
            stats['l3_filter_rate'] = stats['l3_filtered'] / stats['total_messages']
            stats['l4_silence_rate'] = stats['l4_silenced'] / stats['total_messages']
        
        return stats
    
    def reset_stats(self):
        """重置统计信息"""
        self.stats = {
            'total_messages': 0,
            'l1_triggers': 0,
            'l2_triggers': 0,
            'l3_filtered': 0,
            'l4_silenced': 0,
            'false_positives': 0,
            'false_negatives': 0,
            'avg_processing_time': 0
        }


class L1RuleProcessor:
    """L1 规则处理器"""
    
    def __init__(self, trigger_config: Dict[str, Any]):
        self.config = trigger_config.get('l1_rule_trigger', {})
        self.logger = logging.getLogger(__name__)
    
    def check_image_with_text(self, message_text: str, has_image: bool, has_document: bool) -> bool:
        """检查图片+文字触发"""
        if not (has_image or has_document):
            return False
        
        image_config = self.config.get('image_with_text', {})
        if not image_config.get('enabled', True):
            return False
        
        min_length = image_config.get('min_text_length', 1)
        return len(message_text.strip()) >= min_length
    
    def check_high_frequency_keywords(self, message_text: str) -> Tuple[bool, Optional[str]]:
        """检查高频关键词触发"""
        keywords_config = self.config.get('high_frequency_keywords', {})
        if not keywords_config.get('enabled', True):
            return False, None
        
        keywords = keywords_config.get('all_keywords', [])
        case_sensitive = keywords_config.get('case_sensitive', False)
        
        text_to_check = message_text if case_sensitive else message_text.lower()
        
        for keyword in keywords:
            keyword_check = keyword if case_sensitive else keyword.lower()
            if keyword_check in text_to_check:
                return True, keyword
        
        return False, None
    
    def check_order_number_patterns(self, message_text: str) -> Tuple[bool, Optional[str]]:
        """检查订单号格式"""
        order_config = self.config.get('order_number_patterns', {})
        if not order_config.get('enabled', True):
            return False, None

        patterns = order_config.get('patterns', [])
        raw = (message_text or "").strip()
        stripped = re.sub(r'^[\u4e00-\u9fff]{1,2}', '', raw)

        for text_variant in (raw, stripped):
            if not text_variant:
                continue
            for pattern in patterns:
                if re.search(pattern, text_variant, re.IGNORECASE):
                    return True, pattern

        return False, None
    
    def check_mention_trigger(self, message_text: str) -> Tuple[bool, Optional[str]]:
        """检查@提及触发"""
        mention_config = self.config.get('mention_trigger', {})
        if not mention_config.get('enabled', True):
            return False, None
        
        usernames = mention_config.get('usernames', [])
        
        for username in usernames:
            clean_username = username.lstrip('@')
            if clean_username.lower() in message_text.lower():
                return True, username
        
        return False, None


class L3ContextProcessor:
    """L3 上下文处理器"""
    
    def __init__(self, trigger_config: Dict[str, Any], context_manager=None):
        self.config = trigger_config.get('l3_context_filter', {})
        self.context_manager = context_manager
        self.logger = logging.getLogger(__name__)
    
    def check_cooldown(self, chat_id: str, user_id: str, cooldown_tracker: Dict[str, float]) -> Tuple[bool, float]:
        """检查冷却时间"""
        cooldown_config = self.config.get('cooldown', {})
        if not cooldown_config.get('enabled', True):
            return True, 0  # 不启用冷却，总是通过
        
        cooldown_time = cooldown_config.get('default_cooldown', 90)
        user_key = f"{chat_id}_{user_id}"
        
        if user_key in cooldown_tracker:
            current_time = time.time()
            last_reply_time = cooldown_tracker[user_key]
            remaining = cooldown_time - (current_time - last_reply_time)
            
            if remaining > 0:
                return False, remaining  # 仍在冷却中
        
        return True, 0  # 冷却通过
    
    def check_small_talk(self, message_text: str) -> bool:
        """检查是否是闲聊"""
        small_talk_config = self.config.get('small_talk_detection', {})
        if not small_talk_config.get('enabled', True):
            return False  # 不启用闲聊检测，不认为是闲聊
        
        small_talk_keywords = small_talk_config.get('small_talk_keywords', [])
        business_keywords = small_talk_config.get('business_keywords', [])
        
        text_lower = message_text.lower()
        
        # 检查是否包含闲聊关键词
        has_small_talk = False
        for keyword in small_talk_keywords:
            if keyword in text_lower:
                has_small_talk = True
                break
        
        if not has_small_talk:
            return False  # 不包含闲聊关键词
        
        # 如果包含闲聊关键词，检查是否也包含业务关键词
        has_business = False
        for keyword in business_keywords:
            if keyword in text_lower:
                has_business = True
                break
        
        # 如果包含业务关键词，不认为是纯闲聊
        return not has_business
    
    def get_smart_cooldown(self, chat_id: str, user_id: str) -> int:
        """基于上下文智能调整冷却时间"""
        l3_config = self.trigger_config.get('l3_context_filter', {})
        cooldown_config = l3_config.get('cooldown', {})
        smart_config = cooldown_config.get('smart_cooldown', {})
        default_cd = cooldown_config.get('default_cooldown', 90)

        if not smart_config.get('enabled', True):
            return default_cd

        business_cd = smart_config.get('business_conversation_cooldown', 30)
        small_talk_cd = smart_config.get('small_talk_cooldown', 120)

        if not self.context_manager:
            return default_cd

        try:
            recent = self.context_manager.get_recent_messages(chat_id, limit=5)
            biz_keywords = l3_config.get('small_talk_detection', {}).get('business_keywords', [])
            talk_keywords = l3_config.get('small_talk_detection', {}).get('small_talk_keywords', [])

            biz_count = 0
            talk_count = 0
            for msg in recent:
                txt = (msg.get('text') or '').lower()
                if any(k in txt for k in biz_keywords):
                    biz_count += 1
                if any(k in txt for k in talk_keywords):
                    talk_count += 1

            if biz_count > talk_count:
                return business_cd
            elif talk_count > biz_count:
                return small_talk_cd
        except Exception:
            pass

        return default_cd