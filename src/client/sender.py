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

    # ── 统一发送护栏/节流/记账（A 线文本回复 + 形象照直发共用一套，防图文混发绕过风控） ──

    def _presend_blocked(self) -> bool:
        """发送前统一护栏：G1 全局 Kill-Switch + N 线反封号闸门。

        返回 True=应跳过本次外发（冻结/被闸门拦）；任何异常一律静默放行（绝不因护栏自身报错阻断发送）。
        文本回复与形象照直发共用本判断——避免「文字被拦但图照发」的风控绕过。
        """
        try:
            from src.ops.kill_switch import is_blocked as _ks_blocked
            _ks_on, _ks_scope, _ = _ks_blocked(
                "telegram", getattr(self, "account_id", "default"))
            if _ks_on:
                self.logger.warning(
                    "[kill-switch] 冻结发送，跳过 A 线外发（scope=%s）", _ks_scope)
                return True
        except Exception:
            pass
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
                    return True
        except Exception:
            pass
        return False

    async def _presend_pace(self) -> None:
        """发送间隔节流：距上次外发不足 ``reply.split_send.min_interval_seconds`` 则补足。

        文本与照片共用同一 ``_last_send_wallclock`` 基准——图文混发也排队、不会瞬时双发触发反垃圾。
        异常静默（节流自身出错不阻断发送）。
        """
        try:
            split_cfg = self.config.get("reply", {}).get("split_send", {})
            min_interval = float(split_cfg.get("min_interval_seconds", 0) or 0)
            last = float(getattr(self, "_last_send_wallclock", 0) or 0)
            if min_interval > 0 and last > 0:
                elapsed = time.time() - last
                if elapsed < min_interval:
                    await asyncio.sleep(min_interval - elapsed)
        except Exception:
            pass

    def _postsend_record_count(self) -> None:
        """发送成功后统一记账：刷新墙钟 + 记入与 B 线共用的发送计数器。

        墙钟供下次 ``_presend_pace`` 节流；计数器喂反封号闸门 + 机群健康灯今日外发量（best-effort）。
        """
        self._last_send_wallclock = time.time()
        try:
            _lim = self._shared_send_limiter(
                self.config.config if hasattr(self.config, "config") else {}
            )
            if _lim is not None:
                _lim.record_sent(
                    f"telegram:{getattr(self, 'account_id', 'default')}")
        except Exception:
            pass

    def _postsend_mirror_and_record(self, chat_id: Any, preview: str,
                                    msg_id: Any = "") -> None:
        """发送成功后：出站镜像到坐席台（N4b）+ 记入 contacts 的外发互动（Q3）。

        文本回复与富媒体（照片/语音）共用——富媒体传带标记的 preview（如「[图片] 配文」/「[语音]」），
        让坐席台**看见** AI 发了富媒体、IntimacyEngine 也**计入**一次外发（否则只见入站、mutuality 偏低）。
        两步各自 best-effort，绝不阻断发送。

        ``msg_id``：发送 API 返回的真实 message.id（治本幂等键）。带上它后乐观出站镜像行与
        「自身已发消息被回显」共用同一 platform_msg_id → 主键级精确去重，不再依赖时间窗近似。
        """
        try:
            _emit = getattr(self, "_emit_inbox", None)
            if _emit is not None:
                _emit(chat_id=chat_id, text=preview, direction="out",
                      msg_id=str(msg_id or ""))
                # P4-4：镜像出站即置「已发送」（单勾）；对端读后由 UpdateReadHistoryOutbox
                # 回执升级为「已读」（蓝色双勾）。仅 companion 镜像开启且带真实 id 时生效。
                if getattr(self, "_mirror_inbox", False) and msg_id:
                    from src.integrations.protocol_bridge import report_message_status
                    report_message_status(
                        "telegram", getattr(self, "account_id", "default"),
                        str(chat_id), str(msg_id), "sent")
        except Exception:
            pass
        try:
            from src.utils.companion_context import (
                record_relationship_message as _rec_rel_msg,
            )
            _rec_rel_msg(
                getattr(self, "account_id", "default"),
                chat_id, "out", text_preview=preview or "",
            )
        except Exception:
            pass

    async def _send_reply(self, original_message, reply_text: str, parse_mode=None):
        try:
            # 统一发送前护栏（与 send_photo 共用）：G1 Kill-Switch + N 线反封号闸门。
            if self._presend_blocked():
                return
            # 统一发送间隔节流（与 send_photo 共用同一墙钟，图文混发不瞬时双发）。
            await self._presend_pace()
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
            _sent = await self.client.send_message(**send_kw)
            # 统一发送后记账（与 send_photo 共用）：刷新墙钟 + 记入共用发送计数器
            # （喂反封号闸门 + 机群健康灯今日外发量，best-effort 绝不阻断发送）。
            self._postsend_record_count()
            # N4b 出站镜像（坐席台）+ Q3 contacts 外发互动（mutuality）——与富媒体共用一处。
            # 带回真实 message.id 作幂等键，乐观镜像行与回显共用主键 → 精确去重。
            self._postsend_mirror_and_record(
                original_message.chat.id, _out_text,
                msg_id=getattr(_sent, "id", "") or "")
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

    async def _send_text_guarded(self, chat_id: int, text: str):
        """A 线外发文本核心：过发送前护栏 + 节流 + 记账，返回 ``(ok, sent_message)``。

        - ``ok``：是否成功送出（过护栏且未抛；与旧 ``send_message`` 的 bool 语义一致）。
        - ``sent_message``：底层 ``client.send_message`` 的返回（真实 pyrogram 为 ``Message``，
          可取 ``.id``；测试桩/无返回时为 None）。

        **不**做出站镜像（避免与编排器中心化收件箱回写重复镜像）。
        """
        try:
            # 统一发送前护栏：G1 Kill-Switch + N 线反封号闸门（与 _send_reply/send_photo 共用）
            if self._presend_blocked():
                return False, None
            await self._presend_pace()
            if not self.client:
                self.logger.error("客户端未初始化")
                return False, None
            _sent = await self.client.send_message(chat_id, text)
            self._postsend_record_count()
            self.logger.info("已发送消息到 %s: %s...", chat_id, text[:50])
            return True, _sent
        except Exception as e:
            self.logger.error("发送消息失败: %s", e)
            # G2 封号信号自动急停：风控错误 → 分级处置（退避/暂停/封禁），best-effort
            try:
                from src.ops.ban_signal import handle_send_exception as _g2
                _g2("telegram", getattr(self, "account_id", "default"), e)
            except Exception:
                pass
            return False, None

    async def send_message(self, chat_id: int, text: str) -> bool:
        """A 线主动外发文本（主动问候/唤醒/关怀/编排器受管 worker 都经此）。

        Stage M：此前是裸 Pyrogram 调用，绕过 Kill-Switch/反封号/节流——成为旁路风控缺口
        （主动问候经 CompanionWorker.send→本方法 直发）。现统一走与 ``_send_reply`` 同一套发送前
        护栏 + 节流 + 记账。**不**做出站镜像（避免与编排器中心化收件箱回写重复镜像）。
        """
        ok, _ = await self._send_text_guarded(chat_id, text)
        return ok

    async def send_message_return_id(self, chat_id: int, text: str):
        """同 ``send_message``，但回传 ``(ok, msg_id)``——``msg_id`` 为发出的**真实**
        ``message.id``（无则空串）。

        P4-4：供 companion worker 把已读回执（``UpdateReadHistoryOutbox``）精确绑定到
        对应出站消息行；旧 ``send_message`` 只回 bool、丢弃了 id，导致 companion 手动发送的
        消息无法显示双勾。best-effort：失败/被拦 → ``(False, "")``。
        """
        ok, _sent = await self._send_text_guarded(chat_id, text)
        return ok, (str(getattr(_sent, "id", "") or "") if ok else "")

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
            # 统一发送前护栏（与文本回复共用）：冻结/被反封号闸门拦 → 不发，避免图绕过风控。
            if self._presend_blocked():
                self.logger.info("照片发送被发送前护栏拦截，跳过（chat=%s）", chat_id)
                return False
            # 统一节流：与文本共用墙钟，图文混发也排队（不瞬时双发触发反垃圾）。
            await self._presend_pace()
            _sent = await self.client.send_photo(chat_id, photo_path, caption=caption or "")
            # 统一记账：刷新墙钟 + 记入共用计数器（照片也计入今日外发量，反封号不漏算）。
            self._postsend_record_count()
            # 出站镜像 + contacts 记账：坐席台看见「AI 发了图」、亲密度计入这次外发。
            _cap = (caption or "").strip()
            self._postsend_mirror_and_record(
                chat_id, f"[图片] {_cap}".strip() if _cap else "[图片]",
                msg_id=getattr(_sent, "id", "") or "")
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
        peer_audio_emotion: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Try to send a TTS voice note for *reply_text*.

        Returns ``True`` if a voice note was sent (caller should skip text send).
        Returns ``False`` if voice was skipped/failed (caller sends text normally).

        Trigger modes (``telegram.voice_reply.trigger``):
        - ``when_peer_voice`` — only when the incoming message was a voice note
        - ``always``          — every reply
        - ``random``          — with configurable probability
        - ``smart``           — context-aware fitness scoring (shared ai.voice_fitness)
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
            if trigger == "smart":
                # 与 System Z autosend 同源的上下文感知评分（消除重复决策逻辑）。原生 TG
                # 路径暂只喂「回复情绪 + 对等」信号（频率/客户情绪可后续接入）；内容/长度
                # 硬否决与 autosend 完全一致。低分/不达标 → 回落文本。
                from src.ai.voice_fitness import voice_fitness
                _smart = vr_cfg.get("smart") if isinstance(vr_cfg.get("smart"), dict) else {}
                _merged = {
                    "max_chars": int(vr_cfg.get("max_text_chars", 220) or 220), **_smart}
                _dec = voice_fitness(
                    (reply_text or "").strip(),
                    peer_sent_voice=is_peer_voice, cfg=_merged)
                if not _dec.send_voice:
                    self.logger.debug(
                        "[voice_reply] skip: smart fitness=%s (%s)", _dec.score, _dec.reason)
                    return False

            max_chars = int(vr_cfg.get("max_text_chars", 220) or 220)
            clean_text = (reply_text or "").strip()
            if not clean_text or len(clean_text) > max_chars:
                self.logger.debug(
                    "[voice_reply] skipped: text len=%d max=%d", len(clean_text), max_chars
                )
                return False

            # 统一发送前护栏（与文本/照片共用）：冻结/被反封号闸门拦 → 不出语音、也不白跑 TTS。
            # 返回 False → 调用方回退文本 _send_reply，文本同样会被护栏拦 → 冻结期彻底静默。
            if self._presend_blocked():
                self.logger.info("[voice_reply] skip: 发送前护栏拦截（kill-switch/反封号闸门）")
                return False

            # P3：端用户身份（私聊 chat.id 即对端 user_id）→ 会员档分层路由 TTS 后端
            # （VIP→旗舰，免费→降级省成本）。monetization 未就绪 → tier=None → 不路由。
            try:
                _contact_key = str(original_message.chat.id)
            except Exception:
                _contact_key = None
            _acc_pid = (
                self.account_persona_ids[0]
                if getattr(self, "account_persona_ids", None)
                else ""
            )
            from src.ai.persona_voice import resolve_effective_voice_context
            voice_ctx = resolve_effective_voice_context(
                raw_cfg, chat_key=_contact_key, account_persona_id=_acc_pid,
                contact_key=_contact_key, platform="telegram",
                account_id=getattr(self, "account_id", None), text=clean_text,
                peer_audio_emotion=peer_audio_emotion)
            voice_cfg = voice_ctx.get("voice_cfg") or {}
            voice_cfg["enabled"] = True

            # ── Synthesize ──
            from src.ai.tts_pipeline import TTSPipeline

            tts = TTSPipeline(voice_cfg)
            timeout_sec = float(vr_cfg.get("timeout_sec", 30) or 30)
            result = await tts.synthesize(
                clean_text, timeout_sec=timeout_sec,
                emotion=voice_ctx.get("emotion"))
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

            # 统一节流：与文本/照片共用墙钟，语音不与前一条外发瞬时双发。
            await self._presend_pace()
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
                    original_message.chat.id,
                    voice_ctx.get("persona_id") or "",
                    dur_int,
                )
                # 统一记账：语音也刷墙钟 + 计入今日外发量（反封号/健康灯不漏算语音条）。
                self._postsend_record_count()
                if vr_cfg.get("send_text_summary", False):
                    # 文本摘要走 _send_reply→自带护栏/节流/计数/镜像/记账
                    # （语音+文本=确有 2 条外发，各记一次属正确口径）。
                    await self._send_reply(original_message, reply_text)
                else:
                    # 仅发语音时也要镜像/记账，否则坐席台/亲密度看不到这次外发。
                    self._postsend_mirror_and_record(
                        original_message.chat.id, "[语音]")
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
