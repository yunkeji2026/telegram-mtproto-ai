"""通道信息技能（含额度规则子分支），支持代收/代付分开查询"""

import re
import time
from typing import Dict, Any, Optional

from src.skills.base import Skill
from src.utils.channel_status_format import (
    is_channel_disabled,
    is_direction_disabled,
    customer_should_omit_channel,
    _get_sub,
    AMOUNT_TYPE_LABELS,
)


def _detect_direction(text: str) -> str:
    """检测用户问的是代收、代付还是两者都问。返回 'payin' / 'payout' / 'both'"""
    t = (text or "").lower()
    has_payin = "代收" in t or "payin" in t or "collection" in t
    has_payout = "代付" in t or "payout" in t or "disburs" in t or "下发" in t
    if has_payin and has_payout:
        return "both"
    if has_payin:
        return "payin"
    if has_payout:
        return "payout"
    return "both"


class _TranslationCache:
    """Lightweight LRU translation cache with TTL to avoid redundant API calls."""

    def __init__(self, maxsize: int = 64, ttl: float = 300):
        self._maxsize = maxsize
        self._ttl = ttl
        self._store: Dict[str, tuple] = {}
        self._order: list = []

    def get(self, key: str) -> Optional[str]:
        if key not in self._store:
            return None
        ts, val = self._store[key]
        if time.time() - ts > self._ttl:
            self._store.pop(key, None)
            try:
                self._order.remove(key)
            except ValueError:
                pass
            return None
        return val

    def put(self, key: str, val: str):
        if key in self._store:
            try:
                self._order.remove(key)
            except ValueError:
                pass
        elif len(self._store) >= self._maxsize:
            old = self._order.pop(0)
            self._store.pop(old, None)
        self._store[key] = (time.time(), val)
        self._order.append(key)


