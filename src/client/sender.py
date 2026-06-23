"""
发送 Mixin：消息发送、回复分段、术语替换、日志脱敏
"""

import asyncio
import html
import os
import random
import re
import time
from typing import Any, Dict, List, Optional


class TelegramSenderMixin:

    def _reply_to_message_id_for_send(self, original_message) -> Optional[int]:
        """Telegram reply / quote bar: off for natural chat when configured or conversion domain."""
        tg = (self.config.get("telegram") or {}) if getattr(self, "config", None) else {}
        if "reply_to_user_message" in tg:
            return int(original_message.id) if tg.get("reply_to_user_message") else None
        try:
            from src.utils.domain_policy import effective_domain_name

            raw = self.config.config if hasattr(self.config, "config") else {}
            if isinstance(raw, dict) and effective_domain_name(raw) == "conversion":
                return None
        except Exception:
            pass
        return int(original_message.id)

    def _sanitize_parenthetical_stage_directions(self, text: str) -> str:
        """Strip short （…）/(...) asides typical of LLM stage directions; conversion domain only."""
        if not text:
            return text
        try:
            from src.utils.domain_policy import effective_domain_name

            raw = self.config.config if hasattr(self.config, "config") else {}
            if not isinstance(raw, dict) or effective_domain_name(raw) != "conversion":
                return text
        except Exception:
            return text
        t = text
        t = re.sub(r"（[^）]{1,28}）", "", t)
        t = re.sub(r"\([^)]{1,32}\)", "", t)
        return re.sub(r"[ \t\f\v]{2,}", " ", t).strip()

    def _rewrite_companion_helpdesk_ping(
        self, reply: str, user_message: str
    ) -> str:
        """conversion 域：用户短寒暄/探询（在吗等）时，避免「有什么可以帮」类客服套话。"""
        if not reply or not (user_message or "").strip():
            return reply
        try:
            from src.utils.domain_policy import effective_domain_name

            raw = self.config.config if hasattr(self.config, "config") else {}
            if not isinstance(raw, dict) or effective_domain_name(raw) != "conversion":
                return reply
        except Exception:
            return reply
        try:
            from src.utils.greeting_lexicon import (
                is_greeting_message,
                is_standalone_zai_query,
            )
        except Exception:
            return reply
        u = (user_message or "").strip()
        if len(u) > 36:
            return reply
        if not (is_greeting_message(u) or is_standalone_zai_query(u)):
            return reply
        markers = (
            "有什么可以帮",
            "请问有什么",
            "需要什么服务",
            "竭诚为您",
            "为您服务",
        )
        if not any(m in reply for m in markers):
            return reply
        if len(reply) <= 80:
            return random.choice(
                (
                    "嗯嗯我在～怎么啦？",
                    "在呀，找我呢？",
                    "在的，你说～",
                    "来啦～刚还在看手机",
                )
            )
        for old, new in (
            ("在的，有什么可以帮您的？", "在呀～"),
            ("在的，有什么可以帮您？", "在呀～"),
            ("有什么可以帮您的？", "怎么啦？"),
            ("有什么可以帮您？", "怎么啦？"),
        ):
            if old in reply:
                reply = reply.replace(old, new, 1)
        return reply

    def _apply_terminology(self, text: str) -> str:
        if not (text and isinstance(text, str)):
            return text or ""
        terms = (self.config.get("ai") or {}).get("terminology") or {}
        if not isinstance(terms, dict):
            return text
        for wrong, right in sorted(terms.items(), key=lambda x: -len(x[0])):
            if wrong and right is not None:
                text = text.replace(wrong, str(right))
        return text

    def _split_at_safe_boundary(self, text: str, max_pos: int) -> int:
        if max_pos >= len(text):
            return len(text)
        pay_in = re.search(r"Pay\s+in", text, re.I)
        if pay_in:
            a, b = pay_in.start(), pay_in.end()
            if a < max_pos < b:
                return a if max_pos - a <= b - max_pos else b
        pay_out = re.search(r"Pay\s+out", text, re.I)
        if pay_out:
            a, b = pay_out.start(), pay_out.end()
            if a < max_pos < b:
                return a if max_pos - a <= b - max_pos else b
        for m in re.finditer(r"\bEP\b|\bJC\b", text):
            a, b = m.start(), m.end()
            if a < max_pos < b:
                return a if max_pos - a <= 1 else b
        for m in re.finditer(r"\d{4,}", text):
            a, b = m.start(), m.end()
            if a < max_pos < b:
                return a if max_pos - a < b - max_pos else b
        slice_ = text[:max_pos]
        for sep in ("\n", "。", "！", "？", ".", "!", "?", "，", ",", ";", "；"):
            idx = slice_.rfind(sep)
            if idx >= max_pos // 2:
                return idx + 1
        idx = slice_.rfind(" ")
        if idx >= max_pos // 2:
            return idx + 1
        return max_pos

    def _chunk_segment_safe(self, seg: str, max_chars: int) -> List[str]:
        seg = seg.strip()
        if not seg:
            return []
        if len(seg) <= max_chars:
            return [seg]
        out: List[str] = []
        rest = seg
        while len(rest) > max_chars:
            cut = self._split_at_safe_boundary(rest, max_chars)
            if cut <= 0:
                cut = max_chars
            piece = rest[:cut].strip()
            if piece:
                out.append(piece)
            rest = rest[cut:].strip()
        if rest:
            out.append(rest)
        return out if out else [seg]

    def _split_reply_for_send(
        self,
        text: str,
        max_chars_per_message: int = 120,
        min_segments_to_split: int = 2,
    ) -> List[str]:
        s = (text or "").strip()
        if not s:
            return []
        if len(s) <= max_chars_per_message:
            return [s]
        segments = [t.strip() for t in re.split(r"\n\s*\n", s) if t.strip()]
        if len(segments) < min_segments_to_split:
            return self._chunk_segment_safe(s, max_chars_per_message)
        chunks: List[str] = []
        for seg in segments:
            if len(seg) <= max_chars_per_message:
                chunks.append(seg)
            else:
                sentences = re.split(r"(?<=[。！？.!?])\s*", seg)
                sentences = [x.strip() for x in sentences if x.strip()]
                current = ""
                for sent in sentences:
                    if len(sent) > max_chars_per_message:
                        if current:
                            chunks.append(current)
                            current = ""
                        chunks.extend(self._chunk_segment_safe(sent, max_chars_per_message))
                        continue
                    if not current:
                        current = sent
                    elif len(current) + len(sent) + 1 <= max_chars_per_message:
                        current = (current + " " + sent) if current else sent
                    else:
                        if current:
                            chunks.append(current)
                        current = sent
                if current:
                    chunks.append(current)
        return chunks if chunks else [s]

    def _log_safe_text(self, text: str, max_chars: Optional[int] = None) -> str:
        log_cfg = (self.config.get("logging") or {}).get("desensitize") or {}
        if not log_cfg.get("enabled", False):
            return (text or "")[: max_chars or 500]
        max_c = int(log_cfg.get("max_chars", 80) or 80)
        max_digit = int(log_cfg.get("max_digit_run", 6) or 6)
        s = text or ""
        if max_digit > 0:
            s = re.sub(r"\d{%d,}" % max_digit, "***", s)
        if len(s) > max_c:
            s = s[:max_c] + "…"
        return s

    def _shared_send_limiter(self, cfg):
        """取与 B 线协议自动回复共用的 AutoReplyLimiter 单例（一个计数器喂两线）。

        失败返回 None（闸门/计数静默降级，绝不阻断 A 线发送）。
        """
        try:
            from src.integrations.protocol_autoreply_limits import (
                get_autoreply_limiter,
            )
            return get_autoreply_limiter(cfg or {})
        except Exception:
            return None

    async def _send_reply(self, original_message, reply_text: str, parse_mode=None):
        try:
            # G1 全局 Kill-Switch：紧急冻结时直接跳过发送（无视预热闸门是否开）。
            try:
                from src.ops.kill_switch import is_blocked as _ks_blocked
                _ks_on, _ks_scope, _ = _ks_blocked(
                    "telegram", getattr(self, "account_id", "default"))
                if _ks_on:
                    self.logger.warning(
                        "[kill-switch] 冻结发送，跳过 A 线回复（scope=%s）", _ks_scope)
                    return
            except Exception:
                pass
            # N 线 核心3：发送前反封号闸门（A/B 两线共用 companion_send_gate；默认关→零破坏）
            # 优化1：sends_today 取自与 B 线共用的同一计数器（_shared_send_limiter），
            # A 线发送在成功后也记入该计数器 → 一个计数器喂两线，A 线反封号满血。
            try:
                from src.skills.companion_send_gate import evaluate, gate_enabled
                from src.skills.account_signals import build_account_signals
                _gcfg = self.config.config if hasattr(self.config, "config") else {}
                if gate_enabled(_gcfg):
                    _sig = build_account_signals(
                        "telegram", getattr(self, "account_id", "default"),
                        limiter=self._shared_send_limiter(_gcfg),
                        extra={"proxy_bound": bool(getattr(self, "proxy_id", ""))},
                    )
                    _dec = evaluate(_sig, _gcfg)
                    if not _dec.get("allowed", True):
                        self.logger.warning(
                            "[send_gate] 账号 %s 被反封号闸门拦截: %s (light=%s, score=%s)",
                            _sig["account_id"], _dec.get("reason"),
                            _dec.get("light"), _dec.get("score"),
                        )
                        return
            except Exception:
                pass
            split_cfg = self.config.get("reply", {}).get("split_send", {})
            min_interval = float(split_cfg.get("min_interval_seconds", 0) or 0)
            if min_interval > 0 and self._last_send_wallclock > 0:
                elapsed = time.time() - self._last_send_wallclock
                if elapsed < min_interval:
                    await asyncio.sleep(min_interval - elapsed)
            if not self.client:
                self.logger.error("客户端未初始化，无法发送回复")
                return
            _out_text = self._sanitize_parenthetical_stage_directions(reply_text)
            _rt = self._reply_to_message_id_for_send(original_message)
            send_kw: Dict[str, Any] = dict(
                chat_id=original_message.chat.id,
                text=_out_text,
            )
            if _rt is not None:
                send_kw["reply_to_message_id"] = _rt
            if parse_mode is not None:
                send_kw["parse_mode"] = parse_mode
            await self.client.send_message(**send_kw)
            self._last_send_wallclock = time.time()
            # 优化1：记入共用发送计数器（与 B 线 AutoReplyLimiter 同一份 day_used），
            # 供反封号闸门 + 机群健康灯统计本号今日外发量（best-effort，绝不阻断发送）。
            try:
                _lim = self._shared_send_limiter(
                    self.config.config if hasattr(self.config, "config") else {}
                )
                if _lim is not None:
                    _lim.record_sent(f"telegram:{getattr(self, 'account_id', 'default')}")
            except Exception:
                pass
            # N4b：出站镜像（companion 模式才生效）→ 坐席台看到 AI 自动回复的内容
            try:
                _emit = getattr(self, "_emit_inbox", None)
                if _emit is not None:
                    _emit(chat_id=original_message.chat.id, text=_out_text,
                          direction="out")
            except Exception:
                pass
            # Q3：出站记入 contacts（recorder 未开则 no-op）→ IntimacyEngine 才有
            # 收/发互动（mutuality）信号，分数不再因只见入站而偏低
            try:
                from src.utils.companion_context import (
                    record_relationship_message as _rec_rel_msg,
                )
                _rec_rel_msg(
                    getattr(self, "account_id", "default"),
                    original_message.chat.id, "out",
                    text_preview=_out_text or "",
                )
            except Exception:
                pass
            if getattr(original_message, 'from_user', None) and getattr(original_message.from_user, 'id', None):
                self._record_session_reply(original_message.chat.id, original_message.from_user.id)
                if getattr(self, 'four_layer_trigger', None):
                    self.four_layer_trigger.update_cooldown(
                        f"group_{original_message.chat.id}",
                        str(original_message.from_user.id),
                    )
            self.logger.info("已回复消息: %s", self._log_safe_text(reply_text))
        except Exception as e:
            self.logger.error("发送回复失败: %s", e)
            # G2 封号信号自动急停：风控错误 → 分级处置（退避/暂停/封禁），best-effort
            try:
                from src.ops.ban_signal import handle_send_exception as _g2
                _g2("telegram", getattr(self, "account_id", "default"), e)
            except Exception:
                pass

    async def send_message(self, chat_id: int, text: str) -> bool:
        try:
            if not self.client:
                self.logger.error("客户端未初始化")
                return False
            await self.client.send_message(chat_id, text)
            self.logger.info("已发送消息到 %s: %s...", chat_id, text[:50])
            return True
        except Exception as e:
            self.logger.error("发送消息失败: %s", e)
            return False

    async def send_photo(self, chat_id: Any, photo_path: str,
                         caption: str = "") -> bool:
        """A 线主客户端直发照片（Pyrogram send_photo）。供陪伴形象照「直发」缝。

        失败绝不抛、返回 False（调用方退回文字陪伴）；命中风控走 G2 封号信号分级处置。
        """
        try:
            if not self.client:
                self.logger.error("客户端未初始化")
                return False
            if not photo_path:
                return False
            await self.client.send_photo(chat_id, photo_path, caption=caption or "")
            self.logger.info("已发送照片到 %s（%s）", chat_id, photo_path)
            return True
        except Exception as e:
            self.logger.error("发送照片失败: %s", e)
            try:
                from src.ops.ban_signal import handle_send_exception as _g2
                _g2("telegram", getattr(self, "account_id", "default"), e)
            except Exception:
                pass
            return False

    async def _send_escalation_private_jump_hint(
        self,
        peer: Any,
        spec: Dict[str, Any],
        message_id: int,
        *,
        after_forward_ok: bool,
    ) -> None:
        """
        私聊内追加一条「可点击定位」说明：HTML 正文 + 内联按钮（t.me 或 tg://openmessage）。
        解决仅靠转发条在部分客户端无法跳回群内指定消息的问题。
        """
        if not self.client:
            return
        he_cfg = (self.config.get("human_escalation") or {}) if self.config else {}
        if not bool(he_cfg.get("forward_private_jump_hint", True)):
            return
        from src.utils.human_escalation import build_telegram_message_link

        from_chat_id = spec.get("from_chat_id")
        chat_username = spec.get("chat_username")
        chat_title = (spec.get("chat_title") or "").strip()
        url = build_telegram_message_link(
            from_chat_id, int(message_id), chat_username
        )

        try:
            from pyrogram.enums import ParseMode
            from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        except Exception:
            ParseMode = None  # type: ignore
            InlineKeyboardButton = None  # type: ignore
            InlineKeyboardMarkup = None  # type: ignore

        if after_forward_ok:
            head = (
                "👆 上一条为<strong>群内用户原话</strong>（转发）。\n"
                "若转发预览无法点进群里，请用下方<strong>按钮</strong>或<strong>链接</strong>直达该条消息。"
            )
        else:
            head = (
                "⚠️ 未能转发群内原消息到私聊，请用下方<strong>按钮</strong>或<strong>链接</strong>"
                "进入群内查看对应话术。"
            )
        parts: List[str] = [head]
        if chat_title:
            parts.append(f"群：{html.escape(chat_title)}")
        parse_mode = ParseMode.HTML if ParseMode else None
        reply_markup = None

        if url:
            parts.append(
                f'直达消息：<a href="{html.escape(url, quote=True)}">打开 #msg{message_id}</a>'
            )
            if InlineKeyboardMarkup and InlineKeyboardButton:
                try:
                    reply_markup = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "📍 打开群内该条消息", url=url
                                )
                            ]
                        ]
                    )
                except Exception:
                    reply_markup = None
            body = "\n".join(parts)
            try:
                await self.client.send_message(
                    chat_id=peer,
                    text=body,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
                self.logger.info(
                    "人工转接: 已向客服 peer=%s 发送私聊定位提示 msg_id=%s",
                    peer,
                    message_id,
                )
            except Exception as e:
                self.logger.warning(
                    "人工转接: 私聊定位提示(HTML)失败 peer=%s: %s，尝试纯文本",
                    peer,
                    e,
                )
                try:
                    await self.client.send_message(
                        chat_id=peer,
                        text=f"打开群内消息：\n{url}",
                    )
                except Exception as e2:
                    self.logger.warning(
                        "人工转接: 私聊定位纯文本也失败 peer=%s: %s", peer, e2
                    )
        else:
            tail = (
                "当前无法生成 t.me / openmessage 直达链接（例如非标准会话 id）。\n"
                "请点按上一条「转发」顶栏进入群，或向管理员索取群邀请链接。"
            )
            try:
                await self.client.send_message(
                    chat_id=peer,
                    text="\n".join(parts + [tail]),
                    parse_mode=parse_mode,
                )
            except Exception as e:
                self.logger.warning(
                    "人工转接: 私聊定位说明(无 URL)失败 peer=%s: %s", peer, e
                )

    async def _maybe_send_voice_reply(
        self,
        original_message,
        reply_text: str,
        *,
        is_peer_voice: bool = False,
    ) -> bool:
        """Try to send a TTS voice note for *reply_text*.

        Returns ``True`` if a voice note was sent (caller should skip text send).
        Returns ``False`` if voice was skipped/failed (caller sends text normally).

        Trigger modes (``telegram.voice_reply.trigger``):
        - ``when_peer_voice`` — only when the incoming message was a voice note
        - ``always``          — every reply
        - ``random``          — with configurable probability
        - ``never``           — effectively disables (same as ``enabled: false``)
        """
        try:
            raw_cfg = self.config.config if hasattr(self.config, "config") else {}
            vr_cfg: Dict[str, Any] = (raw_cfg.get("telegram") or {}).get("voice_reply") or {}
            if not vr_cfg.get("enabled", False):
                self.logger.warning("[voice_reply] skip: enabled=false (section=%s)", "found" if vr_cfg else "missing")
                return False

            trigger = str(vr_cfg.get("trigger", "when_peer_voice")).strip().lower()
            if trigger == "never":
                self.logger.debug("[voice_reply] skip: trigger=never")
                return False
            if trigger == "when_peer_voice" and not is_peer_voice:
                self.logger.debug("[voice_reply] skip: trigger=when_peer_voice but msg is not voice")
                return False
            if trigger == "random":
                prob = float(vr_cfg.get("probability", 0.3) or 0.3)
                if random.random() >= prob:
                    return False

            max_chars = int(vr_cfg.get("max_text_chars", 220) or 220)
            clean_text = (reply_text or "").strip()
            if not clean_text or len(clean_text) > max_chars:
                self.logger.debug(
                    "[voice_reply] skipped: text len=%d max=%d", len(clean_text), max_chars
                )
                return False

            # ── Resolve persona → voice config (3-tier fallback) ──
            from src.ai.persona_voice import resolve_voice_cfg

            persona_id: Optional[str] = None
            try:
                from src.utils.persona_manager import PersonaManager

                pm = PersonaManager.get_instance()
                _acc_pid = (
                    self.account_persona_ids[0]
                    if getattr(self, "account_persona_ids", None)
                    else ""
                )
                pid_dict = pm.get_persona(
                    str(original_message.chat.id), _acc_pid
                )
                persona_id = (
                    pid_dict.get("id")
                    if isinstance(pid_dict, dict)
                    else None
                ) or _acc_pid or None
            except Exception:
                pass

            voice_cfg = resolve_voice_cfg(persona_id, raw_cfg)
            voice_cfg["enabled"] = True

            # ── Synthesize ──
            from src.ai.tts_pipeline import TTSPipeline

            tts = TTSPipeline(voice_cfg)
            timeout_sec = float(vr_cfg.get("timeout_sec", 30) or 30)
            result = await tts.synthesize(clean_text, timeout_sec=timeout_sec)
            if not result.ok:
                self.logger.warning("[voice_reply] TTS failed: %s", result.error)
                return False

            # ── Duration gate ──
            max_sec = float(vr_cfg.get("max_seconds", 60) or 60)
            if result.duration_sec > 0 and result.duration_sec > max_sec:
                self.logger.warning(
                    "[voice_reply] audio %.1fs exceeds max %.1fs, fallback text",
                    result.duration_sec, max_sec,
                )
                try:
                    os.unlink(result.audio_path)
                except Exception:
                    pass
                return False

            dur_int = int(result.duration_sec) if result.duration_sec > 0 else None
            _rt = self._reply_to_message_id_for_send(original_message)

            # ── Send voice ──
            from src.client.voice_sender import send_telegram_voice

            sent = await send_telegram_voice(
                self.client,
                original_message.chat.id,
                result.audio_path,
                duration=dur_int,
                reply_to_message_id=_rt,
            )
            try:
                os.unlink(result.audio_path)
            except Exception:
                pass

            if sent:
                self.logger.info(
                    "[voice_reply] voice sent chat=%s persona=%s dur=%s",
                    original_message.chat.id, persona_id, dur_int,
                )
                if vr_cfg.get("send_text_summary", False):
                    await self._send_reply(original_message, reply_text)
                return True
            return False
        except Exception as ex:
            self.logger.error("[voice_reply] unexpected error: %s", ex)
            return False

    async def _forward_escalation_user_to_agents(self, spec) -> None:
        """
        人工转接触发且群内回复已发出后：把用户在该群的原消息转发到各客服私聊，
        并可选再发一条带内联按钮 + 直达链接的说明（forward_private_jump_hint，默认开）。
        spec: from_chat_id, message_id, targets, chat_username?, chat_title?
        """
        if not spec or not self.client:
            return
        from_chat_id = spec.get("from_chat_id")
        mid = spec.get("message_id")
        targets = spec.get("targets") or []
        if from_chat_id is None or mid is None:
            return
        try:
            mid_int = int(mid)
        except (TypeError, ValueError):
            return
        if mid_int <= 0:
            return
        for t in targets:
            uid = int(t.get("user_id") or 0)
            un = (t.get("username") or "").strip().lstrip("@")
            peer = uid if uid > 0 else (un or None)
            if peer is None:
                continue
            forward_ok = False
            try:
                await self.client.forward_messages(
                    chat_id=peer,
                    from_chat_id=from_chat_id,
                    message_ids=mid_int,
                )
                forward_ok = True
                self.logger.info(
                    "人工转接: 已转发用户原消息 → 客服 peer=%s from_chat=%s msg_id=%s",
                    peer,
                    from_chat_id,
                    mid_int,
                )
            except Exception as e:
                self.logger.warning(
                    "人工转接: 转发至客服 peer=%s 失败: %s", peer, e
                )
            try:
                await self._send_escalation_private_jump_hint(
                    peer,
                    spec,
                    mid_int,
                    after_forward_ok=forward_ok,
                )
            except Exception as ex:
                self.logger.warning(
                    "人工转接: 私聊定位跟进异常 peer=%s: %s", peer, ex
                )

        group_target = spec.get("group_target")
        if isinstance(group_target, dict):
            group_id = (group_target.get("group_id") or "").strip()
            if group_id:
                try:
                    group_peer = int(group_id) if group_id.lstrip("-").isdigit() else group_id
                    await self.client.forward_messages(
                        chat_id=group_peer,
                        from_chat_id=from_chat_id,
                        message_ids=mid_int,
                    )
                    self.logger.info(
                        "人工转接: 已转发用户原消息 → 客服群 group=%s msg_id=%s",
                        group_id, mid_int,
                    )
                except Exception as e:
                    self.logger.warning(
                        "人工转接: 转发至客服群 group=%s 失败: %s", group_id, e
                    )
