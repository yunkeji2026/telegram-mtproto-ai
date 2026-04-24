"""GXP 命令代发技能 — 代用户向群内发送 gxp_notify_bot 命令"""

import re
import time
import random
from typing import Dict, Any, Optional, List

from src.skills.base import Skill


class GxpCommandSkill(Skill):
    """
    代用户向群内发送 gxp_notify_bot 命令。支持：仅发单号时先问需求再代发；先发单号、下条说需求时用缓存单号。
    """
    _INTENT_CXDS = re.compile(r"^1$|查(询)?代收|代收(订单)?查询|查询代收订单", re.IGNORECASE)
    _INTENT_HTDS = re.compile(r"^2$|回调(代收|交易)|代收回调|回调交易订单|htds", re.IGNORECASE)
    _INTENT_CXDF = re.compile(r"^3$|查(询)?提现|提现(订单)?查询|查询提现订单", re.IGNORECASE)
    _INTENT_HTDF = re.compile(r"^4$|回调提现|提现回调|htdf", re.IGNORECASE)

    def __init__(self, config, ai_client):
        super().__init__(config, ai_client)
        self.priority = 1

    @staticmethod
    def _is_bare_order_no_skill(text: str) -> tuple:
        """与 SkillManager._is_bare_order_no 一致：仅单号且无需求词时返回 (True, order_no)。"""
        raw = (text or "").strip()
        if not raw:
            return False, None
        intent_words = re.compile(
            r"查(询)?代收|代收(订单)?查询|回调(代收|交易)|代收回调|查(询)?提现|提现(订单)?查询|回调提现|查询|回调",
            re.IGNORECASE
        )
        if intent_words.search(raw):
            return False, None
        m = re.match(r"^\s*(\d{6,24})\s*$", raw)
        if m:
            return True, m.group(1)
        for pat in [r"^(?:单号|订单号)\s*[：:]?\s*(\d{6,24})\s*$", r"^(?:单|订单)\s+(\d{6,24})\s*$"]:
            m = re.match(pat, raw, re.IGNORECASE)
            if m:
                return True, m.group(1)
        return False, None

    @staticmethod
    def _extract_order_no(text: str) -> Optional[str]:
        """从文本提取单号（6~24 位数字）。"""
        if not text:
            return None
        for pat in [r"单号\s*[：:]\s*(\d{6,24})", r"订单号?\s*[：:]\s*(\d{6,24})", r"订单\s+(\d{6,24})", r"单\s+(\d{6,24})"]:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        m = re.search(r"(\d{6,24})(?=\D|$)", text)
        if m:
            return m.group(1)
        cleaned = re.sub(r'^[\u4e00-\u9fff]{1,3}', '', text.strip())
        if cleaned != text.strip():
            m = re.search(r"(\d{6,24})(?=\D|$)", cleaned)
            if m:
                return m.group(1)
        m = re.search(r"\b(\d{6,24})\b", text)
        return m.group(1) if m else None

    @staticmethod
    def _extract_mentions(text: str) -> List[str]:
        return re.findall(r"@[\w]{4,32}", text)

    @staticmethod
    def _extract_utr(text: str) -> Optional[str]:
        """提取 UTR（gxp_notify_bot 要求 12 位纯数字）。"""
        m = re.search(r"utr\s*[：:]\s*(\d{12})", text, re.IGNORECASE)
        if m:
            return m.group(1)
        m = re.search(r"\b(\d{12})\b", text)
        return m.group(1) if m else None

    def _get_valid_pending(self, context: Dict[str, Any], chat_id: Any, expire_seconds: int) -> tuple:
        """返回 (order_no or None, is_expired)。仅当 chat_id 一致且未过期时返回有效单号。"""
        pending_no = (context or {}).get("gxp_pending_order_no")
        pending_time = (context or {}).get("gxp_pending_time") or 0
        pending_chat = context.get("gxp_pending_chat_id")
        if not pending_no:
            return None, False
        if str(pending_chat) != str(chat_id):
            return None, False
        now = time.time()
        if expire_seconds > 0 and (now - pending_time) > expire_seconds:
            context["gxp_pending_order_no"] = None
            context["gxp_pending_time"] = None
            context["gxp_pending_chat_id"] = None
            return None, True
        return pending_no, False

    def _clear_pending(self, context: Dict[str, Any]) -> None:
        context["gxp_pending_order_no"] = None
        context["gxp_pending_time"] = None
        context["gxp_pending_chat_id"] = None

    def _get_template(self, name: str, **kwargs) -> str:
        kb_reply = self._kb_reply(name, **kwargs)
        if kb_reply:
            return kb_reply
        if hasattr(self.config, 'get_dynamic_templates_config'):
            dynamic = self.config.get_dynamic_templates_config() or {}
            tpl = dynamic.get(name)
            if tpl is None:
                tpl = self.config.get_templates_config().get(name) or ""
        else:
            tpl = self.config.get_templates_config().get(name) or ""
        if isinstance(tpl, list) and tpl:
            tpl = random.choice(tpl)
        tpl = str(tpl or "")
        for k, v in kwargs.items():
            tpl = tpl.replace("{" + k + "}", str(v))
        return tpl

    _RAW_GXP_CMDS = re.compile(
        r"^/(qhyy|tjcy|mnds|cxds|htds|cxdf|htdf|utr|hl|cxye|cgl)\b", re.IGNORECASE
    )

    async def execute(self, text: str, user_id: str, context: Dict[str, Any]) -> Optional[str]:
        tg = self.config.get_telegram_config() if hasattr(self.config, "get_telegram_config") else {}
        gxp_cfg = tg.get("gxp_commands") or {}
        if not gxp_cfg.get("enabled", True):
            return None
        send_to_chat = context.get("_send_to_chat")
        chat_id = context.get("chat_id")
        if not callable(send_to_chat) or chat_id is None:
            return None
        t = (text or "").strip()
        if re.search(r"查询失败|查询成功|操作成功|操作失败|不存在|已过期|无此订单", t):
            return None

        hint = "请查看上方机器人回复。"
        expire_sec = int(gxp_cfg.get("pending_order_expire_seconds") or 300)
        ask_when_bare = gxp_cfg.get("ask_when_bare_order_no", True)

        async def _send(cmd, h=None):
            return await self._send_cmd(send_to_chat, chat_id, cmd, h or hint, context)

        if self._RAW_GXP_CMDS.match(t):
            return await _send(t)

        is_ambiguous_what = (
            re.search(r"^(查|查一下|帮我查|查查|看看|查询|看一下|想查)$", t, re.IGNORECASE)
            or (len(t) <= 12 and re.search(r"查|看看|查询", t) and not re.search(
                r"汇率|余额|代收|提现|成功率|utr|回调|订单号|单号", t, re.IGNORECASE
            ))
            or (re.search(
                r"帮[我你]?查(单|订单)?(状态)?|查单(状态)?[啊]?|查新?订单|查(到了)?吗|查看(我?发?给?你?)?(的?)?订单",
                t, re.IGNORECASE
            ) and not re.search(r"代收|提现|汇率|余额|成功率|utr|回调", t, re.IGNORECASE))
            or re.match(r"^\s*查\s*\d{6,24}\s*$", t) or re.match(r"^\s*查\d{6,24}\s*$", t)
        )
        if is_ambiguous_what:
            order_in_msg = self._extract_order_no(t)
            if order_in_msg:
                context["gxp_pending_order_no"] = order_in_msg
                context["gxp_pending_time"] = time.time()
                context["gxp_pending_chat_id"] = chat_id
                context["gxp_last_ask"] = "intent"
                return self._get_template("gxp_ask_intent", order_no=order_in_msg)
            context["gxp_last_ask"] = "what"
            return self._get_template("gxp_ask_what")

        if context.get("gxp_last_ask") == "what" and re.match(r"^[1-5]\s*$", t):
            context["gxp_last_ask"] = None
            if t.strip() == "1":
                return await _send("/hl")
            if t.strip() == "2":
                return await _send("/cxye")
            if t.strip() == "5":
                return await _send("/cgl")
            if t.strip() == "3":
                pending_no, is_expired = self._get_valid_pending(context, chat_id, expire_sec)
                if pending_no and not is_expired:
                    self._clear_pending(context)
                    return await _send(f"/cxds {pending_no}")
                return self._kb_reply("gxp_hint_query_deposit") or "查代收订单请发单号，或说：查代收 单号。"
            if t.strip() == "4":
                pending_no, is_expired = self._get_valid_pending(context, chat_id, expire_sec)
                if pending_no and not is_expired:
                    self._clear_pending(context)
                    return await _send(f"/cxdf {pending_no}")
                return self._kb_reply("gxp_hint_query_withdraw") or "查提现订单请发单号，或说：查提现 单号。"

        _intent_digit_m = re.match(r"^([1-4])\s*[\u4e00-\u9fff]*", t)
        if context.get("gxp_last_ask") == "intent" and _intent_digit_m:
            context["gxp_last_ask"] = None
            pending_no, is_expired = self._get_valid_pending(context, chat_id, expire_sec)
            if not pending_no or is_expired:
                return (self._kb_reply("gxp_expired") or "单号已过期") if is_expired \
                    else (self._kb_reply("gxp_need_order_no") or "请先发送需要查询的单号。")
            self._clear_pending(context)
            choice = _intent_digit_m.group(1)
            if choice == "1":
                return await _send(f"/cxds {pending_no}")
            if choice == "2":
                return await _send(f"/htds {pending_no}")
            if choice == "3":
                return await _send(f"/cxdf {pending_no}")
            if choice == "4":
                return await _send(f"/htdf {pending_no}")

        if re.search(r"切换语言|换语言|语言切换", t, re.IGNORECASE):
            return await _send("/qhyy")
        if re.search(r"添加(操作)?成员|添加成员|@.*验证码", t, re.IGNORECASE) or ("tjcy" in t.lower() and "@" in t):
            mentions = self._extract_mentions(t)
            code = ""
            for part in re.split(r"[\s,，]+", t):
                part = part.strip()
                if part.isdigit() and 4 <= len(part) <= 10:
                    code = part
                    break
            cmd = " ".join(["/tjcy"] + mentions + ([code] if code else []))
            return await _send(cmd)
        if re.search(r"代收.*模拟|模拟.*回调|模拟回调|商户单号", t, re.IGNORECASE):
            no = self._extract_order_no(t)
            if no:
                return await _send(f"/mnds {no}")
            return self._kb_reply("gxp_hint_mock_callback") or "请提供商户单号，例如：代收模拟回调 12345678"
        if re.search(r"utr|补单|utr查询", t, re.IGNORECASE):
            utr = self._extract_utr(t)
            no = self._extract_order_no(t)
            if utr:
                cmd = f"/utr {utr}" + (f" {no}" if no and no != utr else "")
            elif no:
                cmd = f"/utr {no}"
            else:
                return self._kb_reply("gxp_hint_utr_query") or "请提供 UTR（12位数字）或单号"
            return await _send(cmd)
        if re.search(r"系统汇率|查汇率|汇率(多少)?|^/?hl\b", t, re.IGNORECASE):
            return await _send("/hl")
        if re.search(r"余额(查询)?|查余额|查询余额|cxye", t, re.IGNORECASE):
            return await _send("/cxye")
        if re.search(r"代收成功率|(查)?成功率|cgl|^5\s*$", t, re.IGNORECASE):
            return await _send("/cgl")

        is_bare, order_no = self._is_bare_order_no_skill(t)
        if is_bare and order_no and ask_when_bare:
            pending_no, _ = self._get_valid_pending(context, chat_id, expire_sec)
            if pending_no == order_no:
                return self._get_template("gxp_ask_same_no", order_no=order_no)
            context["gxp_pending_order_no"] = order_no
            context["gxp_pending_time"] = time.time()
            context["gxp_pending_chat_id"] = chat_id
            context["gxp_last_ask"] = "intent"
            return self._get_template("gxp_ask_intent", order_no=order_no)

        no_from_msg = self._extract_order_no(t)
        cmd_line: Optional[str] = None
        used_pending = False

        if self._INTENT_CXDS.search(t):
            no = no_from_msg or self._get_valid_pending(context, chat_id, expire_sec)[0]
            if no:
                cmd_line = f"/cxds {no}"
                if not no_from_msg:
                    used_pending = True
            else:
                pending_no, is_exp = self._get_valid_pending(context, chat_id, expire_sec)
                if is_exp:
                    return self._get_template("gxp_expired")
                return self._get_template("gxp_need_order_no")
        elif self._INTENT_HTDS.search(t):
            no = no_from_msg or self._get_valid_pending(context, chat_id, expire_sec)[0]
            if no:
                cmd_line = f"/htds {no}"
                if not no_from_msg:
                    used_pending = True
            else:
                pending_no, is_exp = self._get_valid_pending(context, chat_id, expire_sec)
                if is_exp:
                    return self._get_template("gxp_expired")
                return self._kb_reply("gxp_hint_callback_deposit") or "请提供单号，例如：回调代收 12345678"
        elif self._INTENT_CXDF.search(t):
            no = no_from_msg or self._get_valid_pending(context, chat_id, expire_sec)[0]
            if no:
                cmd_line = f"/cxdf {no}"
                if not no_from_msg:
                    used_pending = True
            else:
                cmd_line = "/cxdf"
        elif self._INTENT_HTDF.search(t):
            no = no_from_msg or self._get_valid_pending(context, chat_id, expire_sec)[0]
            if no:
                cmd_line = f"/htdf {no}"
                if not no_from_msg:
                    used_pending = True
            else:
                pending_no, is_exp = self._get_valid_pending(context, chat_id, expire_sec)
                if is_exp:
                    return self._get_template("gxp_expired")
                return self._kb_reply("gxp_hint_callback_withdraw") or "请提供单号，例如：回调提现 12345678"

        if cmd_line:
            try:
                await send_to_chat(int(chat_id), cmd_line)
                if used_pending:
                    self._clear_pending(context)
                    context["gxp_last_ask"] = None
                self.logger.info("已代发 gxp 命令: %s", cmd_line)
                record_fn = context.get("_record_gxp_cmd")
                if callable(record_fn):
                    try:
                        record_fn(int(chat_id), cmd_line,
                                  int(context.get("user_id") or 0),
                                  int(context.get("user_msg_id") or 0))
                    except Exception:
                        pass
                return self._kb_reply("gxp_request_sent", hint=hint) or f"已帮您发起请求，{hint}"
            except Exception as e:
                self.logger.warning("代发 gxp 命令失败: %s", e)
                return self._kb_reply("gxp_processing_fallback") or "好的亲，系统正在处理中，请稍等片刻～"
        return None

    async def _send_cmd(self, send_to_chat, chat_id: Any, cmd: str, hint: str,
                        context: Optional[Dict] = None) -> Optional[str]:
        try:
            await send_to_chat(int(chat_id), cmd)
            self.logger.info("已代发 gxp 命令: %s", cmd)
            record_fn = (context or {}).get("_record_gxp_cmd")
            if callable(record_fn):
                try:
                    record_fn(int(chat_id), cmd,
                              int((context or {}).get("user_id") or 0),
                              int((context or {}).get("user_msg_id") or 0))
                except Exception:
                    pass
            return self._kb_reply("gxp_request_sent", hint=hint) or f"已帮您发起请求，{hint}"
        except Exception as e:
            self.logger.warning("代发 gxp 命令失败: %s", e)
            return self._kb_reply("gxp_processing_fallback") or "好的亲，系统正在处理中，请稍等片刻～"