class ChannelInfoSkill(Skill):
    """通道信息技能（含额度规则子分支：按群名区分普通/特殊/黑名单）"""

    _translation_cache = _TranslationCache(maxsize=64, ttl=300)

    def __init__(self, config, ai_client):
        super().__init__(config, ai_client)
        self.priority = 6

    def _get_strategy_overrides(self, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        so = super()._get_strategy_overrides(context) or {}
        so["context_rounds"] = 0
        return so

    def _get_disabled_channel_info(self) -> dict:
        try:
            rates = getattr(self.config, 'get_exchange_rates_config', lambda: None)()
            channels = (rates or {}).get('channels', {})
            return {
                k: (ch.get('display_name') or k.upper())
                for k, ch in channels.items()
                if isinstance(ch, dict) and is_channel_disabled(ch)
            }
        except Exception:
            return {}

    def _get_active_channel_summary(self) -> str:
        try:
            rates = getattr(self.config, 'get_exchange_rates_config', lambda: None)()
            channels = (rates or {}).get('channels', {})
            parts = []
            for k, ch in channels.items():
                if not isinstance(ch, dict):
                    continue
                if is_channel_disabled(ch):
                    continue
                name = ch.get('display_name') or k.upper()
                dir_parts = []
                for d, label in [("payin", "代收"), ("payout", "代付")]:
                    if isinstance(ch.get(d), dict) and not is_direction_disabled(ch, d):
                        dir_parts.append(label)
                if dir_parts:
                    parts.append(f"{name}（{'/'.join(dir_parts)}可用）")
                else:
                    parts.append(f"{name}")
            return "、".join(parts) if parts else "暂无"
        except Exception:
            return "暂无"

    def _check_asking_disabled_channel(self, text: str) -> str:
        disabled = self._get_disabled_channel_info()
        if not disabled:
            return ""
        text_lower = (text or "").lower()
        asked_disabled = []
        for key, display_name in disabled.items():
            names_to_check = [key.lower(), display_name.lower()]
            rates = getattr(self.config, 'get_exchange_rates_config', lambda: None)()
            ch = (rates or {}).get('channels', {}).get(key, {})
            for alias in (ch.get('names') or []):
                names_to_check.append(str(alias).lower())
            if any(n in text_lower for n in names_to_check):
                asked_disabled.append(display_name)
        if not asked_disabled:
            return ""
        active_summary = self._get_active_channel_summary()
        names = "、".join(asked_disabled)
        return f"{names}目前已下线，暂不可用哈。当前可用的通道有：{active_summary}。"

    def _get_disabled_channel_ids(self) -> set:
        try:
            rates = getattr(self.config, 'get_exchange_rates_config', lambda: None)()
            channels = (rates or {}).get('channels', {})
            return {
                k for k, ch in channels.items()
                if isinstance(ch, dict) and is_channel_disabled(ch)
            }
        except Exception:
            return set()

    def _format_quota_range(self, range_str: str) -> str:
        if not range_str or "-" not in range_str:
            return range_str or ""
        parts = range_str.strip().split("-", 1)
        if len(parts) != 2:
            return range_str
        low, high = parts[0].strip(), parts[1].strip()
        try:
            high_int = int(high.replace(",", ""))
            high = f"{high_int:,}"
        except ValueError:
            pass
        return f"{low} – {high}"

    def _get_live_direction_info(self, channel_key: str, direction: str) -> Dict[str, Any]:
        """从 exchange_rates.yaml 读取指定通道指定方向的实时数据"""
        try:
            rates = getattr(self.config, 'get_exchange_rates_config', lambda: None)()
            ch = (rates or {}).get('channels', {}).get(channel_key, {})
            return {
                "fee_rate": _get_sub(ch, direction, "fee_rate") or "",
                "success_rate": _get_sub(ch, direction, "success_rate"),
                "minimum_amount": str(_get_sub(ch, direction, "minimum_amount") or ""),
                "maximum_amount": str(_get_sub(ch, direction, "maximum_amount") or ""),
                "status": str(_get_sub(ch, direction, "status") or "正常"),
                "amount_type": _get_sub(ch, direction, "amount_type") or ch.get("amount_type", ""),
            }
        except Exception:
            return {}

    def _quota_reply(self, text: str, context: Dict[str, Any]) -> Optional[str]:
        """额度子分支：按 quota_rules.yaml 与群名生成固定回复，区分代收/代付"""
        rules = getattr(self.config, "get_quota_rules", None)
        if not callable(rules):
            return None
        quota = rules()
        if not quota or not isinstance(quota.get("channels"), dict):
            if (text or "").strip() and any(k in (text or "") for k in ("额度", "限额", "能过多少", "最大多少")):
                self.logger.warning("额度规则未加载或为空，额度类问题将走 AI 回复")
            return None
        supplement = (quota.get("quota_note_supplement") or "").strip()

        def _append_supplement(s: str) -> str:
            if not s or not supplement:
                return s
            return s + "\n\n" + supplement

        channels_cfg = quota["channels"]
        quota_kw = quota.get("quota_keywords") or []
        raw_text = text or ""
        text_lower = raw_text.lower().strip()
        meta_words = ("知识库", "他没地方", "不会", "没地方判断")
        if any(m in raw_text for m in meta_words) and any(k in raw_text for k in ("限额", "额度")):
            return None

        _multilang_quota_kw = (
            "limit", "quota", "amount", "maximum", "minimum",
            "حد", "مبلغ", "حد", "رقم", "سیما", "राशि",
        )
        is_quota = any(kw in raw_text for kw in quota_kw)
        if not is_quota:
            is_quota = any(kw in text_lower for kw in _multilang_quota_kw)
        if not is_quota:
            for ch_id, ch in channels_cfg.items():
                if not isinstance(ch, dict):
                    continue
                for name in ch.get("names") or []:
                    if name.lower() in text_lower:
                        is_quota = True
                        break
        if not is_quota:
            return None

        direction = _detect_direction(raw_text)

        asked_ep = any(n.lower() in text_lower for n in (channels_cfg.get("ep") or {}).get("names") or ["ep", "easypaisa"])
        asked_jc = any(n.lower() in text_lower for n in (channels_cfg.get("jc") or {}).get("names") or ["jc", "jazzcash"]) or "jp" in text_lower
        if not asked_ep and not asked_jc and is_quota:
            asked_ep, asked_jc = True, True
        disabled_ids = self._get_disabled_channel_ids()
        if "ep" in disabled_ids:
            asked_ep = False
        if "jc" in disabled_ids:
            asked_jc = False
        if not asked_ep and not asked_jc and is_quota and disabled_ids:
            return None

        want_both_tiers = ("普通" in raw_text and "特殊" in raw_text) and asked_ep
        chat_title = (context.get("chat_title") or "").strip()
        special_list = quota.get("special_groups") or []
        blacklist_map = quota.get("blacklist_groups") or {}
        if not isinstance(blacklist_map, dict):
            blacklist_map = {}
        is_special = chat_title in special_list
        is_blacklist = chat_title in blacklist_map
        templates = quota.get("templates") or {}

        def _quota_lang(txt: str) -> str:
            if not txt:
                return "zh"
            if re.search(r"[\u0600-\u06FF\u0750-\u077F\u0400-\u04FF]", txt):
                return "zh"
            letters = len(re.findall(r"[A-Za-z]", txt))
            cjk = len(re.findall(r"[\u4e00-\u9fff]", txt))
            if letters >= 3 and cjk == 0:
                return "en"
            return "zh"

        lang = _quota_lang(raw_text)
        en_tpl = templates.get("en") if isinstance(templates.get("en"), dict) else {}
        if lang == "en" and en_tpl:
            line_only_tpl = en_tpl.get("line_only") or "{channel_name}: {range}"
            footer = templates.get("footer_en") or "\n\nNote: limits may change in real time; please refer to actual submission."
            ask_tpl = en_tpl.get("ask_channel") or "Which channel would you like to check?"
        else:
            line_only_tpl = templates.get("line_only") or "{channel_name}：{range}"
            footer = templates.get("footer_zh") or "\n\n当前通道额度根据实时情况会有调整，请以实际提交为准。"
            ask_tpl = templates.get("ask_channel") or "请问您需要查询哪个通道的额度？目前支持的通道有：EP, JC\n请回复具体通道代号获取详细信息。"

        if is_blacklist:
            custom = blacklist_map.get(chat_title)
            if isinstance(custom, dict):
                parts = []
                if asked_ep and "ep" in custom:
                    parts.append((custom["ep"] or "").strip())
                if asked_jc and "jc" in custom:
                    parts.append((custom["jc"] or "").strip())
                if parts:
                    return _append_supplement("\n\n".join(p for p in parts if p))
                return _append_supplement(ask_tpl)
            return _append_supplement(ask_tpl)
        if not asked_ep and not asked_jc:
            return _append_supplement(ask_tpl)

        directions_to_show = []
        if direction in ("payin", "both"):
            directions_to_show.append(("payin", "代收" if lang == "zh" else "Payin"))
        if direction in ("payout", "both"):
            directions_to_show.append(("payout", "代付" if lang == "zh" else "Payout"))

        line_parts = []
        for ch_key, ch_asked in [("ep", asked_ep), ("jc", asked_jc)]:
            if not ch_asked:
                continue
            ch = channels_cfg.get(ch_key)
            if not isinstance(ch, dict):
                continue
            ch_name = ch_key.upper()
            live = {}
            try:
                rates = getattr(self.config, 'get_exchange_rates_config', lambda: None)()
                live_ch = (rates or {}).get('channels', {}).get(ch_key, {})
            except Exception:
                live_ch = {}

            for d_key, d_label in directions_to_show:
                d_amt = _get_sub(live_ch, d_key, "amount_type") or live_ch.get("amount_type", "")
                amt_label = AMOUNT_TYPE_LABELS.get(d_amt, "")
                info = self._get_live_direction_info(ch_key, d_key)
                if info.get("status", "").strip() in ("禁用", "disabled", "停用"):
                    continue
                if want_both_tiers and ch_key == "ep":
                    line_parts.append(line_only_tpl.format(
                        channel_name=f"{ch_name}{d_label}（普通客户）",
                        range=self._format_quota_range(ch.get("default_range") or f"{info.get('minimum_amount','100')}-{info.get('maximum_amount','20000')}")
                    ))
                    line_parts.append(line_only_tpl.format(
                        channel_name=f"{ch_name}{d_label}（特殊客户）",
                        range=self._format_quota_range(ch.get("special_range") or ch.get("default_range") or "100-100000")
                    ))
                else:
                    lo = info.get("minimum_amount", "100")
                    hi = info.get("maximum_amount", "100000")
                    if is_special and ch_key == "ep":
                        range_str = ch.get("special_range") or f"{lo}-{hi}"
                    else:
                        range_str = ch.get("default_range") or f"{lo}-{hi}"
                    entry = line_only_tpl.format(
                        channel_name=f"{ch_name} {d_label}",
                        range=self._format_quota_range(range_str)
                    )
                    if amt_label:
                        entry += f"（{amt_label}）"
                    line_parts.append(entry)

        if not line_parts:
            return _append_supplement(ask_tpl)
        header = "当前额度如下：\n" if lang == "zh" else "Current limits:\n"
        return _append_supplement(header + "\n".join(line_parts) + footer)

    _LANG_NAMES = {
        "en": "English", "ar_ur": "Arabic/Urdu", "hi": "Hindi",
        "ru": "Русский", "ja": "日本語", "ko": "한국어",
        "th": "ภาษาไทย", "pt": "Português", "es": "Español",
    }

    async def _translate_via_ai(self, zh_text: str, target_lang: str,
                                context: Optional[Dict[str, Any]] = None) -> str:
        """Use AI to translate a Chinese programmatic reply into the user's language.
        Results are cached (keyed on text hash + lang) to avoid redundant API calls."""
        import hashlib
        _cache_key = hashlib.md5(f"{zh_text[:500]}|{target_lang}".encode()).hexdigest()
        cached = self._translation_cache.get(_cache_key)
        if cached:
            self.logger.debug("Translation cache hit (%s): %s...", target_lang, cached[:40])
            return cached

        lang_name = self._LANG_NAMES.get(target_lang, target_lang)
        try:
            translate_ctx = (context or {}).copy()
            translate_ctx["_intent_supplement"] = (
                f"将以下中文客服回复翻译为{lang_name}。保持格式、数字和通道名(EP/JC等)不变，"
                f"语气自然专业。只输出翻译结果，不加解释。"
            )
            translate_ctx["_skip_lang_guard"] = True
            translate_ctx["_current_user_message_for_lang"] = "x" * 5
            reply = await self.ai_client.generate_reply(
                f"请将以下内容翻译为{lang_name}：\n\n{zh_text}",
                translate_ctx,
            )
            if reply and len(reply) > 10:
                self._translation_cache.put(_cache_key, reply)
                return reply
        except Exception as e:
            self.logger.warning("AI翻译失败(%s): %s", target_lang, e)
        return zh_text

    _OPTIMIZE_KW = ("优化", "optimize", "improvement", "بہتری")

    def _build_channel_blocks(self, channels: dict, asked_keys: list,
                              direction: str) -> tuple:
        """构建通道数据块，返回 (lines, has_abnormal)。"""
        lines = []
        has_abnormal = False
        for key in asked_keys:
            ch = channels.get(key)
            if not isinstance(ch, dict):
                continue
            if is_channel_disabled(ch):
                name = ch.get('display_name') or key.upper()
                lines.append(f"**{name}** 目前已下线，暂不可用。")
                has_abnormal = True
                continue
            name = ch.get('display_name') or key.upper()
            dir_lines = []
            for d, label in [("payin", "代收"), ("payout", "代付")]:
                if direction not in ("both", d):
                    continue
                if not isinstance(ch.get(d), dict):
                    continue
                if is_direction_disabled(ch, d):
                    dir_lines.append(f"  * {label}：已暂停")
                    has_abnormal = True
                    continue
                st = str(_get_sub(ch, d, "status") or "正常")
                if st not in ("正常", "normal"):
                    has_abnormal = True
                sr = _get_sub(ch, d, "success_rate")
                lo = _get_sub(ch, d, "minimum_amount") or ""
                hi = _get_sub(ch, d, "maximum_amount") or ""
                pt = _get_sub(ch, d, "processing_time") or ""
                amt = _get_sub(ch, d, "amount_type") or ch.get("amount_type", "")
                amt_lbl = AMOUNT_TYPE_LABELS.get(amt, "")

                parts = [f"成功率 {sr}%" if sr is not None else None]
                if lo and hi:
                    try:
                        hi_fmt = f"{int(str(hi).replace(',', '')):,}"
                    except ValueError:
                        hi_fmt = hi
                    parts.append(f"限额 {lo}-{hi_fmt}")
                parts.append(f"状态{st}")
                if pt:
                    parts.append(f"处理时间{pt}")
                if amt_lbl:
                    parts.append(f"金额类型{amt_lbl}")
                dir_lines.append(f"  * {label}：{'，'.join(p for p in parts if p)}")

            if dir_lines:
                lines.append(f"**{name}**")
                lines.extend(dir_lines)
        return lines, has_abnormal

    def _optimize_reply(self, channels: dict, asked_keys: list) -> Optional[str]:
        """「优化」专用话术：承认关切 + 实时数据 + 持续优化承诺。"""
        lines, has_abnormal = self._build_channel_blocks(
            channels, asked_keys, "both"
        )
        if not lines:
            return None

        header = "明白您的意思，我来汇报下当前通道表现：\n\n"
        body = "\n".join(lines)
        if has_abnormal:
            closing = (
                "\n\n部分通道目前存在波动，我们正在积极跟进优化。"
                "有任何进展会第一时间通知您，感谢理解与支持。"
            )
        else:
            closing = (
                "\n\n整体运行正常，我们会持续关注并优化通道表现，"
                "有任何变化会第一时间通知您。"
            )
        return header + body + closing

    def _programmatic_channel_reply(self, text: str, context: Dict[str, Any]) -> Optional[str]:
        """当有实时通道数据时，程序化生成通道状态/成功率回复，不走 AI。"""
        live_status = (context or {}).get("channel_status_info", "").strip()
        if not live_status:
            return None

        raw = (text or "").lower().strip()
        _status_kw = (
            "成功率", "状态", "通道", "channel", "status", "success",
            "rate", "payin", "payout", "代收", "代付",
            "额度", "限额", "优化", "能过多少", "最大", "最小",
            "limit", "quota", "amount",
            "ep", "jc", "easypaisa", "jazzcash",
        )
        if not any(kw in raw for kw in _status_kw):
            return None

        try:
            rates = getattr(self.config, 'get_exchange_rates_config', lambda: None)()
            channels = (rates or {}).get('channels', {})
        except Exception:
            return None
        if not channels:
            return None

        asked_keys = self._detect_asked_channels(raw, channels)

        is_optimize = any(kw in raw for kw in self._OPTIMIZE_KW)
        if is_optimize:
            return self._optimize_reply(channels, asked_keys)

        direction = _detect_direction(text or "")
        lines, _ = self._build_channel_blocks(channels, asked_keys, direction)

        if not lines:
            return None

        header = "当前通道信息如下：\n"
        footer = "\n\n以上数据为实时数据，具体以实际提交为准。"
        return header + "\n".join(lines) + footer

    def _detect_asked_channels(self, text_lower: str, channels: dict) -> list:
        """检测用户问到了哪些通道，返回 key 列表；若没指定则返回所有活跃通道。"""
        asked = []
        for key, ch in channels.items():
            if not isinstance(ch, dict):
                continue
            if customer_should_omit_channel(key, ch):
                continue
            names_to_check = [key.lower()]
            if ch.get("display_name"):
                names_to_check.append(ch["display_name"].lower())
            for alias in (ch.get("names") or []):
                names_to_check.append(str(alias).lower())
            if any(n in text_lower for n in names_to_check):
                asked.append(key)
        if not asked:
            for key, ch in channels.items():
                if not isinstance(ch, dict):
                    continue
                if customer_should_omit_channel(key, ch):
                    continue
                if not is_channel_disabled(ch):
                    asked.append(key)
        return asked

    async def execute(self, text, user_id, context):
        """提供通道信息（禁用拦截 > 程序化实时回复 > 额度模板 > AI > 兜底）"""
        _reply_lang = (context or {}).get("reply_lang", "zh")

        disabled_reply = self._check_asking_disabled_channel(text)
        if disabled_reply:
            if _reply_lang != "zh":
                self.logger.info("禁用通道拦截(非中文 %s → AI翻译): %s", _reply_lang, disabled_reply[:60])
                return await self._translate_via_ai(disabled_reply, _reply_lang, context)
            self.logger.info("禁用通道拦截: %s", disabled_reply[:80])
            return disabled_reply

        programmatic = self._programmatic_channel_reply(text or "", context or {})
        if programmatic:
            if _reply_lang != "zh":
                self.logger.info("程序化通道回复(非中文 %s → AI翻译): %s", _reply_lang, programmatic[:80])
                return await self._translate_via_ai(programmatic, _reply_lang, context)
            self.logger.info("程序化通道回复（绕过AI）: %s", programmatic[:100])
            return programmatic

        _has_kb = bool(context and context.get("kb_context"))
        _in_bot_followup = bool(
            context
            and context.get("_bot_question_ts")
            and (time.time() - context.get("_bot_question_ts", 0)) < 120
        )

        quota_reply = None
        if not _has_kb and not _in_bot_followup:
            quota_reply = self._quota_reply(text or "", context or {})
            if not quota_reply and context:
                last_msg = (context.get("last_message") or "").strip()
                cur = (text or "").strip()
                if last_msg and ("额度" in last_msg or "通道" in last_msg) and any(k in cur for k in ("介绍", "也不知道", "你说", "听不懂")):
                    quota_reply = self._quota_reply("通道的额度", context)
        if quota_reply:
            if _reply_lang not in ("zh", "en") and not any(c in quota_reply for c in "abcdefghijklmnopqrstuvwxyz"):
                return await self._translate_via_ai(quota_reply, _reply_lang, context)
            return quota_reply
        try:
            reply = await self.ai_client.generate_reply_with_intent(
                user_message=text, intent='channel_info',
                user_context=context,
                strategy_overrides=self._get_strategy_overrides(context)
            )
            if reply:
                return reply
        except Exception as e:
            self.logger.warning(f"AI生成通道信息回复失败: {e}")
        return self._kb_fallback("channel_info", lang=_reply_lang)
