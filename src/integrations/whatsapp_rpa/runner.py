"""WhatsApp RPA Runner — 读 UI → Skill/AI → 回发。

执行流程（单轮 run_once）：
  1. 确保 WhatsApp 前台
  2. uiautomator dump 聊天列表
  3. 找有未读的会话行 → 点进去
  4. dump 聊天界面 → 提取最新对方消息
  5. 去重（hash 比对）
  6. SkillManager → 生成回复
  7. 拟人节奏发送（分条 + 打字停顿 + 重试）
  8. 记录结果到 WaRpaStateStore
  9. 返回聊天列表（Back）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from src.integrations.line_rpa import adb_helpers as adb  # 复用 LINE 的 ADB 工具
from src.integrations.line_rpa import screen_ocr           # 复用截屏工具
from src.integrations.line_rpa.human_pacing import (       # 复用拟人节奏
    PacingConfig,
    jitter_ms,
    split_message,
    typing_duration_sec,
)
from src.integrations.whatsapp_rpa import ui_hierarchy as ui
from src.integrations.whatsapp_rpa.state_store import WaRpaStateStore
from src.integrations.whatsapp_rpa.intent_detector import (
    StopContactIntentDetector,
    create_detector,
)

# P15-j: 表情增强器支持
_EMOTION_ENHANCER_AVAILABLE = False
try:
    from src.skills.emotion_enhancer import EmotionEnhancer
    _EMOTION_ENHANCER_AVAILABLE = True
except Exception:
    EmotionEnhancer = None  # type: ignore

logger = logging.getLogger(__name__)

_WA_PKG_PERSONAL = "com.whatsapp"
_WA_PKG_BUSINESS = "com.whatsapp.w4b"
_WA_HOME_ACTIVITY_PERSONAL = "com.whatsapp/.Main"
_WA_HOME_ACTIVITY_BUSINESS = "com.whatsapp.w4b/.Main"
_WA_DUMP_REMOTE = "/sdcard/wa_rpa_dump.xml"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# WhatsApp 默认人设（config.yaml 未设置 reply_style_hint 时使用）
_WA_DEFAULT_STYLE_HINT = (
    "你正在通过 WhatsApp 和这个人私聊。这不是客服，是日常聊天。\n\n"
    "## 你是谁\n"
    "你有自己的生活节奏：上班/通勤/吃饭/看剧/偶尔累/偶尔发呆。你不是 24/7 待命。\n"
    "你记得跟这个人之前聊过什么；TA 提过的事你会偶尔顺嘴问一下进展。\n\n"
    "## 怎么聊\n"
    "- 一句话最多 1-2 行；偶尔一个 emoji 就够（不要堆）\n"
    "- 不要客服腔：「请问」「为您解答」「很高兴为您服务」→ 删\n"
    "- 不要总结对方刚说的话再回（像 chatbot），自然接话就行\n"
    "- 对方用什么语言你就用什么语言\n"
    "- 如果对方刚说完一件烦的事 → 先反应（「哎」「那确实累」），再说别的\n"
    "- 信息密度低 > 信息密度高。一次说一件事就好\n\n"
    "## 不要做的事\n"
    "- 不主动要对方手机号/邮箱/支付信息/任何账号\n"
    "- 不发外链/不推销/不引导付费\n"
    "- 对方问「你是不是 AI」→ 不直接承认也不强行否认：「哈哈怎么这么问」「你觉得呢」\n"
    "- 不要每条都问问题（很烦）；问问题的频率 ≤ 1/3\n"
)


# ── 用户主动指定回复检测 ─────────────────────────────────────────────────────
# 匹配诸如"回我那条"/"第一条"/"你没回那个问题"等意图，排除纯能力询问
_QUOTE_REQUEST_RE = re.compile(
    r"(回|回复)(我|我的|一下|下)?(刚才|那条|那个|上面|之前|以前)"   # 回我那条 / 回刚才那个
    r"|(那条|那个)(消息|问题)?.{0,5}(没?回|回复)"                   # 那条消息你没回
    r"|(第[一二三四五六七八九十\d]+条)"                              # 第一条 / 第2条
    r"|(指定回复|引用回复)"                                          # 技术术语直接匹配
    r"|(你.{0,4}(没|没有)(回|回复)(那|这|我|上面))",                # 你还没回那条
    re.IGNORECASE,
)

# 多条计数匹配："那两条都回了" / "三条消息你没回"
_MULTI_COUNT_RE = re.compile(
    r"(那|这)?([两三四五2345])(条|个).{0,6}(都?回|回复)"
    r"|(都|每条|逐条).{0,4}(回|回复)",
    re.IGNORECASE,
)

# quote reply 连续失败上限（超过后本 session 禁用，避免重复卡顿）
_QUOTE_REPLY_MAX_FAIL_STREAK = 5

# 过滤关键词提取时的无意义词汇
_QUOTE_STOP_WORDS = frozenset({
    "刚才", "上面", "之前", "以前", "没有", "指定", "引用",
    "关于", "提到", "那条", "那个", "消息", "问题", "一下",
    "回复", "还有", "那个", "这个", "什么", "如何", "怎么",
})


def _extract_quote_keywords(text: str) -> Optional[str]:
    """从指定回复请求中提取内容关键词，用于定位目标气泡。

    例："那个关于旅游的问题" → "旅游"；"讲股票的那条" → "股票"
    """
    # 剥掉请求框架词，留下实质内容
    cleaned = re.sub(
        r"[你我能帮把回复一下那个条消息问题吗啊呢嗯呀指定引用没有都还把]",
        " ", text,
    )
    words = re.findall(r"[\u4e00-\u9fff]{2,8}", cleaned)
    meaningful = [w for w in words if w not in _QUOTE_STOP_WORDS]
    return meaningful[0] if meaningful else None


def _detect_quote_targets(
    peer_text: str,
    all_msgs: "List[ui.IncomingMessage]",
) -> "Tuple[bool, List[ui.IncomingMessage]]":
    """检测指定回复意图并返回目标气泡列表。

    返回值：(is_quote_request, targets)
    - (False, [])     : 不是指定回复请求，普通流程继续
    - (True,  [])     : 是请求但目标不在屏幕 → 触发向上滚屏
    - (True,  [m])    : 单条目标
    - (True,  [a, b]) : 多条目标（"那两条都回了"）

    检测优先级：多条 > 序号 > 关键词匹配 > 最早一条
    """
    t = (peer_text or "").strip()
    if len(t) > 60 or not t:
        return False, []
    if not _QUOTE_REQUEST_RE.search(t):
        return False, []

    # 排除当前消息本身
    candidates = [m for m in all_msgs if m.text.strip() != t]
    if not candidates:
        return True, []   # 意图存在但无可见候选 → 触发滚屏

    _CN_MAP = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
               "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
               "两": 2, "2": 2, "3": 3, "4": 4, "5": 5}

    # 1. 多条计数："那两条都回了"
    _mc = _MULTI_COUNT_RE.search(t)
    if _mc:
        n_str = _mc.group(2) if _mc.lastindex and _mc.lastindex >= 2 else "2"
        n = _CN_MAP.get(n_str, 2)
        return True, candidates[:n]   # 取最早的 N 条

    # 2a. 方向词感知："倒数第N条" / "最近那条" / "最后那条"
    _rev_m = re.search(r"(倒数|最近|最后)(第([一二三四五六七八九十\d]+)条)?", t)
    if _rev_m:
        if _rev_m.group(3):
            ord_str = _rev_m.group(3)
            try:
                n = int(ord_str)
            except ValueError:
                n = _CN_MAP.get(ord_str, 1)
            idx = len(candidates) - n
            if 0 <= idx < len(candidates):
                return True, [candidates[idx]]
        else:
            return True, [candidates[-1]]   # 最近/最后 = 最新一条

    # 2b. 正序序号："第一条" / "第3条"
    _ord_m = re.search(r"第([一二三四五六七八九十\d]+)条", t)
    if _ord_m:
        ord_str = _ord_m.group(1)
        try:
            n = int(ord_str)
        except ValueError:
            n = _CN_MAP.get(ord_str, 1)
        if 1 <= n <= len(candidates):
            return True, [candidates[n - 1]]

    # 3. 关键词匹配："那个关于旅游的问题"
    kw = _extract_quote_keywords(t)
    if kw:
        for m in reversed(candidates):   # 最近的有效匹配优先
            if kw in m.text:
                return True, [m]

    # 4. 默认：最早一条（最可能是被遗漏的问题）
    return True, [candidates[0]]


def _detect_wa_foreground(png_bytes: bytes, sw: int, sh: int) -> bool:
    """Return True if screencap shows WA chat-list.

    檢測底部導航欄特徵：搜索圖標（Y≈85%-95%）+ 聊天/通話/狀態三個圖標。
    不依賴綠色標題欄（深色模式/主題可能變色）。
    """
    if not png_bytes or not png_bytes.startswith(_PNG_MAGIC):
        return False
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        iw, ih = img.size
        # 底部導航欄 Y 範圍：85%-95%
        by1 = max(0, int(ih * 0.85))
        by2 = min(ih - 1, int(ih * 0.95))
        # 檢測搜索圖標（放大鏡）：左側有深色像素
        dark_left = 0
        for y in range(by1, by2 + 1, 2):
            for x in range(0, int(iw * 0.2), 4):
                r, g, b = img.getpixel((x, y))
                if r < 80 and g < 80 and b < 80:
                    dark_left += 1
                    if dark_left >= 5:
                        break
            if dark_left >= 5:
                break
        # 檢測三個導航圖標（聊天/通話/狀態）：底部有深色像素
        dark_bottom = 0
        for y in range(by1, by2 + 1, 2):
            for x in range(int(iw * 0.2), int(iw * 0.8), 4):
                r, g, b = img.getpixel((x, y))
                if r < 80 and g < 80 and b < 80:
                    dark_bottom += 1
                    if dark_bottom >= 10:
                        break
            if dark_bottom >= 10:
                break
        return dark_left >= 5 and dark_bottom >= 10
    except Exception:
        return False


def _detect_wa_unread_badge(png_bytes: bytes, sw: int, sh: int) -> bool:
    """Scan WA chat-list screencap for green unread badge (MIUI V14 fallback).

    WA brand green #25D366 = RGB(37, 211, 102).
    Badge area: right side of first chat row (sw*0.80…sw-4, row1_cy±32px).
    """
    if not png_bytes or not png_bytes.startswith(_PNG_MAGIC):
        return False
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        iw, ih = img.size
        # Badge area: right 25% of screen, rows 18%-34% of screen height
        # (measured on Q4N 720x1600: badge at x=592-682, y=356-436)
        bx1 = max(0, int(iw * 0.75))
        bx2 = iw - 4
        by1 = max(0, int(ih * 0.18))
        by2 = min(ih - 1, int(ih * 0.34))
        green_hits = 0
        for y in range(by1, by2 + 1, 2):  # step=2 for speed
            for x in range(bx1, bx2 + 1, 2):
                r, g, b = img.getpixel((x, y))
                if abs(r - 37) < 50 and g > 160 and abs(b - 102) < 60:
                    green_hits += 1
                    if green_hits >= 3:
                        return True
        return False
    except Exception:
        return False


# ── 通知文本中检测语音消息 ────────────────────────────────────────────────────
# WhatsApp 通知格式: "🎤 语音消息" / "🎤 Voice message" / "🎤 Audio (0:05)" 等
_VOICE_NOTIF_PAT = re.compile(
    r"(?:"
    r"\U0001F3A4"        # 🎤
    r"|语音消息"
    r"|voice\s*message"
    r"|audio\s*message"
    r"|pesan\s*suara"     # Indonesian
    r"|voice\s*\("        # "Voice (0:02)"
    r")",
    re.IGNORECASE,
)


def _is_voice_notification(text: str) -> bool:
    """检测通知文本是否为语音消息通知。"""
    return bool(_VOICE_NOTIF_PAT.search(text or ""))


class WhatsAppRpaRunner:
    """单设备 WhatsApp RPA 执行器。由 WhatsAppRpaService 持有。"""

    def __init__(
        self,
        *,
        config_manager: Any,
        skill_manager: Any,
        wa_cfg: Optional[Dict[str, Any]] = None,
        state_store: Optional[WaRpaStateStore] = None,
    ) -> None:
        self._cm = config_manager
        self._sm = skill_manager
        self._cfg: Dict[str, Any] = dict(wa_cfg or {})
        self._state_store = state_store
        self._serial: Optional[str] = None
        self._pacing = PacingConfig.from_dict(self._cfg.get("human_pacing") or {})
        self._consecutive_dump_fails: int = 0
        self._monkey_fail_streak: int = 0  # MIUI: consecutive monkey-blocked count
        self._quote_reply_fail_streak: int = 0   # 连续 quote reply 失败次数
        self._quote_reply_disabled: bool = False  # 超限后本 session 禁用
        # 多账号 + 人设池（与 Messenger / LINE 链路对齐）
        self._account_id: str = str(self._cfg.get("account_id") or "default")
        self._persona_ids: List[str] = list(self._cfg.get("persona_ids") or [])
        # ContactHooks 由 service 在 contacts 子系统 bootstrap 后注入；None 时静默跳过
        self._contact_hooks: Optional[Any] = None
        self._last_wa_full_check_ts: float = time.time()  # MIUI badge scan throttle
        self._tts_semaphore: Optional[asyncio.Semaphore] = None  # P13-B: 防并发 TTS
        # 语音管道统计（内存级，API 可读）
        self._voice_metrics: Dict[str, int] = {
            "stt_attempts": 0, "stt_ok": 0, "stt_fail": 0,
            "stt_fallback_used": 0, "stt_placeholder": 0,
            "stt_batch_multi": 0,
            "tts_attempts": 0, "tts_ok": 0, "tts_fail": 0,
            "tts_sent": 0, "tts_send_fail": 0,
        }
        # 媒体管道统计（内存级，API 可读）
        self._media_metrics: Dict[str, int] = {
            "detected": 0, "vision_attempts": 0, "vision_ok": 0,
            "vision_fail": 0, "placeholder": 0,
            "kind_image": 0, "kind_video": 0, "kind_gif": 0,
            "kind_sticker": 0, "kind_file": 0, "kind_other": 0,
        }
        # ═══════════════════════════════════════════════════════════════════════
        # P15: 已打开对话新消息检测（解决"已读但未回复"问题）
        # ═══════════════════════════════════════════════════════════════════════
        # 当消息被用户已读（无 badge），但对话仍打开时，检测新消息内容变化
        self._open_thread_check_enabled: bool = bool(
            self._cfg_get("open_thread_check_enabled", True)
        )
        self._open_thread_check_interval_sec: float = float(
            self._cfg_get("open_thread_check_interval_sec", 15)
        )
        self._open_thread_last_check_ts: Dict[str, float] = {}  # chat_key -> last_check_ts
        self._open_thread_last_content_hash: Dict[str, str] = {}  # chat_key -> content_hash
        self._open_thread_max_rounds: int = int(
            self._cfg_get("open_thread_max_rounds", 3)  # 单轮最大处理消息数
        )
        # 主动巡检：消息被自动已读（无 badge）且机器人不在该对话内时，
        # 周期性打开列表最顶部会话核对「已读未回」消息（复用 open_thread 检测逻辑）。
        self._active_sweep_enabled: bool = bool(
            self._cfg_get("active_chat_sweep_enabled", True)
        )
        self._active_sweep_interval_sec: float = float(
            self._cfg_get("active_chat_sweep_interval_sec", 60)
        )
        self._last_active_sweep_ts: float = time.time()
        # P15-d: 用户明确要求停止联系 → 黑名单/静默
        self._stop_contact_quiet_minutes: float = float(
            self._cfg_get("stop_contact_quiet_minutes", 1440)
        )
        self._stop_contact_blacklist: bool = bool(
            self._cfg_get("stop_contact_blacklist", True)
        )
        # P15-f: 轻量意图检测器（替代纯关键词匹配）
        _intent_cfg = {
            "strong_threshold": float(self._cfg_get("stop_contact_strong_threshold", 0.85)),
            "weak_threshold": float(self._cfg_get("stop_contact_weak_threshold", 0.70)),
            "enable_negative_check": bool(self._cfg_get("stop_contact_enable_negative_check", True)),
        }
        self._stop_contact_detector: StopContactIntentDetector = create_detector(_intent_cfg)
        # P15-j: 初始化表情增强器
        self._emotion_enhancer: Optional[Any] = None
        if _EMOTION_ENHANCER_AVAILABLE and EmotionEnhancer is not None:
            try:
                _emo_cfg = self._cfg.get("emoticons", {})
                _nat = _emo_cfg.get("naturalization", {})
                if _nat.get("enabled", True):
                    # 构造表情配置（兼容全局配置结构）
                    _config_for_emo = {"emoticons": _emo_cfg}
                    self._emotion_enhancer = EmotionEnhancer(config=_config_for_emo)
                    logger.info("[wa_rpa] EmotionEnhancer initialized")
            except Exception as _emo_err:
                logger.warning("[wa_rpa] EmotionEnhancer init failed: %s", _emo_err)

    def set_contact_hooks(self, hooks: Optional[Any]) -> None:
        """注入/摘除 ContactHooks；线程安全的原子替换。"""
        self._contact_hooks = hooks

    def _account_persona_id(self) -> str:
        """WhatsApp 是 1v1 私聊，直接取 persona_ids[0]。无 persona 配置时返回空。"""
        return str(self._persona_ids[0]) if self._persona_ids else ""

    def get_voice_metrics(self) -> Dict[str, int]:
        """返回语音管道统计快照（API 读取用）。"""
        return dict(self._voice_metrics)

    def get_media_metrics(self) -> Dict[str, int]:
        """返回媒体管道统计快照（API 读取用）。"""
        return dict(self._media_metrics)

    # ── 配置读取 ─────────────────────────────────────────────────────────

    def reconfigure(self, wa_cfg: Dict[str, Any]) -> None:
        self._cfg = dict(wa_cfg)
        self._pacing = PacingConfig.from_dict(wa_cfg.get("human_pacing") or {})
        # 热更新 persona_ids（账号级人设池可热改）
        self._persona_ids = list(wa_cfg.get("persona_ids") or [])
        # P15-j: 热更新表情增强器配置
        try:
            _emo_cfg = wa_cfg.get("emoticons", {})
            _nat = _emo_cfg.get("naturalization", {})
            if _EMOTION_ENHANCER_AVAILABLE and EmotionEnhancer is not None:
                if _nat.get("enabled", True):
                    _config_for_emo = {"emoticons": _emo_cfg}
                    self._emotion_enhancer = EmotionEnhancer(config=_config_for_emo)
                    logger.info("[wa_rpa] EmotionEnhancer reconfigured")
                else:
                    self._emotion_enhancer = None
                    logger.info("[wa_rpa] EmotionEnhancer disabled")
        except Exception as _emo_err:
            logger.warning("[wa_rpa] EmotionEnhancer reconfigure failed: %s", _emo_err)

    def _cfg_get(self, key: str, default: Any = None) -> Any:
        val = self._cfg.get(key)
        return val if val is not None else default

    @property
    def _wa_pkg(self) -> str:
        biz = self._cfg_get("use_business_app", False)
        return _WA_PKG_BUSINESS if biz else _WA_PKG_PERSONAL

    @property
    def _wa_activity(self) -> str:
        biz = self._cfg_get("use_business_app", False)
        return _WA_HOME_ACTIVITY_BUSINESS if biz else _WA_HOME_ACTIVITY_PERSONAL

    # ── ADB 工具 ─────────────────────────────────────────────────────────

    def _resolve_serial(self) -> Optional[str]:
        preferred = str(self._cfg_get("adb_serial") or "")
        prefer_wa = bool(self._cfg_get("prefer_wa_device", True))
        return adb.pick_serial(
            preferred,
            prefer_line_installed=prefer_wa,
            line_pkg=self._wa_pkg,
        )

    def _dump_ui_xml(self) -> Tuple[Optional[bytes], str]:
        serial = self._serial
        if not serial:
            return None, "no_serial"
        remote = str(self._cfg_get("dump_remote_path", _WA_DUMP_REMOTE))
        # 先删旧文件，防止 uiautomator dump 失败时 cat 读取上一轮的聊天界面旧 XML
        # 最多重试 3 次（手机返回动画期间 uiautomator 可能瞬态失败）
        raw = None
        last_rc = -1
        for attempt in range(3):
            if attempt > 0:
                time.sleep(1.5)
            adb.run_adb(["shell", f"rm -f {remote}"], serial=serial, timeout=8.0)
            r = adb.dump_ui_hierarchy_xml(serial, remote)
            if r.returncode == 0 and r.stdout:
                raw = r.stdout
                break
            last_rc = r.returncode
        if raw is None:
            r2 = adb.dump_ui_hierarchy_xml_as_root(serial, remote)
            if r2.returncode == 0 and r2.stdout:
                raw = r2.stdout
        # ── u2 fallback: MIUI 杀 native uiautomator 时，用 python-uiautomator2 ──
        if raw is None:
            raw = self._dump_ui_xml_u2(serial)
            if raw is None:
                return None, f"dump_fail rc={last_rc}"
        # MIUI 设备 stdout 有时包含异常堆栈/警告在 XML 之前；只截取 XML 部分
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        idx = raw.find("<?xml")
        if idx == -1:
            idx = raw.find("<hierarchy")
        if idx > 0:
            raw = raw[idx:]
        if idx == -1:
            return None, "no_xml_in_stdout"
        return raw.encode("utf-8"), "ok"

    def _dump_ui_xml_u2(self, serial: str) -> Optional[str]:
        """使用 python-uiautomator2 dump_hierarchy 作为 MIUI 兜底方案。"""
        try:
            import uiautomator2 as u2
            d = u2.connect(serial)
            xml_str = d.dump_hierarchy()
            if xml_str and ("<hierarchy" in xml_str or "<?xml" in xml_str):
                return xml_str
        except Exception as e:
            logger.debug("[wa_rpa] u2 dump_hierarchy failed serial=%s: %s", serial[:8], e)
        return None

    def _tap(self, cx: int, cy: int) -> None:
        if self._serial:
            adb.input_tap(self._serial, cx, cy)

    def _back(self) -> None:
        if self._serial:
            adb.input_keyevent(self._serial, "KEYCODE_BACK")

    # ═══════════════════════════════════════════════════════════════════════
    # P15: 已打开对话新消息检测（解决"已读但未回复"问题）
    # ═══════════════════════════════════════════════════════════════════════

    async def _check_open_thread_for_new_messages(
        self,
        result: Dict[str, Any],
        t0: float,
    ) -> Optional[Dict[str, Any]]:
        """
        当 badge=0 时，检测是否有已打开的对话中有新消息。

        场景：
        - 用户正在和 AI 对话，消息已读（无 badge）
        - 用户连续发了多条消息，但 AI 正在处理前一条，后面的消息被"已读"了
        - 系统返回 no_unread，但这些消息需要被检测和回复

        机制：
        1. 检查 WA 是否在前台（dumpsys window）
        2. dump 当前界面，检查是否在聊天 thread 内
        3. 提取最后一条对方消息
        4. 与上次记录的内容 hash 比对
        5. 若内容变化 → 触发正常回复流程
        6. 循环处理直到没有新消息或达到上限

        Returns:
            处理了新消息则返回 result dict，否则返回 None
        """
        if not self._serial:
            return None

        serial = self._serial
        _now = time.time()

        # 1) 检查是否在聊天 thread 内（通过 XML 特征）
        # 聊天界面特征：有 entry（输入框）+ 有消息气泡
        try:
            xml_bytes, _ = await asyncio.to_thread(self._dump_ui_xml)
            if not xml_bytes:
                return None

            # 检查是否在聊天界面（有 entry 输入框）
            if b"com.whatsapp:id/entry" not in xml_bytes:
                # 不在聊天界面，无需检测
                return None

            # 2) 提取对方名字（从 title 或界面元素）
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml_bytes)

            _peer_name = ""
            for el in root.iter():
                # 尝试从 title 提取
                _text = el.get("text") or ""
                _cd = el.get("content-desc") or ""
                # WhatsApp 聊天界面 title 通常是对方名字
                if _cd and any(suffix in _cd for suffix in ["'s profile photo", " profile photo"]):
                    _peer_name = _cd.replace("'s profile photo", "").replace(" profile photo", "").strip()
                    break

            if not _peer_name:
                # 无法确定对方名字，跳过（避免误操作）
                logger.debug("[wa_rpa][open_thread] 无法提取对方名字，跳过检测")
                return None

            chat_key = f"wa:{self._account_id}:{_peer_name}"

            # 3) 检查冷却时间（避免过于频繁检查同一对话）
            _last_check = self._open_thread_last_check_ts.get(chat_key, 0)
            if (_now - _last_check) < self._open_thread_check_interval_sec:
                logger.debug(
                    "[wa_rpa][open_thread] 冷却中 chat=%s elapsed=%.1fs < %.1fs",
                    chat_key, _now - _last_check, self._open_thread_check_interval_sec
                )
                return None

            self._open_thread_last_check_ts[chat_key] = _now

            # 4) 提取最后一条对方消息
            _peer_text = ui.pick_last_incoming_text(xml_bytes)
            if not _peer_text:
                logger.debug("[wa_rpa][open_thread] 未检测到对方消息 chat=%s", chat_key)
                return None

            # 5) 内容 hash 比对
            _current_hash = hashlib.sha256(_peer_text.encode()).hexdigest()[:16]
            _last_hash = self._open_thread_last_content_hash.get(chat_key, "")

            if _current_hash == _last_hash:
                # 内容无变化，跳过
                logger.debug(
                    "[wa_rpa][open_thread] 内容无变化 chat=%s hash=%s",
                    chat_key, _current_hash[:8]
                )
                return None

            # ═════════════════════════════════════════════════════════════════
            # 发现新消息！触发处理流程
            # ═════════════════════════════════════════════════════════════════
            logger.warning(
                "[wa_rpa][open_thread] 发现新消息（无badge）chat=%s peer_len=%d hash=%s",
                chat_key, len(_peer_text), _current_hash[:8]
            )

            # 更新状态
            self._open_thread_last_content_hash[chat_key] = _current_hash

            # 更新 result 准备进入处理流程
            result["chat_key"] = chat_key
            result["peer_name"] = _peer_name
            result["peer_text"] = _peer_text

            # 6) 进入正常处理流程（复用现有逻辑）
            _processed_count = 0
            for _round in range(self._open_thread_max_rounds):
                # 检查是否重复消息（dedup）
                _state = (self._state_store.get_chat_state(chat_key)
                          if self._state_store else {})
                _is_repeat = _state.get("last_peer_hash") == _current_hash
                if _is_repeat:
                    _last_reply_ts = float(_state.get("last_reply_ts") or 0)
                    _dedup_window = float(self._cfg_get("dedup_window_sec", 3600))
                    if (_now - _last_reply_ts) < _dedup_window:
                        logger.info(
                            "[wa_rpa][open_thread] 消息已回复过（在dedup窗口内）chat=%s",
                            chat_key
                        )
                        result["step"] = "already_replied"
                        result["ok"] = True
                        return self._finish(result, t0)

                # 调用消息处理流程
                _process_result = await self._process_open_thread_message(
                    serial, chat_key, _peer_text, result, t0
                )
                _processed_count += 1

                # 检查是否还有更多消息
                await asyncio.sleep(1.0)  # 等待界面稳定
                xml_bytes, _ = await asyncio.to_thread(self._dump_ui_xml)
                if not xml_bytes:
                    break

                _new_text = ui.pick_last_incoming_text(xml_bytes)
                if not _new_text:
                    break

                _new_hash = hashlib.sha256(_new_text.encode()).hexdigest()[:16]
                if _new_hash == _current_hash:
                    # 无新消息
                    break

                # 有新消息，继续下一轮
                _peer_text = _new_text
                _current_hash = _new_hash
                self._open_thread_last_content_hash[chat_key] = _current_hash
                result["peer_text"] = _peer_text
                logger.warning(
                    "[wa_rpa][open_thread] 继续处理第%d条消息 chat=%s",
                    _round + 2, chat_key
                )

            logger.warning(
                "[wa_rpa][open_thread] 处理完成 chat=%s processed=%d",
                chat_key, _processed_count
            )
            return result

        except Exception as e:
            logger.warning("[wa_rpa][open_thread] 检测异常: %s", e, exc_info=True)
            return None

    async def _sweep_recent_chat_for_unanswered(
        self,
        list_xml: Optional[bytes],
        result: Dict[str, Any],
        t0: float,
    ) -> Optional[Dict[str, Any]]:
        """主动巡检：打开聊天列表最顶部（最近）会话，核对「已读未回」消息。

        解决：消息被 WhatsApp 自动标记已读（无 badge），且机器人停在聊天列表
        （不在该对话内）时，badge 扫描与 open_thread 检测都发现不了的盲区。

        机制：节流（active_sweep_interval_sec）下，打开最顶部会话 → 复用
        _check_open_thread_for_new_messages 的「读最后一条对方消息 + 去重」逻辑。
        若该会话最后一条是己方消息或已回复过，则不会重复回复（由去重保证）。

        Returns: 处理了新消息则返回 result，否则返回 None（并退回聊天列表）。
        """
        if not self._serial or not list_xml:
            return None
        _now = time.time()
        if (_now - self._last_active_sweep_ts) < self._active_sweep_interval_sec:
            return None
        # 仅在确实停在聊天列表时巡检（不在任何对话内）
        if b"conversations_row" not in list_xml or b"com.whatsapp:id/entry" in list_xml:
            logger.warning(
                "[wa_rpa][sweep] 跳过：未停在聊天列表 has_rows=%s has_entry=%s serial=%s",
                b"conversations_row" in list_xml,
                b"com.whatsapp:id/entry" in list_xml,
                self._serial,
            )
            return None
        top = ui.find_top_chat_row(list_xml, wa_pkg=self._wa_pkg)
        if not top:
            logger.warning("[wa_rpa][sweep] 跳过：未解析到顶部会话行 serial=%s", self._serial)
            return None
        self._last_active_sweep_ts = _now
        _name, _cx, _cy = top
        logger.warning("[wa_rpa][sweep] 打开最近会话核对已读未回 name=%r serial=%s", _name, self._serial)
        try:
            self._tap(_cx, _cy)
            await asyncio.sleep(1.2)
            _open_result = await self._check_open_thread_for_new_messages(result, t0)
            if _open_result and _open_result.get("step") not in (None, "no_unread", "duplicate"):
                logger.warning(
                    "[wa_rpa][sweep] 已读未回消息已处理 name=%r step=%s",
                    _name, _open_result.get("step"),
                )
                return _open_result
        except Exception as e:
            logger.warning("[wa_rpa][sweep] 巡检异常: %s", e, exc_info=True)
        finally:
            # 退回聊天列表，保持下一轮在列表态
            try:
                self._back()
                await asyncio.sleep(0.4)
            except Exception:
                pass
        return None

    async def _process_open_thread_message(
        self,
        serial: str,
        chat_key: str,
        peer_text: str,
        result: Dict[str, Any],
        t0: float,
    ) -> Dict[str, Any]:
        """处理已打开对话中的单条消息（复用现有逻辑）。"""
        # 使用 SkillManager 生成回复 + _pace_and_send 发送

        _state = (self._state_store.get_chat_state(chat_key)
                  if self._state_store else {})
        _peer_hash = hashlib.sha256(peer_text.encode()).hexdigest()[:16]
        _is_repeat = _state.get("last_peer_hash") == _peer_hash

        if _is_repeat:
            result["step"] = "duplicate"
            result["ok"] = True
            return result

        # 构建上下文
        ctx: Dict[str, Any] = {
            "platform": "whatsapp",
            "account_id": self._account_id,
            "account_persona_id": self._account_persona_id(),
        }
        # 注入上次回复 → 激活角度轮换/反重复系统
        _last_reply = (_state.get("last_reply") or "").strip()
        if _last_reply:
            ctx["last_reply"] = _last_reply

        # 生成回复
        logger.debug(
            "[wa_rpa][open_thread] calling skill_manager peer=%r chat=%s",
            peer_text.strip()[:30], chat_key
        )
        try:
            reply_text = await self._sm.process_message(
                peer_text.strip(),
                user_id=chat_key,
                context=ctx,
            )
        except Exception as e:
            result["step"] = "skill_error"
            result["error"] = f"skill_error:{e}"
            logger.warning("[wa_rpa][open_thread] skill_error chat=%s: %s", chat_key, e)
            return result

        _p_name = (ctx.get("_resolved_persona_name") or "").strip() or ctx.get("account_persona_id", "?")
        logger.warning(
            "[wa_rpa][open_thread] reply persona=%s peer=%r reply=%r chat=%s",
            _p_name, peer_text.strip()[:30], str(reply_text or "").strip()[:60], chat_key,
        )

        if not reply_text or not str(reply_text).strip():
            result["step"] = "empty_reply"
            result["ok"] = True
            return result

        reply_text = str(reply_text).strip()

        # P15-j: 表情增强处理
        if self._emotion_enhancer and reply_text:
            try:
                _emo_ctx = {"suggested_emoticons": []}
                reply_text = self._emotion_enhancer.enhance_reply(
                    original_reply=reply_text,
                    emotion="neutral",
                    context_analysis=_emo_ctx,
                    message_text=peer_text.strip(),
                    chat_id=chat_key,
                )
                reply_text = reply_text.strip()
            except Exception as _ee_err:
                logger.debug("[wa_rpa] emotion enhance failed: %s", _ee_err)

        result["reply_text"] = reply_text

        # 发送回复（坐标 fallback，需要屏幕尺寸定位输入框/发送键）
        _screen = await asyncio.to_thread(adb.screen_size, serial)
        _sw = _screen[0] if _screen else 1080
        _sh = _screen[1] if _screen else 1920
        send_res = await self._send_text_coord_fallback(serial, reply_text, (_sw, _sh))

        if send_res.get("ok"):
            result["ok"] = True
            result["step"] = "sent"
            # 更新状态
            if self._state_store:
                self._state_store.upsert_chat_state(
                    chat_key,
                    last_peer_text=peer_text,
                    last_peer_hash=_peer_hash,
                    last_reply=reply_text,
                    last_peer_ts=t0,
                    last_reply_ts=time.time(),
                )
        else:
            result["step"] = "send_fail"
            result["error"] = send_res.get("error", "unknown")

        return result

    def _clear_focused_input(self, serial: str) -> bool:
        try:
            r = adb.run_adb(
                ["shell", "am", "broadcast", "-a", "ADB_CLEAR_TEXT"],
                serial=serial, timeout=5.0,
            )
            if r.returncode == 0:
                return True
        except Exception:
            pass
        try:
            adb.input_keyevent(serial, "123")
            for _ in range(96):
                adb.input_keyevent(serial, "67")
            return True
        except Exception:
            return False

    # ── 发送文字 ─────────────────────────────────────────────────────────

    def _send_text(self, xml_bytes: Optional[bytes], text: str) -> Dict[str, Any]:
        serial = self._serial
        if not serial:
            return {"ok": False, "error": "no_serial"}

        # 对齐 LINE：发送前刷新 hierarchy，避免 AI 耗时后界面已变导致坐标失效
        if bool(self._cfg_get("redump_before_send", True)):
            fresh_xml, _ = self._dump_ui_xml()
            if fresh_xml:
                xml_bytes = fresh_xml

        # 1) 找输入框并点击
        input_xy = ui.find_input_field(xml_bytes) if xml_bytes else None
        if input_xy is None:
            # 尝试重新 dump
            xml_bytes2, _ = self._dump_ui_xml()
            input_xy = ui.find_input_field(xml_bytes2) if xml_bytes2 else None
        if input_xy is None:
            return {"ok": False, "error": "input_field_not_found"}
        adb.input_tap(serial, *input_xy)
        time.sleep(0.3)

        # 2) 输入文字 + 发送（优先 uiautomator2，支持中文/emoji）
        _u2_used = False
        _orig_ime: Optional[str] = None
        try:
            import uiautomator2 as _u2  # type: ignore
            _dev = _u2.connect(serial)
            _el = _dev(resourceId=f"{self._wa_pkg}:id/entry")
            _el.set_text(text)
            time.sleep(0.3)
            # 用 u2 直接点发送按钮
            _send_sel = _dev(resourceId=f"{self._wa_pkg}:id/send")
            if _send_sel.exists(timeout=2.0):
                _send_sel.click()
            else:
                _dev(description="Send").click()
            _u2_used = True
            # 发送后释放 u2 accessibility session，防止与后续 uiautomator dump 冲突
            try:
                _dev.stop_uiautomator()
            except Exception:
                pass
        except Exception:
            pass

        if not _u2_used:
            # fallback: adb keyboard broadcast or clipboard_paste
            _UIAUT_IME = "com.github.uiautomator/.AdbKeyboard"
            try:
                if adb.is_adbkeyboard_installed(serial):
                    adb.adb_keyboard_input_text(serial, text, use_base64=True, package=self._wa_pkg)
                elif adb.run_adb(["shell", "pm", "path", "com.github.uiautomator"],
                                 serial=serial, timeout=6.0).returncode == 0:
                    r_orig = adb.run_adb(
                        ["shell", "settings", "get", "secure", "default_input_method"],
                        serial=serial, timeout=6.0,
                    )
                    _orig_ime = (r_orig.stdout or "").strip() or None
                    adb.run_adb(["shell", "ime", "enable", _UIAUT_IME], serial=serial, timeout=6.0)
                    adb.run_adb(["shell", "ime", "set", _UIAUT_IME], serial=serial, timeout=6.0)
                    time.sleep(0.8)
                    adb.input_tap(serial, *input_xy)
                    time.sleep(0.5)
                    adb.adb_keyboard_input_text(
                        serial, text, use_base64=True, package="com.github.uiautomator",
                    )
                else:
                    adb.clipboard_paste(serial, text)
            except Exception as e2:
                return {"ok": False, "error": f"input_fail:{e2}"}

            time.sleep(0.4)

            # fallback 发送键
            xml3, _ = self._dump_ui_xml()
            send_xy = ui.find_send_button(xml3) if xml3 else None
            if send_xy is None:
                if input_xy:
                    adb.input_tap(serial, min(input_xy[0] + 200, 9999), input_xy[1])
                else:
                    adb.input_keyevent(serial, "KEYCODE_ENTER")
            else:
                adb.input_tap(serial, *send_xy)

            # 还原原 IME（若做了切换）
            if _orig_ime:
                try:
                    adb.run_adb(["shell", "ime", "set", _orig_ime], serial=serial, timeout=6.0)
                except Exception:
                    pass

        return {"ok": True}

    # ── 语音识别（Voice Input / ASR） ─────────────────────────────────────

    async def _try_transcribe_voice(
        self,
        chat_xml: bytes,
        screen_width: int,
        result: Dict[str, Any],
    ) -> Optional[str]:
        """检测聊天界面中的语音消息并转写为文字。

        优化方案：直接从 WhatsApp 文件系统读取 .opus 文件（无需播放录制）。
        返回转写文字，无语音或转写失败返回 None。
        """
        # 0) 检查配置开关
        voice_cfg = self._cfg_get("voice_input") or {}
        if isinstance(voice_cfg, dict) and not voice_cfg.get("enabled", True):
            return None

        # 1) XML 检测语音消息（支持多条连续语音）
        all_voices = ui.detect_voice_messages(chat_xml, screen_width=screen_width)
        incoming_voices = [v for v in all_voices if v.is_incoming]
        if not incoming_voices:
            return None
        voice_msg = incoming_voices[-1]  # 最新一条（用于时长/坐标参考）

        logger.info(
            "[wa_rpa] 检测到语音消息 count=%d dur=%s incoming=%s play=(%d,%d)",
            len(incoming_voices), voice_msg.duration_text, voice_msg.is_incoming,
            voice_msg.play_cx, voice_msg.play_cy,
        )
        result["voice_detected"] = True
        result["voice_count"] = len(incoming_voices)
        result["voice_duration"] = voice_msg.duration_text

        # 2) 从文件系统获取最新语音文件
        serial = self._serial
        if not serial:
            result["voice_error"] = "no_serial"
            return None

        from src.integrations.whatsapp_rpa.voice_grabber import get_latest_voice_note

        use_biz = self._cfg_get("use_business_app", False)
        # 已处理过的语音文件名集合（避免重复转写同一条）
        _processed = getattr(self, "_voice_processed_files", None)
        if _processed is None:
            _processed = set()
            self._voice_processed_files = _processed  # type: ignore[attr-defined]

        _max_age = float(voice_cfg.get("max_age_sec", 600) if isinstance(voice_cfg, dict) else 600)
        # 多条语音：尝试拉取与 XML 检测到的 incoming 数量匹配的文件
        _batch_target = min(len(incoming_voices), int(voice_cfg.get("batch_max", 5) if isinstance(voice_cfg, dict) else 5))
        _pulled_files: list = []
        for _ in range(_batch_target):
            vn = await asyncio.to_thread(
                get_latest_voice_note, serial,
                use_business=use_biz, max_age_sec=_max_age,
                already_processed=_processed,
            )
            if not vn.ok:
                break
            _pulled_files.append(vn)
            _processed.add(vn.filename)  # 下次循环跳过此文件

        if not _pulled_files:
            logger.warning("[wa_rpa] 语音文件获取失败 (batch)")
            result["voice_error"] = "no_recent_voice_file"
            return None

        result["voice_file"] = _pulled_files[0].filename
        result["voice_files_pulled"] = len(_pulled_files)
        logger.info("[wa_rpa] 获取语音文件: %d files, first=%s", len(_pulled_files), _pulled_files[0].filename)

        # 3) ASR 转写（多文件按时间顺序合并）
        try:
            from src.ai.audio_pipeline import get_audio_pipeline

            ap = get_audio_pipeline(self._cfg_get("audio_pipeline") or None)
            if not ap.is_available():
                logger.warning("[wa_rpa] audio_pipeline 不可用")
                result["voice_error"] = "audio_pipeline_unavailable"
                return None

            self._voice_metrics["stt_attempts"] += 1
            if len(_pulled_files) > 1:
                self._voice_metrics["stt_batch_multi"] += 1
            transcripts: list = []
            total_asr_ms = 0
            _any_fallback = False
            for vf in _pulled_files:
                tr = await ap.transcribe_file(vf.local_path)
                if tr.ok and tr.text.strip():
                    transcripts.append(tr.text.strip())
                    total_asr_ms += tr.latency_ms
                    if tr.extra.get("fallback_used"):
                        _any_fallback = True
                else:
                    logger.warning("[wa_rpa] ASR partial fail: %s err=%s", vf.filename, tr.error)

            if not transcripts:
                self._voice_metrics["stt_fail"] += 1
                self._voice_metrics["stt_placeholder"] += 1
                result["voice_error"] = "asr_all_failed"
                return self._voice_placeholder(voice_msg, result)

            transcript = " ".join(transcripts)
            result["voice_transcript"] = transcript
            result["voice_lang"] = tr.language  # 用最后成功的语种
            result["voice_asr_ms"] = total_asr_ms
            result["voice_batch_transcribed"] = len(transcripts)
            self._voice_metrics["stt_ok"] += 1
            if _any_fallback:
                result["voice_fallback_used"] = True
                self._voice_metrics["stt_fallback_used"] += 1
            logger.info(
                "[wa_rpa] 语音转写成功: %d/%d files, text=%r (%dms) fallback=%s",
                len(transcripts), len(_pulled_files), transcript[:60],
                total_asr_ms, _any_fallback,
            )
            return transcript

        except Exception as e:
            logger.warning("[wa_rpa] ASR 异常: %s", e, exc_info=True)
            self._voice_metrics["stt_fail"] += 1
            self._voice_metrics["stt_placeholder"] += 1
            result["voice_error"] = f"asr_exception:{e}"
            return self._voice_placeholder(voice_msg, result)

    async def _try_transcribe_voice_from_fs(
        self, result: Dict[str, Any],
    ) -> Optional[str]:
        """无 XML 时直接从文件系统拉取最新语音并转写。

        用于 screencap fallback 路径：通知里检测到语音标记但无 UI dump。
        """
        serial = self._serial
        if not serial:
            return None
        voice_cfg = self._cfg_get("voice_input") or {}
        if isinstance(voice_cfg, dict) and not voice_cfg.get("enabled", True):
            return None
        try:
            from src.integrations.whatsapp_rpa.voice_grabber import get_latest_voice_note

            use_biz = self._cfg_get("use_business_app", False)
            _processed = getattr(self, "_voice_processed_files", None)
            if _processed is None:
                _processed = set()
                self._voice_processed_files = _processed  # type: ignore[attr-defined]

            vn = await asyncio.to_thread(
                get_latest_voice_note, serial,
                use_business=use_biz,
                max_age_sec=float(voice_cfg.get("max_age_sec", 600) if isinstance(voice_cfg, dict) else 600),
                already_processed=_processed,
            )
            if not vn.ok:
                result["voice_fs_error"] = vn.error
                logger.warning("[wa_rpa][fb] 语音文件未找到: %s serial=%s", vn.error, serial)
                return None

            from src.ai.audio_pipeline import get_audio_pipeline
            ap = get_audio_pipeline(self._cfg_get("audio_pipeline") or None)
            if not ap.is_available():
                result["voice_fs_error"] = "audio_pipeline_unavailable"
                logger.warning("[wa_rpa][fb] audio_pipeline 不可用（STT 跳过）serial=%s", serial)
                return None

            tr = await ap.transcribe_file(vn.local_path)
            if not tr.ok or not tr.text.strip():
                result["voice_fs_error"] = f"asr_fail:{tr.error or 'empty'}"
                logger.warning("[wa_rpa][fb] STT 失败: %s file=%s serial=%s", tr.error or 'empty', vn.filename, serial)
                return None

            _processed.add(vn.filename)
            transcript = tr.text.strip()
            result["voice_file"] = vn.filename
            result["voice_asr_ms"] = tr.latency_ms
            logger.info("[wa_rpa][fb] 语音文件转写: %s → %r (%dms)", vn.filename, transcript[:60], tr.latency_ms)
            return transcript
        except Exception as e:
            result["voice_fs_error"] = str(e)
            logger.warning("[wa_rpa][fb] voice_from_fs 异常: %s serial=%s", e, serial)
            return None

    @staticmethod
    def _voice_placeholder(voice_msg: Any, result: Dict[str, Any]) -> str:
        """ASR 失败时生成含时长的占位提示，让 AI 仍能感知语音消息。"""
        dur = ""
        if voice_msg is not None:
            dur = getattr(voice_msg, "duration_text", "") or ""
        if dur:
            placeholder = f"[对方发送了一条 {dur} 的语音消息，语音转文字失败]"
        else:
            placeholder = "[对方发送了一条语音消息，语音转文字失败]"
        result["voice_transcribe_fallback"] = True
        result["voice_placeholder"] = placeholder
        return placeholder

    # ── 媒体消息理解（Media Understanding） ────────────────────────────────

    async def _try_describe_media(
        self,
        chat_xml: bytes,
        screen_width: int,
        result: Dict[str, Any],
    ) -> Optional[str]:
        """检测聊天界面媒体消息（图片/视频/贴纸/GIF/文件）并生成自然语言描述。

        策略：XML 粗分类 → ADB 截图 → 裁剪媒体区域 → VisionClient。
        Vision 不可用时退回 placeholder，保证主流程不阻断。
        """
        media_cfg = self._cfg_get("media_input") or {}
        if isinstance(media_cfg, dict) and not media_cfg.get("enabled", False):
            return None

        media_msg = ui.detect_last_incoming_media(chat_xml, screen_width=screen_width)
        if media_msg is None:
            return None

        self._media_metrics["detected"] += 1
        kind = media_msg.kind
        kind_key = f"kind_{kind}" if f"kind_{kind}" in self._media_metrics else "kind_other"
        self._media_metrics[kind_key] += 1

        logger.info(
            "[wa_rpa] 检测到媒体消息 kind=%s incoming=%s bounds=%s",
            kind, media_msg.is_incoming, media_msg.bounds,
        )
        result["media_detected"] = True
        result["media_kind"] = kind

        lang = str(self._cfg_get("default_reply_lang", "zh"))
        use_vision = bool(
            isinstance(media_cfg, dict) and media_cfg.get("use_vision", True)
        )

        if not use_vision or kind == "file":
            from src.integrations.whatsapp_rpa.media_vision import media_placeholder
            ph = media_placeholder(kind, lang=lang, duration_text=media_msg.duration_text)
            result["media_placeholder"] = True
            result["media_desc"] = ph
            self._media_metrics["placeholder"] += 1
            logger.info("[wa_rpa] 媒体 placeholder: %s", ph)
            return ph

        serial = self._serial
        if not serial:
            from src.integrations.whatsapp_rpa.media_vision import media_placeholder
            ph = media_placeholder(kind, lang=lang, duration_text=media_msg.duration_text)
            result["media_placeholder"] = True
            result["media_desc"] = ph
            self._media_metrics["placeholder"] += 1
            return ph

        # 截图当前聊天界面
        try:
            png_bytes = await asyncio.to_thread(
                screen_ocr.capture_screen_png, serial, adb,
            )
        except Exception as _e:
            logger.warning("[wa_rpa] 媒体截图失败: %s", _e)
            png_bytes = None

        from src.integrations.whatsapp_rpa.media_vision import (
            describe_wa_media, media_placeholder,
        )

        vision_cfg_raw = self._cm.config if self._cm else {}
        vision_cfg = (vision_cfg_raw or {}).get("vision") or {}
        if isinstance(media_cfg, dict) and media_cfg.get("vision_cfg"):
            vision_cfg = {**vision_cfg, **media_cfg["vision_cfg"]}
        global_vision = vision_cfg

        padding = int(media_cfg.get("crop_padding", 24) if isinstance(media_cfg, dict) else 24)
        max_dim = int(media_cfg.get("max_image_dim", 960) if isinstance(media_cfg, dict) else 960)
        timeout = float(media_cfg.get("timeout_sec", 30) if isinstance(media_cfg, dict) else 30)

        if png_bytes:
            self._media_metrics["vision_attempts"] += 1
            desc, tag = await describe_wa_media(
                png_bytes, media_msg.bounds, kind,
                vision_cfg=vision_cfg,
                global_vision=global_vision,
                lang=lang,
                padding=padding,
                max_image_dim=max_dim,
                timeout_sec=timeout,
            )
            result["media_vision_backend"] = tag
            if desc:
                self._media_metrics["vision_ok"] += 1
                result["media_desc"] = desc
                logger.info("[wa_rpa] 媒体描述成功 kind=%s: %r", kind, desc[:60])
                return desc
            else:
                self._media_metrics["vision_fail"] += 1
                logger.warning("[wa_rpa] 媒体描述失败 kind=%s tag=%s", kind, tag)
        else:
            self._media_metrics["vision_fail"] += 1

        ph = media_placeholder(kind, lang=lang, duration_text=media_msg.duration_text)
        result["media_placeholder"] = True
        result["media_desc"] = ph
        self._media_metrics["placeholder"] += 1
        logger.info("[wa_rpa] 媒体 fallback placeholder: %s", ph)
        return ph

    # ── TTS 语音回复（Voice Output） ──────────────────────────────────────

    async def _generate_pending_tts(
        self, pending_id: int, reply_text: str, reply_lang: str,
    ) -> None:
        """P13-B/P14-B: 为 approval-mode pending 行异步生成 TTS 预览（共享模块）。"""
        vo = self._cfg_get("voice_output") or {}
        if not (isinstance(vo, dict) and vo.get("enabled")) or self._state_store is None:
            return
        if self._tts_semaphore is None:
            self._tts_semaphore = asyncio.Semaphore(1)
        from src.integrations.shared.tts_preview import generate_approval_tts
        await generate_approval_tts(
            pending_id, reply_text, reply_lang,
            voice_cfg=dict(vo), state_store=self._state_store,
            semaphore=self._tts_semaphore, fname_prefix="wa-tts",
        )

    async def _maybe_prepare_tts_reply(
        self, reply_text: str, result: Dict[str, Any],
        tts_lang: Optional[str] = None,
    ) -> None:
        """生成 TTS 语音回复并可选自动发送。

        Args:
            tts_lang: 检测到的 XTTS 语言代码（如 'de'/'ja'/'zh-cn'），
                      覆盖 voice_profile.language 配置，实现动态多语言合成。
        """
        cfg = self._cfg_get("voice_output") or {}
        if not isinstance(cfg, dict) or not cfg.get("enabled", False):
            return
        mode = str(cfg.get("mode") or "approval_only").strip().lower()
        trigger = str(cfg.get("trigger") or "when_peer_voice").strip().lower()
        if trigger == "when_peer_voice" and not result.get("voice_transcript"):
            return
        elif trigger == "random":
            import random as _rnd
            if _rnd.random() >= float(cfg.get("voice_probability", 0.3) or 0.3):
                return
        if mode in ("off", "disabled"):
            return
        # 动态语言注入：用检测到的语言覆盖 voice_profile.language
        if tts_lang:
            cfg = dict(cfg)
            vp = dict(cfg.get("voice_profile") or {})
            vp["language"] = tts_lang
            cfg["voice_profile"] = vp
            result["tts_lang"] = tts_lang
            logger.info("[wa_rpa] TTS 语言已切换 → %s", tts_lang)
        max_chars = int(cfg.get("max_text_chars", 220) or 220)
        text = (reply_text or "").strip()
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "..."
            result["tts_truncated"] = True
        try:
            self._voice_metrics["tts_attempts"] += 1
            from src.ai.tts_pipeline import get_tts_pipeline
            tts = get_tts_pipeline(cfg)
            rv = await tts.synthesize(text, timeout_sec=float(cfg.get("timeout_sec", 30) or 30))
            result["tts_provider"] = rv.provider
            result["tts_latency_ms"] = rv.latency_ms
            if rv.ok:
                max_sec = float(cfg.get("max_seconds", 20) or 20)
                hard_max = max_sec * float(cfg.get("duration_max_ratio", 1.5) or 1.5)
                min_sec = float(cfg.get("duration_min_sec", 0.3) or 0.3)
                result["tts_duration_sec"] = round(rv.duration_sec, 2)
                if rv.duration_sec > 0 and (rv.duration_sec > hard_max or rv.duration_sec < min_sec):
                    result["tts_error"] = f"duration_guard:{rv.duration_sec:.1f}s"
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
                self._voice_metrics["tts_ok"] += 1
                logger.warning("[wa_rpa] TTS ok: %s dur=%.1fs %dms", rv.provider, rv.duration_sec, rv.latency_ms)
                if mode == "auto_voice":
                    await self._maybe_send_tts_audio(rv.audio_path, cfg, result)
                    if result.get("tts_send_ok"):
                        self._voice_metrics["tts_sent"] += 1
                    else:
                        self._voice_metrics["tts_send_fail"] += 1
                        _err = str(result.get("tts_send_error") or "")
                        # 错误分类：share UI 未出现→轻量恢复；发送按钮未找到→全量回退
                        if _err.startswith("share_skip_"):
                            logger.warning("[wa_rpa] TTS send skipped: %s (no recovery needed)", _err)
                        elif self._serial:
                            logger.warning("[wa_rpa] TTS send fail → BACK×3 recovery: %s", _err)
                            for _ in range(3):
                                adb.run_adb(["shell", "input", "keyevent", "KEYCODE_BACK"], serial=self._serial, timeout=5.0)
                                await asyncio.sleep(0.4)
                            # 额外：go-home 确保回到聊天列表
                            adb.run_adb(["shell", "input", "keyevent", "KEYCODE_HOME"], serial=self._serial, timeout=5.0)
                            await asyncio.sleep(0.5)
            else:
                self._voice_metrics["tts_fail"] += 1
                result["tts_error"] = rv.error
        except Exception:
            self._voice_metrics["tts_fail"] += 1
            logger.debug("[wa_rpa] TTS 异常", exc_info=True)

    async def _maybe_send_tts_audio(
        self, audio_path: str, cfg: Dict[str, Any], result: Dict[str, Any],
    ) -> None:
        """通过 share intent 向 WhatsApp 发送 TTS 音频。"""
        if not audio_path or not self._serial:
            result["tts_send_error"] = "missing_serial_or_path"
            return
        recipient_name = str(
            cfg.get("share_recipient_name")
            or result.get("peer_name") or ""
        ).strip()
        if not recipient_name:
            result["tts_send_error"] = "missing_recipient"
            return
        try:
            from src.integrations.whatsapp_rpa.voice_sender import WhatsAppVoiceSender
            sender = WhatsAppVoiceSender(self._serial, wa_pkg=self._wa_pkg)
            rv = await asyncio.to_thread(
                sender.send_audio_file, audio_path, recipient_name=recipient_name,
            )
            result["tts_send_ok"] = rv.ok
            result["tts_send_remote"] = rv.remote_path
            result["tts_send_extra"] = rv.extra
            if not rv.ok:
                result["tts_send_error"] = rv.error
                logger.warning("[wa_rpa] TTS send fail: %s", rv.error)
            else:
                logger.info("[wa_rpa] TTS voice sent ok to %s", recipient_name)
        except Exception as ex:
            result["tts_send_error"] = f"{type(ex).__name__}: {ex}"
            logger.debug("[wa_rpa] TTS send 异常", exc_info=True)

    # ── 拟人节奏发送（支持分条 + 重试） ─────────────────────────────────

    async def _pace_and_send(
        self, xml_bytes: Optional[bytes], text: str
    ) -> Dict[str, Any]:
        # G1 全局 Kill-Switch（Phase C：RPA 覆盖）：紧急冻结时跳过物理发送
        try:
            from src.integrations.shared.rpa_send_guard import rpa_send_blocked
            _ks_on, _ks_scope = rpa_send_blocked("whatsapp", self._account_id)
            if _ks_on:
                logger.warning("[wa_rpa][kill-switch] 冻结发送，跳过（scope=%s）", _ks_scope)
                return {"ok": False, "error": "kill_switch", "scope": _ks_scope, "parts": []}
        except Exception:
            pass
        pacing = self._pacing
        if pacing.enabled:
            await asyncio.sleep(jitter_ms(pacing.read_pause_ms_lo, pacing.read_pause_ms_hi))

        parts = split_message(text, pacing) if pacing.enabled else [text.strip()]
        if not parts:
            return {"ok": False, "error": "empty_after_split", "parts": []}
        if len(parts) > 1:
            logger.warning("[wa_rpa] 拆分为 %d 条消息: %s", len(parts),
                           [p[:20] for p in parts])

        results: List[Dict] = []
        overall_ok = True
        last_xml = xml_bytes
        redump_before_send = bool(self._cfg_get("redump_before_send", True))

        for idx, piece in enumerate(parts):
            if pacing.enabled and not pacing.slow_type:
                await asyncio.sleep(min(3.5, typing_duration_sec(piece, pacing)))

            if redump_before_send:
                redump_xml, _ = await asyncio.to_thread(self._dump_ui_xml)
                if redump_xml:
                    last_xml = redump_xml

            t0 = time.time()
            send_res = await asyncio.to_thread(self._send_text, last_xml, piece)
            if not send_res.get("ok"):
                # 1 次自动重试
                await asyncio.sleep(1.5)
                redump_xml, _ = await asyncio.to_thread(self._dump_ui_xml)
                send_res = await asyncio.to_thread(
                    self._send_text, redump_xml or last_xml, piece
                )
                send_res["retried"] = True
            send_res["took_ms"] = int((time.time() - t0) * 1000)
            send_res["text"] = piece
            results.append(send_res)
            if not send_res.get("ok"):
                overall_ok = False
                logger.warning(
                    "[wa_rpa] send part %d/%d failed err=%s retried=%s piece=%r",
                    idx + 1, len(parts),
                    send_res.get("error", "?"),
                    send_res.get("retried"),
                    piece[:40],
                )
                break
            if pacing.enabled and idx < len(parts) - 1:
                await asyncio.sleep(jitter_ms(pacing.inter_msg_ms_lo, pacing.inter_msg_ms_hi))

        out: Dict[str, Any] = {"ok": overall_ok, "parts": results, "parts_count": len(parts)}
        if not overall_ok and results:
            last_fail = results[-1]
            out["error"] = str(last_fail.get("error") or "send_part_failed")
        return out

    # ── P4-B: 手动发送队列处理 ────────────────────────────────────────────────────────────────────

    async def _handle_queued_send(
        self, item: Dict[str, Any], t0: float, *, dry_run: bool = False
    ) -> Dict[str, Any]:
        """P4-B: 处理一条手动发送任务：在聊天列表找到联系人，点进去，发送文本。"""
        result: Dict[str, Any] = {
            "ok": False, "step": "queued_send", "ts": t0,
            "peer_text": "", "reply_text": item.get("text", ""),
            "chat_key": item.get("chat_key", ""),
        }
        item_id = int(item.get("id") or 0)
        peer_name = (item.get("peer_name") or "").strip()
        text = (item.get("text") or "").strip()
        chat_key = item.get("chat_key", "")

        if not text or not peer_name:
            if self._state_store:
                self._state_store.mark_send_queue_item(item_id, "failed", "empty_text_or_name")
            result["error"] = "empty_text_or_name"
            return self._finish(result, t0)

        # P5-3: dump + 最多滚动 3 次重试，确保联系人可见
        xml_bytes, _ = await asyncio.to_thread(self._dump_ui_xml)
        tap_xy: Optional[Tuple[int, int]] = None
        for _scroll_try in range(4):
            if xml_bytes:
                tap_xy = ui.find_chat_row_by_name(xml_bytes, peer_name, wa_pkg=self._wa_pkg)
            if tap_xy is not None:
                break
            if _scroll_try < 3:
                # 向下滚动半屏再重新 dump
                await asyncio.to_thread(
                    adb.run_adb,
                    ["shell", f"input swipe 540 900 540 300"],
                    serial=self._serial, timeout=5.0,
                )
                await asyncio.sleep(0.8)
                xml_bytes, _ = await asyncio.to_thread(self._dump_ui_xml)

        # P5-3 兜底：尝试 WA 内置搜索
        if tap_xy is None and xml_bytes:
            search_btn = ui.find_search_button(xml_bytes, wa_pkg=self._wa_pkg)
            if search_btn:
                logger.info("[wa_rpa] 手动发送：聊天列表未找到 peer=%r，尝试 WA 搜索", peer_name)
                self._tap(*search_btn)
                await asyncio.sleep(0.8)
                # 通过 ADB 键盘输入联系人名
                await asyncio.to_thread(
                    adb.run_adb,
                    ["shell", f"input text '{peer_name.replace(chr(39), '')}' "],
                    serial=self._serial, timeout=8.0,
                )
                await asyncio.sleep(1.2)
                search_xml, _ = await asyncio.to_thread(self._dump_ui_xml)
                if search_xml:
                    tap_xy = ui.find_chat_row_by_name(search_xml, peer_name, wa_pkg=self._wa_pkg)
                if tap_xy is None:
                    # 清除搜索框，退出搜索模式
                    await asyncio.to_thread(adb.input_keyevent, self._serial, "KEYCODE_BACK")
                    await asyncio.sleep(0.5)

        if tap_xy is None:
            if self._state_store:
                self._state_store.mark_send_queue_item(item_id, "failed", "contact_not_found")
            result["error"] = "contact_not_found"
            result["step"] = "queued_send_not_found"
            logger.warning("[wa_rpa] 手动发送：滚动+搜索仍未找到 peer=%r", peer_name)
            return self._finish(result, t0)

        # 点进会话
        self._tap(*tap_xy)
        await asyncio.sleep(1.5)

        if dry_run:
            if self._state_store:
                self._state_store.mark_send_queue_item(item_id, "sent")
            result["ok"] = True
            result["step"] = "dry_run_done"
            self._back()
            return self._finish(result, t0)

        chat_xml, _ = await asyncio.to_thread(self._dump_ui_xml)
        send_result = await self._pace_and_send(chat_xml, text)

        if send_result.get("ok"):
            if self._state_store:
                self._state_store.mark_send_queue_item(item_id, "sent")
                self._state_store.upsert_chat_state(
                    chat_key, last_reply=text, last_reply_ts=time.time()
                )
            result["ok"] = True
            result["step"] = "queued_send_done"
        else:
            err = str(send_result.get("error", "send_fail"))
            if self._state_store:
                self._state_store.mark_send_queue_item(item_id, "failed", err)
            result["error"] = err
            result["step"] = "queued_send_fail"

        self._back()
        await asyncio.sleep(0.6)
        return self._finish(result, t0)

    # ── MIUI screencap fallback（uiautomator dump 被系统安全功能杀死时使用） ────

    def _read_wa_pending_msgs(self, serial: str) -> List[Dict[str, str]]:
        """从 dumpsys notification 提取 WhatsApp 待读消息（sender, text）列表。
        在 launch WA 之前调用，因为 WA 启动后通知会被清除。
        """
        import re as _re
        try:
            r = adb.run_adb(
                ["shell", "dumpsys notification --noredact 2>/dev/null"],
                serial=serial, timeout=10.0,
            )
            if r.returncode != 0 or not r.stdout:
                return []
            text_out = r.stdout if isinstance(r.stdout, str) else r.stdout.decode("utf-8", errors="replace")
            msgs: List[Dict[str, str]] = []
            _BLOCK_RE = _re.compile(
                r"NotificationRecord[^\n]*pkg=com\.whatsapp[^\n]*\n"
                r"((?:(?!\s*NotificationRecord).+\n?)*)",
                _re.MULTILINE,
            )
            # MIUI V14 格式：android.title=String (VALUE)  ← VALUE 在括号内，无引号
            # 兼容旧格式：android.title=String (N) "VALUE" 或 android.title=String (N) VALUE
            _TITLE_RE = _re.compile(
                r"android\.title\s*=\s*(?:"
                r"String\s*\(([^)\n]+)\)"           # MIUI V14: String (VALUE)
                r"|String\s*\(\d+\)\s*[\"']?([^\"'\n]+)[\"']?"  # 旧格式: String (N) "VALUE"
                r")",
                _re.IGNORECASE,
            )
            # 优先从 android.textLines 取最新一条消息文本
            _TEXTLINES_RE = _re.compile(
                r"android\.textLines\s*=\s*CharSequence\[\][^\n]*\n"
                r"((?:\s+\[\d+\][^\n]+\n?)+)",
                _re.MULTILINE,
            )
            _LINEITEM_RE = _re.compile(r"\[\d+\]\s*(.+)", _re.MULTILINE)
            # 回退：android.text=String (VALUE)
            _TEXT_RE = _re.compile(
                r"android\.text\s*=\s*(?:"
                r"String\s*\(([^)\n]+)\)"
                r"|String\s*\(\d+\)\s*[\"']?([^\"'\n]+)[\"']?"
                r")",
                _re.IGNORECASE,
            )
            _SKIP_SENDERS = {"WhatsApp", "WhatsApp Business", ""}
            seen: set = set()
            for blk_m in _BLOCK_RE.finditer(text_out):
                blk = blk_m.group(1)
                # ── sender
                tm = _TITLE_RE.search(blk)
                sender = ""
                if tm:
                    sender = (tm.group(1) or tm.group(2) or "").strip().rstrip("\"' ")
                if not sender or sender in _SKIP_SENDERS:
                    continue
                # ── message text: prefer last textLine, fallback to android.text
                msg_text = ""
                tl_m = _TEXTLINES_RE.search(blk)
                if tl_m:
                    items = _LINEITEM_RE.findall(tl_m.group(1))
                    if items:
                        msg_text = items[-1].strip()
                if not msg_text:
                    xm = _TEXT_RE.search(blk)
                    if xm:
                        msg_text = (xm.group(1) or xm.group(2) or "").strip().rstrip("\"' ")
                # 过滤 "N条新消息" 汇总文本（只保留真实消息）
                if not msg_text or _re.match(r"^\d+\s*条\s*新\s*消息", msg_text):
                    continue
                key = (sender, msg_text)
                if key in seen:
                    continue
                seen.add(key)
                msgs.append({"sender": sender, "text": msg_text})
            logger.warning(
                "[wa_rpa] 通知预读结果: %d 条 serial=%s raw_len=%d",
                len(msgs), serial, len(text_out),
            )
            return msgs
        except Exception:
            logger.exception("[wa_rpa] _read_wa_pending_msgs 异常 serial=%s", serial)
            return []

    async def _run_screencap_fallback(
        self,
        result: Dict[str, Any],
        t0: float,
        pending_msgs: List[Dict[str, str]],
        *,
        dry_run: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """MIUI 屏蔽 uiautomator 时的 screencap+坐标 fallback 流程：
        1. 确认 WhatsApp 在前台（via dumpsys window）
        2. 如无 pending_msgs 则截图检测是否有未读（通知数已清时放弃）
        3. 使用坐标点击进入第一个会话
        4. 使用 notification 数据 或 OCR 取 peer_msg
        5. 生成回复，用 ADB keyboard 发送
        返回 None 表示 fallback 失败，调用方继续原有 dump_fail 路径。
        """
        serial = self._serial
        if not serial:
            return None

        size = adb.screen_size(serial) or (720, 1600)
        sw, sh = size[0], size[1]

        # 1. screencap 验证 WA 在前台（替代 dumpsys visible=true 检查——后者在 MIUI V14 shell 内 = 号不匹配）
        _scr_bytes = b""
        _wa_in_fg = False
        for _fg_try in range(3):
            _scr_b, _scr_err, _scr_rc = await asyncio.to_thread(
                adb.run_adb_binary, ["exec-out", "screencap", "-p"],
                serial=serial, timeout=20.0,
            )
            if _scr_rc == 0 and _scr_b.startswith(_PNG_MAGIC):
                if _detect_wa_foreground(_scr_b, sw, sh):
                    _scr_bytes = _scr_b
                    _wa_in_fg = True
                    break
            if _fg_try < 2:
                logger.warning(
                    "[wa_rpa][fb] screencap 非WA界面(尝试%d/3)，重新唤起 serial=%s",
                    _fg_try + 1, serial,
                )
                await asyncio.to_thread(
                    adb.run_adb,
                    ["shell", f"input keyevent KEYCODE_HOME ; sleep 0.5 ; "
                     f"monkey -p {self._wa_pkg} -c android.intent.category.LAUNCHER 1"],
                    serial=serial, timeout=12.0,
                )
                await asyncio.sleep(3.0)

        if not _wa_in_fg:
            logger.warning(
                "[wa_rpa][fb] 3次 screencap WA 仍未在前台，放弃 serial=%s",
                serial,
            )
            return None

        # 2. 若无 pending_msgs：从截图扫描未读 badge（MIUI V14 通知已被提前清除）
        if not pending_msgs:
            if _detect_wa_unread_badge(_scr_bytes, sw, sh):
                logger.warning(
                    "[wa_rpa][fb] screencap 检测到未读 badge → 继续处理 serial=%s",
                    serial,
                )
                # 2.1 badge 检测后、进入聊天前：预先尝试抓取语音文件并转写
                #     目的：进入聊天后立即有回复内容，减少「已读不回」窗口
                _badge_sentinel: dict = {"sender": "", "text": ""}
                _vi_cfg = self._cfg_get("voice_input") or {}
                if isinstance(_vi_cfg, dict) and _vi_cfg.get("enabled"):
                    try:
                        _pre_result: Dict[str, Any] = {}
                        _pre_ts = await self._try_transcribe_voice_from_fs(_pre_result)
                        if _pre_ts:
                            _badge_sentinel["text"] = _pre_ts
                            _badge_sentinel["_voice_pretranscribed"] = True
                            logger.warning(
                                "[wa_rpa][fb] badge 预转写成功 → %r serial=%s",
                                _pre_ts[:40], serial,
                            )
                    except Exception as _pre_e:
                        logger.warning("[wa_rpa][fb] badge 预转写失败: %s", _pre_e)
                pending_msgs = [_badge_sentinel]
            else:
                logger.warning(
                    "[wa_rpa][fb] 无通知且 screencap 无badge → no_unread serial=%s",
                    serial,
                )
                result["step"] = "no_unread"
                result["ok"] = True
                return self._finish(result, t0)

        # 4. 坐标点击第一个会话（通知证明有未读，第一个会话即未读）
        # 标准 WhatsApp 布局（320dpi 720x1600）：
        #   状态栏≈47px, 顶栏≈112px, 搜索≈96px, Story行≈168px, 第一行中心≈467px
        _ratio = sh / 1600.0
        _first_row_cy = int(467 * _ratio)
        _cx = sw // 2
        logger.warning(
            "[wa_rpa][fb] 坐标点击第一会话 (%d,%d) pending=%r serial=%s",
            _cx, _first_row_cy, pending_msgs[0]["sender"], serial,
        )
        await asyncio.to_thread(adb.input_tap, serial, _cx, _first_row_cy)
        await asyncio.sleep(2.5)

        # 4. 取 peer_msg（优先 notification 数据，免去 XML 解析）
        msg = pending_msgs[0]
        peer_name = msg["sender"]
        peer_text = msg["text"]
        _voice_transcribed = bool(msg.get("_voice_pretranscribed"))
        if _voice_transcribed:
            result["voice_transcript"] = peer_text
            result["voice_detected_from_badge_pretranscribe"] = True
        chat_key = f"wa:{self._account_id}:{peer_name}"
        result["chat_key"] = chat_key
        result["peer_name"] = peer_name

        # 4.0 screencap-badge 兜底：通知文本为空时（MIUI 提前清除），
        #     dump 聊天界面 XML 获取真实消息内容和语音气泡
        if not peer_text:
            try:
                _dump_path = str(self._cfg_get("dump_remote_path") or "/sdcard/wa_rpa_dump.xml")
                _chat_xml_raw, _ = await asyncio.to_thread(
                    adb.dump_ui, serial, _dump_path
                )
                if _chat_xml_raw:
                    _voice_msg_xml = ui.detect_voice_messages(_chat_xml_raw, screen_width=sw)
                    if _voice_msg_xml:
                        logger.warning(
                            "[wa_rpa][fb] screencap-badge 通知空→XML检测到语音气泡，先转写 serial=%s", serial
                        )
                        result["voice_detected_from_xml"] = True
                        _transcript = await self._try_transcribe_voice_from_fs(result)
                        if _transcript:
                            peer_text = _transcript
                            _voice_transcribed = True
                            result["voice_transcript"] = _transcript
                        else:
                            peer_text = "[对方发送了一条语音消息]"
                            result["voice_transcribe_fallback"] = True
                    else:
                        _xml_peer_text, _ = ui.pick_last_incoming_text(
                            _chat_xml_raw, wa_pkg=self._wa_pkg, screen_width=sw
                        )
                        if _xml_peer_text:
                            peer_text = _xml_peer_text
                            logger.warning(
                                "[wa_rpa][fb] screencap-badge 通知空→XML读到: %r serial=%s",
                                peer_text[:40], serial,
                            )
                    if not peer_name:
                        _cn = ui.find_chat_title(_chat_xml_raw)
                        if _cn:
                            peer_name = _cn
                            chat_key = f"wa:{self._account_id}:{peer_name}"
                            result["chat_key"] = chat_key
                            result["peer_name"] = peer_name
            except Exception as _e:
                logger.warning("[wa_rpa][fb] screencap-badge XML fallback 失败: %s", _e)

        # 4.1 通知中检测语音消息标记 → 从文件系统转写
        if not _voice_transcribed and _is_voice_notification(peer_text):
            logger.info("[wa_rpa][fb] 检测到语音通知: %r → 尝试文件系统转写", peer_text)
            result["voice_detected_from_notification"] = True
            _transcript = await self._try_transcribe_voice_from_fs(result)
            if _transcript:
                peer_text = _transcript
                _voice_transcribed = True
                result["voice_transcript"] = _transcript
            else:
                # 语音转写失败 → 给 AI 一个有意义的提示
                peer_text = "[对方发送了一条语音消息]"
                result["voice_transcribe_fallback"] = True

        result["peer_text"] = peer_text

        # 4.2 最终兜底：所有手段都无法获取消息内容 → 退出不回复，避免已读不回
        if not peer_text or not peer_text.strip():
            logger.warning(
                "[wa_rpa][fb] 无法获取消息内容（通知/XML均为空），退出不消费已读 serial=%s", serial
            )
            result["step"] = "no_peer_text"
            result["ok"] = True
            await asyncio.to_thread(adb.input_keyevent, serial, "KEYCODE_BACK")
            return self._finish(result, t0)

        logger.warning(
            "[wa_rpa][fb] peer=%r msg=%r chat=%s serial=%s voice=%s",
            peer_name, peer_text[:40], chat_key, serial, _voice_transcribed,
        )

        # 5. 去重：已回复过的消息在 dedup_window 内不重复回复
        peer_hash = hashlib.sha256(peer_text.encode()).hexdigest()[:16]
        state = (self._state_store.get_chat_state(chat_key) if self._state_store else {})
        _is_repeat = state.get("last_peer_hash") == peer_hash
        if _is_repeat:
            # 用 last_reply_ts（上次实际发送回复的时间）而非 last_peer_ts
            # 防止轮询间隔 > dedup_window 导致每轮都重复回复同一条消息
            _last_reply_ts = float(state.get("last_reply_ts") or 0)
            _dedup_window = float(self._cfg_get("dedup_window_sec", 3600))
            if _last_reply_ts > 0 and (time.time() - _last_reply_ts) < _dedup_window:
                result["step"] = "already_replied"
                result["ok"] = True
                await asyncio.to_thread(adb.input_keyevent, serial, "KEYCODE_BACK")
                return self._finish(result, t0)
            if _last_reply_ts > 0:
                logger.info(
                    "[wa_rpa][fb] 用户重复提问（%.0fs 前回复过），放行 chat=%s",
                    time.time() - _last_reply_ts, chat_key,
                )

        # 6. 回复模式检查
        reply_mode = str(self._cfg_get("reply_mode", "auto"))
        if reply_mode == "off":
            result["step"] = "reply_mode_off"
            result["ok"] = True
            if self._state_store:
                self._state_store.upsert_chat_state(
                    chat_key, last_peer_text=peer_text, last_peer_hash=peer_hash,
                    last_peer_ts=t0,
                )
            await asyncio.to_thread(adb.input_keyevent, serial, "KEYCODE_BACK")
            return self._finish(result, t0)

        # P15-d: 用户停止联系检测（关键词 → 黑名单/静默）
        # P15-e: 递进式防骚扰 - 首次命中静默，二次命中黑名单
        # P15-f: 轻量意图检测（替代纯关键词，降低误判）
        stop_words = self._cfg_get("stop_contact_keywords") or [
            "stop", "unsubscribe", "do not contact", "别联系", "停止联系", "不要再发",
        ]

        # 双层检测：意图检测器 + 关键词兜底
        intent_result = self._stop_contact_detector.detect(peer_text)
        keyword_match = any((pat or "").lower() in peer_text.lower() for pat in stop_words)

        # 判定逻辑：意图检测器高置信度 或 关键词匹配
        is_stop_contact = intent_result["is_stop_contact"] or (
            keyword_match and intent_result["confidence"] < 0.5  # 低置信度时用关键词兜底
        )

        if is_stop_contact:
            # 递进策略：首次命中静默，二次命中黑名单
            escalation_hours = float(self._cfg_get("stop_contact_escalation_hours", 72))
            is_second_hit = False
            if self._state_store and escalation_hours > 0:
                try:
                    # 检查最近 escalation_hours 小时内是否有 stop_contact 记录
                    tl = self._state_store.timeline(minutes=int(escalation_hours * 60), limit=100)
                    for rec in tl:
                        if rec.get("kind") == "stop_contact":
                            detail = json.loads(rec.get("detail") or "{}")
                            if detail.get("chat_key") == chat_key:
                                is_second_hit = True
                                break
                except Exception:
                    is_second_hit = False

            if self._state_store:
                # 首次仅静默，二次才黑名单
                if is_second_hit and self._stop_contact_blacklist:
                    self._state_store.upsert_chat_state(chat_key, blacklist=1)
                    _action = "blacklist+quiet"
                else:
                    _action = "quiet_only"

                if self._stop_contact_quiet_minutes > 0:
                    self._state_store.upsert_chat_state(
                        chat_key,
                        quiet_until=time.time() + self._stop_contact_quiet_minutes * 60,
                    )
                try:
                    self._state_store.insert_timeline(
                        "stop_contact",
                        f"chat={chat_key} {_action}",
                        {
                            "chat_key": chat_key,
                            "peer_name": peer_name,
                            "action": _action,
                            "blacklist": is_second_hit and self._stop_contact_blacklist,
                            "quiet_minutes": self._stop_contact_quiet_minutes,
                            "intent": intent_result,  # 记录意图检测结果
                            "keyword_match": keyword_match,
                            "is_second_hit": is_second_hit,
                            "escalation_hours": escalation_hours,
                        },
                    )
                except Exception:
                    logger.debug("[wa_rpa][stop_contact] timeline insert failed", exc_info=True)
            result["step"] = "user_stop_contact"
            result["ok"] = True
            logger.warning(
                "[wa_rpa][stop_contact] chat=%s action=%s intent_conf=%.2f keyword=%s",
                chat_key, _action if self._state_store else "none",
                intent_result.get("confidence", 0), keyword_match
            )
            await asyncio.to_thread(adb.input_keyevent, serial, "KEYCODE_BACK")
            return self._finish(result, t0)

        if dry_run:
            result["step"] = "dry_run_done"
            result["ok"] = True
            return self._finish(result, t0)

        # 7. AI 生成回复
        lang = str(self._cfg_get("default_reply_lang", "zh"))
        req_id = f"warpa-fb-{uuid.uuid4().hex[:12]}"
        _style_hint = str(self._cfg_get("reply_style_hint") or "").strip()
        if not _style_hint:
            _style_hint = _WA_DEFAULT_STYLE_HINT
        ctx: Dict[str, Any] = {
            "chat_id": chat_key,
            "request_id": req_id,
            "channel": "whatsapp_rpa",
            "platform": "whatsapp_rpa",
            "reply_lang": lang,
            "whatsapp_rpa_chat_key": chat_key,
            "whatsapp_rpa_peer_name": peer_name,
            "whatsapp_rpa_style_hint": _style_hint,
            "account_persona_id": self._account_persona_id(),
        }
        # 注入上次回复 → 激活角度轮换/反重复
        _last_reply = (state.get("last_reply") or "").strip()
        if _last_reply:
            ctx["last_reply"] = _last_reply
        # ★ 必须始终设置，否则旧的 True 值会残留在 user_context 中
        if _is_repeat and _last_reply:
            ctx["_is_repeated_message"] = True
            ctx["_prev_reply_for_repeat"] = _last_reply
        else:
            ctx["_is_repeated_message"] = False
            ctx["_prev_reply_for_repeat"] = ""
        try:
            reply_text = await self._sm.process_message(
                peer_text.strip(), user_id=chat_key, context=ctx,
            )
        except Exception as e:
            result["error"] = f"skill_error:{e}"
            result["step"] = "skill_error"
            logger.warning("[wa_rpa][fb] skill_error: %s", e)
            await asyncio.to_thread(adb.input_keyevent, serial, "KEYCODE_BACK")
            return self._finish(result, t0)

        if not reply_text or not str(reply_text).strip():
            result["step"] = "empty_reply"
            result["ok"] = True
            await asyncio.to_thread(adb.input_keyevent, serial, "KEYCODE_BACK")
            return self._finish(result, t0)

        reply_text = str(reply_text).strip()

        # P15-j: 表情增强处理
        if self._emotion_enhancer and reply_text:
            try:
                _emo_ctx = {"suggested_emoticons": []}
                reply_text = self._emotion_enhancer.enhance_reply(
                    original_reply=reply_text,
                    emotion="neutral",
                    context_analysis=_emo_ctx,
                    message_text=peer_text.strip(),
                    chat_id=chat_key,
                )
                reply_text = reply_text.strip()
            except Exception as _ee_err:
                logger.debug("[wa_rpa] emotion enhance failed (fb): %s", _ee_err)

        result["reply_text"] = reply_text

        if reply_mode == "approve":
            if self._state_store:
                pid = self._state_store.insert_pending(
                    chat_key=chat_key, peer_name=peer_name,
                    peer_text=peer_text, proposed_reply=reply_text,
                )
                self._state_store.upsert_chat_state(
                    chat_key, last_peer_text=peer_text, last_peer_hash=peer_hash,
                    last_peer_ts=t0,
                )
                # P13-B: fire-and-forget TTS preview for approval panel
                if pid:
                    _tts_lang = str(result.get("tts_lang") or result.get("reply_lang") or
                                    self._cfg_get("default_reply_lang") or "zh")
                    asyncio.create_task(self._generate_pending_tts(pid, reply_text, _tts_lang))
            result["step"] = "pending_queued"
            result["ok"] = True
            await asyncio.to_thread(adb.input_keyevent, serial, "KEYCODE_BACK")
            return self._finish(result, t0)

        # 8. 坐标发送（不依赖 XML）
        send_res = await self._send_text_coord_fallback(serial, reply_text, (sw, sh))
        result["send"] = send_res
        if send_res.get("ok"):
            result["step"] = "sent"
            result["ok"] = True
            _now_ts = time.time()
            if self._state_store:
                # P15-g: 检查是否是对 proactive 消息的回复
                _tpl_info = None
                try:
                    _last_tpl = state.get("last_proactive_template")
                    if _last_tpl:
                        _tpl_info = json.loads(_last_tpl)
                        # 48 小时内视为对 proactive 的有效回复
                        if _tpl_info and (_now_ts - _tpl_info.get("ts", 0)) < 48 * 3600:
                            self._state_store.insert_timeline(
                                "proactive_replied",
                                f"chat={chat_key} replied to proactive",
                                {
                                    "chat_key": chat_key,
                                    "template_category": _tpl_info.get("category"),
                                    "template_idx": _tpl_info.get("idx"),
                                    "proactive_ts": _tpl_info.get("ts"),
                                    "reply_latency_hours": round((_now_ts - _tpl_info.get("ts", _now_ts)) / 3600, 2),
                                },
                            )
                            logger.debug(
                                "[wa_rpa][proactive] recorded reply cat=%s idx=%s chat=%s",
                                _tpl_info.get("category"), _tpl_info.get("idx"), chat_key
                            )
                except Exception:
                    pass  # 静默失败，不影响主流程

                self._state_store.upsert_chat_state(
                    chat_key,
                    last_peer_text=peer_text,
                    last_peer_hash=peer_hash,
                    last_reply=reply_text,
                    last_peer_ts=t0,
                    last_reply_ts=_now_ts,
                )
            logger.warning(
                "[wa_rpa][fb] 坐标发送成功 chat=%s reply=%r", chat_key, reply_text[:40]
            )
        else:
            result["step"] = "send_fail"
            result["error"] = str(send_res.get("error", ""))

        await asyncio.to_thread(adb.input_keyevent, serial, "KEYCODE_BACK")
        return self._finish(result, t0)

    # ── 多条消息引用回复 ─────────────────────────────────────────────────────────

    def _find_context_menu_reply(self, xml_bytes: bytes) -> Optional[Tuple[int, int]]:
        """在长按弹出的 context menu 里找「回复」按钮坐标。"""
        import xml.etree.ElementTree as _ET
        LABELS = {"回复", "Reply", "回覆", "Répondre", "Responder"}
        try:
            root = _ET.fromstring(xml_bytes)
        except Exception:
            return None
        for el in root.iter():
            text = (el.get("text") or "").strip()
            cd = (el.get("content-desc") or "").strip()
            if text in LABELS or any(lbl in cd for lbl in LABELS):
                bb = ui._parse_bounds(el.get("bounds") or "")
                if bb:
                    l, t, r, b = bb
                    return (l + r) // 2, (t + b) // 2
        return None

    async def _quote_reply_msg(
        self, serial: str, msg_cx: int, msg_cy: int,
        reply_text: str, screen_size: Tuple[int, int],
    ) -> Dict[str, Any]:
        """长按消息气泡 → 点「回复」→ 输入文字 → 发送。失败时返回 ok=False。"""
        await asyncio.to_thread(adb.input_swipe, serial, msg_cx, msg_cy, msg_cx, msg_cy, 900)
        await asyncio.sleep(1.3)
        try:
            xml_bytes = await asyncio.to_thread(adb.dump_ui, serial)
        except Exception as e:
            return {"ok": False, "error": f"dump_fail:{e}"}
        btn = self._find_context_menu_reply(xml_bytes)
        if not btn:
            logger.warning("[wa_rpa][multi] 未找到「回复」按钮，降级普通发送 msg_cy=%d", msg_cy)
            # 按 BACK 关闭可能打开的菜单
            await asyncio.to_thread(adb.input_keyevent, serial, "KEYCODE_BACK")
            await asyncio.sleep(0.4)
            return {"ok": False, "error": "no_reply_btn"}
        await asyncio.to_thread(adb.input_tap, serial, btn[0], btn[1])
        await asyncio.sleep(0.7)
        return await self._send_text_coord_fallback(serial, reply_text, screen_size)

    async def _scroll_find_quote_targets(
        self,
        peer_text: str,
        sw: int,
        sh: int,
    ) -> "List[ui.IncomingMessage]":
        """向上滚屏寻找已滚出视野的指定回复目标消息。

        最多尝试 3 次向上翻屏；找到目标后立即返回（不再继续滚）。
        若 3 次均未找到，执行一次大幅向下滚屏恢复底部视图，返回空列表。
        WhatsApp 在 quote reply 发送后会自动滚回底部，因此只需在此找到坐标即可。
        """
        for _ in range(3):
            await asyncio.to_thread(
                adb.input_swipe,
                self._serial, sw // 2, int(sh * 0.35), sw // 2, int(sh * 0.72), 400,
            )
            await asyncio.sleep(0.7)
            try:
                _xml = await asyncio.to_thread(adb.dump_ui, self._serial)
            except Exception:
                break
            if not _xml:
                break
            _msgs = ui.pick_all_visible_incoming(_xml, screen_width=sw)
            _is_req, _targets = _detect_quote_targets(peer_text, _msgs)
            if _targets:
                logger.info("[wa_rpa][scroll_up] found %d target(s) for quote", len(_targets))
                return _targets
        # 未找到 → 滚回底部恢复正常视图
        await asyncio.to_thread(
            adb.input_swipe,
            self._serial, sw // 2, int(sh * 0.65), sw // 2, int(sh * 0.12), 700,
        )
        await asyncio.sleep(0.5)
        return []

    async def _handle_multi_quote_reply(
        self,
        serial: str,
        chat_key: str,
        targets: "List[ui.IncomingMessage]",
        state: Dict[str, Any],
        ctx: Dict[str, Any],
        result: Dict[str, Any],
        t0: float,
        sw: int = 1080,
        sh: int = 1920,
    ) -> Optional[Dict[str, Any]]:
        """处理用户主动请求多条指定回复（"那两条都回了"）。

        策略：并行 AI 调用 → 顺序 quote reply（与 multi_intent 流程对齐）。
        若全部发送失败则返回 None（降级到普通流程）。
        """
        # 并行获取各目标的 AI 回复
        async def _ai_reply(target: "ui.IncomingMessage") -> Optional[str]:
            try:
                r = await self._sm.process_message(
                    target.text.strip(), user_id=chat_key, context=dict(ctx),
                )
                return str(r).strip() if r else None
            except Exception as e:
                logger.warning("[wa_rpa][multi_quote] AI error target=%r: %s", target.text[:30], e)
                return None

        replies = await asyncio.gather(*[_ai_reply(tgt) for tgt in targets])

        sent_count = 0
        last_reply_text = ""
        for tgt, reply in zip(targets, replies):
            if not reply:
                continue
            send_res = await self._quote_reply_msg(serial, tgt.cx, tgt.cy, reply, (sw, sh))
            if send_res.get("ok"):
                self._quote_reply_fail_streak = 0
            else:
                self._quote_reply_fail_streak += 1
                send_res = await self._send_text_coord_fallback(serial, reply, (sw, sh))
            if send_res.get("ok"):
                sent_count += 1
                last_reply_text = reply
                await asyncio.sleep(0.8)

        if sent_count == 0:
            return None   # 全部失败 → 降级

        last_tgt = targets[-1]
        if self._state_store:
            _h = hashlib.sha256(last_tgt.text.encode()).hexdigest()[:16]
            self._state_store.upsert_chat_state(
                chat_key,
                last_peer_text=last_tgt.text,
                last_peer_hash=_h,
                last_reply=last_reply_text,
                last_peer_ts=t0,
                last_reply_ts=time.time(),
            )
        result.update(
            step="multi_quote_sent",
            ok=True,
            reply_text=last_reply_text,
            peer_text=last_tgt.text,
            multi_quote_count=sent_count,
        )
        logger.warning(
            "[wa_rpa][multi_quote] sent=%d/%d chat=%s", sent_count, len(targets), chat_key,
        )
        self._back()
        await asyncio.sleep(0.4)
        return self._finish(result, t0)

    async def _handle_multi_msg_flow(
        self,
        serial: str,
        chat_key: str,
        new_msgs: List,
        chat_xml: bytes,
        state: Dict[str, Any],
        ctx: Dict[str, Any],
        result: Dict[str, Any],
        t0: float,
    ) -> Optional[Dict[str, Any]]:
        """多条消息处理主流程。

        - casual / combined (单组): 返回 None → 由主流程处理（兼容现有逻辑）
        - multi_intent (多组): 逐组引用回复 → 返回 result（主流程直接使用）
        """
        from src.integrations.whatsapp_rpa.multi_msg_handler import analyze_multi_msg

        ai_client = getattr(self._sm, "ai_client", None)
        if not ai_client:
            return None  # 无 AI 客户端，退回主流程

        screen_w = adb.screen_size(serial)
        sw, sh = (screen_w[0], screen_w[1]) if screen_w else (1080, 1920)

        analysis = await analyze_multi_msg(new_msgs, ai_client)
        logger.info(
            "[wa_rpa][multi] mode=%s groups=%d msgs=%d chat=%s",
            analysis.mode, len(analysis.groups), len(new_msgs), chat_key,
        )

        if analysis.mode in ("casual", "combined"):
            # 合并文字 → 让主流程用合并后的 peer_text
            combined = analysis.groups[0].combined_text if analysis.groups else new_msgs[-1].text
            if analysis.mode == "combined" and len(new_msgs) > 1:
                result["peer_text"] = combined
                ctx["_multi_msg_combined"] = True
                ctx["_multi_msg_count"] = len(new_msgs)
                # ★ 记录真实最后一条消息文本供 state 锚点使用，避免合并文本污染下次检测
                result["_multi_real_last_peer"] = new_msgs[-1].text
            # 返回 None 让主流程继续（用更新后的 peer_text）
            return None

        # multi_intent: Phase 1 — 并行 AI 生成所有回复（asyncio.gather 并发）
        _total_groups = len(analysis.groups)

        async def _ai_for_group(grp: "MsgGroup") -> Optional[str]:
            grp_ctx = dict(ctx)
            grp_ctx["_multi_msg_group_topic"] = grp.topic
            grp_ctx["_multi_msg_total_groups"] = _total_groups
            if grp.topic:
                grp_ctx["_current_user_message_for_lang"] = grp.reply_to.text
            try:
                r = await self._sm.process_message(
                    grp.combined_text.strip(),
                    user_id=chat_key,
                    context=grp_ctx,
                )
                return str(r).strip() if r and str(r).strip() else None
            except Exception as e:
                logger.warning("[wa_rpa][multi] ai_error group=%r: %s", grp.topic, e)
                return None

        ai_replies = await asyncio.gather(*[_ai_for_group(g) for g in analysis.groups])
        logger.info(
            "[wa_rpa][multi] parallel AI done groups=%d replies=%s",
            _total_groups, [bool(r) for r in ai_replies],
        )

        # Phase 2 — 顺序 UI 发送（设备单线操作）
        sent_count = 0
        last_peer_text = ""
        last_reply_text = ""
        for grp, reply_text in zip(analysis.groups, ai_replies):
            if not reply_text:
                continue

            send_res = await self._quote_reply_msg(
                serial, grp.reply_to.cx, grp.reply_to.cy, reply_text, (sw, sh)
            )
            if not send_res.get("ok"):
                # 降级：普通发送
                send_res = await self._send_text_coord_fallback(serial, reply_text, (sw, sh))

            if send_res.get("ok"):
                sent_count += 1
                last_peer_text = grp.reply_to.text
                last_reply_text = reply_text
                await asyncio.sleep(0.8)  # 两条回复间间隔

        if sent_count > 0 and self._state_store:
            import hashlib as _hl
            last_hash = _hl.sha256(new_msgs[-1].text.encode()).hexdigest()[:16]
            self._state_store.upsert_chat_state(
                chat_key,
                last_peer_text=new_msgs[-1].text,
                last_peer_hash=last_hash,
                last_reply=last_reply_text,
                last_peer_ts=t0,
                last_reply_ts=time.time(),
            )
            result["step"] = "multi_sent"
            result["ok"] = True
            result["reply_text"] = last_reply_text
            result["peer_text"] = new_msgs[-1].text
            result["multi_sent_count"] = sent_count
            logger.warning(
                "[wa_rpa][multi] step=multi_sent groups=%d sent=%d chat=%s",
                len(analysis.groups), sent_count, chat_key,
            )
            self._back()
            await asyncio.sleep(0.4)
            return self._finish(result, t0)

        # 全部发送失败 → 退回主流程用最新一条
        logger.warning("[wa_rpa][multi] all groups failed, fallback single chat=%s", chat_key)
        return None

    async def _send_text_coord_fallback(
        self, serial: str, text: str, screen_size: Tuple[int, int]
    ) -> Dict[str, Any]:
        """坐标+ADB keyboard 发送文字（不依赖 XML/uiautomator）。
        适用于 MIUI 屏蔽 uiautomator 的情况。
        """
        sw, sh = screen_size
        # WhatsApp 输入框：底部上方约 4.4%（720x1600 → Y≈1530）
        _input_y = int(sh * 0.956)
        _input_x = int(sw * 0.45)
        _send_x = int(sw * 0.94)
        _send_y = _input_y
        try:
            await asyncio.to_thread(adb.input_tap, serial, _input_x, _input_y)
            await asyncio.sleep(0.5)
            await asyncio.to_thread(self._clear_focused_input, serial)
            await asyncio.sleep(0.2)
            _sent = False
            # 优先 ADB keyboard broadcast（支持中文/emoji）
            try:
                if adb.is_adbkeyboard_installed(serial):
                    await asyncio.to_thread(
                        adb.adb_keyboard_input_text, serial, text, True, self._wa_pkg
                    )
                    _sent = True
            except Exception:
                pass
            if not _sent:
                # 剪贴板粘贴 fallback
                try:
                    await asyncio.to_thread(adb.clipboard_paste, serial, text)
                    _sent = True
                except Exception:
                    pass
            if not _sent:
                return {"ok": False, "error": "text_inject_fail"}
            await asyncio.sleep(0.5)
            # 点发送按钮
            await asyncio.to_thread(adb.input_tap, serial, _send_x, _send_y)
            await asyncio.sleep(0.5)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── 主执行轮次 ──────────────────────────────────────────────────────────────────────────────────────────────────────

    async def run_once(self, *, dry_run: bool = False) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "ok": False, "step": "init", "ts": time.time(),
            "peer_text": "", "reply_text": "", "chat_key": "",
        }
        t0 = time.time()
        self._wa_launched = False  # track whether WA was brought to foreground
        try:
            return await self._run_once_inner(result, t0, dry_run=dry_run)
        finally:
            # ★ 每次 run_once 结束后，如果 WA 曾被拉起，按 HOME 退到后台
            #   确保新消息能产生通知（WA 在前台时消息直接已读，无通知）
            await self._async_go_home()

    async def _run_once_inner(
        self,
        result: Dict[str, Any],
        t0: float,
        *,
        dry_run: bool = False,
    ) -> Dict[str, Any]:

        # 1) 解析串口
        self._serial = self._resolve_serial()
        if not self._serial:
            result["error"] = "no_adb_device"
            result["step"] = "no_adb_device"
            return self._finish(result, t0)

        # 2) 拉起 WhatsApp 前台，强制回到聊天列表
        # MIUI-fix: 在 launch 之前先读取 pending 通知（WA 启动后通知会被清除）
        _wa_pending_msgs: List[Dict[str, str]] = []
        if not dry_run:
            _wa_pending_msgs = await asyncio.to_thread(
                self._read_wa_pending_msgs, self._serial
            )
            if _wa_pending_msgs:
                logger.warning(
                    "[wa_rpa] 通知预读：%d 条 WA 待读 %s",
                    len(_wa_pending_msgs),
                    [(m["sender"], m["text"][:20]) for m in _wa_pending_msgs],
                )
            else:
                # ★ 快速短路：无通知 + 无队列 + 距上次全量检查 < 90s → 不扰动手机
                _has_queued = False
                if self._state_store:
                    try:
                        _has_queued = self._state_store.has_pending_send()
                    except Exception:
                        pass
                _now_t = time.time()
                _force_badge_scan = (
                    (_now_t - self._last_wa_full_check_ts) >= float(
                        self._cfg_get("badge_scan_interval_sec", 90)
                    )
                )
                if not _has_queued and not _force_badge_scan:
                    result["step"] = "no_unread"
                    result["ok"] = True
                    return self._finish(result, t0)
                if not _has_queued:
                    logger.warning(
                        "[wa_rpa] 全量 badge 扫描（%.0fs 无通知）serial=%s",
                        _now_t - self._last_wa_full_check_ts, self._serial,
                    )
                self._last_wa_full_check_ts = _now_t
            # ★ 横屏保护：启动 WA 前强制竖屏
            await asyncio.to_thread(
                adb.run_adb,
                ["shell", "settings put system accelerometer_rotation 0 ; "
                 "settings put system user_rotation 0"],
                serial=self._serial, timeout=8.0,
            )
            # ★ badge 扫描时跳过 force-stop（避免冷启动耗时 >15s 导致前台检测失败）
            # 有通知时仍 force-stop 保证 WA 干净状态
            _badge_scan_only = not bool(_wa_pending_msgs)
            if not _badge_scan_only:
                await asyncio.to_thread(
                    adb.run_adb,
                    ["shell", f"am force-stop {self._wa_pkg}"],
                    serial=self._serial, timeout=10.0,
                )
                await asyncio.sleep(1.2)
            # MIUI-fix: 先唤醒屏幕，再回 Home（屏幕关闭时 KEYCODE_HOME 无效，monkey 注入成功但 WA 不在前台）
            await asyncio.to_thread(
                adb.run_adb,
                ["shell", "input keyevent KEYCODE_WAKEUP; sleep 0.3; input keyevent KEYCODE_HOME"],
                serial=self._serial, timeout=5.0,
            )
            await asyncio.sleep(0.8)
            # MIUI-fix: 用 monkey 绕过 MIUI V14 后台启动限制
            # 若连续 3 次被 MIUI 阻断（streak），直接走 am start，省去 monkey 失败开销
            # 超过 6 次 am start 也全失败后自动重置 streak，重新尝试 monkey
            _MONKEY_FAIL_SKIP = 3
            _AM_START_RESET_AT = 6
            if self._monkey_fail_streak >= _AM_START_RESET_AT:
                logger.warning(
                    "[wa_rpa] am start 连续失败 %d 次，重置 monkey streak serial=%s",
                    self._monkey_fail_streak, self._serial,
                )
                self._monkey_fail_streak = 0
            _use_am_start_direct = (self._monkey_fail_streak >= _MONKEY_FAIL_SKIP)
            if _use_am_start_direct:
                logger.info(
                    "[wa_rpa] monkey-skip (streak=%d): am start direct serial=%s",
                    self._monkey_fail_streak, self._serial,
                )
                await asyncio.to_thread(
                    adb.run_adb,
                    ["shell", f"input keyevent KEYCODE_WAKEUP; sleep 0.3; "
                     f"am force-stop {self._wa_pkg}; sleep 1; "
                     f"am start -n {self._wa_pkg}/.HomeActivity 2>&1; echo AMSTART_DONE"],
                    serial=self._serial, timeout=20.0,
                )
                await asyncio.sleep(float(self._cfg_get("after_launch_sleep_sec", 3.0)) + 1.0)
            else:
                # （am start 被 MIUI 后台限制静默忽略；monkey 模拟 Launcher tap，始终生效）
                _launch_script = (
                    f"monkey -p {self._wa_pkg} -c android.intent.category.LAUNCHER 1"
                    f" 2>&1; echo LAUNCH_DONE"
                )
                fg = await asyncio.to_thread(
                    adb.run_adb, ["shell", _launch_script],
                    serial=self._serial, timeout=30.0,
                )
                _launch_out = (fg.stdout or "").strip()
                if "Events injected: 0" in _launch_out or "Error" in _launch_out:
                    logger.warning("[wa_rpa] monkey launch 警告: %s", _launch_out[:200])
                await asyncio.sleep(float(self._cfg_get("after_launch_sleep_sec", 3.0)))
            self._wa_launched = True

        # 2.5) P4-B: 如果手动发送队列有任务，优先处理
        if self._state_store:
            _queued = self._state_store.pop_send_queue_item()
            if _queued:
                logger.info("[wa_rpa] 手动发送任务 id=%s peer=%s",
                            _queued.get("id"), _queued.get("peer_name"))
                return await self._handle_queued_send(_queued, t0, dry_run=dry_run)

        # 3) dump 聊天列表；若卡在聊天内（无 conversations_row）最多按 3 次 BACK 退出
        xml_bytes, reason = await asyncio.to_thread(self._dump_ui_xml)
        if not xml_bytes:
            self._consecutive_dump_fails += 1
            # MIUI-fix: uiautomator dump 被 MIUI 安全功能杀死时 → screencap+坐标 fallback
            # 条件：WA 已启动 + 有通知预读数据 OR 强制尝试
            _miui_blocked = "dump_fail" in reason or reason == "no_xml_in_stdout"
            if _miui_blocked and not dry_run:
                logger.warning(
                    "[wa_rpa] uiautomator dump 被 MIUI 阻断，尝试 screencap+坐标 fallback "
                    "pending_msgs=%d serial=%s",
                    len(_wa_pending_msgs), self._serial,
                )
                _fb_result = await self._run_screencap_fallback(
                    result, t0, _wa_pending_msgs, dry_run=dry_run
                )
                if _fb_result is not None:
                    return _fb_result
            # 连续 3 次失败 → 重启 uiautomator 服务
            if self._consecutive_dump_fails >= 3:
                logger.warning(
                    "[wa_rpa] %d 次连续 dump_fail，重启 uiautomator 服务自恢复",
                    self._consecutive_dump_fails,
                )
                await asyncio.to_thread(
                    adb.run_adb,
                    ["shell", "sleep 1; am force-stop com.github.uiautomator 2>/dev/null; true"],
                    serial=self._serial, timeout=15.0,
                )
                await asyncio.sleep(3.0)
                self._consecutive_dump_fails = 0
            result["error"] = f"dump_fail:{reason}"
            result["step"] = "dump_fail"
            return self._finish(result, t0)
        self._consecutive_dump_fails = 0  # 成功则复位

        for _back_try in range(3):
            if b"conversations_row" in xml_bytes:
                break
            # 不在聊天列表，尝试 BACK 退回
            logger.debug("[wa_rpa] not on chat list (try %d), pressing BACK", _back_try + 1)
            await asyncio.to_thread(adb.input_keyevent, self._serial, "KEYCODE_BACK")
            await asyncio.sleep(1.2)
            _new_xml, _new_reason = await asyncio.to_thread(self._dump_ui_xml)
            if _new_xml:
                xml_bytes = _new_xml

        # MIUI-fix: monkey 被 MIUI 静默阻断时（仍在 Launcher），尝试 am start 直接前台启动
        # ADB shell 拥有比 monkey 更高的权限，可绕过 MIUI V14 后台启动限制
        if b"conversations_row" not in xml_bytes:
            logger.warning(
                "[wa_rpa][diag] no conversations_row after BACK loop: miui_home=%s wa_pkg=%s xml_head=%r serial=%s",
                b"com.miui.home" in xml_bytes,
                self._wa_pkg.encode() in xml_bytes,
                xml_bytes[:120],
                self._serial,
            )
        if b"conversations_row" not in xml_bytes and b"com.miui.home" in xml_bytes:
            if not _use_am_start_direct:
                # monkey 刚刚失败，累计 streak
                self._monkey_fail_streak += 1
                logger.warning(
                    "[wa_rpa] monkey-blocked (streak=%d→%d), am start fallback serial=%s",
                    self._monkey_fail_streak - 1, self._monkey_fail_streak, self._serial,
                )
                await asyncio.to_thread(
                    adb.run_adb,
                    ["shell", f"am force-stop {self._wa_pkg}; sleep 1; "
                     f"am start -n {self._wa_pkg}/.HomeActivity 2>&1; echo AMSTART_DONE"],
                    serial=self._serial, timeout=20.0,
                )
                await asyncio.sleep(float(self._cfg_get("after_launch_sleep_sec", 3.0)) + 1.0)
                _new_xml, _ = await asyncio.to_thread(self._dump_ui_xml)
                if _new_xml:
                    xml_bytes = _new_xml
                    logger.warning(
                        "[wa_rpa] am start fallback 完成 conversations_row=%s wa_pkg=%s serial=%s",
                        b"conversations_row" in xml_bytes,
                        self._wa_pkg.encode() in xml_bytes,
                        self._serial,
                    )
            else:
                # am start 直接模式下仍未到聊天列表；累计 streak 以触发自动重置回 monkey 模式
                self._monkey_fail_streak += 1
                logger.warning(
                    "[wa_rpa] am start direct 后仍在 Launcher (streak=%d) serial=%s",
                    self._monkey_fail_streak, self._serial,
                )
        elif not _use_am_start_direct and not _badge_scan_only and b"conversations_row" in xml_bytes:
            # monkey 在通知模式下成功（badge_scan 模式 WA 可能已打开，不算真正成功），重置 streak
            self._monkey_fail_streak = 0

        # MIUI-fix: WA 开在 setup/onboarding/backup 界面（非聊天列表）时，
        # 自动寻找 "Not now" / "稍后" / "Skip" 按钮并点击跳过，避免每次被阻塞
        _SKIP_TEXTS = {"not now", "skip", "稍后", "以后再说", "暂不", "跳过",
                       "cancel", "取消", "later", "no thanks"}
        if b"conversations_row" not in xml_bytes and b"com.whatsapp" in xml_bytes:
            try:
                import xml.etree.ElementTree as _ET2
                _root2 = _ET2.fromstring(xml_bytes)
                _skip_el = None
                for _el in _root2.iter():
                    _t = (_el.get("text") or "").strip().lower()
                    _cd = (_el.get("content-desc") or "").strip().lower()
                    if _t in _SKIP_TEXTS or _cd in _SKIP_TEXTS:
                        _skip_el = _el
                        break
                if _skip_el is not None:
                    _bb = _skip_el.get("bounds") or ""
                    import re as _re2
                    _bm = _re2.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", _bb)
                    if _bm:
                        _bx = (int(_bm.group(1)) + int(_bm.group(3))) // 2
                        _by = (int(_bm.group(2)) + int(_bm.group(4))) // 2
                        logger.warning(
                            "[wa_rpa] WA setup 界面检测到 skip 按钮 text=%r，点击跳过 serial=%s",
                            _skip_el.get("text") or _skip_el.get("content-desc"), self._serial,
                        )
                        self._tap(_bx, _by)
                        await asyncio.sleep(2.0)
                        _new_xml2, _ = await asyncio.to_thread(self._dump_ui_xml)
                        if _new_xml2:
                            xml_bytes = _new_xml2
                else:
                    logger.warning(
                        "[wa_rpa] WA setup 界面无 skip 按钮，按 BACK 尝试退出 serial=%s",
                        self._serial,
                    )
                    await asyncio.to_thread(adb.input_keyevent, self._serial, "KEYCODE_BACK")
                    await asyncio.sleep(1.5)
                    _new_xml2, _ = await asyncio.to_thread(self._dump_ui_xml)
                    if _new_xml2:
                        xml_bytes = _new_xml2
            except Exception:
                pass

        # 4) 扫描未读行
        unread_rows = ui.scan_unread_chat_rows(xml_bytes, wa_pkg=self._wa_pkg)
        result["unread_count"] = len(unread_rows)

        if not unread_rows:
            result["step"] = "no_unread"
            result["ok"] = True
            # [DIAG] 诊断日志：帮助判断是"不在聊天列表"还是"未读标识解析失败"
            try:
                import xml.etree.ElementTree as _ET
                _root = _ET.fromstring(xml_bytes)
                _rids = {(el.get("resource-id") or "").split("/")[-1]
                         for el in _root.iter() if el.get("resource-id")}
                _has_list = any("conversations_row" in r for r in _rids)
                _unread_rids = [r for r in _rids if "unread" in r or "message_count" in r]
                logger.debug(
                    "[wa_rpa][diag] no_unread: has_conversations_row=%s unread_rids=%s",
                    _has_list, _unread_rids[:5],
                )
                # 扩展诊断：当聊天列表可见但没找到未读标识时，打出所有 conversations_row_* id
                if _has_list and not _unread_rids:
                    _row_rids = sorted(r for r in _rids if "conversations_row" in r)
                    logger.debug(
                        "[wa_rpa][diag] all_conversations_row_rids=%s",
                        _row_rids,
                    )
                    # 打出所有联系人名+日期，及 header 的 FrameLayout 子树（未读徽章位置）
                    _names_seen = []
                    for _el in _root.iter():
                        _rid2 = (_el.get("resource-id") or "").split("/")[-1]
                        if "conversations_row_contact_name" in _rid2:
                            _names_seen.append(_el.get("text", "?"))
                    logger.debug("[wa_rpa][diag] visible_contacts=%s", _names_seen)
                    for _el in _root.iter():
                        _rid2 = (_el.get("resource-id") or "").split("/")[-1]
                        if "conversations_row_header" in _rid2:
                            _date_el = next(
                                (c for c in _el.iter()
                                 if "conversations_row_date" in (c.get("resource-id") or "")), None
                            )
                            _badge_fl = next(
                                (c for c in list(_el) if (c.get("class") or "").endswith("FrameLayout")), None
                            )
                            _badge_children = list(_badge_fl) if _badge_fl is not None else []
                            logger.debug(
                                "[wa_rpa][diag] header: date=%r badge_children=%d %s",
                                _date_el.get("text", "") if _date_el else "",
                                len(_badge_children),
                                [(c.get("text", ""), c.get("content-desc", "")) for c in _badge_children],
                            )
            except Exception:
                pass

            # ═════════════════════════════════════════════════════════════════
            # P15: 已打开对话新消息检测（核心修复）
            # ═════════════════════════════════════════════════════════════════
            # 场景：用户已读消息（badge=0），但对话仍打开且有新消息未回复
            # 检测：若 WA 在前台且当前有打开的对话，检查最后一条消息是否变化
            if self._open_thread_check_enabled and not dry_run:
                _open_result = await self._check_open_thread_for_new_messages(result, t0)
                if _open_result:
                    # 已检测到并处理了新消息，直接返回结果
                    return _open_result

            # 主动巡检：机器人不在任何对话内（停在聊天列表）时，open_thread 检测会立即
            # 返回 None。此处周期性打开最顶部（最近）会话，核对是否有「已读未回」消息。
            if self._active_sweep_enabled and not dry_run:
                _sweep_result = await self._sweep_recent_chat_for_unanswered(
                    xml_bytes, result, t0,
                )
                if _sweep_result:
                    return _sweep_result

            return self._finish(result, t0)

        # 5) 取第一个未读会话
        row = unread_rows[0]
        # P10-A: 用 account_id 代替 ADB serial，保证设备重连后 key 不变
        chat_key = f"wa:{self._account_id}:{row.name}"
        result["chat_key"] = chat_key
        result["peer_name"] = row.name

        # 点进会话
        self._tap(row.cx, row.cy)
        await asyncio.sleep(1.2)

        # 6) dump 聊天界面
        chat_xml, _ = await asyncio.to_thread(self._dump_ui_xml)
        if not chat_xml:
            result["step"] = "chat_dump_fail"
            self._back()
            return self._finish(result, t0)

        # 7) 提取最新消息（按气泡位置判断新旧：语音/媒体 > 旧文字）
        screen_w = adb.screen_size(self._serial)
        sw = screen_w[0] if screen_w else 1080
        sh = screen_w[1] if screen_w else 1920
        peer_text, reason2 = ui.pick_last_incoming_text(chat_xml, wa_pkg=self._wa_pkg, screen_width=sw)

        _voice_transcribed = False
        _media_described = False

        # 检测语音气泡位置；若语音气泡 bottom_y > 文字 bottom_y，说明语音消息更新
        # → 优先转写语音，避免回退到屏幕上更早的文字消息（如"在哪里"）被重复回复
        _last_voice = ui.detect_last_incoming_voice(chat_xml, screen_width=sw)
        if _last_voice:
            import re as _re_pos
            _m = _re_pos.search(r'bottom_y=(\d+)', reason2 or '')
            _text_bottom_y = int(_m.group(1)) if _m else 0
            if not peer_text or _last_voice.bottom_y >= _text_bottom_y:
                _voice_text = await self._try_transcribe_voice(chat_xml, sw, result)
                if _voice_text:
                    peer_text = _voice_text
                    _voice_transcribed = True
                elif not peer_text:
                    # 语音转写失败且无文字 → 占位符，让 AI 知道对方发了语音
                    peer_text = f"[对方发来一条语音消息，时长 {_last_voice.duration_text}]"
                    result["voice_transcribe_fallback"] = True
                # 若语音转写失败但有旧文字，仍用旧文字（下方 if not peer_text 兜底）

        if not peer_text:
            # 尝试检测媒体消息（图片/视频/贴纸/GIF/文件）
            peer_text = await self._try_describe_media(chat_xml, sw, result)
            if peer_text:
                _media_described = True

        if not peer_text:
            result["step"] = "no_peer_text"
            self._back()
            await asyncio.sleep(0.4)
            return self._finish(result, t0)

        result["peer_text"] = peer_text

        # 8) 去重：已回复过的消息在 dedup_window 内不重复回复
        #    用 last_reply_ts（上次实际发送时间）而非 last_peer_ts，
        #    防止轮询间隔（15s）> dedup_window（旧默认10s）导致每轮重复回复
        peer_hash = hashlib.sha256(peer_text.encode()).hexdigest()[:16]
        state = (self._state_store.get_chat_state(chat_key)
                 if self._state_store else {})
        _is_repeat = state.get("last_peer_hash") == peer_hash
        if _is_repeat:
            _last_reply_ts = float(state.get("last_reply_ts") or 0)
            _dedup_window = float(self._cfg_get("dedup_window_sec", 3600))
            if _last_reply_ts > 0 and (time.time() - _last_reply_ts) < _dedup_window:
                result["step"] = "already_replied"
                result["ok"] = True
                self._back()
                await asyncio.sleep(0.4)
                return self._finish(result, t0)
            if _last_reply_ts > 0:
                logger.info(
                    "[wa_rpa] 用户重复提问（%.0fs 前回复过同内容），放行给 AI chat=%s",
                    time.time() - _last_reply_ts, chat_key,
                )

        # 9) 回复模式
        reply_mode = str(self._cfg_get("reply_mode", "auto"))

        if reply_mode == "off":
            result["step"] = "reply_mode_off"
            result["ok"] = True
            if self._state_store:
                self._state_store.upsert_chat_state(
                    chat_key, last_peer_text=peer_text, last_peer_hash=peer_hash,
                    last_peer_ts=t0,
                )
            self._back()
            await asyncio.sleep(0.4)
            return self._finish(result, t0)

        # 10) AI 生成回复
        # 10-a) 检测用户消息语言 → 动态设置 reply_lang + 语言指令
        from src.integrations.whatsapp_rpa.lang_detect import detect_tts_lang, tts_lang_to_human
        _vo_cfg = self._cfg_get("voice_output") or {}
        _lang_default = str(_vo_cfg.get("voice_profile", {}).get("language") or "zh-cn")
        _peer_tts_lang: Optional[str] = None
        # P4-A: 运营手动锁定优先级最高，跳过一切自动检测
        _forced_lang = str(state.get("forced_lang") or "").strip()
        if _forced_lang:
            _peer_tts_lang = _forced_lang
        elif _vo_cfg.get("auto_language", True):
            _peer_stripped = peer_text.strip()
            _cached_lang = str(state.get("detected_lang") or "").strip()
            # 短消息（打招呼/yes/ok）易误判 → 复用上轮检测结果
            if len(_peer_stripped) < 8 and _cached_lang:
                _peer_tts_lang = _cached_lang
            else:
                _peer_tts_lang = detect_tts_lang(_peer_stripped, fallback=_lang_default)
                # 写入对话级缓存（只在 state_store 可用时）
                if _peer_tts_lang and _peer_tts_lang != _cached_lang and self._state_store:
                    try:
                        self._state_store.upsert_chat_state(
                            chat_key, detected_lang=_peer_tts_lang
                        )
                    except Exception:
                        pass
        lang = _peer_tts_lang or str(self._cfg_get("default_reply_lang", "zh"))
        req_id = f"warpa-{uuid.uuid4().hex[:12]}"
        _style_hint = str(self._cfg_get("reply_style_hint") or "").strip()
        if not _style_hint:
            _style_hint = _WA_DEFAULT_STYLE_HINT
        ctx: Dict[str, Any] = {
            "chat_id": chat_key,
            "request_id": req_id,
            "channel": "whatsapp_rpa",
            "platform": "whatsapp_rpa",
            "reply_lang": lang,
            "whatsapp_rpa_chat_key": chat_key,
            "whatsapp_rpa_peer_name": row.name,
            "whatsapp_rpa_style_hint": _style_hint,
            "_current_user_message_for_lang": peer_text.strip(),
            "account_persona_id": self._account_persona_id(),
        }
        # 语言指令：明确告知 AI 用对方语言回复（LLM 通常已隐式多语言，此处显式强化）
        if _peer_tts_lang:
            ctx["_reply_in_language"] = tts_lang_to_human(_peer_tts_lang)
            logger.debug("[wa_rpa] 检测到用户语言=%s (%s) chat=%s",
                         _peer_tts_lang, tts_lang_to_human(_peer_tts_lang), chat_key)
        # 注入上次回复 → 激活 SkillManager 的角度轮换/反重复系统
        _last_reply = (state.get("last_reply") or "").strip()
        if _last_reply:
            ctx["last_reply"] = _last_reply
        # 重复消息标记 → 让 AI 知道对方又问了同样的话，像真人一样回应
        # ★ 必须始终设置，否则旧的 True 值会残留在 user_context 中
        if _is_repeat and _last_reply:
            ctx["_is_repeated_message"] = True
            ctx["_prev_reply_for_repeat"] = _last_reply
        else:
            ctx["_is_repeated_message"] = False
            ctx["_prev_reply_for_repeat"] = ""

        # 语音转写标记：让 AI 知道对方发的是语音（回复风格更口语化）
        if _voice_transcribed:
            ctx["_peer_message_is_voice"] = True
            ctx["_voice_duration"] = result.get("voice_duration", "")

        # 媒体消息标记：让 AI 知道对方发的是图片/视频/贴纸等，自然回应
        if _media_described:
            ctx["_peer_message_is_media"] = True
            ctx["_media_kind"] = result.get("media_kind", "")
            ctx["_media_desc"] = result.get("media_desc", "")
            ctx["_media_vision_backend"] = result.get("media_vision_backend", "")

        # W4-Runner：ContactHooks inbound 入库 + portrait block 注入
        _journey_ctx = None
        _hooks = self._contact_hooks
        if _hooks is not None:
            try:
                _journey_ctx = _hooks.on_message(
                    channel="whatsapp",
                    account_id=self._account_id,
                    external_id=row.name or chat_key,
                    direction="in",
                    text_preview=peer_text.strip()[:120],
                    display_name=row.name or "",
                    trace_id=req_id,
                )
            except Exception:
                logger.debug("[wa_rpa] contact_hooks on_message(in) 异常", exc_info=True)
        if _journey_ctx is not None:
            try:
                _contact = getattr(_journey_ctx, "contact", None)
                _journey = getattr(_journey_ctx, "journey", None)
                if _contact is not None:
                    ctx["contact_id"] = str(getattr(_contact, "contact_id", "") or "")
                if _journey is not None:
                    _is = getattr(_journey, "intimacy_score", None)
                    if _is is not None:
                        try:
                            _is_f = float(_is)
                            ctx["intimacy_score"] = _is_f
                            # P4-A: 持久化到 chat_state，对话卡可直接读取
                            if self._state_store:
                                self._state_store.upsert_chat_state(
                                    chat_key, intimacy_score=_is_f
                                )
                        except (TypeError, ValueError):
                            pass
                    _fs = getattr(_journey, "funnel_stage", None)
                    if _fs:
                        ctx["funnel_stage"] = str(_fs)
                    snap_json = str(getattr(_journey, "context_snapshot_json", "") or "")
                    if snap_json:
                        try:
                            from src.contacts.portrait_extractor import render_block
                            _block = render_block(snap_json)
                            if _block:
                                ctx["_contact_portrait_block"] = _block
                        except Exception:
                            pass
            except Exception:
                logger.debug("[wa_rpa] portrait inject 异常", exc_info=True)
        # 9) 多条消息引用回复（ctx 已完整构建，此处是正确插入点）
        if (
            self._cfg_get("multi_msg_reply_enabled", False)
            and not _voice_transcribed
            and not _media_described
        ):
            _last_peer_text = state.get("last_peer_text", "")
            _new_msgs = ui.pick_new_incoming_messages(
                chat_xml,
                _last_peer_text,
                screen_width=sw,
                max_count=int(self._cfg_get("multi_msg_max_count", 5)),
            )
            logger.warning(
                "[wa_rpa][multi_msg] detected=%d last_anchor=%r msgs=%r chat=%s",
                len(_new_msgs),
                _last_peer_text[:30] if _last_peer_text else "",
                [m.text[:20] for m in _new_msgs],
                chat_key,
            )
            if len(_new_msgs) >= 2:
                _multi_result = await self._handle_multi_msg_flow(
                    self._serial, chat_key, _new_msgs, chat_xml,
                    state, ctx, result, t0,
                )
                if _multi_result is not None:
                    return _multi_result
                # casual/combined: 主流程用合并后的 peer_text
                peer_text = result.get("peer_text") or peer_text

        # 9.5) 引用回复目标检测 + 坐标预查
        _peer_msg_for_quote: Optional[ui.IncomingMessage] = None

        # a) 用户主动指定回复（"回我那条"/"第一条"/"那两条都回了"等）
        if not _voice_transcribed and not _media_described and not ctx.get("_multi_msg_combined"):
            _all_visible = ui.pick_all_visible_incoming(chat_xml, screen_width=sw)
            _is_quote_req, _qt_list = _detect_quote_targets(peer_text, _all_visible)

            if _is_quote_req and not _qt_list:
                # 目标不在屏幕 → 向上滚屏寻找（最多 3 次翻屏）
                logger.info("[wa_rpa] quote_target off-screen, scrolling up chat=%s", chat_key)
                _qt_list = await self._scroll_find_quote_targets(peer_text, sw, sh)

            if _is_quote_req and len(_qt_list) > 1:
                # 多条指定回复："那两条都回了" → 并行 AI + 顺序 quote reply
                _mq_result = await self._handle_multi_quote_reply(
                    self._serial, chat_key, _qt_list, state, ctx, result, t0, sw, sh,
                )
                if _mq_result is not None:
                    return _mq_result
                # 全部失败 → 降级，用第一条目标走普通 quote reply
                if _qt_list:
                    _peer_msg_for_quote = _qt_list[0]
                    peer_text = _qt_list[0].text
                    ctx["_quote_reply_requested"] = True

            elif _is_quote_req and len(_qt_list) == 1:
                # 单条指定回复
                _peer_msg_for_quote = _qt_list[0]
                peer_text = _qt_list[0].text
                ctx["_quote_reply_requested"] = True
                logger.info("[wa_rpa] quote_target → %r chat=%s", _qt_list[0].text[:40], chat_key)

        # b) 普通坐标预查（combined / 单条主流程，非用户显式指定）
        if _peer_msg_for_quote is None and not _voice_transcribed and not _media_described:
            _quote_lookup = result.get("_multi_real_last_peer") or peer_text or ""
            if _quote_lookup:
                _peer_msg_for_quote = ui.find_incoming_by_text(
                    chat_xml, _quote_lookup, screen_width=sw
                )

        logger.debug("[wa_rpa] sm_type=%s calling skill_manager peer=%r chat=%s", type(self._sm).__name__, peer_text.strip(), chat_key)
        try:
            reply_text = await self._sm.process_message(
                peer_text.strip(),
                user_id=chat_key,
                context=ctx,
            )
        except Exception as e:
            result["error"] = f"skill_error:{e}"
            result["step"] = "skill_error"
            logger.warning("WA skill_error chat=%s: %s", chat_key, e)
            self._back()
            await asyncio.sleep(0.4)
            return self._finish(result, t0)

        _p_name = (ctx.get("_resolved_persona_name") or "").strip() or ctx.get("account_persona_id", "?")
        logger.warning(
            "[wa_rpa] reply persona=%s peer=%r reply=%r chat=%s",
            _p_name, peer_text.strip()[:30], str(reply_text or "").strip()[:60], chat_key,
        )
        if not reply_text or not str(reply_text).strip():
            result["step"] = "empty_reply"
            result["ok"] = True
            self._back()
            await asyncio.sleep(0.4)
            return self._finish(result, t0)

        reply_text = str(reply_text).strip()

        # P15-j: 表情增强处理
        if self._emotion_enhancer and reply_text:
            try:
                _emo_ctx = {"suggested_emoticons": []}
                reply_text = self._emotion_enhancer.enhance_reply(
                    original_reply=reply_text,
                    emotion="neutral",
                    context_analysis=_emo_ctx,
                    message_text=peer_text.strip(),
                    chat_id=chat_key,
                )
                reply_text = reply_text.strip()
            except Exception as _ee_err:
                logger.debug("[wa_rpa] emotion enhance failed (main): %s", _ee_err)

        result["reply_text"] = reply_text

        # 11) approve 模式 → 进待审批队列
        if reply_mode == "approve":
            if self._state_store:
                self._state_store.insert_pending(
                    chat_key=chat_key,
                    peer_name=row.name,
                    peer_text=peer_text,
                    proposed_reply=reply_text,
                )
                self._state_store.upsert_chat_state(
                    chat_key, last_peer_text=peer_text, last_peer_hash=peer_hash,
                    last_peer_ts=t0,
                )
            result["step"] = "pending_queued"
            result["ok"] = True
            self._back()
            await asyncio.sleep(0.4)
            return self._finish(result, t0)

        # 11.5) TTS 语音回复准备（在文本发送之前）
        # 从 reply_text 二次检测语言（比 peer_text 更稳定，LLM 已规范输出）
        _tts_lang: Optional[str] = None
        if (self._cfg_get("voice_output") or {}).get("auto_language", True):
            _tts_lang = detect_tts_lang(
                reply_text,
                fallback=(
                    (self._cfg_get("voice_output") or {}).get("voice_profile", {}).get("language")
                    or "zh-cn"
                ),
            )
        await self._maybe_prepare_tts_reply(reply_text, result, tts_lang=_tts_lang)

        # 12) auto 模式 → 发送
        if dry_run:
            result["step"] = "dry_run_done"
            result["ok"] = True
            return self._finish(result, t0)

        # TTS auto_voice 已发送语音时，跳过文本发送（避免重复）
        if result.get("tts_send_ok"):
            result["step"] = "voice_sent"
            result["ok"] = True
            if self._state_store:
                self._state_store.upsert_chat_state(
                    chat_key,
                    last_peer_text=peer_text,
                    last_peer_hash=peer_hash,
                    last_reply=reply_text,
                    last_peer_ts=t0,
                    last_reply_ts=time.time(),
                )
            # voice sent 后需回到 WA 主页
            _go_home = (
                f"am start -n {self._wa_pkg}/{self._wa_activity} "
                f"--activity-clear-top --activity-new-task >/dev/null 2>&1"
            )
            await asyncio.to_thread(
                adb.run_adb, ["shell", _go_home],
                serial=self._serial, timeout=15.0,
            )
            await asyncio.sleep(2.5)
            return self._finish(result, t0)

        # AI 思考 15–40s 后界面可能滚动；发送前刷新 hierarchy（与 LINE redump_before_send 对齐）
        _pre_send_xml, _pre_send_sl = await asyncio.to_thread(self._dump_ui_xml)
        if _pre_send_xml:
            chat_xml = _pre_send_xml
            if _peer_msg_for_quote is not None:
                _quote_lookup = result.get("_multi_real_last_peer") or peer_text or ""
                if _quote_lookup:
                    _ref = ui.find_incoming_by_text(
                        chat_xml, _quote_lookup, screen_width=sw
                    )
                    if _ref:
                        _peer_msg_for_quote = _ref
                    else:
                        logger.warning(
                            "[wa_rpa] 发送前未刷新 quote 坐标 anchor=%r chat=%s",
                            _quote_lookup[:30], chat_key,
                        )
        else:
            logger.warning(
                "[wa_rpa] 发送前 UI dump 失败，沿用旧 hierarchy sl=%s chat=%s",
                _pre_send_sl, chat_key,
            )

        # auto_quote_reply：可选引用回复（long-press → 「回复」→ 发送）
        # combined 模式总是尝试，用户显式请求总是尝试，单条消息受 config 控制
        _do_quote = (
            _peer_msg_for_quote is not None
            and not _voice_transcribed
            and not self._quote_reply_disabled          # 连续失败超限时本 session 禁用
            and (
                ctx.get("_quote_reply_requested")      # 用户显式请求指定回复
                or ctx.get("_multi_msg_combined")      # combined 模式明确所回的消息
                or bool(self._cfg_get("auto_quote_reply", False))  # 配置开关
            )
        )
        if _do_quote:
            send_result = await self._quote_reply_msg(
                self._serial,
                _peer_msg_for_quote.cx,
                _peer_msg_for_quote.cy,
                reply_text,
                (sw, sh),
            )
            if send_result.get("ok"):
                self._quote_reply_fail_streak = 0   # 成功 → 重置计数
            else:
                self._quote_reply_fail_streak += 1
                logger.info(
                    "[wa_rpa] quote_reply 降级普通发送 chat=%s fail_streak=%d",
                    chat_key, self._quote_reply_fail_streak,
                )
                if self._quote_reply_fail_streak >= _QUOTE_REPLY_MAX_FAIL_STREAK:
                    self._quote_reply_disabled = True
                    logger.warning(
                        "[wa_rpa] quote_reply 连续失败%d次，本session已禁用 serial=%s",
                        self._quote_reply_fail_streak, self._serial,
                    )
                    if self._state_store:
                        self._state_store.insert_alert(
                            kind="quote_reply_disabled",
                            severity="warn",
                            message=(
                                f"quote_reply 连续失败{self._quote_reply_fail_streak}次已禁用"
                                f" serial={self._serial}"
                            ),
                            dedup_window_sec=3600.0,
                        )
                send_result = await self._pace_and_send(chat_xml, reply_text)
        else:
            send_result = await self._pace_and_send(chat_xml, reply_text)
        result["send"] = send_result

        if send_result.get("ok"):
            result["step"] = "sent"
            result["ok"] = True
            if self._state_store:
                # combined 模式用真实最后一条消息作锚点，避免合并文本污染下次检测
                _state_peer = result.get("_multi_real_last_peer") or peer_text
                self._state_store.upsert_chat_state(
                    chat_key,
                    last_peer_text=_state_peer,
                    last_peer_hash=peer_hash,
                    last_reply=reply_text,
                    last_peer_ts=t0,
                    last_reply_ts=time.time(),
                )
            # W4-Runner：ContactHooks outbound 入库
            if _hooks is not None:
                try:
                    _hooks.on_message(
                        channel="whatsapp",
                        account_id=self._account_id,
                        external_id=row.name or chat_key,
                        direction="out",
                        text_preview=reply_text[:120],
                        display_name=row.name or "",
                        trace_id=req_id,
                    )
                except Exception:
                    logger.debug("[wa_rpa] contact_hooks on_message(out) 异常", exc_info=True)
        else:
            result["step"] = "send_fail"
            result["error"] = str(send_result.get("error", "") or "send_unknown")
            if send_result.get("parts"):
                result["send_parts"] = send_result.get("parts")
            if self._state_store:
                self._state_store.insert_alert(
                    kind="send_fail",
                    severity="warn",
                    message=f"发送失败: chat={chat_key} err={result['error'][:80]}",
                    dedup_window_sec=120.0,
                )

        # 抓包补漏：发完后检测 AI 处理期间到达的新消息（最多 N 轮）
        # 背景：bot 打开聊天 → AI 思考 15-25s → 用户发新消息 → WhatsApp 自动已读
        # → bot 回主页后无 unread badge → 消息永久漏处理
        # 解法：发完回复后原地再 dump，有新消息则继续处理，直到清空或达上限
        if send_result.get("ok"):
            _catchup_last_hash = peer_hash
            _catchup_max = int(self._cfg_get("catchup_limit", 3))
            for _cu_round in range(1, _catchup_max + 1):
                await asyncio.sleep(1.2)  # 从 2.0 压缩到 1.2s，加快漏读检测
                _cu_xml, _ = await asyncio.to_thread(self._dump_ui_xml)
                if not _cu_xml:
                    break
                _cu_text, _ = ui.pick_last_incoming_text(
                    _cu_xml, wa_pkg=self._wa_pkg, screen_width=sw
                )
                # catchup: 语音 fallback（用户在 AI 思考期间发了语音）
                if not _cu_text:
                    _cu_result: Dict[str, Any] = {}
                    _cu_text = await self._try_transcribe_voice(_cu_xml, sw, _cu_result)
                    if _cu_text:
                        logger.info("[wa_rpa] catchup voice round=%d text=%r", _cu_round, _cu_text[:40])
                if not _cu_text:
                    break
                _cu_hash = hashlib.sha256(_cu_text.encode()).hexdigest()[:16]
                if _cu_hash == _catchup_last_hash:
                    break  # 无新消息
                logger.info(
                    "[wa_rpa] catchup round=%d new_msg chat=%s peer=%r",
                    _cu_round, chat_key, _cu_text[:40],
                )
                # catchup 也支持指定回复检测（单条；多条降级用第一条）
                _cu_rid = f"warpa-{uuid.uuid4().hex[:12]}"
                _cu_ctx = dict(ctx)
                _cu_ctx["request_id"] = _cu_rid
                _cu_ai_text = _cu_text
                _cu_quote_msg: Optional[ui.IncomingMessage] = None
                _cu_all_vis = ui.pick_all_visible_incoming(_cu_xml, screen_width=sw)
                _cu_is_req, _cu_qt_list = _detect_quote_targets(_cu_text, _cu_all_vis)
                if _cu_is_req and _cu_qt_list:
                    _cu_qt = _cu_qt_list[0]   # catchup 降级只处理第一条目标
                    _cu_ai_text = _cu_qt.text
                    _cu_quote_msg = _cu_qt
                    _cu_ctx["_quote_reply_requested"] = True
                    logger.info("[wa_rpa] catchup quote_target round=%d target=%r",
                                _cu_round, _cu_qt.text[:30])
                try:
                    _cu_reply = await self._sm.process_message(
                        _cu_ai_text.strip(), user_id=chat_key, context=_cu_ctx,
                    )
                except Exception as _cu_err:
                    logger.warning("[wa_rpa] catchup skill_error round=%d: %s", _cu_round, _cu_err)
                    break
                if not _cu_reply or not str(_cu_reply).strip():
                    _catchup_last_hash = _cu_hash  # 标为已见，避免重复
                    continue
                _cu_reply = str(_cu_reply).strip()
                if _cu_quote_msg is not None:
                    _cu_send = await self._quote_reply_msg(
                        self._serial, _cu_quote_msg.cx, _cu_quote_msg.cy, _cu_reply, (sw, sh)
                    )
                    if not _cu_send.get("ok"):
                        _cu_send = await self._pace_and_send(_cu_xml, _cu_reply)
                else:
                    _cu_send = await self._pace_and_send(_cu_xml, _cu_reply)
                if not _cu_send.get("ok"):
                    logger.warning("[wa_rpa] catchup send_fail round=%d", _cu_round)
                    break
                _catchup_last_hash = _cu_hash
                if self._state_store:
                    self._state_store.upsert_chat_state(
                        chat_key,
                        last_peer_text=_cu_text,
                        last_peer_hash=_cu_hash,
                        last_reply=_cu_reply,
                        last_peer_ts=t0,
                        last_reply_ts=time.time(),
                    )
                if _hooks is not None:
                    try:
                        _hooks.on_message(
                            channel="whatsapp",
                            account_id=self._account_id,
                            external_id=row.name or chat_key,
                            direction="out",
                            text_preview=_cu_reply[:120],
                            display_name=row.name or "",
                            trace_id=_cu_rid,
                        )
                    except Exception:
                        logger.debug("[wa_rpa] catchup contact_hooks 异常", exc_info=True)
                logger.warning(
                    "[wa_rpa] catchup step=sent round=%d chat=%s peer_len=%d",
                    _cu_round, chat_key, len(_cu_text),
                )

        # 发完后强制跳回聊天列表（am start 比 KEYCODE_BACK 更可靠）
        # 防止 WA 停在聊天界面 → 后续新消息被自动已读 → runner 永久漏检
        _go_home = (
            f"am start -n {self._wa_pkg}/{self._wa_activity} "
            f"--activity-clear-top --activity-new-task >/dev/null 2>&1; echo OK"
        )
        await asyncio.to_thread(
            adb.run_adb, ["shell", _go_home],
            serial=self._serial, timeout=15.0,
        )
        await asyncio.sleep(2.5)
        return self._finish(result, t0)

    async def _async_go_home(self) -> None:
        """按 HOME 键把 WhatsApp 退到后台，确保新消息产生通知。"""
        if getattr(self, '_wa_launched', False) and self._serial:
            try:
                await asyncio.to_thread(
                    adb.run_adb,
                    ["shell", "input keyevent KEYCODE_HOME"],
                    serial=self._serial, timeout=5.0,
                )
            except Exception:
                pass
            self._wa_launched = False

    def _finish(self, result: Dict[str, Any], t0: float) -> Dict[str, Any]:
        result["total_ms"] = int((time.time() - t0) * 1000)
        # 写入运行记录
        if self._state_store:
            try:
                self._state_store.insert_run(
                    chat_key=result.get("chat_key", ""),
                    ok=int(bool(result.get("ok"))),
                    step=result.get("step", ""),
                    peer_text=result.get("peer_text", "")[:400],
                    reply_text=result.get("reply_text", "")[:400],
                    total_ms=result["total_ms"],
                    error=str(result.get("error", ""))[:200],
                )
            except Exception:
                pass
        logger.warning(
            "[wa_rpa] step=%s ok=%s chat=%s peer_len=%d took=%dms err=%s",
            result.get("step"), result.get("ok"),
            result.get("chat_key", "")[:40],
            len(result.get("peer_text") or ""),
            result["total_ms"],
            result.get("error", "-"),
        )
        return result

    # ── 自动接受联系人申请 ────────────────────────────────────────────────

    async def maybe_auto_accept_contacts(
        self, max_accept: int = 5
    ) -> Dict[str, Any]:
        """在当前屏幕 XML 查找接受按钮并点击（不主动导航）。"""
        aa_cfg = self._cfg_get("auto_accept") or {}
        if not isinstance(aa_cfg, dict) or not aa_cfg.get("enabled"):
            return {"skipped": True, "reason": "disabled"}
        serial = self._serial
        if not serial:
            return {"skipped": True, "reason": "no_serial"}

        out: Dict[str, Any] = {"tapped": 0, "coords": []}
        try:
            xml_bytes, _ = await asyncio.to_thread(self._dump_ui_xml)
            if not xml_bytes:
                return {"skipped": True, "reason": "no_xml"}
            coords = ui.find_accept_button_coords(xml_bytes)
            if not coords:
                return {"skipped": True, "reason": "no_accept_buttons"}
            for cx, cy in coords[:max_accept]:
                adb.input_tap(serial, cx, cy)
                await asyncio.sleep(0.6)
                out["tapped"] += 1
                out["coords"].append([cx, cy])
                logger.info("wa auto_accept_contacts: tapped (%d,%d)", cx, cy)
            if out["tapped"] and self._state_store:
                self._state_store.insert_alert(
                    kind="wa_contact_accepted",
                    severity="info",
                    message=f"WhatsApp 自动接受联系人申请 {out['tapped']} 个",
                    detail={"coords": out["coords"]},
                    dedup_window_sec=300,
                )
        except Exception as e:
            out["error"] = str(e)
            logger.debug("wa maybe_auto_accept_contacts 失败: %s", e, exc_info=True)
        return out
