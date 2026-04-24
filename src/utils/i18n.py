"""轻量 i18n — 按 chat/user 存储语言偏好，提供翻译字典"""

import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger("i18n")

_MESSAGES = {
    "zh": {
        "gxp_timeout": "查询超时（{sec}s 未收到回复），请稍后重试。\n命令: {cmd}",
        "gxp_result_fail": "查询结果：{text}",
        "gxp_result_ok": "查询结果：\n{text}",
        "gxp_result_other": "GXP 回复：\n{text}",
        "reload_notify": "[系统] 配置已热重载 ({ts})，新配置已生效。",
        "no_permission": "无权限执行此操作。",
        "batch_preview_title": "📋 批量修改预览 — 所有通道费率\n",
        "batch_confirm_hint": "请回复「确认批量修改」执行，或忽略取消。",
        "batch_done": "已批量更新 {n} 个通道费率",
        "lang_switched": "语言已切换为：中文",
        "lang_current": "当前语言：中文\n发送「切换英文」或「switch to english」切换",
        "order_ask_intent": "收到订单号 {order}，请问您需要：\n1. 查代收订单\n2. 查提现订单\n3. 回调交易订单\n4. 回调提现订单\n请回复数字 1-4",
        "help_title": "配置管理命令",
    },
    "en": {
        "gxp_timeout": "Query timed out ({sec}s no response). Please try again.\nCommand: {cmd}",
        "gxp_result_fail": "Query result: {text}",
        "gxp_result_ok": "Query result:\n{text}",
        "gxp_result_other": "GXP reply:\n{text}",
        "reload_notify": "[System] Config hot-reloaded ({ts}), changes are live.",
        "no_permission": "No permission to execute this operation.",
        "batch_preview_title": "📋 Batch change preview — All channel rates\n",
        "batch_confirm_hint": "Reply 'confirm batch' to execute, or ignore to cancel.",
        "batch_done": "Batch updated {n} channel rates",
        "lang_switched": "Language switched to: English",
        "lang_current": "Current language: English\nSend '切换中文' or 'switch to chinese' to change",
        "order_ask_intent": "Order #{order} received. What do you need?\n1. Check deposit order\n2. Check withdrawal order\n3. Callback deposit\n4. Callback withdrawal\nReply 1-4",
        "help_title": "Config management commands",
    },
}

_DEFAULT_LANG = "zh"


class I18n:

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._cache: dict = {}
        if db_path:
            self._init_db()

    def _init_db(self):
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS lang_prefs (
                chat_id INTEGER PRIMARY KEY,
                lang TEXT NOT NULL DEFAULT 'zh'
            )
        """)
        self._conn.commit()

    def get_lang(self, chat_id: int) -> str:
        if chat_id in self._cache:
            return self._cache[chat_id]
        if self._conn:
            row = self._conn.execute(
                "SELECT lang FROM lang_prefs WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if row:
                self._cache[chat_id] = row[0]
                return row[0]
        return _DEFAULT_LANG

    def set_lang(self, chat_id: int, lang: str) -> str:
        lang = lang.lower().strip()
        if lang not in _MESSAGES:
            lang = _DEFAULT_LANG
        self._cache[chat_id] = lang
        if self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO lang_prefs (chat_id, lang) VALUES (?, ?)",
                (chat_id, lang),
            )
            self._conn.commit()
        return lang

    def t(self, key: str, chat_id: int = 0, **kwargs) -> str:
        lang = self.get_lang(chat_id) if chat_id else _DEFAULT_LANG
        msg = _MESSAGES.get(lang, _MESSAGES[_DEFAULT_LANG]).get(key)
        if msg is None:
            msg = _MESSAGES[_DEFAULT_LANG].get(key, key)
        if kwargs:
            try:
                return msg.format(**kwargs)
            except (KeyError, IndexError):
                return msg
        return msg

    def merge_domain_keys(self, domain_i18n: dict):
        """Merge domain-specific i18n keys into the message dictionaries.
        domain_i18n: {lang: {key: value, ...}, ...}
        """
        for lang, keys in domain_i18n.items():
            if lang not in _MESSAGES:
                _MESSAGES[lang] = {}
            _MESSAGES[lang].update(keys)
        logger.info("Merged domain i18n keys for languages: %s", list(domain_i18n.keys()))

    @staticmethod
    def available_langs():
        return list(_MESSAGES.keys())
