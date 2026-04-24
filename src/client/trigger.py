"""
触发决策 Mixin：判断是否回复群组消息
包含回复链、追问上下文、会话窗口、AI上下文、L2兜底、四层/旧版触发
"""

import asyncio
import re
import time
from typing import Optional, Tuple, Dict, Any


class TelegramTriggerMixin:

    def _should_reply_by_reply_chain(self, message) -> bool:
        reply_logic = self.config.get('telegram', {}).get('reply_logic', {})
        reply_chain = reply_logic.get('reply_chain', {})
        if not reply_chain.get('enabled', True):
            return False
        if not reply_chain.get('reply_to_me_always_reply', True):
            return False
        reply_to = getattr(message, 'reply_to_message', None)
        if not reply_to:
            return False
        from_user = getattr(reply_to, 'from_user', None)
        if not from_user:
            return False
        my_id = getattr(self.user_info, 'id', None) if self.user_info else None
        if my_id is None:
            return False
        if getattr(from_user, 'id', None) != my_id:
            return False
        skip_short = reply_chain.get('skip_if_only_emoji_or_short', False)
        if skip_short:
            text = (message.text or message.caption or "").strip()
            if len(text) <= 2 and not any(c in text for c in "?？吗呢"):
                return False
        self.logger.info("[回复链] 用户回复了我们的消息，判定应回复")
        return True

    async def _should_reply_by_follow_up_context(self, message, current_text: str) -> bool:
        reply_logic = self.config.get('telegram', {}).get('reply_logic', {})
        follow_up = reply_logic.get('follow_up', {})
        if not follow_up.get('enabled', True):
            return False
        if not self.client or not self.user_info:
            return False
        lookback = max(5, min(20, follow_up.get('lookback_count', 10)))
        max_len = follow_up.get('max_text_length', 200)
        try:
            chat_id = message.chat.id
            my_id = getattr(self.user_info, 'id', None)
            if my_id is None:
                return False
            t = (current_text or "").strip()
            if not t or len(t) > max_len:
                return False
            found_our_message = False
            count = 0
            async for msg in self.client.get_chat_history(chat_id, limit=lookback):
                count += 1
                if count == 1:
                    continue
                from_user = getattr(msg, 'from_user', None)
                if not from_user:
                    continue
                if getattr(from_user, 'id', None) == my_id:
                    found_our_message = True
                    break
            if not found_our_message:
                return False
            if t in ("1", "2", "3", "4", "5") or (len(t) <= 3 and t.rstrip(".。,，、").strip() in ("1", "2", "3", "4", "5")):
                self.logger.info("[追问上下文] 最近 %d 条内曾出现我们且当前为选项数字 1~5，应回复", lookback)
                return True
            question_marks = "?？吗呢啊呀"
            follow_up_keywords = (
                "什么时候", "多久", "怎么", "怎样", "如何", "为什么", "能否", "可以吗",
                "when", "how", "what", "why", "order", "inquiry", "status", "check",
                "回调", "可用", "到账", "出来", "好了吗", "查到了吗",
                "pedido", "consulta", "pagamento", "sipariş", "sorgu", "durum",
                "طلب", "استفسار", "حالة", "commande", "statut", "bestellung", "anfrage",
                "ordine", "richiesta", "заказ", "запрос", "статус", "pago", "estado",
            )
            t_lower = t.lower()
            if any(c in question_marks for c in t) or any(k in t_lower for k in follow_up_keywords):
                self.logger.info("[追问上下文] 最近 %d 条内曾出现我们且当前像追问，应回复", lookback)
                return True
            return False
        except Exception as e:
            self.logger.debug("追问上下文检查异常: %s", e)
            return False

    def _record_session_reply(self, chat_id: int, user_id: int) -> None:
        reply_logic = self.config.get('telegram', {}).get('reply_logic', {})
        if not reply_logic.get('session_window', {}).get('enabled', True):
            return
        key = f"{chat_id}:{user_id}"
        self._session_reply_ts[key] = time.time()
        window_min = reply_logic.get('session_window', {}).get('reply_within_minutes', 45)
        expire = time.time() - (window_min * 2 * 60)
        to_del = [k for k, ts in self._session_reply_ts.items() if ts < expire]
        for k in to_del:
            del self._session_reply_ts[k]

    def _is_in_session_window(self, chat_id: int, user_id: int) -> bool:
        reply_logic = self.config.get('telegram', {}).get('reply_logic', {})
        sw = reply_logic.get('session_window', {})
        if not sw.get('enabled', True):
            return False
        window_min = sw.get('reply_within_minutes', 45)
        key = f"{chat_id}:{user_id}"
        ts = self._session_reply_ts.get(key)
        if ts is None:
            return False
        if time.time() - ts > window_min * 60:
            del self._session_reply_ts[key]
            return False
        return True

    async def _get_previous_message(self, chat_id: int) -> Optional[Tuple[str, str]]:
        if not self.client:
            return None
        try:
            count = 0
            async for msg in self.client.get_chat_history(chat_id, limit=3):
                count += 1
                if count == 2:
                    prev_text = (msg.text or msg.caption or "").strip() or "[无文字]"
                    prev_date = getattr(msg, "date", None)
                    if prev_date:
                        try:
                            ts = prev_date.strftime("%Y-%m-%d %H:%M")
                        except Exception:
                            ts = str(prev_date)
                    else:
                        ts = ""
                    return (ts, prev_text[:800])
            return None
        except Exception as e:
            self.logger.debug("获取前一条消息异常: %s", e)
            return None

    async def _should_reply_by_ai_context(self, message, text: str) -> bool:
        reply_logic = self.config.get('telegram', {}).get('reply_logic', {})
        cfg = reply_logic.get('ai_context_reply', {})
        if not cfg.get('enabled', True):
            return False
        if not self.ai_client or not text or len(text) > 600:
            return False
        prev = await self._get_previous_message(message.chat.id)
        if not prev:
            return False
        prev_time, prev_text = prev
        timeout = float(cfg.get('timeout_seconds', 8))
        try:
            should, reason = await asyncio.wait_for(
                self.ai_client.should_reply_by_context(prev_text, prev_time, text),
                timeout=timeout,
            )
            if should:
                self.logger.info("[AI上下文] 判定与工作相关应回复: %s", reason[:80] if reason else "")
                return True
            return False
        except asyncio.TimeoutError:
            self.logger.debug("AI上下文判断超时")
            return False
        except Exception as e:
            self.logger.debug("AI上下文判断异常: %s", e)
            return False

    async def _should_reply_by_l2_fallback(self, message, text: str) -> bool:
        reply_logic = self.config.get('telegram', {}).get('reply_logic', {})
        l2_cfg = reply_logic.get('l2_fallback', {})
        if not l2_cfg.get('enabled', True):
            return False
        if not self.four_layer_trigger:
            return False
        if not message.from_user:
            return False
        chat_id = message.chat.id
        user_id = message.from_user.id
        if l2_cfg.get('only_in_session_window', True) and not self._is_in_session_window(chat_id, user_id):
            return False
        min_len = l2_cfg.get('text_min_len', 2)
        max_len = l2_cfg.get('text_max_len', 300)
        if len(text) < min_len or len(text) > max_len:
            return False
        timeout = l2_cfg.get('timeout_seconds', 6)
        try:
            chat_id_str = f"group_{chat_id}"
            should, reason = await asyncio.wait_for(
                self.four_layer_trigger.check_l2_only(text, chat_id_str, user_id),
                timeout=float(timeout)
            )
            if should:
                self.logger.info("[L2兜底] 会话窗口内 L2 判定应回复: %s", reason[:80] if reason else "")
                return True
            return False
        except asyncio.TimeoutError:
            self.logger.debug("L2兜底超时，不回复")
            return False
        except Exception as e:
            self.logger.debug("L2兜底异常: %s", e)
            return False

    def _contains_mention_of_self(self, message) -> bool:
        if not self.user_info:
            return False
        text = (message.text or message.caption or "")
        if not text:
            return False
        my_username = getattr(self.user_info, 'username', None)
        if not my_username:
            return False
        return my_username.lower() in text.lower() or f"@{my_username}".lower() in text.lower()

    async def _should_reply_to_group_message(self, message) -> bool:
        if self.user_info and self._should_reply_by_reply_chain(message):
            message._trigger_path = "reply_chain"
            return True
        if self._contains_mention_of_self(message):
            self.logger.info("[@本账号] 消息中 @ 了当前登录账号，判定应回复")
            message._trigger_path = "mention"
            return True
        text = (message.text or message.caption or "").strip()
        trigger_config = self.config.get('trigger', {})
        if trigger_config.get('enabled', False) and self.four_layer_trigger:
            if await self._should_reply_with_four_layer_trigger(message):
                return True
            if text and await self._should_reply_by_follow_up_context(message, text):
                message._trigger_path = "follow_up"
                return True
            if text and await self._should_reply_by_l2_fallback(message, text):
                message._trigger_path = "l2_fallback"
                return True
            if text and await self._should_reply_by_ai_context(message, text):
                message._trigger_path = "ai_context"
                return True
            return False
        if self._should_reply_with_legacy_method(message):
            message._trigger_path = "legacy"
            return True
        if text and await self._should_reply_by_follow_up_context(message, text):
            message._trigger_path = "follow_up"
            return True
        if text and await self._should_reply_by_l2_fallback(message, text):
            message._trigger_path = "l2_fallback"
            return True
        if text and await self._should_reply_by_ai_context(message, text):
            message._trigger_path = "ai_context"
            return True
        return False

    async def _should_reply_with_four_layer_trigger(self, message) -> bool:
        try:
            text = message.text or message.caption or ""
            has_image = bool(message.photo or (message.document and
                         message.document.mime_type and
                         message.document.mime_type.startswith('image/')))
            has_document = bool(message.document)
            if not text and (message.voice or message.audio or has_image):
                return False
            user_id = message.from_user.id if message.from_user else 0
            username = message.from_user.username if message.from_user else "unknown"
            chat_id = f"group_{message.chat.id}" if message.chat.id else "unknown"
            my_username = getattr(self.user_info, "username", None) if self.user_info else None
            should_reply, decision_details = await self.four_layer_trigger.should_reply(
                message_text=text,
                chat_id=chat_id,
                user_id=str(user_id),
                username=username,
                message_type="text",
                has_image=has_image,
                has_document=has_document,
                bot_username=my_username,
            )
            if decision_details.get('final_decision', False):
                reason = decision_details.get('reason', '未知原因')
                if 'L1' in reason:
                    message._trigger_path = "l1_rule"
                elif 'L2' in reason:
                    message._trigger_path = "l2_semantic"
                else:
                    message._trigger_path = "four_layer"
                self.logger.info(
                    "四层触发决策: 回复 - %s, 置信度: %.3f",
                    reason,
                    decision_details.get('layers', {}).get('l2', {}).get('confidence', 0),
                )
            else:
                reason = decision_details.get('reason', '未知原因')
                self.logger.info(
                    "四层触发决策: 不回复 - %s (若需回复可: 回复我们的消息 / @本账号 / 关键词或图片+文字 / 追问或会话窗口内L2)",
                    reason,
                )
            return should_reply
        except Exception as e:
            self.logger.error("四层触发决策失败: %s", e)
            return False

    def _should_reply_with_legacy_method(self, message) -> bool:
        group_config = self.config.get('telegram', {}).get('group_reply', {})
        mode = group_config.get('mode', 'always')
        if mode == 'always':
            return True
        text = message.text or message.caption or ""
        if not text:
            return False
        if mode == 'mention_only':
            return self._contains_mention(text, group_config)
        if mode == 'keyword_only':
            return self._contains_keyword(text, group_config)
        if mode == 'mention_or_keyword':
            return (self._contains_mention(text, group_config) or
                    self._contains_keyword(text, group_config))
        return True

    def _contains_mention(self, text: str, group_config: dict) -> bool:
        usernames = group_config.get('mention_usernames', [])
        for username in usernames:
            clean_username = username.lstrip('@')
            if clean_username.lower() in text.lower():
                return True
            if username.lower() in text.lower():
                return True
        return False

    def _contains_keyword(self, text: str, group_config: dict) -> bool:
        keywords = group_config.get('keywords', [])
        case_sensitive = group_config.get('case_sensitive', False)
        require_exact = group_config.get('require_exact_match', False)
        if not case_sensitive:
            text = text.lower()
        for keyword in keywords:
            keyword_check = keyword if case_sensitive else keyword.lower()
            if require_exact:
                if text == keyword_check:
                    return True
            else:
                if keyword_check in text:
                    return True
        return False
