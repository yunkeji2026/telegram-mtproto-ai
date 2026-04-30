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
    """
    lr = _compact_for_self_overlap(last_reply)
    pc = _compact_for_self_overlap(peer_text)
    if len(lr) < 12 or len(pc) < 12:
        return 0.0
    if pc in lr or lr in pc:
        return 1.0

    words_lr = set(re.findall(r"[a-z0-9]{2,}", (last_reply or "").lower()))
    words_pc = set(re.findall(r"[a-z0-9]{2,}", (peer_text or "").lower()))
    if len(words_pc) >= 3 and words_lr:
        return len(words_lr & words_pc) / len(words_pc)

    n = 3 if len(pc) >= 18 else 2
    lr_grams = {lr[i:i + n] for i in range(0, max(0, len(lr) - n + 1))}
    pc_grams = {pc[i:i + n] for i in range(0, max(0, len(pc) - n + 1))}
    if not lr_grams or not pc_grams:
        return 0.0
    return len(lr_grams & pc_grams) / len(pc_grams)


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

    def refresh_cfg(self, new_cfg: Dict[str, Any]) -> None:
        """热重载 runner 配置（service drain/run_once 每轮调用）。
        使 config.yaml 修改无需重启即对 pacing / gate / language 生效。
        """
        if new_cfg:
            self._cfg = dict(new_cfg)
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

    @staticmethod
    def _chat_name_matches_any(chat_name: str, names: List[str]) -> bool:
        cn = (chat_name or "").strip().lower()
        if not cn:
            return False
        for raw in names:
            want = (raw or "").strip().lower()
            if want and (cn == want or want in cn or cn in want):
                return True
        return False

    def _thread_title_from_xml(self, serial: str, result: Dict[str, Any]) -> str:
        try:
            from src.integrations.messenger_rpa import thread_actions as _ta
            from src.integrations.messenger_rpa import ui_scraper as _uis

            xml = _ta.dump_view_tree(
                serial,
                dump_timeout=float(self._cfg.get("ui_dump_timeout_s") or 6.0),
                cat_timeout=4.0,
            )
            title = (_uis.find_thread_title(xml) or "").strip() if xml else ""
            if title:
                result["thread_title_xml"] = title
            return title
        except Exception:
            logger.debug("[messenger_rpa] thread title xml read failed", exc_info=True)
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

            if not self._foreground_messenger(serial, result):
                return self._finish(result, t0)

            inbox_png = await self._screenshot(serial, "inbox", run_id)
            if not inbox_png:
                result["step"] = "screenshot_inbox_failed"
                return self._finish(result, t0)
            result["screenshot_path"] = inbox_png

            # ── 自动校准（首次、像素级、~200ms）──
            self._maybe_auto_calibrate(serial, wh, inbox_png, result)

            # ── inbox guard + 未读扫描 ──
            if self._use_combined_vision:
                guard, unread = await self._inbox_combined(inbox_png, result)
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
                    inbox_png = await self._screenshot(serial, "inbox_retry", run_id)
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
                    inbox_png = await self._screenshot(serial, "inbox_retry", run_id)
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
            target: Optional[UnreadChat] = None
            skipped_names: List[str] = []
            skipped_self_previews: List[str] = []
            target_names = self._run_once_target_names()
            if target_names:
                result["target_chat_names"] = target_names
            for c in unread:
                if target_names and not self._chat_name_matches_any(c.name, target_names):
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
            if target is None:
                result["step"] = "all_unread_skipped"
                result["ok"] = True
                return self._finish(result, t0)
            chat_key = f"{self._chat_key_prefix}:{target.name}"
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

            self._tap_chat_row(serial, wh, target)
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

            actual_title = self._thread_title_from_xml(serial, result)
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
                chat_key = f"{self._chat_key_prefix}:{target.name}"
                result["chat_key"] = chat_key
                result["chat_name"] = target.name

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

            # Hard guard: UI XML is more reliable than Vision for "who spoke last".
            # If the latest visible thread snippet starts with "You:" / "你:" etc.,
            # the newest message is ours, so do not let a Vision role mistake create
            # self-question/self-answer loops.
            if self._latest_thread_snippet_is_self(serial, result):
                result["step"] = "self_latest_xml_skip"
                result["ok"] = True
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
            if not peer_msg or not peer_msg.is_peer_anything:
                result["step"] = "no_peer_message"
                result["ok"] = True
                self._exit_thread(serial)
                return self._finish(result, t0)

            if self._latest_thread_snippet_is_self(serial, result):
                result["step"] = "self_latest_xml_skip"
                result["ok"] = True
                self._exit_thread(serial)
                return self._finish(result, t0)

            result["peer_text"] = peer_msg.to_text_for_ai()
            result["peer_kind"] = peer_msg.kind

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
                    self._exit_thread(serial)
                    return self._finish(result, t0)

            fp = fingerprint(peer_msg)
            if self._state.is_duplicate(chat_key, fp):
                result["step"] = "duplicate_skip"
                result["ok"] = True
                self._exit_thread(serial)
                return self._finish(result, t0)

            _chat_st = self._state.get_chat_state(chat_key)

            # ★ 反幻觉：Vision 有时把己方最后一条消息误识为对方消息（气泡颜色/位置判断失误）。
            # 若"对方消息"内容与我方 last_reply 高度重叠，则判定为误读，跳过。
            if peer_msg.kind == "text":
                _lr_raw = (_chat_st.get("last_reply") or "").strip()
                _pc_raw = (peer_msg.content or "").strip()
                _self_overlap = _self_reply_overlap_ratio(_lr_raw, _pc_raw)
                result["self_reply_overlap"] = round(_self_overlap, 3)
                if _self_overlap >= 0.7:
                    result["step"] = "self_message_skip"
                    result["ok"] = True
                    result["error"] = (
                        f"vision_misread_self_as_peer: overlap={_self_overlap:.2f} "
                        f"peer={_pc_raw[:60]!r}"
                    )
                    logger.warning(
                        "[messenger_rpa] vision 把己方消息误识为 peer，跳过 "
                        "chat=%s overlap=%.2f peer=%r",
                        chat_key, _self_overlap, _pc_raw[:80],
                    )
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
                    )
                except Exception:
                    logger.debug("update_chat_state 失败", exc_info=True)
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
                if not search_first_tried:
                    target = await self._search_chat_by_name(
                        serial, wh, chat_name, result,
                    )
                    if target:
                        just_verified_ts = time.time()
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
        """
        from src.integrations.messenger_rpa import thread_actions as _ta
        from src.integrations.messenger_rpa import ui_scraper as _uis
        from src.integrations.messenger_rpa.text_input import inject_text

        name = (chat_name or "").strip()
        if not name:
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
            for _ in range(72):
                adb.input_keyevent(serial, "KEYCODE_DEL")
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
                for i in range(3):
                    rx, ry = cc.chat_row_for(i, width=wh[0], height=wh[1])
                    taps.append((rx, ry, f"coord_row{i}"))
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
                return None
            await asyncio.sleep(1.4)

            for _round in range(4):
                plan = _build_tap_plan()
                if not plan:
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
            return None
        except Exception as ex:
            logger.warning("[messenger_rpa] _search_chat_by_name 异常: %s", ex)
            result.setdefault("hints", []).append(
                f"search_chat_by_name_exc:{type(ex).__name__}",
            )
            return None

    # ── 内部：设备/屏幕 ───────────────────────────
    def _resolve_serial(self, result: Dict[str, Any]) -> Optional[str]:
        cfg_serial = (self._cfg.get("adb_serial") or "").strip()
        if cfg_serial:
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
                if not healthy:
                    result["step"] = "device_unhealthy"
                    err_attempts = info.get("attempts") or []
                    last_err = (
                        err_attempts[-1].get("error", "") if err_attempts else ""
                    )
                    result["error"] = f"device_health: {last_err}"
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
    ) -> bool:
        """Cheap hard guard against replying to our own latest thread message."""
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
            row = _uis.latest_snippet_row(xml)
            if row is None:
                result.setdefault("hints", []).append("thread_self_xml_guard:no_snippet")
                return False
            result["thread_latest_preview"] = row.preview[:200]
            result["thread_latest_is_self"] = bool(row.is_self_last)
            result["thread_latest_bounds"] = row.bounds.as_tuple()
            result["thread_latest_in_thread"] = bool(in_thread)
            if row.is_self_last:
                logger.warning(
                    "[messenger_rpa] latest thread snippet is self; skip reply "
                    "preview=%r bounds=%s",
                    row.preview[:120], row.bounds.as_tuple(),
                )
                return True
        except Exception:
            logger.debug("thread_self_xml_guard failed", exc_info=True)
            result.setdefault("hints", []).append("thread_self_xml_guard:error")
        return False

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

    async def _inbox_combined(
        self,
        inbox_png: str,
        result: Dict[str, Any],
        retry: bool = False,
        *,
        max_rows: Optional[int] = None,
    ) -> Tuple[Any, List[UnreadChat]]:
        """单次 vision 同时拿 inbox guard + 未读列表。"""
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
        # ★ P3-1：inbox 也可能暴露风控 banner，同 thread 一样处理
        risk = getattr(cr, "risk", None)
        if risk is not None and risk.hit:
            self._handle_risk_hit(risk, result=result, where="inbox")

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

    async def _thread_combined(
        self, thread_png: str, result: Dict[str, Any]
    ) -> Tuple[Any, Optional[PeerMessage]]:
        cr, tag = await analyze_thread_combined(
            thread_png,
            vision_cfg=self._vision_cfg(),
            global_vision=self._global_vision_cfg(),
        )
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
        risk = getattr(cr, "risk", None)
        if risk is not None and risk.hit:
            self._handle_risk_hit(risk, result=result, where="thread")
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
                    dump_inbox_rows, find_row_by_preview, find_row_by_name,
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
                # 2. （已弃用）按 chat name 在 raw_desc 里匹配 — 真机调研发现
                #     messenger 的 inbox row content-desc 是
                #     "X.2Wn@HASH, SimpleTextThreadSnippet(text=...)" 格式，
                #     **完全不含 sender name**，所以这种匹配永远 None。保留接口
                #     但实际不工作。下一步要么改用截图 OCR 头像名字，要么完全靠
                #     row_index 对齐（见下面 stories-aware 修复）。
                if target_ui is None and chat.name:
                    target_ui = find_row_by_name(ui_rows, chat.name)
                    if target_ui is not None:
                        match_src = f"name_match_in_desc"
                # 3. ★★★ 真机暴露 row_index 偏移 bug ★★★
                #    vision 看 inbox 时把 Stories 头像行算 row 0，第一个 chat 算 row 1
                #    UI XML 抓的 ui_rows 只含 chat 行，不含 Stories
                #    → vision row_index=N 实际对应 ui_rows[N-1]
                #    fix: 先试 row_index-1（stories-aware），再 fallback 原 row_index
                #    ★ 真机进一步暴露：messenger 未读 chat 行可能用不同 view layout
                #      → ui_rows 完全没那行 → ui_rows[N-1] 仍指向错位 chat
                #      所以拿到 candidate 后，必须用 chat.preview 做 overlap 验证。
                #      不沾边 → 放弃 UI XML，走公式坐标（用 vision row_index 直接算）
                if target_ui is None and chat.row_index > 0 \
                        and (chat.row_index - 1) < len(ui_rows):
                    cand = ui_rows[chat.row_index - 1]
                    cand_prev = (cand.preview or "").strip().lower()
                    vis_prev = (chat.preview or "").strip().lower()
                    # 至少 3 字符相互含 → 才认为是同一 chat
                    overlap_ok = (
                        len(vis_prev) >= 3
                        and len(cand_prev) >= 3
                        and (vis_prev[:5] in cand_prev or cand_prev[:5] in vis_prev)
                    )
                    if overlap_ok:
                        target_ui = cand
                        match_src = f"row_index_stories_aware({chat.row_index}-1)"
                    else:
                        # 显式留 None → 后面公式坐标 fallback 会接手
                        logger.info(
                            "[messenger_rpa] stories_aware 拒收：cand_prev=%r vs vis=%r 不重叠 → 走公式",
                            cand_prev[:30], vis_prev[:30],
                        )
                # 4. 启发式：preview 匹配失败但 Vision 说 row_index=0 → 点 UI XML row 0
                if target_ui is None and chat.row_index == 0 and ui_rows:
                    target_ui = ui_rows[0]
                    match_src = "row0_heuristic"
                # 5. ★ 已删除 row_index_fallback —— 真机暴露它系统性错位（vision 算 stories
                #    UI XML 不算）。如果上面 stories_aware preview 重叠验证失败，
                #    应该让 ui_rows tap 失败 → runner 走下方公式坐标 fallback（
                #    line 2543 区块），用 vision row_index + 标定 / 公式 算坐标。
                if target_ui is not None:
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

        # ★ 真机暴露 row_index 偏移：vision 算 stories 行，所以 chat.row_index
        # 应该减 1 才是真 chat 行序号（用于公式坐标 / 校准坐标）
        adjusted_row = max(0, chat.row_index - 1)
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
        logger.info(
            "[messenger_rpa] tap chat: name=%r row_index=%d src=%s -> (%d, %d)",
            chat.name, chat.row_index, src, x, y,
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
        旧的 verified 记录就过期了，下次 verify 必须真测。
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
        default_profile: Dict[str, Any] = {}
        for raw in profiles:
            if not isinstance(raw, dict):
                continue
            pid = str(raw.get("id") or raw.get("name") or "").strip()
            if default_id and pid == default_id:
                default_profile = raw
            keys = raw.get("match_chat_keys") or []
            names = raw.get("match_names") or []
            if isinstance(keys, str):
                keys = [keys]
            if isinstance(names, str):
                names = [names]
            if any(str(k).strip().lower() and str(k).strip().lower() in chat_key_l for k in keys):
                return raw
            if any(str(n).strip().lower() and str(n).strip().lower() in chat_name_l for n in names):
                return raw
        return default_profile

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
        """
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
        self, image_path: str, *, timeout_sec: float = 8.0
    ) -> str:
        """同步调用：返回图片 caption 或空串。超时/失败都静默返回空串。"""
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
            vision_cfg = self._cfg.get("vision") or {}
            global_vision = {}
            try:
                full = (self._cm.config or {}) if hasattr(self._cm, "config") else {}
                global_vision = full.get("vision") or {}
            except Exception:
                pass
            lang = self._deep_lang()
            caption, tag = await asyncio.wait_for(
                describe_peer_image_detail(
                    image_path,
                    vision_cfg=vision_cfg,
                    global_vision=global_vision,
                    language=lang,
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

        # ★ P2-1 + P3-3：图片深度理解
        # - 若 runner 已经乐观并发预跑了 caption_task（P3-3），直接 await 它拿结果
        # - 否则走 P2-1 老路径，同步调一次 vision
        # 必须在 P2-2 多消息合并之前做，确保合并后 "[图片：caption]" 能进入最终 prompt
        if peer_msg.kind == "image":
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
            if not caption:
                caption = await self._try_describe_peer_image(
                    result.get("screenshot_path", ""),
                    timeout_sec=self._deep_timeout(),
                )
                if caption and result.get("caption_source") not in ("prefetch",):
                    result["caption_source"] = "sync"
            if caption:
                text_for_ai = f"[图片：{caption}]"
                result["image_caption"] = caption
                logger.info(
                    "[messenger_rpa] P2-1 image caption chat=%s src=%s caption=%r",
                    chat_key, result.get("caption_source", "?"), caption[:100],
                )
        else:
            # 非 image：把预跑的 caption_task cancel 掉（token 已花，但本次不等）
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
        if bool(self._cfg.get("suppress_global_ai_identity", True)):
            ctx["suppress_global_ai_identity"] = True
        if bool(self._cfg.get("disable_episodic_memory", True)):
            ctx["disable_episodic_memory"] = True
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

    # ── P7-3：递进降级的发送重试 wrapper ─────────────
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
            settle_sec=float(self._cfg.get("inject_verify_settle_sec", 0.85) or 0.85),
            tolerate_truncation_chars=int(
                self._cfg.get("inject_verify_tol_chars", 2) or 2,
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
    _MEDIA_ACK_TEMPLATES: Dict[str, Dict[str, str]] = {
        "image": {
            "en": "Got your photo — let me take a look and get back to you 📷",
            "zh": "收到你的照片啦，我看看然后回你～📷",
        },
        "sticker": {
            "en": "Haha, love that one 😄",
            "zh": "哈哈这个贴纸我爱了 😄",
        },
        "voice": {
            "en": "Heard your voice note, give me a sec to listen properly 🎙️",
            "zh": "收到你的语音，我等下认真听一遍再回你哈 🎙️",
        },
        "file": {
            "en": "Thanks for the file, I'll check it on my end 📎",
            "zh": "收到文件啦，我这边看一下～📎",
        },
        "link": {
            "en": "Got the link, opening it now 🔗",
            "zh": "链接收到了，我打开看看 🔗",
        },
        "other": {
            "en": "Got it, let me respond properly in a bit 👀",
            "zh": "收到啦，我等下好好回你 👀",
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
        media_kinds = {"image", "sticker", "voice", "file", "video", "other"}
        # 允许 config 开关是否覆盖 link
        if bool(self._cfg.get("media_include_links", False)):
            media_kinds.add("link")
        if kind not in media_kinds:
            return None, ""

        # 语种：沿用 language_alignment 语义 + 历史 peer 文本
        lang_mode = str(
            self._cfg.get("language_alignment", "english_fallback_only")
        ).strip().lower()
        prefer_zh = False
        if lang_mode == "auto":
            raw = (peer_msg.raw or "") + " " + (peer_msg.desc or "")
            ai = getattr(self._sm, "ai_client", None)
            # _MEDIA_ACK_TEMPLATES 当前只有 zh/en 两套；其他语言（ja/ko/...）走 en fallback
            prefer_zh = (_detect_peer_lang(raw, ai_client=ai) == "zh")
        # 设备发不了 unicode 时，中文模板会走 ASCII guard 降级为审批；
        # 默认还是给英文以避免 approve 堆积
        lang = "zh" if prefer_zh else "en"

        tbl = self._MEDIA_ACK_TEMPLATES.get(kind) or self._MEDIA_ACK_TEMPLATES["other"]
        reply = tbl.get(lang) or tbl.get("en") or "Got it!"
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
