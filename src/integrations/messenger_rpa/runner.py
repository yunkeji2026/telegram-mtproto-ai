"""Messenger RPA：单次 run_once 完整流程。

流程（黄金路径，已在 d113 PoC 验证）：
  1. 设备解析（adb_serial 或自动选）
  2. 屏幕尺寸缓存
  3. foreground Messenger（am start StartScreenActivity）
  4. 截图当前屏 → guard_navigator 检测/闪避 modal
  5. 截图 Inbox → inbox_scanner 扫未读列表
  6. 选第一条未读 → 计算坐标 → input tap 进入会话
  7. 截图 Thread view → guard_navigator 闪避 modal → chat_reader 读对方最后一条
  8. 去重检查（fingerprint）→ 已回过则跳过
  9. SkillManager.process_message 生成回复
  10. 回到输入框：input tap → AdbKeyboard 输入 → input tap 发送
  11. 写 state_store；记录 run

对外只暴露 run_once() 一个协程。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.integrations.line_rpa import adb_helpers as adb
from src.integrations.line_rpa.human_pacing import (
    PacingConfig,
    jitter_ms,
    typing_duration_sec,
)
from src.integrations.messenger_rpa import coords as cc
from src.integrations.messenger_rpa.bloks_navigator import (
    ACTION_NEED_HUMAN,
    ACTION_NONE,
    ACTION_PRESS_BACK,
    ACTION_TAP_CLOSE_X,
    ACTION_TAP_OK,
    detect_guard_screen,
)
from src.integrations.messenger_rpa.chat_reader import (
    PeerMessage,
    fingerprint,
    read_peer_message_vision,
)
from src.integrations.messenger_rpa.combined_vision import (
    analyze_inbox_combined,
    analyze_thread_combined,
    analyze_unread_only,
    is_outbound_or_draft_preview,
)
from src.integrations.messenger_rpa.row_resolver import resolve_row_by_name
from src.integrations.messenger_rpa.inbox_scanner import (
    UnreadChat,
    scan_inbox_vision,
)
from src.integrations.messenger_rpa.lead_qualification import (
    LeadQualificationEngine,
)
from src.integrations.messenger_rpa.persona_runtime import (
    ConversationStateMachine,
    detect_customer_language,
    flatten_persona_facts,
    infer_customer_type,
)
from src.integrations.messenger_rpa.state_store import MessengerRpaStateStore
from src.integrations.messenger_rpa import escalation as _escalation

logger = logging.getLogger(__name__)

# screencap 成功时至少应有 PNG 头 + 少量 chunk；过小或是 ASCII 错误串一律视为失败
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_MIN_PNG_BYTES = 200


def _messenger_png_screencap_ok(png_bytes: bytes) -> bool:
    return (
        bool(png_bytes)
        and len(png_bytes) >= _MIN_PNG_BYTES
        and png_bytes[:8] == _PNG_MAGIC
    )


# ── Messenger 相关常量 ──────────────────────────────
MESSENGER_PKG = "com.facebook.orca"
MESSENGER_LAUNCH_ACTIVITY = "com.facebook.orca/com.facebook.orca.auth.StartScreenActivity"
MESSENGER_MAIN_ALIAS = "com.facebook.orca/.auth.StartScreenActivity"


def _detect_peer_lang(text: str, ai_client: Any = None) -> str:
    """语种判定。优先用 ai_client._detect_message_language（16+ 语言），
    没传 ai_client 时退回到 zh/en/unknown 的极简实现。

    返回任意 _LANG_NAMES 里的语言码（'zh' / 'en' / 'ja' / 'ko' / 'ar_ur' / ...）
    或 'unknown'。
    """
    if not text:
        return "unknown"
    if ai_client is not None and hasattr(ai_client, "_detect_message_language"):
        try:
            return ai_client._detect_message_language(text) or "unknown"
        except Exception:
            pass
    # fallback 极简（仅当 ai_client 未注入时使用）
    cjk = 0
    ascii_letters = 0
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            cjk += 1
        elif ch.isascii() and ch.isalpha():
            ascii_letters += 1
    if cjk >= 1 and cjk * 3 >= ascii_letters:
        return "zh"
    if ascii_letters >= 2:
        return "en"
    return "unknown"


def _compact_for_self_overlap(text: str) -> str:
    """Keep visible letters/digits only for self-message overlap checks."""
    return "".join(ch.lower() for ch in (text or "") if ch.isalnum())


def _self_reply_overlap_ratio(last_reply: str, peer_text: str) -> float:
    """Estimate whether Vision re-read our own last reply as a peer message.

    Word splitting works for English, but Chinese/Japanese/Korean messages often
    have no spaces.  Use compact string containment plus n-gram overlap so CJK
    self messages are caught without making unrelated short replies collide.

    P14 (2026-05-04): 加 substring echo 检测——peer 含 last_reply 任意 5+
    字符连续片段就视为 echo 强信号。实测 vision 在 fast_path 把 bot 历史
    多条 reply 合并/串联读成一条 "peer message" 时，整体字符级 LCS 不高，
    但 peer 文本里**必然含 bot 最近常用短语**（如 "また明日ね"、"今日は"
    引导句、emoji signature）。任何 ≥5 字 substring 命中即 1.0，让 0.7 阈值
    能稳定触发。
    """
    lr = _compact_for_self_overlap(last_reply)
    pc = _compact_for_self_overlap(peer_text)
    if len(lr) < 12 or len(pc) < 12:
        return 0.0
    if pc in lr or lr in pc:
        return 1.0

    # ★ Substring echo：peer 含 last_reply 的任意 N+ 字符连续片段 → 强信号
    #   bot signature phrase 命中，足以挡住 vision 把 self 串联成 peer 的 case
    #
    # P32 (2026-05-05) 紧急修复：找最长公共子串，要求覆盖率 ≥ 60% 才视为 echo。
    # 原 N=5 任意命中即 1.0 太宽——"今日は特に"5 字、"予定もなく"5 字等
    # 日常话术 signature peer 真消息也常用，导致大量 false positive
    # 死循环（夜间 15+ 次野末/Maipon Senda 死锁）。
    #
    # 修复逻辑：
    #   1. N=8 起步（8 字才代表 bot 个性化短语）
    #   2. 找最长公共连续子串
    #   3. 公共子串占 peer 长度 ≥ 60% 才视为 echo
    # 实测 case："今日は特に予定もなく、" 11 字 / peer 28 字 = 39% < 60%
    #          → 不算 echo，让 peer 真消息（quote bot 头部 + 自己后续）通过
    #          但 vision 真把整段 self 串联（phrase ≈ peer length）→ ratio
    #          必然 ≥ 60% → 仍能识别 vision 误读
    # P32-tune (2026-05-05)：60% → 75% 进一步严格化。
    # 60% 阈值在 peer "今日は特に予定もなく、のんびり過ごしています。あなたは？"
    # 这种 peer 真消息（quote bot 头部 + 自己后续）场景仍触发 false positive。
    # 75% 让 phrase 必须占 peer 大部分才视为 echo——peer 真消息含
    # 自己内容 ≥ 25% 就放行。
    _MIN_PHRASE = 8
    _MIN_ECHO_PEER_COVERAGE = 0.75
    if len(lr) >= _MIN_PHRASE and len(pc) >= _MIN_PHRASE:
        # 找最长公共子串（O(N*M) but N,M 通常 < 100）
        _longest = 0
        for i in range(len(lr) - _MIN_PHRASE + 1):
            # 二分扩展 phrase 长度找最长命中
            _max_len = min(len(lr) - i, len(pc))
            for size in range(_MIN_PHRASE, _max_len + 1):
                phrase = lr[i:i + size]
                if phrase in pc:
                    if size > _longest:
                        _longest = size
                else:
                    break  # 更长的 phrase 必然也不在 pc
        if _longest > 0 and (
            _longest / max(len(pc), 1) >= _MIN_ECHO_PEER_COVERAGE
        ):
            return 1.0

    word_ratio = 0.0
    words_lr = set(re.findall(r"[a-z0-9]{2,}", (last_reply or "").lower()))
    words_pc = set(re.findall(r"[a-z0-9]{2,}", (peer_text or "").lower()))
    if len(words_pc) >= 3 and words_lr:
        word_ratio = len(words_lr & words_pc) / len(words_pc)

    n = 3 if len(pc) >= 18 else 2
    lr_grams = {lr[i:i + n] for i in range(0, max(0, len(lr) - n + 1))}
    pc_grams = {pc[i:i + n] for i in range(0, max(0, len(pc) - n + 1))}
    ngram_ratio = 0.0
    if lr_grams and pc_grams:
        ngram_ratio = len(lr_grams & pc_grams) / len(pc_grams)

    # 字符级 LCS 相似度（CJK-friendly 兜底）
    try:
        import difflib
        sm_ratio = difflib.SequenceMatcher(None, lr, pc).ratio()
    except Exception:
        sm_ratio = 0.0

    return max(word_ratio, ngram_ratio, sm_ratio)


_SELF_MEDIA_OCR_MARKERS = (
    "main profile",
    "display name",
    "status message",
    "my qr code",
    "friends who see this profile",
    "phone number",
    "line id",
    "allow others to add me by id",
    "background music",
    "not set",
    "copy",
)


def _self_skip_norm_key(name: str) -> str:
    """Normalize chat name for self-skip cooldown & wrong-chat matching.

    Vision OCR often returns different readings for the same Japanese name
    (e.g. 神沢颯人, 神沢風人 — 颯 vs 風 are visually similar kanji).
    CJK: keep first 2 chars (surname) for maximum OCR tolerance.
    ASCII: keep first 8 chars for disambiguation.
    """
    s = re.sub(r"\s+", "", (name or "").strip())
    ascii_ratio = sum(1 for c in s if ord(c) < 128) / max(len(s), 1)
    keep = 8 if ascii_ratio > 0.5 else 2
    return s[:keep].casefold()


class _PersistentSelfSkipDict(dict):
    """P0-4: dict 子类，写入即同步落库。

    存储语义不变：key=norm_key, value=monotonic_until（ time.monotonic() 域）。
    内存读路径完全透明（dict.get/__getitem__ 等沿用基类）。仅在 __setitem__ /
    __delitem__ / pop 时把"剩余秒数"换算成 epoch 时间写入 state_store，从而
    让 runaway_guard 等触发的 cooldown 在进程重启后仍然生效。

    epoch ↔ monotonic 转换：
      Δ = mono_until - time.monotonic()  # 剩余秒数
      epoch_until = time.time() + Δ
    转换误差 < 调度延迟（毫秒级），对 30 分钟级冷却无影响。

    持久化失败（例如 DB 锁）静默忽略 — 内存仍然是真理（fail-open，不阻塞主流程）。
    """

    def __init__(self, store: Any, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._store = store

    def __setitem__(self, key: str, value: float) -> None:
        super().__setitem__(key, value)
        try:
            mono_until = float(value)
        except (TypeError, ValueError):
            return
        try:
            delta = mono_until - time.monotonic()
            if delta <= 0:
                return  # 已过期，懒删（下次 get 时 caller 会忽略）
            epoch_until = time.time() + delta
            if self._store is not None:
                self._store.set_self_skip(str(key), epoch_until)
        except Exception:
            logger.debug("persist self_skip failed", exc_info=True)

    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)
        try:
            if self._store is not None:
                self._store.clear_self_skip(str(key))
        except Exception:
            pass

    def pop(self, key: str, *args: Any) -> Any:
        had = key in self
        out = super().pop(key, *args)
        if had:
            try:
                if self._store is not None:
                    self._store.clear_self_skip(str(key))
            except Exception:
                pass
        return out


def _looks_like_self_media_ocr(peer_msg: Optional[PeerMessage]) -> bool:
    """Detect OCR text read from our own sent screenshot/media bubble.

    The XML guard can expose ``You: <private icon>`` for a self-sent image even
    after a real peer text is visible.  Only suppress replies when Vision text
    itself looks like screenshot UI chrome, not natural customer language.
    """
    if peer_msg is None:
        return False
    kind = str(getattr(peer_msg, "kind", "") or "").lower()
    if kind in ("image", "sticker", "voice", "file"):
        return True
    text = " ".join(
        str(x or "") for x in (
            getattr(peer_msg, "content", ""),
            getattr(peer_msg, "desc", ""),
            getattr(peer_msg, "raw", ""),
        )
    )
    low = re.sub(r"\s+", " ", text).strip().lower()
    if not low:
        return False
    hits = sum(1 for marker in _SELF_MEDIA_OCR_MARKERS if marker in low)
    if hits >= 1 and ("line" in low or "profile" in low or "not set" in low):
        return True
    if re.search(r"\+\s?81[\d\s-]{8,}", low) and ("phone" in low or "line" in low):
        return True
    return False


def pick_unread_row_for_peer_name(
    unread: List[UnreadChat],
    chat_name: str,
    inbox_ranking: Optional[List[Dict[str, Any]]],
    *,
    min_preview_substr_len: int = 4,
    hint_out: Optional[List[str]] = None,
) -> Optional[UnreadChat]:
    """纯函数：在未读列表 / inbox_ranking 里找与 ``chat_name`` 对应的行。

    顺序：① 顶栏名匹配 ② 预览子串唯一命中（Vision 偶发把联系人名写进 preview）
    ③ inbox_ranking 里按名匹配 ④ ranking 里预览唯一命中。
    """
    from src.integrations.messenger_rpa import thread_actions as _ta

    want = (chat_name or "").strip()
    if not want:
        return None
    for c in unread:
        if _ta.peer_names_match_inbox_pick(c.name, want):
            return c
    mpl = int(min_preview_substr_len or 0)
    if mpl > 0 and len(want) >= mpl:
        pv_hits = [
            c for c in unread
            if want.lower() in (c.preview or "").lower()
        ]
        if len(pv_hits) == 1:
            if hint_out is not None:
                hint_out.append(
                    "send_to_chat_name:match_preview_substring_unique",
                )
            return pv_hits[0]
    rk = inbox_ranking or []
    for entry in rk:
        name = str(entry.get("name") or "").strip()
        if not name or not _ta.peer_names_match_inbox_pick(name, want):
            continue
        try:
            ri = int(entry.get("row_index", 0))
        except (TypeError, ValueError):
            ri = 0
        ri = max(0, min(ri, 6))
        if hint_out is not None:
            hint_out.append("send_to_chat_name:match_from_inbox_ranking")
        return UnreadChat(
            name=name,
            preview=str(entry.get("preview") or "")[:500],
            time="",
            row_index=ri,
            y_percent=0.0,
            quality_hint=str(entry.get("hint") or "unsure"),
            score=float(entry.get("score") or 0.0),
            skip_inbox_tap=False,
        )
    if mpl > 0 and len(want) >= mpl:
        rk_prev = [
            e for e in rk
            if want.lower() in str(e.get("preview") or "").lower()
        ]
        if len(rk_prev) == 1:
            e = rk_prev[0]
            name = str(e.get("name") or "").strip() or want
            try:
                ri = int(e.get("row_index", 0))
            except (TypeError, ValueError):
                ri = 0
            ri = max(0, min(ri, 6))
            if hint_out is not None:
                hint_out.append(
                    "send_to_chat_name:match_ranking_preview_unique",
                )
            return UnreadChat(
                name=name,
                preview=str(e.get("preview") or "")[:500],
                time="",
                row_index=ri,
                y_percent=0.0,
                quality_hint=str(e.get("hint") or "unsure"),
                score=float(e.get("score") or 0.0),
                skip_inbox_tap=False,
            )
    return None


class MessengerRpaRunner:
    """单次 run_once 协程；可被 service 反复触发。"""

    def __init__(
        self,
        *,
        config_manager: Any,
        skill_manager: Any,
        messenger_rpa_cfg: Dict[str, Any],
        state_store: MessengerRpaStateStore,
    ) -> None:
        self._cm = config_manager
        self._sm = skill_manager
        self._cfg = dict(messenger_rpa_cfg or {})
        self._state = state_store
        self._base_cfg = dict(messenger_rpa_cfg or {})
        self._screen_wh_cache: Dict[str, Tuple[int, int]] = {}
        # ★ 校准缓存：(serial, w, h) -> CalibratedCoords | None
        self._calib_cache: Dict[Tuple[str, int, int], Any] = {}
        # ★ chat 入口缓存：(serial, chat_name) -> (tap_x, tap_y, ts, source)
        # 一旦 search/inbox 路径成功打开过某 chat，记下点击坐标。下次发同
        # 个人时（TTL 内）直接 tap+verify_thread_title，跳过 search 完整环节
        # （省 ≥10s）。verify 失败即失效，fallthrough 到正常路径。
        self._chat_entry_cache: Dict[
            Tuple[str, str], Tuple[int, int, float, str]
        ] = {}
        # ★ P2-D: per-chat 搜索失败熔断
        # {chat_name: {"fails": N, "cooldown_until": ts, "last_fail_ts": ts}}
        # 连续 N 次搜索失败 → 冷却 M 分钟。冷却内的搜索请求直接短路返回 None，
        # 由上游 fallback（inbox scan / cache）处理。避免对同一个系统事件/
        # 已删好友反复 4 轮 12-16s 搜索，单 chat 卡死拖累整账号。
        # 进程重启后状态清空（认为重启 = 假定健康）。
        self._search_failure_state: Dict[str, Dict[str, float]] = {}
        # ★ 短期冷却：进入 thread 后发现是 self-sent 的 chat，避免反复尝试
        # key = _self_skip_norm_key(name), value = monotonic time until which to skip
        # P0-4: 用 _PersistentSelfSkipDict 替代普通 dict —— 写入即落库，重启回填，
        # 让 runaway_guard 30 分钟熔断 cooldown 跨重启生效（修 R4 根因）。
        self._self_skip_until: Dict[str, float] = _PersistentSelfSkipDict(
            self._state
        )
        try:
            now_mono = time.monotonic()
            now_epoch = time.time()
            persisted = self._state.load_active_self_skips() or {}
            restored = 0
            for nk, (epoch_until, _reason) in persisted.items():
                delta = float(epoch_until) - now_epoch
                if delta > 0:
                    # 直接写基类 __setitem__ 避免回写 DB（已经是从 DB 来的）
                    dict.__setitem__(
                        self._self_skip_until, str(nk), now_mono + delta,
                    )
                    restored += 1
            if restored:
                logger.info(
                    "[messenger_rpa] P0-4 restored %d active self_skip "
                    "cooldowns from DB", restored,
                )
        except Exception:
            logger.debug("self_skip 回填失败", exc_info=True)

        # ── thread title vision cache（per-target_name, TTL 默认 1800s）─────
        # MIUI 上 uiautomator dump 被 lowmemkill 时，每 cycle 都得调 vision
        # 拿 title strip（5–15s）。同一 chat 的 peer 名不会变，进同一 thread
        # 没必要再走一次 OCR。命中后直接返 cached title，省整段 vision。
        #
        # TTL 选 1800s (30 min)：实测真实 cycle 间隔 3.5–5 min（120s post-send
        # cooldown + peer 思考回复），30s TTL 命中率 0%；1800s 覆盖 99% 活跃
        # 对话节奏。peer 改 nickname 罕见（月级），且 vision 返新名字会触发
        # wrong_chat_rollback 自动清掉这个 entry，所以长 TTL 安全。
        self._title_vision_cache: Dict[Tuple[str, str], Tuple[str, float]] = {}
        self._title_vision_cache_ttl_sec = float(
            self._cfg.get("thread_title_vision_cache_ttl_sec", 1800.0) or 0.0
        )

        # ── pre_foreground title cache（per-serial slot）──────────
        # smart_current_thread 路径每 cycle 都问"当前 foreground 是哪个 chat？"
        # XML 通常被 lowmemkill 杀，vision 又每次 5–15s。但只要我们没离开
        # thread（_exit_thread），上次 vision 读到的 title 就还有效。
        # 用 serial 单 slot 缓存：进 thread 后写、_exit_thread 时主动清。
        self._foreground_title_cache: Dict[str, Tuple[str, float]] = {}

        # ── P15: per-chat 最近 N 条 self reply（in-memory，重启丢失）──
        # P14 self_overlap 只对比 chat_state.last_reply 一条，但 vision 偶发
        # 串联的是更早的 self message（不在 last_reply 里）。扩展到 last 3
        # replies，对每条计算 ratio 取 max——更广覆盖 vision 串联误读场景。
        self._recent_replies_per_chat: Dict[str, List[str]] = {}
        self._recent_replies_max = 3

        # ── P16 (2026-05-04) self_message_skip 反空转三件套 ──
        # 现象：vision 反复把同一条已发自发消息识为新 peer 消息，单 chat 14:29~14:50
        # 触发 6+ 次 self_message_skip，每次浪费 100~200s。三层防御：
        #   D 层：peer_msg.content 指纹去重，命中"上次已 skip 文本"立即短路
        #   B 层：bubble_detector 信号与 overlap 联合判定（bubble=self+overlap≥0.7
        #         即 100% vision 误读，无需再等 strict_window）
        #   C 层：同 chat 连续 self_message_skip ≥ N 次 → chat 级长冷却覆盖
        self._skipped_peer_text_per_chat: Dict[str, "deque[str]"] = {}
        self._self_overlap_skip_streak: Dict[str, int] = {}
        self._chat_overlap_skip_until: Dict[str, float] = {}
        # P29 (2026-05-05) D 层指纹 TTL（修死循环 false positive）：
        # 现象：bot 发"今日は特に予定もなく..."→peer 真回复"今日は特に予定もなく、
        # のんびり過ごしてるよ"，"今日は特に"短公共子串让 _self_reply_overlap_ratio
        # 算 1.00 → 第一次误判 self → 入指纹 → 后续 vision 每次读到（OCR 漂移
        # 也算 1.00）→ D 层永久短路 → bot 永远不回。
        # 修复：每个 skipped 文本带时间戳，过 N 秒（默认 300s）失效，让 vision
        # 重新判定有机会通过。已观察过 10+ 次同条文本短路，必须有 TTL。
        self._skipped_peer_text_ts_per_chat: Dict[str, Dict[str, float]] = {}

        # ── P17 (2026-05-04) thread_combined 截屏 hash 缓存 ──
        # 现象：vision 偶发对同一截屏在不同 run 内被反复调用（sticky_thread
        # fast_path / 重试），同截屏输出客观一致。命中缓存可省 ~3-5s 延迟 +
        # 整次 vision API token。LRU 16 条进程级。
        # 安全约束：仅缓存 risk.hit=False 的结果，避免 risk 链路被旁路。
        from collections import OrderedDict as _OD
        self._thread_combined_cache: "OrderedDict[str, Any]" = _OD()
        self._thread_combined_cache_max = 16

        # ── P26 (2026-05-04) auto-sticky chat ──
        # 现状：sticky_thread.target_chat_names 仅 1 个静态白名单（Victor Zan），
        # 其他 chat 每轮都走完整 inbox vision (50s+) → 反应慢。
        # 优化：发送成功后该 chat 自动 sticky N 秒（默认 300s = 5 分钟），让它
        # 享受 fast_path（current_thread_fast_path / sticky_idle 短间隔 1.5s
        # poll）。peer 在 5 分钟内继续聊 → bot 几秒内响应；过期自动退出。
        self._auto_sticky_until: Dict[str, float] = {}

        # ── P25 (2026-05-04) inbox_combined ROI hash 缓存 ──
        # phase_duration 数据：inbox_vision 平均 57s/次，是最大瓶颈。
        # 当 peer 没发新消息时 inbox 列表稳定（5 分钟级时间戳粗粒度），ROI
        # hash 应能命中。命中省 ~50s vision API 调用 + 整轮等待。
        # ROI = [23.75%, 92.5%]，跳过 stories（动画头像）+ 底部 nav。
        # LRU 4 条（inbox 切换不频繁）。
        self._inbox_combined_cache: "OrderedDict[str, Any]" = _OD()
        # P25-v2-fix：双键写入（text + ROI 各占 1 entry），LRU 容量 4 → 8
        # 让能存 4 个完整 chat 列表 snapshot
        self._inbox_combined_cache_max = 8

        # ── P21 (2026-05-04) 长冷却硬上限熔断 ──
        # 单 chat 在滚动窗口（默认 24h）内累计触发 ≥ N 次（默认 3）long_cooldown
        # → 自动加入 state_store.skipped_chats 永久跳过列表 + ERROR 告警。
        # 防御场景：vision 对某个 chat 系统性持续幻觉，反复触发 600s 长冷却也
        # 救不回（cycle 永远空转）。入黑后由人工 review + 调用 remove_skipped_chat
        # 解禁。
        self._chat_long_cooldown_history: Dict[str, List[float]] = {}

        # ── P24 (2026-05-04) window 上限保护 ──
        # 防御 P19 误拦事故重演：bubble_pre_vision_recent_window_sec 配置过大
        # 时（>300s）会让 peer 久不回的真消息被误判 self 拦截，必须 clamp。
        # warning 只打一次防止日志刷屏。
        self._pv_window_clamp_warned: bool = False

        # ── P18 (2026-05-04) device_unhealthy 反空转 ──
        # 现象：USB 设备物理掉线时，每次 _resolve_serial 都调 ensure_device_ready
        # 跑 30s 重连—但 USB 模式下 _adb_connect 是 no-op，重连无效，30s 全浪费。
        # 1.5h 采样到 12 次 device_unhealthy = 360s+ 空转。
        # 防御：连续 N 次 unhealthy 后进入 backoff 短路，不再调 ensure_device_ready。
        self._device_unhealthy_streak: Dict[str, int] = {}
        self._device_unhealthy_skip_until: Dict[str, float] = {}

        # P2 (2026-05-04) sticker reply：记录每 chat 的 sticker 发送时间戳列表，
        # 用于 cooldown / 24h cap 计算。即使 dry_run 也记录决策模拟次数。
        self._sticker_send_history: Dict[str, List[float]] = {}
        # P2++F3 (2026-05-04)：记录每 chat 最近选过的 sticker 文件路径（按时间）
        # _pick_sticker_file 时排除这些，避免连发同一张感觉机械。
        self._sticker_recent_per_chat: Dict[str, List[str]] = {}
        self._sticker_recent_max = 3

        # 调试截图目录
        self._debug_dir = Path(
            self._cfg.get("debug_screenshot_dir") or "tmp_messenger_rpa"
        ).resolve()
        try:
            self._debug_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.debug("debug 截图目录创建失败，将禁用保存", exc_info=True)

        self._pacing = PacingConfig.from_dict(self._cfg.get("human_pacing") or {})
        self._max_inbox_per_run = int(self._cfg.get("max_inbox_per_run", 1) or 1)
        self._reply_mode = (self._cfg.get("reply_mode") or "auto").lower().strip()
        self._chat_key_prefix = self._cfg.get("chat_key_prefix") or "messenger_rpa"
        # 多用户机器（MIUI XSpace、Android Multi-user）需要明确 user id
        # 否则 am start 落到上次 active user，inbox 全错位
        self._adb_user_id: Optional[int] = self._cfg.get("adb_user_id")
        if self._adb_user_id is not None:
            try:
                self._adb_user_id = int(self._adb_user_id)
            except (TypeError, ValueError):
                self._adb_user_id = None
        # 合并 vision：单次 prompt 同时拿 guard+content，约 4 次 → 2 次，时延降一半
        self._use_combined_vision = bool(
            self._cfg.get("use_combined_vision", True)
        )
        # ★ P2-4：telegram_client 由 service.bind_telegram_client 后置注入
        self._telegram_client: Optional[Any] = None
        # W4-Runner：ContactHooks 由 main.py 在 contacts 子系统 bootstrap 后注入；
        # None 时所有 contact hook 调用静默跳过，runner 正常跑
        self._contact_hooks: Optional[Any] = None
        # Phase 1：用户画像 extractor，由 service 注入；None 时不抽画像
        self._portrait_extractor: Optional[Any] = None
        self._lead_qualifier = LeadQualificationEngine(
            self._cfg.get("lead_qualification") or {}
        )
        self._conversation_fsm = ConversationStateMachine()
        # ★ 跨账号协调器：共享画像 + 同用户聊天互斥；由 service 注入
        self._coordinator: Optional[Any] = None

        # ★ W2-D1.5：guardrail engine（companion_mode 默认开；可在 config 微调）
        try:
            from src.integrations.safety import GuardrailEngine
            _grd_cfg = self._cfg.get("guardrail") or {}
            # companion_mode=true 时默认 enabled；非 companion 默认关
            if "enabled" not in _grd_cfg:
                _grd_cfg = dict(_grd_cfg)
                _grd_cfg["enabled"] = bool(self._cfg.get("companion_mode", False))
            self._guardrail = GuardrailEngine(_grd_cfg)
        except Exception:
            logger.warning("guardrail 初始化失败，禁用", exc_info=True)
            self._guardrail = None

        # ★ W2-D3.4 + D4.8：peer typing detector（vision 调用，懒加载 vision_client）
        # 真实初始化推迟到首次 detect 调用（避免 startup 时 vision config 未就绪）
        try:
            from src.integrations.messenger_rpa.peer_typing import (
                build_peer_typing_detector,
            )
            pt_cfg = self._cfg.get("peer_typing") or {}
            vision_for_pt = None
            if bool(pt_cfg.get("enabled", False)) and \
                    str(pt_cfg.get("backend", "")).lower() == "vision":
                try:
                    from src.vision_client import VisionClient
                    vc = VisionClient(config=self._vision_cfg() or {})
                    if vc.initialize():
                        vision_for_pt = vc
                    else:
                        logger.warning(
                            "peer_typing: vision_client init 失败，回退 Null",
                        )
                except Exception:
                    logger.debug("peer_typing vision init 异常", exc_info=True)
            self._peer_typing = build_peer_typing_detector(
                pt_cfg, vision_client=vision_for_pt,
            )
        except Exception:
            logger.debug("peer_typing 初始化失败", exc_info=True)
            self._peer_typing = None

        # ★ P0-E2 (2026-05-03 监控发现 OCR chat_key 分裂)：
        # chat_name → canonical chat_key 的 in-memory cache + fuzzy resolve。
        # 同一真实用户被 OCR 抖动到多个 chat_name（"Victor Zan" / "Victor"）
        # → 之前各自独立 chat_state 行 + 独立 history → AI hist=0 冷启动。
        # 这里给每个 chat_name 解析到 canonical chat_key（与现有表里相似名
        # 共用），降低分裂面。
        self._chat_key_resolve_cache: Dict[str, str] = {}

    def refresh_cfg(self, new_cfg: Dict[str, Any]) -> None:
        """热重载 runner 配置（service drain/run_once 每轮调用）。
        使 config.yaml 修改无需重启即对 pacing / gate / language 生效。
        """
        if new_cfg:
            self._cfg = dict(new_cfg)
            self._reply_mode = (
                self._cfg.get("reply_mode") or "auto"
            ).lower().strip()
            try:
                self._lead_qualifier.update_cfg(
                    self._cfg.get("lead_qualification") or {}
                )
            except Exception:
                logger.debug("[messenger_rpa] lead_qualifier refresh failed", exc_info=True)

    def bind_telegram_client(self, tg_client: Any) -> None:
        self._telegram_client = tg_client

    def _run_once_target_names(self) -> List[str]:
        """Optional allowlist for controlled manual tests."""
        raw = (
            self._cfg.get("run_once_target_names")
            or self._cfg.get("target_chat_names")
            or self._cfg.get("test_target_names")
        )
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [str(x).strip() for x in raw if str(x or "").strip()]

    def _sticky_thread_names(self) -> List[str]:
        """P2-S 粘性会话白名单：这些 chat 发送成功后不退 thread，让下轮
        smart_current_thread 直接接管，省去 inbox 扫描 + tap 进入的开销
        （30-60s → 5-10s）。空列表 = 不启用粘性。

        P26 (2026-05-04)：合并 config 静态白名单 + auto-sticky 动态名单
        （发送成功后短期内自动 sticky）。auto-sticky 受
        sticky_thread.auto_sticky_enabled（默认 true）控制。
        """
        cfg = self._cfg.get("sticky_thread") or {}
        if not isinstance(cfg, dict) or not cfg.get("enabled", False):
            return []
        raw = cfg.get("target_chat_names") or cfg.get("chat_names") or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            raw = []
        result = [str(x).strip() for x in raw if str(x or "").strip()]
        # P26：拼上 auto-sticky 中尚未过期的 chat
        if cfg.get("auto_sticky_enabled", True):
            now_m = time.monotonic()
            expired: List[str] = []
            for name, expiry in self._auto_sticky_until.items():
                if expiry > now_m:
                    if name and name not in result:
                        result.append(name)
                else:
                    expired.append(name)
            for k in expired:
                self._auto_sticky_until.pop(k, None)
        return result

    def _is_sticky_chat(self, chat_name: str) -> bool:
        """判断某 chat 是否在粘性会话白名单内。"""
        if not chat_name:
            return False
        names = self._sticky_thread_names()
        if not names:
            return False
        return self._chat_name_matches_any(chat_name, names)

    def _should_skip_send(
        self, chat_name: str, *, source: str = "",
    ) -> Optional[str]:
        """🛡 统一的"是否应该跳过本次回复"决策门（single source of truth）。

        架构重构（4 次疯狂事件后引入）：所有可能触发 send 的路径**强制**调用
        此方法做前置检查，避免 sticky/inbox/fast_path 等多条路径**各有各的
        cooldown** 但**任一路径绕过其他路径的保护**。

        检查项（按优先级，任一不通过即拒绝）：
          1. self_skip_until cooldown（norm_key 维度，OCR 漂移也兜得住）
          2. last_sent_at + post_send_cooldown_sec（state_store 权威源）
          3. is_skipped_chat（永久跳过名单）
          4. P0-2: per_chat_hourly_cap（配置开关；防长窗口刷屏的硬天花板）

        返回：
          None = 通过，可以发
          str  = 拒绝原因（已写日志）
        """
        if not chat_name:
            return None
        try:
            chat_key = self._chat_key_for(chat_name)  # P0-E2 OCR 容忍
            # ── 1. self_skip_until（内存 cooldown，norm_key 抗 OCR 漂移）──
            ss_key = _self_skip_norm_key(chat_name)
            ss_until = self._self_skip_until.get(ss_key, 0.0)
            now_mono = time.monotonic()
            if ss_until > now_mono:
                remaining = ss_until - now_mono
                logger.warning(
                    "[messenger_rpa] 🛡 send_gate REJECT chat=%r src=%s "
                    "reason=self_skip_cooldown remaining=%.0fs",
                    chat_name, source or "?", remaining,
                )
                return f"self_skip_cooldown:{remaining:.0f}s"
            # ── 2. last_sent_at + post_send_cooldown_sec（持久权威源）──
            try:
                cs = self._state.get_chat_state(chat_key)
                last_sent = float(cs.get("last_sent_at") or 0)
            except Exception:
                last_sent = 0.0
            sticky_cfg = self._cfg.get("sticky_thread") or {}
            base_cd = float(sticky_cfg.get("post_send_cooldown_sec", 90) or 90)
            if last_sent > 0:
                elapsed = time.time() - last_sent
                if elapsed < base_cd:
                    remaining = base_cd - elapsed
                    logger.warning(
                        "[messenger_rpa] 🛡 send_gate REJECT chat=%r src=%s "
                        "reason=last_sent_cooldown remaining=%.0fs",
                        chat_name, source or "?", remaining,
                    )
                    return f"last_sent_cooldown:{remaining:.0f}s"
            # ── 3. 永久跳过名单 ──
            try:
                if self._state.is_skipped_chat(chat_key):
                    return "skipped_chat_blacklist"
            except Exception:
                pass
            # ── 4. P0-2: per_chat_hourly_cap（长窗口硬天花板）──
            # runaway_guard 是 5 分钟 3 次的短窗口熔断，但**长时间**（一小时几十次）
            # 仍可能堆出骚扰量。此处给运营一个独立维度的硬上限闸门。
            # 复用 _chat_send_timestamps 内存 deque（runaway_guard 已在维护），
            # 故无额外存储成本；进程重启清零是预期行为（重启 = 假定健康重新计数）。
            try:
                cap_raw = self._cfg.get("per_chat_hourly_cap", 0)
                cap = int(cap_raw or 0)
            except (TypeError, ValueError):
                cap = 0
            if cap > 0 and hasattr(self, "_chat_send_timestamps"):
                dq = self._chat_send_timestamps.get(chat_name)
                if dq:
                    now_w = time.time()
                    window = 3600.0  # 1 小时
                    # 不修改 deque（runaway 在用），只 count
                    recent = sum(1 for ts in dq if (now_w - ts) <= window)
                    if recent >= cap:
                        logger.warning(
                            "[messenger_rpa] 🛡 send_gate REJECT chat=%r src=%s "
                            "reason=per_chat_hourly_cap recent=%d cap=%d",
                            chat_name, source or "?", recent, cap,
                        )
                        return f"per_chat_hourly_cap:{recent}/{cap}"
        except Exception:
            logger.debug(
                "[messenger_rpa] _should_skip_send exception", exc_info=True,
            )
        return None

    def _record_chat_send(self, chat_name: str, peer_text: str = "") -> None:
        """P3-A 疯狂回复熔断器：记录每次 send 成功的时间戳 + 当时 peer_text 到滚动窗口。
        peer_text 用于 sequence_check 检测"main.py 自顾自重复回复同一上下文"。

        P0-C (2026-05-03)：加诊断日志便于反查"runaway 为什么没 trip"。
        """
        if not chat_name:
            logger.info(
                "[messenger_rpa] _record_chat_send: empty chat_name → noop",
            )
            return
        if not hasattr(self, "_chat_send_timestamps"):
            from collections import deque as _dq
            self._chat_send_timestamps: Dict[str, Any] = {}
            self._chat_send_peer_texts: Dict[str, Any] = {}
        if chat_name not in self._chat_send_timestamps:
            from collections import deque as _dq
            self._chat_send_timestamps[chat_name] = _dq(maxlen=20)
            self._chat_send_peer_texts[chat_name] = _dq(maxlen=20)
        self._chat_send_timestamps[chat_name].append(time.time())
        self._chat_send_peer_texts[chat_name].append((peer_text or "")[:300])
        logger.info(
            "[messenger_rpa] _record_chat_send: chat=%r dq_len=%d (after append)",
            chat_name, len(self._chat_send_timestamps[chat_name]),
        )

    @staticmethod
    def _decide_inbox_self_sent_skip(
        *,
        vision_preview: str,
        last_sent_at: float,
        last_reply: str,
        hard_window_sec: float = 60.0,
        overlap_threshold: float = 0.5,
        now_ts: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """P0-A 三层守卫的纯逻辑函数（无副作用，便于独立单元测试）。

        前提：调用方已经判定 UI XML 显示当前 inbox 顶行是 self-sent
        （target_ui.is_self_last == True）。本函数决定是否要"信任 XML，
        跳过 tap"，还是"信任 Vision 截图、覆盖 XML、继续 tap"。

        判定优先级（任一命中即返回 should_skip=True）：

          L1) hard_skip_window:
              我方刚发不久（now - last_sent_at < hard_window_sec）→ 强信任
              XML，无视 Vision。理由：60s 内对方"刚收到回复就发新消息且
              Messenger 把新对方消息缓存为 self-sent 预览"概率几乎为零，
              该组合几乎只在 vision OCR 漏读 "You:" 前缀时出现。

          L2) vision_self_prefix:
              Vision preview 自身就以 "You:" / "你:" 等前缀打头 → 已是
              self-sent，无需覆盖。

          L3) overlap_with_last_reply:
              Vision preview 与 last_reply 文本重叠 ≥ overlap_threshold
              → 视为 OCR 漏前缀，仍是上次自方回复。

          L4) ambiguous_status:
              Vision preview 太短或纯状态词（"Sent" / "Delivered" 等）
              → 不足以否定 XML 的 self-sent 判断。

          L5) vision_overrides_xml（默认）:
              Vision 看到了不同的、可信的 peer 内容 → XML content-desc
              确实陈旧，允许覆盖。

        Args:
            vision_preview: Vision 截图读到的该 inbox 行预览
            last_sent_at: chat_state.last_sent_at（我方上次发送 epoch 时间）
            last_reply: chat_state.last_reply（我方上次回复内容）
            hard_window_sec: L1 时间窗（秒），<=0 关闭
            overlap_threshold: L3 重叠率阈值，[0,1]
            now_ts: 注入用，None 则用 time.time()

        Returns:
            (should_skip, reason)
            should_skip=True → 调用方应当 return 跳过该 chat
            should_skip=False → 调用方应当继续 tap（Vision 覆盖 XML）
            reason ∈ {"hard_skip_window", "vision_self_prefix",
                      "overlap_with_last_reply", "ambiguous_status",
                      "vision_overrides_xml"}
        """
        from src.integrations.messenger_rpa.ui_scraper import (
            _self_prefixed_preview_has_text,
        )

        vp = (vision_preview or "").strip()
        if now_ts is None:
            now_ts = time.time()

        # ── L1: last_sent_at 硬窗口 ──
        if (
            hard_window_sec > 0
            and last_sent_at > 0
            and (now_ts - last_sent_at) < hard_window_sec
        ):
            return True, "hard_skip_window"

        # ── L2: vision 自我前缀检查 ──
        # vp 为空时也视为 self（保守 — XML 已经说 self，没有信号反驳）
        if not vp or _self_prefixed_preview_has_text(vp):
            return True, "vision_self_prefix"

        # ── L3: vision_preview vs last_reply 重叠 ──
        last_reply_clean = (last_reply or "").strip()
        if last_reply_clean and len(last_reply_clean) >= 4:
            ovl = MessengerRpaRunner._text_overlap_ratio(
                last_reply_clean, vp,
            )
            if ovl >= overlap_threshold:
                return True, "overlap_with_last_reply"

        # ── L4: 模糊状态词 ──
        vp_clean = re.sub(r"[\d:.\s]+", "", vp)
        AMBIGUOUS_STATUS = {
            "sent", "delivered", "seen", "read",
            "已发送", "已送达", "已读", "已送達",
            "送信済み", "配信済み", "既読",
        }
        if (
            len(vp_clean) < 3
            or vp.casefold().strip().rstrip(".") in AMBIGUOUS_STATUS
        ):
            return True, "ambiguous_status"

        # ── L5: 允许 Vision 覆盖 XML（XML content-desc 真陈旧的合法路径）──
        return False, "vision_overrides_xml"

    @staticmethod
    def _text_overlap_ratio(a: str, b: str) -> float:
        """两个字符串字符级重叠率（用于自方序列重复检测）。
        简单 set intersection over union of char trigrams。"""
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        def _trigrams(s: str) -> set:
            s = s.strip()
            if len(s) < 3:
                return {s}
            return {s[i:i+3] for i in range(len(s) - 2)}
        ta, tb = _trigrams(a), _trigrams(b)
        if not ta or not tb:
            return 0.0
        inter = len(ta & tb)
        union = len(ta | tb)
        return inter / union if union else 0.0

    def _check_runaway_circuit(
        self, chat_name: str, result: Dict[str, Any],
    ) -> bool:
        """P3-A 疯狂回复熔断器（防客户被刷屏的最后底线）：
        如果同一 chat 在 window_sec 内 sent ≥ max_sends_per_window 次 →
        立即熔断：返回 True，调用方应跳过本次回复 + 退 thread + 设长 cooldown。
        触发场景：未来某 bug（如 hash baseline 误判）导致 main.py 自顾自连发。

        P0-C (2026-05-03)：
          - 加诊断日志：每次入口打 deque len + window，便于反查"为什么没 trip"
          - 加硬天花板：即使 enabled=false 也保留 5 分钟 N 次（默认 10）
            的硬熔断作为最终底线，配置项 runaway_guard.hard_ceiling_sends
            （0 = 关闭硬天花板，默认 10）。
        """
        if not chat_name:
            return False
        cfg = self._cfg.get("runaway_guard") or {}
        enabled = bool(cfg.get("enabled", True))
        if not hasattr(self, "_chat_send_timestamps"):
            return False
        dq = self._chat_send_timestamps.get(chat_name)
        if not dq:
            # 诊断：runaway 检查命中此分支说明 deque 为空——
            # 反查"为什么 8 条爆炸 runaway 没 trip"用
            logger.info(
                "[messenger_rpa] runaway_circuit check: chat=%r dq=empty "
                "(没 sent 历史，pass)", chat_name,
            )
            return False
        now = time.time()
        window_sec = float(cfg.get("window_sec", 300) or 300)
        # 清理窗口外的旧记录
        while dq and (now - dq[0]) > window_sec:
            dq.popleft()
        # 诊断日志：每次都输出 deque 状态
        logger.info(
            "[messenger_rpa] runaway_circuit check: chat=%r dq_len=%d "
            "window=%ds enabled=%s",
            chat_name, len(dq), int(window_sec), enabled,
        )
        # ── P0-C 硬天花板（永不可关）──
        # 即使 cfg.enabled=false，也保留一个更宽松的硬熔断
        try:
            hard_ceiling = int(cfg.get("hard_ceiling_sends", 10) or 0)
        except (TypeError, ValueError):
            hard_ceiling = 10
        if hard_ceiling > 0 and len(dq) >= hard_ceiling:
            cooldown_sec = float(cfg.get("cooldown_sec", 1800) or 1800)
            self._self_skip_until[_self_skip_norm_key(chat_name)] = (
                time.monotonic() + cooldown_sec
            )
            result.setdefault("hints", []).append(
                f"runaway_hard_ceiling:count_n={len(dq)}/{window_sec}s"
            )
            logger.error(
                "[messenger_rpa] 🚨 RUNAWAY HARD-CEILING TRIPPED chat=%r: "
                "sent %d times in %ds (ceiling=%d, enabled=%s) → "
                "cooldown %ds（终极底线）",
                chat_name, len(dq), int(window_sec), hard_ceiling,
                enabled, int(cooldown_sec),
            )
            return True
        # 软门（可被 enabled=false 关闭）
        if not enabled:
            return False
        max_sends = int(cfg.get("max_sends_per_window", 3) or 3)
        if len(dq) >= max_sends:
            cooldown_sec = float(cfg.get("cooldown_sec", 1800) or 1800)
            self._self_skip_until[_self_skip_norm_key(chat_name)] = (
                time.monotonic() + cooldown_sec
            )
            result.setdefault("hints", []).append(
                f"runaway_circuit_tripped:count_n={len(dq)}/{window_sec}s"
            )
            logger.error(
                "[messenger_rpa] 🚨 RUNAWAY CIRCUIT TRIPPED chat=%r: "
                "sent %d times in %ds → cooldown %ds（防止刷屏客户）",
                chat_name, len(dq), int(window_sec), int(cooldown_sec),
            )
            return True
        # 优化 C：自方序列重复检测（比次数熔断更精准）
        # 如果最近 N 次 sent 之间 peer_text 相互高度相似 → main.py 在自顾自重复
        if cfg.get("sequence_check_enabled", True):
            seq_max = int(cfg.get("sequence_max_consecutive_self", 2) or 2)
            seq_thr = float(cfg.get("sequence_overlap_threshold", 0.6) or 0.6)
            peer_dq = getattr(self, "_chat_send_peer_texts", {}).get(chat_name)
            if peer_dq and len(peer_dq) >= seq_max + 1:
                # 取最近 seq_max + 1 个 peer_text，对比相邻两两相似度
                recent = list(peer_dq)[-(seq_max + 1):]
                similar_pairs = 0
                for i in range(len(recent) - 1):
                    if self._text_overlap_ratio(recent[i], recent[i + 1]) >= seq_thr:
                        similar_pairs += 1
                if similar_pairs >= seq_max:
                    cooldown_sec = float(cfg.get("cooldown_sec", 1800) or 1800)
                    self._self_skip_until[_self_skip_norm_key(chat_name)] = (
                        time.monotonic() + cooldown_sec
                    )
                    result.setdefault("hints", []).append(
                        f"runaway_circuit_tripped:sequence_n={similar_pairs}"
                    )
                    logger.error(
                        "[messenger_rpa] 🚨 RUNAWAY CIRCUIT TRIPPED (sequence) "
                        "chat=%r: %d 次连续 sent 之间 peer_text 高度相似 → "
                        "main.py 自顾自重复回复，cooldown %ds",
                        chat_name, similar_pairs, int(cooldown_sec),
                    )
                    return True
        return False

    def _check_sticky_thread_changed(
        self,
        thread_png_path: str,
        chat_name: str,
        result: Dict[str, Any],
    ) -> bool:
        """P2-A 粘性 thread 入口快速 hash diff：
        crop thread 截图底部气泡区域 → md5 hash → 对比上次 hash
        返回 True = 有变化（需要 vision 处理），False = 无变化（早退）
        每 full_check_after_n_idle 次 idle 强制走 vision（兜底防 hash 假阳性）
        """
        cfg = self._cfg.get("sticky_thread") or {}
        if not cfg.get("hash_diff_enabled", True):
            return True  # 没启用 → 默认有变化（走 vision）
        if not thread_png_path or not chat_name:
            return True
        # 初始化 runner 状态字段（懒加载）
        if not hasattr(self, "_sticky_last_hash"):
            self._sticky_last_hash: Dict[str, str] = {}
        if not hasattr(self, "_sticky_idle_count"):
            self._sticky_idle_count: Dict[str, int] = {}

        try:
            from PIL import Image
            with Image.open(thread_png_path) as im:
                w, h = im.size
                # crop 底部气泡区域：y=35%-82%（避开顶栏 + 输入框 + 键盘）
                top = int(h * 0.35)
                bottom = int(h * 0.82)
                crop = im.crop((0, top, w, bottom))
                # 缩到 128x128 求 md5（normalize 抖动 + 计算快）
                crop_small = crop.resize((128, 128))
                raw = crop_small.tobytes()
            h_now = hashlib.md5(raw).hexdigest()[:16]
        except Exception:
            logger.debug("[messenger_rpa] sticky hash diff failed", exc_info=True)
            return True  # 失败 fail-open（走 vision）

        last = self._sticky_last_hash.get(chat_name, "")
        result["sticky_hash"] = h_now
        if last and last == h_now:
            # 没变化
            idle_count = self._sticky_idle_count.get(chat_name, 0) + 1
            self._sticky_idle_count[chat_name] = idle_count
            # P-fix (2026-05-04)：30 → 10。bot 状态/屏幕脱钩 case（屏幕已在
            # inbox 但 runner 内存仍 sticky）需要 ≤15s 自愈，30 次（45s）太慢。
            full_check_n = int(cfg.get("full_check_after_n_idle", 10) or 10)
            if idle_count >= full_check_n:
                # 强制走完整 vision 兜底
                self._sticky_idle_count[chat_name] = 0
                result.setdefault("hints", []).append(
                    f"sticky_force_full:{idle_count}"
                )
                # ★ 同时 dump XML 验证屏幕真在 thread 内——不在就告诉调用方
                #   早退到外层 cycle，下个 cycle 入口的 B 守卫会重 sync。
                try:
                    from src.integrations.messenger_rpa import (
                        thread_actions as _ta_sf,
                        ui_scraper as _uis_sf,
                    )
                    _serial_sf = (
                        result.get("serial")
                        or result.get("device_serial")
                        or self._resolve_serial(result)
                    )
                    _xml_sf = _ta_sf.dump_view_tree(_serial_sf) if _serial_sf else None
                    if _xml_sf and not _uis_sf.is_in_thread(_xml_sf):
                        # 屏幕在 inbox/其他页面，不在 thread——清 sticky 状态
                        self._sticky_last_hash.pop(chat_name, None)
                        self._sticky_idle_count.pop(chat_name, None)
                        result.setdefault("hints", []).append(
                            "sticky_force_full_screen_not_thread:reset"
                        )
                        logger.warning(
                            "[messenger_rpa] sticky force_full: 屏幕不在 thread "
                            "(chat=%r)，清 sticky 让下个 cycle 重 sync", chat_name,
                        )
                        return False  # 让调用方早退，外层重走 cycle 入口
                except Exception:
                    logger.debug("sticky force_full screen check 异常", exc_info=True)
                return True
            result["sticky_idle_count"] = idle_count
            return False  # 没变化 → 调用方早退
        # 有变化（或首次）→ 更新 hash + 重置 idle count
        self._sticky_last_hash[chat_name] = h_now
        self._sticky_idle_count[chat_name] = 0
        if last:
            result.setdefault("hints", []).append("sticky_thread_changed")
        return True

    def _check_inbox_changed(
        self,
        inbox_png_path: str,
        serial: str,
        result: Dict[str, Any],
    ) -> bool:
        """方案 3 (2026-05-03): inbox 阶段 hash 早退。
        crop inbox 列表区域 → md5 hash → 对比上次 hash
        返回 True = 有变化（需要走 vision），False = 无变化（早退）

        每 full_check_after_n_idle 次 idle 强制走 vision（兜底防 hash 假阳性
        / messenger 推送未刷新 inbox UI 但有新消息的极端 case）。

        预期收益：节约 inbox vision call ~30s/轮（与 P2-A sticky_idle 同款效果）。
        风险：messenger 推送通知未刷新 inbox UI 时漏检；由 full_check_n=30 兜底
        （interval_sec=8s × 30 = 240s = 4 分钟内一定走一次完整 vision）。
        """
        cfg = self._cfg.get("inbox_hash_diff") or {}
        if not cfg.get("enabled", True):
            return True  # 没启用 → 默认有变化（走 vision）
        if not inbox_png_path:
            return True
        # 用 serial 作 key，避免不同设备共享 hash
        key = serial or "_default"
        if not hasattr(self, "_inbox_last_hash"):
            self._inbox_last_hash: Dict[str, str] = {}
        if not hasattr(self, "_inbox_idle_count"):
            self._inbox_idle_count: Dict[str, int] = {}

        try:
            from PIL import Image
            with Image.open(inbox_png_path) as im:
                w, h = im.size
                # crop inbox 列表区域：y=15%-75%（避开顶栏搜索栏 + 底部 nav）
                top = int(h * 0.15)
                bottom = int(h * 0.75)
                crop = im.crop((0, top, w, bottom))
                # 缩到 128x128 求 md5（normalize 抖动 + 计算快）
                crop_small = crop.resize((128, 128))
                raw = crop_small.tobytes()
            h_now = hashlib.md5(raw).hexdigest()[:16]
        except Exception:
            logger.debug(
                "[messenger_rpa] inbox hash diff failed", exc_info=True,
            )
            return True  # 失败 fail-open（走 vision）

        last = self._inbox_last_hash.get(key, "")
        result["inbox_hash"] = h_now
        if last and last == h_now:
            # 没变化
            idle_count = self._inbox_idle_count.get(key, 0) + 1
            self._inbox_idle_count[key] = idle_count
            # P-fix (2026-05-04): 30 → 10. inbox unread badge 偶发不让 hash 变
            # （u2 看到 unread 但 hash 一致），30 次 (4 min) 太慢自愈。
            full_check_n = int(cfg.get("full_check_after_n_idle", 10) or 10)
            if idle_count >= full_check_n:
                # 强制走完整 vision 兜底
                self._inbox_idle_count[key] = 0
                result.setdefault("hints", []).append(
                    f"inbox_force_full:{idle_count}"
                )
                return True
            result["inbox_idle_count"] = idle_count
            return False  # 没变化 → 调用方早退
        # 有变化（或首次）→ 更新 hash + 重置 idle count
        self._inbox_last_hash[key] = h_now
        self._inbox_idle_count[key] = 0
        if last:
            result.setdefault("hints", []).append("inbox_changed")
        return True

    def _run_once_start_mode(self) -> str:
        """How run_once should position Messenger before reading messages."""
        raw = str(
            self._cfg.get("run_once_start_mode")
            or self._cfg.get("start_position_mode")
            or ""
        ).strip().lower()
        aliases = {
            "smart": "smart_current_thread",
            "preserve": "smart_current_thread",
            "preserve_current_thread": "smart_current_thread",
            "current_thread": "smart_current_thread",
            "force_chats": "force_chats",
            "force_inbox": "force_chats",
            "chats": "force_chats",
            "inbox": "force_chats",
        }
        mode = aliases.get(raw, raw or "smart_current_thread")
        if bool(self._cfg.get("force_return_to_chats", False)):
            return "force_chats"
        if mode not in ("smart_current_thread", "force_chats"):
            return "smart_current_thread"
        return mode

    def _chat_key_for(self, chat_name: str) -> str:
        """P0-E2: chat_name → canonical chat_key（带 OCR 容忍 fuzzy resolve）。

        替换原 ``f"{self._chat_key_prefix}:{chat_name}"`` 字符串拼接。
        策略：
          1. cache 命中（同 session 已 resolved）→ 直接返回
          2. 严格匹配：chat_state 表已有 exact key → 返回
          3. fuzzy match：扫表用 SequenceMatcher.ratio()，超阈值
             （chat_key_fuzzy_threshold，默认 0.85）→ 用现有 key
          4. 都不命中 → 返回新的严格 key

        阈值 0.85 较保守，仅归并极相似变体（避免误归不同真实用户）。
        阶段 2 数据驱动调阈值 / 加 prefix bucket。
        """
        if not chat_name:
            return f"{self._chat_key_prefix}:_empty"
        # Lazy-init cache（防 object.__new__ 跳过 __init__ 的测试 fixture 路径）
        if not hasattr(self, "_chat_key_resolve_cache"):
            self._chat_key_resolve_cache = {}
        cn_key = chat_name
        cached = self._chat_key_resolve_cache.get(cn_key)
        if cached:
            return cached

        strict = f"{self._chat_key_prefix}:{chat_name}"

        # 1. 严格匹配优先
        try:
            if self._state.get_chat_state(strict):
                self._chat_key_resolve_cache[cn_key] = strict
                return strict
        except Exception:
            logger.debug("chat_key strict lookup failed", exc_info=True)

        # 2. fuzzy resolve
        try:
            threshold = float(
                self._cfg.get("chat_key_fuzzy_threshold", 0.85) or 0
            )
        except (TypeError, ValueError):
            threshold = 0.85
        if threshold > 0 and threshold < 1:
            try:
                from difflib import SequenceMatcher
                target_lower = chat_name.casefold().strip()
                if target_lower:
                    candidates = self._state.list_chat_states(limit=200)
                    prefix = self._chat_key_prefix + ":"
                    best_key, best_score = None, 0.0
                    for row in candidates:
                        ck = str(row.get("chat_key") or "")
                        if not ck.startswith(prefix):
                            continue
                        existing_name = ck[len(prefix):]
                        existing_lower = existing_name.casefold().strip()
                        if not existing_lower:
                            continue
                        ratio = SequenceMatcher(
                            None, target_lower, existing_lower,
                        ).ratio()
                        if ratio > best_score:
                            best_key, best_score = ck, ratio
                    if best_key and best_score >= threshold:
                        logger.warning(
                            "[messenger_rpa] P0-E2 chat_key fuzzy resolve: "
                            "%r → %r (ratio=%.2f >= %.2f)",
                            chat_name, best_key, best_score, threshold,
                        )
                        self._chat_key_resolve_cache[cn_key] = best_key
                        return best_key
            except Exception:
                logger.debug(
                    "chat_key fuzzy resolve failed", exc_info=True,
                )

        # 3. fallback: 新建 strict key
        self._chat_key_resolve_cache[cn_key] = strict
        return strict

    @staticmethod
    def _chat_name_matches_any(chat_name: str, names: List[str]) -> bool:
        """检查 chat_name 是否命中任一 names 项。

        P0-E1 (2026-05-03 监控发现 P0-H 入口)：
          - 原 `cn == want or want in cn or cn in want` 三支匹配
          - `cn in want` 是 OCR 漏后缀的 P0-H 入口（sticky 配 "Victor Zan"，
            OCR 给 "Victor" → "Victor" in "Victor Zan" → True → fast_path
            接管错的 chat_key → 跨人设串戏）
          - 移除该支，保留 `want in cn`（运营常见的"配短名匹配多个变体"
            场景仍工作，如 sticky 配 "Victor"，OCR 给 "Victor Zan/Smith"
            仍 match）

        效果：
          - sticky=["Victor Zan"]，chat="Victor Zan"     → True ✓
          - sticky=["Victor Zan"]，chat="Victor Zan San" → True ✓ (want in cn)
          - sticky=["Victor Zan"]，chat="Victor"         → False ✓ 切断 P0-H 入口
          - sticky=["Victor"], chat="Victor Zan"         → True ✓ 运营前缀仍工作
        """
        cn = (chat_name or "").strip().lower()
        if not cn:
            return False
        for raw in names:
            want = (raw or "").strip().lower()
            if want and (cn == want or want in cn):
                return True
        return False

    def _current_thread_target_from_title(
        self,
        title: str,
        target_names: List[str],
        result: Dict[str, Any],
    ) -> Optional[UnreadChat]:
        """Return a synthetic target when run_once starts inside target thread.

        The fast path is deliberately allowlist-gated. Without an explicit
        target name, a background run could resume whatever thread Messenger
        last displayed and send in the wrong conversation.
        """
        name = (title or "").strip()
        if not name:
            return None
        result["current_thread_title"] = name
        if not target_names:
            result.setdefault("hints", []).append(
                "current_thread_seen:no_target_allowlist"
            )
            return None
        if not self._chat_name_matches_any(name, target_names):
            result.setdefault("hints", []).append(
                f"current_thread_seen:not_target:{name}"
            )
            return None
        return UnreadChat(
            name=name,
            preview="",
            time="",
            row_index=0,
            y_percent=0.0,
            quality_hint="current_thread",
            score=100.0,
            skip_inbox_tap=True,
        )

    def _xml_inbox_unread_fallback(self, serial: str) -> List["UnreadChat"]:
        """vision 全失败时，用 XML dump_inbox_rows + chat_state 历史名做 last
        resort 兜底。u2 让 inbox row XML 稳定可读，对 vision 漏读最终保底。

        策略：
          - dump 拿 row bounds + preview
          - 跳过 self_last 的 row（"You: ..." / "You sent..."）
          - 从 preview 提 sender 前缀（如 "Victor sent a voice..." → "Victor"）
          - 用 sender 模糊匹配 _state 历史 chat name 列表得到完整 name
          - 没 sender 就用 row 索引顺序，name 留 sender 本身或空
        """
        try:
            from src.integrations.messenger_rpa.ui_inbox_scraper import (
                dump_inbox_rows,
            )
        except Exception:
            return []
        try:
            rows = dump_inbox_rows(
                serial, adb_user_id=self._adb_user_id, timeout_s=6.0,
            )
        except Exception:
            logger.debug(
                "[messenger_rpa] xml_inbox_unread_fallback dump 失败",
                exc_info=True,
            )
            return []
        if not rows:
            return []
        # 历史 chat name pool（来自 chat_state DB）
        historical: List[str] = []
        try:
            for row in self._state.list_chat_states(limit=200) or []:
                nm = (row.get("chat_name") or "").strip()
                if nm and nm not in historical:
                    historical.append(nm)
        except Exception:
            pass

        out: List[UnreadChat] = []
        # rows 已按 y_top 排序；用 enumerate 作 row_index
        for ix, row in enumerate(rows):
            if row.is_self_last:
                continue  # bot 自己最末气泡，不当作 unread
            preview = (row.preview or "").strip()
            if not preview:
                continue
            # 提取 sender 前缀："Victor sent..." / "Victor: ..." / "Yunshan ..."
            sender = ""
            m = re.match(r"^([\wÀ-￿]{1,40})(?:\s+sent|:)", preview)
            if m:
                sender = m.group(1)
            # 用 sender 模糊匹配历史完整 chat name
            chat_name = ""
            if sender and historical:
                _s_low = sender.lower()
                for hc in historical:
                    if _s_low in hc.lower() or hc.lower() in _s_low:
                        chat_name = hc
                        break
            if not chat_name:
                chat_name = sender or f"row_{ix}"
            out.append(UnreadChat(
                name=chat_name,
                preview=preview,
                time="",
                row_index=ix,
                y_percent=0.0,
                quality_hint="friend",
                score=70.0 - ix,  # 顶部 row 优先
            ))
            if len(out) >= 3:
                break
        return out

    def _push_recent_reply(self, chat_key: str, reply_text: str) -> None:
        """P15: 维护 per-chat 最近 N 条 self reply（in-memory，重启丢失）。
        每次成功 send 后调用，给后续 self_overlap 检测提供更广的对比集。"""
        if not chat_key or not reply_text:
            return
        text = reply_text.strip()
        if not text:
            return
        bucket = self._recent_replies_per_chat.setdefault(chat_key, [])
        bucket.append(text)
        if len(bucket) > self._recent_replies_max:
            del bucket[: len(bucket) - self._recent_replies_max]

    @staticmethod
    def _sanitize_actual_title(s: str) -> str:
        """挡住 LLM/parse 偶发回出的 JSON 字面 token（"{}" / "[]" / "null" 等）
        被当作合法会话标题——一旦落到 actual_title 会触发 wrong_chat_rollback。"""
        if not s:
            return ""
        t = s.strip()
        if t in {"{}", "[]", "null", "None", "undefined", "{ }", "[ ]"}:
            return ""
        return t

    def _thread_title_from_xml(self, serial: str, result: Dict[str, Any]) -> str:
        try:
            from src.integrations.messenger_rpa import thread_actions as _ta
            from src.integrations.messenger_rpa import ui_scraper as _uis

            xml = _ta.dump_view_tree(
                serial,
                dump_timeout=float(self._cfg.get("ui_dump_timeout_s") or 6.0),
                cat_timeout=4.0,
            )
            raw = (_uis.find_thread_title(xml) or "").strip() if xml else ""
            title = self._sanitize_actual_title(raw)
            if title:
                result["thread_title_xml"] = title
            return title
        except Exception:
            logger.debug("[messenger_rpa] thread title xml read failed", exc_info=True)
            return ""

    async def _thread_title_from_vision(
        self,
        serial: str,
        result: Dict[str, Any],
        *,
        reason: str,
        target_name: Optional[str] = None,
    ) -> str:
        if not bool(self._cfg.get("thread_title_vision_fallback", True)):
            return ""
        # ── cache 命中：跳过整段 vision OCR（5–15s/cycle）────────
        # cache key 用 (serial, normalized target_name)。同 chat 的 peer
        # 显示名在一次会话生命周期内不会变，TTL 30s 足够保证安全。
        cache_key: Optional[Tuple[str, str]] = None
        if target_name and self._title_vision_cache_ttl_sec > 0:
            norm = (target_name or "").strip()
            if norm:
                cache_key = (serial, norm)
                hit = self._title_vision_cache.get(cache_key)
                if hit and hit[1] > time.monotonic():
                    cached_title, _exp = hit
                    result[f"thread_title_vision_{reason}"] = {
                        "title": cached_title,
                        "debug": "cache_hit",
                    }
                    result.setdefault("hints", []).append(
                        f"thread_title_vision_cache_hit:{reason}"
                    )
                    if cached_title:
                        result["thread_title_vision"] = cached_title
                    return cached_title
        try:
            from src.integrations.messenger_rpa.thread_title_vision import (
                read_thread_title_via_vision,
            )

            vr = await asyncio.to_thread(
                read_thread_title_via_vision,
                serial,
                self._vision_cfg(),
                self._global_vision_cfg(),
            )
            title = self._sanitize_actual_title((vr.title or "").strip())
            result[f"thread_title_vision_{reason}"] = {
                "title": title,
                "debug": vr.debug,
            }
            if title:
                result["thread_title_vision"] = title
                # 只缓存非空 + 通过 sanitize 的合法 title
                if cache_key is not None:
                    self._title_vision_cache[cache_key] = (
                        title,
                        time.monotonic() + self._title_vision_cache_ttl_sec,
                    )
            return title
        except Exception:
            logger.debug(
                "[messenger_rpa] thread title vision fallback failed",
                exc_info=True,
            )
            result.setdefault("hints", []).append(
                f"thread_title_vision_failed:{reason}"
            )
            return ""

    def _is_send_to_screen_xml(self, xml: str) -> bool:
        """Messenger share/forward target picker is not an inbox."""
        if not xml:
            return False
        low = xml.lower()
        if "send to" in low and ("create group" in low or "write a message" in low):
            return True
        # Japanese/Chinese locale fallbacks plus repeated Send buttons.
        if ("送信先" in xml or "发送给" in xml or "傳送給" in xml) and (
            "create group" in low or "send" in low or "送信" in xml
        ):
            return True
        return False

    def _search_result_fallback_taps(
        self, wh: Tuple[int, int]
    ) -> List[Tuple[int, int, str]]:
        """Coordinate fallbacks for Messenger search results.

        Search results do not share the inbox row grid.  On 720x1600 the first
        exact "People" result sits around y=350; using inbox row y=600 taps the
        "More people" section and fails to open the target.
        """
        w, h = int(wh[0]), int(wh[1])
        rx = w / 720.0
        ry = h / 1600.0
        base = [
            (280, 350, "search_result_primary"),
            (280, 560, "search_more_0"),
            (280, 675, "search_more_1"),
        ]
        return [
            (int(round(x * rx)), int(round(y * ry)), tag)
            for x, y, tag in base
        ]

    async def _recover_send_to_screen(
        self,
        serial: str,
        wh: Tuple[int, int],
        run_id: str,
        result: Dict[str, Any],
    ) -> str:
        """Exit Messenger's ``Send to`` share picker before inbox vision.

        The share picker visually resembles a people list and Vision may report
        its rows as unread chats.  Treating it as an inbox can make the runner
        tap Send/people rows, so recovery must happen before any unread scan.
        """
        try:
            from src.integrations.messenger_rpa import thread_actions as _ta

            xml = _ta.dump_view_tree(
                serial,
                dump_timeout=float(self._cfg.get("ui_dump_timeout_s") or 6.0),
                cat_timeout=4.0,
            )
            if not self._is_send_to_screen_xml(xml or ""):
                return ""
            result.setdefault("hints", []).append("send_to_screen_recovered")
            logger.warning(
                "[messenger_rpa] detected Messenger Send-to share picker; "
                "press BACK and return to Chats before scanning inbox"
            )
            for _ in range(2):
                adb.input_keyevent(serial, "KEYCODE_BACK")
                await asyncio.sleep(0.55)
                xml2 = _ta.dump_view_tree(
                    serial,
                    dump_timeout=float(self._cfg.get("ui_dump_timeout_s") or 6.0),
                    cat_timeout=4.0,
                )
                if not self._is_send_to_screen_xml(xml2 or ""):
                    break
            self._foreground_messenger(serial, result)
            await asyncio.sleep(0.5)
            try:
                tx, ty = cc.TAB_CHATS.at(*wh)
                adb.input_tap(serial, tx, ty)
                await asyncio.sleep(0.5)
            except Exception:
                pass
            png = await self._screenshot(serial, "inbox_after_send_to_recover", run_id)
            return png or ""
        except Exception:
            logger.debug("[messenger_rpa] send-to recovery failed", exc_info=True)
            result.setdefault("hints", []).append("send_to_screen_recover_error")
            return ""

    def set_contact_hooks(self, hooks: Optional[Any]) -> None:
        """注入/摘除 ContactHooks；线程安全的原子替换，无锁即可。"""
        self._contact_hooks = hooks

    def set_portrait_extractor(self, extractor: Optional[Any]) -> None:
        """Phase 1：注入 PortraitExtractor；None 时跳过画像抽取。"""
        self._portrait_extractor = extractor

    def set_coordinator(self, coordinator: Optional[Any]) -> None:
        """注入跨账号协调器（CrossAccountCoordinator）。"""
        self._coordinator = coordinator

    def _is_spam_whitelisted_contact(
        self, account_id: str, external_id: str,
    ) -> Tuple[bool, Dict[str, Any]]:
        """优化 B：已建立画像 + 累计 ≥ N 入站 → 白名单保护。

        命中时 spam HIGH 也只单次跳过不入永久 skip 表（避免老客户突发某条
        含赌博词的复述被永久封）。返回 (whitelisted, info_dict for log)。

        config 总开关：messenger_rpa.spam_whitelist.enabled (默认 true)
        """
        wh_cfg = (self._cfg.get("spam_whitelist") or {})
        if not bool(wh_cfg.get("enabled", True)):
            return (False, {"reason": "disabled_by_config"})
        info: Dict[str, Any] = {}
        hooks = self._contact_hooks
        if hooks is None:
            return (False, {"reason": "no_hooks"})
        gw = getattr(hooks, "_gw", None)
        store = getattr(gw, "_store", None) if gw is not None else None
        if store is None:
            return (False, {"reason": "no_store"})
        try:
            ci = store.get_ci_by_external(
                "messenger", str(account_id or "default"),
                str(external_id or ""),
            )
            if ci is None:
                return (False, {"reason": "no_contact"})
            journey = store.get_journey_by_contact(ci.contact_id)
            if journey is None:
                return (False, {"reason": "no_journey"})
            has_portrait = bool(
                (getattr(journey, "context_snapshot_json", "") or "").strip()
            )
            events = store.list_events(journey.journey_id, limit=50)
            msg_in_count = sum(
                1 for e in events if e.get("event_type") == "msg_in"
            )
            min_inbound = int(
                (self._cfg.get("spam_whitelist") or {}).get(
                    "min_inbound_msgs", 5
                ) or 5
            )
            require_portrait = bool(
                (self._cfg.get("spam_whitelist") or {}).get(
                    "require_portrait", True
                )
            )
            wh = (msg_in_count >= min_inbound) and (
                has_portrait or not require_portrait
            )
            info = {
                "contact_id": ci.contact_id,
                "msg_in_count": msg_in_count,
                "has_portrait": has_portrait,
                "min_inbound": min_inbound,
                "require_portrait": require_portrait,
            }
            return (wh, info)
        except Exception as ex:
            return (False, {"reason": f"exception:{type(ex).__name__}"})

    async def _maybe_refresh_portrait_bg(
        self, journey: Any, display_name: str = "",
    ) -> None:
        """fire-and-forget：判断 + 抽画像 + 写库；任何异常都吞，不影响 runner。"""
        ext = self._portrait_extractor
        if ext is None or journey is None:
            return
        try:
            need = await asyncio.to_thread(ext.should_refresh, journey)
            if not need:
                return
            snap = await ext.extract_and_persist(
                journey=journey,
                display_name=display_name or "",
            )
            # ★ 新鲜画像推给跨账号协调器，让其他账号无需重新抽取
            if snap is not None and self._coordinator is not None:
                import json as _j
                self._coordinator.update_portrait(
                    display_name or "",
                    str(getattr(self, "_account_id", "") or "default"),
                    _j.dumps(snap, ensure_ascii=False),
                    time.time(),
                )
        except Exception:
            logger.debug("[messenger_rpa] portrait refresh bg 失败", exc_info=True)

    # P6-3/P7：统一的 approval 入队 wrapper —— 自动注入当前轮的 ai_tier，便于
    # 批量审批按 tier 过滤。所有 6 处旧 `self._state.enqueue_approval(` 已被
    # 重命名为本 wrapper。
    def _enqueue_approval_wrapped(self, **kwargs) -> int:
        if "ai_tier" not in kwargs or not kwargs.get("ai_tier"):
            # 优先看 kwargs 里是否传了 extra 带着 result / 或调用者已有 ai_tier；
            # 最稳的是调用方传 ai_tier；这里只是兜底从当前 runner 的 last tier 拿
            last_tier = getattr(self, "_last_ai_tier", "") or ""
            if last_tier:
                kwargs["ai_tier"] = last_tier
        return self._state.enqueue_approval(**kwargs)

    # ── public API ────────────────────────────────
    async def run_once(self) -> Dict[str, Any]:
        """完整一次循环；不抛异常，所有错误都进 result.error。"""
        run_id = uuid.uuid4().hex[:8]
        t0 = time.monotonic()
        # P6-3：每轮重置 tier 缓存，避免跨 chat 误打标
        self._last_ai_tier = ""
        result: Dict[str, Any] = {
            "ts": time.time(),
            "run_id": run_id,
            "ok": False,
            "step": "init",
            "chat_key": "",
            "chat_name": "",
            "peer_text": "",
            "peer_kind": "",
            "reply_text": "",
            "reader_path": "vision",
            "total_ms": 0,
            "error": "",
            "screenshot_path": "",
        }

        try:
            # ★ P3-1：风控 block 早退（0 代价，不触设备）
            try:
                blocked, until_ts = self._state.is_risk_blocked_now()
            except Exception:
                blocked, until_ts = False, 0.0
            if blocked:
                result["step"] = "risk_blocked"
                result["error"] = (
                    f"risk_blocked_until={int(until_ts)}"
                )
                result["risk"] = {
                    "hit": True, "status": "blocked",
                    "blocked_until_ts": until_ts,
                }
                return self._finish(result, t0)

            serial = self._resolve_serial(result)
            if not serial:
                return self._finish(result, t0)

            wh = self._screen_size(serial)
            result["device_wh"] = wh

            target_names = self._run_once_target_names()
            if target_names:
                result["target_chat_names"] = target_names
            # P2-S 粘性白名单也参与 smart_current_thread 判定，让粘性 chat
            # 即使没配 target_chat_names 也能被"已在 thread 内"快速通道识别
            sticky_names = self._sticky_thread_names()
            preserve_current_thread = False
            preserved_thread_title = ""
            start_mode = self._run_once_start_mode()
            result["run_once_start_mode"] = start_mode
            _smart_target_names = target_names or sticky_names

            # ── B-fix (2026-05-04)：cycle 入口屏幕状态守卫 ──────
            # 实测场景：runner 内存认为已退出 thread (sticky cleared)，
            # 但实际屏幕仍停在 Victor Zan thread 内（用户手动开/BACK 没退）。
            # runner 在 inbox 路径扫描却看到 thread 屏幕 → cr.rows=0 inbox_idle
            # 死循环，永远漏掉 thread 内新消息。
            # 修复：u2 dump XML 看真实顶栏。在 thread 内但 title 不在
            # sticky/target → 强制 BACK 让下个 cycle 重扫 inbox。
            if bool(self._cfg.get("cycle_entry_screen_state_guard", True)):
                try:
                    from src.integrations.messenger_rpa import (
                        thread_actions as _ta_se,
                        ui_scraper as _uis_se,
                    )
                    _xml_se = _ta_se.dump_view_tree(serial)
                    if _xml_se and _uis_se.is_in_thread(_xml_se):
                        _title_se = (
                            _uis_se.find_thread_title(_xml_se) or ""
                        ).strip()
                        _smart_se = list(_smart_target_names or [])
                        # 把当前 thread title 加入 sticky → 让 fast_path 自然处理
                        if _title_se and _title_se not in _smart_se:
                            logger.warning(
                                "[messenger_rpa] cycle entry: in thread %r but "
                                "not in sticky/target → 临时加入 sticky 让 fast_path 接管",
                                _title_se,
                            )
                            _smart_target_names = _smart_se + [_title_se]
                            result.setdefault("hints", []).append(
                                f"cycle_entry_thread_recovered:{_title_se[:40]}"
                            )
                except Exception:
                    logger.debug(
                        "cycle entry screen state guard 异常",
                        exc_info=True,
                    )
            if (
                start_mode == "smart_current_thread"
                and _smart_target_names
                and not self._is_messenger_lost(serial)
            ):
                # ── pre_foreground cache：上次进 thread 后没 _exit_thread 的话，
                #   foreground 还是同一个 chat，title 也没变。直接复用即省整段
                #   xml+vision（5–15s）。失效时机：_exit_thread / TTL 兜底。
                pre_title = ""
                _fg_cache_key = serial
                _fg_cache_ttl = self._title_vision_cache_ttl_sec
                _now_m = time.monotonic()
                _fg_hit = self._foreground_title_cache.get(_fg_cache_key)
                if _fg_hit and _fg_hit[1] > _now_m and _fg_cache_ttl > 0:
                    pre_title = _fg_hit[0]
                    result.setdefault("hints", []).append(
                        "pre_foreground_title_cache_hit"
                    )
                if not pre_title:
                    pre_title = self._thread_title_from_xml(serial, result)
                if not pre_title:
                    pre_title = await self._thread_title_from_vision(
                        serial, result, reason="pre_foreground",
                    )
                # 任意路径成功拿到 title 都写 cache（XML / vision 都行）
                if pre_title and _fg_cache_ttl > 0:
                    self._foreground_title_cache[_fg_cache_key] = (
                        pre_title, _now_m + _fg_cache_ttl,
                    )
                preserve_current_thread = (
                    self._current_thread_target_from_title(
                        pre_title, _smart_target_names, result,
                    )
                    is not None
                )
                if preserve_current_thread:
                    preserved_thread_title = pre_title
            if preserve_current_thread:
                result["foreground_skipped_current_thread"] = True
                result.setdefault("hints", []).append(
                    "foreground_preserved_current_thread"
                )
            else:
                result.pop("thread_title_xml", None)
                if not self._foreground_messenger(serial, result):
                    return self._finish(result, t0)

            first_shot_tag = "thread_current" if preserve_current_thread else "inbox"
            inbox_png = await self._screenshot(serial, first_shot_tag, run_id)
            if not inbox_png:
                result["step"] = (
                    "screenshot_thread_current_failed"
                    if preserve_current_thread else "screenshot_inbox_failed"
                )
                return self._finish(result, t0)
            result["screenshot_path"] = inbox_png
            if not preserve_current_thread:
                recovered_png = await self._recover_send_to_screen(
                    serial, wh, run_id, result,
                )
                if recovered_png:
                    inbox_png = recovered_png
                    result["screenshot_path"] = inbox_png

            target: Optional[UnreadChat] = None
            chat_key = ""
            thread_png = ""
            actual_title = ""

            current_title = (
                preserved_thread_title
                if preserve_current_thread else self._thread_title_from_xml(
                    serial, result
                )
            )
            # P2-S：粘性 chat 也参与"当前 thread 快速识别"，避免被 exit_to_inbox
            current_thread_target = self._current_thread_target_from_title(
                current_title, _smart_target_names, result,
            )
            if current_thread_target is not None:
                target = current_thread_target
                thread_png = inbox_png
                actual_title = current_title
                result["current_thread_fast_path"] = True
                result["screenshot_path"] = thread_png
                result.setdefault("hints", []).append("current_thread_fast_path")
                # P2-A bugfix：fast_path 跳过了 L1211 的 _original_vision_name
                # 赋值，导致后续 sticky cooldown 设置抛 UnboundLocalError → cooldown
                # 失效 → main.py 持续处理同一 chat 疯狂回复。这里补上。
                _original_vision_name = target.name
            elif current_title:
                result.setdefault("hints", []).append("current_thread_exit_to_inbox")
                self._exit_thread(serial)
                await asyncio.sleep(0.6)
                still_title = self._thread_title_from_xml(serial, result)
                if still_title and still_title == current_title:
                    result.setdefault("hints", []).append("current_thread_exit_retry")
                    self._exit_thread(serial)
                    await asyncio.sleep(0.6)
                inbox_png = await self._screenshot(
                    serial, "inbox_after_thread_exit", run_id
                )
                if not inbox_png:
                    result["step"] = "screenshot_inbox_after_thread_exit_failed"
                    return self._finish(result, t0)
                result["screenshot_path"] = inbox_png
                result.pop("thread_title_xml", None)

            if target is None:
                # ── 自动校准（首次、像素级、~200ms）──
                self._maybe_auto_calibrate(serial, wh, inbox_png, result)

                # ★ 方案 3 (2026-05-03): inbox hash 早退（节约 vision ~30s/轮）
                # 仿 P2-A sticky_thread.hash_diff_enabled，对 inbox 做相同优化。
                # 配置 inbox_hash_diff.enabled (默认 True) + full_check_after_n_idle (30)
                if not self._check_inbox_changed(inbox_png, serial, result):
                    result["step"] = "inbox_idle"
                    result["ok"] = True
                    logger.info(
                        "[messenger_rpa] 💤 inbox_idle hash unchanged "
                        "idle_count=%s (skip vision, save ~30s)",
                        result.get("inbox_idle_count"),
                    )
                    return self._finish(result, t0)

                # ── inbox guard + 未读扫描 ──
                if self._use_combined_vision:
                    guard, unread = await self._inbox_combined(inbox_png, result)
                    # P-XML fallback (2026-05-04)：vision 全失败时用 XML
                    # dump_inbox_rows 兜底——u2 让 inbox row content-desc
                    # 稳定可读，是 last-resort ground truth
                    if not unread:
                        xml_unread = self._xml_inbox_unread_fallback(serial)
                        if xml_unread:
                            result.setdefault("hints", []).append(
                                f"xml_inbox_fallback:{len(xml_unread)}rows"
                            )
                            logger.warning(
                                "[messenger_rpa] vision 全失败，XML inbox 兜底 "
                                "返 %d 条: %s",
                                len(xml_unread),
                                [(r.name, r.preview[:30]) for r in xml_unread],
                            )
                            unread = xml_unread
                else:
                    guard = await self._handle_guard(
                        serial, inbox_png, result, "inbox"
                    )
                    if guard.needs_human:
                        result["step"] = "guard_needs_human"
                        result["error"] = f"profile_picker:{guard.title}"
                        return self._finish(result, t0)
                    if guard.type != "none":
                        wh_now = self._screen_size(serial)
                        tx, ty = cc.TAB_CHATS.at(*wh_now)
                        adb.input_tap(serial, tx, ty)
                        await asyncio.sleep(0.6)
                        inbox_png = await self._screenshot(
                            serial, "inbox_retry", run_id
                        )
                        if not inbox_png:
                            result["step"] = "screenshot_inbox_retry_failed"
                            return self._finish(result, t0)
                        result["screenshot_path"] = inbox_png
                    unread = await self._scan_inbox(inbox_png, result)
                if guard.needs_human:
                    result["step"] = "guard_needs_human"
                    result["error"] = f"profile_picker:{guard.title}"
                    return self._finish(result, t0)
                if guard.type != "none" and self._use_combined_vision:
                    # combined 模式下若识别到 modal，先尝试闪避再重扫
                    handled = self._apply_guard_action(serial, guard)
                    if handled:
                        await asyncio.sleep(0.6)
                        inbox_png = await self._screenshot(
                            serial, "inbox_retry", run_id
                        )
                        if inbox_png:
                            result["screenshot_path"] = inbox_png
                            guard, unread = await self._inbox_combined(
                                inbox_png, result, retry=True
                            )
                # Vision 偶发漏掉首屏外未读：上滑列表再扫（与 send_to_chat_name 同手势）
                guard, unread = await self._run_once_scroll_rescan_if_no_unread(
                    serial, wh, run_id, result, guard, unread,
                )
                if getattr(guard, "needs_human", False):
                    result["step"] = "guard_needs_human"
                    result["error"] = (
                        f"profile_picker:{getattr(guard, 'title', '')}"
                    )
                    return self._finish(result, t0)
                if not unread:
                    result["step"] = "no_unread"
                    result["ok"] = True
                    return self._finish(result, t0)

                # 过滤永久跳过的 chat（spam 兜底命中过的、人工标记的）
                skipped_names: List[str] = []
                skipped_self_previews: List[str] = []
                # B1 修复：inbox 扫描跳过 messenger UI 上的 "Message request"
                # 按钮（陌生人请求）和 "Facebook user" 等无效 row。vision 经常
                # 把它们当成 chat row，但 main.py tap 进去会失败浪费 70s vision call
                _INVALID_INBOX_NAMES = {
                    "message request", "message requests",
                    "facebook user",  # 匿名/已注销账号
                }
                for c in unread:
                    _name_l = (c.name or "").strip().lower()
                    if _name_l in _INVALID_INBOX_NAMES:
                        logger.info(
                            "[messenger_rpa] skip invalid inbox row: name=%r",
                            c.name,
                        )
                        skipped_names.append(c.name)
                        continue
                    if target_names and not self._chat_name_matches_any(
                        c.name, target_names
                    ):
                        skipped_names.append(c.name)
                        continue
                    if is_outbound_or_draft_preview(c.preview):
                        logger.info(
                            "[messenger_rpa] skip inbox row with outbound/draft preview: "
                            "name=%r preview=%r",
                            c.name, (c.preview or "")[:80],
                        )
                        skipped_names.append(c.name)
                        skipped_self_previews.append(c.name)
                        continue
                    ck = f"{self._chat_key_prefix}:{c.name}"
                    if self._state.is_skipped_chat(ck):
                        logger.info(
                            "[messenger_rpa] skip chat (in skipped_chats): %r", c.name
                        )
                        skipped_names.append(c.name)
                        continue
                    # ★ 短期冷却：进入 thread 后发现 self-sent → 暂时跳过
                    _ss_key = _self_skip_norm_key(c.name)
                    _ss_until = self._self_skip_until.get(_ss_key, 0.0)
                    if _ss_until > time.monotonic():
                        logger.warning(
                            "[messenger_rpa] skip chat (self_skip cooldown %.0fs): %r key=%r",
                            _ss_until - time.monotonic(), c.name, _ss_key,
                        )
                        skipped_names.append(c.name)
                        continue
                    # ★ P16-IL (2026-05-04)：chat 级 self_overlap 长冷却前移到 inbox。
                    # 与 thread 内兜底（_chat_overlap_skip_until 检查）形成双层。
                    # 命中即跳过整个 tap → 截屏 → vision → exit_thread 流程，
                    # 单次省 100~200s，连续幻觉 chat 不再阻塞其他对话。
                    _co_until = float(
                        self._chat_overlap_skip_until.get(ck, 0.0) or 0.0
                    )
                    if _co_until > time.monotonic():
                        # P16-IL2 (2026-05-04) 逃逸机制：长冷却期内若 inbox
                        # preview 与已 skip 的 thread peer 文本明显不同（相似度
                        # < threshold，默认 0.6），视为真新消息到达 → 立即解除
                        # 冷却 + 重置 streak。指纹保留作为 D 层兜底。
                        # 没有已 skip 文本指纹时，保守维持冷却。
                        _esc_thr = float(
                            self._cfg.get(
                                "chat_overlap_inbox_escape_threshold", 0.6,
                            ) or 0.0
                        )
                        _esc_preview = (c.preview or "").strip()
                        _skipped_for_chat = list(
                            self._skipped_peer_text_per_chat.get(ck) or []
                        )
                        _max_sim = 0.0
                        if _esc_preview and _skipped_for_chat and _esc_thr > 0:
                            for _sk_text in _skipped_for_chat:
                                _r = _self_reply_overlap_ratio(
                                    _sk_text, _esc_preview,
                                )
                                if _r > _max_sim:
                                    _max_sim = _r
                                    if _max_sim >= 1.0:
                                        break
                        _can_escape = (
                            _esc_thr > 0
                            and _esc_preview
                            and _skipped_for_chat
                            and _max_sim < _esc_thr
                        )
                        if _can_escape:
                            self._chat_overlap_skip_until.pop(ck, None)
                            self._self_overlap_skip_streak.pop(ck, None)
                            result.setdefault("hints", []).append(
                                f"chat_overlap_inbox_escape:sim={_max_sim:.2f}"
                            )
                            logger.warning(
                                "[messenger_rpa] chat_overlap 长冷却逃逸 "
                                "chat=%r preview=%r max_sim=%.2f<%.2f → "
                                "解除冷却放行",
                                c.name, _esc_preview[:60], _max_sim, _esc_thr,
                            )
                            # 不 continue，继续走后续 cooldown 检查
                        else:
                            logger.warning(
                                "[messenger_rpa] skip chat (chat_overlap_cooldown "
                                "%.0fs left, max_sim=%.2f): %r",
                                _co_until - time.monotonic(), _max_sim, c.name,
                            )
                            result.setdefault("hints", []).append(
                                "chat_overlap_inbox_skip"
                            )
                            skipped_names.append(c.name)
                            continue
                    # ★ P23 (2026-05-04) vision 三信号 + 最近发过守卫 ──
                    # vision INBOX_COMBINED 已输出 name_bold/preview_bold/blue_dot
                    # 三个未读视觉特征。当三个全 F + 我们最近 N 秒内（默认 60s）
                    # 发过消息时，大概率是 vision 把已读列入 unread 的"前进式
                    # 误识"（lowmemkill 后 inbox 缓存陈旧）。
                    # 保守设计：60s 窗口外不 skip（不破坏"双机互发联调"边界）。
                    _p23_window = float(self._cfg.get(
                        "no_unread_signal_recent_window_sec", 60.0,
                    ) or 0.0)
                    if (
                        _p23_window > 0
                        and c.unread_signals_count == 0
                    ):
                        try:
                            _cs_p23 = self._state.get_chat_state(ck)
                            _last_sent_p23 = float(
                                _cs_p23.get("last_sent_at") or 0.0
                            )
                        except Exception:
                            _last_sent_p23 = 0.0
                        if (
                            _last_sent_p23 > 0
                            and (time.time() - _last_sent_p23) < _p23_window
                        ):
                            _gap_p23 = int(time.time() - _last_sent_p23)
                            logger.warning(
                                "[messenger_rpa] skip chat (vision "
                                "unread_signals=0 + last_sent_ago=%ds < %ds)"
                                ": %r",
                                _gap_p23, int(_p23_window), c.name,
                            )
                            result.setdefault("hints", []).append(
                                f"no_unread_signal_skip:gap={_gap_p23}s"
                            )
                            skipped_names.append(c.name)
                            continue

                    # ★ P0-1: 前移 companion_reply_cooldown_sec 检查到 inbox 阶段。
                    # 之前该检查仅在 L2014（thread 内）兜底，导致同一会话每轮被
                    # tap 进 thread → vision 读屏 → 退出，浪费 5-10s/轮，并使其他
                    # 会话排队等待。此处提前判定，命中冷却直接 skip 不进 thread。
                    # 保留 L2014 thread 内兜底用于 sticky / fast_path 绕过 inbox 的路径。
                    _reply_cd_raw_v = self._cfg.get(
                        "companion_reply_cooldown_sec", 300
                    )
                    try:
                        _reply_cd_v = float(
                            300 if _reply_cd_raw_v is None or _reply_cd_raw_v == ""
                            else _reply_cd_raw_v
                        )
                    except (TypeError, ValueError):
                        _reply_cd_v = 300.0
                    if _reply_cd_v > 0:
                        try:
                            _cs_v = self._state.get_chat_state(ck)
                            _last_sent_v = float(_cs_v.get("last_sent_at") or 0)
                        except Exception:
                            _last_sent_v = 0.0
                        if _last_sent_v > 0:
                            _elapsed_v = time.time() - _last_sent_v
                            if _elapsed_v < _reply_cd_v:
                                logger.info(
                                    "[messenger_rpa] skip chat (inbox-stage "
                                    "reply_cooldown %.0fs left/%.0fs total): %r",
                                    _reply_cd_v - _elapsed_v, _reply_cd_v, c.name,
                                )
                                skipped_names.append(c.name)
                                continue
                    target = c
                    break
                if target is None and target_names:
                    for wanted_name in target_names:
                        target = self._pick_unread_row_for_peer(
                            unread, wanted_name, result,
                        )
                        if target is not None:
                            result.setdefault("hints", []).append(
                                "run_once_target:matched_from_ranking"
                            )
                            break
                result["unread_names"] = [c.name for c in unread]
                result["skipped_names"] = skipped_names
                if skipped_self_previews:
                    result["skipped_self_previews"] = skipped_self_previews
                # P-XML fallback (2026-05-04)：vision 给的 ranking 全部被
                # cooldown/self skip，但 XML 可能看到 vision 漏读的 row（如
                # vision 把 cooldown chat 排错位置，真正 unread 在别处）。
                # 用 XML inbox 兜底找 vision 没拿到的 chat name 试一次。
                if target is None and unread:
                    try:
                        xml_unread = self._xml_inbox_unread_fallback(serial)
                    except Exception:
                        xml_unread = []
                    _existing_names_low = {
                        (c.name or "").strip().lower() for c in unread
                    }
                    _xml_extras = [
                        c for c in xml_unread
                        if (c.name or "").strip().lower()
                        not in _existing_names_low
                    ]
                    if _xml_extras:
                        result.setdefault("hints", []).append(
                            f"xml_inbox_supplement:{len(_xml_extras)}rows"
                        )
                        logger.warning(
                            "[messenger_rpa] vision ranking 全 skip，XML 补 %d 条: %s",
                            len(_xml_extras),
                            [c.name for c in _xml_extras],
                        )
                        # 直接 pick 第一个非 cooldown 的 chat
                        for cand in _xml_extras:
                            ss_key = _self_skip_norm_key(cand.name)
                            if self._self_skip_until.get(ss_key, 0.0) <= time.monotonic():
                                target = cand
                                break
                if target is None:
                    result["step"] = "all_unread_skipped"
                    result["ok"] = True
                    return self._finish(result, t0)

            chat_key = self._chat_key_for(target.name)  # P0-E2 OCR 容忍
            result["chat_key"] = chat_key
            result["chat_name"] = target.name

            # ★ 跨账号互斥：同一用户同时只能由一个账号处理，防止双账号并发聊天
            _coord_ext_id = (target.name or "").strip()
            _coord_my_aid = str(getattr(self, "_account_id", "") or "default")
            if self._coordinator is not None and _coord_ext_id:
                if not self._coordinator.try_lock(_coord_ext_id, _coord_my_aid):
                    _holder = self._coordinator.active_chat_holder(_coord_ext_id)
                    result["step"] = "chat_locked_by_other_account"
                    result["ok"] = True
                    result["locked_by"] = _holder
                    logger.info(
                        "[messenger_rpa] account=%s chat=%s 被 %s 占用，本轮跳过",
                        _coord_my_aid, _coord_ext_id, _holder,
                    )
                    return self._finish(result, t0)
                result["_coord_lock_held"] = _coord_ext_id

            if not thread_png:
                # ★ 二次确认 row_index：用名字独立问 vision，避免 combined/fallback 猜错行
                # （vision 输出 row_index 时任务繁重；单任务可精确些）
                if bool(self._cfg.get("resolve_row_by_name", True)):
                    try:
                        confirmed_idx = await resolve_row_by_name(
                            inbox_png,
                            target.name,
                            vision_cfg=self._vision_cfg(),
                            global_vision=self._global_vision_cfg(),
                        )
                        if confirmed_idx is not None and confirmed_idx != target.row_index:
                            if target.row_index == 0 and confirmed_idx > 0:
                                result.setdefault("hints", []).append(
                                    f"row_resolve_ignored:first_row {target.row_index}->{confirmed_idx}"
                                )
                                confirmed_idx = None
                        if confirmed_idx is not None and confirmed_idx != target.row_index:
                            logger.info(
                                "[messenger_rpa] row_index 修正 %r: %d → %d (vision 单问)",
                                target.name, target.row_index, confirmed_idx,
                            )
                            target = UnreadChat(
                                name=target.name,
                                preview=target.preview,
                                time=target.time,
                                row_index=confirmed_idx,
                                y_percent=target.y_percent,
                                quality_hint=target.quality_hint,
                                score=target.score,
                                skip_inbox_tap=target.skip_inbox_tap,
                            )
                            result["row_index_resolved"] = confirmed_idx
                    except Exception:
                        logger.debug("resolve_row_by_name 失败（非致命）", exc_info=True)

                _tap_rv = self._tap_chat_row(serial, wh, target)
                _tap_src = (_tap_rv[2] if _tap_rv else "unknown")
                result["tap_src"] = _tap_src
                # ★ inbox self-sent guard: 自己发的最后一条 → 跳过这个会话
                if _tap_rv and _tap_rv[2] == "inbox_self_sent_skip":
                    result["step"] = "inbox_self_sent_skip"
                    result["ok"] = True
                    logger.info(
                        "[messenger_rpa] skip chat=%r: latest is self-sent",
                        target.name,
                    )
                    self._exit_thread(serial)
                    return self._finish(result, t0)
                # 等会话页加载（含淡入动画 + 历史消息渲染）
                await asyncio.sleep(jitter_ms(800, 1500))

                thread_png = await self._screenshot(serial, "thread", run_id)
                if not thread_png:
                    result["step"] = "screenshot_thread_failed"
                    return self._finish(result, t0)
                result["screenshot_path"] = thread_png

                # ★ calibration 自愈：tap 完之后若仍看到多行头像（=还在 Inbox
                # 列表页，说明坐标点偏了），清校准缓存 + 重扫 + 再 tap 一次
                if bool(self._cfg.get("calib_selfheal", True)):
                    selfheal_info = await self._thread_open_selfheal(
                        serial, wh, target, thread_png, run_id, result,
                    )
                    if selfheal_info.get("retried"):
                        thread_png = selfheal_info.get("new_png") or thread_png
                        result["screenshot_path"] = thread_png

                _original_vision_name = target.name  # 保存 vision 原名，用于 self-skip cooldown
                actual_title = self._thread_title_from_xml(serial, result)
                if not actual_title:
                    # 传 target_name 启用 vision title cache —— 同 chat 30s 内
                    # 命中可直接跳过 5–15s 的 OCR（lowmemkill 场景下每 cycle 都要走）。
                    actual_title = await self._thread_title_from_vision(
                        serial, result, reason="after_tap",
                        target_name=target.name,
                    )
            if (
                not actual_title
                and bool(self._cfg.get("search_on_thread_title_missing", True))
                and (target.name or "").strip()
            ):
                result.setdefault("hints", []).append(
                    "thread_title_missing_after_tap:search_fallback"
                )
                searched = await self._search_chat_by_name(
                    serial, wh, target.name or "", result,
                )
                if searched is not None:
                    target = searched
                    thread_png2 = await self._screenshot(
                        serial, "thread_search_recovered", run_id,
                    )
                    if thread_png2:
                        thread_png = thread_png2
                        result["screenshot_path"] = thread_png
                    actual_title = self._thread_title_from_xml(serial, result)
                    if actual_title:
                        result["thread_title_search_recovered"] = actual_title
            # ★ P0 fallback：UI XML tap 像素级精确（ui_inbox_scraper 已验证过
            #   row 内容），但 MIUI lowmemkill 把 uiautomator dump 杀掉时 XML
            #   持续返空，Vision fallback 也常超时，搜索 fallback 同样依赖
            #   dump 而失效。这种结构性失败下信任 ui_xml/ 来源的 target.name
            #   作为 actual_title，避免系统完全无法发送任何消息。
            #
            # ★ 扩展信任：tap_src=calibrated_*（基于精确校准的公式坐标）也是
            #   像素精确（与 ui_xml 同级），同样可信任。另外当 target.name 命中
            #   ocr_drift_accept_names 白名单时（运营明确指定的客户），无论
            #   tap_src 来源如何都信任 — 这是避开 thread title 验证 + OCR 漂移
            #   双失败下的最后兜底。
            _tap_src_for_trust = result.get("tap_src", "")
            _trust_by_src = (
                _tap_src_for_trust.startswith("ui_xml/")
                or _tap_src_for_trust.startswith("calibrated")
            )
            _accept_names_trust = (
                self._cfg.get("ocr_drift_accept_names")
                or self._cfg.get("ocr_drift_accepted_chat_names")
                or []
            )
            if isinstance(_accept_names_trust, str):
                _accept_names_trust = [_accept_names_trust]
            _trust_by_whitelist = bool(
                _accept_names_trust
                and (target.name or "").strip()
                and self._chat_name_matches_any(target.name, _accept_names_trust)
            )
            if (
                not actual_title
                and (_trust_by_src or _trust_by_whitelist)
                and bool(self._cfg.get("trust_xml_tap_target_name", True))
                and (target.name or "").strip()
            ):
                actual_title = target.name
                result["thread_title_xml"] = actual_title
                _reason = (
                    "src" if _trust_by_src else "whitelist"
                )
                result.setdefault("hints", []).append(
                    f"trust_xml_tap_target_name:{_reason}"
                )
                logger.warning(
                    "[messenger_rpa] thread title 双 fallback 失败 + tap_src=%r "
                    "(reason=%s) → 信任 target.name=%r 作为 actual_title",
                    _tap_src_for_trust, _reason, target.name,
                )
            if (
                not actual_title
                and bool(self._cfg.get("require_thread_title_before_reply", True))
            ):
                result["step"] = "not_in_thread_after_tap"
                result["ok"] = True
                result["error"] = (
                    f"could not verify thread title for {target.name!r}; "
                    "skip reply to avoid sending from inbox/search page"
                )
                logger.warning(
                    "[messenger_rpa] 未确认进入目标会话，跳过回复 chat=%r",
                    target.name,
                )
                self._exit_thread(serial)
                return self._finish(result, t0)
            # ★ 错误聊天检测 + 回滚：仅当 tap 用了公式坐标时才检查
            # UI XML 坐标是像素级精确的，不会点错行；OCR 名字太不稳定会产生误报
            _tap_src = result.get("tap_src", "")
            _is_formula_tap = not _tap_src.startswith("ui_xml/")
            # ★ Vision OCR 漂移容忍：如果 actual_title（thread XML/Vision 真名）
            #   命中了运营配置的 target_chat_names（白名单），即使与 Vision 在
            #   inbox 阶段给出的 target.name 不一致，也认为这是 OCR 漂移导致的
            #   误判（如英文名"Victor Zan"被错读成"さとう たかひろ"），接受当前
            #   会话不进 rollback。后续 title 修正阶段会把 target.name 改对。
            _accept_due_to_target_match = False
            if actual_title and actual_title != (target.name or "") and _is_formula_tap:
                # 优先看独立的 ocr_drift_accept_names（专用容忍白名单），
                # 没配则 fallback 到 target_chat_names（运行时白名单）
                _accept_names = (
                    self._cfg.get("ocr_drift_accept_names")
                    or self._cfg.get("ocr_drift_accepted_chat_names")
                    or self._run_once_target_names()
                    or []
                )
                if isinstance(_accept_names, str):
                    _accept_names = [_accept_names]
                if _accept_names and self._chat_name_matches_any(
                    actual_title, _accept_names
                ):
                    _accept_due_to_target_match = True
                    logger.warning(
                        "[messenger_rpa] OCR 漂移容忍：vision_target=%r 实际=%r "
                        "命中 ocr_drift_accept_names → 接受会话，不 rollback",
                        target.name, actual_title,
                    )
                    result.setdefault("hints", []).append(
                        "ocr_drift_target_name_accept"
                    )
            if (
                actual_title and actual_title != (target.name or "")
                and _is_formula_tap and not _accept_due_to_target_match
            ):
                _norm_actual = _self_skip_norm_key(actual_title)
                _norm_target = _self_skip_norm_key(_original_vision_name)
                if _norm_actual != _norm_target:
                    logger.warning(
                        "[messenger_rpa] ⚠ 进入了错误聊天！目标=%r(key=%r) "
                        "实际=%r(key=%r) → 回滚退出，不触碰输入框",
                        _original_vision_name, _norm_target,
                        actual_title, _norm_actual,
                    )
                    result["step"] = "wrong_chat_rollback"
                    result["ok"] = True
                    result["error"] = (
                        f"entered {actual_title!r} instead of "
                        f"{_original_vision_name!r}"
                    )
                    _cd = float(self._cfg.get("wrong_chat_cooldown_sec", 60) or 60)
                    _now_m = time.monotonic() + _cd
                    self._self_skip_until[_norm_target] = _now_m
                    self._self_skip_until[_norm_actual] = _now_m
                    # 失效 vision title cache —— 否则 30s TTL 内反复返回错的
                    # cached title，wrong_chat 自我循环。
                    self._title_vision_cache.pop(
                        (serial, (_original_vision_name or "").strip()), None,
                    )
                    self._exit_thread(serial)
                    return self._finish(result, t0)
            # ★ title 修正：无论 tap 来源，只要 XML actual_title 与 Vision name
            #   不同就用 XML 结果修正 target — 保证 chat_key 一致，去重才有效
            if actual_title and actual_title != (target.name or ""):
                result.setdefault("hints", []).append(
                    f"thread_title_corrected:{target.name}->{actual_title}"
                )
                target_names = self._run_once_target_names()
                if target_names and not self._chat_name_matches_any(actual_title, target_names):
                    result["step"] = "target_title_mismatch_skip"
                    result["ok"] = True
                    result["error"] = (
                        f"opened {actual_title!r}, not in target_chat_names"
                    )
                    self._exit_thread(serial)
                    return self._finish(result, t0)
                old_lock = result.pop("_coord_lock_held", None)
                my_aid = str(getattr(self, "_account_id", "") or "default")
                if self._coordinator is not None and old_lock:
                    try:
                        self._coordinator.unlock(str(old_lock), my_aid)
                    except Exception:
                        logger.debug("coordinator unlock old title failed", exc_info=True)
                if self._coordinator is not None and actual_title:
                    if not self._coordinator.try_lock(actual_title, my_aid):
                        holder = self._coordinator.active_chat_holder(actual_title)
                        result["step"] = "chat_locked_by_other_account"
                        result["ok"] = True
                        result["locked_by"] = holder
                        self._exit_thread(serial)
                        return self._finish(result, t0)
                    result["_coord_lock_held"] = actual_title
                target = UnreadChat(
                    name=actual_title,
                    preview=target.preview,
                    time=target.time,
                    row_index=target.row_index,
                    y_percent=target.y_percent,
                    quality_hint=target.quality_hint,
                    score=target.score,
                    skip_inbox_tap=target.skip_inbox_tap,
                )
                chat_key = self._chat_key_for(target.name)  # P0-E2 OCR 容忍
                result["chat_key"] = chat_key
                result["chat_name"] = target.name
                logger.warning(
                    "[messenger_rpa] title 修正: vision=%r → xml=%r chat_key=%s",
                    _original_vision_name, actual_title, chat_key,
                )
                # P3-FIX-1: vision misroute guard
                # vision 反复把 victor 错放 row=1，main.py tap row=1 就进了
                # 别的 chat（如 うめだゆうき）。即使本轮 step=no_peer_message
                # 早退，几分钟后 vision 又会重复同样错误。给"非粘性 actual_title"
                # 设较长 cooldown，inbox 阶段就拦下避免浪费 vision/LLM。
                vmg_cfg = self._cfg.get("vision_misroute_guard") or {}
                if vmg_cfg.get("enabled", True):
                    only_when_sticky = bool(
                        vmg_cfg.get("only_when_vision_target_was_sticky", True)
                    )
                    # 只在 vision 目标本是粘性 chat（如 Victor Zan）但实际进了
                    # 非粘性 chat 时触发（确认是 vision 路由错误，不是正常处理）
                    vision_was_sticky = self._is_sticky_chat(_original_vision_name)
                    actual_is_sticky = self._is_sticky_chat(actual_title)
                    # 2026-05-04 fix1：跳过 row_N 类 vision fallback 标签——
                    # vision 偶发只返回 row_0/row_1 (没识别名字)，被 guard 当
                    # 成"误路由"反复给真 chat 设 600s cooldown。
                    import re as _re
                    _vt = (_original_vision_name or "").strip()
                    _is_row_idx = bool(
                        _re.match(r"^(row_\d+|row\d+|none|null|unknown)$",
                                  _vt, _re.IGNORECASE)
                    )
                    # 2026-05-04 fix2 (P2++I1)：fuzzy name match——vision OCR
                    # 偶发名字错一字（'野木'→'野末'），但 row 是对的。用
                    # SequenceMatcher.ratio 判 vision name 和 actual name 是
                    # 否"基本相同"。≥0.5 视为 OCR 偏差，不触发 guard。
                    # 'Victor Zan' vs '野末' ratio≈0  → 真误路由 → guard 触发
                    # '野木' vs '野末'      ratio=0.5 → OCR 偏差 → 不触发
                    fuzzy_threshold = float(
                        vmg_cfg.get("fuzzy_name_ratio_threshold", 0.5)
                    )
                    name_similar = False
                    try:
                        from difflib import SequenceMatcher as _SM
                        if _vt and actual_title:
                            _ratio = _SM(
                                None,
                                _vt.strip().lower(),
                                actual_title.strip().lower(),
                            ).ratio()
                            name_similar = _ratio >= fuzzy_threshold
                    except Exception:
                        pass
                    should_guard = (
                        not actual_is_sticky
                        and (vision_was_sticky or not only_when_sticky)
                        and bool(_vt)
                        and not _is_row_idx
                        and not name_similar
                    )
                    if should_guard:
                        cd = float(vmg_cfg.get("cooldown_sec", 600) or 600)
                        ss_key = _self_skip_norm_key(actual_title)
                        self._self_skip_until[ss_key] = time.monotonic() + cd
                        result.setdefault("hints", []).append(
                            f"vision_misroute_guard:{actual_title}_cd={int(cd)}s"
                        )
                        logger.warning(
                            "[messenger_rpa] 🛡 vision misroute guard: vision "
                            "target=%r 实际进入非粘性 chat=%r → 给 %r 设 %ds "
                            "cooldown（防 vision 反复选错浪费 vision call）",
                            _original_vision_name, actual_title, actual_title, int(cd),
                        )

            if bool(self._cfg.get("pre_thread_self_xml_guard", True)):
                if self._latest_thread_snippet_is_self(serial, result):
                    result["step"] = "self_latest_xml_prevision_skip"
                    result["ok"] = True
                    logger.warning(
                        "[messenger_rpa] 线程内 XML 检测自发，跳过 chat=%r step=self_latest_xml_prevision_skip",
                        target.name,
                    )
                    _cd = float(self._cfg.get("self_skip_cooldown_sec", 30) or 30)
                    _now_m = time.monotonic() + _cd
                    self._self_skip_until[_self_skip_norm_key(target.name)] = _now_m
                    self._self_skip_until[_self_skip_norm_key(_original_vision_name)] = _now_m
                    self._exit_thread(serial)
                    return self._finish(result, t0)

            # P2-A：粘性 thread 入口 hash diff 早退（**真"几秒回"** 的核心）
            # 仅对粘性 chat + 已在 thread 内 + 已有上次 hash 时早退。
            # 没新消息 → step=sticky_idle ok=True 立即返回（不调 vision）。
            # service.py 主循环看到 sticky_idle 会用 1.5s 短间隔继续 poll。
            # 这把响应延迟从 30-50s 降到 5-10s（hash diff 1-2s + vision 5-10s）。
            if self._is_sticky_chat(target.name) and thread_png:
                if not self._check_sticky_thread_changed(
                    thread_png, target.name, result,
                ):
                    result["step"] = "sticky_idle"
                    result["ok"] = True
                    result["chat_name"] = target.name
                    result["chat_key"] = chat_key or f"{self._chat_key_prefix}:{target.name}"
                    # 不调 _exit_thread，保持粘性
                    logger.warning(
                        "[messenger_rpa] 💤 sticky_idle chat=%r idle_count=%s "
                        "（hash 不变，跳过 vision，下轮 1.5s 后再 check）",
                        target.name, result.get("sticky_idle_count"),
                    )
                    return self._finish(result, t0)
                # 🛡 P3-X：统一 send gate（取代 v3 的散落 cooldown 检查）
                # sticky hash changed 后用统一 gate 决定能否进 vision/LLM。
                # 这是 4 次疯狂事件的根本架构修复 — 所有 cooldown 决策走同一处。
                _gate_reason = self._should_skip_send(
                    target.name, source="sticky_hash_changed",
                )
                if _gate_reason:
                    result["step"] = "sticky_gate_skip"
                    result["ok"] = True
                    result["gate_reason"] = _gate_reason
                    result["chat_name"] = target.name
                    result["chat_key"] = chat_key or f"{self._chat_key_prefix}:{target.name}"
                    return self._finish(result, t0)
                # P3-C：hash 变化（victor 可能发新消息）→ 立刻提前 typing warmup
                # 让 victor 在整个 vision + LLM 思考期间持续看到"对方正在输入..."
                # 而不只是最后发送前 1.5s 闪一下。
                try:
                    await self._typing_indicator_early_warmup(
                        serial, target.name, result,
                    )
                except Exception:
                    logger.debug(
                        "[messenger_rpa] early typing warmup failed",
                        exc_info=True,
                    )

            # ── P19 (2026-05-04) bubble_detector 前置短路 ──
            # 与 P1-C XML 守卫并列，覆盖 XML Litho 顶栏不暴露 "You:" 前缀。
            # bubble_detector 像素扫描 ≈ 100-300ms，仅在最近发过时调用以避免
            # happy path（peer 新消息）浪费——peer 新消息时 bubble=peer，
            # 不会触发 self skip，提前查 last_sent_at 让 bubble_detector 只在
            # 它可能发挥作用的场景跑。
            # 1.5h 生产数据：4 次 vision_misread 全部 bubble=self → 预期 100%
            # 拦截 vision 误读源头。bubble=peer/unknown 时仍走 vision。
            if thread_png and bool(
                self._cfg.get("bubble_pre_vision_guard_enabled", True)
            ):
                _ck_pv = (
                    chat_key or f"{self._chat_key_prefix}:{target.name}"
                )
                try:
                    _cs_pv = self._state.get_chat_state(_ck_pv)
                    _last_sent_pv = float(_cs_pv.get("last_sent_at") or 0.0)
                except Exception:
                    _last_sent_pv = 0.0
                # P19-FIX (2026-05-04 emergency)：window 紧缩到 120s（与
                # post_send_cooldown_sec 对齐）。
                # 历史 v2 升到 3600s 的假设是"bubble=self 已确认最底部仍是
                # 我们的"——但生产观察到 last_sent_ago=2443s（40 分钟前）
                # 仍触发 self-skip，导致 peer 真回复后 bot 不应答的 bug：
                # bubble_detector 扫描区 [45%, 85%]，peer 新气泡可能在
                # [85%, 100%] 被键盘/输入栏遮挡 → bubble_detector 看不到
                # 新 peer 气泡 → 误判 self → 真消息被吞。
                # 120s 内才信任 bubble=self（post-send 期内 vision 也容易
                # 误读，bubble 是更可靠源），过 120s 必须走 vision 路径
                # 让模型完整看屏（含被遮挡区域的 hint）。
                #
                # P24 (2026-05-04) 上限保护：window 配置 > 上限（默认 300s）
                # 时强制 clamp + WARNING 一次。防御"配置错乱重演 P19 事故"。
                _pv_window_raw = float(self._cfg.get(
                    "bubble_pre_vision_recent_window_sec", 120.0,
                ) or 0.0)
                _pv_window_max = float(self._cfg.get(
                    "bubble_pre_vision_window_max_sec", 300.0,
                ) or 300.0)
                if (
                    _pv_window_max > 0
                    and _pv_window_raw > _pv_window_max
                ):
                    if not self._pv_window_clamp_warned:
                        logger.warning(
                            "[messenger_rpa] P24 ⚠️ "
                            "bubble_pre_vision_recent_window_sec=%.0fs "
                            "> 上限 %.0fs，clamp 到 %.0fs。"
                            "原因：bubble_detector 扫描区 [45%%-85%%] 看不"
                            "到键盘/输入栏遮挡的 peer 新气泡，window 过长会"
                            "把 peer 真消息误判 self 拦截（P19 历史事故）。",
                            _pv_window_raw, _pv_window_max, _pv_window_max,
                        )
                        self._pv_window_clamp_warned = True
                    _pv_window = _pv_window_max
                else:
                    _pv_window = _pv_window_raw
                _pv_within_window = (
                    _last_sent_pv > 0
                    and _pv_window > 0
                    and (time.time() - _last_sent_pv) < _pv_window
                )
                if _pv_within_window:
                    try:
                        from src.integrations.messenger_rpa.bubble_detector \
                            import detect_latest_sender as _pv_detect
                        _pv_sender, _pv_info = _pv_detect(thread_png)
                        result["pre_vision_bubble_sender"] = _pv_sender
                        result["pre_vision_bubble_info"] = _pv_info
                        if _pv_sender == "self":
                            _gap = int(time.time() - _last_sent_pv)
                            result["step"] = "bubble_pre_vision_self_skip"
                            result["ok"] = True
                            result["error"] = (
                                f"bubble_pre_vision: self confirmed, "
                                f"last_sent_ago={_gap}s < "
                                f"{int(_pv_window)}s window"
                            )
                            result.setdefault("hints", []).append(
                                "bubble_pre_vision_self_skip"
                            )
                            _cd_pv = float(
                                self._cfg.get("self_skip_cooldown_sec", 30)
                                or 30
                            )
                            _now_pv = time.monotonic() + _cd_pv
                            self._self_skip_until[
                                _self_skip_norm_key(target.name)
                            ] = _now_pv
                            if (
                                _original_vision_name
                                and _original_vision_name != target.name
                            ):
                                self._self_skip_until[
                                    _self_skip_norm_key(_original_vision_name)
                                ] = _now_pv
                            logger.warning(
                                "[messenger_rpa] P19 bubble 前置: self 确认 + "
                                "最近 %ds 内发过 → 跳过 vision chat=%r",
                                _gap, target.name,
                            )
                            self._exit_thread(serial)
                            return self._finish(result, t0)
                    except Exception:
                        logger.debug("P19 bubble_pre_vision 异常", exc_info=True)

            if self._use_combined_vision:
                # ★ P3-3：乐观并发 — 同时启动 thread_combined 和 caption
                # kind!=image 时 caption 浪费一次 vision 调用但不浪费墙上时间；
                # kind==image 时省 ~2500ms（caption 已经在跑）
                cap_task: Optional[asyncio.Task] = None
                if self._should_prefetch_caption():
                    cap_task = asyncio.create_task(
                        self._try_describe_peer_image(
                            thread_png,
                            timeout_sec=self._deep_timeout(),
                        ),
                        name=f"mrpa_caption_{run_id}",
                    )
                    result["caption_prefetch"] = True
                # ★ P3-4：phase 耗时统计
                _tp = time.monotonic()
                guard2, peer_msg = await self._thread_combined(thread_png, result)
                result.setdefault("phase_ms", {})["thread_vision"] = int(
                    (time.monotonic() - _tp) * 1000
                )
                vision_tag = result.get("thread_vision_tag", "")
                # 将 cap_task 挂到 result，_generate_reply 里能直接 await
                if cap_task is not None:
                    result["_cap_task"] = cap_task
            else:
                guard2 = await self._handle_guard(
                    serial, thread_png, result, "thread"
                )
                if guard2.type != "none" and not guard2.needs_human:
                    thread_png = await self._screenshot(serial, "thread_retry", run_id)
                    if thread_png:
                        result["screenshot_path"] = thread_png
                peer_msg, vision_tag = await self._read_peer(thread_png, result)
            if guard2.needs_human:
                result["step"] = "guard_needs_human_thread"
                result["error"] = f"profile_picker:{guard2.title}"
                return self._finish(result, t0)

            # ★ 像素级气泡归属检测：独立于 Vision 和 XML 的第三验证源
            # P19：优先复用前置探测结果（vision 调用前已扫过一次，避免重复 50ms）
            _bubble_sender = result.get("pre_vision_bubble_sender") or "unknown"
            _bub_info = result.get("pre_vision_bubble_info") or {}
            if (
                _bubble_sender == "unknown"
                and thread_png
                and bool(self._cfg.get("bubble_detector_enabled", True))
            ):
                try:
                    from src.integrations.messenger_rpa.bubble_detector import (
                        detect_latest_sender,
                    )
                    _bubble_sender, _bub_info = detect_latest_sender(thread_png)
                except Exception:
                    logger.debug("bubble_detector 异常", exc_info=True)
            result["bubble_sender"] = _bubble_sender
            result["bubble_info"] = _bub_info
            # Hard guard: UI XML is more reliable than Vision for "who spoke last".
            # If the latest visible thread snippet starts with "You:" / "你:" etc.,
            # the newest message is ours, so do not let a Vision role mistake create
            # self-question/self-answer loops.
            # ★ 升级：加入 bubble_detector 交叉验证——
            #   XML says self + bubble says self → 确定自发，跳过
            #   XML says self + bubble says peer → XML 可能陈旧，信任 bubble，继续
            _xml_says_self = self._latest_thread_snippet_is_self(
                serial, result, peer_msg=peer_msg
            )
            if _xml_says_self:
                if _bubble_sender == "peer":
                    logger.warning(
                        "[messenger_rpa] XML 说自发但 bubble 检测到对方气泡 → "
                        "信任 bubble，继续处理 chat=%r",
                        target.name,
                    )
                    result.setdefault("hints", []).append(
                        "bubble_override_xml_self"
                    )
                else:
                    result["step"] = "self_latest_xml_skip"
                    result["ok"] = True
                    logger.warning(
                        "[messenger_rpa] XML+bubble 均确认自发，跳过 chat=%r "
                        "bubble=%s",
                        target.name, _bubble_sender,
                    )
                    _cd = float(self._cfg.get("self_skip_cooldown_sec", 30) or 30)
                    _now_m = time.monotonic() + _cd
                    self._self_skip_until[_self_skip_norm_key(target.name)] = _now_m
                    self._self_skip_until[_self_skip_norm_key(_original_vision_name)] = _now_m
                    self._exit_thread(serial)
                    return self._finish(result, t0)

            # ★ P2-A：系统事件（"You can now message..."、E2EE 通知等）fast-path
            # 跳过。不进 peer-retry，也不走 no_peer_message 通用分支 —— 给一个
            # 专属 step 方便看板与 metrics 区分，同时避免浪费 2 次截图重试。
            if peer_msg is not None and getattr(peer_msg, "is_system_event", False):
                result["step"] = "system_event_skip"
                result["ok"] = True
                result["peer_kind"] = "system_event"
                result.setdefault("hints", []).append(
                    f"system_event_skip:{(peer_msg.content or peer_msg.desc or '')[:60]}"
                )
                self._exit_thread(serial)
                return self._finish(result, t0)

            # ★ peer 多轮重试：thread 页有时第一次截图还在渲染（peer 气泡
            # 刚动画进场、未成 image），给 1~2 次再次捕捉的机会
            if not peer_msg or not peer_msg.is_peer_anything:
                max_retry = int(self._cfg.get("peer_retry_max", 2))
                waits = [0.7, 1.2]
                for i in range(max_retry):
                    await asyncio.sleep(waits[min(i, len(waits) - 1)])
                    retry_png = await self._screenshot(
                        serial, f"thread_peer_retry_{i+1}", run_id
                    )
                    if not retry_png:
                        continue
                    peer_msg, vision_tag = await self._read_peer(
                        retry_png, result
                    )
                    result.setdefault("hints", []).append(
                        f"peer_retry_{i+1}:"
                        f"got={bool(peer_msg and peer_msg.is_peer_anything)}"
                    )
                    if peer_msg and peer_msg.is_peer_anything:
                        thread_png = retry_png
                        result["screenshot_path"] = thread_png
                        break
                    # retry 里拿到的若也是系统事件，同样走 fast-path
                    if peer_msg is not None and getattr(
                        peer_msg, "is_system_event", False,
                    ):
                        result["step"] = "system_event_skip"
                        result["ok"] = True
                        result["peer_kind"] = "system_event"
                        result.setdefault("hints", []).append(
                            "system_event_skip:detected_in_peer_retry"
                        )
                        self._exit_thread(serial)
                        return self._finish(result, t0)
            if not peer_msg or not peer_msg.is_peer_anything:
                result["step"] = "no_peer_message"
                result["ok"] = True
                self._exit_thread(serial)
                return self._finish(result, t0)

            _xml_says_self2 = self._latest_thread_snippet_is_self(
                serial, result, peer_msg=peer_msg
            )
            if _xml_says_self2:
                if _bubble_sender == "peer":
                    logger.warning(
                        "[messenger_rpa] post-retry XML 说自发但 bubble 检测到对方 → "
                        "继续处理 chat=%r",
                        target.name,
                    )
                    result.setdefault("hints", []).append(
                        "bubble_override_xml_self_post_retry"
                    )
                else:
                    result["step"] = "self_latest_xml_skip"
                    result["ok"] = True
                    logger.warning(
                        "[messenger_rpa] post-retry XML+bubble 确认自发，跳过 chat=%r",
                        target.name,
                    )
                    _cd = float(self._cfg.get("self_skip_cooldown_sec", 30) or 30)
                    _now_m = time.monotonic() + _cd
                    self._self_skip_until[_self_skip_norm_key(target.name)] = _now_m
                    self._self_skip_until[_self_skip_norm_key(_original_vision_name)] = _now_m
                    self._exit_thread(serial)
                    return self._finish(result, t0)

            result["peer_text"] = peer_msg.to_text_for_ai()
            result["peer_kind"] = peer_msg.kind

            # Voice input should be interpreted before generic media ACK.
            # If ASR succeeds, the message becomes normal text and can use the
            # full AI/persona pipeline.  If it fails, keep kind=voice so the
            # existing media ACK + approval fallback handles it safely.
            if peer_msg.kind == "voice":
                transcript = await self._try_transcribe_peer_voice(
                    serial,
                    chat_name=target.name or "",
                    wh=wh,
                    result=result,
                )
                if transcript:
                    result["voice_transcript"] = transcript[:500]
                    peer_msg = PeerMessage(
                        role="peer",
                        kind="text",
                        content=transcript,
                        desc="",
                        raw=f"voice_transcript:{transcript[:300]}",
                    )
                    result["peer_text"] = transcript
                    result["peer_kind"] = "voice_transcript"

            # 二级 spam 过滤：消息正文级（inbox 是 preview 级，可能漏）
            # 优化 A：分级处理 — HIGH 一次永久 skip；LOW 只单次跳过不入永久表
            # 优化 B：已建立画像的客户即使 HIGH 命中也降级到单次跳过 + alert
            if bool(self._cfg.get("skip_spam", True)):
                _spam_hit, _spam_level, _spam_kw = peer_msg.spam_match()
                if _spam_hit:
                    _aid_for_wh = str(
                        getattr(self, "_account_id", "") or "default"
                    )
                    _wh, _wh_info = self._is_spam_whitelisted_contact(
                        _aid_for_wh, target.name or "",
                    )
                    if _spam_level == "high" and not _wh:
                        # 强信号 + 非白名单：赌博域名/IM 引流 → 永久 skip
                        try:
                            self._state.add_skipped_chat(
                                chat_key,
                                chat_name=target.name,
                                reason=f"msg_level_spam:high:{_spam_kw}"[:60],
                            )
                        except Exception:
                            logger.debug("add_skipped_chat 失败", exc_info=True)
                        result["step"] = "msg_level_spam_skip"
                        result["spam_reason"] = f"high:{_spam_kw}"
                    elif _spam_level == "high" and _wh:
                        # 老客户突发 HIGH spam → 单次跳过 + 留 alert，不污染永久表
                        result["step"] = "msg_level_spam_skip_once_whitelisted"
                        result["spam_reason"] = f"high_whitelisted:{_spam_kw}"
                        result["whitelist_info"] = _wh_info
                        logger.warning(
                            "[messenger_rpa] HIGH spam detected on whitelisted "
                            "contact (msg_in=%d, has_portrait=%s) — skipping "
                            "ONCE not permanent: chat=%s kw=%r preview=%s",
                            _wh_info.get("msg_in_count", 0),
                            _wh_info.get("has_portrait", False),
                            chat_key, _spam_kw,
                            (peer_msg.content or peer_msg.desc or "")[:80],
                        )
                    else:
                        # LOW (无论是否白名单)：单次跳过不污染永久表
                        result["step"] = "msg_level_spam_skip_once"
                        result["spam_reason"] = f"low:{_spam_kw}"
                        logger.info(
                            "[messenger_rpa] LOW-confidence spam, "
                            "skip once but NOT marking permanent: "
                            "chat=%s kw=%r preview=%s",
                            chat_key, _spam_kw,
                            (peer_msg.content or peer_msg.desc or "")[:80],
                        )
                    result["ok"] = True
                    _cd = float(self._cfg.get("spam_cooldown_sec", 3600) or 3600)
                    _now_m = time.monotonic() + _cd
                    self._self_skip_until[_self_skip_norm_key(target.name)] = _now_m
                    self._self_skip_until[_self_skip_norm_key(_original_vision_name)] = _now_m
                    self._exit_thread(serial)
                    return self._finish(result, t0)

            _chat_st = self._state.get_chat_state(chat_key)

            if self._stale_peer_after_recent_self_marker(
                _chat_st, peer_msg, result,
            ):
                result["step"] = "stale_peer_after_self_skip"
                result["ok"] = True
                result["error"] = (
                    "vision_repeated_previous_peer_after_recent_self_reply"
                )
                logger.warning(
                    "[messenger_rpa] skip stale peer after self marker chat=%s "
                    "overlap=%s peer=%r",
                    chat_key,
                    result.get("last_peer_repeat_overlap"),
                    (peer_msg.content or "")[:80],
                )
                _cd = float(self._cfg.get("stale_peer_cooldown_sec", 90) or 90)
                _now_m = time.monotonic() + _cd
                self._self_skip_until[_self_skip_norm_key(target.name)] = _now_m
                self._self_skip_until[_self_skip_norm_key(_original_vision_name)] = _now_m
                self._exit_thread(serial)
                return self._finish(result, t0)

            # ★ 反幻觉：Vision 有时把己方最后一条消息误识为对方消息（气泡颜色/位置判断失误）。
            # 若"对方消息"内容与我方 last_reply 高度重叠，先尝试从 extra_peers
            # 提升真实客户连发消息；没有可用候选时才跳过。
            #
            # P15：扩展到 last 3 replies——vision 偶发串联的是更早的 self message
            # （不在 chat_state.last_reply 里），单条比对漏过。对最近 N 条 reply
            # 各算 ratio 取 max。
            #
            # P16 (2026-05-04) 三层反空转：
            #   D 层：peer_msg 内容指纹去重——本 chat 上次已 skip 过的同样文本
            #         立刻短路，不重复算 overlap、不重复 promote。
            #   B 层：bubble_detector 联合判定——bubble=self + overlap≥0.7 即可
            #         100% 判定 vision 误读，跳过 promote 直接 skip。
            #   C 层：连续 self_message_skip ≥ N 次 → chat 级长冷却，避免
            #         单 chat 反复 100~200ms 空转。
            if peer_msg.kind == "text":
                _pc_raw = (peer_msg.content or "").strip()
                # ── D 层短路 + C 层 chat 级冷却兜底 ──
                _chat_skip_until_m = float(
                    self._chat_overlap_skip_until.get(chat_key, 0.0) or 0.0
                )
                if _chat_skip_until_m > time.monotonic():
                    result["step"] = "self_message_skip"
                    result["ok"] = True
                    result["error"] = (
                        "chat_overlap_skip_cooldown:remaining="
                        f"{int(_chat_skip_until_m - time.monotonic())}s"
                    )
                    result.setdefault("hints", []).append(
                        "chat_overlap_skip_cooldown"
                    )
                    self._exit_thread(serial)
                    return self._finish(result, t0)
                # D2：精确相等 + 高相似度（>=0.85）双重命中。
                # vision 偶发把同样消息加 emoji 截断略改 → 精确匹配漏掉，
                # 但 _self_reply_overlap_ratio 能捕到。
                #
                # P28-FIX (2026-05-04)：D 层短路也加长度差异守卫——
                # 实测 bug：bot 发"お疲れ様です。どうぞ軽く食べてください"(22 字)
                # peer 真回复"お疲れ様です。これを軽く消化しました。そちらはお食事
                # はどうですか？"(33 字)，因公共子串"お疲れ様"+"軽く" 算出 ratio=1.0
                # 被 D 层误拦真消息。peer 显著长于 prev_skipped 时不短路。
                _skipped_texts = self._skipped_peer_text_per_chat.get(chat_key)
                _short_circuit_match: Optional[Tuple[str, float]] = None
                _d_len_thr = float(self._cfg.get(
                    "self_overlap_peer_length_ratio_threshold", 1.3,
                ) or 0.0)
                # P29 D 层 TTL：过期指纹不再算命中
                _d_ttl = float(self._cfg.get(
                    "skipped_peer_text_ttl_sec", 300.0,
                ) or 0.0)
                _d_now = time.time()
                _d_ts_map = self._skipped_peer_text_ts_per_chat.get(
                    chat_key, {},
                )
                if _pc_raw and _skipped_texts:
                    _dedup_thr = float(
                        self._cfg.get("skipped_peer_text_dedup_threshold", 0.85)
                        or 0.0
                    )
                    for _prev in _skipped_texts:
                        # P29：TTL 过期跳过此项（让 vision 重新判定）
                        if _d_ttl > 0:
                            _prev_ts = float(_d_ts_map.get(_prev, 0.0) or 0.0)
                            if _prev_ts <= 0 or _d_now - _prev_ts > _d_ttl:
                                continue
                        # P28-FIX：peer 显著长于 prev → peer 大概率是真消息
                        # quote 部分 prev 子串，跳过此条短路（让下游 P28 主路
                        # 径处理）
                        if (
                            _d_len_thr > 0
                            and _prev
                            and len(_pc_raw) > len(_prev) * _d_len_thr
                        ):
                            continue
                        if _prev == _pc_raw:
                            _short_circuit_match = (_prev, 1.0)
                            break
                        if _dedup_thr > 0:
                            _r = _self_reply_overlap_ratio(_prev, _pc_raw)
                            if _r >= _dedup_thr:
                                _short_circuit_match = (_prev, _r)
                                break
                if _short_circuit_match is not None:
                    _prev_text, _ratio = _short_circuit_match
                    result["step"] = "self_message_skip"
                    result["ok"] = True
                    result["error"] = (
                        f"vision_repeat_skipped_text: ratio={_ratio:.2f} "
                        f"peer={_pc_raw[:60]!r}"
                    )
                    result.setdefault("hints", []).append(
                        "skipped_peer_text_short_circuit"
                    )
                    result["short_circuit_ratio"] = round(_ratio, 3)
                    logger.warning(
                        "[messenger_rpa] vision 重复识别上次已 skip 文本，短路"
                        " chat=%s ratio=%.2f peer=%r",
                        chat_key, _ratio, _pc_raw[:80],
                    )
                    self._exit_thread(serial)
                    return self._finish(result, t0)

                _lr_raw = (_chat_st.get("last_reply") or "").strip()
                _candidate_replies = list(
                    self._recent_replies_per_chat.get(chat_key) or []
                )
                if _lr_raw and _lr_raw not in _candidate_replies:
                    _candidate_replies.append(_lr_raw)
                _self_overlap = 0.0
                for _cand in _candidate_replies:
                    _r = _self_reply_overlap_ratio(_cand, _pc_raw)
                    if _r > _self_overlap:
                        _self_overlap = _r
                        if _self_overlap >= 1.0:
                            break  # 已经触顶，无需再算
                result["self_reply_overlap"] = round(_self_overlap, 3)
                result["self_overlap_against_n_replies"] = len(_candidate_replies)
                # P15-strict (2026-05-04)：post-send 短窗口内禁用 promote 路径。
                # 实测 vision 在 lowmemkill + 无 XML 场景下，extra_peers 列表
                # 也常被 hallucination 污染——promote 用了它仍然是自言自语。
                # 真 peer 反应平均 > 120s（post-send cooldown 设计值），所以
                # 短窗口内严格 skip：self_overlap >= 0.7 直接终止本 cycle，
                # 不试 promote。窗口外（peer 真的有时间回复）才放宽用 promote。
                #
                # P16：strict_window 默认 180→120s 与 post_send_cooldown_sec
                # 对齐，避免发送后 60s 必空转。
                _strict_window = float(
                    self._cfg.get("self_overlap_strict_window_sec", 120.0) or 0.0
                )
                _last_sent_at = float(_chat_st.get("last_sent_at") or 0.0)
                _within_strict_window = (
                    _strict_window > 0
                    and _last_sent_at > 0
                    and (time.time() - _last_sent_at) < _strict_window
                )
                # B 层：bubble_detector 强信号
                _bubble_says_self = (result.get("bubble_sender") == "self")
                # P30 (2026-05-05)：bubble=peer 是更强信号（bubble_detector 明
                # 确看到底部是对方灰色气泡 → peer 真发消息）。即使 overlap=1.00
                # 也大概率是公共子串误判，不能直接 skip。
                _bubble_says_peer = (result.get("bubble_sender") == "peer")
                # ── P28 (2026-05-04) 长度差异守卫 ──
                # 实测 bug：bot 发"...自分のペースでいいよ..." (50 字)，peer 真消息
                # "あのばとろ🍵 そう言ってもらえると安心..." (80+ 字) 在自己回复
                # 里 echo 公共子串"自分のペースで"，被 _self_reply_overlap_ratio
                # 算出 1.00 → 守卫误拦真消息，bot 进 thread 但不回复。
                # 修复：peer 文本长度 > last_reply × ratio 时降级为"试 promote"
                # 路径，避免直接 skip。bubble=self 时仍信任 self 信号，跳过此降级。
                _len_ratio_thr = float(self._cfg.get(
                    "self_overlap_peer_length_ratio_threshold", 1.3,
                ) or 0.0)
                _peer_significantly_longer = (
                    _lr_raw
                    and _len_ratio_thr > 0
                    and len(_pc_raw) > len(_lr_raw) * _len_ratio_thr
                )
                if _self_overlap >= 0.7:
                    promoted = None
                    if _bubble_says_self:
                        # bubble + overlap 双确认 vision 误读，无需 promote
                        result.setdefault("hints", []).append(
                            "bubble_self_confirms_overlap"
                        )
                    elif _bubble_says_peer:
                        # P30：bubble 明确说 peer → peer 真发消息，overlap=1.00
                        # 大概率是公共子串误判（_self_reply_overlap_ratio 算法
                        # 边界），降级到 promote 路径。promote 失败保留 peer_msg
                        # 原样而非 skip。
                        result.setdefault("hints", []).append(
                            "bubble_peer_overrides_overlap"
                        )
                        promoted = (
                            self._promote_extra_peer_after_self_overlap(
                                result, last_reply=_lr_raw, chat_key=chat_key,
                            )
                        )
                        if promoted is None:
                            result.setdefault("hints", []).append(
                                "bubble_peer_keep_original"
                            )
                            promoted = peer_msg
                    elif _peer_significantly_longer:
                        # P28：peer 显著更长 + bubble 非 self → 视为 peer 真消息
                        # quote bot，降级到 promote 路径让下游再裁定
                        result.setdefault("hints", []).append(
                            f"self_overlap_peer_longer_promote:"
                            f"peer={len(_pc_raw)}"
                            f"_self={len(_lr_raw)}"
                        )
                        promoted = (
                            self._promote_extra_peer_after_self_overlap(
                                result, last_reply=_lr_raw, chat_key=chat_key,
                            )
                        )
                        # 若 promote 失败仍直接用 peer_msg 本体（不再 skip）
                        if promoted is None:
                            result.setdefault("hints", []).append(
                                "self_overlap_peer_longer_keep_original"
                            )
                            # 保留 peer_msg 原样，不进入下面 self_message_skip 分支
                            # 用 sentinel "promoted" 标记进入 happy-path
                            promoted = peer_msg
                    elif _within_strict_window:
                        # P31 (2026-05-05) 紧急：strict_window 内不再硬 skip。
                        # 实测 false positive：peer 真消息 1-2 分钟内回，但与
                        # bot reply 公共子串导致 overlap=1.00 → bot 错过真消息。
                        # 改为先试 promote（用 vision 的 extra_peers 兜底），
                        # promote 失败仍 skip。
                        result.setdefault("hints", []).append(
                            f"self_overlap_strict_promote:gap="
                            f"{int(time.time() - _last_sent_at)}s"
                        )
                        promoted = (
                            self._promote_extra_peer_after_self_overlap(
                                result, last_reply=_lr_raw, chat_key=chat_key,
                            )
                        )
                    else:
                        promoted = self._promote_extra_peer_after_self_overlap(
                            result, last_reply=_lr_raw, chat_key=chat_key,
                        )
                    if promoted is not None:
                        result.setdefault("hints", []).append(
                            "promoted_extra_peer_after_self_overlap"
                        )
                        result["self_reply_overlap_promoted"] = True
                        peer_msg = promoted
                        result["peer_text"] = peer_msg.to_text_for_ai()
                        result["peer_kind"] = peer_msg.kind
                    else:
                        # ── C 层：累计 streak、内容入指纹、必要时长冷却 ──
                        from collections import deque as _dq
                        _q = self._skipped_peer_text_per_chat.get(chat_key)
                        if _q is None:
                            _q = _dq(maxlen=5)
                            self._skipped_peer_text_per_chat[chat_key] = _q
                        if _pc_raw and _pc_raw not in _q:
                            _q.append(_pc_raw)
                        # P29：同步记录时间戳让 D 层 TTL 过期能识别
                        if _pc_raw:
                            _ts_map = self._skipped_peer_text_ts_per_chat \
                                .setdefault(chat_key, {})
                            _ts_map[_pc_raw] = time.time()
                            # 清理 deque 之外的过期 key（防字典无界增长）
                            _alive = set(_q)
                            for _k in list(_ts_map.keys()):
                                if _k not in _alive:
                                    _ts_map.pop(_k, None)
                        _streak = self._self_overlap_skip_streak.get(chat_key, 0) + 1
                        self._self_overlap_skip_streak[chat_key] = _streak
                        _streak_threshold = int(
                            self._cfg.get("self_overlap_streak_threshold", 3) or 3
                        )
                        _long_cd = float(
                            self._cfg.get("self_overlap_long_cooldown_sec", 600.0)
                            or 600.0
                        )
                        if _streak >= _streak_threshold and _long_cd > 0:
                            self._chat_overlap_skip_until[chat_key] = (
                                time.monotonic() + _long_cd
                            )
                            result.setdefault("hints", []).append(
                                f"chat_overlap_long_cooldown:{int(_long_cd)}s"
                                f":streak={_streak}"
                            )
                            self._self_overlap_skip_streak[chat_key] = 0
                            # ── P21 长冷却硬上限熔断 ──
                            _now_epoch = time.time()
                            _bw_window = float(self._cfg.get(
                                "chat_long_cooldown_blacklist_window_sec",
                                86400.0,
                            ) or 0.0)
                            _bw_cutoff = _now_epoch - _bw_window
                            _bw_hist = [
                                t
                                for t in self._chat_long_cooldown_history.get(
                                    chat_key, []
                                )
                                if t >= _bw_cutoff
                            ]
                            _bw_hist.append(_now_epoch)
                            self._chat_long_cooldown_history[chat_key] = _bw_hist
                            _bw_threshold = int(self._cfg.get(
                                "chat_long_cooldown_blacklist_threshold", 3,
                            ) or 3)
                            if (
                                _bw_threshold > 0
                                and len(_bw_hist) >= _bw_threshold
                            ):
                                _bl_reason = (
                                    f"chat_long_cooldown_blacklist: "
                                    f"{len(_bw_hist)} long_cd in "
                                    f"{int(_bw_window)}s window"
                                )
                                try:
                                    self._state.add_skipped_chat(
                                        chat_key,
                                        chat_name=getattr(target, "name", ""),
                                        reason=_bl_reason,
                                    )
                                    result.setdefault("hints", []).append(
                                        f"chat_long_cooldown_blacklisted:"
                                        f"{len(_bw_hist)}"
                                    )
                                    logger.error(
                                        "[messenger_rpa] 🚫 chat 永久跳过："
                                        "%s 在 %ds 内累计 %d 次 long_cooldown"
                                        "，已加入 skipped_chats 黑名单（人工"
                                        " unban 用 remove_skipped_chat）",
                                        chat_key, int(_bw_window),
                                        len(_bw_hist),
                                    )
                                    # 清理本 chat 内存状态，避免 dangling
                                    self._chat_overlap_skip_until.pop(
                                        chat_key, None
                                    )
                                    self._self_overlap_skip_streak.pop(
                                        chat_key, None
                                    )
                                    self._chat_long_cooldown_history.pop(
                                        chat_key, None
                                    )
                                    self._skipped_peer_text_per_chat.pop(
                                        chat_key, None
                                    )
                                except Exception:
                                    logger.warning(
                                        "add_skipped_chat 异常 chat=%s",
                                        chat_key, exc_info=True,
                                    )
                        result["step"] = "self_message_skip"
                        result["ok"] = True
                        result["error"] = (
                            f"vision_misread_self_as_peer: overlap={_self_overlap:.2f} "
                            f"peer={_pc_raw[:60]!r}"
                        )
                        result["self_overlap_skip_streak"] = _streak
                        logger.warning(
                            "[messenger_rpa] vision 把己方消息误识为 peer，跳过 "
                            "chat=%s overlap=%.2f peer=%r strict_window=%s "
                            "bubble=%s streak=%d",
                            chat_key, _self_overlap, _pc_raw[:80],
                            _within_strict_window,
                            result.get("bubble_sender") or "?",
                            _streak,
                        )
                        self._exit_thread(serial)
                        return self._finish(result, t0)
                else:
                    # 没有触发 overlap 守卫：streak 归零（peer 内容确实变化了）
                    if self._self_overlap_skip_streak.get(chat_key):
                        self._self_overlap_skip_streak[chat_key] = 0

            # ★ P0-B (2026-05-03 自我对话死循环兜底)：
            # sticky_thread fast_path 跳过 inbox tap → P0-A 的 inbox 守卫
            # 不会触发。这里加独立时间窗硬守卫：我方刚发不久（默认 30s）就
            # 出现"vision 检出 peer 新消息"的组合 → 视为 vision 幻觉，强制
            # skip。配置项 inbox_thread_self_skip_min_gap_sec 单独控制，
            # 不被 companion_reply_cooldown_sec=0 影响（地板独立生效）。
            try:
                _hard_gap = float(
                    self._cfg.get(
                        "inbox_thread_self_skip_min_gap_sec", 30
                    ) or 0
                )
            except (TypeError, ValueError):
                _hard_gap = 30.0
            try:
                _last_sent_pb = float(_chat_st.get("last_sent_at") or 0)
            except (TypeError, ValueError):
                _last_sent_pb = 0.0
            if (
                _hard_gap > 0
                and _last_sent_pb > 0
                and (time.time() - _last_sent_pb) < _hard_gap
                and peer_msg.is_peer_anything
            ):
                result["step"] = "thread_self_skip_hard_gap"
                result["ok"] = True
                _gap_now = time.time() - _last_sent_pb
                result["error"] = (
                    f"thread_self_skip_hard_gap: last_sent_ago={_gap_now:.0f}s "
                    f"< {_hard_gap:.0f}s, suspect vision hallucination "
                    f"(peer={(peer_msg.content or '')[:60]!r})"
                )
                logger.warning(
                    "[messenger_rpa] thread self-skip HARD-GAP (P0-B): "
                    "chat=%s last_sent_ago=%.0fs < %.0fs → 视为 vision 幻觉，"
                    "skip (peer_kind=%s peer=%r)",
                    chat_key, _gap_now, _hard_gap,
                    peer_msg.kind, (peer_msg.content or "")[:80],
                )
                # 同步设短期内存冷却，sticky fast_path 下轮也能拦
                try:
                    _hg_until = time.monotonic() + _hard_gap
                    self._self_skip_until[
                        _self_skip_norm_key(target.name)
                    ] = _hg_until
                except Exception:
                    pass
                self._exit_thread(serial)
                return self._finish(result, t0)

            fp = fingerprint(peer_msg)
            if self._state.is_duplicate(chat_key, fp):
                result["step"] = "duplicate_skip"
                result["ok"] = True
                self._exit_thread(serial)
                return self._finish(result, t0)

            # ★ 反刷屏：发送后等对方回复，否则在冷却窗口内跳过
            # companion_reply_cooldown_sec (默认 300s)：我方上次发送后必须等待这段时间
            # 才允许再次对同一条 peer 消息生成回复。
            _reply_cd_raw = self._cfg.get("companion_reply_cooldown_sec", 300)
            try:
                _reply_cd = float(
                    300 if _reply_cd_raw is None or _reply_cd_raw == "" else _reply_cd_raw
                )
            except (TypeError, ValueError):
                _reply_cd = 300.0
            _last_sent_at = float(_chat_st.get("last_sent_at") or 0)
            if _last_sent_at > 0 and (time.time() - _last_sent_at) < _reply_cd:
                result["step"] = "reply_cooldown_skip"
                result["ok"] = True
                result["error"] = (
                    f"reply_cooldown: sent {int(time.time()-_last_sent_at)}s ago "
                    f"< {int(_reply_cd)}s cooldown, waiting for peer reply"
                )
                self._exit_thread(serial)
                return self._finish(result, t0)

            # ★ P1-4：人工转接检测（在 AI 生成之前、审批入队之前）
            # 触发条件命中时：不发自动回复，强制进审批队列，标记 chat 进入
            # escalation 冷却窗口，窗口内即便 reply_mode=auto 也仅走 approve
            esc_decision = self._evaluate_escalation(peer_msg, chat_key)
            is_esc_active, esc_active_info = self._state.is_escalated(chat_key)
            if esc_decision.should_escalate or is_esc_active:
                reason = (
                    esc_decision.reason if esc_decision.should_escalate
                    else f"cooldown:{esc_active_info.get('escalation_reason', '')}"
                )
                human_msg = (
                    esc_decision.human_message if esc_decision.should_escalate
                    else (
                        f"chat under escalation cooldown "
                        f"({esc_active_info.get('remaining_sec', 0)}s remaining)"
                    )
                )
                # 进审批队列（不发送，reply_text 空，等人工 Suggest More 生成）
                try:
                    approval_id = self._enqueue_approval_wrapped(
                        chat_key=chat_key,
                        chat_name=target.name,
                        peer_text=result.get("peer_text", ""),
                        peer_kind=peer_msg.kind,
                        reply_text="",  # 无预生成，等人工处理
                        allow_empty_reply=True,  # 合法 pending 场景
                        screenshot_path=result.get("screenshot_path", ""),
                        run_id=run_id,
                        extra={
                            "escalation": True,
                            "escalation_reason": reason,
                            "escalation_message": human_msg,
                        },
                    )
                    result["approval_id"] = approval_id
                except Exception as ex:
                    logger.exception("escalation 审批入队失败")
                    result["error"] = (
                        f"esc_enqueue_failed:{type(ex).__name__}:{ex}"
                    )
                # 新触发 → 写 escalation 冷却窗口
                if esc_decision.should_escalate:
                    cd_sec = int(
                        (self._cfg.get("escalation") or {})
                        .get("cooldown_sec", 3 * 3600)
                    )
                    try:
                        self._state.set_escalation(
                            chat_key,
                            until_ts=time.time() + cd_sec,
                            reason=esc_decision.reason,
                            chat_name=target.name,
                        )
                    except Exception:
                        logger.debug("set_escalation 失败", exc_info=True)
                    # ERROR 级日志，确保触达 app.log + 外部告警
                    logger.error(
                        "[ALERT] Messenger 人工转接触发 chat=%s reason=%s msg=%s",
                        target.name, reason, human_msg,
                    )
                    logging.getLogger("ai_chat_assistant").error(
                        "[ALERT] Messenger 人工转接触发 chat=%s reason=%s",
                        target.name, reason,
                    )
                    self._notify_escalation(
                        chat_name=target.name,
                        chat_key=chat_key,
                        reason=esc_decision.reason,
                        message=human_msg,
                        peer_text=result.get("peer_text", ""),
                    )
                result["step"] = (
                    "escalation_new" if esc_decision.should_escalate
                    else "escalation_cooldown"
                )
                result["escalation_reason"] = reason
                result["ok"] = True
                # 写 fp 防止同消息反复触发同一次 escalation
                try:
                    self._state.update_chat_state(
                        chat_key,
                        chat_name=target.name,
                        last_peer_fp=fp,
                    )
                except Exception:
                    pass
                self._exit_thread(serial)
                return self._finish(result, t0)

            # ★ P1-5：媒体消息（图片/语音/视频/贴纸/文件）最小应答
            # AI 根据占位文字 "[图片] xxx" 生成的回复大多不切题 → 走模板 ack，
            # 并可选同时进审批队列让人工跟进
            media_reply_text, media_policy = self._maybe_media_ack(
                peer_msg, target.name
            )
            if media_reply_text is not None:
                # ★ anti-duplicate: skip if last_reply is the same template text
                _prev_reply = (_chat_st.get("last_reply") or "").strip()
                if _prev_reply and _prev_reply == media_reply_text.strip():
                    result["step"] = "media_ack_duplicate_skip"
                    result["ok"] = True
                    result["error"] = (
                        f"media_ack_identical_to_last_reply: {media_reply_text[:60]!r}"
                    )
                    logger.warning(
                        "[messenger_rpa] media ACK identical to last_reply, "
                        "skip to prevent duplicate: chat=%s ack=%r",
                        chat_key, media_reply_text[:60],
                    )
                    self._exit_thread(serial)
                    return self._finish(result, t0)
                result["peer_kind"] = peer_msg.kind
                result["reply_text"] = media_reply_text
                result["media_policy"] = media_policy
                if media_policy == "ack_and_approve":
                    try:
                        approval_id = self._enqueue_approval_wrapped(
                            chat_key=chat_key,
                            chat_name=target.name,
                            peer_text=result.get("peer_text", ""),
                            peer_kind=peer_msg.kind,
                            reply_text=media_reply_text,
                            screenshot_path=result.get("screenshot_path", ""),
                            run_id=run_id,
                            extra={
                                "media_ack": True,
                                "media_kind": peer_msg.kind,
                                "ack_template": media_reply_text,
                            },
                        )
                        result["approval_id"] = approval_id
                        # ★ P2-1：图片类后台异步补 caption 到 approval.extra_json
                        # 不阻塞主流程（让 ack 快速发出）
                        if peer_msg.kind == "image" and approval_id:
                            try:
                                asyncio.create_task(
                                    self._bg_enrich_image_caption(
                                        approval_id,
                                        result.get("screenshot_path", ""),
                                    )
                                )
                            except Exception:
                                logger.debug(
                                    "bg image caption 调度失败", exc_info=True
                                )
                    except Exception as ex:
                        logger.exception("媒体审批入队失败")
                        result["error"] = (
                            f"media_approve_enqueue_failed:{type(ex).__name__}:{ex}"
                        )
                if self._reply_mode == "off":
                    result["step"] = f"media_{peer_msg.kind}_reply_mode_off"
                    result["ok"] = True
                else:
                    # 发送 ack
                    if not media_reply_text.isascii() and \
                            self._reply_needs_approve_fallback(
                                serial, media_reply_text
                            ):
                        # 非 ASCII 且设备发不了 unicode → 走降级审批
                        if media_policy != "ack_and_approve":
                            try:
                                approval_id = self._enqueue_approval_wrapped(
                                    chat_key=chat_key,
                                    chat_name=target.name,
                                    peer_text=result.get("peer_text", ""),
                                    peer_kind=peer_msg.kind,
                                    reply_text=media_reply_text,
                                    screenshot_path=result.get(
                                        "screenshot_path", ""
                                    ),
                                    run_id=run_id,
                                    extra={
                                        "media_ack": True,
                                        "auto_downgrade":
                                            "media_ack_non_ascii",
                                    },
                                )
                                result["approval_id"] = approval_id
                            except Exception:
                                logger.exception("媒体 ASCII guard 入队失败")
                        result["step"] = f"media_{peer_msg.kind}_approve_fallback"
                        result["ok"] = True
                    else:
                        sent_ok = await self._send_reply_with_retry(
                            serial, wh, media_reply_text, result
                        )
                        result["step"] = (
                            f"media_{peer_msg.kind}_sent" if sent_ok
                            else f"media_{peer_msg.kind}_send_failed"
                        )
                        result["ok"] = bool(sent_ok)
                # 状态记录（便于去重、审计）
                try:
                    self._state.update_chat_state(
                        chat_key,
                        chat_name=target.name,
                        last_peer_text=result.get("peer_text", ""),
                        last_peer_fp=fp,
                        last_peer_kind=peer_msg.kind,
                        last_reply=media_reply_text,
                        last_sent_at=time.time(),
                    )
                    # P15：媒体 ACK reply 也算 self reply，纳入 recent 队列
                    self._push_recent_reply(chat_key, media_reply_text)
                except Exception:
                    logger.debug("update_chat_state 失败", exc_info=True)
                # ── P2++G_FIX (2026-05-04) fast-ack 路径接入 modality 决策 ──
                # 修盲区：peer 发 sticker/voice/image 时这里就 return 了，之前
                # 永远不会触发 sticker 决策器。现在调统一 helper，让 modality
                # 决策对所有 reply 路径都生效（dry_run 阶段只 log，real_send
                # 阶段双开关 OK 时真发 sticker 跟在 ack 文本之后）。
                # caption 来自 P1 路径写入的 image_caption / media_caption。
                if peer_msg.kind in (
                    "sticker", "animated_sticker", "image", "video",
                    "gif", "voice",
                ):
                    await self._run_modality_hook(
                        serial=serial,
                        target=target,
                        chat_key=chat_key,
                        peer_msg=peer_msg,
                        reply_text=media_reply_text or "",
                        multi_peer_count=int(result.get("multi_peer_count") or 0),
                        peer_sticker_cat=result.get("sticker_category"),
                        result=result,
                        caption=(
                            result.get("media_caption")
                            or result.get("image_caption")
                            or peer_msg.desc
                        ),
                    )
                self._exit_thread(serial)
                return self._finish(result, t0)

            # ★ typing 反馈：在 AI 生成期间并行 pulse 输入框，让 peer 看见 "typing..."
            # 只在 auto 模式下启用（approve 模式不真发，不需要假装在打字）
            typing_task: Optional[asyncio.Task] = None
            if (
                self._reply_mode == "auto"
                and bool(self._cfg.get("typing_indicator_enabled", True))
            ):
                try:
                    typing_task = asyncio.create_task(
                        self._typing_indicator_pulse(serial, wh)
                    )
                except Exception:
                    logger.debug("启动 typing 指示任务失败", exc_info=True)

            # ★ P4-7：信用分前置门禁
            cred_cfg = (self._cfg.get("credit_policy") or {})
            credit_forced_approve = False
            if cred_cfg.get("enabled", True):
                try:
                    cred = self._state.get_credit(chat_key)
                    result["credit"] = cred
                    bl = int(cred_cfg.get("blacklist_threshold", 20) or 20)
                    lo = int(cred_cfg.get("low_threshold", 40) or 40)
                    if int(cred.get("credit", 100)) < bl:
                        if typing_task is not None:
                            typing_task.cancel()
                            try:
                                await typing_task
                            except (asyncio.CancelledError, Exception):
                                pass
                        logger.warning(
                            "[messenger_rpa] P4-7 credit blacklist chat=%s credit=%d",
                            chat_key, int(cred.get("credit", 100)),
                        )
                        result["step"] = "credit_blacklist_skip"
                        result["ok"] = True  # 主动 skip 不算 error
                        self._exit_thread(serial)
                        return self._finish(result, t0)
                    if int(cred.get("credit", 100)) < lo:
                        credit_forced_approve = True
                        logger.info(
                            "[messenger_rpa] P4-7 credit low → force approve chat=%s credit=%d",
                            chat_key, int(cred.get("credit", 100)),
                        )
                except Exception:
                    logger.debug("P4-7 credit 前置门禁异常", exc_info=True)

            # ★ W3-D2.5：peer_typing detect prefetch — 与 LLM 并发跑，省掉 ~1.5s 串行
            # 命中（典型 < 5%）时仍要花一次 LLM 钱（pre-spent），但 not_typing（95%+）路径节省时间
            peer_typing_prefetch_task = None
            if (
                bool(self._cfg.get("companion_mode", False))
                and self._peer_typing is not None
                and bool((self._cfg.get("peer_typing") or {}).get("enabled", False))
                and result.get("screenshot_path")
            ):
                try:
                    peer_typing_prefetch_task = asyncio.create_task(
                        self._peer_typing.detect(
                            result.get("screenshot_path", "") or "",
                            chat_key=chat_key,
                        ),
                        name="peer_typing_prefetch",
                    )
                except Exception:
                    logger.debug("peer_typing prefetch 启动失败", exc_info=True)
                    peer_typing_prefetch_task = None
            # 把 prefetch task 挂到 result 上让下游消费
            result["_peer_typing_prefetch_task"] = peer_typing_prefetch_task

            # P2++G_FIX：让 _generate_reply 内的 modality hook 拿到 serial（real_send 才用）
            result["_serial"] = serial
            try:
                reply_text = await self._generate_reply(peer_msg, target, chat_key, result)
            finally:
                if typing_task is not None:
                    typing_task.cancel()
                    try:
                        await typing_task
                    except (asyncio.CancelledError, Exception):
                        pass

            if not reply_text:
                result["step"] = "skill_no_reply"
                self._exit_thread(serial)
                return self._finish(result, t0)
            result["reply_text"] = reply_text

            # ★ P0-F (2026-05-03 5.5h 监控发现): reply_text 字字相同去重。
            # LLM 在 deterministic 模式 + 类似 prompt 下可输出**完全一致**的
            # reply，被反复发到客户端（监控发现 17:21+17:26、17:30+17:35 各
            # 一对字字相同消息发到 Victor Zan 真实用户 → "AI 复读机"现象）。
            # 现有 fingerprint 只去重 peer 内容、不去重 reply_text；media_ack
            # 路径已有此守卫但普通 text reply 路径漏了。这里补齐。
            # 命中后写短期 self_skip cooldown 防 sticky fast_path 下轮立即重入。
            _prev_reply = (_chat_st.get("last_reply") or "").strip()
            if _prev_reply and _prev_reply == reply_text.strip():
                result["step"] = "reply_text_duplicate_skip"
                result["ok"] = True
                result["error"] = (
                    f"reply_text_duplicate: identical to last_reply "
                    f"({len(reply_text)} chars)"
                )
                logger.warning(
                    "[messenger_rpa] reply_text duplicate skip (P0-F): "
                    "chat=%s reply=%r — identical to last_reply, abort send",
                    chat_key, reply_text[:80],
                )
                try:
                    _dup_cd = float(
                        self._cfg.get(
                            "reply_text_duplicate_cooldown_sec", 300
                        ) or 300
                    )
                    if _dup_cd > 0:
                        self._self_skip_until[
                            _self_skip_norm_key(target.name)
                        ] = time.monotonic() + _dup_cd
                except Exception:
                    logger.debug(
                        "P0-F self_skip cooldown 写入失败", exc_info=True,
                    )
                self._exit_thread(serial)
                return self._finish(result, t0)

            await self._maybe_prepare_tts_reply(reply_text, peer_msg, result)
            # ★ P4-7：低信用 → 本轮强制 approve（覆盖 reply_mode=auto）
            if credit_forced_approve and self._reply_mode == "auto":
                result["credit_forced_approve"] = True

            if self._reply_mode == "off":
                result["step"] = "reply_mode_off_skip_send"
                result["ok"] = True
            elif self._reply_mode == "approve":
                # 把候选回复推入审批队列；不真发
                try:
                    approval_id = self._enqueue_approval_wrapped(
                        chat_key=chat_key,
                        chat_name=target.name,
                        peer_text=result.get("peer_text", ""),
                        peer_kind=peer_msg.kind,
                        reply_text=reply_text,
                        screenshot_path=result.get("screenshot_path", ""),
                        run_id=run_id,
                        extra={
                            "thread_vision_tag": result.get(
                                "thread_vision_tag"
                            ),
                            "inbox_vision_tag": result.get(
                                "inbox_vision_tag"
                            ),
                            "device_wh": result.get("device_wh"),
                            "guard_history": result.get("guard_history", []),
                            "tts_audio_path": result.get("tts_audio_path", ""),
                            "tts_provider": result.get("tts_provider", ""),
                            "tts_latency_ms": result.get("tts_latency_ms", 0),
                        },
                    )
                    result["approval_id"] = approval_id
                    result["step"] = "approve_pending"
                    result["ok"] = True
                except Exception as ex:
                    logger.exception("入队审批失败")
                    result["step"] = "approve_enqueue_failed"
                    result["error"] = f"{type(ex).__name__}:{ex}"
            else:  # auto
                # ★ P1-6：反封号门控（最小间隔 / 日上限 / 静夜 / 禁用词 → 降级审批）
                _gate = self._pre_send_gate(reply_text)
                # ★ P4-7：低信用 chat 强制 approve（伪造 _gate 触发现有降级分支）
                # companion_mode 不受 credit 低分影响（黑名单仍有效）
                if _gate is None and result.get("credit_forced_approve") \
                        and not bool(self._cfg.get("companion_mode", False)):
                    _gate = {
                        "reason": (
                            f"credit:low credit="
                            f"{result.get('credit', {}).get('credit', '?')}"
                        ),
                        "credit_forced": True,
                    }
                if _gate is not None:
                    # ★ companion_mode：拒绝把 auto 偷偷降级到 approve；改成 safe_skip 等下一轮
                    # 反封号 gate（静夜 / 日上限 / 最小间隔）触发时直接跳过本轮，不堆给人审。
                    # 信用低（credit_forced）时同理，让 chat 自然冷却而不是占审批队列。
                    if bool(self._cfg.get("companion_mode", False)):
                        # ★ W2-D1：safe_skip 不再丢消息，写入 deferred 队列等到时候发
                        defer_until = self._calc_defer_until_sec(_gate)
                        defer_reason_short = (_gate.get("reason") or "")[:80]
                        if defer_until is not None:
                            try:
                                deferred_id = self._state.enqueue_deferred(
                                    chat_key=chat_key,
                                    chat_name=target.name,
                                    peer_text=result.get("peer_text", ""),
                                    peer_kind=peer_msg.kind,
                                    reply_text=reply_text,
                                    defer_until=defer_until,
                                    defer_reason=defer_reason_short,
                                    run_id=run_id,
                                    extra={
                                        "gate_reason": _gate.get("reason"),
                                        "credit_forced": _gate.get("credit_forced", False),
                                    },
                                )
                                result["deferred_id"] = deferred_id
                                result["deferred_until"] = defer_until
                                result["step"] = "companion_deferred"
                                wait_min = max(0, int((defer_until - time.time()) / 60))
                                logger.info(
                                    "[messenger_rpa] companion_mode defer chat=%s "
                                    "reason=%s wait=%dmin id=%d",
                                    chat_key, defer_reason_short, wait_min, deferred_id,
                                )
                            except Exception:
                                logger.exception("enqueue_deferred 失败，降级 safe_skip")
                                result["step"] = "companion_safe_skip"
                        else:
                            # forbidden_keyword 等：reply 本身不安全 → 丢弃不 defer
                            logger.warning(
                                "[messenger_rpa] companion_mode reply 含违禁内容 chat=%s reason=%s",
                                chat_key, defer_reason_short,
                            )
                            result["step"] = "companion_drop_unsafe"
                        try:
                            from src.monitoring.metrics_store import get_metrics_store
                            get_metrics_store().record_companion_safe_skip(
                                _gate.get("reason") or "pre_send_gate"
                            )
                        except Exception:
                            pass
                        result["gate_reason"] = _gate.get("reason")
                        result["ok"] = True
                        # 标已读：deferred 队列已经持有承诺，本条 peer_msg 不必再触发
                        self._state.update_chat_state(
                            chat_key,
                            chat_name=target.name,
                            last_peer_text=result["peer_text"],
                            last_peer_fp=fp,
                            last_peer_kind=peer_msg.kind,
                        )
                        self._exit_thread(serial)
                        return self._finish(result, t0)
                    logger.warning(
                        "[messenger_rpa] pre_send_gate 触发降级 chat=%s reason=%s",
                        chat_key, _gate.get("reason"),
                    )
                    try:
                        approval_id = self._enqueue_approval_wrapped(
                            chat_key=chat_key,
                            chat_name=target.name,
                            peer_text=result.get("peer_text", ""),
                            peer_kind=peer_msg.kind,
                            reply_text=reply_text,
                            screenshot_path=result.get("screenshot_path", ""),
                            run_id=run_id,
                            extra={
                                "auto_downgrade": "pre_send_gate",
                                "gate_reason": _gate.get("reason"),
                                "thread_vision_tag": result.get("thread_vision_tag"),
                            },
                        )
                        result["approval_id"] = approval_id
                        result["step"] = "approve_pending_rate_limit"
                        result["gate_reason"] = _gate.get("reason")
                        result["ok"] = True
                    except Exception as ex:
                        logger.exception("rate-limit 降级入队失败")
                        result["step"] = "approve_enqueue_failed"
                        result["error"] = f"{type(ex).__name__}:{ex}"
                    # 跳过 update_chat_state 里的 last_reply 覆盖（reply 未真发送）
                    self._state.update_chat_state(
                        chat_key,
                        chat_name=target.name,
                        last_peer_text=result["peer_text"],
                        last_peer_fp=fp,
                        last_peer_kind=peer_msg.kind,
                    )
                    self._exit_thread(serial)
                    return self._finish(result, t0)
                # ── P3-D: companion_dry_run ─────────────────────────────
                # dry_run=true 时：完整跑完 LLM + TTS（已在前面完成），
                # 记录样本到 metrics dashboard 供运营审核，但 **不** 真发送。
                # 与 reactivation dry_run 共用同一 dashboard 展示，避免引入新 UI。
                # 场景：上线前灰度验证、TTS 时长/质量审计、AI 话术复核。
                if (
                    bool(self._cfg.get("companion_mode", False))
                    and bool(self._cfg.get("companion_dry_run", False))
                ):
                    try:
                        from src.monitoring.metrics_store import get_metrics_store
                        get_metrics_store().record_reactivation_dry_run(sample={
                            "chat_name": target.name,
                            "chat_key": chat_key,
                            "peer_text": result.get("peer_text", ""),
                            "reply_text": reply_text,
                            "tts_duration_sec": result.get("tts_duration_sec"),
                            "trigger": "companion_dry_run",
                            "account_id": getattr(self, "_account_id", ""),
                        })
                    except Exception:
                        pass
                    result["step"] = "companion_dry_run_skip"
                    result["ok"] = True
                    self._state.update_chat_state(
                        chat_key, chat_name=target.name,
                        last_peer_text=result.get("peer_text", ""),
                        last_peer_fp=fp, last_peer_kind=peer_msg.kind,
                    )
                    logger.info(
                        "[messenger_rpa] P3-D dry_run chat=%s reply_len=%d "
                        "tts_dur=%s",
                        chat_key, len(reply_text),
                        result.get("tts_duration_sec"),
                    )
                    self._exit_thread(serial)
                    return self._finish(result, t0)

                # ★ W2-D3.4 + D4.8 + D3-D2.5：对方"在输入..."检测 → 让一让，避免抢话
                # D2.5 优化：从 prefetch task 拿结果（与 LLM 并发，typically 此时已就绪）
                if (
                    bool(self._cfg.get("companion_mode", False))
                    and self._peer_typing is not None
                    and bool((self._cfg.get("peer_typing") or {}).get("enabled", False))
                ):
                    try:
                        prefetch_task = result.pop("_peer_typing_prefetch_task", None)
                        # ★ W3-D3.4：观测 prefetch 是否真节省了时间
                        _wait_t0 = time.monotonic()
                        already_done = (prefetch_task is not None and prefetch_task.done())
                        if prefetch_task is not None and not prefetch_task.done():
                            try:
                                pt = await asyncio.wait_for(prefetch_task, timeout=2.0)
                            except asyncio.TimeoutError:
                                from src.integrations.messenger_rpa.peer_typing import PeerTypingResult
                                pt = PeerTypingResult.not_typing()
                                try:
                                    prefetch_task.cancel()
                                except Exception:
                                    pass
                        elif prefetch_task is not None:
                            try:
                                pt = prefetch_task.result()
                            except Exception:
                                from src.integrations.messenger_rpa.peer_typing import PeerTypingResult
                                pt = PeerTypingResult.not_typing()
                        else:
                            pt = await self._peer_typing.detect(
                                result.get("screenshot_path", "") or "",
                                chat_key=chat_key,
                            )
                        _wait_ms = (time.monotonic() - _wait_t0) * 1000.0
                        try:
                            from src.monitoring.metrics_store import get_metrics_store
                            get_metrics_store().record_peer_typing_prefetch(
                                already_done=already_done, wait_ms=_wait_ms,
                            )
                        except Exception:
                            pass
                        if pt.is_typing:
                            wait = max(5.0, min(30.0, pt.suggested_wait_sec or 8.0))
                            defer_until = time.time() + wait
                            try:
                                deferred_id = self._state.enqueue_deferred(
                                    chat_key=chat_key,
                                    chat_name=target.name,
                                    peer_text=result.get("peer_text", ""),
                                    peer_kind=peer_msg.kind,
                                    reply_text=reply_text,
                                    defer_until=defer_until,
                                    defer_reason=f"peer_typing:{wait:.0f}s",
                                    run_id=run_id,
                                    extra={
                                        "peer_typing_confidence": pt.confidence,
                                        "peer_typing_detail": pt.detail,
                                    },
                                    staleness_sec=120.0,
                                )
                                result["deferred_id"] = deferred_id
                                result["deferred_until"] = defer_until
                                result["step"] = "companion_peer_typing_deferred"
                                result["ok"] = True
                                self._state.update_chat_state(
                                    chat_key, chat_name=target.name,
                                    last_peer_text=result["peer_text"],
                                    last_peer_fp=fp, last_peer_kind=peer_msg.kind,
                                )
                                logger.info(
                                    "[messenger_rpa] peer_typing detected chat=%s "
                                    "defer=%.1fs conf=%.2f",
                                    chat_key, wait, pt.confidence,
                                )
                                self._exit_thread(serial)
                                return self._finish(result, t0)
                            except Exception:
                                logger.debug(
                                    "peer_typing enqueue_deferred 失败", exc_info=True,
                                )
                    except Exception:
                        logger.debug("peer_typing detect 异常", exc_info=True)

                # ★ W2-D2.5：自然节奏 — gate 通过后 reply 也不秒回
                # 短延迟（< short_threshold）：直接 await 后真发
                # 长延迟：enqueue_deferred(reason="pacing:")，独立 drain loop 异步发
                if bool(self._cfg.get("companion_mode", False)):
                    pacing_action = await self._maybe_pacing_defer(
                        reply_text=reply_text,
                        peer_text=result.get("peer_text", ""),
                        peer_kind=peer_msg.kind,
                        chat_key=chat_key,
                        chat_name=target.name,
                        run_id=run_id,
                        result=result,
                        fp=fp,
                        serial=serial,
                        wh=wh,
                    )
                    if pacing_action == "deferred":
                        self._exit_thread(serial)
                        return self._finish(result, t0)
                    # 否则继续走真发链路（短 await 已在 helper 内完成）

                # ★ ASCII guard：设备只能发 ASCII 但 reply 含非 ASCII → 自动降级到审批队列
                # （避免直接 send_failed 打断流程）
                self._hint_non_ascii_adbkeyboard(serial, reply_text, result)
                if self._reply_needs_approve_fallback(serial, reply_text):
                    # ★ companion_mode：设备装 AdbKeyboard 是部署任务，不该堆给运营审。
                    # 跳过本轮 + 持续告警，让运维真去装；不要让一台坏设备污染审批队列。
                    if bool(self._cfg.get("companion_mode", False)):
                        logger.error(
                            "[messenger_rpa] companion_mode ascii_guard safe_skip "
                            "chat=%s len=%d — 设备未装 AdbKeyboard 导致非 ASCII 无法发送，"
                            "请尽快部署 com.android.adbkeyboard",
                            chat_key, len(reply_text),
                        )
                        try:
                            from src.monitoring.metrics_store import get_metrics_store
                            get_metrics_store().record_companion_safe_skip("ascii_guard:no_adbkeyboard")
                        except Exception:
                            pass
                        result["step"] = "companion_ascii_skip"
                        result["ok"] = True
                        self._state.update_chat_state(
                            chat_key,
                            chat_name=target.name,
                            last_peer_text=result["peer_text"],
                            last_peer_fp=fp,
                            last_peer_kind=peer_msg.kind,
                        )
                        self._exit_thread(serial)
                        return self._finish(result, t0)
                    logger.warning(
                        "[messenger_rpa] reply 含非 ASCII 字符但设备无 AdbKeyboard，"
                        "降级到 approve 模式入队 chat=%s len=%d",
                        chat_key, len(reply_text),
                    )
                    try:
                        approval_id = self._enqueue_approval_wrapped(
                            chat_key=chat_key,
                            chat_name=target.name,
                            peer_text=result.get("peer_text", ""),
                            peer_kind=peer_msg.kind,
                            reply_text=reply_text,
                            screenshot_path=result.get("screenshot_path", ""),
                            run_id=run_id,
                            extra={
                                "auto_downgrade": "non_ascii_no_adbkeyboard",
                                "thread_vision_tag": result.get("thread_vision_tag"),
                            },
                        )
                        result["approval_id"] = approval_id
                        result["step"] = "approve_pending_ascii_guard"
                        result["ok"] = True
                    except Exception as ex:
                        logger.exception("ASCII guard 降级入队失败")
                        result["step"] = "approve_enqueue_failed"
                        result["error"] = f"{type(ex).__name__}:{ex}"
                else:
                    # W4-Handoff-Auto-Inject：AI 出稿后、真发前，问 contacts 要不要追加引流话术
                    handoff_token: Optional[str] = None
                    hooks = self._contact_hooks
                    if hooks is not None and hasattr(hooks, "maybe_before_reply"):
                        try:
                            dec = hooks.maybe_before_reply(
                                account_id=str(getattr(
                                    self, "_account_id", "") or "default"),
                                external_id=target.name or "",
                                ai_reply=reply_text,
                                latest_in_text=peer_msg.to_text_for_ai(),
                                trace_id=str(
                                    result.get("request_id") or ""),
                            )
                            if dec.reason == "ok" and dec.augmented_text:
                                reply_text = dec.augmented_text
                                handoff_token = dec.token
                                result["handoff_injected"] = True
                                result["handoff_script_id"] = dec.script_id
                                result["handoff_token"] = dec.token
                                logger.info(
                                    "[messenger_rpa] 自动注入引流话术 "
                                    "chat=%s script=%s",
                                    chat_key, dec.script_id,
                                )
                            elif dec.reason and dec.reason != "auto_inject_disabled":
                                result["handoff_skipped"] = dec.reason
                        except Exception:
                            logger.debug(
                                "maybe_before_reply 异常", exc_info=True)

                    # ★ TTS auto_voice guard: 如果语音已经通过 share sheet 发送，
                    # 不要再注入文字 + 点 send（此时 Messenger 已不在 chat 页面）
                    if result.get("tts_send_ok"):
                        logger.info(
                            "[messenger_rpa] skip text send: TTS auto_voice "
                            "already sent chat=%s", chat_key,
                        )
                        result["step"] = "sent_voice_only"
                        result["ok"] = True
                    else:
                        sent_ok = await self._send_reply_with_retry(serial, wh, reply_text, result)
                        if not sent_ok:
                            result["step"] = "send_failed"
                            return self._finish(result, t0)
                        result["step"] = "sent"
                        result["ok"] = True
                    # ★ W2-D1.4：发送成功 → 清掉同 chat 老的 deferred 防止重发
                    try:
                        n_exp = self._state.expire_deferred_for_chat(
                            chat_key, reason="superseded_by_new_send"
                        )
                        if n_exp:
                            logger.debug(
                                "[messenger_rpa] expire %d 旧 deferred chat=%s",
                                n_exp, chat_key,
                            )
                    except Exception:
                        logger.debug("expire_deferred 失败", exc_info=True)
                    # 发送成功 → 推进 handoff stage 到 HANDOFF_SENT
                    if handoff_token and hooks is not None:
                        try:
                            hooks.on_handoff_sent(
                                account_id=str(getattr(
                                    self, "_account_id", "") or "default"),
                                external_id=target.name or "",
                                token=handoff_token,
                                trace_id=str(
                                    result.get("request_id") or ""),
                            )
                        except Exception:
                            logger.debug(
                                "on_handoff_sent 异常", exc_info=True)

            self._state.update_chat_state(
                chat_key,
                chat_name=target.name,
                last_peer_text=result["peer_text"],
                last_peer_fp=fp,
                last_peer_kind=peer_msg.kind,
                last_reply=reply_text,
                last_sent_at=time.time(),
            )
            # P15：同步刷新 in-memory recent reply 队列
            self._push_recent_reply(chat_key, reply_text)
            # ★ post-send 冷却：发送成功后短暂屏蔽同一 chat，等对方回复
            # 双保险：companion_reply_cooldown_sec 按 chat_key 检查（state_store），
            # _self_skip_until 按 norm_key 检查（内存）→ OCR 变体也能拦住
            _ps_cd = float(self._cfg.get("post_send_cooldown_sec", 120) or 120)
            _ps_until = time.monotonic() + _ps_cd
            self._self_skip_until[_self_skip_norm_key(target.name)] = _ps_until
            if _original_vision_name:
                self._self_skip_until[
                    _self_skip_norm_key(_original_vision_name)
                ] = _ps_until
            logger.warning(
                "[messenger_rpa] ✓ 发送成功 chat=%r → post-send cooldown %ds",
                target.name, int(_ps_cd),
            )
            # ── P26 auto-sticky：发送成功后该 chat 自动 sticky N 秒，让 peer
            # 在 TTL 内继续聊时 bot 走 fast_path 几秒响应（跳过 50s inbox vision）
            try:
                _sticky_cfg_post = self._cfg.get("sticky_thread") or {}
                if _sticky_cfg_post.get("enabled", False) and _sticky_cfg_post.get(
                    "auto_sticky_enabled", True,
                ):
                    _auto_ttl = float(_sticky_cfg_post.get(
                        "auto_sticky_ttl_sec", 300.0,
                    ) or 300.0)
                    if _auto_ttl > 0 and target.name:
                        self._auto_sticky_until[target.name] = (
                            time.monotonic() + _auto_ttl
                        )
                        result.setdefault("hints", []).append(
                            f"auto_sticky_armed:{int(_auto_ttl)}s"
                        )
            except Exception:
                logger.debug("auto_sticky 设置异常", exc_info=True)
            # P2.3 (2026-05-04) sticker 发送 hook：
            # text 已发出 → 若 modality 决策命中且 dry_run=False 且
            # real_send_enabled=True 双开关同时打开 → 异步真发 sticker。
            # 默认 dry_run=True / real_send_enabled=False 都关，**真发不会触发**，
            # 安全收集决策数据；任一为 True 才进入真发分支。
            try:
                _md = result.get("modality_decision") or {}
                _sr_cfg = self._cfg.get("sticker_reply") or {}
                _real_enabled = bool(_sr_cfg.get("real_send_enabled", False))
                if (
                    _md.get("would_send")
                    and not _md.get("dry_run", True)
                    and _real_enabled
                    and _md.get("sticker_path")
                ):
                    asyncio.create_task(self._send_sticker_after_text(
                        serial=serial,
                        chat_name=target.name or "",
                        sticker_path=_md["sticker_path"],
                        sticker_cat=_md.get("sticker_cat") or "",
                        result=result,
                    ))
                elif _md.get("would_send") and _md.get("dry_run"):
                    # dry_run 模式下也 log 一遍，方便 grep 验证决策频率
                    logger.info(
                        "[messenger_rpa] sticker DRY_RUN skipped real send "
                        "chat=%r cat=%s path=%s (dry_run=True)",
                        target.name,
                        _md.get("sticker_cat"), _md.get("sticker_path"),
                    )
            except Exception:
                logger.debug(
                    "[messenger_rpa] sticker send hook failed", exc_info=True,
                )
            # P3-A：记录 send 时间戳 + peer_text 供熔断器下轮检测
            # peer_text 用于序列检测（连续 sent 之间 peer 文本高度相似 → 熔断）
            self._record_chat_send(
                target.name or "",
                peer_text=str(result.get("peer_text") or "")[:300],
            )
            # P2-S 粘性会话：白名单内的 chat 发送成功后不退 thread，让下轮
            # smart_current_thread 直接接管，把响应延迟从 60-90s 降到 5-10s。
            if self._is_sticky_chat(target.name):
                result["sticky_thread_kept"] = True
                result.setdefault("hints", []).append(
                    f"sticky_thread_kept:{target.name}"
                )
                logger.warning(
                    "[messenger_rpa] 🔒 sticky_thread: 保持在 %r 会话内，"
                    "下轮直接接管（省去 inbox 扫描）",
                    target.name,
                )
                # 同时缩短 post_send_cooldown 让粘性快速循环
                _sticky_cd = float(
                    (self._cfg.get("sticky_thread") or {}).get(
                        "post_send_cooldown_sec", 5
                    ) or 5
                )
                # ★ P0-D (2026-05-03 自我对话死循环兜底)：强制 cooldown 地板。
                # 历史上 sticky_thread.post_send_cooldown_sec 配置过 5s，加上
                # vision 偶发误识 → 5s 内下轮 fast_path 又生成回复 → 死循环。
                # 设独立地板配置 post_send_cooldown_floor_sec（默认 30s），
                # **即使 post_send_cooldown_sec=0/5 也保底**。运维仍可显式调
                # floor_sec=0 关闭地板（不推荐）。
                _floor_raw = (self._cfg.get("sticky_thread") or {}).get(
                    "post_send_cooldown_floor_sec", 30
                )
                try:
                    _sticky_cd_floor = float(
                        30 if _floor_raw is None else _floor_raw
                    )
                except (TypeError, ValueError):
                    _sticky_cd_floor = 30.0
                if _sticky_cd_floor > 0 and _sticky_cd < _sticky_cd_floor:
                    logger.warning(
                        "[messenger_rpa] sticky cooldown raised by floor "
                        "(P0-D): %.1fs → %.1fs",
                        _sticky_cd, _sticky_cd_floor,
                    )
                    _sticky_cd = _sticky_cd_floor
                _ps_until_sticky = time.monotonic() + _sticky_cd
                self._self_skip_until[_self_skip_norm_key(target.name)] = _ps_until_sticky
                if _original_vision_name:
                    self._self_skip_until[
                        _self_skip_norm_key(_original_vision_name)
                    ] = _ps_until_sticky
                # P2-A bugfix（CRITICAL）：发送成功后自方气泡会出现在 thread 底部，
                # 让 hash diff 误识为"peer 新消息" → 下轮又触发完整流程 → **疯狂
                # 回复死循环**。这里强制等渲染 + 重新 screencap + 更新 hash baseline，
                # 让下一轮 hash diff 以"包含自方刚发气泡"的截图为新基线。
                #
                # P2-A v2：等 5s 而不是 2s。messenger 渲染包括：自方气泡、Delivered
                # 标记、Seen 标记、时间戳。这些渲染分多次发生，2s 不够。5s 让渲染
                # 完全稳定，避免后续"自然变化"被 hash diff 误判 changed。
                # 还做 dual-hash 验证：截图 1 + 等 1.5s + 截图 2，两次 hash 一致才信任。
                try:
                    await asyncio.sleep(3.0)
                    post_send_png_1 = await self._screenshot(
                        serial, "thread_post_send_b1", run_id,
                    )
                    await asyncio.sleep(2.0)
                    post_send_png_2 = await self._screenshot(
                        serial, "thread_post_send_b2", run_id,
                    )
                    chosen_baseline = post_send_png_2 or post_send_png_1
                    if chosen_baseline:
                        baseline_result: Dict[str, Any] = {}
                        self._check_sticky_thread_changed(
                            chosen_baseline, target.name, baseline_result,
                        )
                        result["sticky_baseline_updated"] = True
                        logger.warning(
                            "[messenger_rpa] 🔄 sticky baseline updated to "
                            "post-send screenshot (settled 5s), hash=%s",
                            baseline_result.get("sticky_hash", "?"),
                        )
                except Exception:
                    logger.debug(
                        "[messenger_rpa] sticky baseline update failed",
                        exc_info=True,
                    )
            else:
                self._exit_thread(serial)
            return self._finish(result, t0)

        except Exception as ex:
            logger.exception("run_once 异常")
            result["step"] = result["step"] or "exception"
            result["error"] = f"{type(ex).__name__}: {ex}"
            return self._finish(result, t0)

    # ── public：审批批准后的真发 ─────────────────
    async def send_to_chat_name(
        self,
        *,
        chat_name: str,
        reply_text: str,
        typing_pulse_sec: float = 0.0,
        skip_search: bool = False,
    ) -> Dict[str, Any]:
        """重新打开 Messenger → 在 inbox 找 chat_name → 进会话 → 发送。

        和 run_once 复用大量内部步骤，但**强制走 send 路径，不重新生成 AI 回复**。

        ★ W2-D3.1：``typing_pulse_sec`` > 0 时，进入 thread 之后真发之前先做
        一段"在输入..."脉冲（典型 2-3 秒），让对方感觉到 AI 是先打字再发，
        而不是凭空蹦一条消息出来 —— drain loop 调时建议传 2.0-3.0。
        """
        run_id = uuid.uuid4().hex[:8]
        t0 = time.monotonic()
        result: Dict[str, Any] = {
            "ts": time.time(),
            "run_id": run_id,
            "ok": False,
            "step": "send_to_chat_name:init",
            "chat_name": chat_name,
            "reply_text": reply_text,
            "total_ms": 0,
            "error": "",
        }
        try:
            serial = self._resolve_serial(result)
            if not serial:
                return self._finish(result, t0)
            wh = self._screen_size(serial)
            result["device_wh"] = wh
            if not self._foreground_messenger(serial, result):
                return self._finish(result, t0)
            self._hint_non_ascii_adbkeyboard(serial, reply_text, result)

            # P0：已在目标 thread（用户常停在会话里做互测）→ 跳过截图+Vision，省
            # 时省费且避开 inbox 误识别。
            target: Optional[UnreadChat] = None
            # 同一次 send 内最近一次 verify_thread_title 通过的时间戳；safety net
            # 在阈值内信任并跳过——dump-dead 设备上一次 verify ≈ 4s 多次省可观。
            just_verified_ts: float = 0.0
            try:
                from src.integrations.messenger_rpa import thread_actions as _ta0
                vt_pre = _ta0.verify_thread_title(
                    serial, chat_name,
                    vision_cfg=self._vision_cfg(),
                    global_vision_cfg=self._global_vision_cfg(),
                )
                result["pre_inbox_title_check"] = {
                    "ok": vt_pre.ok,
                    "actual": vt_pre.actual,
                    "reason": vt_pre.reason,
                }
                if vt_pre.ok:
                    cn = (chat_name or "").strip()
                    target = UnreadChat(
                        name=cn,
                        preview="",
                        time="",
                        row_index=0,
                        y_percent=0.0,
                        quality_hint="already_in_thread",
                        score=100.0,
                        skip_inbox_tap=True,
                    )
                    just_verified_ts = time.time()
                    result["screenshot_path"] = ""
                    result.setdefault("hints", []).append(
                        "send_to_chat_name:in_thread_skip_inbox_vision",
                    )
            except Exception as _pre_ex:
                result.setdefault("hints", []).append(
                    f"pre_inbox_title_check_exc:{type(_pre_ex).__name__}",
                )

            # ★ Fast-path：已缓存的"该 chat 上次工作过的 tap 坐标"
            # 适用：连发同一人（reactivation 偶尔重发 / 多 burst）。直接
            # tap → verify；verify 通过即跳过 search/inbox 全部步骤。
            search_first_tried = False
            if target is None:
                cached = self._cached_chat_entry(serial, chat_name)
                if cached:
                    cx, cy, _ts, src = cached
                    adb.input_tap(serial, cx, cy)
                    await asyncio.sleep(jitter_ms(700, 1100))
                    try:
                        from src.integrations.messenger_rpa import thread_actions as _ta_c
                        # use_recent_cache=False：刚 tap 完必须真验证 chat
                        # 是否 land 对了，cache 不可信
                        vt_c = _ta_c.verify_thread_title(
                            serial, chat_name,
                            vision_cfg=self._vision_cfg(),
                            global_vision_cfg=self._global_vision_cfg(),
                            use_recent_cache=False,
                        )
                        if vt_c.ok:
                            target = UnreadChat(
                                name=chat_name, preview="", time="",
                                row_index=0, y_percent=0.0,
                                quality_hint=f"entry_cache:{src}",
                                score=99.0, skip_inbox_tap=True,
                            )
                            just_verified_ts = time.time()
                            result["screenshot_path"] = ""
                            result.setdefault("hints", []).append(
                                f"send_to_chat_name:entry_cache_hit:{src}",
                            )
                        else:
                            self._invalidate_chat_entry(serial, chat_name)
                            result.setdefault("hints", []).append(
                                f"entry_cache_miss:{vt_c.reason}",
                            )
                            try:
                                self._exit_thread(serial)
                            except Exception:
                                pass
                    except Exception as _ce:
                        self._invalidate_chat_entry(serial, chat_name)
                        result.setdefault("hints", []).append(
                            f"entry_cache_exc:{type(_ce).__name__}",
                        )

            # ★ Search-first：当 prefer_search=true 且未被调用方禁用时，
            # 绕过 inbox screencap+vision parse 直接走搜索框路径。
            # skip_search=True（drain 场景）时跳过，直接走 inbox scan，
            # 避免搜索结果打开 Profile 页而非 Chat 线程。
            if target is None and not skip_search and bool(
                self._cfg.get("send_to_chat_prefer_search", False)
            ):
                target = await self._search_chat_by_name(
                    serial, wh, chat_name, result,
                )
                search_first_tried = True
                if target:
                    # _search_chat_by_name 内部已 verify_thread_title ok 才返
                    # 回。同一 send 内的 safety net 可信任跳过。
                    just_verified_ts = time.time()
                    result.setdefault("hints", []).append(
                        "send_to_chat_name:search_first_hit",
                    )

            if target is None:
                inbox_png = await self._screenshot(serial, "send_inbox", run_id)
                if not inbox_png:
                    result["step"] = "send:screenshot_inbox_failed"
                    return self._finish(result, t0)
                result["screenshot_path"] = inbox_png

                # 用 combined vision 找未读 + 选 chat_name
                row_cap = self._inbox_row_cap_for_send_chat_name()
                if self._use_combined_vision:
                    _, unread = await self._inbox_combined(
                        inbox_png, result, max_rows=row_cap,
                    )
                else:
                    unread = await self._scan_inbox(
                        inbox_png, result, max_rows=row_cap,
                    )
                target = self._pick_unread_row_for_peer(
                    unread, chat_name, result,
                )
                scroll_try = int(
                    self._cfg.get("send_to_chat_inbox_scroll_attempts") or 0,
                )
                if target is None and scroll_try > 0:
                    w, h = wh
                    y1r = float(self._cfg.get("send_to_chat_scroll_y1_ratio") or 0.66)
                    y2r = float(self._cfg.get("send_to_chat_scroll_y2_ratio") or 0.44)
                    swipe_ms = int(self._cfg.get("send_to_chat_scroll_duration_ms") or 380)
                    swipe_ms = max(120, min(900, swipe_ms))
                    for si in range(scroll_try):
                        adb.input_swipe(
                            serial,
                            w // 2,
                            int(h * y1r),
                            w // 2,
                            int(h * y2r),
                            swipe_ms,
                        )
                        await asyncio.sleep(0.55)
                        sp2 = await self._screenshot(
                            serial, f"send_inbox_sc{si}", run_id,
                        )
                        if not sp2:
                            continue
                        try:
                            png_blob = Path(sp2).read_bytes()
                        except OSError:
                            result.setdefault("hints", []).append(
                                f"send_to_chat_name:scroll_png_read_err_{si}",
                            )
                            continue
                        if not _messenger_png_screencap_ok(png_blob):
                            result.setdefault("hints", []).append(
                                f"send_to_chat_name:scroll_bad_png_skip_{si}",
                            )
                            continue
                        result["screenshot_path"] = sp2
                        if self._use_combined_vision:
                            _, unread2 = await self._inbox_combined(
                                sp2, result, max_rows=row_cap,
                            )
                        else:
                            unread2 = await self._scan_inbox(
                                sp2, result, max_rows=row_cap,
                            )
                        target = self._pick_unread_row_for_peer(
                            unread2, chat_name, result,
                        )
                        if target:
                            result.setdefault("hints", []).append(
                                f"send_to_chat_name:scroll_hit_{si}",
                            )
                            break
            if not target:
                # chat_name 已不在未读列表（可能已被其他人/我们自己读过了）
                # 兜底搜索框；search-first 已跑过的 case 不再重复
                # ★ skip_search=True（drain 场景）时不再做无效兜底搜索：
                # drain 本意是"这条 reply 早就生成过、chat 应该还在未读；
                # 若已不在未读说明对方/我们已读，没必要再花 12-16s 搜索"。
                # 直接返回让上层 mark_deferred_failed("chat_not_in_unread")。
                if not search_first_tried and not skip_search:
                    target = await self._search_chat_by_name(
                        serial, wh, chat_name, result,
                    )
                    if target:
                        just_verified_ts = time.time()
                elif skip_search:
                    result.setdefault("hints", []).append(
                        "skip_search_honored:chat_not_in_unread"
                    )
                if not target:
                    result["step"] = "send:chat_not_found"
                    result["error"] = f"chat_name={chat_name!r} 不在未读列表也搜不到"
                    return self._finish(result, t0)

            # ── Tap + Verify（最多 2 次：首次 + 1 次 fresh-scan 重试） ──────────
            # 处理「vision 截图 vs XML dump 之间 inbox 顺序变动」的竞态条件：
            # 首次进错线程后退出 → 重新 foreground + 截图 → 用最新位置重 tap。
            for _send_attempt in range(2):
                if _send_attempt > 0:
                    try:
                        await self._foreground_messenger(serial, result)
                        await asyncio.sleep(1.2)
                        _r_png = await self._screenshot(
                            serial, "send_retry", run_id,
                        )
                        if not _r_png:
                            break
                        row_cap = self._inbox_row_cap_for_send_chat_name()
                        if self._use_combined_vision:
                            _, _r_unread = await self._inbox_combined(
                                _r_png, result, max_rows=row_cap,
                            )
                        else:
                            _r_unread = await self._scan_inbox(
                                _r_png, result, max_rows=row_cap,
                            )
                        _r_tgt = self._pick_unread_row_for_peer(
                            _r_unread, chat_name, result,
                        )
                        if not _r_tgt:
                            break
                        target = _r_tgt
                    except Exception as _re:
                        result.setdefault("hints", []).append(
                            f"send_retry_exc:{type(_re).__name__}",
                        )
                        break

                inbox_tap_xy: Optional[Tuple[int, int, str]] = None
                if not target.skip_inbox_tap:
                    inbox_tap_xy = self._tap_chat_row(serial, wh, target)
                    if inbox_tap_xy and inbox_tap_xy[2] == "inbox_self_sent_skip":
                        result["step"] = "send:inbox_self_sent_skip"
                        result["ok"] = True
                        return self._finish(result, t0)
                    await asyncio.sleep(jitter_ms(800, 1500))
                elif target.quality_hint == "already_in_thread":
                    await asyncio.sleep(jitter_ms(120, 320))
                else:
                    # 搜索路径等：已在 thread，略等 UI 稳定即可
                    await asyncio.sleep(jitter_ms(420, 900))

                # ── U1 前置校验（view-tree 顶栏二次核对） ──
                try:
                    from src.integrations.messenger_rpa import thread_actions as _ta
                    bypass_sec = float(
                        self._cfg.get("safety_net_verify_bypass_sec", 4.0) or 0.0
                    )
                    age = time.time() - just_verified_ts
                    if just_verified_ts > 0 and age < bypass_sec:
                        vt = _ta.VerifyResult(
                            ok=True, actual=chat_name, expected=chat_name,
                            reason=f"recently_verified_skip_age={age:.1f}s",
                        )
                        result["title_verify"] = {
                            "ok": True, "actual": chat_name,
                            "reason": vt.reason,
                        }
                        result.setdefault("hints", []).append(
                            f"safety_net_dedup_age={age:.1f}s",
                        )
                    else:
                        vt = _ta.verify_thread_title(
                            serial, chat_name,
                            vision_cfg=self._vision_cfg(),
                            global_vision_cfg=self._global_vision_cfg(),
                            use_recent_cache=False,
                        )
                        result["title_verify"] = {
                            "ok": vt.ok,
                            "actual": vt.actual,
                            "reason": vt.reason,
                        }
                    if vt.ok and inbox_tap_xy is not None:
                        ix, iy, isrc = inbox_tap_xy
                        self._record_chat_entry(
                            serial, chat_name, ix, iy, source=f"inbox:{isrc}",
                        )
                        result.setdefault("hints", []).append(
                            f"entry_cache_recorded:inbox:{isrc}",
                        )
                    if not vt.ok:
                        result["step"] = "send:wrong_thread_opened"
                        result["error"] = (
                            f"title mismatch: expected={chat_name!r} "
                            f"actual={vt.actual!r} reason={vt.reason}"
                        )
                        try:
                            self._exit_thread(serial)
                        except Exception:
                            pass
                        if _send_attempt < 1:
                            result.setdefault("hints", []).append(
                                "send_wrong_retrying",
                            )
                            continue
                        return self._finish(result, t0)
                except Exception as _vex:
                    # U1 出错不应阻断发送（fail-open），但记录 hint 供审计
                    result.setdefault("hints", []).append(
                        f"title_verify_exception:{type(_vex).__name__}",
                    )
                break  # verify 通过（或 fail-open）→ 进入发送

            # ★ W2-D3.1：真发前 burst typing（让对方"看到 AI 在打字"几秒）
            burst_task = None
            if typing_pulse_sec and typing_pulse_sec > 0.5:
                try:
                    burst_task = asyncio.create_task(
                        self._typing_indicator_burst(
                            serial, wh, duration_sec=typing_pulse_sec,
                        ),
                        name="typing_burst_before_send",
                    )
                    # 给 burst 1 秒头部时间（让对方那边 typing 状态先亮起来）
                    await asyncio.sleep(min(1.0, typing_pulse_sec * 0.4))
                except Exception:
                    burst_task = None

            # 跳过 thread guard（因为是审批后的自主发送）
            try:
                sent_ok = await self._send_reply_with_retry(serial, wh, reply_text, result)
            finally:
                if burst_task is not None:
                    burst_task.cancel()
                    try:
                        await burst_task
                    except (asyncio.CancelledError, Exception):
                        pass
            if not sent_ok:
                result["step"] = "send:send_failed"
                return self._finish(result, t0)
            result["step"] = "send:sent"
            result["ok"] = True

            # 心跳：发送成功 → recent_verify_cache TS 续期。下次发同一人在
            # TTL 内可直接跳过 verify。
            try:
                from src.integrations.messenger_rpa import recent_verify_cache as _rvc
                _rvc.send_succeeded(serial, chat_name)
            except Exception:
                logger.debug("recent_verify_cache 心跳失败", exc_info=True)

            # ── U4 发送后端到端 ASSERT（view-tree 版） ──
            # 不依赖 Vision；失败不回退 ok，但会标注 assert 字段供上游排障。
            try:
                from src.integrations.messenger_rpa import thread_actions as _ta
                _sent = await _ta.assert_sent(
                    serial, reply_text,
                    screen_w=int(wh[0]), screen_h=int(wh[1]),
                    wait_sec=0.8,
                )
                result["post_send_assert"] = {
                    "ok": _sent.ok,
                    "reason": _sent.reason,
                    "seen_by": _sent.seen_by,
                }
                if not _sent.ok:
                    result.setdefault("hints", []).append(
                        f"post_send_not_observed:{_sent.reason}",
                    )
            except Exception as _aex:
                result.setdefault("hints", []).append(
                    f"post_send_assert_exception:{type(_aex).__name__}",
                )

            self._exit_thread(serial)
            return self._finish(result, t0)
        except Exception as ex:
            logger.exception("send_to_chat_name 异常")
            result["step"] = result["step"] or "send:exception"
            result["error"] = f"{type(ex).__name__}: {ex}"
            return self._finish(result, t0)

    # ── P2-D: per-chat 搜索失败熔断 helper ────────────────────────
    def _search_circuit_is_open(self, chat_name: str) -> Tuple[bool, float]:
        """返回 (is_open, remaining_sec)。is_open=True 表示应该跳过本次搜索。"""
        st = self._search_failure_state.get(chat_name)
        if not st:
            return False, 0.0
        until = float(st.get("cooldown_until", 0.0))
        if until <= 0:
            return False, 0.0
        now = time.time()
        if until <= now:
            # 冷却窗口已过 → 清空状态（给下一次新鲜机会）
            self._search_failure_state.pop(chat_name, None)
            return False, 0.0
        return True, until - now

    def _search_circuit_record_failure(self, chat_name: str) -> Dict[str, float]:
        """累加失败计数，达到阈值则开启冷却。返回更新后的 state dict。"""
        now = time.time()
        max_fails = int(self._cfg.get("search_failure_max", 3) or 3)
        cooldown_sec = float(
            self._cfg.get("search_cooldown_sec", 1800) or 1800,
        )
        st = self._search_failure_state.setdefault(
            chat_name, {"fails": 0.0, "cooldown_until": 0.0, "last_fail_ts": 0.0},
        )
        st["fails"] = float(st.get("fails", 0.0)) + 1.0
        st["last_fail_ts"] = now
        if st["fails"] >= max_fails:
            st["cooldown_until"] = now + cooldown_sec
            logger.warning(
                "[messenger_rpa] P2-D search circuit OPEN: chat=%r fails=%d "
                "cooldown_sec=%.0f",
                chat_name, int(st["fails"]), cooldown_sec,
            )
        # P3-C: metrics
        try:
            from src.monitoring.metrics_store import get_metrics_store
            get_metrics_store().record_search_chat(ok=False)
        except Exception:
            pass
        return st

    def _search_circuit_record_success(self, chat_name: str) -> None:
        """成功即清空失败状态，关闭熔断。"""
        if chat_name in self._search_failure_state:
            prev_fails = int(self._search_failure_state[chat_name].get("fails", 0))
            if prev_fails > 0:
                logger.info(
                    "[messenger_rpa] P2-D search circuit RESET: chat=%r "
                    "prev_fails=%d",
                    chat_name, prev_fails,
                )
        self._search_failure_state.pop(chat_name, None)
        # P3-C: metrics
        try:
            from src.monitoring.metrics_store import get_metrics_store
            get_metrics_store().record_search_chat(ok=True)
        except Exception:
            pass

    async def _search_chat_by_name(
        self,
        serial: str,
        wh: Tuple[int, int],
        chat_name: str,
        result: Dict[str, Any],
    ) -> Optional[UnreadChat]:
        """搜索框 → 清空 → 输入名字 → **dump 匹配行 / 坐标回退** → U1 校验。

        旧实现盲点 ``chat_row_for(0)``，Messenger 搜索排序下极易开错会话。
        现逻辑：每次 tap 后用 ``verify_thread_title`` 确认顶栏；失败则 ``BACK``
        并重开搜索再试下一候选（仍失败则返回 ``None``）。

        P2-D: 入口加熔断检查 —— 若该 chat 在冷却窗口内，直接返回 None，
        避免对同名陌生人/已删好友反复 4 轮浪费 12-16s。
        """
        from src.integrations.messenger_rpa import thread_actions as _ta
        from src.integrations.messenger_rpa import ui_scraper as _uis
        from src.integrations.messenger_rpa.text_input import inject_text

        name = (chat_name or "").strip()
        if not name:
            return None
        # P2-D: 熔断检查 - 冷却窗口内直接短路
        is_open, remaining = self._search_circuit_is_open(name)
        if is_open:
            result.setdefault("hints", []).append(
                f"search_circuit_open:{name}:remaining={int(remaining)}s"
            )
            logger.info(
                "[messenger_rpa] P2-D search circuit OPEN: chat=%r skip, "
                "remaining=%.0fs",
                name, remaining,
            )
            # P3-C: 熔断短路 → 记 skip
            try:
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().record_search_chat(skipped=True)
            except Exception:
                pass
            return None
        search_bar_taps = cc.inbox_search_tap_candidates(wh[0], wh[1])

        async def _open_search_and_type(bar_idx: int) -> bool:
            # 先回 Chats：避免停在 Calls/Menu 时误点其它区域导致搜不到人
            tcx, tcy = cc.TAB_CHATS.at(*wh)
            adb.input_tap(serial, tcx, tcy)
            await asyncio.sleep(0.38)
            sx, sy = search_bar_taps[bar_idx % len(search_bar_taps)]
            adb.input_tap(serial, sx, sy)
            await asyncio.sleep(0.55)
            adb.run_adb(
                ["shell", "input", "keyevent"] + ["KEYCODE_DEL"] * 40,
                serial=serial, timeout=8.0,
            )
            await asyncio.sleep(0.12)
            ime = (self._cfg.get("adb_keyboard_ime") or "").strip()
            use_adb_keyboard = bool(self._cfg.get("use_adb_keyboard", True))
            ir = inject_text(
                serial,
                name,
                use_adb_keyboard=use_adb_keyboard,
                adb_keyboard_ime=ime,
                adb_keyboard_package=(
                    self._cfg.get("adb_keyboard_package")
                    or "com.android.adbkeyboard"
                ).strip(),
                allow_clipboard_fallback=bool(
                    self._cfg.get("allow_clipboard_fallback", True)
                ),
                allow_input_text_fallback_for_ascii=bool(
                    self._cfg.get("allow_input_text_fallback_for_ascii", True)
                ),
            )
            if not ir.ok:
                result.setdefault("hints", []).append(
                    f"search_inject_failed:{ir.path}:{ir.error[:80]}",
                )
                return False
            return True

        tried_xy: set[Tuple[int, int]] = set()

        def _build_tap_plan() -> List[Tuple[int, int, str]]:
            taps: List[Tuple[int, int, str]] = []
            xml = _ta.dump_view_tree(serial)
            if xml:
                for cx, cy, _sc, reason in _uis.find_search_suggestion_taps(
                    xml,
                    name,
                    screen_w=int(wh[0]),
                    screen_h=int(wh[1]),
                ):
                    taps.append((cx, cy, f"xml:{reason}"))
            # ★ 搜索 overlay 的公式坐标（inbox 行公式）不适用：
            # 搜索结果区与 inbox 布局不同，formula y 可能打到 All/People/Messages
            # 过滤 tab，导致切换 tab 而非打开会话。只在 XML 命中为空时才加入
            # 最多 3 条 formula 兜底（已在 XML 尝试完后使用）。
            if not taps:
                taps.extend(self._search_result_fallback_taps(wh))
            seen: set[Tuple[int, int]] = set()
            out: List[Tuple[int, int, str]] = []
            for tx, ty, tag in taps:
                if (tx, ty) in tried_xy:
                    continue
                key = (tx // 55, ty // 40)
                if key in seen:
                    continue
                seen.add(key)
                out.append((tx, ty, tag))
            return out[:10]

        try:
            if not await _open_search_and_type(0):
                self._search_circuit_record_failure(name)  # P2-D
                result.setdefault("hints", []).append(
                    "search_fail:open_type_initial"
                )
                return None
            await asyncio.sleep(1.4)

            for _round in range(4):
                plan = _build_tap_plan()
                if not plan:
                    self._search_circuit_record_failure(name)  # P2-D
                    result.setdefault("hints", []).append(
                        f"search_fail:empty_plan_round={_round}"
                    )
                    return None
                tx, ty, tag = plan[0]
                tried_xy.add((tx, ty))
                adb.input_tap(serial, tx, ty)
                await asyncio.sleep(0.92)
                # 搜索 loop 内的 verify：刚 tap 候选行，cache hit 等于跳过
                # 安全检查 → use_recent_cache=False 保 safety
                vt = _ta.verify_thread_title(
                    serial, name,
                    vision_cfg=self._vision_cfg(),
                    global_vision_cfg=self._global_vision_cfg(),
                    use_recent_cache=False,
                )
                result.setdefault("hints", []).append(
                    f"search_try:{tag}:ok={vt.ok}:reason={vt.reason}"
                    f":actual={vt.actual!r}",
                )
                if vt.ok:
                    result.setdefault("hints", []).append(
                        f"search_chat_by_name:opened_ok:{tag}",
                    )
                    # 记下"这个 tap 对该 chat 工作过"——下次发同人时可以
                    # 直接 tap 这个坐标 + verify，跳过整个 search 流程。
                    self._record_chat_entry(
                        serial, name, tx, ty, source=f"search:{tag}",
                    )
                    # P2-D: 成功即清零熔断计数
                    self._search_circuit_record_success(name)
                    return UnreadChat(
                        name=name,
                        preview="",
                        time="",
                        row_index=0,
                        y_percent=0.0,
                        quality_hint="search_u1_ok",
                        score=100.0,
                        skip_inbox_tap=True,
                    )
                adb.input_keyevent(serial, "KEYCODE_BACK")
                await asyncio.sleep(0.48)
                bar_idx = (_round + 1) % len(search_bar_taps)
                if not await _open_search_and_type(bar_idx):
                    self._search_circuit_record_failure(name)  # P2-D
                    result.setdefault("hints", []).append(
                        f"search_fail:open_type_round={_round}"
                    )
                    return None
                await asyncio.sleep(1.4)
            # 搜索全部轮次失败 → 按 BACK 退出搜索 overlay，回到 inbox
            # 让上游 send_to_chat_name 的 inbox scan fallback 能正常工作
            try:
                adb.input_keyevent(serial, "KEYCODE_BACK")
                time.sleep(0.4)
                tcx, tcy = cc.TAB_CHATS.at(*wh)
                adb.input_tap(serial, tcx, tcy)
                time.sleep(0.4)
            except Exception:
                pass
            # P2-D: 所有 4 轮都 U1 失败
            st = self._search_circuit_record_failure(name)
            result.setdefault("hints", []).append(
                f"search_fail:all_rounds_u1_miss:fails={int(st['fails'])}"
            )
            return None
        except Exception as ex:
            logger.warning("[messenger_rpa] _search_chat_by_name 异常: %s", ex)
            self._search_circuit_record_failure(name)  # P2-D
            result.setdefault("hints", []).append(
                f"search_chat_by_name_exc:{type(ex).__name__}",
            )
            return None

    # ── 内部：设备/屏幕 ───────────────────────────
    def _resolve_serial(self, result: Dict[str, Any]) -> Optional[str]:
        cfg_serial = (self._cfg.get("adb_serial") or "").strip()
        if cfg_serial:
            # ★ P18 反空转：连续 unhealthy 后进入 backoff，跳过 ensure_device_ready
            # 30s 等待。避免 USB 物理掉线时 service 持续 6+ 分钟空转。
            _bo_until_m = float(
                self._device_unhealthy_skip_until.get(cfg_serial, 0.0) or 0.0
            )
            if _bo_until_m > time.monotonic():
                _remaining = int(_bo_until_m - time.monotonic())
                result["step"] = "device_unhealthy_backoff"
                result["error"] = (
                    f"device_unhealthy_backoff: {_remaining}s remaining "
                    f"(streak={self._device_unhealthy_streak.get(cfg_serial, 0)})"
                )
                result.setdefault("hints", []).append(
                    f"device_unhealthy_backoff:{_remaining}s"
                )
                return None

            # ★ P4-2：heal cache — 同 serial N 秒内不重复 heal
            # 成功后短缓存，失败后更短缓存（好让下次 run 重试）
            hc_cfg = (self._cfg.get("adb_healthcheck") or {})
            heal_cache_sec = float(hc_cfg.get("heal_cache_sec", 20.0) or 20.0)
            now = time.time()
            if not hasattr(self, "_heal_cache"):
                self._heal_cache = {}  # serial → (last_ok_ts, last_info)
            last = self._heal_cache.get(cfg_serial)
            if last and (now - last[0]) < heal_cache_sec and last[1].get("ok"):
                result["device_health"] = {**last[1], "cache_hit": True}
                # 健康路径：清 streak
                self._device_unhealthy_streak.pop(cfg_serial, None)
                return cfg_serial

            # ★ 设备健康守护：自动重连/唤醒/解锁 + IME 预检
            try:
                from src.integrations.messenger_rpa.device_health import (
                    ensure_device_ready,
                )
                healthy, info = ensure_device_ready(
                    cfg_serial,
                    try_reconnect=bool(self._cfg.get("auto_reconnect", True)),
                    try_wake=bool(self._cfg.get("auto_wake", True)),
                    try_unlock_swipe=bool(
                        self._cfg.get("auto_unlock_swipe", True)
                    ),
                    max_attempts=int(self._cfg.get("device_max_attempts", 3)),
                    preferred_ime=str(
                        hc_cfg.get("preferred_ime", "") or ""
                    ).strip() or None,
                    hard_restart_on_fail=bool(
                        hc_cfg.get("hard_restart_on_fail", True)
                    ),
                )
                result["device_health"] = info
                if healthy:
                    self._heal_cache[cfg_serial] = (now, info)
                    # P18：健康路径清 streak / backoff
                    self._device_unhealthy_streak.pop(cfg_serial, None)
                    self._device_unhealthy_skip_until.pop(cfg_serial, None)
                if not healthy:
                    result["step"] = "device_unhealthy"
                    err_attempts = info.get("attempts") or []
                    last_err = (
                        err_attempts[-1].get("error", "") if err_attempts else ""
                    )
                    result["error"] = f"device_health: {last_err}"
                    # ── P18 反空转：累加 streak，达阈值进 backoff ──
                    _streak = self._device_unhealthy_streak.get(
                        cfg_serial, 0
                    ) + 1
                    self._device_unhealthy_streak[cfg_serial] = _streak
                    _thr = int(self._cfg.get(
                        "device_unhealthy_streak_threshold", 3
                    ) or 3)
                    _bo = float(self._cfg.get(
                        "device_unhealthy_backoff_sec", 60.0
                    ) or 0.0)
                    if _streak >= _thr and _bo > 0:
                        self._device_unhealthy_skip_until[cfg_serial] = (
                            time.monotonic() + _bo
                        )
                        result.setdefault("hints", []).append(
                            f"device_unhealthy_backoff_armed:{int(_bo)}s"
                            f":streak={_streak}"
                        )
                    result["device_unhealthy_streak"] = _streak
                    return None
            except Exception as ex:
                logger.exception("[runner] device_health 异常")
                result["device_health"] = {
                    "ok": False,
                    "error": f"{type(ex).__name__}: {ex}",
                }
            return cfg_serial
        serials = adb.list_device_serials()
        if not serials:
            result["step"] = "no_adb_device"
            result["error"] = "adb devices 列表为空"
            return None
        return serials[0]

    def _screen_size(self, serial: str) -> Tuple[int, int]:
        if serial in self._screen_wh_cache:
            return self._screen_wh_cache[serial]
        wh = adb.screen_size(serial)
        if not wh:
            wh = (cc.BASE_WIDTH, cc.BASE_HEIGHT)
        self._screen_wh_cache[serial] = wh
        return wh

    def _foreground_messenger(self, serial: str, result: Dict[str, Any]) -> bool:
        """前台化 Messenger 并强制归位到 Chats tab。

        Messenger 是单 Activity + 多 Fragment 架构（Bloks），单看 mCurrentFocus
        永远是 StartScreenActivity，无法区分 thread/inbox。
        因此每次都按"BACK 一次 + 点 Chats tab"来 best-effort 归位：
        - 如果当前在 thread 里：BACK 回 inbox 列表 → 点 Chats tab（无害）
        - 如果当前在 modal 里：BACK 关闭 modal → 点 Chats tab（无害）
        - 如果当前已在 Chats inbox：BACK 可能误退 Messenger，
          → 用 dumpsys 检查 launcher 是否抢焦点；若是再 am start 一次。
        """
        # 多用户切换（MIUI XSpace）必须先 force-stop，否则 am start 会复用旧 user 的进程
        if self._adb_user_id is not None and bool(
            self._cfg.get("force_stop_before_start", True)
        ):
            other_user = 999 if self._adb_user_id == 0 else 0
            adb.run_adb(
                [
                    "shell", "am", "force-stop",
                    "--user", str(other_user),
                    MESSENGER_PKG,
                ],
                serial=serial,
                timeout=8.0,
            )
            time.sleep(0.4)

        am_args = self._am_start_args()
        logger.info(
            "[messenger_rpa] foreground am start args=%s user_id=%s",
            am_args, self._adb_user_id,
        )
        r = adb.run_adb(
            am_args,
            serial=serial,
            timeout=15.0,
        )
        if r.returncode != 0:
            result["step"] = "foreground_failed"
            result["error"] = (r.stderr or r.stdout or "")[:200]
            return False
        time.sleep(0.8)

        # ★ 防错位校验：dump 当前 ResumedActivity 看是不是预期 user
        if self._adb_user_id is not None:
            actual_user = self._dumpsys_resumed_user(serial)
            if actual_user is not None and actual_user != self._adb_user_id:
                logger.warning(
                    "[messenger_rpa] user 错位：期望 u%d 但 ResumedActivity 在 u%d，"
                    "再次 force-stop + am start",
                    self._adb_user_id, actual_user,
                )
                # 强制把错的 user 关掉，重新启
                adb.run_adb(
                    [
                        "shell", "am", "force-stop",
                        "--user", str(actual_user),
                        MESSENGER_PKG,
                    ],
                    serial=serial,
                    timeout=8.0,
                )
                time.sleep(0.5)
                adb.run_adb(am_args, serial=serial, timeout=15.0)
                time.sleep(0.8)

        # 最多 3 次 BACK 闪避（thread / modal / 设置深层子页）
        for _back_i in range(3):
            adb.input_keyevent(serial, "KEYCODE_BACK")
            time.sleep(0.4)
            # BACK 之后 launcher 可能抢焦点，需要检测并重启
            if self._is_messenger_lost(serial):
                adb.run_adb(
                    self._am_start_args(skip_w=True),
                    serial=serial,
                    timeout=10.0,
                )
                time.sleep(0.6)
                break  # 已重新拉起，无需继续按 BACK

        # 点 Chats tab（保证站在 inbox fragment）
        wh = self._screen_size(serial)
        x, y = cc.TAB_CHATS.at(*wh)
        adb.input_tap(serial, x, y)
        time.sleep(0.6)
        return True

    def _am_start_args(self, *, skip_w: bool = False) -> List[str]:
        """组装 am start 参数；多用户机器必须显式 --user，否则落到上次 active user。"""
        args: List[str] = ["shell", "am", "start"]
        if not skip_w:
            args.append("-W")
        if self._adb_user_id is not None:
            args.extend(["--user", str(self._adb_user_id)])
        args.extend(
            [
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.LAUNCHER",
                "-n",
                MESSENGER_LAUNCH_ACTIVITY,
            ]
        )
        return args

    def _dumpsys_resumed_user(self, serial: str) -> Optional[int]:
        """读 dumpsys activity 的 ResumedActivity，提取 'u\\d+' 用户号。"""
        try:
            r = adb.run_adb(
                ["shell", "dumpsys", "activity", "activities"],
                serial=serial,
                timeout=6.0,
            )
            stdout = r.stdout or ""
        except Exception:
            return None
        # 找类似 "ResumedActivity: ActivityRecord{... u0 com.facebook.orca/..."
        import re
        for line in stdout.splitlines():
            if "ResumedActivity" in line and MESSENGER_PKG in line:
                m = re.search(r"\bu(\d+)\b\s+" + re.escape(MESSENGER_PKG), line)
                if m:
                    try:
                        return int(m.group(1))
                    except (TypeError, ValueError):
                        return None
        return None

    def _is_messenger_lost(self, serial: str) -> bool:
        """判断 Messenger 是否已经离开前台（被 BACK 误退到 launcher）。"""
        try:
            r = adb.run_adb(
                ["shell", "dumpsys", "window"], serial=serial, timeout=5.0
            )
            stdout = r.stdout or ""
        except Exception:
            return True
        for line in stdout.splitlines():
            if "mCurrentFocus=" in line:
                return "com.facebook.orca/" not in line
        return True

    async def _screenshot(
        self, serial: str, tag: str, run_id: str
    ) -> Optional[str]:
        """exec-out screencap → 写到 debug_dir。

        P0：MIUI/USB 上 ``device not found``、空包、非 PNG 头较常见；做
        **带退避的重试** + 可选 **wait-for-device / adb reconnect** 再拉一次，
        显著降低 ``send:screenshot_inbox_failed`` 误杀。
        """
        sc_cfg = self._cfg.get("screencap") or {}
        max_retries = max(1, min(10, int(sc_cfg.get("max_retries", 6) or 6)))
        heal = bool(sc_cfg.get("heal_on_transient_fail", True))
        allow_global_reconnect = bool(sc_cfg.get("allow_global_reconnect", True))
        last_err = ""
        did_reconnect = False
        try:
            for attempt in range(max_retries):
                if attempt > 0:
                    delay = min(0.22 * (1.65 ** (attempt - 1)), 2.8)
                    await asyncio.sleep(delay)
                    if heal:
                        wfd = adb.run_adb(
                            ["wait-for-device"], serial=serial, timeout=22.0,
                        )
                        if wfd.returncode != 0:
                            logger.debug(
                                "[messenger_rpa] wait-for-device rc=%s err=%r",
                                wfd.returncode, (wfd.stderr or "")[:120],
                            )
                        if (
                            attempt >= 3
                            and not did_reconnect
                            and allow_global_reconnect
                        ):
                            adb.run_adb(["reconnect"], serial=None, timeout=14.0)
                            did_reconnect = True
                            await asyncio.sleep(0.55)

                png_bytes, err, code = adb.run_adb_binary(
                    ["exec-out", "screencap", "-p"],
                    serial=serial, timeout=22.0,
                )
                last_err = err or ""
                ok = (
                    code == 0
                    and _messenger_png_screencap_ok(png_bytes)
                )
                if ok:
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    path = self._debug_dir / f"{ts}_{run_id}_{tag}.png"
                    path.write_bytes(png_bytes)
                    if attempt > 0:
                        logger.info(
                            "[messenger_rpa] screencap 第 %d 次成功 tag=%s bytes=%d",
                            attempt + 1, tag, len(png_bytes),
                        )
                    return str(path)

                transient = adb.adb_stderr_looks_transient(err or "") or code == 124
                logger.warning(
                    "[messenger_rpa] screencap 失败 attempt=%d/%d tag=%s "
                    "code=%d transient=%s err=%s head=%r",
                    attempt + 1,
                    max_retries,
                    tag,
                    code,
                    transient,
                    (err or "")[:160],
                    png_bytes[:24] if png_bytes else b"",
                )
                if attempt == max_retries - 1:
                    break
            return None
        except Exception:
            logger.exception("截图保存失败 last_err=%r", last_err[:200])
            return None

    # ── 内部：守卫屏 ─────────────────────────────
    async def _handle_guard(
        self,
        serial: str,
        image_path: str,
        result: Dict[str, Any],
        where: str,
    ) -> Any:
        """检测 modal；若有则尝试闪避（点 OK / 按 BACK / 等待人工）。

        ★ 防误闪避策略 ★
        - 在 thread 页（where='thread'）只信任**已知白名单 modal**
          （note_reactions / previews_on / send_first_like / permission_dialog）
        - other_modal 在任何页面、任何置信度都不主动闪避（避免 BACK 把会话退回 Inbox）
        - profile_picker 除外（必须报警）
        """
        vision_cfg = self._vision_cfg()
        global_vision = self._global_vision_cfg()
        guard, tag = await detect_guard_screen(
            image_path,
            vision_cfg=vision_cfg,
            global_vision=global_vision,
        )
        if guard.is_clear:
            return guard

        logger.info(
            "[messenger_rpa] guard@%s type=%s action=%s conf=%s title=%r tag=%s",
            where, guard.type, guard.action, guard.confidence, guard.title, tag,
        )
        result.setdefault("guard_history", []).append(
            {
                "where": where,
                "type": guard.type,
                "action": guard.action,
                "confidence": guard.confidence,
                "title": guard.title,
            }
        )

        if guard.action == ACTION_NEED_HUMAN:
            return guard

        # ★ 防御性策略：只对 4 个已知 modal 类型执行闪避
        trusted_types = {
            "note_reactions",
            "previews_on",
            "send_first_like",
            "permission_dialog",
        }
        if guard.type not in trusted_types:
            logger.info(
                "[messenger_rpa] guard@%s type=%s 不在 trusted_types 白名单，跳过闪避",
                where, guard.type,
            )
            # 仍记录但不动手；让上层把 guard.type 当 'none' 处理
            return guard.__class__(
                type="none",
                action=ACTION_NONE,
                title=guard.title,
                confidence=guard.confidence,
                raw=guard.raw,
            )

        wh = self._screen_size(serial)
        if guard.action == ACTION_TAP_OK:
            x, y = cc.MODAL_OK_BTN.at(*wh)
            adb.input_tap(serial, x, y)
        elif guard.action == ACTION_TAP_CLOSE_X:
            x, y = cc.MODAL_CLOSE_X.at(*wh)
            adb.input_tap(serial, x, y)
        elif guard.action == ACTION_PRESS_BACK:
            adb.input_keyevent(serial, "KEYCODE_BACK")
        else:
            pass

        await asyncio.sleep(0.6)
        return guard

    # ── 内部：合并 vision（默认路径）──────────────
    def _inbox_row_cap_for_send_chat_name(self) -> int:
        """``send_to_chat_name`` 需在列表里找人：不能只用 ``max_inbox_per_run=1``。"""
        raw = int(self._cfg.get("send_to_chat_inbox_row_cap") or 16)
        return max(self._max_inbox_per_run, max(1, min(28, raw)))

    def _pick_unread_row_for_peer(
        self,
        unread: List[UnreadChat],
        chat_name: str,
        result: Dict[str, Any],
    ) -> Optional[UnreadChat]:
        """Vision 未读行里找 ``chat_name``；失败则从 ``inbox_ranking`` 合成一行。"""
        hints = result.setdefault("hints", [])
        mpl = int(self._cfg.get("send_to_chat_preview_match_min_len") or 4)
        return pick_unread_row_for_peer_name(
            unread,
            chat_name,
            result.get("inbox_ranking"),
            min_preview_substr_len=mpl,
            hint_out=hints,
        )

    def _latest_thread_snippet_is_self(
        self,
        serial: str,
        result: Dict[str, Any],
        peer_msg: Optional[PeerMessage] = None,
        chat_name: str = "",
    ) -> bool:
        """Cheap hard guard against replying to our own latest thread message.

        P1-C (2026-05-03)：原有 latest_snippet_row 依赖 SimpleTextThreadSnippet
        marker，仅 inbox 列表行可靠（thread 内气泡通常没有该 marker，且自方
        气泡 preview 没有 "You:" 前缀），导致 03:40-03:46 死循环时此守卫**没
        拦下**。新增"气泡 cx 几何启发 + last_sent_at 时间窗双信号"路径：
        thread 内气泡按 cx > screen_w * left_ratio 判 self/peer，配合"我方
        刚发不久"的时间窗加成防误判（cx 启发偶发不准时 fallback 到 vision）。
        旧 latest_snippet_row 路径保留作兜底（仍能识别 self media placeholder）。
        """
        if not bool(self._cfg.get("thread_self_xml_guard", True)):
            return False
        try:
            from src.integrations.messenger_rpa import thread_actions as _ta
            from src.integrations.messenger_rpa import ui_scraper as _uis

            xml = _ta.dump_view_tree(
                serial,
                dump_timeout=float(self._cfg.get("ui_dump_timeout_s") or 6.0),
                cat_timeout=4.0,
            )
            if not xml:
                result.setdefault("hints", []).append("thread_self_xml_guard:no_xml")
                return False
            in_thread = _uis.is_in_thread(xml)
            if not in_thread:
                # The Messenger top-bar XML is not stable across locales/builds.  This
                # guard is only called after we have already opened a candidate thread,
                # so still inspect the latest visible snippet before trusting Vision.
                result.setdefault("hints", []).append("thread_self_xml_guard:not_thread_but_checking")

            # ── P1-C: 气泡 cx 几何 + last_sent_at 时间窗双信号 ──
            # 不依赖 in_thread（旧注释已说明顶栏 XML 在不同 locale/build
            # 下不稳定）。仅靠两个独立信号判定：
            #   ① 我方刚发不久（last_sent_at 在窗口内）
            #   ② 最末气泡 cx > left_ratio * screen_w（在右侧）
            # 两条都成立时几乎一定是真 self 气泡（误判风险极低）。
            if bool(self._cfg.get("thread_xml_bubble_guard", True)):
                try:
                    _eff_chat_name = chat_name or str(
                        result.get("chat_name") or ""
                    )
                    _last_sent = 0.0
                    if _eff_chat_name:
                        _ck_xbg = f"{self._chat_key_prefix}:{_eff_chat_name}"
                        try:
                            _cs_xbg = self._state.get_chat_state(_ck_xbg)
                            _last_sent = float(
                                _cs_xbg.get("last_sent_at") or 0
                            )
                        except Exception:
                            _last_sent = 0.0
                    try:
                        # P19 (2026-05-04): 默认 90 → 300s。u2 让 XML 可读，
                        # last_bubble_preview cx 检测稳定。延长窗口覆盖 peer
                        # 反应慢的对话节奏（cycle 间距常 3–5 min）。
                        _guard_window = float(
                            self._cfg.get(
                                "thread_xml_bubble_guard_window_sec", 300
                            ) or 0
                        )
                    except (TypeError, ValueError):
                        _guard_window = 300.0
                    if (
                        _guard_window > 0
                        and _last_sent > 0
                        and (time.time() - _last_sent) < _guard_window
                    ):
                        try:
                            _wh_xbg = self._screen_size(serial)
                            _screen_w = (
                                int(_wh_xbg[0]) if _wh_xbg else 720
                            )
                        except Exception:
                            _screen_w = 720
                        try:
                            _left_ratio = float(
                                self._cfg.get(
                                    "thread_xml_bubble_guard_left_ratio", 0.6
                                ) or 0.6
                            )
                        except (TypeError, ValueError):
                            _left_ratio = 0.6
                        _bub_text, _bub_dbg = _uis.last_bubble_preview(
                            xml,
                            screen_w=_screen_w,
                            left_ratio=_left_ratio,
                        )
                        result["thread_xml_bubble_dbg"] = _bub_dbg
                        if _bub_text:
                            result["thread_xml_bubble_text"] = (
                                _bub_text[:200]
                            )
                        if _bub_text and _bub_dbg.startswith("self "):
                            result.setdefault("hints", []).append(
                                "thread_xml_bubble_guard:self"
                            )
                            _ago_xbg = time.time() - _last_sent
                            logger.warning(
                                "[messenger_rpa] thread XML bubble guard "
                                "(P1-C): cx_says_self=True "
                                "last_sent_ago=%.0fs (< %.0fs) bubble=%r "
                                "dbg=%s → skip vision",
                                _ago_xbg, _guard_window,
                                _bub_text[:60], _bub_dbg,
                            )
                            return True
                except Exception:
                    logger.debug(
                        "thread_xml_bubble_guard 异常（fallback 到旧路径）",
                        exc_info=True,
                    )

            row = _uis.latest_snippet_row(xml)
            if row is None:
                result.setdefault("hints", []).append("thread_self_xml_guard:no_snippet")
                return False
            result["thread_latest_preview"] = row.preview[:200]
            result["thread_latest_is_self"] = bool(row.is_self_last)
            result["thread_latest_has_self_prefix"] = bool(
                getattr(row, "has_self_prefix", False)
            )
            result["thread_latest_self_media"] = bool(
                getattr(row, "is_self_media_placeholder", False)
            )
            result["thread_latest_bounds"] = row.bounds.as_tuple()
            result["thread_latest_in_thread"] = bool(in_thread)
            if row.is_self_last:
                logger.warning(
                    "[messenger_rpa] latest thread snippet is self; skip reply "
                    "preview=%r bounds=%s",
                    row.preview[:120], row.bounds.as_tuple(),
                )
                return True
            if (
                peer_msg is not None
                and bool(self._cfg.get("thread_self_media_xml_guard", True))
                and bool(getattr(row, "is_self_media_placeholder", False))
            ):
                if _looks_like_self_media_ocr(peer_msg):
                    result.setdefault("hints", []).append("self_media_xml_guard")
                    logger.warning(
                        "[messenger_rpa] latest thread snippet is self media; skip "
                        "Vision OCR reply preview=%r peer_kind=%s peer_text=%r",
                        row.preview[:120], getattr(peer_msg, "kind", ""),
                        peer_msg.to_text_for_ai()[:120],
                    )
                    return True
                result.setdefault("hints", []).append(
                    "self_media_xml_guard_ignored_natural_peer"
                )
        except Exception:
            logger.debug("thread_self_xml_guard failed", exc_info=True)
            result.setdefault("hints", []).append("thread_self_xml_guard:error")
        return False

    def _promote_extra_peer_after_self_overlap(
        self,
        result: Dict[str, Any],
        *,
        last_reply: str,
        chat_key: str = "",
    ) -> Optional[PeerMessage]:
        """Use a nearby extra peer bubble when Vision picked our own reply.

        ``extra_peers`` is ordered near -> far by ``combined_vision``.  When
        the primary ``peer`` overlaps our previous reply, the nearest extra
        non-overlapping item is usually the actual customer message below an
        unread separator.

        P15 hardening (2026-05-04): 之前只对比 last_reply 单条，extra_peers
        里若混入了更老的 self message（在 _recent_replies_per_chat 队列里），
        会被当真 peer 用——bot 继续自言自语。改为对所有 candidate replies
        （DB last_reply + queue 队列）取 max ratio，任何一条命中即拒绝。
        """
        extra = result.get("extra_peers") or []
        if not isinstance(extra, list):
            return None
        # 构建 P15 候选集（与上层 self_overlap 检查口径一致）
        _candidates = list(self._recent_replies_per_chat.get(chat_key) or [])
        if last_reply and last_reply not in _candidates:
            _candidates.append(last_reply)
        for item in extra[:3]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "text").strip().lower()
            if kind not in ("text", "image", "sticker", "voice", "file", "link", "other"):
                kind = "other"
            content = str(item.get("content") or "").strip()
            desc = str(item.get("desc") or "").strip()
            if not content and not desc:
                continue
            if kind == "text":
                _max_ovlp = 0.0
                for _cand in _candidates:
                    _r = _self_reply_overlap_ratio(_cand, content)
                    if _r > _max_ovlp:
                        _max_ovlp = _r
                        if _max_ovlp >= 1.0:
                            break
                if _max_ovlp >= 0.7:
                    continue
            return PeerMessage(
                role="peer",
                kind=kind,
                content=content,
                desc=desc,
                raw=f"promoted_extra_peer:{kind}:{(content or desc)[:300]}",
            )
        return None

    def _stale_peer_after_recent_self_marker(
        self,
        chat_state: Dict[str, Any],
        peer_msg: PeerMessage,
        result: Dict[str, Any],
    ) -> bool:
        """Detect Vision re-reading an old peer bubble after our latest reply."""
        if not bool(self._cfg.get("stale_peer_after_self_guard", True)):
            return False
        if peer_msg.kind != "text":
            return False
        if not (
            bool(result.get("thread_latest_has_self_prefix"))
            or bool(result.get("thread_latest_is_self"))
            or bool(result.get("thread_latest_self_media"))
        ):
            return False
        last_sent_at = float(chat_state.get("last_sent_at") or 0.0)
        if last_sent_at <= 0:
            return False
        window = float(self._cfg.get("stale_peer_after_self_window_sec", 900) or 900)
        if time.time() - last_sent_at > window:
            return False
        last_peer = str(chat_state.get("last_peer_text") or "").strip()
        cur_peer = str(peer_msg.content or "").strip()
        if not last_peer or not cur_peer:
            return False
        overlap = _self_reply_overlap_ratio(last_peer, cur_peer)
        result["last_peer_repeat_overlap"] = round(overlap, 3)
        threshold = float(
            self._cfg.get("stale_peer_after_self_overlap_threshold", 0.45)
            or 0.45
        )
        return overlap >= threshold

    async def _run_once_scroll_rescan_if_no_unread(
        self,
        serial: str,
        wh: Tuple[int, int],
        run_id: str,
        result: Dict[str, Any],
        guard: Any,
        unread: List[UnreadChat],
    ) -> Tuple[Any, List[UnreadChat]]:
        """首屏 Vision 报 0 未读时，上滑 Chats 再截图识别（找列表下方未读）。"""
        if unread:
            return guard, unread
        attempts = int(
            self._cfg.get("run_once_inbox_scroll_if_zero_unread_attempts") or 0,
        )
        if attempts <= 0:
            return guard, unread
        if getattr(guard, "needs_human", False):
            return guard, unread
        if getattr(guard, "type", "none") != "none":
            return guard, unread
        w, h = wh
        y1r = float(self._cfg.get("send_to_chat_scroll_y1_ratio") or 0.66)
        y2r = float(self._cfg.get("send_to_chat_scroll_y2_ratio") or 0.44)
        swipe_ms = int(self._cfg.get("send_to_chat_scroll_duration_ms") or 380)
        swipe_ms = max(120, min(900, swipe_ms))
        last_guard = guard
        for si in range(attempts):
            adb.input_swipe(
                serial,
                w // 2,
                int(h * y1r),
                w // 2,
                int(h * y2r),
                swipe_ms,
            )
            await asyncio.sleep(0.55)
            sp = await self._screenshot(
                serial, f"run_once_inbox_zu_{si}", run_id,
            )
            if not sp:
                result.setdefault("hints", []).append(
                    f"run_once:zero_unread_scroll_png_fail_{si}",
                )
                continue
            try:
                png_blob = Path(sp).read_bytes()
            except OSError:
                result.setdefault("hints", []).append(
                    f"run_once:zero_unread_scroll_png_read_err_{si}",
                )
                continue
            if not _messenger_png_screencap_ok(png_blob):
                result.setdefault("hints", []).append(
                    f"run_once:zero_unread_scroll_bad_png_skip_{si}",
                )
                continue
            result["screenshot_path"] = sp
            if self._use_combined_vision:
                g2, ur2 = await self._inbox_combined(sp, result, retry=False)
            else:
                g2 = guard
                ur2 = await self._scan_inbox(sp, result)
            last_guard = g2
            result.setdefault("hints", []).append(
                f"run_once:zero_unread_scroll_rescan_{si}",
            )
            if getattr(g2, "needs_human", False):
                break
            if getattr(g2, "type", "none") != "none":
                break
            if ur2:
                return g2, ur2
        return last_guard, unread

    def _apply_inbox_combined_side_effects(
        self,
        cr: Any,
        tag: str,
        result: Dict[str, Any],
        retry: bool,
        *,
        replay_risk: bool,
    ) -> None:
        """P25：抽出 _inbox_combined 对 result 的写入，便于 cache 命中时重放。"""
        result["inbox_vision_tag"] = tag + ("|retry" if retry else "")
        result["inbox_unread_count"] = len(cr.rows)
        result["inbox_ranking"] = [
            {
                "name": r.name,
                "preview": r.preview[:60],
                "hint": r.quality_hint,
                "score": round(r.score, 2),
                "row_index": r.row_index,
            }
            for r in cr.rows[:8]
        ]
        if cr.guard.type != "none":
            result.setdefault("guard_history", []).append(
                {
                    "where": "inbox(combined)",
                    "type": cr.guard.type,
                    "action": cr.guard.action,
                    "confidence": cr.guard.confidence,
                    "title": cr.guard.title,
                }
            )
        if replay_risk:
            risk = getattr(cr, "risk", None)
            if risk is not None and risk.hit:
                self._handle_risk_hit(risk, result=result, where="inbox")

    def _inbox_text_hash(self) -> Optional[str]:
        """P25-v2 (2026-05-05) 文本级 hash：用 InboxRow.preview 拼字符串作 cache key。

        理由：原 P25 ROI 像素 hash 命中率仅 4%（10:30 metrics 数据：12 hit /
        ~300 calls），因为时间戳"5m ago"分钟级变化让像素永不一致。改用从
        uiautomator dump 提取的 row preview 文本——peer 没发新消息时 row
        preview 字面稳定（时间戳不在 preview 字段里）。

        失败（dump timeout / lowmemkill）→ 返 None，caller 用 ROI hash fallback。
        """
        serial = (self._cfg.get("adb_serial") or "").strip()
        if not serial:
            return None
        _timeout = float(self._cfg.get(
            "inbox_text_hash_dump_timeout_s", 2.0,
        ) or 0.0)
        if _timeout <= 0:
            return None
        try:
            from src.integrations.messenger_rpa.ui_inbox_scraper import (
                dump_inbox_rows,
            )
            rows = dump_inbox_rows(
                serial,
                adb_user_id=self._adb_user_id,
                timeout_s=_timeout,
            )
        except Exception:
            return None
        if not rows:
            return None
        # 拼前 8 行的 preview 字段（截断 60 字防异常长 preview 把 hash 打散）
        text = "|".join(
            (r.preview or "")[:60] for r in rows[:8]
        )
        if not text:
            return None
        return "inbox_text:" + hashlib.md5(
            text.encode("utf-8")
        ).hexdigest()

    async def _inbox_combined(
        self,
        inbox_png: str,
        result: Dict[str, Any],
        retry: bool = False,
        *,
        max_rows: Optional[int] = None,
    ) -> Tuple[Any, List[UnreadChat]]:
        """单次 vision 同时拿 inbox guard + 未读列表。

        P25 (2026-05-04)：加 ROI hash 缓存。peer 没发新消息时同分钟内 inbox
        列表稳定（时间戳"5m"粗粒度），ROI hash 命中可省 ~50s vision API。
        风险约束：仅 risk 未命中且 retry=False 时缓存（retry 路径用单任务
        prompt 不该混入 cache）。

        P25-v2 (2026-05-05)：双层 cache key。优先 text hash（uiautomator
        dump 的 row preview 拼字符串），健康设备命中率应 > 30%。dump 失败
        fallback 到 ROI hash（保持 v1 行为）。
        """
        # ── P25-v2 双层 cache key ──
        # 优先 text hash（更稳定，避开像素时间戳变化）
        text_hash = self._inbox_text_hash() if not retry else None
        # ROI hash 作为 fallback（dump 失败时仍可用）
        roi_hash = (
            self._screenshot_inbox_hash(inbox_png) if inbox_png else None
        )

        # P25-v2-fix (2026-05-05) 双键查找：
        # 原 v2 写入只用 text key（dump 成功时），下次 dump 失败用 ROI 查找
        # 找不到 → 命中率低。修复：text 和 ROI 都试。
        _hit_hash = None
        _hit_kind = None
        if not retry:
            for _try_hash, _kind in (
                (text_hash, "text"), (roi_hash, "roi"),
            ):
                if _try_hash and _try_hash in self._inbox_combined_cache:
                    _hit_hash = _try_hash
                    _hit_kind = _kind
                    break

        if _hit_hash:
            cr, tag = self._inbox_combined_cache[_hit_hash]
            self._inbox_combined_cache.move_to_end(_hit_hash)
            result.setdefault("hints", []).append("inbox_combined_cache_hit")
            result["inbox_cache_hash_kind"] = _hit_kind
            result.setdefault("hints", []).append(
                f"inbox_combined_cache_hit_kind:{_hit_kind}"
            )
            result.setdefault("phase_ms", {})["inbox_vision"] = 0
            self._apply_inbox_combined_side_effects(
                cr, tag, result, retry, replay_risk=False,
            )
            cap = (
                self._max_inbox_per_run
                if max_rows is None else int(max_rows)
            )
            cap = max(1, min(30, cap))
            return cr.guard, cr.rows[:cap]

        _t_iv = time.monotonic()
        cr, tag = await analyze_inbox_combined(
            inbox_png,
            vision_cfg=self._vision_cfg(),
            global_vision=self._global_vision_cfg(),
            skip_spam=bool(self._cfg.get("skip_spam", True)),
        )
        result.setdefault("phase_ms", {})["inbox_vision"] = int(
            (time.monotonic() - _t_iv) * 1000
        )
        # P25：仅 risk 未命中 + 非 retry 时缓存
        risk = getattr(cr, "risk", None)
        risk_hit = bool(risk and getattr(risk, "hit", False))
        if not retry and not risk_hit:
            # P25-v2-fix：text 和 ROI 两个 key 都写（指向同一结果），让下次
            # 任意一种 hash 查找都能命中
            for _wkey in (text_hash, roi_hash):
                if _wkey and _wkey not in self._inbox_combined_cache:
                    self._inbox_combined_cache[_wkey] = (cr, tag)
            while len(
                self._inbox_combined_cache
            ) > self._inbox_combined_cache_max:
                self._inbox_combined_cache.popitem(last=False)
        # P25：side effects 复用 helper（含 risk 实时处理）
        self._apply_inbox_combined_side_effects(
            cr, tag, result, retry, replay_risk=True,
        )

        # ★ combined 漏报时用单任务 prompt 再兜底一次
        # 两种触发情形：
        #   (a) combined 返 0 条（原逻辑）
        #   (b) combined 返了几条但**没有** row_index=0——Vision 常把 Stories 下方的
        #       第一条会话误判为 Stories 延伸，系统性漏掉 row 0；fallback 单任务精度更高
        missing_top_row = bool(cr.rows) and not any(
            r.row_index == 0 for r in cr.rows
        )
        # 优化 D：row_index 分布 log → 便于 prompt 调优 / 诊断系统性漏行模式
        # （combined prompt 已对 row_index=0 反复强调；若仍漏，说明 vision 模型在
        # 当前设备/账号情境下系统性误判 Stories→row0 的边界）
        _row_indices = sorted(r.row_index for r in cr.rows)
        logger.warning(
            "[messenger_rpa] _inbox_combined 决策: cr.rows=%d, "
            "row_indices=%s, missing_top=%s, guard=%s, retry=%s, "
            "fallback_cfg=%s",
            len(cr.rows),
            _row_indices,
            missing_top_row, cr.guard.type, retry,
            bool(self._cfg.get("unread_fallback_prompt", True)),
        )
        if (
            (not cr.rows or missing_top_row)
            and cr.guard.type == "none"
            and not retry
            and bool(self._cfg.get("unread_fallback_prompt", True))
        ):
            logger.warning(
                "[messenger_rpa] 触发 fallback analyze_unread_only "
                "(cr.rows=%d, missing_top=%s, cr.guard=%s)",
                len(cr.rows), missing_top_row, cr.guard.type,
            )
            fb_rows, fb_tag = await analyze_unread_only(
                inbox_png,
                vision_cfg=self._vision_cfg(),
                global_vision=self._global_vision_cfg(),
                skip_spam=bool(self._cfg.get("skip_spam", True)),
            )
            result["unread_fallback_tag"] = fb_tag
            logger.warning(
                "[messenger_rpa] fallback 返回 %d 条 tag=%s: names=%s",
                len(fb_rows), fb_tag, [r.name for r in fb_rows[:6]],
            )
            if fb_rows:
                if not cr.rows:
                    # 情形 (a)：combined 空，用 fallback 整体替换
                    merged = fb_rows
                    logger.info(
                        "[messenger_rpa] combined 漏报 unread，fallback 补回 %d 条: %s",
                        len(merged), [r.name for r in merged],
                    )
                else:
                    # 情形 (b)：combined 有条目但缺 row 0；看 fallback 有没有补上
                    fb_row0 = [r for r in fb_rows if r.row_index == 0]
                    if fb_row0:
                        # 把 fallback 的 row 0 放到 combined 头部（score 也靠前）
                        merged = fb_row0 + cr.rows
                        logger.info(
                            "[messenger_rpa] combined 漏报 row=0，fallback 补齐: "
                            "name=%r，原列表 %d 条，合并后 %d 条",
                            fb_row0[0].name, len(cr.rows), len(merged),
                        )
                    else:
                        # fallback 也没捞到 row 0，维持原样
                        merged = cr.rows
                        logger.info(
                            "[messenger_rpa] row=0 仍缺失（fallback tag=%s）", fb_tag,
                        )
                result["inbox_unread_count"] = len(merged)
                result["inbox_ranking"] = [
                    {
                        "name": r.name,
                        "preview": r.preview[:60],
                        "hint": r.quality_hint,
                        "score": round(r.score, 2),
                        "row_index": r.row_index,
                        "src": "unread_only_fallback" if r in fb_rows else "combined",
                    }
                    for r in merged[:8]
                ]
                cap = self._max_inbox_per_run if max_rows is None else int(max_rows)
                cap = max(1, min(30, cap))
                return cr.guard, merged[:cap]
        cap = self._max_inbox_per_run if max_rows is None else int(max_rows)
        cap = max(1, min(30, cap))
        return cr.guard, cr.rows[:cap]

    @staticmethod
    def _screenshot_hash(path: str) -> Optional[str]:
        """对消息气泡 ROI（去掉顶栏 Active 时间 + 输入栏 typing 闪烁）算 MD5。

        P17-v2 (2026-05-04)：v1 用全图 PNG bytes，1.5h 生产 0% 命中——顶栏
        "Active 5m ago" / 输入栏游标闪烁 / 状态栏每秒变化都让全图 hash 永远
        不同。改 ROI = 屏高 [3.75%, 83%]，720x1600 屏对应 [60, 1328]，正好
        卡掉这两个变化区域，保留消息气泡区。

        PIL 不可用时 fallback 到全图 hash（保留 v1 行为）。
        前缀区分两种 hash 用于调试：roi: / full:
        """
        return MessengerRpaRunner._screenshot_roi_hash(
            path, top_pct=0.0375, bottom_pct=0.83, prefix="roi",
        )

    @staticmethod
    def _screenshot_inbox_hash(path: str) -> Optional[str]:
        """P25：inbox 截图 ROI hash。

        ROI = 屏高 [23.75%, 92.5%]，跳过：
        - [0, 23.75%]：状态栏 + Messenger header + 搜索框 + Stories 动画头像
        - [92.5%, 100%]：底部 nav 栏

        保留：chat 列表区。peer 没发新消息时同分钟内列表稳定（时间戳是
        "5m ago" 粒度，5 分钟才滚一次）。
        """
        return MessengerRpaRunner._screenshot_roi_hash(
            path, top_pct=0.2375, bottom_pct=0.925, prefix="inbox_roi",
        )

    @staticmethod
    def _screenshot_roi_hash(
        path: str,
        *,
        top_pct: float,
        bottom_pct: float,
        prefix: str,
    ) -> Optional[str]:
        """通用 ROI hash 实现。PIL 失败时 fallback 全图。"""
        try:
            from PIL import Image
            with Image.open(path) as img:
                w, h = img.size
                top = int(h * top_pct)
                bottom = int(h * bottom_pct)
                if top < bottom:
                    roi = img.crop((0, top, w, bottom))
                    return f"{prefix}:" + hashlib.md5(
                        roi.tobytes()
                    ).hexdigest()
        except Exception:
            pass
        try:
            with open(path, "rb") as fh:
                return "full:" + hashlib.md5(fh.read()).hexdigest()
        except (OSError, TypeError):
            return None

    def _thread_combined_apply_side_effects(
        self, cr: Any, tag: Any, result: Dict[str, Any], *, replay_risk: bool,
    ) -> None:
        """把 _thread_combined 对 result 的写入抽出，供 cache 命中时重放。
        replay_risk=False 时跳过 risk hit 副作用（避免重复触发 webhook 等）。
        """
        result["thread_vision_tag"] = tag
        if cr.guard.type != "none":
            result.setdefault("guard_history", []).append(
                {
                    "where": "thread(combined)",
                    "type": cr.guard.type,
                    "action": cr.guard.action,
                    "confidence": cr.guard.confidence,
                    "title": cr.guard.title,
                }
            )
        # ★ P2-2：把 extra_peers 存到 result，供下游 _generate_reply 消费
        try:
            ep = getattr(cr, "extra_peers", ()) or ()
            if ep:
                result["extra_peers"] = [
                    {
                        "kind": pm.kind,
                        "content": pm.content,
                        "desc": pm.desc,
                    }
                    for pm in ep
                ]
        except Exception:
            pass
        # ★ P3-1：thread 也扫描 risk
        if replay_risk:
            risk = getattr(cr, "risk", None)
            if risk is not None and risk.hit:
                self._handle_risk_hit(risk, result=result, where="thread")

    async def _thread_combined(
        self, thread_png: str, result: Dict[str, Any]
    ) -> Tuple[Any, Optional[PeerMessage]]:
        # ── P17-v2 截屏 ROI hash 缓存（chat_key 隔离）──
        # cache key = "{chat_key}|{roi_hash}"，避免不同 chat 但 ROI 像素巧合
        # 相同时误命中。
        img_hash = self._screenshot_hash(thread_png) if thread_png else None
        chat_key_hint = str(result.get("chat_key") or "")
        cache_key = f"{chat_key_hint}|{img_hash}" if img_hash else None
        if cache_key and cache_key in self._thread_combined_cache:
            cr, tag = self._thread_combined_cache[cache_key]
            # LRU touch
            self._thread_combined_cache.move_to_end(cache_key)
            result.setdefault("hints", []).append("thread_combined_cache_hit")
            # 命中时不重放 risk hit 副作用（首次已处理过）
            self._thread_combined_apply_side_effects(
                cr, tag, result, replay_risk=False,
            )
            return cr.guard, cr.peer

        cr, tag = await analyze_thread_combined(
            thread_png,
            vision_cfg=self._vision_cfg(),
            global_vision=self._global_vision_cfg(),
        )
        # 仅在 risk 未命中时缓存（risk 链路保持每次实时判定）
        risk = getattr(cr, "risk", None)
        risk_hit = bool(risk and getattr(risk, "hit", False))
        if cache_key and not risk_hit:
            self._thread_combined_cache[cache_key] = (cr, tag)
            while len(self._thread_combined_cache) > self._thread_combined_cache_max:
                self._thread_combined_cache.popitem(last=False)
        self._thread_combined_apply_side_effects(
            cr, tag, result, replay_risk=True,
        )
        return cr.guard, cr.peer

    def _apply_guard_action(self, serial: str, guard: Any) -> bool:
        """combined 模式下复用 _handle_guard 的白名单 + 动作执行。"""
        trusted_types = {
            "note_reactions",
            "previews_on",
            "send_first_like",
            "permission_dialog",
        }
        if guard.type not in trusted_types:
            return False
        wh = self._screen_size(serial)
        if guard.action == ACTION_TAP_OK:
            x, y = cc.MODAL_OK_BTN.at(*wh)
            adb.input_tap(serial, x, y)
            return True
        if guard.action == ACTION_TAP_CLOSE_X:
            x, y = cc.MODAL_CLOSE_X.at(*wh)
            adb.input_tap(serial, x, y)
            return True
        if guard.action == ACTION_PRESS_BACK:
            adb.input_keyevent(serial, "KEYCODE_BACK")
            return True
        return False

    # ── 内部：Inbox / Thread (legacy 分离调用)──────
    async def _scan_inbox(
        self,
        inbox_png: str,
        result: Dict[str, Any],
        *,
        max_rows: Optional[int] = None,
    ) -> List[UnreadChat]:
        rows, tag = await scan_inbox_vision(
            inbox_png,
            vision_cfg=self._vision_cfg(),
            global_vision=self._global_vision_cfg(),
            skip_spam=bool(self._cfg.get("skip_spam", True)),
        )
        result["inbox_vision_tag"] = tag
        result["inbox_unread_count"] = len(rows)
        # 完整 ranking（debug 用），不入库防止隐私
        result["inbox_ranking"] = [
            {
                "name": r.name,
                "preview": r.preview[:60],
                "hint": r.quality_hint,
                "score": round(r.score, 2),
                "row_index": r.row_index,
                "y_pct": round(r.y_percent, 1),
            }
            for r in rows[:8]
        ]
        cap = self._max_inbox_per_run if max_rows is None else int(max_rows)
        cap = max(1, min(30, cap))
        return rows[:cap]

    def _tap_chat_row(
        self, serial: str, wh: Tuple[int, int], chat: UnreadChat
    ) -> Tuple[int, int, str]:
        """点击会话列表的第 row_index 行；返回 (x, y, source)。

        source 取值如 ``ui_xml/preview_match(preview)`` / ``calibrated_stories_aware``
        / ``scaled_stories_aware``——纯诊断/缓存标签，未来追溯时知道这条 tap
        是哪条路径决定的。其他 caller 可忽略返回值。

        优先级（2026-04 新）：
          0) **uiautomator XML 解析**——从真实 UI 树拿 bounds，对 Stories 高度变化免疫
          1) 本机持久化校准
          2) 等比缩放公式（最后兜底）
        """
        width, height = wh

        # ★ P7-UI：先试 uiautomator，拿到真实 bounds 直接点
        if bool(self._cfg.get("use_ui_hierarchy_tap", True)):
            try:
                from src.integrations.messenger_rpa.ui_inbox_scraper import (
                    dump_inbox_rows, find_row_by_preview,
                )
                ui_rows = dump_inbox_rows(
                    serial,
                    adb_user_id=self._adb_user_id,
                    timeout_s=float(self._cfg.get("ui_dump_timeout_s") or 6.0),
                )
                target_ui = None
                match_src = "none"
                # 1. 最可靠：preview 前缀匹配（Vision 和 UI XML 都用 preview）
                for probe_name, probe in (("preview", chat.preview),
                                            ("name", chat.name)):
                    target_ui = find_row_by_preview(ui_rows, probe)
                    if target_ui is not None:
                        match_src = f"preview_match({probe_name})"
                        break
                # 2. row_index 直接映射：UI XML rows 按 y_top 升序排列
                #    Vision row_index=N → ui_rows[N]（两者均 0-indexed，不含 Stories）
                if target_ui is None and 0 <= chat.row_index < len(ui_rows):
                    target_ui = ui_rows[chat.row_index]
                    match_src = f"row_index_direct({chat.row_index})"
                # 3. 邻近匹配：用校准 Y 找最近的 XML 行（XML 缺行时 index 不对齐）
                if target_ui is None and ui_rows:
                    _cal = self._get_calibration(serial, width, height)
                    if _cal:
                        _exp_y = _cal.chat_row_first_y + chat.row_index * _cal.chat_row_height
                        _best = min(ui_rows, key=lambda r: abs(r.y_center - _exp_y))
                        if abs(_best.y_center - _exp_y) < _cal.chat_row_height * 0.7:
                            target_ui = _best
                            match_src = f"proximity(exp={_exp_y},act={_best.y_center})"
                # 4. ★ 动态校准更新：XML 有 ≥3 行时实时修正缓存
                if len(ui_rows) >= 3:
                    _ys = [r.y_center for r in ui_rows]
                    _diffs = [_ys[i+1] - _ys[i] for i in range(len(_ys) - 1)]
                    _diffs.sort()
                    _dyn_h = _diffs[len(_diffs) // 2]
                    _dyn_first = _ys[0]
                    if 80 < _dyn_h < 220 and 350 < _dyn_first < 900:
                        _ck = (serial, width, height)
                        _old = self._calib_cache.get(_ck)
                        if _old is None or abs(getattr(_old, 'chat_row_first_y', 0) - _dyn_first) > 15:
                            try:
                                from src.integrations.messenger_rpa.coord_calibrator import (
                                    InboxAnchors, save_calibration, calibrated_for,
                                )
                                _anch = InboxAnchors(
                                    width=width, height=height,
                                    chat_row_first_y=_dyn_first,
                                    chat_row_height=_dyn_h,
                                    notes=f"dynamic_xml:rows={len(ui_rows)}",
                                )
                                _ws = Path(self._cm.config_path).parent
                                save_calibration(_ws, serial, _anch)
                                self._calib_cache[_ck] = calibrated_for(
                                    serial, width, height, _anch,
                                )
                                logger.warning(
                                    "[messenger_rpa] 动态校准更新 first_y=%d row_h=%d (xml_rows=%d)",
                                    _dyn_first, _dyn_h, len(ui_rows),
                                )
                            except Exception:
                                logger.debug("动态校准保存失败", exc_info=True)
                if target_ui is not None:
                    # ★ inbox self-sent guard: 如果 XML 行的预览是自己发的，
                    # 不要进入这个会话（防止 TTS 语音发送后无限循环 / vision
                    # OCR 漏 "You:" 前缀引发的自我对话死循环 — 见 P0-A）。
                    # 决策逻辑抽到 _decide_inbox_self_sent_skip 静态方法
                    # （便于独立单元测试），本处只读 chat_state + 配置 + 调用 +
                    # 写日志 + 路由。
                    if getattr(target_ui, "is_self_last", False):
                        vision_preview = (chat.preview or "").strip()
                        try:
                            _ck_ssl = f"{self._chat_key_prefix}:{chat.name}"
                            _cs_ssl = self._state.get_chat_state(_ck_ssl)
                        except Exception:
                            _cs_ssl = {}
                        try:
                            _last_sent_ssl = float(
                                _cs_ssl.get("last_sent_at") or 0
                            )
                        except (TypeError, ValueError):
                            _last_sent_ssl = 0.0
                        _last_reply_ssl = str(
                            _cs_ssl.get("last_reply") or ""
                        ).strip()
                        try:
                            _hard_window = float(
                                self._cfg.get(
                                    "inbox_self_sent_hard_skip_sec", 60
                                ) or 0
                            )
                        except (TypeError, ValueError):
                            _hard_window = 60.0
                        try:
                            _ovl_thr = float(
                                self._cfg.get(
                                    "inbox_self_sent_overlap_threshold", 0.5
                                ) or 0.5
                            )
                        except (TypeError, ValueError):
                            _ovl_thr = 0.5

                        _should_skip, _ssl_reason = (
                            self._decide_inbox_self_sent_skip(
                                vision_preview=vision_preview,
                                last_sent_at=_last_sent_ssl,
                                last_reply=_last_reply_ssl,
                                hard_window_sec=_hard_window,
                                overlap_threshold=_ovl_thr,
                            )
                        )
                        if _should_skip:
                            _ago = (
                                f"{(time.time() - _last_sent_ssl):.0f}s"
                                if _last_sent_ssl > 0 else "n/a"
                            )
                            logger.warning(
                                "[messenger_rpa] inbox skip self-sent "
                                "reason=%s name=%r ui_preview=%r "
                                "vision_preview=%r last_sent_ago=%s",
                                _ssl_reason, chat.name,
                                target_ui.preview[:60],
                                vision_preview[:60], _ago,
                            )
                            return None, None, "inbox_self_sent_skip"
                        logger.warning(
                            "[messenger_rpa] inbox self-sent guard 被 Vision "
                            "覆盖 (reason=%s): name=%r ui_preview=%r "
                            "vision_preview=%r → 继续 tap（XML content-desc 陈旧）",
                            _ssl_reason, chat.name,
                            target_ui.preview[:40], vision_preview[:40],
                        )
                    x, y = target_ui.x_center, target_ui.y_center
                    logger.warning(
                        "[messenger_rpa] tap chat: name=%r row_index=%d "
                        "src=ui_xml/%s -> (%d, %d) ui_preview=%r "
                        "vision_preview=%r",
                        chat.name, chat.row_index, match_src, x, y,
                        target_ui.preview[:40], (chat.preview or "")[:40],
                    )
                    adb.input_tap(serial, x, y)
                    return x, y, f"ui_xml/{match_src}"
                logger.warning(
                    "[messenger_rpa] ui_xml 没匹配到 target（ui_rows=%d, "
                    "chat.row_index=%d, name=%r, preview=%r），退化到公式坐标",
                    len(ui_rows), chat.row_index, chat.name,
                    (chat.preview or "")[:40],
                )
            except Exception:
                logger.debug(
                    "[messenger_rpa] ui_inbox_scraper 异常，回退公式",
                    exc_info=True,
                )

        # ★ Vision row_index 从第一个 chat 行开始（0-indexed），不含 Stories。
        # 之前的 -1 假设 Vision 把 Stories 算作 row 0，但实际不是——
        # 多次日志确认第一个 chat = row_index=0。去掉偏移。
        adjusted_row = chat.row_index
        # ★ 优先用本机校准
        cal = self._get_calibration(serial, width, height)
        if cal is not None:
            x, y = cc.chat_row_for(
                adjusted_row,
                width=width, height=height,
                chat_row_first_y=cal.chat_row_first_y,
                chat_row_height=cal.chat_row_height,
                chat_row_text_x=cal.chat_row_text_x,
            )
            src = "calibrated_stories_aware"
        else:
            x = int(round(cc.CHAT_ROW_TEXT_X * width / cc.BASE_WIDTH))
            # ★ stories-aware：用 adjusted_row 替代 chat.row_index 直接调
            # click_y 内部公式（避免 stories 偏移）
            scale = float(height) / float(cc.BASE_HEIGHT)
            y_base = cc.CHAT_ROW_FIRST_Y + adjusted_row * cc.CHAT_ROW_HEIGHT
            y = int(round(y_base * scale))
            src = "scaled_stories_aware"
        logger.warning(
            "[messenger_rpa] tap chat: name=%r row_index=%d adjusted=%d "
            "src=%s -> (%d, %d)",
            chat.name, chat.row_index, adjusted_row, src, x, y,
        )
        adb.input_tap(serial, x, y)
        return x, y, src

    # ── 校准 ───────────────────────────────────────
    def _get_calibration(
        self, serial: str, width: int, height: int
    ):
        if not bool(self._cfg.get("auto_calibrate", True)):
            return None
        cache_key = (serial, width, height)
        if cache_key in self._calib_cache:
            return self._calib_cache[cache_key]
        try:
            from src.integrations.messenger_rpa.coord_calibrator import (
                load_calibration, calibrated_for,
            )
            workspace = Path(self._cm.config_path).parent
            anchors = load_calibration(workspace, serial)
            if anchors is None:
                self._calib_cache[cache_key] = None
                return None
            cal = calibrated_for(serial, width, height, anchors)
            self._calib_cache[cache_key] = cal
            logger.info(
                "[messenger_rpa] 已加载校准 serial=%s wh=%dx%d "
                "row_first_y=%d row_h=%d tab_y=%d",
                serial, width, height,
                cal.chat_row_first_y, cal.chat_row_height, cal.tab_chats_y,
            )
            return cal
        except Exception:
            logger.exception("[messenger_rpa] 加载校准失败 serial=%s", serial)
            self._calib_cache[cache_key] = None
            return None

    def _maybe_auto_calibrate(
        self,
        serial: str,
        wh: Tuple[int, int],
        inbox_png: str,
        result: Dict[str, Any],
    ) -> None:
        """首次遇到此 serial 时，用像素级标定并落盘。后续 _get_calibration 直接读。

        全程不发 vision 调用，纯 PIL 像素扫描，<200ms。
        """
        if not bool(self._cfg.get("auto_calibrate", True)):
            return
        try:
            from src.integrations.messenger_rpa.coord_calibrator import (
                load_calibration, save_calibration, InboxAnchors,
            )
            from src.integrations.messenger_rpa.auto_calibrate import (
                calibrate_inbox_rows,
            )
            workspace = Path(self._cm.config_path).parent
            if load_calibration(workspace, serial) is not None:
                return
            calib = calibrate_inbox_rows(inbox_png)
            if not calib.ok:
                logger.info(
                    "[messenger_rpa] auto_calibrate 跳过（像素扫描失败 reason=%s）",
                    calib.reason,
                )
                return
            ry = float(wh[1]) / 1600.0
            anchors = InboxAnchors(
                width=wh[0],
                height=wh[1],
                chat_row_first_y=int(round(calib.first_y * ry)),
                chat_row_height=int(round(calib.row_height * ry)),
                notes=f"pixel_auto:rows={calib.visible_rows}",
            )
            save_calibration(workspace, serial, anchors)
            self._calib_cache.pop((serial, wh[0], wh[1]), None)
            result.setdefault("hints", []).append(
                f"auto_calibrated:first_y={anchors.chat_row_first_y} "
                f"h={anchors.chat_row_height}"
            )
            logger.info(
                "[messenger_rpa] 自动标定已保存 serial=%s first_y=%d h=%d",
                serial, anchors.chat_row_first_y, anchors.chat_row_height,
            )
        except Exception:
            logger.debug("auto_calibrate 失败（非致命）", exc_info=True)

    async def calibrate_now(self, *, force: bool = False) -> Dict[str, Any]:
        """对当前 adb_serial 做一次 inbox 校准并保存。

        force=True 时即使校准文件已存在也覆盖。
        """
        from src.integrations.messenger_rpa.coord_calibrator import (
            detect_anchors, save_calibration, load_calibration,
        )
        result: Dict[str, Any] = {"ok": False, "step": "init"}
        result_serial: Dict[str, Any] = {}
        serial = self._resolve_serial(result_serial)
        if not serial:
            result["error"] = "no serial"
            result.update(result_serial)
            return result

        workspace = Path(self._cm.config_path).parent
        if not force and load_calibration(workspace, serial) is not None:
            result["ok"] = True
            result["step"] = "already_calibrated"
            return result

        wh = self._screen_size(serial)
        if not self._foreground_messenger(serial, result):
            return result

        run_id = f"calibrate_{int(time.time())}"
        png_path = await self._screenshot(serial, "calibrate", run_id)
        if not png_path:
            result["step"] = "screenshot_failed"
            return result

        from src.vision_client import VisionClient
        v_cfg = dict(self._vision_cfg() or {})
        client = VisionClient(config=v_cfg)
        if not client.initialize():
            result["error"] = "vision client init failed"
            return result

        try:
            anchors = await detect_anchors(
                vision_client=client,
                screenshot_png_path=png_path,
                width=wh[0],
                height=wh[1],
            )
        except Exception as ex:
            result["error"] = f"detect_anchors: {type(ex).__name__}:{ex}"
            return result

        # ★ chat_row_first_y + chat_row_height 是核心，tab_bar_y 可选
        if (
            anchors.chat_row_first_y is None
            or anchors.chat_row_height is None
        ):
            result["step"] = "anchors_incomplete"
            result["anchors"] = anchors.__dict__
            return result

        save_calibration(workspace, serial, anchors)
        # 清缓存让下次 _get_calibration 重新读
        self._calib_cache.pop((serial, wh[0], wh[1]), None)
        result["ok"] = True
        result["step"] = "saved"
        result["anchors"] = anchors.__dict__
        return result

    async def _read_peer(
        self, thread_png: str, result: Dict[str, Any]
    ) -> Tuple[Optional[PeerMessage], str]:
        msg, tag = await read_peer_message_vision(
            thread_png,
            vision_cfg=self._vision_cfg(),
            global_vision=self._global_vision_cfg(),
        )
        result["thread_vision_tag"] = tag
        return msg, tag

    def _exit_thread(self, serial: str) -> None:
        """会话结束后按 BACK 回到 Inbox，避免下次还停在同一会话。

        副作用：失效该 serial 的 recent_verify_cache 全部条目——thread 一退，
        旧的 verified 记录就过期了，下次 verify 必须真测。同样失效
        pre_foreground title cache：foreground 已变，原 title 不再有效。
        """
        try:
            adb.input_keyevent(serial, "KEYCODE_BACK")
        except Exception:
            logger.debug("exit_thread BACK 失败", exc_info=True)
        try:
            from src.integrations.messenger_rpa import recent_verify_cache as _rvc
            _rvc.invalidate(serial)
        except Exception:
            logger.debug("recent_verify_cache invalidate 失败", exc_info=True)
        # 退出 thread 后 foreground 不再是原 chat，cache 必须清掉
        self._foreground_title_cache.pop(serial, None)

    # ── 内部：P3-1 风控处理 ────────────────────────
    def _handle_risk_hit(
        self,
        risk: Any,
        *,
        result: Dict[str, Any],
        where: str,
    ) -> None:
        """vision 报告 risk.hit=True 时调用。

        策略：
          - 记 state_store（累计 hit_count），连续 N 次同级命中才升级 status
          - just_blocked=True → 主动 pause 24h（service 级），推 TG 红色告警
          - just_warned=True → 推黄色告警（不 pause）
          - 每种状态只推一次（record_risk_hit 自身 dedup 过 hit_count）
          - 把 risk 写入 result 便于 Web 显示
        """
        try:
            cfg = (self._cfg.get("risk") or {})
            if not bool(cfg.get("enabled", True)):
                return
            require = int(cfg.get("require_consecutive", 2) or 2)
            block_hours = int(cfg.get("block_duration_hours", 24) or 24)
            rec = self._state.record_risk_hit(
                severity=risk.severity,
                reason=risk.reason,
                block_duration_sec=block_hours * 3600,
                require_consecutive=require,
            )
            result["risk"] = {
                "hit": True,
                "severity": risk.severity,
                "reason": risk.reason,
                "where": where,
                "hit_count": rec.get("hit_count"),
                "status": rec.get("status"),
            }
            logger.warning(
                "[messenger_rpa] P3-1 risk recorded where=%s sev=%s hit_count=%s status=%s",
                where, risk.severity, rec.get("hit_count"), rec.get("status"),
            )
            # 确认升级才推告警 / pause
            if rec.get("just_blocked"):
                self._notify_risk(rec, risk, blocked=True)
            elif rec.get("just_warned"):
                self._notify_risk(rec, risk, blocked=False)
        except Exception:
            logger.debug("_handle_risk_hit 异常", exc_info=True)

    def _notify_risk(self, rec: Dict[str, Any], risk: Any, *, blocked: bool) -> None:
        """推送风控告警到 TG + webhook。"""
        # webhook
        try:
            notifier = getattr(self, "_webhook_notifier", None)
            if notifier is not None:
                notifier.notify(
                    event="messenger_rpa.risk",
                    payload={
                        "severity": risk.severity,
                        "reason": risk.reason,
                        "hit_count": rec.get("hit_count"),
                        "blocked": blocked,
                        "blocked_until_ts": rec.get("blocked_until_ts"),
                        "ts": time.time(),
                    },
                )
        except Exception:
            logger.debug("risk webhook 失败", exc_info=True)
        # TG
        tg = self._telegram_client
        if tg is None or not hasattr(tg, "client"):
            return
        target = str(
            (self._cfg.get("escalation") or {}).get("telegram_chat_id")
            or ((self._cm.config or {}).get("telegram", {}) or {}).get("admin_chat_id")
            or ""
        ).strip()
        if not target:
            return
        title = "🚨 账号被封禁" if blocked else "⚠️ 账号风控警告"
        duration_info = ""
        if blocked and rec.get("blocked_until_ts"):
            import datetime as _dt
            exp = _dt.datetime.fromtimestamp(
                float(rec.get("blocked_until_ts") or 0)
            ).strftime("%Y-%m-%d %H:%M")
            duration_info = f"\n⏸ pause 至 {exp}"
        text = (
            f"{title}\n"
            f"🏷 级别: {risk.severity}\n"
            f"📝 原文: {str(risk.reason)[:200]}\n"
            f"🔢 连续命中: {rec.get('hit_count')} 次"
            f"{duration_info}"
        )

        async def _send():
            try:
                cid = int(target) if str(target).lstrip("-").isdigit() else target
                await tg.client.send_message(chat_id=cid, text=text)
            except Exception as ex:
                logger.warning("risk TG 告警失败: %s", ex)
        try:
            asyncio.get_running_loop().create_task(_send())
        except RuntimeError:
            pass

    # ── 内部：A/B persona 分配（P2-3） ─────────────
    def _pick_persona_variant(
        self, chat_key: str
    ) -> Tuple[str, str]:
        """按 config.persona_experiment.variants 分配 variant。

        返回 (variant_name, style_hint_text)。实验关闭或无权重时返回 ("", "")。
        Sticky：同一 chat_key 永远落同一 variant（state_store 落盘）。

        ★ P6-5 auto_winner（epsilon-greedy）：
          - 对 **首次出现** 的 chat_key，若启用 auto_winner：
             * 以概率 ε 保留原 weights（探索）
             * 以概率 1-ε 把 winner 的 weight 乘上 boost（利用）
          - 已分配的 chat 完全不动，防"语气突变"。
          - Winner 由 `state_store.variant_stats()` 的 `approve_ratio` 最高者决定，
            需满足 `apr_sent+apr_rejected >= min_samples`，每 refresh_sec 刷一次。
          - ε 随实验运行天数线性衰减：
            eps = max(min_epsilon, init_epsilon * (1 - days/decay_days))
        """
        import random as _random
        import time as _time

        exp = self._cfg.get("persona_experiment") or {}
        if not exp or not bool(exp.get("enabled", False)):
            return "", ""
        variants = exp.get("variants") or []
        if not isinstance(variants, list) or not variants:
            return "", ""
        weights: Dict[str, float] = {}
        hint_map: Dict[str, str] = {}
        for v in variants:
            if not isinstance(v, dict):
                continue
            name = str(v.get("name") or "").strip()
            w = float(v.get("weight", 1.0) or 0.0)
            hint = str(v.get("style_hint") or "").strip()
            if not name or w <= 0:
                continue
            weights[name] = w
            hint_map[name] = hint
        if not weights:
            return "", ""

        # ── P6-5：epsilon-greedy winner override（仅对 new chat_key 起效）──
        aw_cfg = exp.get("auto_winner") or {}
        if aw_cfg.get("enabled", False):
            cached = getattr(self, "_auto_winner_cache", None) or {}
            now = _time.time()
            refresh_sec = float(aw_cfg.get("refresh_sec", 600.0) or 600.0)
            if (now - float(cached.get("ts", 0))) > refresh_sec:
                winner, samples = self._compute_winner_variant(
                    min_samples=int(aw_cfg.get("min_samples", 20) or 20),
                )
                first_ts = float(cached.get("first_ts", 0)) or now
                cached = {
                    "ts": now, "first_ts": first_ts,
                    "winner": winner, "samples": samples,
                }
                self._auto_winner_cache = cached
            winner = str(cached.get("winner") or "")
            if winner and winner in weights:
                # 注意：0.0 是合法探索概率，不能用 `or` 回退默认值
                init_eps = float(aw_cfg.get("init_epsilon", 0.30))
                min_eps = float(aw_cfg.get("min_epsilon", 0.10))
                decay_days = max(float(aw_cfg.get("decay_days", 30.0)), 1.0)
                days = (now - float(cached.get("first_ts", now))) / 86400.0
                eps = max(min_eps, init_eps * (1.0 - days / decay_days))
                if _random.random() > eps:
                    # exploit：把 winner 的权重提至占绝对多数（不强制 100%，
                    # 仍保留其它 variant 的极小漂移，便于收集对照样本）
                    boost = float(aw_cfg.get("winner_boost", 8.0) or 8.0)
                    weights = dict(weights)
                    weights[winner] = weights[winner] * boost
        try:
            picked = self._state.assign_variant(chat_key, weights=weights)
            if picked:
                return picked, hint_map.get(picked, "")
        except Exception:
            logger.debug("assign_variant 异常", exc_info=True)
        return "", ""

    def _pick_reply_profile(
        self,
        chat_key: str,
        chat_name: str = "",
    ) -> Dict[str, Any]:
        """Pick a Messenger-specific persona/profile for the current chat.

        Config shape:
          messenger_rpa.reply_profiles:
            default: warm_friend
            profiles:
              - id: warm_friend
                match_names: ["Alice"]
                language: auto
                style_hint: ...
                persona: {name: "...", role: "...", ...}

        The profile only affects this chat's prompt. It does not persist to
        persona_runtime.yaml, so operators can change config and hot-reload.
        """
        cfg = self._cfg.get("reply_profiles") or {}
        if not isinstance(cfg, dict):
            return {}
        profiles = cfg.get("profiles") or []
        if not isinstance(profiles, list) or not profiles:
            return {}
        chat_key_l = (chat_key or "").lower()
        chat_name_l = (chat_name or "").lower()
        default_id = str(cfg.get("default") or "").strip()
        account_id = str(getattr(self, "_account_id", "") or "").strip()
        account_profile_id = str(
            self._cfg.get("account_reply_profile_id")
            or self._cfg.get("reply_profile_id")
            or self._cfg.get("persona_id")
            or ""
        ).strip()
        # B2 优先级 #1：运营手动指定的 chat_persona_override（最高优先级）
        # web 后台手动给某 chat 指定 reply_profile → 覆盖所有自动匹配规则
        _state_for_override = getattr(self, "_state", None)
        if chat_name and _state_for_override is not None and hasattr(
            _state_for_override, "get_chat_persona_override"
        ):
            try:
                override = _state_for_override.get_chat_persona_override(
                    chat_name=chat_name, account_id=account_id,
                )
                if override:
                    override_pid = str(override.get("reply_profile_id") or "").strip()
                    if override_pid:
                        # 在 profiles 里找这个 id
                        for raw in profiles:
                            if (
                                isinstance(raw, dict)
                                and str(raw.get("id") or raw.get("name") or "").strip() == override_pid
                            ):
                                logger.warning(
                                    "[messenger_rpa] persona pick chat_key=%s chat_name=%r → "
                                    "id=%r source=manual_override (bound_by=%s)",
                                    chat_key, chat_name, override_pid,
                                    override.get("bound_by", "?"),
                                )
                                return raw
                        logger.warning(
                            "[messenger_rpa] manual override 的 profile_id=%r 不存在于 profiles，fallback 自动匹配",
                            override_pid,
                        )
            except Exception:
                logger.debug("get_chat_persona_override failed", exc_info=True)
        default_profile: Dict[str, Any] = {}
        account_profile: Dict[str, Any] = {}
        chosen: Dict[str, Any] = {}
        match_source = ""
        for raw in profiles:
            if not isinstance(raw, dict):
                continue
            pid = str(raw.get("id") or raw.get("name") or "").strip()
            if default_id and pid == default_id:
                default_profile = raw
            if account_profile_id and pid == account_profile_id:
                account_profile = raw
            keys = raw.get("match_chat_keys") or []
            names = raw.get("match_names") or []
            accounts = raw.get("match_account_ids") or raw.get("account_ids") or []
            if isinstance(keys, str):
                keys = [keys]
            if isinstance(names, str):
                names = [names]
            if isinstance(accounts, str):
                accounts = [accounts]
            if any(str(k).strip().lower() and str(k).strip().lower() in chat_key_l for k in keys):
                chosen = raw
                match_source = "chat_key"
                break
            if any(str(n).strip().lower() and str(n).strip().lower() in chat_name_l for n in names):
                chosen = raw
                match_source = "chat_name"
                break
            if account_id and any(str(a).strip() == account_id for a in accounts):
                account_profile = raw
        if not chosen:
            if account_profile:
                chosen = account_profile
                match_source = "account_id_or_pinned"
            elif default_profile:
                chosen = default_profile
                match_source = "default"
        # P0-B：决策可观测性 — 记录命中的人设 ID + 来源 + 关键字段，方便运营在
        # web 改完配置后到日志里对账"我改的字段是不是真生效了"。
        try:
            picked_id = str(
                (chosen.get("id") if isinstance(chosen, dict) else "")
                or (chosen.get("name") if isinstance(chosen, dict) else "")
                or ""
            ).strip()
            persona_dict = (
                chosen.get("persona") if isinstance(chosen.get("persona"), dict) else {}
            ) if isinstance(chosen, dict) else {}
            persona_name = str(persona_dict.get("name") or "").strip()
            persona_lang = str((chosen or {}).get("language") or "").strip()
            style_hint = str((chosen or {}).get("style_hint") or "").strip()
            forb = ((persona_dict.get("speaking") or {}).get("forbidden_phrases") or []) \
                if isinstance(persona_dict, dict) else []
            logger.warning(
                "[messenger_rpa] persona pick chat_key=%s chat_name=%r → "
                "id=%r source=%s persona_name=%r lang=%s style_hint=%r "
                "forbidden_n=%d default_id=%r",
                chat_key, chat_name, picked_id, match_source or "none",
                persona_name, persona_lang or "auto", style_hint[:40],
                len(forb) if isinstance(forb, list) else 0, default_id,
            )
        except Exception:
            logger.debug("[messenger_rpa] persona pick log failed", exc_info=True)
        return chosen

    @staticmethod
    def _strip_lang_detection_markup(text: str) -> str:
        """Remove local RPA labels that would bias language detection to zh."""
        t = str(text or "")
        t = t.replace("[对方连发]", " ")
        t = re.sub(r"^\(\d+\)\s*", "", t, flags=re.M)
        t = re.sub(
            r"\[(最新|图片|链接|语音|贴纸|文件|image|link|voice|sticker|file)[^\]]*\]",
            " ",
            t,
            flags=re.I,
        )
        t = re.sub(r"^【[^】]{1,20}】", " ", t)
        return t.strip()

    def _previous_reply_lang(self, chat_key: str) -> str:
        try:
            cs = getattr(self._sm, "_context_store", None)
            if cs is None:
                return ""
            uctx = cs.get(chat_key) or {}
            lang = str(uctx.get("reply_lang") or "").strip().lower()
            return lang
        except Exception:
            return ""

    def _resolve_reply_lang(
        self,
        *,
        peer_msg: PeerMessage,
        text_for_ai: str,
        chat_key: str,
        profile: Dict[str, Any],
    ) -> str:
        """Resolve output language with current text first, then profile/history.

        Non-text media often arrives as Chinese local labels ("[图片]"), so for
        those messages we prefer explicit profile language or prior chat language.

        Config keys:
          force_reply_lang   e.g. "ja" — hard override, ignores all detection.
          default_reply_lang e.g. "ja" — fallback when detection yields nothing.
        """
        # Global hard override: force_reply_lang skips all detection.
        _force_global = str(self._cfg.get("force_reply_lang") or "").strip().lower()
        if _force_global and _force_global not in ("auto", "detect", ""):
            return _force_global

        forced = str(profile.get("language") or "").strip().lower()
        if forced and forced not in ("auto", "detect"):
            return forced

        cfg_default = str(self._cfg.get("default_reply_lang", "zh") or "zh").lower()
        ai_for_lang = getattr(self._sm, "ai_client", None)
        votes: Dict[str, int] = {}
        if peer_msg.kind == "text":
            lines: List[str] = []
            if "[对方连发]" in text_for_ai:
                for line in text_for_ai.splitlines():
                    clean = self._strip_lang_detection_markup(line)
                    if clean:
                        lines.append(clean)
            else:
                clean = self._strip_lang_detection_markup(peer_msg.raw or text_for_ai)
                if clean:
                    lines.append(clean)
            for line in lines:
                lang = _detect_peer_lang(line, ai_client=ai_for_lang)
                if lang not in ("unknown", ""):
                    votes[lang] = votes.get(lang, 0) + 1
        if votes:
            non_en = {k: v for k, v in votes.items() if k != "en"}
            return max(non_en or votes, key=(non_en or votes).get)

        prev = self._previous_reply_lang(chat_key)
        if prev:
            return prev
        return cfg_default

    def _compute_winner_variant(
        self, *, min_samples: int = 20
    ) -> Tuple[str, int]:
        """从 variant_stats 找 approve_ratio 最高且样本足够的 variant。

        返回 (winner_name, max_samples)；无合格 winner 则返回 ("", 0)。
        """
        try:
            stats = self._state.variant_stats() or {}
        except Exception:
            return "", 0
        variants_d = stats.get("variants") or stats
        best: Optional[Tuple[str, float, int]] = None
        for name, d in variants_d.items():
            if not isinstance(d, dict) or name == "_none":
                continue
            samples = int(d.get("apr_sent", 0)) + int(d.get("apr_rejected", 0))
            if samples < min_samples:
                continue
            ratio = d.get("approve_ratio")
            if ratio is None:
                continue
            r = float(ratio)
            if best is None or r > best[1] or (
                r == best[1] and samples > best[2]
            ):
                best = (str(name), r, samples)
        return (best[0], best[2]) if best else ("", 0)

    # ── 内部：P3-6 episodic 摘要 ───────────────────
    def _dispatch_episodic_summary(self, chat_key: str) -> None:
        """在 reply 发送成功后调。

        异步触发一次 LLM 摘要（若达阈值），结果写 context_store._conversation_summary。
        失败不影响主流程。
        """
        try:
            em_cfg = (self._cfg.get("episodic_memory") or {})
            if not em_cfg.get("enabled", True):
                return
            threshold = int(em_cfg.get("threshold_rounds", 12) or 12)
            cooldown = int(em_cfg.get("cooldown_rounds", 5) or 5)
            keep_tail = int(em_cfg.get("keep_tail_rounds", 5) or 5)
            max_chars = int(em_cfg.get("max_chars", 200) or 200)

            cs = getattr(self._sm, "_context_store", None)
            if cs is None:
                return
            ctx = cs.get(chat_key)
            hist = ctx.get("_conversation_history") or []
            if not isinstance(hist, list):
                return
            rounds = len(hist) // 2
            if rounds < threshold:
                return
            # cooldown：距上次摘要的轮数
            last_sum_rounds = int(ctx.get("_last_summary_rounds") or 0)
            if (rounds - last_sum_rounds) < cooldown:
                return
            # 找 ai client
            ai = getattr(self._sm, "_ai_client", None) or getattr(self._sm, "ai_client", None)
            if ai is None or not hasattr(ai, "summarize_conversation"):
                return

            # P7-4：长期记忆配置
            ltm_cfg = (self._cfg.get("long_term_memory") or {})
            ltm_enabled = bool(ltm_cfg.get("enabled", True))
            ltm_every = int(ltm_cfg.get("refresh_every_summaries", 3) or 3)
            ltm_max_facts = int(ltm_cfg.get("max_facts", 15) or 15)
            ltm_abs_rounds = int(ltm_cfg.get("min_rounds", 20) or 20)

            async def _bg():
                try:
                    logger.info(
                        "[messenger_rpa] P3-6 summarize chat=%s rounds=%d",
                        chat_key, rounds,
                    )
                    summary = await ai.summarize_conversation(
                        hist, max_chars=max_chars, timeout_sec=12.0,
                    )
                    if not summary or len(summary) < 8:
                        return
                    # 写回 + 裁剪 history 只留 tail
                    ctx2 = cs.get(chat_key)
                    ctx2["_conversation_summary"] = summary
                    ctx2["_last_summary_rounds"] = rounds
                    # P7-4：记录已压缩次数，用于触发长期蒸馏
                    sum_count = int(ctx2.get("_summary_count") or 0) + 1
                    ctx2["_summary_count"] = sum_count
                    old_hist = ctx2.get("_conversation_history") or []
                    if isinstance(old_hist, list) and len(old_hist) > keep_tail * 2:
                        ctx2["_conversation_history"] = old_hist[-keep_tail * 2:]
                    cs.mark_dirty(chat_key)
                    try:
                        cs.flush(chat_key)
                    except Exception:
                        pass
                    logger.info(
                        "[messenger_rpa] P3-6 summary ok chat=%s len=%d "
                        "summary_count=%d",
                        chat_key, len(summary), sum_count,
                    )

                    # ── P7-4：长期事实蒸馏（二级压缩）──
                    if not ltm_enabled:
                        return
                    # 触发条件：每 ltm_every 次 summary 或 rounds 超过 ltm_abs_rounds
                    should_distill = (
                        (sum_count % ltm_every == 0)
                        or (rounds >= ltm_abs_rounds
                            and not ctx2.get("_long_term_memory"))
                    )
                    if not should_distill:
                        return
                    if not hasattr(ai, "extract_long_term_facts"):
                        return
                    ltm = ctx2.get("_long_term_memory") or {}
                    existing = list(ltm.get("facts") or [])
                    try:
                        new_facts = await ai.extract_long_term_facts(
                            working_summary=summary,
                            recent_history=(ctx2.get("_conversation_history") or []),
                            existing_facts=existing,
                            max_facts=ltm_max_facts,
                            timeout_sec=15.0,
                        )
                    except Exception:
                        logger.debug("P7-4 extract_long_term_facts 异常",
                                     exc_info=True)
                        return
                    if not new_facts:
                        return
                    # 去重（保持顺序）+ 限长
                    dedup: List[str] = []
                    seen = set()
                    for f in new_facts:
                        s = str(f).strip()
                        if s and s not in seen:
                            seen.add(s)
                            dedup.append(s)
                    dedup = dedup[:ltm_max_facts]
                    ctx3 = cs.get(chat_key)
                    ctx3["_long_term_memory"] = {
                        "facts": dedup,
                        "last_updated_ts": time.time(),
                        "distill_rounds": rounds,
                    }
                    cs.mark_dirty(chat_key)
                    try:
                        cs.flush(chat_key)
                    except Exception:
                        pass
                    logger.info(
                        "[messenger_rpa] P7-4 long_term_memory ok chat=%s "
                        "facts=%d (existing=%d)",
                        chat_key, len(dedup), len(existing),
                    )
                except Exception:
                    logger.debug("P3-6 summarize 异常", exc_info=True)

            try:
                asyncio.get_running_loop().create_task(
                    _bg(), name=f"mrpa_episodic_{chat_key[:20]}",
                )
            except RuntimeError:
                pass
        except Exception:
            logger.debug("_dispatch_episodic_summary 异常", exc_info=True)

    # ── 内部：延迟优化（P3-3） ─────────────────────
    def _should_prefetch_caption(self) -> bool:
        """是否在 thread_combined 之前就并发启动 caption（乐观并发）。

        只在所有条件都成立才启动，避免无意义 token 消耗：
          - media_deep_understand.enabled == true
          - media_deep_understand.prefetch != false（默认开）
          - media_handling_policy 会用到 AI 处理（ai / ack_and_approve）
        """
        deep = self._cfg.get("media_deep_understand") or {}
        if not deep.get("enabled", True):
            return False
        if deep.get("prefetch", True) is False:
            return False
        policy = str(self._cfg.get("media_handling_policy") or "").strip().lower()
        return policy in ("ai", "ack_and_approve")

    # ── 内部：图片深度理解（P2-1） ─────────────────
    def _deep_timeout(self) -> float:
        cfg = (self._cfg.get("media_deep_understand") or {})
        try:
            return float(cfg.get("timeout_sec", 8.0) or 8.0)
        except (TypeError, ValueError):
            return 8.0

    def _deep_lang(self) -> str:
        """caption 输出语言：沿用 language_alignment 推断。"""
        align = str(
            self._cfg.get("language_alignment", "english_fallback_only")
        ).lower().strip()
        default_lang = str(self._cfg.get("default_reply_lang", "zh")).lower()
        # auto/english_fallback_only → 默认中文（配 Leo/Camille 人设）
        # off 且 default=en → 英文
        if align == "off" and default_lang.startswith("en"):
            return "en"
        return "zh"

    async def _bg_enrich_image_caption(
        self, approval_id: int, image_path: str
    ) -> None:
        """后台任务：拿 caption → patch 到 approval.extra_json。"""
        try:
            caption = await self._try_describe_peer_image(
                image_path, timeout_sec=self._deep_timeout()
            )
            if caption:
                self._state.patch_approval_extra(
                    int(approval_id),
                    patch={
                        "image_caption": caption,
                        "image_caption_ts": time.time(),
                    },
                )
                logger.info(
                    "[messenger_rpa] approval #%s 补充 image_caption=%r",
                    approval_id, caption[:120],
                )
        except Exception:
            logger.debug("bg image caption 异常", exc_info=True)

    async def _try_describe_peer_image(
        self, image_path: str, *, timeout_sec: float = 8.0,
        kind: str = "_generic",
    ) -> str:
        """同步调用：返回图片 caption 或空串。超时/失败都静默返回空串。

        P1.5-A (2026-05-04)：kind 参数透传到 vision——image/video/gif/sticker/
        animated_sticker 用各自专属 prompt。
        P1.5+D3 (2026-05-04)：默认 kind 从 'image' 改 '_generic'——prefetch 不
        知 kind 时用通用 prompt 覆盖任何媒体（命中率提升 ~25%）；_generate_reply
        里若 prefetch caption 质量不足再用 peer_msg.kind 同步重调一次。
        """
        cfg = (self._cfg.get("media_deep_understand") or {})
        if not cfg.get("enabled", True):
            return ""
        if not image_path:
            return ""
        try:
            from pathlib import Path as _P
            if not _P(image_path).exists():
                return ""
        except Exception:
            return ""
        try:
            from src.integrations.messenger_rpa.combined_vision import (
                describe_peer_image_detail,
            )
            vision_cfg = self._vision_cfg()
            global_vision = self._global_vision_cfg()
            lang = self._deep_lang()
            caption, tag = await asyncio.wait_for(
                describe_peer_image_detail(
                    image_path,
                    vision_cfg=vision_cfg,
                    global_vision=global_vision,
                    language=lang,
                    kind=kind,
                ),
                timeout=float(timeout_sec),
            )
            logger.debug(
                "[messenger_rpa] image caption tag=%s caption=%r",
                tag, (caption or "")[:120],
            )
            return str(caption or "").strip()
        except asyncio.TimeoutError:
            logger.warning(
                "[messenger_rpa] image deep-understand 超时 (%.1fs)", timeout_sec,
            )
        except Exception:
            logger.debug(
                "[messenger_rpa] image deep-understand 异常", exc_info=True,
            )
        return ""

    async def _try_transcribe_peer_voice(
        self,
        serial: str,
        *,
        chat_name: str = "",
        wh: Optional[Tuple[int, int]] = None,
        result: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Try to turn the latest Messenger voice note into text.

        Android Messenger does not expose voice files to non-root ADB.  For
        production APKs, prefer the helper app path: MediaProjection captures
        Messenger playback while the runner plays the voice bubble.
        """
        cfg = self._cfg.get("voice_input") or {}
        if not isinstance(cfg, dict) or not cfg.get("enabled", False):
            return ""
        if not serial:
            return ""
        if result is not None:
            result["voice_input_enabled"] = True
        try:
            from src.integrations.messenger_rpa.voice_grabber import VoiceGrabber
            from src.integrations.messenger_rpa.voice_grabber import VoiceGrabResult
            package = str(
                cfg.get("package")
                or self._cfg.get("messenger_package")
                or "com.facebook.orca"
            )
            out_dir = str(cfg.get("out_dir") or "tmp_voice_notes")
            grabber = VoiceGrabber(serial, package=package, out_dir=out_dir)
            capture_mode = str(cfg.get("capture_mode") or "run_as").strip().lower()
            if result is not None:
                result["voice_capture_mode"] = capture_mode
            if capture_mode in ("screenrecord", "playback_record"):
                grabbed = await asyncio.to_thread(
                    grabber.record_playback_window,
                    float(cfg.get("record_sec", 8.0) or 8.0),
                )
            elif capture_mode in ("helper_session", "session_helper", "audio_bridge_session"):
                xy = cfg.get("voice_tap_xy")
                tap_xy = None
                if isinstance(xy, (list, tuple)) and len(xy) >= 2:
                    try:
                        tap_xy = (int(xy[0]), int(xy[1]))
                    except Exception:
                        tap_xy = None
                grabbed = await asyncio.to_thread(
                    grabber.capture_messenger_voice_session,
                    duration_sec=float(cfg.get("record_sec", 8.0) or 8.0),
                    apk_path=str(
                        cfg.get("helper_apk_path")
                        or "tools/audio_capture_helper/build/MrpAudioBridge.apk"
                    ),
                    helper_package=str(
                        cfg.get("helper_package")
                        or "com.codex.mrpaudiobridge"
                    ),
                    auto_install=cfg.get("helper_auto_install", True) is not False,
                    expected_peer=str(cfg.get("expected_peer") or chat_name or ""),
                    screen_wh=wh,
                    voice_tap_xy=tap_xy,
                    silence_max_abs=int(cfg.get("silence_max_abs", 120) or 120),
                    find_voice_scroll_attempts=int(
                        cfg.get("find_voice_scroll_attempts", 2) or 0
                    ),
                )
            elif capture_mode in ("helper_app", "media_projection", "audio_bridge"):
                grabbed = await asyncio.to_thread(
                    grabber.capture_with_helper_app,
                    duration_sec=float(cfg.get("record_sec", 6.0) or 6.0),
                    apk_path=str(
                        cfg.get("helper_apk_path")
                        or "tools/audio_capture_helper/build/MrpAudioBridge.apk"
                    ),
                    package=str(
                        cfg.get("helper_package")
                        or "com.codex.mrpaudiobridge"
                    ),
                    auto_install=cfg.get("helper_auto_install", True) is not False,
                    wait_for_user_consent_sec=float(
                        cfg.get("helper_wait_sec", 12.0) or 12.0
                    ),
                )
            elif capture_mode in ("external_file", "local_file", "test_file"):
                local_path = str(
                    cfg.get("external_file_path")
                    or cfg.get("test_audio_path")
                    or cfg.get("local_path")
                    or ""
                ).strip()
                p = Path(local_path) if local_path else None
                if p is not None and p.exists():
                    grabbed = VoiceGrabResult(
                        ok=True,
                        local_path=str(p),
                        method="external_file",
                    )
                else:
                    grabbed = VoiceGrabResult(
                        ok=False,
                        method="external_file",
                        error=f"missing_external_file:{local_path}",
                    )
            else:
                grabbed = await asyncio.to_thread(grabber.try_grab_latest_voice)
            if result is not None:
                result["voice_capture_method"] = getattr(grabbed, "method", "")
                result["voice_capture_path"] = getattr(grabbed, "local_path", "")
            if not getattr(grabbed, "ok", False):
                if result is not None:
                    result["voice_capture_error"] = str(
                        getattr(grabbed, "error", "")
                    )[:200]
                logger.info(
                    "[messenger_rpa] voice grab skipped method=%s err=%s",
                    getattr(grabbed, "method", ""),
                    str(getattr(grabbed, "error", ""))[:120],
                )
                return ""

            from src.ai.audio_pipeline import get_audio_pipeline
            ap_cfg = self._cfg.get("audio_pipeline") or {}
            if isinstance(cfg.get("audio_pipeline"), dict):
                ap_cfg = cfg.get("audio_pipeline") or ap_cfg
            ap = get_audio_pipeline(ap_cfg)
            if result is not None:
                result["voice_asr_backend"] = str(ap_cfg.get("backend") or "")
            rv = await ap.transcribe_file(
                getattr(grabbed, "local_path", ""),
                language_hint=str(cfg.get("language_hint") or "").strip() or None,
                timeout_sec=float(cfg.get("timeout_sec", 30) or 30),
            )
            if rv.ok and rv.text:
                if result is not None:
                    result["voice_asr_ok"] = True
                return str(rv.text).strip()
            if result is not None:
                result["voice_asr_ok"] = False
                result["voice_asr_error"] = str(rv.error or "")[:200]
            logger.info(
                "[messenger_rpa] voice transcribe skipped err=%s",
                str(rv.error or "")[:120],
            )
        except Exception:
            if result is not None:
                result["voice_asr_ok"] = False
                result["voice_asr_error"] = "exception"
            logger.debug("[messenger_rpa] voice transcribe failed", exc_info=True)
        return ""

    async def _maybe_prepare_tts_reply(
        self,
        reply_text: str,
        peer_msg: PeerMessage,
        result: Dict[str, Any],
    ) -> None:
        """Generate a TTS artifact for approval/audit.

        Actual Messenger voice attachment sending remains gated behind a later
        device-specific transport.  This step is still valuable in production:
        it proves provider quality/latency and gives operators an audio file to
        review before enabling automatic voice delivery.
        """
        cfg = self._cfg.get("voice_output") or {}
        if not isinstance(cfg, dict) or not cfg.get("enabled", False):
            return
        mode = str(cfg.get("mode") or "approval_only").strip().lower()
        trigger = str(cfg.get("trigger") or "when_peer_voice").strip().lower()
        if trigger == "when_peer_voice":
            if not result.get("voice_transcript"):
                return
        elif trigger == "random":
            import random as _rnd
            _prob = max(0.0, min(1.0, float(cfg.get("voice_probability", 0.3) or 0.3)))
            if _rnd.random() >= _prob:
                result.setdefault("hints", []).append(
                    f"tts_skipped_by_prob:threshold={_prob:.0%}"
                )
                return
        # trigger == "always" or any unrecognised value → fall through
        if mode in ("off", "disabled"):
            return
        max_chars = int(cfg.get("max_text_chars", 220) or 220)
        text = (reply_text or "").strip()
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "..."
            result["tts_truncated"] = True
        try:
            from src.ai.tts_pipeline import get_tts_pipeline

            tts = get_tts_pipeline(cfg)
            rv = await tts.synthesize(
                text,
                timeout_sec=float(cfg.get("timeout_sec", 30) or 30),
            )
            result["tts_provider"] = rv.provider
            result["tts_latency_ms"] = rv.latency_ms
            if rv.ok:
                # ── P3-A：时长硬校验 ──
                # 防御：WAV header bug 曾把 5s 音频报成 745:39（DataSize=INT_MAX）。
                # 这里对照 voice_output.max_seconds × max_ratio 算硬上限；
                # duration 不可测（-1.0）时跳过（不当失败，避免 MP3/Opus 误杀）。
                max_sec = float(cfg.get("max_seconds", 20) or 20)
                min_sec = float(cfg.get("duration_min_sec", 0.3) or 0.3)
                max_ratio = float(cfg.get("duration_max_ratio", 1.5) or 1.5)
                hard_max = max_sec * max_ratio
                result["tts_duration_sec"] = round(rv.duration_sec, 2)
                result["tts_duration_source"] = rv.duration_source
                duration_ok = True
                if rv.duration_sec > 0:
                    if rv.duration_sec > hard_max:
                        duration_ok = False
                        rv.error = (
                            f"duration_exceeds_hard_max:"
                            f"{rv.duration_sec:.1f}s>{hard_max:.1f}s "
                            f"(source={rv.duration_source})"
                        )
                    elif rv.duration_sec < min_sec:
                        duration_ok = False
                        rv.error = (
                            f"duration_below_min:"
                            f"{rv.duration_sec:.1f}s<{min_sec:.1f}s "
                            f"(source={rv.duration_source})"
                        )
                if not duration_ok:
                    result["tts_error"] = rv.error
                    result.setdefault("hints", []).append(
                        f"tts_duration_guard_blocked:{rv.duration_sec:.1f}s"
                    )
                    # 删掉坏的 artifact，避免误投
                    try:
                        import os as _os
                        if rv.audio_path and _os.path.isfile(rv.audio_path):
                            _os.remove(rv.audio_path)
                    except Exception:
                        pass
                    return
                result["tts_audio_path"] = rv.audio_path
                result["tts_voice"] = rv.voice
                result["tts_format"] = rv.format
                result.setdefault("hints", []).append("tts_ready_for_review")
                if mode == "auto_voice" and self._reply_mode == "auto":
                    await self._maybe_send_tts_audio(rv.audio_path, cfg, result)
                    # ★ 若 share-sheet 发送失败，需按 BACK 回到 Messenger 聊天页
                    # 否则后续 text send 会在 "Send to" 页面操作，导致循环
                    # 但如果是预检就中止的（share_skip_），share 根本没打开，不需要 BACK
                    _tts_err = str(result.get("tts_send_error") or "")
                    if (
                        not result.get("tts_send_ok")
                        and not _tts_err.startswith("share_skip_")
                    ):
                        _serial = str(
                            result.get("adb_serial")
                            or self._cfg.get("adb_serial") or ""
                        ).strip()
                        if _serial:
                            for _ in range(3):
                                adb.run_adb(
                                    ["shell", "input", "keyevent", "KEYCODE_BACK"],
                                    serial=_serial, timeout=5.0,
                                )
                                await asyncio.sleep(0.4)
                            logger.warning(
                                "[messenger_rpa] TTS share 失败，已 BACK 3次"
                                " 尝试回到聊天页 error=%s", _tts_err,
                            )
            else:
                result["tts_error"] = rv.error
                result.setdefault("hints", []).append("tts_failed_text_fallback")
        except Exception:
            result.setdefault("hints", []).append("tts_exception_text_fallback")
            logger.debug("[messenger_rpa] TTS generation failed", exc_info=True)

    async def _maybe_send_tts_audio(
        self,
        audio_path: str,
        cfg: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        """Best-effort Messenger audio attachment send for explicit auto_voice mode."""
        if not audio_path:
            return
        serial = str(result.get("adb_serial") or self._cfg.get("adb_serial") or "").strip()
        if not serial:
            result["tts_send_error"] = "missing_adb_serial"
            return
        xy = cfg.get("share_recipient_tap_xy")
        recipient_tap = None
        if isinstance(xy, (list, tuple)) and len(xy) >= 2:
            try:
                recipient_tap = (int(xy[0]), int(xy[1]))
            except Exception:
                recipient_tap = None
        xy = cfg.get("share_send_tap_xy")
        send_tap = None
        if isinstance(xy, (list, tuple)) and len(xy) >= 2:
            try:
                send_tap = (int(xy[0]), int(xy[1]))
            except Exception:
                send_tap = None
        recipient_name = str(
            cfg.get("share_recipient_name")
            or result.get("chat_name")
            or result.get("target_name")
            or ""
        ).strip()
        if send_tap is None and not recipient_name:
            result["tts_send_error"] = "missing_share_recipient"
            result.setdefault("hints", []).append("tts_send_needs_recipient_name")
            return
        try:
            from src.integrations.messenger_rpa.voice_sender import (
                MessengerVoiceSender,
            )

            sender = MessengerVoiceSender(
                serial,
                package=str(
                    cfg.get("messenger_package")
                    or self._cfg.get("messenger_package")
                    or "com.facebook.orca"
                ),
            )
            rv = await asyncio.to_thread(
                sender.send_audio_file,
                audio_path,
                recipient_name=recipient_name,
                recipient_tap_xy=recipient_tap,
                send_tap_xy=send_tap,
                auto_find_send_button=bool(cfg.get("auto_find_share_send_button", True)),
                auto_search_recipient=bool(cfg.get("auto_search_share_recipient", True)),
                audit_dir=str(
                    cfg.get("send_audit_dir")
                    or self._cfg.get("debug_screenshot_dir")
                    or "tmp_messenger_rpa"
                ),
            )
            result["tts_send_ok"] = rv.ok
            result["tts_send_remote_path"] = rv.remote_path
            result["tts_send_method"] = rv.method
            result["tts_send_extra"] = rv.extra
            if rv.ok:
                result.setdefault("hints", []).append("tts_voice_sent")
            else:
                result["tts_send_error"] = rv.error
                result.setdefault("hints", []).append("tts_send_failed_text_fallback")
        except Exception as ex:
            result["tts_send_error"] = f"{type(ex).__name__}: {ex}"
            result.setdefault("hints", []).append("tts_send_exception_text_fallback")

    # ── P2++G_FIX (2026-05-04) modality 决策 + sticker hook 统一入口 ──
    # 之前两条 reply 路径有不一致问题：
    #   - _generate_reply（LLM reply）路径：调 modality 决策 + dry_run log
    #   - _maybe_media_ack（fast-ack）路径：完全跳过 → peer 发 sticker 永远没决策
    # 这个 helper 把两条路径统一，让 sticker / image / voice 的 fast-ack 也能
    # 触发 modality 决策（dry_run 阶段只 log，real_send 阶段真发 sticker）。
    async def _run_modality_hook(
        self,
        *,
        serial: str,
        target: UnreadChat,
        chat_key: str,
        peer_msg: PeerMessage,
        reply_text: str,
        multi_peer_count: int,
        peer_sticker_cat: Optional[str],
        result: Dict[str, Any],
        caption: Optional[str] = None,
    ) -> None:
        """跑 modality 决策 + 拼 hints + dry_run log / real send 调度。"""
        try:
            modality_dec = self._decide_reply_modality(
                reply_text=reply_text or "",
                peer_msg=peer_msg,
                peer_sticker_cat=peer_sticker_cat,
                multi_peer_count=int(multi_peer_count or 0),
                chat_key=chat_key,
                caption=caption,
            )
            result["modality_decision"] = modality_dec
            _mod = str(modality_dec.get("modality") or "text")
            _reason = str(modality_dec.get("reason") or "")
            _mod_norm = _mod.replace("+", "_with_")
            _reason_base = _reason.split(":", 1)[0].strip() or "unknown"
            hints = result.setdefault("hints", [])
            hints.append(f"modality:{_mod}:{_reason}")
            hints.append(f"modality_{_mod_norm}")
            hints.append(f"modality_reason_{_reason_base}")
            _cat = modality_dec.get("sticker_cat")
            if modality_dec.get("would_send") and _cat:
                hints.append(f"modality_cat_{_cat}")

            # 真发 hook（dry_run + real_send_enabled 双开关同时打开才真发）
            sr_cfg = self._cfg.get("sticker_reply") or {}
            _real = bool(sr_cfg.get("real_send_enabled", False))
            if (
                modality_dec.get("would_send")
                and not modality_dec.get("dry_run", True)
                and _real
                and modality_dec.get("sticker_path")
            ):
                asyncio.create_task(self._send_sticker_after_text(
                    serial=serial,
                    chat_name=target.name or "",
                    sticker_path=modality_dec["sticker_path"],
                    sticker_cat=modality_dec.get("sticker_cat") or "",
                    result=result,
                ))
            elif modality_dec.get("would_send") and modality_dec.get("dry_run"):
                logger.info(
                    "[messenger_rpa] sticker DRY_RUN would-send chat=%s "
                    "cat=%s path=%s reason=%s",
                    chat_key, modality_dec.get("sticker_cat"),
                    modality_dec.get("sticker_path"),
                    modality_dec.get("reason"),
                )
        except Exception:
            logger.debug(
                "[messenger_rpa] modality hook failed", exc_info=True,
            )

    # ── P2.3 (2026-05-04) sticker 真发：复用 voice_sender share intent ──
    # voice_sender.send_audio_file 内部用 mimetypes.guess_type 自动识别——PNG
    # 文件会变成 mime=image/png 走 SEND intent，下游 share UI 完全相同。
    # 因此直接复用，不必新写一个 send_image_via_share。
    async def _send_sticker_after_text(
        self,
        *,
        serial: str,
        chat_name: str,
        sticker_path: str,
        sticker_cat: str,
        result: Dict[str, Any],
    ) -> None:
        """text 已发出后，异步发 sticker（image/png via share intent）。

        默认只在 sticker_reply.dry_run=False AND real_send_enabled=True 双开关
        都打开时触发。share intent 会跳出当前 chat → 系统选择器 → 选 messenger
        → 找接收方 → 点发送。完成后 messenger 会回到刚才的 chat（粘性会保留）。
        """
        try:
            from src.integrations.messenger_rpa.voice_sender import (
                MessengerVoiceSender,
            )
            cfg = self._cfg.get("voice_output") or {}
            sender = MessengerVoiceSender(
                serial,
                package=str(
                    cfg.get("messenger_package")
                    or self._cfg.get("messenger_package")
                    or "com.facebook.orca"
                ),
            )
            # text 渲染缓冲——给 messenger 1.5s 把刚发的文字渲染出来再切 share
            await asyncio.sleep(1.5)
            rv = await asyncio.to_thread(
                sender.send_audio_file,  # 通用：mime 由扩展名决定
                sticker_path,
                recipient_name=chat_name,
                auto_find_send_button=bool(cfg.get("auto_find_share_send_button", True)),
                auto_search_recipient=bool(cfg.get("auto_search_share_recipient", True)),
                audit_dir=str(
                    cfg.get("send_audit_dir")
                    or self._cfg.get("debug_screenshot_dir")
                    or "tmp_messenger_rpa"
                ),
            )
            result["sticker_send_ok"] = rv.ok
            result["sticker_send_extra"] = rv.extra
            if rv.ok:
                result.setdefault("hints", []).append(
                    f"sticker_sent:cat={sticker_cat}"
                )
                logger.warning(
                    "[messenger_rpa] ✅ sticker sent chat=%r cat=%s path=%s",
                    chat_name, sticker_cat,
                    Path(sticker_path).name,
                )
            else:
                result.setdefault("hints", []).append(
                    f"sticker_send_failed:{rv.error or 'unknown'}"
                )
                logger.warning(
                    "[messenger_rpa] ❌ sticker send failed chat=%r err=%s",
                    chat_name, rv.error,
                )
        except Exception as ex:
            logger.debug(
                "[messenger_rpa] _send_sticker_after_text exception: %s",
                ex, exc_info=True,
            )
            result.setdefault("hints", []).append(
                f"sticker_send_exception:{type(ex).__name__}"
            )

    # ── 内部：AI 回复 ─────────────────────────────
    async def _generate_reply(
        self,
        peer_msg: PeerMessage,
        target: UnreadChat,
        chat_key: str,
        result: Dict[str, Any],
    ) -> str:
        if self._sm is None:
            result["error"] = "skill_manager 未注入"
            return ""

        text_for_ai = peer_msg.to_text_for_ai()
        if not text_for_ai.strip():
            return ""

        # 🛡 P3-X：统一 send gate（最后保险层）
        # 即使其他路径漏检，_generate_reply 入口最后过一次门
        _gate_reason = self._should_skip_send(
            target.name or "", source="generate_reply_entry",
        )
        if _gate_reason:
            result["step"] = "send_gate_skip"
            result["error"] = f"gate:{_gate_reason}"
            return ""

        # P3-A：疯狂回复熔断器（防客户被刷屏的最后底线）
        # 同一 chat 在 5 分钟内 sent ≥ 3 次 → 立即熔断 + 30 分钟 cooldown
        try:
            if self._check_runaway_circuit(target.name or "", result):
                result["step"] = "runaway_paused"
                result["error"] = "runaway_circuit_tripped"
                return ""
        except Exception:
            logger.debug("runaway circuit check failed", exc_info=True)

        # ★ P0-A：peer message quiet window（防"对方连发→机器人重复回复"）
        # 场景：peer t=0 发 (1) → run_once 启动处理 → peer t=2 发 (2) → 系统
        # 已经基于 (1) 生成完回复发出 → 下一次 run_once 把 (2) 当新未读再回一次。
        # 在生成 LLM 回复前留一个 quiet window：(a) 给对方把后续消息发完，下一
        # 次 run_once 通过 extra_peers 自然合并；(b) peer_typing 探测到正在打字
        # 时再延长一段，让对方完整表达。设置 0 即禁用。
        try:
            quiet_window = float(self._cfg.get("peer_message_quiet_window_sec", 1.8) or 0)
        except (TypeError, ValueError):
            quiet_window = 0.0
        if quiet_window > 0:
            try:
                quiet_max = float(
                    self._cfg.get("peer_message_quiet_window_max_sec", 10.0) or 10.0
                )
            except (TypeError, ValueError):
                quiet_max = 10.0
            wait_sec = quiet_window
            # 如果 vision peer_typing 在 prefetch 里已检测到打字，叠加它的建议等候
            pt_task = result.get("_peer_typing_prefetch_task")
            if pt_task is not None:
                try:
                    pt_res = await asyncio.wait_for(pt_task, timeout=2.0)
                    is_typing = bool(getattr(pt_res, "is_typing", False))
                    suggested = float(getattr(pt_res, "suggested_wait_sec", 0.0) or 0.0)
                    if is_typing and suggested > 0:
                        wait_sec = min(quiet_max, max(quiet_window, suggested))
                        result["peer_typing_detected"] = True
                        result["peer_typing_wait_sec"] = wait_sec
                except (asyncio.TimeoutError, Exception):
                    pass
            wait_sec = min(wait_sec, quiet_max)
            logger.info(
                "[messenger_rpa] P0-A peer quiet window chat=%s wait=%.1fs",
                chat_key, wait_sec,
            )
            await asyncio.sleep(wait_sec)
            result.setdefault("hints", []).append(f"peer_quiet_window:{wait_sec:.1f}s")

        # ★ P2-1 + P3-3 + P1-A1/A2/1.4 (2026-05-04)：媒体深度理解 + 上下文 fusion
        # - 媒体类（image/video/gif/sticker/animated_sticker）一律走 caption 同步等回
        # - sticker 类附加 _classify_sticker_category 得到 happy/love/sad/angry/cute 标签
        # - fusion_hint：上一句 peer 文本（本轮 extra_peers / 上一轮 chat_state.last_peer_text）
        #                帮 LLM 判意图（"看看我的照片" + 自拍 → 倾向夸；+ 动物 → 调侃）
        # 必须在 P2-2 多消息合并之前做，确保合并后增强后的 text_for_ai 能进 prompt
        _MEDIA_KINDS_FOR_CAPTION = {
            "image", "video", "gif", "sticker", "animated_sticker",
        }
        if peer_msg.kind in _MEDIA_KINDS_FOR_CAPTION:
            caption = ""
            _cap_task = result.pop("_cap_task", None)
            if _cap_task is not None:
                try:
                    caption = await asyncio.wait_for(
                        _cap_task, timeout=self._deep_timeout() + 1.0,
                    )
                    caption = str(caption or "").strip()
                    result["caption_source"] = "prefetch"
                except asyncio.TimeoutError:
                    try:
                        _cap_task.cancel()
                    except Exception:
                        pass
                    result["caption_source"] = "timeout"
                except Exception:
                    result["caption_source"] = "error"
            # P1.5+D3：质量评估——prefetch 用 _generic prompt 覆盖率高但质量略低；
            # 当 caption 缺失 / 太短 / 含 NO_IMAGE 占位 时，用 peer_msg.kind 同步重调
            need_resync = (
                not caption
                or len(caption) < 25
                or any(
                    p in caption.lower()
                    for p in ("no_image", "无图片", "看不清的图片")
                )
            )
            if need_resync:
                resync_caption = await self._try_describe_peer_image(
                    result.get("screenshot_path", ""),
                    timeout_sec=self._deep_timeout(),
                    kind=peer_msg.kind,
                )
                if resync_caption:
                    caption = resync_caption
                    result["caption_source"] = (
                        "resync_after_prefetch" if not need_resync
                        else "sync"
                    )
                    result.setdefault("hints", []).append(
                        f"caption_resync_kind:{peer_msg.kind}"
                    )

            # P1-1.4：sticker 类标签（让 LLM 看到对方情绪：happy/love/sad/angry/cute）
            sticker_cat = None
            if peer_msg.kind in ("sticker", "animated_sticker"):
                try:
                    sticker_cat = self._classify_sticker_category(
                        peer_msg.desc or "", peer_msg.content or "",
                    )
                except Exception:
                    sticker_cat = None

            # P1-A2：上下文 fusion hint —— 上一句 peer 文本（本轮 extra_peers / 上一轮 state）
            fusion_hint = self._build_media_fusion_hint(
                chat_key, peer_msg, extra=result.get("extra_peers") or [],
            )

            # 用 to_text_for_ai 的增强签名拿一致输出
            text_for_ai = peer_msg.to_text_for_ai(
                caption=caption or None,
                sticker_category=sticker_cat,
                fusion_hint=fusion_hint,
            )
            if caption:
                result["image_caption"] = caption  # 兼容旧 approval extra
                result["media_caption"] = caption
            if sticker_cat:
                result["sticker_category"] = sticker_cat
            if fusion_hint:
                result["media_fusion_hint"] = fusion_hint[:120]
            logger.info(
                "[messenger_rpa] P1-media chat=%s kind=%s src=%s "
                "caption=%r sticker_cat=%s fusion=%r",
                chat_key, peer_msg.kind, result.get("caption_source", "?"),
                (caption or "")[:80], sticker_cat or "-",
                (fusion_hint or "")[:60],
            )
            result.setdefault("hints", []).append(
                f"media_enriched:{peer_msg.kind}"
                f"{':cap' if caption else ''}"
                f"{':sticker_cat=' + sticker_cat if sticker_cat else ''}"
                f"{':fusion' if fusion_hint else ''}"
            )
        else:
            # 非媒体：把预跑的 caption_task cancel 掉（token 已花，但本次不等）
            _cap_task = result.pop("_cap_task", None)
            if _cap_task is not None:
                try:
                    _cap_task.cancel()
                except Exception:
                    pass

        # ★ P2-2：对方连发合并（extra_peers 由 _thread_combined 写入 result）
        # 当 vision 抓到 peer 底部之上还有连续 peer 气泡时，把它们按
        # "[连发] (1)... (2)... (3) 当前..." 的格式合并为一条，AI 会看到完整上下文。
        extra = result.get("extra_peers") or []
        if isinstance(extra, list) and extra:
            parts: list = []
            ai_for_extra_lang = getattr(self._sm, "ai_client", None)
            current_extra_lang = _detect_peer_lang(
                peer_msg.content or text_for_ai,
                ai_client=ai_for_extra_lang,
            )
            # extra 是近→远顺序，反转成 远→近（1..N-1），最后追加当前 peer
            for pm_d in list(reversed(extra))[-3:]:
                ek = str(pm_d.get("kind") or "text")
                ec = str(pm_d.get("content") or "").strip()
                ed = str(pm_d.get("desc") or "").strip()
                if ek == "text" and ec:
                    extra_lang = _detect_peer_lang(ec, ai_client=ai_for_extra_lang)
                    if (
                        current_extra_lang not in ("", "unknown")
                        and extra_lang not in ("", "unknown")
                        and extra_lang != current_extra_lang
                    ):
                        result.setdefault("dropped_extra_peers", []).append(
                            {
                                "reason": "language_mismatch",
                                "current_lang": current_extra_lang,
                                "extra_lang": extra_lang,
                                "preview": ec[:80],
                            }
                        )
                        continue
                    parts.append(ec)
                elif ek == "link":
                    parts.append(f"[链接] {ed} {ec}".strip())
                elif ek == "image":
                    parts.append(f"[图片] {ed}".strip())
                elif ek in ("sticker", "voice", "file"):
                    parts.append(f"[{ek}] {ed}".strip())
                else:
                    parts.append(f"[{ek}] {ed or ec}".strip())
            parts.append(text_for_ai)
            numbered = "\n".join(
                f"({i+1}) {p}" for i, p in enumerate(parts) if p
            )
            text_for_ai = f"[对方连发]\n{numbered}"
            result["multi_peer_count"] = len(parts)
            logger.info(
                "[messenger_rpa] P2-2 multi-peer merged count=%d chat=%s",
                len(parts), chat_key,
            )

        # SkillManager.process_message(text, user_id, context) — 与 line_rpa / FB webhook 对齐
        # chat_id 必须是 int（SkillManager 内 int(context["chat_id"])），字符串会崩
        cid_num = int(hashlib.md5(chat_key.encode("utf-8")).hexdigest()[:12], 16) % (10**9)
        reply_profile = self._pick_reply_profile(chat_key, target.name or "")
        _lang_single = (
            peer_msg.raw or peer_msg.to_text_for_ai()
            if peer_msg.kind == "text"
            else ""
        )
        _reply_lang_ctx = self._resolve_reply_lang(
            peer_msg=peer_msg,
            text_for_ai=text_for_ai,
            chat_key=chat_key,
            profile=reply_profile,
        )
        result["detected_reply_lang"] = _reply_lang_ctx
        if reply_profile:
            result["reply_profile"] = str(
                reply_profile.get("id") or reply_profile.get("name") or ""
            )
        ctx: Dict[str, Any] = {
            "chat_id": cid_num,
            "request_id": f"mrpa-{uuid.uuid4().hex[:12]}",
            "channel": "messenger_rpa",
            "reply_lang": _reply_lang_ctx,
            "reply_lang_locked": True,
            "chat_title": target.name or "Messenger Friend",
            "messenger_rpa_chat_key": chat_key,
            "messenger_rpa_peer_kind": peer_msg.kind,
            "messenger_rpa_peer_raw": (peer_msg.raw or "")[:300],
            "_current_user_message_for_lang": _lang_single[:200],
        }
        # P3-deep bugfix + B 防御性 (2026-05-04)：所有 cfg-driven ctx flag
        # 都用 **explicit override** —— _context_store.user_context 会持久化
        # 旧值，"only set if True" pattern 让 cfg 改 false 后无法覆盖
        # 历史持久化的 True，导致功能"被永久关闭"。
        ctx["suppress_global_ai_identity"] = bool(
            self._cfg.get("suppress_global_ai_identity", True)
        )
        ctx["disable_episodic_memory"] = bool(
            self._cfg.get("disable_episodic_memory", True)
        )
        # P3-deep (2026-05-04)：episodic_memory_extract 用 chat_id 算 storage_key；
        # messenger_rpa 之前没传 chat_id，导致 storage_key 退化路径异常 →
        # silent return 不写入。把 chat_key 当 chat_id 传入。
        if chat_key and "chat_id" not in ctx:
            ctx["chat_id"] = chat_key
        lead_prompt_block = ""
        try:
            if self._lead_qualifier.enabled:
                cs_lq = getattr(self._sm, "_context_store", None)
                uctx_lq = cs_lq.get(chat_key) if cs_lq is not None else {}
                decision = self._lead_qualifier.evaluate(
                    uctx_lq.get("lead_qualification") if isinstance(uctx_lq, dict) else {},
                    peer_text=text_for_ai,
                    reply_lang=_reply_lang_ctx,
                    chat_name=target.name or "",
                )
                if cs_lq is not None:
                    uctx_lq["lead_qualification"] = decision.profile
                    cs_lq.mark_dirty(chat_key)
                if decision.result is not None:
                    result["lead_qualification"] = decision.result
                lead_prompt_block = decision.prompt_block
                if decision.action == "silent_stop":
                    result.setdefault("hints", []).append("lead_qualification:silent_stop")
                    return ""
                if decision.action == "handoff_line" and decision.forced_reply:
                    result.setdefault("hints", []).append("lead_qualification:line_handoff")
                    return decision.forced_reply
        except Exception:
            logger.debug("[messenger_rpa] lead qualification failed", exc_info=True)
        # Persist this decision into ContextStore so media/sticker-only followups
        # can inherit the user's last natural language.
        try:
            cs_lang = getattr(self._sm, "_context_store", None)
            if cs_lang is not None and _reply_lang_ctx:
                uctx_lang = cs_lang.get(chat_key)
                uctx_lang["reply_lang"] = _reply_lang_ctx
                cs_lang.mark_dirty(chat_key)
        except Exception:
            logger.debug("[messenger_rpa] reply_lang persist failed", exc_info=True)

        # Messenger multi-persona: bind runtime persona to this synthetic chat_id.
        try:
            persona_data = reply_profile.get("persona") if reply_profile else None
            from src.utils.persona_manager import PersonaManager

            pm = PersonaManager.get_instance()
            if isinstance(persona_data, dict) and persona_data:
                pm.bind_chat_persona(str(cid_num), persona_data)
            else:
                pm.unbind_chat_persona(str(cid_num))
        except Exception:
            logger.debug("[messenger_rpa] reply_profile persona bind failed", exc_info=True)

        # ★ P1-3：Messenger 专属 style_hint（可在 config 覆盖 LINE 默认人设）
        _style_hint = str(self._cfg.get("style_hint") or "").strip()
        _profile_hint = ""
        if reply_profile:
            _profile_hint = str(reply_profile.get("style_hint") or "").strip()
        _lang_name = ""
        try:
            ai_for_name = getattr(self._sm, "ai_client", None)
            _lang_name = getattr(ai_for_name, "_LANG_NAMES", {}).get(
                _reply_lang_ctx, _reply_lang_ctx
            )
        except Exception:
            _lang_name = _reply_lang_ctx
        _lang_lock = (
            f"【Messenger 语言锁定】本轮检测/继承的用户语言是 {_lang_name}。"
            f"必须全程使用 {_lang_name} 回复；不要因为中文知识库、中文人设或本地标签而切回中文。"
        )
        style_parts = [_lang_lock]
        if _style_hint:
            style_parts.append(_style_hint)
        if _profile_hint:
            style_parts.append(_profile_hint)
        if lead_prompt_block:
            style_parts.append(lead_prompt_block)
        _style_hint = "\n".join(p for p in style_parts if p)
        if _style_hint:
            ctx["messenger_rpa_style_hint"] = _style_hint

        # ★ P5-4：对话分级路由 — 按 credit + money_mention 定档 → 写入 ctx
        # ai_client 会据此路由到不同 model / temperature
        try:
            tier = self._classify_ai_tier(chat_key, text_for_ai, result)
            if tier:
                ctx["ai_tier"] = tier
                result["ai_tier"] = tier
                # P6-3：_enqueue_approval_wrapped 会从这里拿 tier 自动注入
                self._last_ai_tier = tier
        except Exception:
            logger.debug("P5-4 classify_ai_tier 异常", exc_info=True)

        # ★ P6-4：把 account_id 传给 ai_client，便于按账号聚合 tokens/cost
        try:
            _aid = getattr(self, "_account_id", "") or "default"
            if _aid:
                ctx["account_id"] = str(_aid)
        except Exception:
            pass

        # ★ P2-3：A/B persona 实验 — 按 chat_key sticky 分配 variant，
        # 若命中则用 variant.style_hint 覆盖全局 style_hint
        variant_name, variant_hint = self._pick_persona_variant(chat_key)
        if variant_name:
            ctx["messenger_rpa_variant"] = variant_name
            if variant_hint:
                ctx["messenger_rpa_style_hint"] = variant_hint
            result["variant"] = variant_name
            logger.debug(
                "[messenger_rpa] P2-3 variant=%s chat_key=%s", variant_name, chat_key,
            )

        # ★ P7-4：长期记忆注入 — 把 _long_term_memory.facts 以 bullet 形式前置到
        # style_hint，确保 AI 每次都能看到稳定事实（姓名/地区/长期偏好/承诺）。
        try:
            cs_peek = getattr(self._sm, "_context_store", None)
            if cs_peek is not None:
                uctx_peek = cs_peek.get(chat_key)
                ltm = uctx_peek.get("_long_term_memory") or {}
                facts = ltm.get("facts") or []
                if isinstance(facts, list) and facts:
                    # 前 12 条，避免 prompt 过长
                    bullets = "\n".join(
                        f"- {str(f)[:80]}" for f in facts[:12]
                    )
                    prefix = (
                        "【长期记忆】这位客户已知的稳定信息（优先参考，勿与之矛盾）：\n"
                        f"{bullets}\n"
                    )
                    cur_hint = str(ctx.get("messenger_rpa_style_hint") or "")
                    ctx["messenger_rpa_style_hint"] = prefix + cur_hint
                    result["ltm_facts"] = len(facts)
        except Exception:
            logger.debug("P7-4 ltm inject 异常", exc_info=True)

        persona_facts_for_state: List[str] = []
        conv_state_for_reply: Dict[str, Any] = {}
        try:
            persona_data_for_state = (
                reply_profile.get("persona") if reply_profile else None
            )
            persona_facts_for_state = flatten_persona_facts(
                persona_data_for_state
                if isinstance(persona_data_for_state, dict) else {}
            )
            fsm = getattr(self, "_conversation_fsm", None)
            if fsm is None:
                fsm = ConversationStateMachine()
                self._conversation_fsm = fsm
            prev_state = self._state.get_conversation_state(chat_key)
            customer_lang = (
                _reply_lang_ctx or prev_state.get("customer_language") or
                detect_customer_language(text_for_ai)
            )
            customer_type = (
                prev_state.get("customer_type") or infer_customer_type(text_for_ai)
            )
            conv_state_for_reply = fsm.advance(
                prev_state,
                peer_text=text_for_ai,
                customer_language=customer_lang,
                customer_type=customer_type,
                persona_facts=persona_facts_for_state,
            )
            persona_id_for_state = str(
                (reply_profile or {}).get("id")
                or (reply_profile or {}).get("name")
                or ""
            )
            self._state.update_conversation_state(
                chat_key,
                chat_key=chat_key,
                account_id=str(getattr(self, "_account_id", "") or "default"),
                persona_id=persona_id_for_state,
                customer_language=str(conv_state_for_reply.get("customer_language") or ""),
                customer_type=str(conv_state_for_reply.get("customer_type") or ""),
                stage=str(conv_state_for_reply.get("stage") or "new_lead"),
                memory_summary=str(conv_state_for_reply.get("memory_summary") or ""),
                recent_topics=list(conv_state_for_reply.get("recent_topics") or []),
                used_persona_facts=list(
                    conv_state_for_reply.get("used_persona_facts") or []
                ),
                metadata={
                    "chat_name": target.name or "",
                    "previous_stage": conv_state_for_reply.get("previous_stage", ""),
                },
                last_message_at=float(
                    conv_state_for_reply.get("last_message_at") or time.time()
                ),
            )
            block = fsm.prompt_block(conv_state_for_reply)
            if block:
                cur_hint = str(ctx.get("messenger_rpa_style_hint") or "")
                ctx["messenger_rpa_style_hint"] = (
                    cur_hint + "\n" + block if cur_hint else block
                )
                result["conversation_stage"] = conv_state_for_reply.get("stage", "")
        except Exception:
            logger.debug("[messenger_rpa] conversation state machine failed", exc_info=True)

        # W4-Runner：ContactHooks 入库 inbound 消息（失败不影响 runner）
        # Phase 1：接住 JourneyContext → contact_id + 已有 snapshot 渲染成 portrait block 塞 ctx
        hooks = self._contact_hooks
        journey_ctx_for_portrait = None
        if hooks is not None:
            try:
                journey_ctx_for_portrait = hooks.on_message(
                    channel="messenger",
                    account_id=str(getattr(self, "_account_id", "") or "default"),
                    external_id=target.name or "",
                    direction="in",
                    text_preview=(text_for_ai or "")[:120],
                    display_name=target.name or "",
                    trace_id=ctx.get("request_id", ""),
                )
            except Exception:
                logger.debug("contact_hooks on_message(in) 异常", exc_info=True)

        if journey_ctx_for_portrait is not None:
            try:
                _contact = getattr(journey_ctx_for_portrait, "contact", None)
                _journey = getattr(journey_ctx_for_portrait, "journey", None)
                if _contact is not None:
                    ctx["contact_id"] = str(getattr(_contact, "contact_id", "") or "")
                if _journey is not None:
                    snap_json = str(getattr(_journey, "context_snapshot_json", "") or "")
                    # ★ 跨账号画像共享：本账号无画像时，从 coordinator 取其他账号的
                    if not snap_json and self._coordinator is not None:
                        _shared = self._coordinator.get_portrait(
                            target.name or ""
                        )
                        if _shared:
                            snap_json = _shared
                            result.setdefault("hints", []).append(
                                "portrait_from_coordinator"
                            )
                    # ★ 反向同步：本账号有画像时，推给 coordinator 让其他账号共享
                    if snap_json and self._coordinator is not None:
                        _snap_ts = float(
                            getattr(_journey, "snapshot_refreshed_at", 0) or 0
                        ) or time.time()
                        self._coordinator.update_portrait(
                            target.name or "",
                            str(getattr(self, "_account_id", "") or "default"),
                            snap_json,
                            _snap_ts,
                        )
                    if snap_json:
                        from src.contacts.portrait_extractor import render_block
                        block = render_block(snap_json)
                        if block:
                            ctx["_contact_portrait_block"] = block
            except Exception:
                logger.debug("[messenger_rpa] portrait inject 异常", exc_info=True)

            # ★ Phase 1：异步触发画像 refresh（不阻塞主回复路径）
            if self._portrait_extractor is not None:
                try:
                    _j = getattr(journey_ctx_for_portrait, "journey", None)
                    _c = getattr(journey_ctx_for_portrait, "contact", None)
                    _disp = (
                        getattr(_c, "primary_name", "") or target.name or ""
                    ) if _c is not None else (target.name or "")
                    if _j is not None:
                        asyncio.create_task(
                            self._maybe_refresh_portrait_bg(_j, _disp)
                        )
                except Exception:
                    logger.debug(
                        "[messenger_rpa] portrait refresh schedule 异常",
                        exc_info=True,
                    )

        # ★ W2-D1.6：guardrail 输入侧 — 危机/未成年/AI身份问询 → 直接走话术不调 LLM
        if self._guardrail is not None and self._guardrail.enabled:
            try:
                in_lang = ctx.get("reply_lang") or "zh"
                in_action = self._guardrail.check_input(text_for_ai, lang=in_lang)
                if in_action.kind.value == "force_reply" and in_action.forced_reply:
                    result["guardrail_in"] = in_action.category.value
                    result["guardrail_alert"] = in_action.alert_admin
                    if in_action.alert_admin:
                        # ★ W2-D2.3：危机事件三冗余记录（jsonl + ERROR + telegram）
                        self._record_crisis_event(
                            category=in_action.category.value,
                            chat_key=chat_key,
                            chat_name=target.name or "",
                            peer_text=text_for_ai,
                            detail=in_action.detail,
                        )
                    else:
                        logger.info(
                            "[messenger_rpa] guardrail input %s chat=%s → forced_reply",
                            in_action.category.value, chat_key,
                        )
                    return in_action.forced_reply
            except Exception:
                logger.debug("guardrail check_input 异常", exc_info=True)

        # ★ P3-4 + W2-D1.6：LLM 耗时计时 + guardrail 输出侧 regen 循环
        _t_llm = time.monotonic()
        out_lang = ctx.get("reply_lang") or "zh"
        max_regen = 2
        payload = None
        attempt = 0
        for attempt in range(1, 1 + max_regen + 1):  # 最多 1 + 2 = 3 次
            try:
                payload = await self._sm.process_message(
                    text_for_ai,
                    chat_key,
                    context=ctx,
                )
            except Exception as ex:
                result["error"] = f"skill_error:{type(ex).__name__}:{ex}"
                return ""
            # 抽出 reply 用于 guardrail check（payload 解析与下方一致）
            if isinstance(payload, dict):
                _peek_reply = str(payload.get("reply") or payload.get("text") or "").strip()
            else:
                _peek_reply = str(payload or "").strip()
            if not _peek_reply or self._guardrail is None or not self._guardrail.enabled:
                break
            try:
                out_action = self._guardrail.check_output(
                    _peek_reply, lang=out_lang,
                    attempt=attempt, max_regen=max_regen,
                )
            except Exception:
                logger.debug("guardrail check_output 异常", exc_info=True)
                break
            if out_action.kind.value == "pass":
                break
            if out_action.kind.value == "regenerate":
                # 给 LLM 一个明确提示：上次输出违规，要换一种说法
                ctx = dict(ctx)  # 浅拷避免污染外部
                _existing_anti = ctx.get("_anti_repeat_hint") or ""
                ctx["_anti_repeat_hint"] = (
                    (_existing_anti + " " if _existing_anti else "")
                    + "请不要在回复中提及自己是 AI / 助手 / 模型 / chatbot 等任何"
                    + "暴露身份的内容；用普通朋友的口吻说话即可。"
                )
                logger.info(
                    "[messenger_rpa] guardrail regen attempt=%d cat=%s",
                    attempt, out_action.category.value,
                )
                continue  # 重新调一次 LLM
            if out_action.kind.value == "force_reply":
                # 用兜底替换；后续 language_alignment 仍会跑（无害）
                payload = {"reply": out_action.forced_reply}
                result["guardrail_out"] = f"force:{out_action.category.value}"
                break
            if out_action.kind.value == "block":
                result["guardrail_out"] = f"block:{out_action.category.value}"
                logger.warning(
                    "[messenger_rpa] guardrail block 输出 chat=%s detail=%s",
                    chat_key, out_action.detail,
                )
                return ""
        result["guardrail_attempts"] = attempt
        result.setdefault("phase_ms", {})["llm"] = int(
            (time.monotonic() - _t_llm) * 1000
        )

        # ★ P1-1：强制把 _conversation_history / last_reply 落盘，避免进程崩溃丢 5s 对话窗口
        # SkillManager 默认 % 5 秒才 flush，对 messenger 这种低频长对话太危险
        try:
            cs = getattr(self._sm, "_context_store", None)
            if cs is not None:
                cs.mark_dirty(chat_key)
                cs.flush(chat_key)
                # 记录当前历史长度到 result，供诊断
                try:
                    uctx = cs.get(chat_key)
                    _hist = uctx.get("_conversation_history") or []
                    result["conv_hist_turns"] = len(_hist) // 2
                    _summ = uctx.get("_conversation_summary") or ""
                    if _summ:
                        result["conv_summary_len"] = len(_summ)
                except Exception:
                    pass
        except Exception:
            logger.debug("conversation flush 失败", exc_info=True)

        if isinstance(payload, dict):
            reply = str(payload.get("reply") or payload.get("text") or "").strip()
        else:
            reply = str(payload or "").strip()

        # ── 中日混语过滤（P-LANG-MIX）────────────────────────────────────
        # 当 reply_lang=ja 且 AI 输出含"纯中文句（无假名）"时，
        # 将这些中文句从回复中剥离；保留所有含假名的日文句。
        # 触发条件：_has_chinese_japanese_mixing 检测阳性。
        # 目的：彻底消除语音消息里"一句中文一句日语"的来回混杂。
        if _reply_lang_ctx == "ja" and reply:
            try:
                from src.ai.ai_client import AIClient
                if AIClient._has_chinese_japanese_mixing(reply):
                    _clean_sents = []
                    for _s in re.split(r"([\u3002\uff01\uff1f!?\n])", reply):
                        _kana = len(re.findall(r"[\u3040-\u309F\u30A0-\u30FF]", _s))
                        _cjk = len(re.findall(r"[\u4e00-\u9fff]", _s))
                        if _cjk >= 4 and _kana == 0 and len(_s.strip()) >= 4:
                            result.setdefault("hints", []).append(
                                f"lang_mix_filter:removed_zh_sent:{_s.strip()[:40]!r}"
                            )
                            continue
                        _clean_sents.append(_s)
                    _cleaned = "".join(_clean_sents).strip()
                    if _cleaned:
                        reply = _cleaned
                        result.setdefault("hints", []).append("lang_mix_filter:applied_ja")
            except Exception:
                pass

        # ★ 智能语言对齐（取代旧 force_english_reply 一刀切）
        # 3 种模式：
        #   off                   不翻译
        #   english_fallback_only 只要回复含非 ASCII 就翻译成英文（设备兜底）
        #   auto                  仅当 peer 说英文、AI 回中文时翻成英文；
        #                         peer 说中文时保留中文（交给 AdbKeyboard/审批）
        # 旧 force_english_reply=true 等价于 english_fallback_only
        mode = str(
            self._cfg.get("language_alignment")
            or ("english_fallback_only"
                if bool(self._cfg.get("force_english_reply", False))
                else "off")
        ).strip().lower()

        if reply and mode != "off" and not reply.isascii():
            ai = getattr(self._sm, "ai_client", None)
            peer_lang = (
                str(_reply_lang_ctx or "").strip()
                or _detect_peer_lang(peer_msg.raw or "", ai_client=ai)
            )
            need_translate = False
            if mode == "english_fallback_only":
                need_translate = True
            elif mode == "auto":
                # 只有 peer 说英文时才把 AI 的非 ASCII 回复翻成英文；
                # 其他语言（ja/ko/ar/...）保留原回复，由 LLM 系统提示已要求按对方语言回
                need_translate = (peer_lang == "en")
            if need_translate:
                try:
                    eng = await self._translate_to_english(reply, peer_msg)
                    if eng:
                        result.setdefault("hints", []).append(
                            f"lang_align({mode},peer={peer_lang}): "
                            f"non_ascii_len={len(reply)} → en_len={len(eng)}"
                        )
                        reply = eng
                    else:
                        result.setdefault("hints", []).append(
                            f"lang_align({mode}): translate empty, keep original"
                        )
                except Exception as ex:
                    logger.warning(
                        "[messenger_rpa] language_alignment 翻译失败：%s:%s",
                        type(ex).__name__, ex,
                    )
            else:
                result.setdefault("hints", []).append(
                    f"lang_align({mode},peer={peer_lang}): keep original"
                )

        # ★ 回复长度上限（companion 模式防止 AI 生成超长段落）
        _max_chars = int(self._cfg.get("companion_max_reply_chars", 0) or 0)
        if _max_chars > 0 and len(reply) > _max_chars:
            trimmed = reply[:_max_chars]
            # 尝试在句尾截断，避免截在句子中间
            for sep in ("。", "！", "？", "!", "?", ".", "\n"):
                idx = trimmed.rfind(sep)
                if idx > _max_chars // 2:
                    trimmed = trimmed[:idx + 1]
                    break
            result.setdefault("hints", []).append(
                f"reply_trimmed:{len(reply)}->{len(trimmed)}"
            )
            reply = trimmed

        try:
            if reply and conv_state_for_reply:
                fsm = getattr(self, "_conversation_fsm", None) or ConversationStateMachine()
                updated_conv = fsm.mark_used_facts(
                    conv_state_for_reply, reply, persona_facts_for_state,
                )
                self._state.update_conversation_state(
                    chat_key,
                    chat_key=chat_key,
                    account_id=str(getattr(self, "_account_id", "") or "default"),
                    persona_id=str(
                        (reply_profile or {}).get("id")
                        or (reply_profile or {}).get("name")
                        or ""
                    ),
                    customer_language=str(updated_conv.get("customer_language") or ""),
                    customer_type=str(updated_conv.get("customer_type") or ""),
                    stage=str(updated_conv.get("stage") or "new_lead"),
                    memory_summary=str(updated_conv.get("memory_summary") or ""),
                    recent_topics=list(updated_conv.get("recent_topics") or []),
                    used_persona_facts=list(
                        updated_conv.get("used_persona_facts") or []
                    ),
                    metadata={
                        "chat_name": target.name or "",
                        "last_reply_len": len(reply),
                    },
                    last_message_at=float(
                        updated_conv.get("last_message_at") or time.time()
                    ),
                )
        except Exception:
            logger.debug("[messenger_rpa] conversation state post-reply update failed", exc_info=True)

        # P1-B：post-validation regen 防跳话题
        # LLM 偶尔生成与 peer 上一条问题无关的回复（"你看了什么剧"→AI 回"开车"
        # 这种幻觉）。在发送前用一次轻量 LLM 调用判断是否承接，不承接则 regen。
        try:
            reply = await self._maybe_regen_for_context(
                reply, peer_msg, chat_key, ctx, result,
            )
        except Exception:
            logger.debug("[messenger_rpa] context regen failed", exc_info=True)

        # ── P2 + P2++G_FIX (2026-05-04) sticker modality 决策（统一 helper）──
        # 在 reply decided log 之前调，让 modality 决定结果进 hint，dashboard
        # 即可统计概率命中分布、cooldown 频率、各 cat 选中比例。
        # 注意：fast-ack 路径在 _maybe_media_ack 命中后**也**会调同一个 helper，
        # 让 sticker / voice / image 的快速回应也能附 sticker（P2++G_FIX 修盲区）。
        # serial 由上层 cycle 写入 result["_serial"]——dry_run 阶段不会真发，
        # 空 serial 也无害；real_send 必须保证 caller 已设。
        await self._run_modality_hook(
            serial=str(result.get("_serial") or ""),
            target=target,
            chat_key=chat_key,
            peer_msg=peer_msg,
            reply_text=reply or "",
            multi_peer_count=int(result.get("multi_peer_count") or 0),
            peer_sticker_cat=result.get("sticker_category"),
            result=result,
            caption=result.get("media_caption") or result.get("image_caption"),
        )

        # P0-B：回复决策末段日志 — 让运营能在 web 改完人设/参数后立刻在日志
        # 里看到"哪个 chat 用了什么 persona、生成的回复长什么样"，避免"改了
        # 没反应"的盲区。
        try:
            _picked = result.get("reply_profile") or ""
            _hist_n = result.get("conv_hist_turns")
            _multi_n = result.get("multi_peer_count")
            # P7 监控修复：原 [:120] 截断让 thread_title_vision_cache_hit
            # 这种关键 hint 看不见。bump 到 300 字符覆盖典型 hint 列表全貌。
            _hint_list = result.get("hints") or []
            _hints = ",".join(_hint_list)[:300]
            _self_ovlp = result.get("self_reply_overlap")
            _ovlp_n = result.get("self_overlap_against_n_replies")
            logger.warning(
                "[messenger_rpa] reply decided chat=%s persona=%r hist_turns=%s "
                "multi_peer=%s reply_len=%d self_overlap=%s/n=%s preview=%r hints=%s",
                chat_key, _picked, _hist_n, _multi_n, len(reply or ""),
                _self_ovlp, _ovlp_n,
                (reply or "")[:80], _hints,
            )
            # P11 (2026-05-04)：hints 自动入 metrics 计数器，dashboard/Prometheus
            # 即可看到趋势（如 self_overlap promote 率 / cache hit 率退化告警）。
            try:
                from src.monitoring.metrics_store import get_metrics_store
                _ms = get_metrics_store()
                for _h in _hint_list:
                    # 取冒号前 base name（如 cycle_entry_thread_recovered:Yunshan Zan
                    # → cycle_entry_thread_recovered），动态片段不爆字典
                    _base = (_h or "").split(":", 1)[0].strip()
                    if _base:
                        _ms.record_messenger_rpa_metric(_base)
            except Exception:
                pass
        except Exception:
            logger.debug("[messenger_rpa] reply decided log failed", exc_info=True)

        # ★ P0-I (2026-05-03 监控发现): 双零信号 + 长 reply 守卫
        # 当 hist_turns=0 (无历史) + multi_peer=None (vision 仅看到单 peer) +
        # reply 长 > 30 字 时，LLM 处于"上下文双零 + 长篇生成"状态 → 大概率
        # hallucinate（监控见过 19:04 晚 7 点说"早上好"、20:11 跳话题、21:28
        # 编"买东西"等）。短 reply（"好的"/"嗯"等）不拦避免误伤。
        # 默认 enabled=True，可通过 reply_min_signal_guard.enabled=false 关闭。
        try:
            _msg = (self._cfg.get("reply_min_signal_guard") or {})
            if reply and bool(_msg.get("enabled", True)):
                _hist_now = result.get("conv_hist_turns")
                _multi_now = result.get("multi_peer_count")
                _long_thr = int(_msg.get("long_reply_threshold", 30) or 30)
                if (
                    (_hist_now is None or _hist_now == 0)
                    and _multi_now is None
                    and len(reply) > _long_thr
                ):
                    result.setdefault("hints", []).append(
                        "p0i_min_signal_skip"
                    )
                    logger.warning(
                        "[messenger_rpa] P0-I min_signal skip: chat=%s "
                        "hist=0 multi_peer=None reply_len=%d (>%d) "
                        "preview=%r — 双零信号 + 长 reply 视为 hallucinate，"
                        "abort send",
                        chat_key, len(reply), _long_thr, reply[:80],
                    )
                    return ""
        except Exception:
            logger.debug("[messenger_rpa] P0-I guard failed", exc_info=True)

        return reply

    async def _maybe_regen_for_context(
        self,
        reply: str,
        peer_msg: "PeerMessage",
        chat_key: str,
        ctx: Dict[str, Any],
        result: Dict[str, Any],
    ) -> str:
        """P1-B post-validation regen：检查 reply 是否承接 peer 上一条问题。
        不承接 → 用一次 regen 强制承接。
        触发条件（避免无谓花钱）：
          - humanize.context_check.enabled: true
          - reply 长度 ≥ min_reply_chars（短回复不太可能跳话题）
          - peer_msg 是文字问句
        """
        cfg = self._cfg.get("humanize") or {}
        cc_cfg = cfg.get("context_check") or {}
        if not cc_cfg.get("enabled", False):
            return reply
        if not reply:
            return reply
        peer_text = (peer_msg.content or "").strip()
        if not peer_text or len(reply) < int(cc_cfg.get("min_reply_chars", 20) or 20):
            return reply
        # 跳过非文字 peer 消息（语音/图片/系统事件）
        if peer_msg.kind not in ("text", "link"):
            return reply

        ai = getattr(self._sm, "ai_client", None)
        if ai is None or not hasattr(ai, "chat"):
            return reply

        # 1. 用 ai_client.chat 做轻量 yes/no validation
        validation_prompt = (
            "判断以下「对话回复」是否直接承接了「对方的最后一句」。\n"
            "只回答 yes 或 no，不要解释。\n\n"
            f"[对方的最后一句]: {peer_text[:300]}\n"
            f"[我方回复]: {reply[:300]}\n\n"
            "我方回复是否直接承接了对方的话题或问题？(yes / no):"
        )
        try:
            timeout_s = float(cc_cfg.get("validation_timeout_sec", 5.0) or 5.0)
            rv = await asyncio.wait_for(
                ai.chat(validation_prompt), timeout=timeout_s,
            )
            rv_text = (rv or "").strip().lower()
        except (asyncio.TimeoutError, Exception):
            result.setdefault("hints", []).append("context_check_timeout")
            return reply

        # yes 路径：直接放行
        if "yes" in rv_text and "no" not in rv_text:
            result.setdefault("hints", []).append("context_check_pass")
            return reply

        # no 路径：regen 一次
        result.setdefault("hints", []).append("context_check_regen")
        logger.warning(
            "[messenger_rpa] ⚠ context_check 未承接，regen 一次 chat=%s "
            "peer=%r reply=%r",
            chat_key, peer_text[:60], reply[:60],
        )
        try:
            # 在原 peer text 前面注入强约束指令
            forced_text = (
                f"⚠️ 必须先正面回应「{peer_text[:120]}」这条话题，"
                f"再扩展或反问。绝不要跳到完全不相关的话题。\n\n"
                f"对方的话: {peer_text}"
            )
            regen_ctx = dict(ctx)
            regen_ctx["context_regen"] = True
            new_payload = await self._sm.process_message(
                forced_text,
                chat_key,
                context=regen_ctx,
            )
            new_reply = ""
            if isinstance(new_payload, dict):
                new_reply = str(
                    new_payload.get("reply") or new_payload.get("text") or ""
                ).strip()
            elif new_payload:
                new_reply = str(new_payload).strip()
            if new_reply:
                logger.warning(
                    "[messenger_rpa] context regen ok new_reply=%r",
                    new_reply[:80],
                )
                return new_reply
        except Exception:
            logger.debug("[messenger_rpa] context regen exception", exc_info=True)
        return reply

    async def _translate_to_english(
        self, reply_zh: str, peer_msg: PeerMessage
    ) -> str:
        """用 ai_client.chat 把 reply 翻译成自然英文。失败返回空串。"""
        ai = getattr(self._sm, "ai_client", None)
        if ai is None or not hasattr(ai, "chat"):
            return ""
        peer_preview = (peer_msg.raw or "")[:200]
        prompt = (
            "You are translating a Messenger chat reply.\n"
            "Constraint: output MUST be plain ASCII English only "
            "(letters/digits/punctuation/standard emoji like :) are fine; "
            "NO Chinese characters, NO full-width punctuation, "
            "NO non-ASCII characters).\n"
            "Style: casual, natural, warm, first-person, match the tone of "
            "the original, keep it 1-2 short sentences.\n"
            "Do NOT add prefixes like 'Reply:' or 'Translation:'. Output the "
            "English reply directly.\n\n"
            f"[Peer just said] {peer_preview}\n"
            f"[Original reply to translate] {reply_zh}\n\n"
            "English reply:"
        )
        try:
            out = await ai.chat(prompt)
        except Exception:
            logger.debug("ai_client.chat 翻译抛异常", exc_info=True)
            return ""
        out = (out or "").strip().strip("`\"'")
        if not out:
            return ""
        if not out.isascii():
            out = out.encode("ascii", "ignore").decode("ascii").strip()
        return out[:1000]

    async def _thread_open_selfheal(
        self,
        serial: str,
        wh: Tuple[int, int],
        target: UnreadChat,
        thread_png: str,
        run_id: str,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """tap 行后若屏上仍是 Inbox 列表（多行头像清晰可见），判定 tap 偏了，
        清当前 calibration → pixel 再扫 → 重新 tap 一次 → 更新截图。

        判定"仍是 Inbox"**仅当 calibrate_inbox_rows 成功**（calib.ok）：
        - Inbox 列表满足首行 Y、行高等约束，calib.ok=True。
        - **会话 Thread 页**左侧往往有多条消息头像，raw_peaks 数量也常 ≥3，
          若误用「peaks_raw≥3」会把它当成 Inbox，导致无限清校准、永远进不了发消息阶段。
        """
        info: Dict[str, Any] = {"retried": False, "reason": ""}
        try:
            from src.integrations.messenger_rpa.auto_calibrate import (
                calibrate_inbox_rows,
            )
            calib = calibrate_inbox_rows(thread_png)
            if not calib.ok:
                # 已进入 Thread 或无法解析成标准 Inbox 栅格 → 不触发自愈
                info["reason"] = (
                    f"likely_thread_or_non_inbox:{getattr(calib, 'reason', '')} "
                    f"raw_peaks={len(calib.peaks_raw or [])}"
                )
                return info
        except Exception as ex:
            info["reason"] = f"calib_skip:{ex}"
            return info

        logger.warning(
            "[messenger_rpa] selfheal 检测到 tap 未进入 thread "
            "(peaks=%s), 清校准重扫 + 重 tap 行 %d",
            calib.peaks_raw, target.row_index,
        )
        info["pre_peaks"] = list(calib.peaks_raw or [])
        info["pre_calib_ok"] = calib.ok

        # 1) 清本机 calibration + 缓存
        try:
            from src.integrations.messenger_rpa.coord_calibrator import (
                _calib_file, InboxAnchors, save_calibration,
            )
            workspace = Path(self._cm.config_path).parent
            fp = _calib_file(workspace, serial)
            if fp.exists():
                fp.unlink()
                info["cleared_calib_file"] = True
            self._calib_cache.pop((serial, wh[0], wh[1]), None)

            # 2) 如果 calibrate_inbox_rows 给出了新的首行/行高，直接写新 calib
            if calib.ok:
                ry = float(wh[1]) / 1600.0
                anchors = InboxAnchors(
                    width=wh[0],
                    height=wh[1],
                    chat_row_first_y=int(round(calib.first_y * ry)),
                    chat_row_height=int(round(calib.row_height * ry)),
                    notes=(
                        f"selfheal_auto:prev_file_removed;"
                        f"rows={calib.visible_rows}"
                    ),
                )
                save_calibration(workspace, serial, anchors)
                info["new_calib"] = {
                    "first_y": anchors.chat_row_first_y,
                    "row_height": anchors.chat_row_height,
                }
        except Exception:
            logger.debug("selfheal 清/写 calib 失败", exc_info=True)

        # 3) 重新 tap + 截新图（只做一次，避免死循环）
        try:
            await asyncio.sleep(0.4)
            self._tap_chat_row(serial, wh, target)
            await asyncio.sleep(jitter_ms(900, 1600))
            new_png = await self._screenshot(
                serial, "thread_selfheal", run_id
            )
            if new_png:
                info["retried"] = True
                info["new_png"] = new_png
                result.setdefault("hints", []).append(
                    f"calib_selfhealed:row={target.row_index}"
                )
        except Exception as ex:
            info["reason"] = f"retry_tap_failed:{ex}"
        return info

    # ── P1-6：反封号发送门控 ──────────────────────
    def _classify_ai_tier(
        self, chat_key: str, text_for_ai: str, result: Dict[str, Any],
    ) -> Optional[str]:
        """★ P5-4：返回 tier 字符串（premium/normal/low）或 None 表示不启用。

        规则（可配置，默认启发式）：
          - credit < 40 → 'low'     （省钱，低信用 chat 不配强模型）
          - 文本含 $ / ¥ / 金额 / 价格 / 购买关键词 + credit >= 80 → 'premium'
          - 否则 'normal'
        """
        ai_cfg = (self._cfg.get("ai") or {}) or {}
        tiers_cfg = ai_cfg.get("tiers") or {}
        if not tiers_cfg.get("enabled", False):
            return None

        credit_val = 100
        try:
            cred = result.get("credit") or {}
            credit_val = int(cred.get("credit", 100))
        except Exception:
            pass
        # premium 关键词
        premium_keywords = tiers_cfg.get("premium_keywords") or [
            "$", "￥", "¥", "价格", "多少钱", "how much",
            "price", "buy", "purchase", "order", "付款", "付钱",
            "pay", "payment", "refund", "退款", "订单",
        ]
        low_keywords = tiers_cfg.get("low_keywords") or ["ok", "好的", "hi", "hello"]

        text_lower = (text_for_ai or "").lower()
        # low 优先级最高
        if credit_val < int(tiers_cfg.get("low_threshold", 40) or 40):
            return "low"
        # 非常短 + 低关键词：省 token
        if len(text_lower) <= 10 and any(kw in text_lower for kw in low_keywords):
            return "low"
        # premium
        if credit_val >= int(tiers_cfg.get("premium_threshold", 80) or 80):
            if any(str(kw).lower() in text_lower for kw in premium_keywords):
                return "premium"
        return "normal"

    def _resolve_relationship_stage(self, chat_key: str) -> str:
        """W2-D2.5：尝试从 SkillManager._context_store 拿当前 relationship_stage。

        失败一律回退 "warming"，不抛错。
        """
        try:
            cs = getattr(self._sm, "_context_store", None)
            if cs is None:
                return "warming"
            uctx = cs.get(chat_key)
            stage = str(uctx.get("relationship_stage") or "").strip()
            return stage or "warming"
        except Exception:
            return "warming"

    async def _maybe_pacing_defer(
        self,
        *,
        reply_text: str,
        peer_text: str,
        peer_kind: str,
        chat_key: str,
        chat_name: str,
        run_id: str,
        result: Dict[str, Any],
        fp: str,
        serial: Optional[str] = None,
        wh: Optional[Tuple[int, int]] = None,
    ) -> str:
        """W2-D2.5：根据 pacing 决策选择"短 await 后真发"或"defer 异步发"。

        ★ W2-D5.5b：短 pacing 期间并发跑 typing burst task，让对方看到"在打字"。
        Returns: "deferred"（已入 defer 队列，runner 该结束本轮）
                 或 "passthrough"（本地短 await 完成，继续真发链路）
        """
        try:
            from src.integrations.messenger_rpa.pacing import (
                calc_pacing_delay, short_send_threshold_sec,
            )
        except Exception:
            return "passthrough"

        pacing_cfg = self._cfg.get("pacing") or {}
        # ★ 测试模式：pacing 压缩到最多 10s，走 short-await 路径（不 defer）
        if bool(self._cfg.get("companion_test_mode", False)):
            pacing_cfg = dict(pacing_cfg)
            pacing_cfg["max_sec"] = min(float(pacing_cfg.get("max_sec", 30.0)), 10.0)
            pacing_cfg["short_send_threshold_sec"] = 10.0
        # 配置层关掉 pacing 也能直接放行（不延迟）
        if not bool(pacing_cfg.get("enabled", True)):
            return "passthrough"

        stage = self._resolve_relationship_stage(chat_key)
        pr = calc_pacing_delay(
            reply_text=reply_text,
            peer_text=peer_text,
            relationship_stage=stage,
            config=pacing_cfg,
        )
        result["pacing_delay_sec"] = round(pr.delay_sec, 2)
        result["pacing_reason"] = pr.reason
        # ★ W2-D6.3：pacing 实际延迟写入 metrics（avg/p95 用）
        try:
            from src.monitoring.metrics_store import get_metrics_store
            get_metrics_store().record_pacing_delay(pr.delay_sec)
        except Exception:
            pass
        short_th = short_send_threshold_sec(pacing_cfg)
        if pr.delay_sec <= short_th:
            # 短延迟：直接 await（不阻塞 _loop 但本 chat 卡几秒可接受）
            # ★ W2-D5.5b：并发跑 typing burst，让对方在等待中看到"在打字"
            burst_task = None
            if serial and wh and pr.delay_sec >= 1.5 \
                    and bool(self._cfg.get("typing_indicator_mode", "focus_only") != "off"):
                try:
                    burst_task = asyncio.create_task(
                        self._typing_indicator_burst(
                            serial, wh, duration_sec=pr.delay_sec,
                        ),
                        name="typing_burst_short_pacing",
                    )
                except Exception:
                    burst_task = None
            try:
                await asyncio.sleep(pr.delay_sec)
            except asyncio.CancelledError:
                if burst_task:
                    burst_task.cancel()
                raise
            except Exception:
                pass
            finally:
                if burst_task is not None:
                    burst_task.cancel()
                    try:
                        await burst_task
                    except (asyncio.CancelledError, Exception):
                        pass
            return "passthrough"
        # 长延迟：enqueue_deferred，独立 drain loop 异步发
        defer_until = time.time() + pr.delay_sec
        try:
            staleness = float(pacing_cfg.get("staleness_sec", 60.0) or 60.0)
            deferred_id = self._state.enqueue_deferred(
                chat_key=chat_key,
                chat_name=chat_name,
                peer_text=peer_text,
                peer_kind=peer_kind,
                reply_text=reply_text,
                defer_until=defer_until,
                defer_reason=f"pacing:{stage}:{int(pr.delay_sec)}s",
                run_id=run_id,
                extra={
                    "pacing_reason": pr.reason,
                    "pacing_delay_sec": pr.delay_sec,  # ★ W2-D5.5a：drain 用此自适应 typing burst
                    "typing_indicator_advised": pr.typing_indicator,
                },
                staleness_sec=staleness,
            )
            result["deferred_id"] = deferred_id
            result["deferred_until"] = defer_until
            result["step"] = "companion_pacing_deferred"
            result["ok"] = True
            self._state.update_chat_state(
                chat_key, chat_name=chat_name,
                last_peer_text=peer_text, last_peer_fp=fp,
                last_peer_kind=peer_kind,
            )
            logger.info(
                "[messenger_rpa] pacing defer chat=%s delay=%.1fs id=%d (%s)",
                chat_key, pr.delay_sec, deferred_id, pr.reason,
            )
            try:
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().record_companion_safe_skip(f"pacing:{stage}")
            except Exception:
                pass
            return "deferred"
        except Exception:
            logger.exception("pacing enqueue_deferred 失败，降级直发")
            return "passthrough"

    def _calc_defer_until_sec(self, gate: Dict[str, Any]) -> Optional[float]:
        """W2-D1：根据 _pre_send_gate 返回的降级原因计算 deferred_until 时间戳。

        返回 None 表示不应 defer（reply 本身不安全，应丢弃）。
        策略：
          - min_gap   → last_send + min_gap + 5s
          - daily_cap → 次日 0:30 + 0~30min 抖动（避免准点冲）
          - quiet_hours → 静夜 end + 0~30min 抖动
          - pace:throttle → 现在 + 8~12 min
          - pace:deny    → 现在 + 30~60 min
          - credit:low   → 现在 + 30 min
          - forbidden_keyword → None（reply 本身有问题，丢弃）
        """
        import datetime as _dt
        import random as _rd
        reason = (gate or {}).get("reason", "")
        now = time.time()
        # 危险 reply 直接丢弃，不 defer
        if reason.startswith("forbidden_keyword"):
            return None
        # 信用低 → 30min 冷却
        if gate.get("credit_forced") or reason.startswith("credit:"):
            return now + 30 * 60
        # min_gap：等到允许下次发送（用 wait_remaining_sec 字段）
        if reason.startswith("rate_limit:min_gap"):
            wait = float(gate.get("wait_remaining_sec") or 30.0)
            return now + max(5.0, wait + 5.0)
        # daily_cap：到次日 0:30 + 0-30 min 抖动
        if reason.startswith("rate_limit:daily_cap"):
            tmr = _dt.datetime.now().replace(hour=0, minute=30, second=0, microsecond=0) \
                  + _dt.timedelta(days=1)
            jitter = _rd.uniform(0, 30 * 60)
            return tmr.timestamp() + jitter
        # quiet_hours：到 end_hour + 0-30 min 抖动
        if reason.startswith("rate_limit:quiet_hours"):
            qh = (self._cfg.get("safety") or {}).get("quiet_hours") or []
            if isinstance(qh, (list, tuple)) and len(qh) == 2:
                try:
                    s, e = int(qh[0]), int(qh[1])
                    now_dt = _dt.datetime.now()
                    target = now_dt.replace(hour=e, minute=0, second=0, microsecond=0)
                    if target <= now_dt:
                        target = target + _dt.timedelta(days=1)
                    jitter = _rd.uniform(0, 30 * 60)
                    return target.timestamp() + jitter
                except Exception:
                    pass
            return now + 6 * 3600
        # pace:throttle → 8-12 min
        if reason.startswith("pace:throttle"):
            return now + _rd.uniform(8 * 60, 12 * 60)
        # pace:deny → 30-60 min
        if reason.startswith("pace:deny"):
            return now + _rd.uniform(30 * 60, 60 * 60)
        # 兜底：未知 reason 默认 15 min
        return now + 15 * 60

    def _pre_send_gate(self, reply_text: str) -> Optional[Dict[str, Any]]:
        """返回 None 表示允许发送；否则返回 {'reason', 'action'} 让上游降级。

        检查项：
        1) 最小发送间隔（全局）
        2) 每日发送上限
        3) 静夜时段（设备时钟为准）
        4) 禁用关键词（reply 内）
        """
        # ★ 测试模式：跳过所有 rate-limit / pace_learning gate
        if bool(self._cfg.get("companion_test_mode", False)):
            return None

        safety = (self._cfg.get("safety") or {})
        if not safety.get("enabled", True):
            return None

        # 1) 最小间隔
        min_gap = float(safety.get("min_send_gap_sec", 0) or 0)
        if min_gap > 0:
            stats = self._state.get_send_stats()
            last_ts = float(stats.get("last_send_ts") or 0)
            if last_ts:
                elapsed = time.time() - last_ts
                if elapsed < min_gap:
                    return {
                        "reason": (
                            f"rate_limit:min_gap elapsed={elapsed:.1f}s "
                            f"required={min_gap:.1f}s"
                        ),
                        "wait_remaining_sec": max(0.0, min_gap - elapsed),
                    }

        # 2) 日上限
        max_per_day = int(safety.get("max_sends_per_day", 0) or 0)
        if max_per_day > 0:
            stats = self._state.get_send_stats()
            if int(stats.get("count") or 0) >= max_per_day:
                return {
                    "reason": (
                        f"rate_limit:daily_cap count={stats.get('count')} "
                        f"cap={max_per_day}"
                    ),
                }

        # ★ P4-3：节奏学习 — 本小时发送量显著高于历史中位数 → 降级/拒发
        pace_cfg = (self._cfg.get("pace_learning") or {})
        if pace_cfg.get("enabled", True):
            try:
                pace = self._state.pace_check(
                    min_samples=int(pace_cfg.get("min_samples", 20) or 20),
                    median_multiplier=float(
                        pace_cfg.get("throttle_multiplier", 1.5) or 1.5
                    ),
                    block_multiplier=float(
                        pace_cfg.get("block_multiplier", 2.5) or 2.5
                    ),
                )
                if not pace.get("allow", True):
                    return {
                        "reason": (
                            f"pace:deny hour={pace.get('hour')} "
                            f"cur={pace.get('current_hour_count')} "
                            f"median={pace.get('hist_median')} "
                            f"ratio={pace.get('ratio')}"
                        ),
                        "pace": pace,
                    }
                if pace.get("throttle"):
                    return {
                        "reason": (
                            f"pace:throttle hour={pace.get('hour')} "
                            f"cur={pace.get('current_hour_count')} "
                            f"median={pace.get('hist_median')} "
                            f"ratio={pace.get('ratio')}"
                        ),
                        "pace": pace,
                    }
            except Exception:
                logger.debug("pace_check 异常", exc_info=True)

        # 3) 静夜窗口（device local time）
        qh = safety.get("quiet_hours") or []
        if isinstance(qh, (list, tuple)) and len(qh) == 2:
            try:
                import datetime as _dt
                h = _dt.datetime.now().hour
                s, e = int(qh[0]), int(qh[1])
                in_quiet = (s <= h < e) if s <= e else (h >= s or h < e)
                if in_quiet:
                    return {
                        "reason": f"rate_limit:quiet_hours hour={h} window={s}-{e}",
                    }
            except Exception:
                pass

        # 4) 禁用关键词
        forbidden = safety.get("forbidden_keywords") or []
        if isinstance(forbidden, (list, tuple)) and forbidden and reply_text:
            lower = reply_text.lower()
            for kw in forbidden:
                k = str(kw or "").strip().lower()
                if k and k in lower:
                    return {
                        "reason": f"content:forbidden_keyword:{k[:30]}",
                    }

        # 5) ★ Phase 0.2：QualityTracker 异常拦截
        # 默认拦 repeated / identity_leak / garbled —— 用户面前一识破，比限流还重要
        qcfg = (self._cfg.get("quality_gate") or {})
        if qcfg.get("enabled", True):
            ai = getattr(self._sm, "ai_client", None)
            qt = getattr(ai, "_quality_tracker", None) if ai is not None else None
            last = list(getattr(qt, "last_call_anomalies", []) or []) if qt is not None else []
            blocked_default = ["repeated", "identity_leak", "garbled"]
            blocked_types = set(qcfg.get("block_types") or blocked_default)
            hits = [a for a in last if a in blocked_types]
            if hits:
                return {
                    "reason": f"quality:{','.join(hits)}",
                    "quality_blocked": True,
                    "anomalies": list(last),
                }
        return None

    def _hint_non_ascii_adbkeyboard(
        self, serial: str, text: str, result: Dict[str, Any],
    ) -> None:
        """非 ASCII 文案时若未装 ADB Keyboard，打结构化 hint 便于排障/互发联调。"""
        if not (text or "").strip():
            return
        if all(ord(c) < 128 for c in text):
            return
        pkg = (self._cfg.get("adb_keyboard_package") or "com.android.adbkeyboard").strip()
        try:
            if adb.is_adbkeyboard_installed(serial, package=pkg):
                return
        except Exception:
            return
        result.setdefault("hints", []).append(
            "P0:non_ascii_needs_adbkeyboard_install",
        )

    def _reply_needs_approve_fallback(self, serial: str, reply: str) -> bool:
        """auto 模式下，如果 reply 有非 ASCII 且设备无 AdbKeyboard/clipboard，
        返回 True → 上游应当走 approve 队列。"""
        try:
            is_ascii = all(ord(c) < 128 for c in (reply or ""))
        except Exception:
            is_ascii = False
        if is_ascii:
            return False
        # 检查设备能力（带 10min 缓存）
        cache = getattr(self, "_unicode_capable_cache", None)
        if cache is None:
            cache = {}
            self._unicode_capable_cache = cache
        now = time.time()
        c = cache.get(serial)
        if c and now - c[0] < 600:
            unicode_ok = c[1]
        else:
            try:
                from src.integrations.messenger_rpa.text_input import (
                    precheck_text_input,
                )
                info = precheck_text_input(
                    serial,
                    adb_keyboard_package=(
                        self._cfg.get("adb_keyboard_package")
                        or "com.android.adbkeyboard"
                    ),
                )
                unicode_ok = bool(info.get("unicode_ok"))
            except Exception:
                unicode_ok = False
            cache[serial] = (now, unicode_ok)
        return not unicode_ok

    # ── Message Request 自动接受 ─────────────────────
    async def _accept_message_request_if_needed(
        self,
        serial: str,
        wh: Tuple[int, int],
        result: Dict[str, Any],
    ) -> bool:
        """检测当前聊天是否为 Message Request，若是则自动点击 Accept。

        返回 True 表示已接受（或不需要接受）；False 表示检测/接受失败。
        """
        try:
            from src.integrations.messenger_rpa import thread_actions as _ta
            from src.integrations.messenger_rpa import ui_scraper as _uis
            xml = _ta.dump_view_tree(serial)
            if xml is None:
                return True  # dump 失败，不阻塞主流程
            accept_b = _uis.find_message_request_accept(
                xml, screen_h=int(wh[1]),
            )
            if accept_b is None:
                return True  # 非 Message Request 页面，正常继续
            # 找到 Accept 按钮 → 点击
            logger.warning(
                "[messenger_rpa] 检测到 Message Request Accept 按钮 "
                "bounds=(%d,%d,%d,%d)，自动接受",
                accept_b.left, accept_b.top, accept_b.right, accept_b.bottom,
            )
            adb.input_tap(serial, accept_b.cx, accept_b.cy)
            result.setdefault("hints", []).append("message_request_accepted")
            # 等待 UI 过渡（Accept 后输入框出现）
            await asyncio.sleep(1.0)
            return True
        except Exception:
            logger.debug(
                "[messenger_rpa] _accept_message_request 异常", exc_info=True,
            )
            return True  # 非致命，不阻塞

    # ── P7-3：递进降级的发送重试 wrapper ─────────────
    @staticmethod
    def _replace_kana_laugh_with_emoji(text: str) -> str:
        """P2-B emoji 真人感后处理：把日语字符化笑「（笑）」「(笑)」「(爆笑)」
        「(泣)」等替换为真 emoji。也处理颜文字「(´∀｀)」「＞<」之类。
        日本中年男士 IM 习惯偶尔用 (笑)，但产品偏好真 emoji 更年轻有活力。
        """
        if not text:
            return text
        import re as _re
        out = text
        # 笑：（笑）/(笑)/（笑い）/(笑い)/(微笑)
        out = _re.sub(r"[（(]\s*(笑い?|w+|wara)\s*[）)]", "😂", out)
        # 爆笑：(爆笑)
        out = _re.sub(r"[（(]\s*爆笑\s*[）)]", "🤣", out)
        # 泣：(泣)
        out = _re.sub(r"[（(]\s*泣\s*[）)]", "😢", out)
        # 怒：(怒)
        out = _re.sub(r"[（(]\s*怒\s*[）)]", "😠", out)
        # 汗：(汗)
        out = _re.sub(r"[（(]\s*汗\s*[）)]", "😅", out)
        # 照：(照) (照れ)
        out = _re.sub(r"[（(]\s*照れ?\s*[）)]", "🥹", out)
        # 萌：(萌)
        out = _re.sub(r"[（(]\s*萌\s*[）)]", "🥰", out)
        # 中文场景：(笑) (哭) (汗)
        out = _re.sub(r"[（(]\s*哭\s*[）)]", "😢", out)
        # 末尾 lol / LOL / xD
        out = _re.sub(r"\s*\b(lol|LOL|xD|XD)\b", " 😂", out)
        # P2-B+ 中文笑声混在日文里出戏 → 替换为 emoji（开头/中间/末尾都覆盖）
        out = _re.sub(r"哈哈+", "😂", out)
        out = _re.sub(r"嗯嗯+", "😊", out)
        out = _re.sub(r"呵呵+", "😄", out)
        # 颜文字常见组合（最常见 4 个，不全覆盖避免误伤）
        out = _re.sub(r"\(´∀｀\)|\(￣▽￣\)", "😄", out)
        out = _re.sub(r"＞<|>_<", "😆", out)
        # 收紧多余空格
        out = _re.sub(r"\s{2,}", " ", out).strip()
        return out

    async def _typing_indicator_early_warmup(
        self,
        serial: str,
        chat_name: str,
        result: Dict[str, Any],
    ) -> None:
        """P3-C 提前 typing warmup（覆盖整个 vision+LLM 思考期）：
        在 hash diff 检测到变化后、vision 调用前立刻广播 typing 字符。
        让 victor 看到的"正在输入..."状态持续 vision(10-15s) + LLM(5-15s)
        全程，不只是最后发送前 1.5s。

        与 _typing_indicator_warmup 区别：
          - 不 sleep 不 DEL（让字符留在输入框，messenger 持续显示 typing）
          - 后续 _send_reply 内的 clear_focused_input 会清掉
          - 30s 内同一 chat 已触发过则跳过（防重复 append 输入框累积空格）
        """
        cfg = self._cfg.get("humanize") or {}
        ti = cfg.get("typing_indicator") or {}
        if not ti.get("enabled", False) or not ti.get("early_trigger", True):
            return
        if not chat_name:
            return
        if not hasattr(self, "_early_typing_active"):
            self._early_typing_active: Dict[str, float] = {}
        last_active = self._early_typing_active.get(chat_name, 0.0)
        # 30 秒内已 early-trigger 过，跳过（避免输入框积累多个空格）
        if last_active and (time.monotonic() - last_active) < 30:
            return
        warmup_chars = str(ti.get("warmup_chars", "　") or "　")
        try:
            adb.run_adb(
                ["shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT",
                 "--es", "msg", warmup_chars],
                serial=serial, timeout=3.0,
            )
            self._early_typing_active[chat_name] = time.monotonic()
            result.setdefault("hints", []).append("typing_indicator_early")
            logger.warning(
                "[messenger_rpa] 🎙 early typing warmup chat=%r → victor 端"
                "立刻看到 '正在输入...'（覆盖 vision+LLM 思考期 10-25s）",
                chat_name,
            )
        except Exception:
            logger.debug(
                "[messenger_rpa] early typing warmup failed", exc_info=True,
            )

    async def _typing_indicator_warmup(
        self, serial: str, result: Dict[str, Any]
    ) -> None:
        """P2-T 打字指示反馈：在真发送前先在输入框广播一个隐形字符触发
        Messenger 的 "对方正在输入..." 指示，让对方先看到 typing 状态再
        看到真消息（拟人感）。随后删除该字符，不影响真回复内容。
        """
        cfg = self._cfg.get("humanize") or {}
        ti = cfg.get("typing_indicator") or {}
        if not ti.get("enabled", False):
            return
        warmup_chars = str(ti.get("warmup_chars", "　") or "　")
        warmup_sec = float(ti.get("warmup_sec", 1.5) or 1.5)
        if warmup_sec <= 0 or not warmup_chars:
            return
        try:
            # 用 ADB Keyboard 广播触发 typing —— 对方 messenger 客户端会立刻
            # 收到 "is_typing=true"，UI 上显示 "对方正在输入..."
            adb.run_adb(
                ["shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT",
                 "--es", "msg", warmup_chars],
                serial=serial, timeout=3.0,
            )
            result.setdefault("hints", []).append("typing_indicator_warmup")
            logger.warning(
                "[messenger_rpa] 🎙 typing indicator warmup chars=%r sec=%.1fs",
                warmup_chars, warmup_sec,
            )
            await asyncio.sleep(warmup_sec)
            # 删除 warmup 字符（每字符 1 次 DEL）
            for _ in range(len(warmup_chars)):
                adb.input_keyevent(serial, "KEYCODE_DEL")
                await asyncio.sleep(0.05)
        except Exception:
            logger.debug("typing_indicator_warmup 异常", exc_info=True)

    def _apply_human_text_filters(self, reply_text: str, result: Dict[str, Any]) -> str:
        """在发送前对 LLM 回复做"真人感"后处理。
        - 字符化笑 → 真 emoji（受 humanize.replace_kana_laugh 开关控制，默认 true）
        """
        if not reply_text:
            return reply_text
        cfg = self._cfg.get("humanize") or {}
        if cfg.get("replace_kana_laugh", True):
            new_text = self._replace_kana_laugh_with_emoji(reply_text)
            if new_text != reply_text:
                result.setdefault("hints", []).append("kana_laugh_replaced")
                logger.warning(
                    "[messenger_rpa] humanize: replaced (笑) → emoji "
                    "before=%r after=%r",
                    reply_text[:80], new_text[:80],
                )
                reply_text = new_text
        return reply_text

    async def _send_reply_with_retry(
        self,
        serial: str,
        wh: Tuple[int, int],
        reply_text: str,
        result: Dict[str, Any],
    ) -> bool:
        """发送失败智能重试（4 级递进降级）。

        - Lv1：直接调 _send_reply（原路径）
        - Lv2：等 5s 再试（应对键盘未弹 / tap 抖动）
        - Lv3：若开着 ADB Keyboard，临时关掉走 input text fallback 再试
        - Lv4：触发 device_health.ensure_device_ready（wake/unlock/ime）后再试
        - 全失败：把 chat_key 短期 cooldown（30min，升级到人工审批）

        非致命错误（inject_text / tap 失败）会重试；致命错误（empty text）立即返回。
        """
        # P2-B：发送前做"真人感"后处理（emoji 替换 等）
        reply_text = self._apply_human_text_filters(reply_text, result)
        # P2-T：发送前先触发 typing indicator（"对方正在输入"），让 victor 端
        # 立刻看到 typing 状态，再看到真消息出现（提升真人感）
        await self._typing_indicator_warmup(serial, result)
        rt_cfg = (self._cfg.get("send_retry") or {})
        if not rt_cfg.get("enabled", True):
            return await self._send_reply(serial, wh, reply_text, result)

        max_attempts = int(rt_cfg.get("max_attempts", 4) or 4)
        retry_delay = float(rt_cfg.get("retry_delay_sec", 5.0) or 5.0)
        _cooldown_raw = rt_cfg.get("chat_cooldown_sec", 1800)
        cooldown_sec = int(1800 if _cooldown_raw is None else _cooldown_raw)
        if bool(self._cfg.get("companion_test_mode", False)):
            cooldown_sec = min(cooldown_sec, 120)
        attempt_log: List[Dict[str, Any]] = []

        orig_use_ime = bool(self._cfg.get("use_adb_keyboard", True))
        ime_toggled = False

        # ★ 首次发送前：检测并自动接受 Message Request
        await self._accept_message_request_if_needed(serial, wh, result)

        for attempt in range(1, max_attempts + 1):
            try:
                ok = await self._send_reply(serial, wh, reply_text, result)
            except Exception as ex:
                ok = False
                result["error"] = f"send_exception:{type(ex).__name__}:{ex}"
                logger.warning(
                    "[messenger_rpa] send attempt %d raised: %s", attempt, ex,
                )
            err = str(result.get("error") or "")
            attempt_log.append({
                "n": attempt, "ok": ok, "path": result.get("send_path"),
                "error": err[:120],
            })
            logger.warning(
                "[messenger_rpa] send attempt #%d ok=%s err=%s path=%s",
                attempt, ok, err[:80], result.get("send_path"),
            )

            if ok:
                result["send_attempts"] = attempt_log
                # 成功后若 IME 被临时关闭，恢复配置
                if ime_toggled:
                    self._cfg["use_adb_keyboard"] = orig_use_ime
                return True

            # 致命错误：文本为空 → 不重试
            if "empty_reply_text" in err:
                result["send_attempts"] = attempt_log
                return False

            # ★ 致命错误：UI 安全护盾触发（误点相机/图库）→ 不重试
            # 已在 _send_reply 里执行了 BACK 恢复；重试只会再误点一次
            if result.get("step") == "ui_unsafe_tap":
                result["send_attempts"] = attempt_log
                return False

            if attempt >= max_attempts:
                break

            # ── 降级策略 ──
            if attempt == 1:
                # Lv2：等一会重试（键盘可能还没弹）
                logger.info(
                    "[messenger_rpa] send Lv2: 等 %.1fs 重试", retry_delay,
                )
                await asyncio.sleep(retry_delay)
                # 清 error 便于下一次干净重试
                result["error"] = ""
                continue
            if attempt == 2 and orig_use_ime and not ime_toggled:
                # Lv3：临时关 ADB Keyboard，走 input text fallback
                logger.info(
                    "[messenger_rpa] send Lv3: 临时关 ADB Keyboard 降级",
                )
                self._cfg["use_adb_keyboard"] = False
                ime_toggled = True
                await asyncio.sleep(2.0)
                result["error"] = ""
                continue
            if attempt == 3 or (attempt == 2 and not orig_use_ime):
                # Lv4：device_health.ensure_device_ready 全面修复
                logger.info(
                    "[messenger_rpa] send Lv4: 触发 device_health 修复",
                )
                try:
                    from src.integrations.messenger_rpa.device_health import (
                        ensure_device_ready,
                    )
                    hr = ensure_device_ready(
                        serial,
                        auto_reconnect=True,
                        auto_wake=True,
                        auto_unlock_swipe=True,
                        preferred_ime=(
                            self._cfg.get("adb_keyboard_ime")
                            if self._cfg.get("use_adb_keyboard", True)
                            else None
                        ),
                        hard_restart_on_fail=False,
                        max_attempts=2,
                    )
                    attempt_log[-1]["device_heal"] = {
                        "ok": bool(getattr(hr, "ok", False)),
                        "note": str(getattr(hr, "note", ""))[:80],
                    }
                except Exception as ex:
                    logger.debug(
                        "P7-3 device_heal 失败: %s", ex, exc_info=True,
                    )
                # 修复后需要重新 foreground messenger（可能被锁屏/切 app）
                try:
                    self._foreground_messenger(serial, result)
                except Exception:
                    pass
                await asyncio.sleep(3.0)
                result["error"] = ""
                continue
            # 其他情况：just wait
            await asyncio.sleep(retry_delay)
            result["error"] = ""

        # ── 全部失败 → 短期 chat cooldown + 恢复 IME 配置 ──
        if ime_toggled:
            self._cfg["use_adb_keyboard"] = orig_use_ime

        result["send_attempts"] = attempt_log
        result["send_all_failed"] = True
        logger.warning(
            "[messenger_rpa] send_all_failed after %d attempts: %s",
            max_attempts, attempt_log,
        )
        chat_key = str(result.get("chat_key") or "")
        if chat_key and cooldown_sec > 0:
            try:
                # 复用 P1-4 的 escalated_until_ts 字段做 30min 屏蔽
                until = time.time() + float(cooldown_sec)
                self._state.set_escalation(
                    chat_key,
                    until_ts=until,
                    reason="send_all_failed",
                )
                logger.warning(
                    "[messenger_rpa] P7-3 chat cooldown: chat=%s 30min "
                    "（连续 %d 次发送失败，升级人工）",
                    chat_key, max_attempts,
                )
            except Exception:
                logger.debug("set_escalation cooldown 失败", exc_info=True)
            # 扣信用分警示
            try:
                cred_cfg = (self._cfg.get("credit_policy") or {})
                if cred_cfg.get("enabled", True):
                    delta = int(cred_cfg.get("send_fail_delta", -10) or -10)
                    if delta:
                        self._state.adjust_credit(
                            chat_key, delta, reason="send_all_failed",
                        )
            except Exception:
                logger.debug("send_all_failed 扣信用失败", exc_info=True)
        return False

    # ── 内部：发送 ────────────────────────────────
    async def _send_reply(
        self,
        serial: str,
        wh: Tuple[int, int],
        reply_text: str,
        result: Dict[str, Any],
    ) -> bool:
        """三段式发送：

        1) 点 INPUT_TEXT_FIELD（键盘未弹态坐标） → 唤起键盘
        2) 等待键盘动画完成（>=600ms）→ 注入文字（ADB Keyboard 优先，
           失败降级 input text ASCII）
        3) 等人类节奏 → 点 SEND_BTN（键盘已弹态坐标 671×940）

        非 ASCII 字符且 ADB Keyboard 不可用时，自动失败并 log，避免发送乱码。
        """
        ime = (self._cfg.get("adb_keyboard_ime") or "").strip()
        use_adb_keyboard = bool(self._cfg.get("use_adb_keyboard", True))
        if use_adb_keyboard and ime:
            try:
                ime_set = adb.ime_set_adb_keyboard(serial, ime)
                ime_ready = adb.wait_for_adb_keyboard_ready(serial, timeout_sec=2.0)
                result["ime_unified"] = {
                    "target": ime,
                    "set_ok": ime_set.returncode == 0,
                    "ready": bool(ime_ready),
                }
                if ime_set.returncode != 0:
                    logger.warning(
                        "[messenger_rpa] 设置 ADB Keyboard 失败 rc=%s stderr=%r",
                        ime_set.returncode,
                        (ime_set.stderr or ime_set.stdout or "")[:160],
                    )
            except Exception as ex:
                result["ime_unified"] = {
                    "target": ime,
                    "set_ok": False,
                    "ready": False,
                    "error": str(ex)[:160],
                }
                logger.warning("[messenger_rpa] 统一输入法失败: %s", ex)

        # 截断到 Messenger 安全长度 (避免一次发太长被风控)
        reply_text = (reply_text or "").strip()[:1500]
        if not reply_text:
            result["error"] = "empty_reply_text"
            return False

        text_x, text_y = cc.INPUT_TEXT_FIELD.at(*wh)
        if use_adb_keyboard:
            send_x, send_y = cc.SEND_BTN_DOCKED.at(*wh)
            send_tap_src = "adbkeyboard_docked"
        else:
            send_x, send_y = cc.SEND_BTN.at(*wh)
            send_tap_src = "formula"

        # ★ P0-UI 安全护盾：优先用 UI XML 精准定位输入框，避免公式坐标
        # 误点相机/图库/麦克风等底栏按钮。公式坐标仅作 fallback。
        _input_tap_src = "formula"
        if (not use_adb_keyboard) and bool(self._cfg.get("use_ui_hierarchy_tap", True)):
            try:
                from src.integrations.messenger_rpa import thread_actions as _ta_pre
                from src.integrations.messenger_rpa import ui_scraper as _uis_pre
                _xml_pre = _ta_pre.dump_view_tree(serial)
                if _xml_pre is not None:
                    _ib_pre = _uis_pre.find_input_box(_xml_pre, screen_h=int(wh[1]))
                    if _ib_pre is not None:
                        text_x, text_y = _ib_pre.bounds.cx, _ib_pre.bounds.cy
                        _input_tap_src = f"ui_xml({text_x},{text_y})"
            except Exception:
                logger.debug("[messenger_rpa] 预定位输入框异常", exc_info=True)
        result["input_tap_src"] = _input_tap_src

        # Step 1: tap 输入框唤起键盘
        adb.input_tap(serial, text_x, text_y)
        await asyncio.sleep(0.5)  # 键盘动画开始

        # ★ P0-UI 安全护盾：验证键盘弹起；未弹时检查是否误开了相机/图库
        try:
            from src.integrations.messenger_rpa import thread_actions as _ta_kbd
            if use_adb_keyboard:
                # ADBKeyboard 通过 broadcast 注入文本，不一定弹出普通软键盘。
                # 这里不能把“键盘未弹”当作失败，否则会在真实 Messenger
                # 会话页误判为 input_tap_left_messenger。后续 inject_and_verify
                # 和 send button 定位仍会兜底校验。
                result["keyboard_open"] = "skipped_adbkeyboard"
            else:
                _kw = await _ta_kbd.wait_keyboard_open(
                    serial, screen_h=int(wh[1]), timeout_sec=4.5,
                )
                if not _kw.ok:
                    _in_msgr = _ta_kbd.check_in_messenger(serial)
                    if not _in_msgr:
                        # 误进了相机/图库等 ── 立刻两次 BACK 回到 Messenger
                        for _ in range(2):
                            adb.run_adb(
                                ["shell", "input", "keyevent", "KEYCODE_BACK"],
                                serial=serial, timeout=5.0,
                            )
                            time.sleep(0.3)
                        result["step"] = "ui_unsafe_tap"
                        result["error"] = (
                            f"input_tap_left_messenger tap_src={_input_tap_src} "
                            f"tap=({text_x},{text_y})"
                        )
                        logger.error(
                            "[messenger_rpa] ★ 安全护盾：点击输入框后离开了 Messenger"
                            "（可能误触相机/图库）；已 BACK 恢复。"
                            " serial=%s tap=(%d,%d) src=%s",
                            serial, text_x, text_y, _input_tap_src,
                        )
                        return False
                    # 仍在 Messenger 但键盘未弹 → 补 tap 并再等一轮
                    logger.warning(
                        "[messenger_rpa] 键盘未弹（仍在 Messenger），补 tap "
                        "serial=%s tap=(%d,%d)", serial, text_x, text_y,
                    )
                    adb.input_tap(serial, text_x, text_y)
                    _kw2 = await _ta_kbd.wait_keyboard_open(
                        serial, screen_h=int(wh[1]), timeout_sec=3.5,
                    )
                    if _kw2.ok:
                        result["keyboard_open"] = True
                    else:
                        _in_msgr2 = _ta_kbd.check_in_messenger(serial)
                        if not _in_msgr2:
                            for _ in range(2):
                                adb.run_adb(
                                    ["shell", "input", "keyevent", "KEYCODE_BACK"],
                                    serial=serial, timeout=5.0,
                                )
                                time.sleep(0.3)
                            result["step"] = "ui_unsafe_tap"
                            result["error"] = (
                                f"input_tap_left_messenger tap_src={_input_tap_src} "
                                f"tap=({text_x},{text_y}) retry2"
                            )
                            logger.error(
                                "[messenger_rpa] ★ 安全护盾(补tap后)：离开了 Messenger"
                                " serial=%s tap=(%d,%d) src=%s",
                                serial, text_x, text_y, _input_tap_src,
                            )
                            return False
                        logger.warning(
                            "[messenger_rpa] 补tap后键盘仍未弹，继续注入 serial=%s",
                            serial,
                        )
                else:
                    result["keyboard_open"] = True
        except Exception:
            # 键盘验证本身异常不阻断发送，补足等待时间即可
            await asyncio.sleep(0.3)

        # Step 2: 注入文字（必须 **验证 EditText** 真写入）
        #
        # 背景：部分 ROM（尤其 MIUI）上 `clipboard_paste` 的 adb 返回码可能为 0，
        # 但实际未把文本粘进输入框；若继续点 SEND，容易落到「空发送/快捷反应」路径，
        # 用户侧表现为只收到 👍 / like。
        from src.integrations.messenger_rpa import thread_actions as _ta_inj
        inj_cfg = {
            "use_adb_keyboard": use_adb_keyboard,
            "adb_keyboard_ime": ime,
            "adb_keyboard_package": (
                self._cfg.get("adb_keyboard_package")
                or "com.android.adbkeyboard"
            ).strip(),
            "allow_clipboard_fallback": bool(
                self._cfg.get("allow_clipboard_fallback", True)
            ),
            "allow_input_text_fallback_for_ascii": bool(
                self._cfg.get("allow_input_text_fallback_for_ascii", True)
            ),
        }
        iv = await _ta_inj.inject_and_verify(
            serial,
            reply_text,
            inject_cfg=inj_cfg,
            screen_h=int(wh[1]),
            # P1-A：0.85 → 1.5，给 emoji 渲染留充足时间；MIUI 日文 IME 渲染
            # 比英文慢 ~50%，原默认值会让 vision 看到不完整文本误报 mismatch
            settle_sec=float(self._cfg.get("inject_verify_settle_sec", 1.5) or 1.5),
            # P1-A：2 → 8 字符容忍；emoji 占 1-4 字符，末尾问号/感叹号 OCR 不
            # 准也常被吃掉。带 emoji 的回复至少 2-3 emoji 共 8 字符要容忍。
            tolerate_truncation_chars=int(
                self._cfg.get("inject_verify_tol_chars", 8) or 8,
            ),
            max_retries=int(
                self._cfg.get("inject_verify_max_retries", 2)
                if self._cfg.get("inject_verify_max_retries", None) is not None
                else 2
            ),
            vision_cfg=self._vision_cfg(),
            global_vision_cfg=self._global_vision_cfg(),
        )
        result["send_path"] = iv.injected_via
        result["inject_verify"] = {
            "ok": bool(iv.ok),
            "reason": iv.reason,
            "tries": iv.tries,
            "actual_text_head": (iv.actual_text or "")[:120],
        }
        if not iv.ok:
            try:
                _ta_inj.clear_focused_input(
                    serial,
                    adb_keyboard_package=(
                        self._cfg.get("adb_keyboard_package")
                        or "com.android.adbkeyboard"
                    ),
                )
                result["draft_cleared_after_inject_fail"] = True
            except Exception:
                logger.debug("[messenger_rpa] 清理失败草稿异常", exc_info=True)
            result["error"] = (
                f"inject_verify_failed:{iv.reason} via={iv.injected_via}"
            )
            return False
        logger.info(
            "[messenger_rpa] 注入文字成功 path=%s len=%d verify=%s",
            iv.injected_via, len(reply_text), iv.reason,
        )

        # Step 3: 人类节奏 → 点 SEND
        time.sleep(typing_duration_sec(reply_text, self._pacing))

        # ★ P7-UI (2026-04)：先 UI XML 精准定位 send 按钮；失败才回退预设公式
        send_tap_src = send_tap_src
        if bool(self._cfg.get("use_ui_hierarchy_tap", True)):
            try:
                from src.integrations.messenger_rpa.ui_inbox_scraper import (
                    find_send_button,
                )
                sb = find_send_button(
                    serial,
                    adb_user_id=self._adb_user_id,
                    timeout_s=float(self._cfg.get("ui_dump_timeout_s") or 4.0),
                )
                if sb is not None:
                    send_x, send_y = sb.x_center, sb.y_center
                    send_tap_src = f"ui_xml({sb.desc})"
                    logger.warning(
                        "[messenger_rpa] send button 通过 UI XML 定位到 "
                        "(%d, %d) desc=%r", send_x, send_y, sb.desc,
                    )
                else:
                    logger.warning(
                        "[messenger_rpa] UI XML 没找到 send button，"
                        "退化到公式坐标 (%d, %d)", send_x, send_y,
                    )
            except Exception:
                logger.debug(
                    "[messenger_rpa] find_send_button 异常", exc_info=True)
        logger.warning(
            "[messenger_rpa] 点 SEND: (%d, %d) src=%s", send_x, send_y, send_tap_src,
        )
        adb.input_tap(serial, send_x, send_y)
        time.sleep(0.6)
        # ★ P1-6：记录发送以供速率/日额统计
        try:
            stats = self._state.record_send()
            result["send_counters"] = stats
        except Exception:
            logger.debug("record_send 失败", exc_info=True)
        # W4-Runner：ContactHooks 入库 outbound 消息
        hooks = self._contact_hooks
        if hooks is not None:
            try:
                peer_name = str(result.get("chat_name") or "")
                hooks.on_message(
                    channel="messenger",
                    account_id=str(getattr(self, "_account_id", "") or "default"),
                    external_id=peer_name,
                    direction="out",
                    text_preview=(reply_text or "")[:120],
                    display_name=peer_name,
                )
            except Exception:
                logger.debug("contact_hooks on_message(out) 异常", exc_info=True)
        # ★ P3-1：成功发送 → 清 risk 计数（非 blocked 状态）
        try:
            self._state.clear_risk()
        except Exception:
            logger.debug("clear_risk 失败", exc_info=True)
        # ★ P3-6：发送成功后后台触发对话摘要（若达阈值）
        try:
            ck = result.get("chat_key") or ""
            if ck:
                self._dispatch_episodic_summary(str(ck))
        except Exception:
            logger.debug("dispatch_episodic_summary 失败", exc_info=True)
        # ★ P4-7：发送成功 → 信用分缓慢恢复
        try:
            ck = result.get("chat_key") or ""
            cred_cfg = (self._cfg.get("credit_policy") or {})
            if ck and cred_cfg.get("enabled", True):
                delta = int(cred_cfg.get("recover_delta", 2) or 2)
                if delta:
                    self._state.adjust_credit(
                        str(ck), delta, reason="send_ok",
                    )
        except Exception:
            logger.debug("P4-7 send_ok credit 加分失败", exc_info=True)
        return True

    # ── 内部：人工转接（P1-4） ────────────────────
    def _evaluate_escalation(
        self, peer_msg: PeerMessage, chat_key: str
    ) -> "_escalation.EscalationDecision":
        """从 state_store 读最近 3 条 peer 历史，调用 escalation.evaluate。"""
        try:
            # 粗略：从 bot.db 的 _conversation_history 取 peer 最近消息
            cs = getattr(self._sm, "_context_store", None)
            recent_peer: List[str] = []
            if cs is not None:
                try:
                    uctx = cs.get(chat_key)
                    for m in (uctx.get("_conversation_history") or [])[::-1]:
                        if m.get("role") == "user":
                            recent_peer.append(str(m.get("content") or ""))
                        if len(recent_peer) >= 5:
                            break
                except Exception:
                    pass
            return _escalation.evaluate(
                peer_text=peer_msg.to_text_for_ai(),
                recent_peer_texts=recent_peer,
                recent_assistant_texts=None,
                config=self._cfg,
            )
        except Exception:
            logger.debug("escalation.evaluate 异常", exc_info=True)
            return _escalation.EscalationDecision.none()

    def _record_crisis_event(
        self,
        *,
        category: str,
        chat_key: str,
        chat_name: str,
        peer_text: str,
        detail: str = "",
    ) -> None:
        """W2-D2.3：危机事件三冗余记录。

        三层保险（任一可用即不丢）：
        1) jsonl 文件（logs/crisis_log.jsonl）— 不依赖网络/服务，最稳
        2) ERROR 级 logger（→ logs/app.log + 任何 stderr 监控）
        3) telegram + webhook（_notify_escalation） — 实时触达运营

        避免事故责任真空：网断 / token 错 / app.log 损毁 都至少留一层。
        """
        ts = time.time()
        # 1) 独立 jsonl
        try:
            import json as _json
            from pathlib import Path as _P
            log_dir = _P("logs")
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "crisis_log.jsonl").open("a", encoding="utf-8") as f:
                f.write(_json.dumps({
                    "ts": ts,
                    "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)),
                    "category": category,
                    "chat_key": chat_key,
                    "chat_name": chat_name,
                    "peer_text": (peer_text or "")[:500],
                    "detail": (detail or "")[:200],
                    "account_id": str(getattr(self, "_account_id", "") or "default"),
                }, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("crisis_log.jsonl 写失败", exc_info=True)
        # 2) ERROR 日志（自带轮转）
        logger.error(
            "[CRISIS] category=%s chat=%s peer=%r detail=%s",
            category, chat_key, (peer_text or "")[:120], detail,
        )
        # 3) telegram / webhook
        try:
            self._notify_escalation(
                chat_name=chat_name,
                chat_key=chat_key,
                reason=f"crisis:{category}",
                message=f"[{category}] {detail}",
                peer_text=peer_text,
            )
        except Exception:
            logger.debug("crisis telegram notify 失败", exc_info=True)

    def _notify_escalation(
        self,
        *,
        chat_name: str,
        chat_key: str,
        reason: str,
        message: str,
        peer_text: str,
    ) -> None:
        """把 escalation 通过 WebhookNotifier + Telegram 群同时送出。

        复用 P0-4 的通知通道 + P2-4 新增的 TG 推送：
        - webhook：结构化 JSON，接企业微信/Slack/自研系统
        - telegram：人读消息，带 deep link 回 Web 审批详情
        两路失败都不影响主流程。
        """
        try:
            notifier = getattr(self, "_webhook_notifier", None)
            if notifier is not None:
                notifier.notify(
                    event="messenger_rpa.escalation",
                    payload={
                        "chat_name": chat_name,
                        "chat_key": chat_key,
                        "reason": reason,
                        "message": message,
                        "peer_text": (peer_text or "")[:300],
                        "ts": time.time(),
                    },
                )
        except Exception:
            logger.debug("_notify_escalation webhook 失败", exc_info=True)

        # ★ P2-4：推送到 Telegram 管理员群
        try:
            self._notify_escalation_telegram(
                chat_name=chat_name,
                chat_key=chat_key,
                reason=reason,
                message=message,
                peer_text=peer_text,
            )
        except Exception:
            logger.debug("_notify_escalation telegram 失败", exc_info=True)

        # ★ P4-7：信用分扣分
        try:
            cred_cfg = (self._cfg.get("credit_policy") or {})
            if cred_cfg.get("enabled", True) and chat_key:
                delta = int(cred_cfg.get("escalation_delta", -10) or -10)
                r = self._state.adjust_credit(
                    chat_key, delta, reason=f"escalation: {reason}"[:200],
                )
                logger.info(
                    "[messenger_rpa] P4-7 credit adjust chat=%s delta=%d → %d",
                    chat_key, delta, r.get("credit", -1),
                )
        except Exception:
            logger.debug("P4-7 escalation credit 扣分失败", exc_info=True)

    def _notify_escalation_telegram(
        self,
        *,
        chat_name: str,
        chat_key: str,
        reason: str,
        message: str,
        peer_text: str,
    ) -> None:
        """把 escalation 推送到 Telegram 管理员群/私聊。

        配置：
        - messenger_rpa.escalation.telegram_chat_id（覆盖）
        - telegram.admin_chat_id（兜底）
        任一存在即推送；两者都为空则跳过。
        """
        tg = self._telegram_client
        if tg is None or not hasattr(tg, "client"):
            return
        esc_cfg = (self._cfg.get("escalation") or {})
        # 优先用 escalation 专属 chat_id
        target_chat = str(
            esc_cfg.get("telegram_chat_id")
            or (
                (self._cm.config or {}).get("telegram", {}).get("admin_chat_id")
                if hasattr(self._cm, "config")
                else ""
            )
            or ""
        ).strip()
        if not target_chat:
            return

        # 构造 deep link（用 web_admin.site_name / host / port）
        wa_cfg = {}
        try:
            wa_cfg = (self._cm.config or {}).get("web_admin") or {}
        except Exception:
            pass
        site = str(wa_cfg.get("site_name") or "Messenger RPA").strip()
        # 运营内部链接，host+port 可能是 0.0.0.0 → 换成 external_url 或默认不提供
        ext_url = str(wa_cfg.get("external_url") or "").strip()
        link_hint = ""
        if ext_url:
            link_hint = f"\n🔗 {ext_url.rstrip('/')}/messenger-rpa"

        text = (
            f"🚨 [{site}] Messenger 人工转接\n"
            f"👤 对方：{chat_name}（key={chat_key}）\n"
            f"🏷 原因：{reason}\n"
            f"📝 说明：{message}\n"
            f"💬 最近消息：{(peer_text or '')[:200]}"
            f"{link_hint}"
        )

        async def _send():
            try:
                cid = int(target_chat) if str(target_chat).lstrip("-").isdigit() \
                    else target_chat
                await tg.client.send_message(chat_id=cid, text=text)
                logger.info(
                    "[messenger_rpa] escalation 已推送到 Telegram chat_id=%s",
                    cid,
                )
            except Exception as ex:
                logger.warning(
                    "[messenger_rpa] escalation 推送 Telegram 失败: %s",
                    ex,
                )

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_send())
        except RuntimeError:
            # 不在 async 上下文：同步 schedule（不应该发生）
            logger.debug("no running loop for telegram escalation send")

    # ── 内部：媒体最小应答 ────────────────────────
    # 按 (kind, lang) 查模板；默认英文，language_alignment=auto 时按 peer 语种回
    #
    # P4-3 (2026-05-04) sticker 风格化：根据 peer_msg.desc（vision OCR 简述）
    # keyword 关键词分类，每类用专属模板池——用户感受 sticker 回应"对得上"。
    # 零额外 vision 调用，复用现有 desc。
    _STICKER_CATEGORY_KEYWORDS: Dict[str, Tuple[str, ...]] = {
        "happy": (
            "笑", "哈哈", "笑脸", "笑顔", "smile", "smiling", "laugh", "lol",
            "haha", "happy", "joy", "fun", "funny",
        ),
        "love": (
            "爱心", "心型", "心形", "比心", "heart", "love", "romance", "kiss",
            "ハート", "好き", "ラブ", "💕", "浪漫", "亲亲", "亲吻",
        ),
        "sad": (
            "哭", "泣", "悲", "cry", "crying", "sad", "tear", "悲しい",
            "つらい", "しんみり",
        ),
        "angry": (
            "怒", "怒り", "angry", "mad", "rage", "irritate",
        ),
        "cute": (
            "可爱", "可愛", "cute", "kawaii", "soft", "ぬいぐるみ",
            "かわいい", "🥰",
        ),
        # P1.5+D1 (2026-05-04)：扩 awkward / wink / thinking 三类，覆盖更多常见 sticker
        "awkward": (
            "尴尬", "脸红", "捂脸", "汗", "💦", "💧", "facepalm", "embarrass",
            "embarrassed", "awkward", "ぎこちない", "恥ずかしい", "あちゃー",
            "汗顔", "🤦",
        ),
        "wink": (
            "眨眼", "暧昧", "调皮", "wink", "smirk", "playful", "tease",
            "winky", "😉", "😏", "ウィンク", "うふふ",
        ),
        "thinking": (
            "思考", "想", "疑问", "puzzled", "thinking", "ponder", "wonder",
            "questioning", "🤔", "うーん", "考える", "考え",
        ),
    }
    _STICKER_CATEGORY_TEMPLATES: Dict[str, Dict[str, List[str]]] = {
        "happy": {
            "en": ["Haha 😆", "That's so funny lol", "Hahaha I love it"],
            "zh": ["哈哈哈太搞笑了 😆", "笑死我了 😂", "哈哈这个真的可以"],
            "ja": ["笑った😆", "ええっそれw", "ウケる〜"],
        },
        "love": {
            "en": ["Aww 🥰", "So sweet ❤️", "Love that"],
            "zh": ["哎呀 🥰", "好甜～❤️", "嘻嘻"],
            "ja": ["きゅん🥰", "それ可愛い❤️", "うふふ"],
        },
        "sad": {
            "en": ["Oh no, you okay?", "Hugs 🥺", "Aw, what happened?"],
            "zh": ["怎么啦？要紧吗", "抱抱 🥺", "怎么了，跟我说说"],
            "ja": ["どうしたの？", "ぎゅっ 🥺", "なんかあった？"],
        },
        "angry": {
            "en": ["Whoa, what happened?", "Take it easy 😅"],
            "zh": ["哎，怎么了", "别气别气 😅"],
            "ja": ["え、どした？", "まあまあ 😅"],
        },
        "cute": {
            "en": ["Cute! 🥰", "Aww", "So adorable"],
            "zh": ["好可爱～🥰", "嘻嘻", "也太萌了"],
            "ja": ["可愛い〜🥰", "うふふ", "それ良い"],
        },
        # P1.5+D1 (2026-05-04)：扩 3 类
        "awkward": {
            "en": ["Lol awkward 😅", "Oof haha", "Hehe", "Hahaha I'm dying 😂"],
            "zh": ["哎呀有点尴尬 😅", "嘿嘿", "哈哈哈这表情", "笑死我了 😂"],
            "ja": ["あー恥ずかしい😅", "あちゃー", "うふふ", "笑った笑った😂"],
        },
        "wink": {
            "en": ["Hehe 😉", "Oh you 😏", "I see what you did", "Hahaha okay"],
            "zh": ["嘿嘿 😉", "你～😏", "我懂的哈哈", "嘻嘻"],
            "ja": ["うふふ😉", "もう〜😏", "わかってるよ", "あはは"],
        },
        "thinking": {
            "en": ["Hmm let me think 🤔", "Good question", "Hmm hmm", "Yeah I get you"],
            "zh": ["嗯～让我想想 🤔", "这个问题嘛", "嗯嗯", "我明白你意思"],
            "ja": ["うーん🤔", "そうだなー", "考えてみる", "なるほどね"],
        },
    }

    @classmethod
    def _classify_sticker_category(
        cls, desc: str, content: str = "",
    ) -> Optional[str]:
        """根据 peer_msg.desc + content 关键词分类 sticker。
        命中返回 category name；不命中返回 None 让上层走通用模板。

        P1.5+D1 (2026-05-04)：从 5 类扩到 8 类（加 awkward/wink/thinking）。
        优先级：love > happy > wink > cute > thinking > awkward > sad > angry。
        排序按"语义精确度高的在前"——爱心/wink 这类强信号优先识别，sad/angry
        是较弱兜底。
        """
        text = ((desc or "") + " " + (content or "")).lower()
        if not text.strip():
            return None
        for cat in (
            "love", "happy", "wink", "cute", "thinking", "awkward", "sad", "angry",
        ):
            kws = cls._STICKER_CATEGORY_KEYWORDS.get(cat, ())
            if any(kw.lower() in text for kw in kws):
                return cat
        return None

    # P1.5+D2 (2026-05-04)：peer 主动让你看图/视频的关键词词典，多语种
    # 命中时升级 fusion_hint 为"对方主动让你看"提示，让 LLM 倾向具体回应内容
    # （例：是本人自拍 → 夸；是宠物 → 评价宠物；是商品 → 评价；不是预期内容 → 调侃）
    _MEDIA_SHOW_INTENT_KEYWORDS: Tuple[str, ...] = (
        # 中文（简繁）
        "看看", "看一下", "瞧瞧", "瞅瞅", "给你看", "我发", "拍了", "拍的",
        "晒一下", "分享给你", "你看", "看下", "睇下", "睇睇",
        # English
        "show you", "show me", "look at this", "look at that", "check this",
        "check it out", "let me show", "picture of", "send you a", "sent you a",
        "i'll send", "ill send", "wanna see", "want to see", "wanna show",
        # 日本語
        "見せる", "見せて", "送るね", "撮った", "撮って", "写真撮", "シェア",
        "見て見て", "見てて", "見てみて",
        # 韓国語
        "보여", "찍었", "사진 보내", "보낼게",
    )

    def _build_media_fusion_hint(
        self,
        chat_key: str,
        peer_msg: PeerMessage,
        extra: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[str]:
        """P1-A2 + P1.5+D2 (2026-05-04)：为媒体消息构造"上一句 peer 文本"上下文提示。

        优先级：
          1. 本轮 extra_peers 里有 text 类 → 返回 None（让 P2-2 多消息合并自然带上下文，避免冗余）
          2. chat_state.last_peer_text 含 "show me / 看看 / 写真撮" 等关键词
             → 返回升级提示："对方主动让你看（前文：'X'），高概率是本人/相关图片，
                倾向具体回应内容"
          3. chat_state.last_peer_text 含其他普通文本 → 返回精简提示
          4. 都没有 → None
        失败/异常静默返回 None，不阻塞主流程。
        """
        try:
            extra = extra or []
            for pm_d in extra:
                if not isinstance(pm_d, dict):
                    continue
                if str(pm_d.get("kind") or "").strip().lower() == "text":
                    txt = str(pm_d.get("content") or "").strip()
                    if txt:
                        return None  # 让 P2-2 合并自然带，避免重复
            chat_state = self._state.get_chat_state(chat_key) or {}
            last_peer = str(chat_state.get("last_peer_text") or "").strip()
            if not last_peer:
                return None
            # 跳过纯媒体占位（如 "[图片] xxx" 上次也是图）
            if last_peer.startswith("[") and "]" in last_peer[:10]:
                return None
            short = last_peer[:80]
            if len(last_peer) > 80:
                short += "…"
            low = last_peer.lower()
            if any(kw in low for kw in self._MEDIA_SHOW_INTENT_KEYWORDS):
                return (
                    f'对方主动让你看（前文："{short}"），'
                    f'高概率是相关/本人内容，倾向直接回应你看到的具体内容'
                )
            return f'上一句对方说："{short}"'
        except Exception:
            return None

    # ── P2 (2026-05-04) sticker reply modality 决策器 ───────────────
    def _sticker_24h_count(self, chat_key: str) -> int:
        """统计 chat 最近 24h 的 sticker 发送次数（含 dry_run 模拟）。"""
        now = time.time()
        bucket = self._sticker_send_history.get(chat_key) or []
        # 清掉超 24h 的旧记录
        bucket = [t for t in bucket if now - t < 86400]
        self._sticker_send_history[chat_key] = bucket
        return len(bucket)

    def _record_sticker_decision(self, chat_key: str) -> None:
        """记录一次 sticker 决策（成功命中概率，dry_run / 真发都计入）。"""
        bucket = self._sticker_send_history.setdefault(chat_key, [])
        bucket.append(time.time())

    def _infer_sticker_cat_from_reply(self, reply_text: str) -> Optional[str]:
        """从 reply 文本反推情绪类——LLM 写了什么样的 reply，就配什么 sticker。

        优先级：emoji > _STICKER_CATEGORY_KEYWORDS（与 peer sticker 分类共用词典）
        """
        if not reply_text:
            return None
        text = reply_text.lower()
        # emoji 直接命中（高置信）
        emoji_map = (
            ("love", ("❤", "💕", "💖", "🥰", "😍", "😘", "💗", "💞")),
            ("happy", ("😂", "🤣", "😆", "😄", "😁", "😺", "🤪")),
            ("cute", ("🥰", "🌸", "🐱", "🐶", "🥺")),
            ("wink", ("😉", "😏", "😜", "😋", "🤭")),
            ("thinking", ("🤔", "💭", "🧐")),
            ("sad", ("😢", "😭", "💔", "😔", "😞")),
            ("awkward", ("😅", "🥲", "😬", "🙃", "🤦", "💦")),
            ("angry", ("😠", "😡", "💢", "🤬")),
        )
        for cat, emojis in emoji_map:
            if any(e in reply_text for e in emojis):
                return cat
        # 关键词兜底（沿用 sticker 分类词典）
        for cat, kws in self._STICKER_CATEGORY_KEYWORDS.items():
            if any(kw.lower() in text for kw in kws):
                return cat
        return None

    def _pick_sticker_file(
        self,
        category: str,
        asset_root: Path,
        *,
        exclude_recent: Optional[List[str]] = None,
    ) -> Optional[str]:
        """从 category 目录随机选一张 sticker 文件。

        P2++F3 (2026-05-04)：exclude_recent 排除最近用过的（per-chat 去重，
        避免连续发同一张）。如果 cat 内全部被排除（< 3 张时可能发生），fallback
        到全部候选。
        """
        cat_dir = asset_root / category
        if not cat_dir.exists() or not cat_dir.is_dir():
            return None
        files = list(cat_dir.glob("*.png")) + list(cat_dir.glob("*.webp"))
        if not files:
            return None
        if exclude_recent:
            exclude_set = {str(p) for p in exclude_recent}
            filtered = [f for f in files if str(f) not in exclude_set]
            if filtered:
                files = filtered
            # 否则保留全部（cat 内文件少于 recent 长度时的兜底）
        import random as _rd
        return str(_rd.choice(files))

    def _decide_reply_modality(
        self,
        *,
        reply_text: str,
        peer_msg: PeerMessage,
        peer_sticker_cat: Optional[str],
        multi_peer_count: int,
        chat_key: str,
        caption: Optional[str] = None,
    ) -> Dict[str, Any]:
        """决定本条 reply 的 modality（text / sticker / text+sticker）。

        返回 dict:
          {
            "modality": "text" | "sticker" | "text+sticker",
            "sticker_path": str | None,
            "sticker_cat": str | None,
            "reason": str,           # 决策原因，进 hint
            "dry_run": bool,
            "would_send": bool,      # dry_run 模式下是否本应发送
          }
        失败一律降级 text。
        """
        cfg = self._cfg.get("sticker_reply") or {}
        if not isinstance(cfg, dict) or not cfg.get("enabled", False):
            return {"modality": "text", "reason": "sticker_disabled",
                    "sticker_path": None, "sticker_cat": None,
                    "dry_run": True, "would_send": False}

        try:
            dry_run = bool(cfg.get("dry_run", True))
            asset_root = Path(cfg.get("asset_root") or "config/stickers")
            cooldown = float(cfg.get("cooldown_after_sec", 90) or 0)
            max_per_24h = int(cfg.get("max_per_chat_per_24h", 12) or 0)
            probs = cfg.get("probabilities") or {}
            now = time.time()

            # 1) cooldown 检查
            bucket = self._sticker_send_history.get(chat_key) or []
            if bucket and (now - bucket[-1]) < cooldown:
                wait = cooldown - (now - bucket[-1])
                return {"modality": "text",
                        "reason": f"cooldown:{wait:.0f}s",
                        "sticker_path": None, "sticker_cat": None,
                        "dry_run": dry_run, "would_send": False}

            # 2) 24h cap 检查
            cnt_24h = self._sticker_24h_count(chat_key)
            if max_per_24h > 0 and cnt_24h >= max_per_24h:
                return {"modality": "text",
                        "reason": f"24h_cap:{cnt_24h}/{max_per_24h}",
                        "sticker_path": None, "sticker_cat": None,
                        "dry_run": dry_run, "would_send": False}

            # 3) 按 peer_kind 选概率 + 候选 cat + 拟定 modality
            peer_kind = (peer_msg.kind or "").lower() if peer_msg else "text"
            if peer_kind in ("sticker", "animated_sticker"):
                prob = float(probs.get("peer_sticker_reply", 0.30) or 0)
                target_cat = peer_sticker_cat or "happy"
                modality = "sticker"
            elif peer_kind in ("image", "video", "gif"):
                prob = float(probs.get("peer_image_video", 0.0) or 0)
                target_cat = "happy"
                modality = "text"
            else:  # text / link / other
                if multi_peer_count and multi_peer_count >= 3:
                    prob = float(probs.get("multi_peer_burst", 0.15) or 0)
                else:
                    prob = float(probs.get("peer_text_attached", 0.05) or 0)
                # P2++H1 (2026-05-04) 修反转 bug：reply_text 优先（bot 自己写
                # 的情绪 = bot 应表达的情绪），caption 仅作 fallback（peer 内容，
                # 不直接反映 bot 情绪）。例：caption "thinking pose" + reply
                # "哈哈太搞笑" → bot 应该用 happy 类 sticker（自己的情绪），
                # 而不是 thinking 类（peer 的内容）。
                target_cat = (
                    self._infer_sticker_cat_from_reply(reply_text)
                    or self._infer_sticker_cat_from_reply(caption or "")
                    or "happy"
                )
                modality = "text+sticker"

            # 4) 概率过滤
            if prob <= 0:
                return {"modality": "text",
                        "reason": f"prob_zero:peer_kind={peer_kind}",
                        "sticker_path": None, "sticker_cat": None,
                        "dry_run": dry_run, "would_send": False}
            import random as _rd
            roll = _rd.random()
            if roll >= prob:
                return {"modality": "text",
                        "reason": f"prob_skip:{prob:.0%}_roll={roll:.2f}",
                        "sticker_path": None, "sticker_cat": None,
                        "dry_run": dry_run, "would_send": False}

            # 5) 选 sticker 文件（P2++F3：排除该 chat 最近 3 张，避免重复）
            recent = self._sticker_recent_per_chat.get(chat_key) or []
            sticker_path = self._pick_sticker_file(
                target_cat, asset_root, exclude_recent=recent,
            )
            if not sticker_path:
                return {"modality": "text",
                        "reason": f"asset_missing:cat={target_cat}",
                        "sticker_path": None, "sticker_cat": None,
                        "dry_run": dry_run, "would_send": False}

            # 6) 命中 → 记一笔（dry_run 也算，让 cooldown / 24h cap / recent 真实生效）
            self._record_sticker_decision(chat_key)
            recent_bucket = self._sticker_recent_per_chat.setdefault(chat_key, [])
            recent_bucket.append(sticker_path)
            if len(recent_bucket) > self._sticker_recent_max:
                recent_bucket.pop(0)
            return {
                "modality": modality,
                "sticker_path": sticker_path,
                "sticker_cat": target_cat,
                "reason": f"hit:p={prob:.0%}_cat={target_cat}_kind={peer_kind}",
                "dry_run": dry_run,
                "would_send": True,
            }
        except Exception as ex:
            logger.debug("[messenger_rpa] modality decision error: %s", ex,
                         exc_info=True)
            return {"modality": "text", "reason": f"error:{type(ex).__name__}",
                    "sticker_path": None, "sticker_cat": None,
                    "dry_run": True, "would_send": False}

    _MEDIA_ACK_TEMPLATES: Dict[str, Dict[str, List[str]]] = {
        "image": {
            "en": [
                "Got your photo - let me take a look 📷",
                "Nice pic! Let me check it out 👀",
                "Just got your photo, looking now~",
                "Photo received! Give me a sec 📸",
                "Oh nice, let me see 🙂",
            ],
            "zh": [
                "收到你的照片啦，我看看然后回你～📷",
                "哇，照片我看看～",
                "收到啦，等下细看 👀",
                "图片收到，让我瞧瞧 📸",
                "嗯嗯，我来看一下哈～",
            ],
            "ja": [
                "写真ありがとう。ちゃんと見てから返すね📷",
                "お、見てみるね〜",
                "写真届いた、ちょっと見させて👀",
                "おっ、ちょっと見るね",
                "ありがと、確認してみる📸",
            ],
        },
        "sticker": {
            "en": [
                "Haha, love that one 😄",
                "Lol nice sticker 😆",
                "Cute! 🥰",
                "Hahaha thats good",
                "Lol 😂",
            ],
            "zh": [
                "哈哈这个贴纸我爱了 😄",
                "笑死 😆",
                "好可爱～🥰",
                "哈哈哈这个好",
                "嘻嘻 😊",
            ],
            "ja": [
                "そのスタンプ、ちょっと可愛いね😄",
                "笑った😆",
                "可愛い〜🥰",
                "ええ感じやんw",
                "うふふ😊",
            ],
        },
        # P1.5-B (2026-05-04)：video / gif / animated_sticker 专属 fast-ack 模板池
        "video": {
            "en": [
                "Watching your video now 🎬",
                "Oh nice, let me play it 📹",
                "Got the video, hang on~",
                "Press play 👀, give me a sec",
                "Video received, watching!",
            ],
            "zh": [
                "视频收到啦，我看看～🎬",
                "哇视频，等我点开 📹",
                "好的好的，我播一下",
                "嗯嗯，让我看完再回 👀",
                "视频收到，正在看～",
            ],
            "ja": [
                "動画ありがとう、再生してみるね🎬",
                "お、見てみる📹",
                "動画届いた、ちょっと見させて",
                "了解、再生中～👀",
                "あとで返すね、まず見るね",
            ],
        },
        "gif": {
            "en": [
                "Lol nice GIF 🤣",
                "Hahaha that GIF tho 😆",
                "Omg this is gold",
                "Hahaha I needed that",
                "Lmao perfect reaction",
            ],
            "zh": [
                "哈哈这个 GIF 太搞笑了 🤣",
                "笑死 😆",
                "这个动图绝了哈哈",
                "哈哈哈我喜欢",
                "笑喷，这个 reaction 满分",
            ],
            "ja": [
                "このGIFｗｗ🤣",
                "ウケる😆",
                "それ最高ｗ",
                "笑った笑った",
                "ナイスリアクション",
            ],
        },
        "animated_sticker": {
            "en": [
                "Aw that animated one 🥰",
                "Cute moving sticker!",
                "Haha I see what you did there 😆",
                "Love the animation",
            ],
            "zh": [
                "动起来好可爱 🥰",
                "这个动图贴纸不错",
                "嘻嘻 😆",
                "动着看更可爱～",
            ],
            "ja": [
                "動くやつ可愛い🥰",
                "それ動くんだ",
                "うふふ😆",
                "動いてるの良いね",
            ],
        },
        "voice": {
            "en": [
                "Heard your voice note, give me a sec 🎙️",
                "Voice note received, listening now~",
                "Got it, let me hear it 🎧",
                "On it, give me a moment to play it back",
            ],
            "zh": [
                "收到你的语音，我等下认真听一遍再回你哈 🎙️",
                "好的，听一下你的语音～",
                "收到收到，我听一下 🎧",
                "嗯嗯，等我听完",
            ],
            "ja": [
                "ボイスありがとう。ちゃんと聞いてから返すね🎙️",
                "了解、ちょっと聞かせて〜",
                "おけ、聞いてみる 🎧",
                "ボイス届いた、ちょっと再生させて",
            ],
        },
        "file": {
            "en": [
                "Thanks for the file 📎",
                "Got the file, taking a look 📂",
                "Received, will review now",
            ],
            "zh": [
                "收到文件啦，我这边看一下～📎",
                "嗯嗯，文件我打开看看 📂",
                "收到，等下我看",
            ],
            "ja": [
                "ファイルありがとう。確認してみるね📎",
                "ファイル届いた、開いてみる📂",
                "了解、見てみるね",
            ],
        },
        "link": {
            "en": [
                "Got the link, opening it now 🔗",
                "Cool link, checking it out",
                "Received, opening~",
            ],
            "zh": [
                "链接收到了，我打开看看 🔗",
                "好的，我点进去看看",
                "嗯嗯，打开了～",
            ],
            "ja": [
                "リンクありがとう。開いて見てみるね🔗",
                "了解、開いてみる〜",
                "リンク届いた、見てみるね",
            ],
        },
        "other": {
            "en": [
                "Got it, let me respond properly in a bit 👀",
                "Received, give me a sec",
                "Noted, let me get back to you~",
            ],
            "zh": [
                "收到啦，我等下好好回你 👀",
                "嗯嗯，等我回你",
                "好的，稍等",
            ],
            "ja": [
                "受け取ったよ。少し見てからちゃんと返すね👀",
                "了解、ちょっと待って",
                "うん、後でゆっくり返すね",
            ],
        },
    }

    def _maybe_media_ack(
        self, peer_msg, chat_name: str
    ) -> Tuple[Optional[str], str]:
        """若 peer 消息是媒体且策略为 ack_only / ack_and_approve，返回 (reply, policy)。

        否则返回 (None, '')，调用方继续走 AI 生成链路。
        """
        policy = str(
            self._cfg.get("media_handling_policy", "ai")
        ).strip().lower()
        if policy not in ("ack_only", "ack_and_approve"):
            return None, ""

        kind = str(peer_msg.kind or "").lower()
        # link 本身文本有信息价值，AI 通常能回好；只对无文本媒体启用
        # P1.5-B (2026-05-04)：补 gif / animated_sticker
        media_kinds = {
            "image", "sticker", "voice", "file", "video", "other",
            "gif", "animated_sticker",
        }
        # 允许 config 开关是否覆盖 link
        if bool(self._cfg.get("media_include_links", False)):
            media_kinds.add("link")
        if kind not in media_kinds:
            return None, ""
        voice_cfg = self._cfg.get("voice_input") or {}
        if (
            kind == "voice"
            and isinstance(voice_cfg, dict)
            and voice_cfg.get("enabled", False)
            and voice_cfg.get("prefer_transcribe", True) is not False
        ):
            return None, ""

        # 语种：优先使用 force / profile / 历史回复语言，再 fallback 到 detection
        # Step-1: global forced language
        _force = str(self._cfg.get("force_reply_lang") or "").strip().lower()
        if _force and _force not in ("auto", "detect", ""):
            preferred_lang = _force
        else:
            # Step-2: per-profile forced language
            _profile_lang = ""
            try:
                _profiles = self._cfg.get("reply_profiles") or {}
                if isinstance(_profiles, dict):
                    _default_p = _profiles.get("default") or ""
                    _profs = _profiles.get("profiles") or []
                    for _pp in (_profs if isinstance(_profs, list) else []):
                        if isinstance(_pp, dict) and _pp.get("id") == _default_p:
                            _profile_lang = str(_pp.get("language") or "").strip().lower()
                            break
            except Exception:
                pass
            if _profile_lang and _profile_lang not in ("auto", "detect", ""):
                preferred_lang = _profile_lang
            else:
                # Step-3: previous reply lang for this chat
                chat_key = self._chat_key_for(chat_name)  # P0-E2 OCR 容忍
                _prev_lang = self._previous_reply_lang(chat_key)
                if _prev_lang:
                    preferred_lang = _prev_lang
                else:
                    # Step-4: language detection (auto mode) or default_reply_lang
                    lang_mode = str(
                        self._cfg.get("language_alignment", "english_fallback_only")
                    ).strip().lower()
                    default_lang = str(
                        self._cfg.get("default_reply_lang") or ""
                    ).lower()
                    if lang_mode == "auto":
                        raw = (peer_msg.raw or "") + " " + (peer_msg.desc or "")
                        ai = getattr(self._sm, "ai_client", None)
                        detected = _detect_peer_lang(raw, ai_client=ai)
                        if detected in ("zh", "ja", "ko"):
                            preferred_lang = detected
                        elif default_lang:
                            preferred_lang = default_lang
                        else:
                            preferred_lang = "en"
                    elif default_lang:
                        preferred_lang = default_lang
                    else:
                        preferred_lang = "en"
        lang = preferred_lang

        # P4-4 (2026-05-04)：模板配置化——优先读 config.yaml 的
        # messenger_rpa.media_ack_templates，运营改文案不用改代码。
        # config 结构与 _MEDIA_ACK_TEMPLATES 同：dict[kind][lang] = list[str]
        # （也兼容 dict[kind][lang] = str 单条）。refresh_cfg 每轮调用让改动
        # 自动生效，无需重启。
        cfg_tbl = self._cfg.get("media_ack_templates") or {}
        # P4-3 (2026-05-04) sticker 风格化：根据 peer_msg.desc/content keyword
        # 分类，命中类别用专属模板池（笑/爱心/哀伤/愤怒/可爱）。失败退回通用。
        sticker_cat = None
        if kind == "sticker":
            sticker_cat = self._classify_sticker_category(
                getattr(peer_msg, "desc", "") or "",
                getattr(peer_msg, "content", "") or "",
            )
            if sticker_cat:
                logger.info(
                    "[messenger_rpa] sticker_category=%s desc=%r → 风格化模板",
                    sticker_cat, (getattr(peer_msg, "desc", "") or "")[:80],
                )
        if sticker_cat and sticker_cat in self._STICKER_CATEGORY_TEMPLATES:
            tbl = self._STICKER_CATEGORY_TEMPLATES[sticker_cat]
        elif isinstance(cfg_tbl, dict) and isinstance(cfg_tbl.get(kind), dict):
            tbl = cfg_tbl[kind]
        else:
            tbl = self._MEDIA_ACK_TEMPLATES.get(kind) or self._MEDIA_ACK_TEMPLATES["other"]
        # P4-1: 每个 lang 一组模板列表，随机挑一条避免每次相同（"重复套话"感）
        candidates = tbl.get(lang) or tbl.get("en") or ["Got it!"]
        if isinstance(candidates, str):
            reply = candidates
        else:
            import random as _rd
            # 避开上一条 last_reply 重复
            try:
                _ck_pa = self._chat_key_for(chat_name)
                _prev_reply_pa = (
                    self._state.get_chat_state(_ck_pa).get("last_reply") or ""
                ).strip()
            except Exception:
                _prev_reply_pa = ""
            pool = [c for c in candidates if c.strip() != _prev_reply_pa] or list(candidates)
            reply = _rd.choice(pool)
        logger.info(
            "[messenger_rpa] media ack chat=%s kind=%s policy=%s lang=%s → %r",
            chat_name, kind, policy, lang, reply[:60],
        )
        return reply, policy

    # ── 内部：typing 指示 ─────────────────────────
    async def _typing_indicator_burst(
        self, serial: str, wh: Tuple[int, int], duration_sec: float = 2.5,
    ) -> None:
        """W2-D3.1：发送前的短"在输入"脉冲（典型 1.5-3 秒）。

        设计差异 vs _typing_indicator_pulse：
        - 这个 burst 在「真发前一刻」启动，让对方在收到消息前几秒看到"在输入..."
        - 持续时长有限（duration_sec），到时自动结束（不依赖 cancel）
        - 真人聊天就是这种节奏：开始打字 → 几秒后发送
        """
        mode = str(self._cfg.get("typing_indicator_mode", "focus_only")).strip().lower()
        if mode == "off" or duration_sec <= 0.5:
            return
        try:
            tx, ty = cc.INPUT_TEXT_FIELD.at(*wh)
        except Exception:
            return
        try:
            await asyncio.to_thread(adb.input_tap, serial, tx, ty)
        except Exception:
            return
        try:
            t_end = time.monotonic() + max(0.5, float(duration_sec))
            interval = 1.5  # 每 1.5s 一次维持
            while time.monotonic() < t_end:
                remain = t_end - time.monotonic()
                await asyncio.sleep(min(interval, max(0.1, remain)))
                if mode == "focus_only":
                    try:
                        await asyncio.to_thread(adb.input_tap, serial, tx, ty)
                    except Exception:
                        pass
                elif mode == "keystroke":
                    try:
                        await asyncio.to_thread(adb.input_text, serial, " ")
                        await asyncio.to_thread(adb.input_keyevent, serial, 67)  # DEL
                    except Exception:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("_typing_indicator_burst 异常", exc_info=True)

    async def _typing_indicator_pulse(
        self, serial: str, wh: Tuple[int, int]
    ) -> None:
        """在 AI 生成期间给 peer 制造 "typing..." 与 "online" 的感知。

        三档模式（messenger_rpa.typing_indicator_mode）：
          off          完全禁用
          focus_only   只 tap 输入框（弹键盘、保持 online）；不干扰 compose 内容；
                       无法可靠触发 peer 端的 "typing..." 指示，但至少减少
                       "沉默 30-60s" 的尴尬（推荐默认）
          keystroke    tap + 周期发送 ' '+KEYCODE_DEL：真实触发 typing 指示；
                       有极小概率在被 cancel 的瞬间残留 1 字符（随后 inject_text
                       会先清空输入框再注入，残留也会被 AdbKeyboard/剪贴板路径覆盖）

        通用保护：
          - 首次 tap 立即执行（尽早让 peer 看到 online / 输入焦点）
          - interval 默认 6s（Messenger typing 指示客户端 10-15s 超时，6s 留足余量）
          - 最多 max_pulses 次（默认 20 次 ≈ 2 分钟，超过就停，避免 AI 卡死时
            给 peer 发假 typing 假无止境）
          - 被 cancel 时安静退出；异常全部吞掉、只打 debug 日志，绝不影响主流程
        """
        mode = str(
            self._cfg.get("typing_indicator_mode", "focus_only")
        ).strip().lower()
        if mode == "off":
            return
        if mode not in ("focus_only", "keystroke"):
            logger.debug("typing_indicator_mode 未知值 %r → focus_only", mode)
            mode = "focus_only"

        try:
            tx, ty = cc.INPUT_TEXT_FIELD.at(*wh)
        except Exception:
            logger.debug("typing_pulse: coord 解析失败", exc_info=True)
            return

        interval = float(self._cfg.get("typing_indicator_interval_sec", 6.0))
        max_pulses = int(self._cfg.get("typing_indicator_max_pulses", 20))

        # 第一次 tap 立即、必然执行
        try:
            await asyncio.to_thread(adb.input_tap, serial, tx, ty)
        except Exception:
            logger.debug("typing_pulse: 首次 tap 失败", exc_info=True)
            return

        try:
            for _ in range(max_pulses):
                await asyncio.sleep(interval)
                if mode == "focus_only":
                    try:
                        await asyncio.to_thread(adb.input_tap, serial, tx, ty)
                    except Exception:
                        logger.debug("typing_pulse: 维持 tap 失败", exc_info=True)
                elif mode == "keystroke":
                    # 成对发：一个字符 + 立即删除，净效果为 0，但触发 typing event
                    # try/finally 保证被 cancel 时 DEL 一定执行，避免残留空格
                    pushed = False
                    try:
                        await asyncio.to_thread(
                            adb.run_adb,
                            ["shell", "input", "text", " "],
                            serial=serial, timeout=6.0,
                        )
                        pushed = True
                    except Exception:
                        logger.debug("typing_pulse: 空格失败", exc_info=True)
                    finally:
                        if pushed:
                            try:
                                await asyncio.to_thread(
                                    adb.run_adb,
                                    ["shell", "input", "keyevent", "67"],  # KEYCODE_DEL
                                    serial=serial, timeout=6.0,
                                )
                            except Exception:
                                logger.debug("typing_pulse: DEL 失败", exc_info=True)
        except asyncio.CancelledError:
            # 兜底：cancel 时再补 3 个 DEL，覆盖任何极端情况下的残留字符
            if mode == "keystroke":
                try:
                    for _ in range(3):
                        await asyncio.to_thread(
                            adb.run_adb,
                            ["shell", "input", "keyevent", "67"],
                            serial=serial, timeout=4.0,
                        )
                except Exception:
                    logger.debug("typing_pulse: cancel 清理失败", exc_info=True)
            return
        except Exception:
            logger.debug("typing_pulse: 异常退出", exc_info=True)

    # ── 内部：chat 入口缓存 ───────────────────────
    def _chat_entry_cache_ttl(self) -> float:
        """缓存有效期。短一点更安全（避免 chat 在 inbox 移位后误点）。"""
        return float(self._cfg.get("chat_entry_cache_ttl_sec", 300.0) or 300.0)

    def _cached_chat_entry(
        self, serial: str, chat_name: str,
    ) -> Optional[Tuple[int, int, float, str]]:
        if not bool(self._cfg.get("send_to_chat_entry_cache", True)):
            return None
        key = (serial, (chat_name or "").strip())
        if not key[1]:
            return None
        rec = self._chat_entry_cache.get(key)
        if not rec:
            return None
        if time.time() - rec[2] > self._chat_entry_cache_ttl():
            self._chat_entry_cache.pop(key, None)
            return None
        return rec

    def _record_chat_entry(
        self, serial: str, chat_name: str,
        tap_x: int, tap_y: int, source: str,
    ) -> None:
        if not bool(self._cfg.get("send_to_chat_entry_cache", True)):
            return
        chat_name = (chat_name or "").strip()
        if not chat_name:
            return
        self._chat_entry_cache[(serial, chat_name)] = (
            int(tap_x), int(tap_y), time.time(), source,
        )

    def _invalidate_chat_entry(self, serial: str, chat_name: str) -> None:
        self._chat_entry_cache.pop(
            (serial, (chat_name or "").strip()), None,
        )

    # ── 内部：配置访问 ────────────────────────────
    def _vision_cfg(self) -> Dict[str, Any]:
        # 允许 messenger_rpa 段内自定义 vision 覆盖；否则用全局 vision
        local = self._cfg.get("vision") or {}
        if local:
            merged = dict(self._global_vision_cfg())
            merged.update(local)
            return merged
        return self._global_vision_cfg()

    def _global_vision_cfg(self) -> Dict[str, Any]:
        try:
            return self._cm.config.get("vision") or {}
        except Exception:
            return {}

    # ── 内部：收尾 ────────────────────────────────
    def _finish(
        self, result: Dict[str, Any], t0: float
    ) -> Dict[str, Any]:
        # ★ 跨账号协调器：释放聊天锁（所有 return 路径都经过 _finish）
        _held_ext_id = result.pop("_coord_lock_held", None)
        if _held_ext_id and self._coordinator is not None:
            _my_aid = str(getattr(self, "_account_id", "") or "default")
            self._coordinator.unlock(_held_ext_id, _my_aid)
        # ★ W3-D3.6：清理挂在 result 上的未 await 的 prefetch task
        # 避免 "Task was destroyed but it is pending" warning 和资源泄漏
        for _key in ("_peer_typing_prefetch_task", "_cap_task"):
            _task = result.pop(_key, None)
            if _task is not None and not _task.done():
                try:
                    _task.cancel()
                except Exception:
                    pass
        result["total_ms"] = int(round((time.monotonic() - t0) * 1000))
        # ★ 统一收尾日志：所有 run_once 出口都经过这里
        _step = result.get("step", "?")
        _ok = result.get("ok")
        _chat = result.get("chat_name", "")
        _err = (result.get("error") or "")[:100]
        logger.warning(
            "[messenger_rpa] ── run_once 结束 step=%s ok=%s chat=%r ms=%d err=%s",
            _step, _ok, _chat, result["total_ms"], _err or "-",
        )
        try:
            self._state.append_run(result)
        except Exception:
            logger.debug("append_run 失败", exc_info=True)
        # ★ P3-4：进程级 metrics
        try:
            from src.integrations.messenger_rpa.metrics import get_metrics
            get_metrics().observe_run(result)
        except Exception:
            logger.debug("observe_run 失败", exc_info=True)
        # ★ P3-7：异常自动打包（run 失败时把中间态打 zip）
        try:
            if result.get("error") and not result.get("_replay_skipped"):
                from src.integrations.messenger_rpa.replay import maybe_pack_run
                maybe_pack_run(result, self._cfg)
        except Exception:
            logger.debug("replay.maybe_pack_run 失败", exc_info=True)
        return result
