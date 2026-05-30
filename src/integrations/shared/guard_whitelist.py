"""共享 guard 白名单 — 跨 RPA 平台统一过滤 Vision 误报。

设计目标：Messenger / LINE / WhatsApp / Telegram RPA 在收件箱扫描后
若 Vision 返回 guard.needs_human=True，先经此函数过滤明显误报，避免
不必要的 human-handoff 中断。

S1-P0A: 从 messenger_rpa.runner._guard_is_inbox_false_positive 提取。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional


# 已知"真实 modal"必含的动词 / 关键词（中英日）
REAL_MODAL_KEYWORDS = frozenset({
    "account", "accounts", "profile", "profiles",
    "switch", "choose", "select", "log in", "login",
    "sign in", "continue as",
    # 中文
    "帐号", "账号", "切换", "选择", "登录", "登陆",
    # 日文
    "アカウント", "ログイン", "切り替え",
})

# 收件箱 inbox 误报常见 title 关键字（小写）
INBOX_FALSE_POSITIVE_PHRASES = (
    "ask meta ai", "meta ai",
    "facebook messenger", "messenger",
    "stories banner", "stories", "story",
    "line", "whatsapp",
    # Messenger inline warning banner（非 modal）：Vision 会把它误报为 profile_picker
    "has been restricted", "account has been restricted",
    "account is restricted", "account temporarily restricted",
)

# 通知悬浮文案
NOTIFICATION_PHRASES_LOWER = ("sent a ", "sent you", "sent a photo", "sent you a")
NOTIFICATION_PHRASES_RAW = ("发送了", "送信", "送りました")

# 还原聊天记录 / 恢复对话 modal —— 应该自动按"否/取消"，不阻塞
RESTORE_CHAT_KEYWORDS = (
    "restore", "restore chat", "restore conversations",
    "还原", "还原聊天记录", "恢复",
    "履歴", "復元", "復元する",
)


def is_inbox_false_positive(guard: Any) -> bool:
    """检测 inbox 阶段的 Vision guard 误报。

    返回 True 表示该 guard 是误报，调用方应当**忽略**它继续正常流程。

    已知误报场景：
    1. profile_picker + title 含 "Ask Meta AI" / "Meta AI" / "Messenger" / "WhatsApp" / "LINE"
       —— 各 APP 收件箱顶部品牌名占位
    2. profile_picker + title == "Stories banner" / "Stories" —— 收件箱顶部 Stories 栏
    3. profile_picker + title 含发消息通知文字（"X 发送了Y消息"）—— Android 通知悬浮
    4. profile_picker + 空 title —— Vision 误报（真 modal 必有 title）
    5. profile_picker + title > 60 字符 —— Vision prompt 回馈泄漏
    6. profile_picker + title 含 "row_index" —— 同上
    7. profile_picker + title 不含任何"真实 modal 关键词"（account/login/switch...）—— 误报联系人名字
    """
    if not getattr(guard, "needs_human", False):
        return False
    title = (getattr(guard, "title", "") or "").strip()
    # 真实 modal 必有 title
    if not title:
        return True
    tl = title.lower()
    # 1. 各 APP 品牌 / 内置占位
    if any(p in tl for p in INBOX_FALSE_POSITIVE_PHRASES):
        # 但要排除 "Choose Facebook account" 这种含 facebook 但又有真关键词的
        if any(kw in tl for kw in REAL_MODAL_KEYWORDS):
            return False  # 含真关键词 → 真 modal
        return True
    # 2. Stories 前缀（含 Vision 把 "Stories 行下方..." prompt 泄漏）
    if tl.startswith("stories"):
        return True
    # 3. Vision prompt 回馈泄漏
    if "row_index" in tl or "row_index" in title:
        return True
    # 4. 超长 title 多为 Vision 幻觉
    if len(title) > 60:
        return True
    # 5. 通知文案
    if any(p in title for p in NOTIFICATION_PHRASES_RAW):
        return True
    if any(p in tl for p in NOTIFICATION_PHRASES_LOWER):
        return True
    # 6. 正向判断：真实 profile_picker 必含动词关键词；不含 → 极可能是
    #    Vision 把联系人名字 / inbox 行误读为 modal
    if not any(kw in tl for kw in REAL_MODAL_KEYWORDS):
        return True
    return False


def is_restore_chat_modal(guard: Any) -> bool:
    """检测"还原聊天记录"modal —— 应自动 dismiss，不阻塞。

    返回 True 表示这是恢复聊天的提示弹窗（陌生人会话首次出现），
    调用方应自动按"否/取消/Don't restore"。
    """
    if not getattr(guard, "needs_human", False) and not getattr(guard, "type", None):
        return False
    title = (getattr(guard, "title", "") or "").strip().lower()
    if not title:
        return False
    return any(kw in title for kw in RESTORE_CHAT_KEYWORDS)


def classify_guard(guard: Any) -> str:
    """对 guard 做一次性分类，返回处理建议：

    - "false_positive"：误报，调用方应继续正常流程
    - "restore_chat"：还原聊天提示，调用方应自动 dismiss
    - "real"：真实 modal，调用方应按原处理流程
    """
    # restore_chat 优先于 false_positive 检查——它的 title 不含 modal 关键词
    # 但不应被宽泛的 false_positive 规则吞掉；需特殊处理（自动 BACK dismiss）
    if is_restore_chat_modal(guard):
        return "restore_chat"
    if is_inbox_false_positive(guard):
        return "false_positive"
    return "real"
