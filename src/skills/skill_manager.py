"""
# Skill管理�?管理和执行Skill工作流，复用Camille的意图识���回�生成逻辑
"""

import asyncio
import json
import os
import time
import random
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Any, Optional, List, Callable, Tuple
import re
import hashlib
import copy
import uuid


def _safe_int_chat_id(v: Any) -> int:
    """Convert any chat_id to int safely.
    Telegram 使用数字 ID；WA/LINE/Messenger 使用字符串 chat_key。
    非数字值通过 MD5 派生稳定 32 bit int，保持每个账号唯一性。
    """
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(hashlib.md5(str(v).encode()).hexdigest()[:8], 16)


from src.utils.audit_store import AuditStore
from src.utils.domain_policy import effective_domain_name, payment_plugin_enabled
from src.utils.channel_status_format import (
    DISABLED_STATUSES as _CHANNEL_DISABLED_STATUSES,
    format_live_channel_status_text,
    is_channel_disabled,
)
from src.utils.greeting_lexicon import is_greeting_message, merge_greeting_substrings
from src.utils.logger import LoggerMixin
from src.ai.ai_client import AIClient
from src.skills.base import Skill

# 通道成功率多轮：短追问（? / 正常吗 / 波动）不应再整段复述 JC/EP
_CHANNEL_FAMILY_FOR_FOLLOWUP = frozenset({"channel_info", "status_check"})


def _last_reply_looks_like_channel_summary(reply: str) -> bool:
    r = (reply or "").strip()
    if len(r) < 18:
        return False
    has_metric = "%" in r or "成功率" in r
    rl = r.lower()
    has_ch = any(
        x in r
        for x in ("JC", "EP", "通道", "Jazz", "Easypaisa", "Pay")
    ) or "jazzcash" in rl or "easypaisa" in rl
    return bool(has_metric and has_ch)


# 仅含通道代号（拉丁字母）时，语言检测易误判为英文；用于继承上句/会话语言
_CHANNEL_AMBIGUOUS_TOKENS = frozenset({
    "ep", "jc", "jp", "jazz", "easypaisa", "easypay", "jazzcash",
})


def _is_ambiguous_channel_token_message(s: str) -> bool:
    """整条消息只有通道名缩写/别名（可多个、可带标点），无自然语言内容。"""
    t = (s or "").strip()
    if not t or len(t) > 48:
        return False
    t = re.sub(r"[?？!！.。,，、]+$", "", t)
    parts = re.split(r"[\s,，/&+]+", t)
    parts = [p for p in parts if p]
    if not parts:
        return False
    for p in parts:
        if p.lower().rstrip("?？") not in _CHANNEL_AMBIGUOUS_TOKENS:
            return False
    return True


# 纯语气词/填充音：不算「通道短追问」，避免「啊」「嗯」继承 channel_info 后整段复述
_INTERJECTION_ONLY_CHARS = frozenset(
    "啊嗯哦噢哈唉额诶哎呀吧呢嘛哼啧哟喽咯哇哒咯呐咯"
)


def _is_meaningless_interjection_only(text: str) -> bool:
    """仅语气词、标点装饰、无业务字；有疑问号/数字/字母则不视为无意义。"""
    t = (text or "").strip()
    if not t:
        return True
    if "?" in t or "？" in t:
        return False
    if any(ch.isdigit() for ch in t):
        return False
    if re.search(r"[a-zA-Z]", t):
        return False
    core = re.sub(r"[\s—－\-~～·…。，、!！]+", "", t)
    if not core:
        return True
    if len(core) > 8:
        return False
    for ch in core:
        if ch not in _INTERJECTION_ONLY_CHARS:
            return False
    return True


_EXPLICIT_QUERY_KW = ("成功率", "额度", "限额", "费率", "手续费", "代收", "代付")


def _is_channel_short_followup(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _is_meaningless_interjection_only(t):
        return False
    if any(k in t for k in _EXPLICIT_QUERY_KW):
        return False
    if len(t) <= 10:
        return True
    tl = t.lower()
    if len(t) <= 22:
        short_kw = (
            "正常吗", "波动", "稳定吗", "能跑吗", "还行吗", "可以吗",
            "稳吗", "行吗", "好吗", "怎么样", "如何", "大吗", "厉害吗",
            "normal", "stable", "working", "ok?", "fine?", "good?",
            "available", "active", "running", "issue", "problem",
        )
        if any(k in tl for k in short_kw):
            return True
    if len(t) <= 14 and any(k in tl for k in ("波动", "通道", "正常", "channel", "status")):
        return True
    return False

# �€�€ 查�向量 LRU 缓存 �€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€
# key = 归一化查询文����?200 字�），value = embedding vector
# 使用 OrderedDict 实现 O(1) LRU 逻辑（Python 3.7+ 保序�?
_EMBED_CACHE: "OrderedDict[str, List[float]]" = OrderedDict()
_EMBED_CACHE_MAX = 500  # �€多缓�?500 条查询向量（�?3MB�?
_BM25_STRONG_THRESHOLD = 0.30  # BM25 分数 >= 此�€��为强命中，跳过向量化

# �€�€ Embedding API 用量统�（模块级，session 内累计）�€�€�€�€�€�€�€�€
_EMBED_STATS: dict = {
    # "api_calls":    0,   # 实际调用 API 次数 "cache_hits":   0,   # 命中缓存次数 "kb_queries":   0,   # �?KB 查�次数 "kb_hits":      0,   # KB 命中次数
    "session_start": time.time(),
}

# episodic backfill 日预算（UTC 日期；按本次参与嵌入的行数累加）
_EPISODIC_BACKFILL_BUDGET_DAY: Optional[str] = None
_EPISODIC_BACKFILL_BUDGET_USED: int = 0


def _episodic_backfill_charge_budget(n: int, mvec: Dict[str, Any]) -> None:
    """补全任务在调用 embed 后按行数计入日预算（需与 daily_embed_budget 配置一致）。"""
    global _EPISODIC_BACKFILL_BUDGET_DAY, _EPISODIC_BACKFILL_BUDGET_USED
    if n <= 0:
        return
    bud = (mvec.get("daily_embed_budget") or {})
    if not bud.get("enabled", False):
        return
    day = time.strftime("%Y-%m-%d", time.gmtime())
    if _EPISODIC_BACKFILL_BUDGET_DAY != day:
        _EPISODIC_BACKFILL_BUDGET_DAY = day
        _EPISODIC_BACKFILL_BUDGET_USED = 0
    _EPISODIC_BACKFILL_BUDGET_USED += n


# 陪伴/闲聊类意图族（与 P0-G 的 _INTENT_FAMILIES["chat"] 一致）——陪伴记忆抽取的默认目标
CHAT_FAMILY_INTENTS = frozenset({
    "greeting", "small_talk", "direct_chat", "casual_chat", "chitchat", "free_chat",
})


def resolve_salience_rerank_cfg(memory_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析「情绪显著性重排」配置（R2/REMT-lite），容忍两种键名。

    历史/预设里既出现过 ``memory.salience_rerank`` 也出现过简写 ``memory.salience``
    （companion 预设曾误用后者，导致这条护城河特性被静默关掉——配置在、代码却读不到）。
    统一在此解析：优先 ``salience_rerank``，回退 ``salience``，让两种拼写都生效，
    并作为预设契约测试的单一事实源（防再次漂移）。
    """
    mcfg = memory_cfg or {}
    return dict(mcfg.get("salience_rerank") or mcfg.get("salience") or {})


def should_extract_intent(intent: str, ex_cfg: Dict[str, Any]) -> bool:
    """记忆抽取意图闸（Phase D：可单测的纯函数，替代内联 ``intent not in intents``）。

    语义（**保持存量部署零回归**）：
    - ``extract.match_all: true`` → 任何意图都抽（陪伴产品「全记」开关，需显式开）。
    - 否则按 ``extract.intents`` 白名单；未配置/空 → 不抽（与历史行为一致，不给存量加 token 成本）。
    """
    if (ex_cfg or {}).get("match_all"):
        return True
    intents = (ex_cfg or {}).get("intents")
    if not intents:
        return False
    return intent in set(intents)


class SkillManager(LoggerMixin):
    """Skill管理�?"""
    
    def __init__(self, config, ai_client: AIClient):
        """
        # 初�化Skill管理�?
        Args:
            # config: 配置管理器实�?            ai_client: AI客户����?        """
        self.config = config
        self.ai_client = ai_client
        self.skills: Dict[str, 'Skill'] = {}
        self.reply_cache: Dict[str, float] = {}  # 内�哈希 -> �€后发送时�?
        self.global_last_reply_time = 0

        from src.utils.context_store import ContextStore
        cfg_dir = Path(config.config_path).parent if hasattr(config, "config_path") else Path("config")
        ttl_days = int(config.config.get("context_store", {}).get("ttl_days", 30)
                       if hasattr(config, "config") else 30)
        self._context_store = ContextStore(db_path=cfg_dir / "bot.db", ttl_days=ttl_days)
        
        # 从配���取��?        skills
        skills_config = config.get_skills_config()
        intent_config = config.get_intent_config()
        templates_config = config.get_templates_config()
        
        # 冷却时间设置
        _cd = skills_config.get('cooldown', {}) or {}
        self.cooldown_per_user = _cd.get('per_user', 60)
        self.cooldown_per_content = _cd.get('per_content', 120)
        self.cooldown_global = _cd.get('global', 0)
        self.cooldown_per_chat_user = _cd.get('per_chat_user', 1)
        self._chat_user_last_reply: Dict[str, float] = {}
        self._user_locks: Dict[str, asyncio.Lock] = {}
        # 按意图最小间隔（秒）；per_user �?0 时仍�闲聊等意图单����?
        self.cooldown_by_intent = _cd.get('by_intent') or {}
        if not isinstance(self.cooldown_by_intent, dict):
            self.cooldown_by_intent = {}
        # 收窄回复：仅允许部分意图（见 config narrow_reply）
        self._narrow_reply_cfg: Dict[str, Any] = {}
        if hasattr(config, "config") and isinstance(getattr(config, "config", None), dict):
            self._narrow_reply_cfg = dict((config.config or {}).get("narrow_reply") or {})
        
        # 意图识别配置
        self.intent_keywords = intent_config.get('keywords', {})
        self.intent_patterns = intent_config.get('patterns', {})
        
        # 回�模板
        self.templates = templates_config
        
        # 回�策略系统（从��� YAML 文件加载，支�?mtime ���新）
        self._load_strategies_from_config()

        # 策略 A/B 效果追踪
        from src.utils.strategy_tracker import StrategyTracker
        self._strategy_tracker = StrategyTracker(db_path=cfg_dir / "strategy_events.db")

        # Auto-Pilot 控制
        self._autopilot_msg_counter = 0
        self._autopilot_check_interval = 100  # �?100 条消�查一�?
        self._autopilot_last_check = 0.0

        # J1: 人工升级冷却 {user
        # FIXME: _id: last_escalation_ts}
        self._escalation_cooldown: Dict[str, float] = {}
        # R8: 危机人工接管冷却 {user_id: last_crisis_escalation_ts}
        self._crisis_escalation_cooldown: Dict[str, float] = {}

        # 人设一致性守卫：默认开（仅当人设声明了 forbidden_phrases / deny_ai 才实际生效，
        # 故对无禁用项的默认人设是零影响）。可经 companion.persona_guard.enabled 关闭。
        self._persona_guard_enabled: bool = True
        try:
            if hasattr(config, "config") and isinstance(getattr(config, "config", None), dict):
                _pg = ((config.config or {}).get("companion") or {}).get("persona_guard") or {}
                self._persona_guard_enabled = bool(_pg.get("enabled", True))
        except Exception:
            self._persona_guard_enabled = True

        self._memory_cfg: Dict[str, Any] = {}
        self._episodic_store = None
        self._memory_llm_last: Dict[str, float] = {}
        if hasattr(config, "config") and isinstance(getattr(config, "config", None), dict):
            self._memory_cfg = dict((config.config or {}).get("memory") or {})
        if self._memory_cfg.get("enabled", True):
            _mdb = self._memory_cfg.get("db_path")
            if _mdb:
                _epath = Path(str(_mdb))
            else:
                _epath = cfg_dir / "bot.db"
            try:
                from src.utils.episodic_memory_store import EpisodicMemoryStore
                self._episodic_store = EpisodicMemoryStore(_epath)
                self.logger.info("情景记忆已启用: %s", _epath)
            except Exception as _mem_err:
                self.logger.warning("情景记忆初始化失败（将禁用）: %s", _mem_err)
                self._episodic_store = None

        self._cpi = None  # S5: CrossPlatformIdentity
        try:
            from src.utils.cross_platform_identity import CrossPlatformIdentity
            self._cpi = CrossPlatformIdentity(_epath)
        except Exception as _cpi_err:
            self.logger.warning("CrossPlatformIdentity 初始化失败: %s", _cpi_err)

        # R9: 危机事件审计库（默认随 wellbeing.crisis_audit 开；落 bot.db 同库独立表）
        self._crisis_store = None
        try:
            from src.utils.crisis_event_store import CrisisEventStore
            self._crisis_store = CrisisEventStore(cfg_dir / "bot.db")
        except Exception as _ce_err:
            self.logger.warning("危机事件库初始化失败（将禁用审计）: %s", _ce_err)
            self._crisis_store = None

        self._ai_fallback_replies = (
            self.config.config.get('reply', {}).get('ai_fallback_replies', [])
            if hasattr(self.config, 'config') else []
        ) or [
            # "在的，请您稍等一下～", "收到，马上为您处理。", "好的亲，稍等我看一下～",
        ]

        self.logger.info("Skill管理器初始化")

    def _get_kb_store(self):
        """获取 KB 实例（与 Skill 共用 kb_registry 单例 + 种子迁移）。"""
        try:
            from src.utils.kb_registry import get_kb_store
            return get_kb_store(self.config, require_exists=False)
        except Exception as e:
            self.logger.warning("KB 话术加载失败: %s", e)
            return None

    def _kb_store_if_exists(self):
        """仅当 knowledge_base.db 已存在时返回共享实例（不仅为副作用新建空库）。"""
        try:
            from src.utils.kb_registry import get_kb_store
            return get_kb_store(self.config, require_exists=True)
        except Exception as e:
            self.logger.debug("KB 侧载失败: %s", e)
            return None

    def _get_ai_fallback_reply(self) -> str:
        kb = self._get_kb_store()
        if kb:
            reply = kb.get_fallback("global")
            if reply:
                return reply
        return random.choice(self._ai_fallback_replies)
    
    def _load_strategies_from_config(self) -> None:
        """�?config_manager 加载策略配置（独�?YAML 文件，自动迁�?+ mtime ���新）"""
        if hasattr(self.config, 'get_strategies_config'):
            rs = self.config.get_strategies_config()
        elif hasattr(self.config, 'config'):
            rs = self.config.config.get('reply_strategies', {})
        else:
            rs = {}
        rs = rs or {}
        self._strategies = rs.get('strategies', {}) or {}
        self._intent_strategy_map = rs.get('intent_strategy_map', {}) or {}
        self._ab_tests = rs.get('ab_tests', {}) or {}

    def _refresh_strategies(self) -> None:
        """�?reply_strategies.yaml 文件变化则重新加载（���新）"""
        if hasattr(self.config, 'get_strategies_config'):
            rs = self.config.get_strategies_config() or {}
            self._strategies = rs.get('strategies', {}) or {}
            self._intent_strategy_map = rs.get('intent_strategy_map', {}) or {}
            self._ab_tests = rs.get('ab_tests', {}) or {}

    def get_strategy_for_intent(self, intent: str, user_id: str = "") -> tuple:
        """根据意图获取回�策略，支�?A/B 灰度分流�?
        Returns:
            (strategy_dict, strategy_id)
        """
        # �€查是否有活跃�?A/B 测试
        ab = self._ab_tests.get(intent)
        if ab and ab.get("enabled") and ab.get("variants") and user_id:
            resolved = self._resolve_ab_variant(ab, user_id, intent)
            if resolved:
                return resolved

        strategy_id = (self._intent_strategy_map or {}).get(intent, 'S3_standard')
        strategies = self._strategies or {}
        strategy = strategies.get(strategy_id, {}) or {}
        if not strategy.get('enabled', True):
            strategy_id = 'S3_standard'
            strategy = strategies.get('S3_standard', {}) or {}
        return strategy, strategy_id

    def _resolve_ab_variant(self, ab: Dict, user_id: str, intent: str):
        """用一致�€�哈希从 A/B 测试变体�€�择策略"""
        variants = ab.get("variants", [])
        if not variants:
            return None
        bucket = int(hashlib.md5(f"{intent}:{user_id}".encode()).hexdigest(), 16) % 100
        cumulative = 0
        for v in variants:
            cumulative += v.get("weight", 0)
            if bucket < cumulative:
                sid = v.get("strategy_id", "")
                strat = (self._strategies or {}).get(sid)
                if strat and strat.get("enabled", True):
                    return strat, sid
        fallback_id = (self._intent_strategy_map or {}).get(intent, 'S3_standard')
        return (self._strategies or {}).get(fallback_id, {}) or {}, fallback_id

    @property
    def strategy_tracker(self):
        """暴露 StrategyTracker 实例给�层（Web ���盘等�?"""
        return self._strategy_tracker

    def get_all_strategies(self) -> Dict[str, Any]:
        """返回�€有策略（�?Web API 使用�?"""
        return self._strategies

    def get_intent_strategy_map(self) -> Dict[str, str]:
        """返回意图-策略映射（供 Web API 使用�?"""
        return self._intent_strategy_map

    def update_strategy(self, strategy_id: str, updates: Dict[str, Any]) -> bool:
        """���新单���略参数并持久化到 YAML 文件"""
        if strategy_id not in self._strategies:
            return False
        self._strategies[strategy_id].update(updates)
        return self._persist_strategies()

    def update_intent_mapping(self, intent: str, strategy_id: str) -> bool:
        """���新意�?策略映射并持久化�?YAML 文件"""
        if strategy_id not in self._strategies:
            return False
        self._intent_strategy_map[intent] = strategy_id
        return self._persist_strategies()

    def _persist_strategies(self) -> bool:
        """将当前内存中的策略写�?reply_strategies.yaml，保�?autopilot/data_retention 等附属配�?"""
        existing = {}
        if hasattr(self.config, 'get_strategies_config'):
            existing = self.config.get_strategies_config()
        data = {
            "strategies": copy.deepcopy(self._strategies),
            "intent_strategy_map": dict(self._intent_strategy_map),
            "ab_tests": copy.deepcopy(self._ab_tests) if self._ab_tests else {},
        }
        for preserve_key in ("autopilot", "data_retention"):
            if preserve_key in existing:
                data[preserve_key] = existing[preserve_key]
        if hasattr(self.config, 'save_strategies'):
            ok, msg = self.config.save_strategies(data)
            if not ok:
                self.logger.warning("策略持久化失�? %s", msg)
            return ok
        return False

    async def initialize(self) -> bool:
        """初始化Skill管理器"""
        try:
            self._register_skills()
            self.logger.info(f"已注册 {len(self.skills)} 个技能")
            return True
        except Exception as e:
            self.logger.error(f"初始化Skill管理器失败: {e}")
            return False

    def _register_skills(self):
        """Register all skills: generic built-ins + domain pack + plugins."""
        skills_config = self.config.get_skills_config()
        enabled_skills = skills_config.get('enabled', [])

        generic_classes = {
            'greeting': GreetingSkill,
            'complaint': ComplaintSkill,
            'small_talk': SmallTalkSkill,
            'test': TestSkill,
        }

        # 1) Register generic (built-in) skills
        for skill_name in enabled_skills:
            if skill_name in generic_classes:
                self.skills[skill_name] = generic_classes[skill_name](self.config, self.ai_client)
                self.logger.debug(f"注册通用技能: {skill_name}")

        # 2) Load active domain pack and register its skills
        self._load_domain_pack(enabled_skills)

        # 3) Ensure greeting is always registered
        if 'greeting' not in self.skills:
            self.skills['greeting'] = GreetingSkill(self.config, self.ai_client)

        # 4) Load plugins
        self._load_plugins()

        # 5) Drop intent keyword/pattern entries whose skills are not registered (e.g. payment disabled)
        self._prune_intent_config_to_loaded_skills()

    def _prune_intent_config_to_loaded_skills(self) -> None:
        """Remove intent.keywords / intent.patterns for intents with no registered skill (avoids mis-routing)."""
        available = set(self.skills.keys())
        dropped: List[str] = []
        new_kw: Dict[str, Any] = {}
        for intent, kws in (self.intent_keywords or {}).items():
            if intent in available:
                new_kw[intent] = kws
            else:
                dropped.append(intent)
        new_pat: Dict[str, Any] = {}
        for intent, pats in (self.intent_patterns or {}).items():
            if intent in available:
                new_pat[intent] = pats
            else:
                dropped.append(intent)
        if dropped:
            self.logger.info(
                "Pruned intent config for unregistered skills: %s",
                sorted(set(dropped)),
            )
        self.intent_keywords = new_kw
        self.intent_patterns = new_pat

    def _load_domain_pack(self, enabled_skills: list):
        """Load the active domain pack via DomainLoader."""
        from src.utils.domain_loader import DomainLoader

        config_obj = self.config.config if hasattr(self.config, 'config') else {}
        if not isinstance(config_obj, dict):
            config_obj = {}
        raw_domain = (config_obj.get("domain") or "").strip()
        domain_name = effective_domain_name(config_obj)
        if raw_domain == "payment" and domain_name != "payment":
            self.logger.info(
                "Payment domain plugin disabled; loading domain pack '%s' instead.",
                domain_name,
            )

        project_root = Path(self.config.config_path).parent.parent \
            if hasattr(self.config, 'config_path') else Path(".")
        domains_dir = project_root / "domains"

        loader = DomainLoader(domains_dir)
        pack = loader.load(domain_name, Skill, self.ai_client, self.config)

        if pack is None:
            self.logger.warning("Domain pack '%s' failed to load.", domain_name)
            if payment_plugin_enabled(config_obj):
                self.logger.warning("Falling back to direct payment skill import.")
                self._fallback_direct_import(enabled_skills)
            return

        self._domain_pack = pack

        # Register domain hook if provided
        if pack.hook_class:
            try:
                from src.hooks.registry import HookRegistry
                hook_instance = pack.hook_class(config=self.config)
                HookRegistry.get_instance().register(hook_instance, domain_name)
                self.logger.info("Domain hook registered: %s", pack.hook_class.__name__)
            except Exception as e:
                self.logger.warning("Failed to register domain hook: %s", e)

        # Register domain persona if provided
        if pack.persona:
            try:
                from src.utils.persona_manager import PersonaManager
                PersonaManager.get_instance().set_domain_persona(pack.persona)
                self.logger.info("Domain persona set: %s", pack.persona.get("name", "?"))
            except Exception as e:
                self.logger.warning("Failed to set domain persona: %s", e)

        # 运行时人设覆盖（Web 保存的 persona_runtime.yaml，重启后仍生效）
        try:
            from src.utils.persona_manager import PersonaManager

            _cp = getattr(self.config, "config_path", None)
            if _cp:
                _cfg_dict = self.config.config if hasattr(self.config, "config") else {}
                if PersonaManager.get_instance().load_runtime_default_persona(
                    Path(_cp), _cfg_dict
                ):
                    self.logger.info("已应用 config/persona_runtime.yaml 人设覆盖")
        except Exception as _e:
            self.logger.debug("runtime persona: %s", _e)

        # Apply domain KB categories if provided
        if pack.kb_categories:
            from src.utils.kb_store import set_kb_categories
            cat_names = [c["name"] if isinstance(c, dict) else c for c in pack.kb_categories]
            set_kb_categories(cat_names)
            self.logger.info("KB categories set from domain '%s': %s", domain_name, cat_names)

        # Push domain prompt/terminology to AI client
        if self.ai_client and hasattr(self.ai_client, 'set_domain_pack'):
            self.ai_client.set_domain_pack(
                system_prompt=pack.system_prompt,
                terminology=pack.terminology,
                context_supplements=pack.context_supplements,
            )

        # Merge domain i18n keys
        if pack.i18n:
            try:
                from src.utils.i18n import I18n
                i18n_instance = I18n()
                i18n_instance.merge_domain_keys(pack.i18n)
            except Exception as e:
                self.logger.warning("Failed to merge domain i18n keys: %s", e)

        # Set domain config directory for kb_direct_render
        domain_config_dir = pack.root / "config"
        if domain_config_dir.exists():
            from src.utils.kb_direct_render import set_domain_config_dir
            set_domain_config_dir(domain_config_dir)

        # Set domain-specific emotion enhancer skip rules
        defaults = pack.config_data.get('defaults', {})
        emotion_skip = defaults.get('emotion_enhancer_skip', {})
        if emotion_skip:
            from src.skills.emotion_enhancer import EmotionEnhancer
            EmotionEnhancer.set_domain_skip_rules(
                skip_phrases=emotion_skip.get('phrases', []),
                skip_patterns=[
                    (p.get('all_of', []), p.get('any_of', []))
                    for p in emotion_skip.get('patterns', [])
                ],
            )

        for skill_name, skill_class in pack.skill_classes.items():
            if skill_name in enabled_skills and skill_name not in self.skills:
                try:
                    self.skills[skill_name] = skill_class(self.config, self.ai_client)
                    self.logger.debug(f"注册域技能: {skill_name} (from {domain_name})")
                except Exception as e:
                    self.logger.error(f"域技能 {skill_name} 实例化失败: {e}")

    def _fallback_direct_import(self, enabled_skills: list):
        """Fallback: import payment skills directly if DomainLoader fails (payment plugin only)."""
        cfg = self.config.config if hasattr(self.config, "config") else {}
        if not payment_plugin_enabled(cfg if isinstance(cfg, dict) else {}):
            self.logger.debug("Payment skill fallback skipped (domain_plugins.payment.enabled is false).")
            return
        try:
            from domains.payment.skills import (
                GxpCommandSkill, OrderQuerySkill, PriceCheckSkill,
                StatusCheckSkill, ChannelInfoSkill, QuotaConfigSkill,
                EnhancedQuotaConfigSkill,
            )
            fallback_map = {
                'gxp_command': GxpCommandSkill,
                'order_query': OrderQuerySkill,
                'price_check': PriceCheckSkill,
                'status_check': StatusCheckSkill,
                'channel_info': ChannelInfoSkill,
                'quota_config': QuotaConfigSkill,
                'enhanced_quota_config': EnhancedQuotaConfigSkill,
            }
            for skill_name in enabled_skills:
                if skill_name in fallback_map and skill_name not in self.skills:
                    self.skills[skill_name] = fallback_map[skill_name](self.config, self.ai_client)
                    self.logger.debug(f"注册技能(fallback): {skill_name}")
        except ImportError as e:
            self.logger.error(f"Fallback import also failed: {e}")

    def _load_plugins(self):
        from src.utils.plugin_loader import PluginLoader
        plugin_dir = Path(self.config.config_path).parent.parent / "plugins" if hasattr(self.config, "config_path") else Path("plugins")
        cfg = self.config.config if hasattr(self.config, "config") else {}
        self._plugin_loader = PluginLoader(plugin_dir, cfg)
        plugins = self._plugin_loader.load_all(Skill, self.ai_client, self.config)
        for name, skill in plugins.items():
            intent_name = f"plugin_{name}"
            self.skills[intent_name] = skill
            self.logger.info("插件�€能已注册: %s �?%s", intent_name, skill.__class__.__name__)

    async def process_message(
        self,
        text: str,
        user_id: Any,
        context: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """
        # 处理用户消息（Per-Chat-User 串�锁：同群同用户串行保序， 不同群可并�处理，避免跨群阻塞）
        """
        chat_id = (context or {}).get('chat_id', '')
        lock_key = f"{chat_id}_{user_id}" if chat_id else str(user_id)
        lock = self._user_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            return await self._handle_message_guarded(text, user_id, context)

    async def _handle_message_guarded(
        self,
        text: str,
        user_id: Any,
        context: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        user_ctx_for_cleanup: Optional[Dict[str, Any]] = None
        try:
            user_id_str = str(user_id)
            context = context or {}
            if not context.get("request_id"):
                context["request_id"] = f"r-{uuid.uuid4().hex[:12]}"

            req_id = context.get("request_id", "")
            log_prefix = f"[{req_id}] " if req_id else ""

            self._refresh_strategies()

            _chat_id = context.get('chat_id', '')

            # 1. 获取或创建用户上下文（遗忘指令需优先于冷却）
            user_context = self._get_user_context(user_id_str)
            user_ctx_for_cleanup = user_context
            last_intent = user_context.get('current_intent', '')

            # P7-1：将调用方单次请求的 LINE RPA 上下文并入 user_context，
            # 供 AIClient._build_context_prompt 读取 channel / line_rpa_style_hint 等
            _line_merge_keys = (
                "channel", "request_id", "reply_lang",
                "reply_lang_locked",
                "line_rpa_style_hint", "line_rpa_chat_key",
                "messenger_rpa_style_hint", "messenger_rpa_chat_key",
                "messenger_rpa_peer_kind",
                "whatsapp_rpa_chat_key", "whatsapp_rpa_peer_name",
                "whatsapp_rpa_style_hint",
                "account_persona_id",
                "suppress_global_ai_identity",
                "disable_episodic_memory",
                "is_group", "mentioned", "vision_room",
                # Phase 1：用户画像上下文 — 由 runner 从 ContactGateway 渲染好后注入
                "contact_id", "_contact_portrait_block",
                # W2-D1：IntimacyEngine 的 score → companion_relationship 双信号融合
                "intimacy_score",
                # P10-C：漏斗阶段 → PersonaManager 平台感知 prompt 注入
                "funnel_stage",
                # 单条主消息（用于语言注入，避免多条合并文本污染检测）
                "_current_user_message_for_lang",
                # S5: 平台标识，用于 CrossPlatformIdentity canonical_id 解析
                "platform",
                # 重复消息标记（runner 检测到用户发了跟上次完全一样的消息）
                "_is_repeated_message", "_prev_reply_for_repeat",
                # 语音消息标记（对方发的是语音，AI 回复应更口语化）
                "_peer_message_is_voice", "_voice_duration",
            )
            for _mk in _line_merge_keys:
                if _mk in context and context[_mk] is not None:
                    user_context[_mk] = context[_mk]

            _forget_reply = self._handle_episodic_forget_command(
                text, user_id_str, user_context, _chat_id
            )
            if _forget_reply is not None:
                self._context_store.mark_dirty(user_id_str)
                self._context_store.flush(user_id_str)
                return _forget_reply

            # Stage 1：剧情/成长指令前懒解析端用户真实付费权益进 user_context——
            # 让付费剧情闸（story_engine.require_unlock）据真实拥有判准入（付费用户进得去、
            # 免费看到锁）。resolver 未注册（变现未就绪）→ entitlement 维持 None，零回归。
            self._ensure_entitlement(user_id_str, user_context)
            # Phase ③：剧情指令（列表/开始/结束）。返回字符串=短路；None=非指令或开始成功
            # （开始成功只置 state，后续正常生成带【剧情场景】块的开场）。
            try:
                _story_reply = self._handle_story_command(
                    text, user_context, _chat_id
                )
            except Exception:
                _story_reply = None
                self.logger.debug("story command skipped", exc_info=True)
            if _story_reply is not None:
                self._context_store.mark_dirty(user_id_str)
                self._context_store.flush(user_id_str)
                return _story_reply

            # Phase ④续³：关系/成长面板（端用户在对话内查询自己的成长——把记忆/成长/剧情
            # 整条链的进度一屏看见）。返回字符串=短路；None=非指令。
            try:
                _growth_reply = self._handle_growth_command(
                    text, user_context, _chat_id
                )
            except Exception:
                _growth_reply = None
                self.logger.debug("growth command skipped", exc_info=True)
            if _growth_reply is not None:
                return _growth_reply

            # Stage A：形象照/自拍请求（「给我看看你」）。返回字符串=短路（搪塞/付费引导/
            # 出图配文/兜底）；""=媒体已发出不再补文字；None=非请求或功能未开。
            try:
                _selfie_reply = await self._handle_selfie_request(
                    text, user_id_str, user_context, _chat_id
                )
            except Exception:
                _selfie_reply = None
                self.logger.debug("selfie request skipped", exc_info=True)
            if _selfie_reply is not None:
                self._context_store.mark_dirty(user_id_str)
                self._context_store.flush(user_id_str)
                return _selfie_reply or None

            # 2. 冷却
            if not self._check_cooldown(text, user_id_str, chat_id=_chat_id):
                self.logger.warning(f"{log_prefix}用户 {user_id_str} 处于冷却期，跳过回�")
                return None

            # 3. 注入情景记忆（关键词 / 向量融合 + 分桶）
            if user_context.get("disable_episodic_memory"):
                user_context.pop("_episodic_memory_text", None)
            else:
                _q_emb = None
                _mvec = (self._memory_cfg or {}).get("vector") or {}
                if (
                    self._episodic_store
                    and (self._memory_cfg or {}).get("enabled", True)
                    and _mvec.get("enabled", False)
                    and self.ai_client
                ):
                    _q_emb = await self._embed_user_message_for_episodic(text)
                self._inject_episodic_into_context(
                    user_context,
                    user_id_str,
                    _chat_id,
                    current_user_text=text,
                    query_embedding=_q_emb,
                    platform=user_context.get("platform", ""),  # S5
                )

            # 3b. 情感智能上下文引擎（情绪分析 + 时间感知 + 记忆反思 + 关系温度）
            try:
                from src.utils.emotional_context import build_emotional_context_block
                _epi_text = (user_context.get("_episodic_memory_text") or "").strip()
                # 共情策略选择器开关（默认开；纯 prompt 提示，零行为风险）
                _cfg_es = self.config.config if hasattr(self.config, "config") else {}
                _es_on = bool(
                    ((_cfg_es.get("companion") or {}).get("empathy_strategy") or {})
                    .get("enabled", True)
                ) if isinstance(_cfg_es, dict) else True
                # R4 安全守卫开关（默认开，与 persona_guard 同为安全家族；纯 prompt 提示）
                _wb_cfg = (
                    ((_cfg_es.get("companion") or {}).get("wellbeing") or {})
                    if isinstance(_cfg_es, dict) else {}
                )
                _wb_on = bool(_wb_cfg.get("enabled", True))
                _antisyc_on = bool(_wb_cfg.get("anti_sycophancy", True))
                _wb_hotline = str(_wb_cfg.get("crisis_resources", "") or "")
                _emo_block = build_emotional_context_block(
                    text, user_context, _epi_text, chat_id=_chat_id,
                    enable_strategy=_es_on,
                    enable_wellbeing=_wb_on,
                    enable_anti_sycophancy=_antisyc_on,
                    wellbeing_hotline=_wb_hotline,
                )
                if _emo_block:
                    user_context["_emotional_context_block"] = _emo_block
                    self.logger.info(
                        "%s情感上下文引擎: emotion=%s valence=%s warmth_label=%s",
                        log_prefix,
                        user_context.get("_prev_emotion", "?"),
                        user_context.get("_prev_valence", "?"),
                        "active",
                    )
            except Exception:
                self.logger.warning("emotional_context inject skipped", exc_info=True)

            # 4. 合并传入的上下文信息（上下文分析、图�?OCR、群内机器人消息、request_id、chat�?
            if 'context_analysis' in context:
                user_context['context_analysis'] = context['context_analysis']
            if 'image_ocr_text' in context and context['image_ocr_text']:
                user_context['image_ocr_text'] = context['image_ocr_text']
            if 'recent_bot_messages' in context and context['recent_bot_messages']:
                user_context['recent_bot_messages'] = context['recent_bot_messages']
            if context.get('request_id'):
                user_context['request_id'] = context['request_id']
            if 'chat_id' in context:
                user_context['chat_id'] = context['chat_id']
            if 'chat_title' in context:
                user_context['chat_title'] = context['chat_title']
            if context.get('user_emotion_hint'):
                user_context['user_emotion_hint'] = context['user_emotion_hint']
            if '_send_to_chat' in context:
                user_context['_send_to_chat'] = context['_send_to_chat']
            if context.get('triggered_by_mention') is not None:
                user_context['triggered_by_mention'] = context['triggered_by_mention']

            # P1 陪伴关系阶段（conversion；持久化于 companion_relationship[chat_key]）
            try:
                _cfg_dom = self.config.config if hasattr(self.config, "config") else {}
                _comp_cfg = (_cfg_dom.get("companion") or {}) if isinstance(_cfg_dom, dict) else {}
                if effective_domain_name(_cfg_dom) == "conversion" and _comp_cfg.get(
                    "enabled", True
                ):
                    from src.utils.companion_relationship import (
                        build_relationship_prompt_block,
                        downgrade_from_user_text,
                        get_rel_state,
                    )

                    _rst = get_rel_state(user_context, _chat_id)
                    downgrade_from_user_text(_rst, text, _comp_cfg)
                    _ain = ""
                    try:
                        _ain = str(
                            (self.config.get_ai_config() or {}).get("ai_name") or ""
                        ).strip()
                    except Exception:
                        pass
                    # W2-D1：拉 IntimacyEngine 的 score（runner 已注入到 context）
                    # 没传则 fusion 自动跳过 → 完全向后兼容
                    _intim_score = context.get("intimacy_score")
                    try:
                        _intim_score = (
                            float(_intim_score) if _intim_score is not None else None
                        )
                    except (TypeError, ValueError):
                        _intim_score = None
                    user_context["_relationship_prompt_block"] = (
                        build_relationship_prompt_block(
                            _rst, _comp_cfg, ai_name=_ain, user_message=text,
                            intimacy_score=_intim_score,
                        )
                    )
                    user_context["relationship_stage"] = str(_rst.get("stage") or "")
            except Exception:
                self.logger.debug("companion relationship inject skipped", exc_info=True)

            # W3-3M：RelationshipStager — 跨域轻量语气指令
            # 与 companion_relationship 互不干扰：conversion 域有完整关系块，
            # 其他域（或 companion 未启用时）通过此注入获得漏斗语气校准。
            try:
                _fstage = (context or {}).get("funnel_stage") or ""
                _fscore = (context or {}).get("intimacy_score")
                if _fstage:
                    from src.contacts.relationship_stager import stage_directive
                    _directive = stage_directive(_fstage, _fscore)
                    if _directive:
                        user_context["_funnel_directive"] = _directive
                        self.logger.debug(
                            "[3M] funnel_directive stage=%s score=%s",
                            _fstage, _fscore,
                        )
            except Exception:
                self.logger.debug("relationship_stager inject skipped", exc_info=True)

            # Phase ②：关系成长「厚度/里程碑」感知块（默认关，companion.bond_level.enabled）。
            # 复用上面已取的 intimacy_score；只在 intimate/steady 或刚达成里程碑时产出一句，
            # 由 build_bond_level_block 内部克制（initial/warming 无里程碑 → 空，不打扰）。
            try:
                _bl_cfg = ((self.config.config or {}).get("companion") or {}).get(
                    "bond_level"
                ) if hasattr(self.config, "config") else None
                if _bl_cfg and _bl_cfg.get("enabled", False):
                    from src.contacts.relationship_level import build_bond_level_block
                    # Phase ④：基础 intimacy + 剧情累计加成（完成深度剧情真实推动 bond）
                    _bscore = self._effective_intimacy(user_context, _chat_id)
                    if _bscore is None:
                        _bscore = (context or {}).get("intimacy_score")
                    _days_known = (context or {}).get("relationship_days")
                    # Phase ④续：一次性消费剧情完成纪念点（持久于 user_context，致意后清除）
                    _fresh = (
                        user_context.pop("bond_fresh_milestone", None)
                        or (context or {}).get("bond_fresh_milestone")
                    )
                    _bl_block = build_bond_level_block(
                        _bscore,
                        days_known=_days_known,
                        fresh_milestone=_fresh,
                    )
                    if _bl_block:
                        user_context["_bond_level_block"] = _bl_block
            except Exception:
                self.logger.debug("bond_level inject skipped", exc_info=True)

            # Phase ③：活动剧情 → 注入当前 beat 的【剧情场景】导演指令（默认关）。
            try:
                _scfg = self._story_cfg()
                if _scfg.get("enabled", False):
                    _sstate = self._get_story_state(user_context, _chat_id)
                    if _sstate:
                        from src.skills.story_engine import build_story_prompt_block
                        _sblk = build_story_prompt_block(
                            _sstate, self._story_scenarios())
                        if _sblk:
                            user_context["_story_block"] = _sblk
            except Exception:
                self.logger.debug("story inject skipped", exc_info=True)

            from src.hooks.registry import HookRegistry as _HR
            _hooks = _HR.get_instance()

            # 3b. 检测用户消息语言，传入 reply_lang 供 KB 搜索和程序化回复
            # 若调用方提供了 _current_user_message_for_lang（单条主消息），优先以此判断
            # 避免 [对方连发] 合并文本中的英文干扰中/日文消息的语言判断
            if (
                self.ai_client
                and hasattr(self.ai_client, '_detect_message_language')
                and not user_context.get("reply_lang_locked")
            ):
                _lang_detect_src = (
                    (context.get("_current_user_message_for_lang") or "").strip() or text
                )
                _detected_lang = self.ai_client._detect_message_language(_lang_detect_src)
                _prev_lang = user_context.get('reply_lang', 'zh')
                _stripped = (text or "").strip()
                if _detected_lang == "zh" and _prev_lang != "zh":
                    _has_cjk = bool(re.search(r"[\u4e00-\u9fff]", _stripped))
                    if not _has_cjk and len(_stripped) <= 20:
                        _detected_lang = _prev_lang
                # Domain-specific ambiguous tokens (e.g. EP/JC): may confuse lang detection
                if _hooks.is_ambiguous_token_message(_stripped):
                    _lm = (user_context.get("last_message") or "").strip()
                    if _lm and not _hooks.is_ambiguous_token_message(_lm):
                        _detected_lang = self.ai_client._detect_message_language(_lm)
                    else:
                        _detected_lang = _prev_lang
                user_context['reply_lang'] = _detected_lang

            # 4. Intent recognition + domain hook override
            text_stripped = text.strip()
            if (
                "order_query" in self.skills
                and last_intent == "order_query"
                and text_stripped.isdigit()
                and 6 <= len(text_stripped) <= 24
            ):
                intent = "order_query"
            else:
                intent = self._recognize_intent(text)

            # Domain hook: allow domain pack to override intent
            from src.hooks.base import HookContext as _HookCtx
            _hook_ctx = _HookCtx(
                text=text, user_id=user_id_str, chat_id=str(_chat_id),
                intent=intent, last_intent=last_intent,
                last_message=user_context.get("last_message", ""),
                last_reply=user_context.get("last_reply", ""),
                reply_lang=user_context.get("reply_lang", "zh"),
                user_context=user_context,
                extra={"available_skills": set(self.skills.keys())},
            )
            intent = await _hooks.dispatch_intent_resolved(intent, _hook_ctx)

            # Intent inheritance: direct_chat fallback inherits recent business intent
            # 且上条是具体业务意图且在 120 秒内，继承上条意图以保持多轮连贯
            _INHERITABLE_INTENTS = {
                k for k in (
                    "order_query", "channel_info", "complaint", "status_check", "price_check",
                ) if k in self.skills
            }
            if (intent == "direct_chat"
                    and last_intent in _INHERITABLE_INTENTS
                    and user_context.get("last_message_time")
                    and (time.time() - user_context["last_message_time"]) < 120):
                if _hooks.is_meaningless_interjection(text_stripped):
                    self.logger.debug(
                        "%s意图继承跳过: 纯语气词/无实质内容 '%s'",
                        log_prefix, text_stripped[:20],
                    )
                else:
                    intent = last_intent
                    self.logger.info(f"{log_prefix}意图继承: direct_chat -> {intent}（上条意图 {last_intent}，120s 内追问）")

            self.logger.debug(f"{log_prefix}识别到意图: {intent} (消息: {text[:50]}...)")

            if not self._narrow_reply_allows(text, intent, last_intent, user_context):
                self.logger.warning("%snarrow_reply: 非允许范围，跳过回复 (intent=%s)", log_prefix, intent)
                return None

            # 4b 按意图冷却：配置 by_intent 时，距上次回复不足N秒数则跳过
            need_gap = self.cooldown_by_intent.get(intent)
            _tp = context.get('_trigger_path')
            if need_gap and isinstance(need_gap, (int, float)) and need_gap > 0:
                _exempt = False
                if (
                    "order_query" in self.skills
                    and intent == "order_query"
                    and text_stripped.isdigit()
                    and 6 <= len(text_stripped) <= 24
                ):
                    _exempt = True
                if _tp and _tp in ("l1_rule", "mention", "reply_chain"):
                    _exempt = True
                if user_context.get("_bot_question_ts") and (time.time() - user_context.get("_bot_question_ts", 0)) < 120:
                    _exempt = True
                if context.get('triggered_by_mention'):
                    _exempt = True
                if not _exempt:
                    last_rt = user_context.get('last_reply_time') or 0
                    now = time.time()
                    if now - last_rt < float(need_gap):
                        self.logger.warning(
                            f"{log_prefix}意图 {intent} 处于 by_intent 冷却期 ({need_gap}s)，距上次回复 {now-last_rt:.1f}s，跳过"
                        )
                        return None

            _saved_prev_message = user_context.get('last_message', '')
            _saved_prev_reply = user_context.get('last_reply', '')

            user_context.update({
                'last_message': text,
                'last_message_time': time.time(),
                'current_intent': intent
            })

            # H3: 意图链跟�?�?记录去重意图序列 + 模式识别
            _intent_chain = user_context.get('_intent_chain', [])
            if not isinstance(_intent_chain, list):
                _intent_chain = []
            if not _intent_chain or _intent_chain[-1] != intent:
                _intent_chain.append(intent)
            if len(_intent_chain) > 15:
                _intent_chain = _intent_chain[-10:]
            user_context['_intent_chain'] = _intent_chain
            _chain_hint = self._detect_chain_pattern(_intent_chain)
            if _chain_hint:
                user_context['_chain_pattern'] = _chain_hint
                if not user_context.get('_case_id'):
                    user_context['_case_id'] = f"CASE-{user_id_str[-6:]}-{int(time.time()) % 100000}"
                self.logger.info(
                    "%s意图链模�? %s case=%s chain=%s",
                    log_prefix, _chain_hint["pattern"],
                    user_context.get('_case_id', ''),
                    # " �?".join(_intent_chain[-5:])
                )

            # K1: 对话历史窗口 + 摘�压缩（保留最�?3 ����?+ 早期摘��?
            _conv_hist = user_context.get('_conversation_history', [])
            if not isinstance(_conv_hist, list):
                _conv_hist = []
            # Fix D: retroactive sanitize on loaded history（修复自我强化幻觉污染；失败不阻断流程）
            try:
                _conv_hist = self._sanitize_history_name_claims(_conv_hist, user_context)
            except Exception as _se1:
                self.logger.debug("sanitize_history skipped: %s", _se1)
            if _saved_prev_message and _saved_prev_reply:
                try:
                    _clean_prev_reply = self._sanitize_assistant_reply(_saved_prev_reply, user_context)
                except Exception as _se2:
                    self.logger.debug("sanitize_prev_reply skipped: %s", _se2)
                    _clean_prev_reply = _saved_prev_reply
                _conv_hist.append({"role": "user", "content": _saved_prev_message[:200]})
                _conv_hist.append({"role": "assistant", "content": _clean_prev_reply[:300]})
            _KEEP_VERBATIM = 5  # 与 reply 策略 context_rounds≈5 对齐，避免本地先裁成 3 轮导致模型「失忆」
            _COMPRESS_THRESHOLD = 8  # 更长对话才摘要，减少过早丢轮次
            _total_rounds = len(_conv_hist) // 2
            if _total_rounds > _COMPRESS_THRESHOLD:
                _old_msgs = _conv_hist[:(-_KEEP_VERBATIM * 2)]
                # ★ Phase 2：默认用 LLM 摘要（更连贯），rule-based 作 fallback
                _summary = await self._summarize_history_with_fallback(_old_msgs)
                _conv_hist = _conv_hist[-_KEEP_VERBATIM * 2:]
                user_context['_conversation_summary'] = _summary
            elif _total_rounds > _KEEP_VERBATIM:
                _conv_hist = _conv_hist[-_KEEP_VERBATIM * 2:]
            user_context['_conversation_history'] = _conv_hist

            # 追问回填 �?用户新消���达，回溯标�前一条策略事�?
            _chat_id = _safe_int_chat_id(context.get("chat_id", 0))
            try:
                self._strategy_tracker.backfill_follow_up(user_id_str, _chat_id, intent)
            except Exception:
                pass

            # 4a. 解析回复策略（支持 A/B 灰度分流）
            strategy, strategy_id = self.get_strategy_for_intent(intent, user_id_str)
            strategy = strategy or {}

            # LINE RPA（私聊）：可选整策略覆盖 + 默认跳过「静默概率」以免实测/私聊被 S5 随机吞掉
            if context.get("channel") == "line_rpa" and hasattr(
                self.config, "get_line_rpa_config"
            ):
                lr = self.config.get_line_rpa_config() or {}
                ro = (lr.get("reply_strategy_override") or "").strip()
                if ro and (self._strategies or {}).get(ro, {}).get("enabled", True):
                    strategy = dict((self._strategies or {}).get(ro) or {})
                    strategy_id = ro
                    self.logger.info(
                        "%sLINE RPA reply_strategy_override -> %s",
                        log_prefix,
                        strategy_id,
                    )
                if lr.get("skip_silent_probability", True):
                    strategy = dict(strategy)
                    strategy["reply_probability"] = 1.0

            # Messenger RPA（1v1 私聊）：同 LINE 一致的整策略覆盖路径
            # 默认跳过 skip_ai（复读机的根源），并默认跳过 S5 静默概率
            if context.get("channel") == "messenger_rpa" and hasattr(
                self.config, "get_messenger_rpa_config"
            ):
                mr = self.config.get_messenger_rpa_config() or {}
                ro = (mr.get("reply_strategy_override") or "").strip()
                if ro and (self._strategies or {}).get(ro, {}).get("enabled", True):
                    strategy = dict((self._strategies or {}).get(ro) or {})
                    strategy_id = ro
                    self.logger.info(
                        "%sMessenger RPA reply_strategy_override -> %s (was intent=%s)",
                        log_prefix,
                        strategy_id,
                        intent,
                    )
                # 不允许 skip_ai（即使 override 没配，也强制让 greeting 走 AI，避免复读机）
                if mr.get("disable_skip_ai_templates", True):
                    strategy = dict(strategy)
                    if strategy.get("skip_ai"):
                        self.logger.info(
                            "%sMessenger RPA: disable skip_ai (intent=%s) → 走 AI 生成",
                            log_prefix,
                            intent,
                        )
                    strategy["skip_ai"] = False
                if mr.get("skip_silent_probability", True):
                    strategy = dict(strategy)
                    strategy["reply_probability"] = 1.0

            # WhatsApp RPA（1v1 私聊）：同 Messenger RPA 一致，强制 reply_probability=1.0 + 禁止 skip_ai
            if context.get("channel") == "whatsapp_rpa":
                strategy = dict(strategy)
                strategy["skip_ai"] = False
                strategy["reply_probability"] = 1.0

            # S5 静默观察：按概率决定不回复
            # 触发系统已判定应回复（_trigger_path 存在）或 @ 时，跳过概率检查
            rp = strategy.get('reply_probability')
            if rp is not None and isinstance(rp, (int, float)) and rp < 1.0:
                _tp = context.get('_trigger_path')
                if context.get('triggered_by_mention') or _tp:
                    self.logger.info(f"{log_prefix}触发���={_tp or 'mention'}，跳�?S5 概率�€�?")
                elif random.random() > rp:
                    self.logger.warning(f"{log_prefix}策略 {strategy_id} 静默跳过 (概率 {rp})")
                    return None

            user_context['_reply_strategy'] = strategy
            user_context['_reply_strategy_id'] = strategy_id

            # �€�€ 隐式反��€�?�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€
            # 若上�€条是 AI 回�，本条是用户���应（3 分钟内），�测情����?
            _pending_fb = user_context.get("_awaiting_kb_feedback")
            if _pending_fb and isinstance(_pending_fb, dict):
                _fb_ts = _pending_fb.get("ts", 0)
                if time.time() - _fb_ts < 180:  # 3 分钟窗口
                    _signal = self._detect_implicit_feedback(text)
                    if _signal:
                        try:
                            _kb2 = self._kb_store_if_exists()
                            if _kb2:
                                _kb2.add_feedback({
                                    "user_message": _pending_fb.get("user_msg", ""),
                                    "ai_reply":     _pending_fb.get("ai_reply", ""),
                                    "score":        1 if _signal == "pos" else -1,
                                    "correction":   text if _signal == "neg" else "",
                                    "operator":     "auto_detect",
                                })
                                self.logger.debug("隐式反�已��? %s", _signal)
                        except Exception as _fb_err:
                            self.logger.debug("隐式反�记录失败: %s", _fb_err)
                user_context.pop("_awaiting_kb_feedback", None)

            # �€�€ KB 混合�€�?�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€
            #策略：BM25 优先，弱命中时懒触发向量搜索（节�?~70% Embedding API 调用�?
            _hook_ctx.intent = intent
            _, _skip_kb_for_channel_metrics = await _hooks.dispatch_kb_pre_search(text, _hook_ctx)
            if _skip_kb_for_channel_metrics:
                self.logger.info(
                    "%sKB 跳过：域包 hook 指示跳过 KB 搜索（意图=%s）",
                    log_prefix, intent,
                )
            try:
                if not _skip_kb_for_channel_metrics:
                    _kb = self._kb_store_if_exists()
                else:
                    _kb = None
                if _kb:
                    _lang = (user_context or {}).get("reply_lang", "zh")

                    # Step 1: BM25 先�
                    _bm25_result = _kb.search(text, top_k=3, lang=_lang)
                    _top_bm25_score = (
                        _bm25_result["entries"][0].get("_score", 0)
                        if _bm25_result["entries"] else 0
                    )

                    if _top_bm25_score >= _BM25_STRONG_THRESHOLD:
                        _search_result = _bm25_result
                        self.logger.info(
                            "%sKB BM25 强命中(%.3f)，跳过向量化", log_prefix, _top_bm25_score
                        )
                    else:
                        #BM25 弱命�?�?尝试向量搜索（懒触发，最多等 8 秒，防� Embedding API 挂起�?
                        try:
                            _query_vec = await asyncio.wait_for(
                                self._get_embedding_cached(text), timeout=8.0
                            )
                        except asyncio.TimeoutError:
                            self.logger.warning("KB Embedding 调用超时（>8s），降级为纯 BM25")
                            _query_vec = None
                        if _query_vec:
                            _search_result = _kb.search(
                                text, top_k=3, lang=_lang, query_vec=_query_vec
                            )
                            _mode = _search_result.get("search_mode", "bm25")
                            self.logger.info(
                                "%sKB 混合搜索模式 %s，BM25原始=%.3f",
                                log_prefix, _mode, _top_bm25_score
                            )
                        else:
                            _search_result = _bm25_result

                    _kb_ctx = _kb.build_ai_context_from_result(_search_result, lang=_lang)
                    _hit = bool(_kb_ctx)
                    _mode = _search_result.get("search_mode", "bm25")
                    _cat  = (_search_result["entries"][0]["category"]
                             if _search_result.get("entries") else "")

                    _matched_eid = (_search_result["entries"][0].get("id", "")
                                    if _search_result.get("entries") else "")

                    if _hit:
                        _top_entry = _search_result["entries"][0] if _search_result.get("entries") else {}
                        _top_title = _top_entry.get("title", "")
                        _top_reply_mode = _top_entry.get("reply_mode", "ai_guided")

                        # Guard: channel_info 意图时，跳过通道数据相关的 direct 条目
                        _CH_BLOCK_CATS = {"通道状态"}
                        _CH_BLOCK_KW = ("成功率", "费率", "额度", "限额", "代收", "代付",
                                        "通道状态", "channel")
                        _is_ch_intent = intent in ("channel_info", "status_check")
                        _e_blob = f"{_top_entry.get('category', '')} {_top_title} {_top_entry.get('triggers', '')}".lower()
                        _e_is_ch = (
                            _top_entry.get("category") in _CH_BLOCK_CATS
                            or any(kw in _e_blob for kw in _CH_BLOCK_KW)
                        )
                        if _is_ch_intent and _e_is_ch and _top_reply_mode == "direct":
                            self.logger.info(
                                "%sKB direct 跳过（通道数据由程序化回复处理）: '%s'",
                                log_prefix, _top_title,
                            )
                            _top_reply_mode = "ai_guided"

                        if _is_ch_intent and not _e_is_ch and _top_reply_mode == "direct":
                            self.logger.info(
                                "%sKB direct 降级（channel_info意图但KB条目非通道类）: '%s'",
                                log_prefix, _top_title,
                            )
                            _top_reply_mode = "ai_guided"

                        # E2a: 非中文用户 direct 降级为 ai_guided，让 AI 翻译
                        if _top_reply_mode == "direct" and _lang != "zh":
                            self.logger.info(
                                "%sKB direct 降级 ai_guided（非中文 lang=%s）: '%s'",
                                log_prefix, _lang, _top_title,
                            )
                            _top_reply_mode = "ai_guided"

                        # E2b: 短查询或低分 direct 降级为 ai_guided
                        _KB_DIRECT_MIN_SCORE = 0.45
                        _KB_DIRECT_MIN_QUERY_LEN = 6
                        if _top_reply_mode == "direct":
                            _text_len = len(text.strip())
                            if _top_bm25_score < _KB_DIRECT_MIN_SCORE:
                                self.logger.info(
                                    "%sKB direct 降级 ai_guided（BM25分数 %.3f < %.2f）: '%s'",
                                    log_prefix, _top_bm25_score, _KB_DIRECT_MIN_SCORE, _top_title,
                                )
                                _top_reply_mode = "ai_guided"
                            elif _text_len < _KB_DIRECT_MIN_QUERY_LEN:
                                self.logger.info(
                                    "%sKB direct 降级 ai_guided（短查询 len=%d < %d）: '%s'",
                                    log_prefix, _text_len, _KB_DIRECT_MIN_QUERY_LEN, _top_title,
                                )
                                _top_reply_mode = "ai_guided"

                        # E2: direct 模式 �?跳过 AI；支�?reply_direct_spec（�€�道/分支/片�/受控����?
                        if _top_reply_mode == "direct":
                            _cfg_dir = Path(self.config.config_path).parent if hasattr(
                                self.config, "config_path"
                            ) else Path("config")
                            try:
                                from src.utils.kb_direct_render import render_kb_direct_reply
                                _direct, _dm = await render_kb_direct_reply(
                                    _top_entry, text, _cfg_dir, self.ai_client
                                )
                            except Exception as _dr_err:
                                self.logger.warning("KB direct 渲染异常，降�?legacy: %s", _dr_err)
                                from src.utils.kb_direct_render import legacy_direct_text
                                _direct = legacy_direct_text(_top_entry)
                                _dm = {"path": ["error", str(_dr_err)]}
                            if _direct:
                                # companion guard: skip KB 直出 for companion domain + non-biz messages
                                _cfg_gd = self.config.config if hasattr(self.config, "config") else {}
                                _is_comp_gd = (
                                    isinstance(_cfg_gd, dict)
                                    and effective_domain_name(_cfg_gd) == "conversion"
                                )
                                if _is_comp_gd:
                                    _biz_kws_gd = (
                                        "通道", "订单", "查单", "费率", "代收", "代付",
                                        "成功率", "限额", "回调", "转账", "支付",
                                        "channel", "order", "payment", "payin", "payout",
                                    )
                                    _has_biz_gd = any(k in (text or "") for k in _biz_kws_gd)
                                    _comp_intents = ("greeting", "small_talk", "direct_chat", "complaint")
                                    if intent in _comp_intents and not _has_biz_gd:
                                        self.logger.info(
                                            "%s companion: skip KB direct 直出 (intent=%s no biz kw)",
                                            log_prefix, intent,
                                        )
                                        _direct = None  # fall through → normal AI with persona
                                if _direct:
                                    _kb.inc_use_count(_matched_eid)
                                self.logger.info(
                                    "%sKB direct 命中 [%s] '%s' �?直出 path=%s branch=%s router=%s",
                                    log_prefix, _mode, _top_title,
                                    _dm.get("path"), _dm.get("branch"), _dm.get("router"),
                                )
                                try:
                                    _kb.log_query(
                                        text, hit=True, search_mode=_mode,
                                        category=_cat, lang=_lang,
                                        score=_top_bm25_score,
                                        matched_entry_id=_matched_eid,
                                    )
                                except Exception:
                                    pass
                                if _direct is not None:
                                    return _direct
                                # companion cleared _direct -> fall through to normal AI

                        _skip_kb_inject = _is_ch_intent and not _e_is_ch
                        if not _skip_kb_inject:
                            user_context["kb_context"] = _kb_ctx
                        else:
                            self.logger.info(
                                "%sKB context skipped (channel_info but KB not channel): '%s'",
                                log_prefix, _top_title,
                            )
                        user_context["_kb_search_mode"] = _mode
                        self.logger.info(
                            "%sKB 命中 [%s] %s (score=%.3f mode=%s) → 注入 %d 字符",
                            log_prefix, _mode, _top_title,
                            _top_bm25_score, _top_reply_mode, len(_kb_ctx)
                        )
                        # M4: 非中文用户命���翻译条目时，记录翻译�€�?
                        if _lang != "zh" and _matched_eid:
                            try:
                                _entry_data = _search_result["entries"][0]
                                _has_trans = bool(_entry_data.get(f"example_reply_{_lang}"))
                                if not _has_trans:
                                    _kb.log_miss(f"[TRANSLATE:{_lang}:{_matched_eid}] {_top_title}")
                                    self.logger.info(
                                        "%s翻译缺口: entry=%s lang=%s",
                                        log_prefix, _matched_eid, _lang
                                    )
                            except Exception:
                                pass
                    else:
                        _kb.log_miss(text)
                        self.logger.info(
                            "%sKB 未命中 (BM25=%.3f) msg='%s'",
                            log_prefix, _top_bm25_score, text[:30]
                        )

                    #写入查�日志（含分数 + 匹配条目ID，用于弱命中分析�?
                    try:
                        _kb.log_query(
                            text, hit=_hit,
                            search_mode=_mode, category=_cat, lang=_lang,
                            score=_top_bm25_score,
                            matched_entry_id=_matched_eid,
                        )
                        _EMBED_STATS["kb_queries"] += 1
                        if _hit:
                            _EMBED_STATS["kb_hits"] += 1
                    except Exception:
                        pass
            except Exception as _kb_err:
                self.logger.warning("KB 检索失败（非阻塞）: %s", _kb_err)

            # Domain hook: inject live状态（仅支付等业务域；陪聊域不注入，避免模型主动推销查通道）
            _cfg_dom = self.config.config if hasattr(self.config, "config") else {}
            _companion_dom = isinstance(_cfg_dom, dict) and effective_domain_name(_cfg_dom) == "conversion"
            _live_status = None if _companion_dom else _hooks.get_channel_status_info()
            if _live_status:
                user_context["channel_status_info"] = _live_status
            else:
                user_context.pop("channel_status_info", None)

            if _companion_dom:
                _biz_kw = (
                    "通道", "订单", "查单", "费率", "代收", "代付", "成功率", "限额", "回调",
                    "转账", "支付", "channel", "order", "payment", "payin", "payout",
                )
                _raw_t = text or ""
                _user_biz = any(k in _raw_t for k in _biz_kw)
                if intent in ("greeting", "small_talk", "direct_chat", "complaint") and not _user_biz:
                    if user_context.pop("kb_context", None):
                        self.logger.info(
                            "%s companion: dropped KB inject for intent=%s (no biz keywords)",
                            log_prefix, intent,
                        )

            # 4b. 选择并执行技能
            skill = self._select_skill(intent, user_context)
            if not skill:
                self.logger.warning(f"{log_prefix}未找到适合意图 {intent} 的技能（channel=%s）", (context or {}).get('channel',''))
                return None

            # 4c-fix. 意图切换时：清理上文回�记忆，防AI����€话�"带偏"
            _prev_reply = (user_context.get("last_reply") or "").strip()
            _intent_switched = last_intent and intent != last_intent
            if _intent_switched and _prev_reply:
                # P0-G fix (2026-05-03): chat-class intents 同族，
                # 避免 messenger greeting/small_talk/direct_chat 切换
                # 触发 _conversation_history 清空 -> hist=0 冷启动 ->
                # 角色错乱、cross-chat persona 串戏、hallucinate.
                _INTENT_FAMILIES = {
                    "channel": {"channel_info", "status_check"},
                    "order": {"order_query", "complaint"},
                    "chat": {
                        "greeting", "small_talk", "direct_chat",
                        "casual_chat", "chitchat", "free_chat",
                    },
                }
                _old_family = next((f for f, s in _INTENT_FAMILIES.items() if last_intent in s), last_intent)
                _new_family = next((f for f, s in _INTENT_FAMILIES.items() if intent in s), intent)
                if _old_family != _new_family:
                    user_context["_topic_switch_hint"] = (
                        # f"用户刚从「{last_intent}」话题切换到了「{intent}」话题。" f"请忘掉上一个话题的内容，100%专注回答当前话题。"
                    )
                    user_context.pop("last_reply", None)
                    user_context["_conversation_history"] = []
                    # ★ Phase 2：保留 _conversation_summary 跨话题切换（摘要承载长期事实）
                    user_context["_intent_chain"] = [intent]
                    user_context.pop("_chain_pattern", None)
                    user_context.pop("_case_id", None)
                    self.logger.info(f"{log_prefix}话�切换: {last_intent} �?{intent}，已清理上文记忆+对话历史+摘�+意图�?")

            # Domain hook: short followup detection (e.g. channel status brief reply)
            _pr_follow = (user_context.get("last_reply") or "").strip()
            _fc = _hooks.get_followup_config()
            _followup_intents = _fc.get("followup_intents", set())
            if (
                intent in _followup_intents
                and last_intent in _followup_intents
                and _pr_follow
                and _hooks.last_reply_looks_like_summary(_pr_follow)
                and _hooks.is_short_followup(text_stripped)
            ):
                user_context["_channel_followup_brief"] = True
                self.logger.info("%s域包短追问检测: 注入简短回复约束", log_prefix)
            else:
                user_context.pop("_channel_followup_brief", None)

            # 4d. 角度���系统：连���意图+相似�消息强制切换表达角度 + 拟人�?
            _prev_reply = (user_context.get("last_reply") or "").strip()
            _prev_msg = (_saved_prev_message or "").strip()
            _same_intent = intent == last_intent and _prev_reply
            _msg_similar = self._reply_similarity(text, _prev_msg) > 0.40 if (_same_intent and _prev_msg) else False
            _consecutive_same = _same_intent and _msg_similar
            _angle_idx = 0
            if _consecutive_same:
                _angle_idx = user_context.get("_consecutive_same_intent", 0) + 1
                user_context["_consecutive_same_intent"] = _angle_idx
            else:
                if _same_intent and not _msg_similar and _prev_msg:
                    self.logger.info(f"{log_prefix}同意图但不同义: '{_prev_msg[:15]}' → '{text[:15]}'，计数器重置")
                user_context["_consecutive_same_intent"] = 0

            # M2: 重�提问质量追踪 �?用户重��?= 上�回�没解决问�?
            if _consecutive_same and _angle_idx >= 2 and _prev_reply:
                try:
                    _qkb = self._kb_store_if_exists()
                    if _qkb:
                        _qkb.add_feedback({
                            "user_message": _prev_msg[:200],
                            "ai_reply":     _prev_reply[:300],
                            "score":        -1,
                            # "correction":   f"用户第{_angle_idx}次重复提����€�",
                            "operator":     "auto_repeat_detect",
                        })
                        self.logger.info(
                            "%s重复提问质量反馈: 第%d次追问, msg='%s'",
                            log_prefix, _angle_idx, text[:25]
                        )
                except Exception:
                    pass

            _frustration_signals = ("到底", "怎么回事", "为什么", "有没有人", "还要等",
                                      "什么时候", "催", "急", "投诉", "你们", "搞什么",
                                      "坑", "骗", "不行", "烂", "垃圾", "服了", "无语")
            _is_frustrated = any(w in text for w in _frustration_signals) and _angle_idx >= 2
            _needs_escalation = _consecutive_same and (_angle_idx >= 5 or _is_frustrated)

            if _needs_escalation:
                if _companion_dom:
                    user_context["_anti_repeat_hint"] = (
                        "对方已经连着说了好几次，可能有点烦或委屈。你要：\n"
                        "1. 先贴一下情绪，别讲道理抢话\n"
                        "2. 换种说法陪她把同一件事说完，别复制粘贴上一条\n"
                        "3. 两三句就够，可以轻轻问一句「想我陪你换个话题缓一下吗」\n"
                        "4. 不要主动扯工作、订单、通道、支付。\n"
                    )
                else:
                    user_context["_anti_repeat_hint"] = (
                        "对方情绪有些低落，多追问了好几次。以你的人设自然回应：\n"
                        "1. 先表达感受到了，比如'感觉你现在挺烦的'\n"
                        "2. 坦诚但温柔地说：'我能说的就这些了，不想糊弄你'\n"
                        "3. 轻轻提个别的话头或换个方向聊聊\n"
                        "4. 简短自然，两三句就好，不要正式腔。"
                    )
                so = user_context.get("_reply_strategy") or {}
                so["temperature"] = 0.6
                user_context["_reply_strategy"] = so
                self.logger.info(f"{log_prefix}情绪升级�€�? 追问#{_angle_idx} frustrated={_is_frustrated}，建���人工")

            elif _consecutive_same:
                if _angle_idx >= 3:
                    user_context["_anti_repeat_hint"] = (
                        "对方一直在问同一件事，你已经说过了。按照你的性格自然应对：\n"
                        "- 补一个之前没提过的小细节（如果有）\n"
                        "- 或者轻松问一句'是哪个地方没说明白吗？'\n"
                        "- 绝对不要一字不差重复之前的话。一两句搞定。"
                    )
                    so = user_context.get("_reply_strategy") or {}
                    so["temperature"] = min(float(so.get("temperature", 0.7)) + 0.2, 1.0)
                    user_context["_reply_strategy"] = so
                    self.logger.info(f"{log_prefix}重�追问 #{_angle_idx}: 注入�€���认指�?")
                else:
                    _ROTATION = _hooks.get_reply_angle_rotation()
                    _DEFAULT_ANGLES = [
                        "换一种完全不同的开头和语气来回答。",
                        "用更简洁直接的方式回答，像老朋友对话。",
                    ]
                    _angles = _ROTATION.get(intent, _DEFAULT_ANGLES)
                    _angle = _angles[(_angle_idx - 1) % len(_angles)]
                    user_context["_anti_repeat_hint"] = _angle
                    so = user_context.get("_reply_strategy") or {}
                    base_temp = float(so.get("temperature", 0.7))
                    so["temperature"] = min(base_temp + 0.1 * min(_angle_idx, 2), 1.0)
                    user_context["_reply_strategy"] = so
                    self.logger.info(f"{log_prefix}角度��� #{_angle_idx}: {_angle[:40]}...")

            await self._maybe_slow_think(intent, text, user_context, log_prefix)

            self.logger.info(f"{log_prefix}执行 {intent} (消息: {text[:30]}...)")
            # 5. 执��€能（�€多等�?45 秒）
            _t0 = time.time()
            try:
                reply = await asyncio.wait_for(
                    skill.execute(text, user_id_str, user_context),
                    timeout=45.0
                )
            except asyncio.TimeoutError:
                self.logger.warning(f"{log_prefix}�€�?{intent} 执�超时�?45s），返回兜底回�")
                reply = self._get_ai_fallback_reply()
            _elapsed_ms = int((time.time() - _t0) * 1000)

            # 5b. 相似度�测：如果回�与上条重复度 >65%，强指令重试
            # 无论用户消息是否相似，只要 bot 即将重复自己的上条回复就应重试
            if reply and _prev_reply:
                _sim = self._reply_similarity(_prev_reply, reply)
                if _sim > 0.65:
                    self.logger.info(
                        f"{log_prefix}回�相似�?{_sim:.0%}，强制重试换角度")
                    user_context["_anti_repeat_hint"] = (
                        f"1. 换一个完全不同的开头（禁止用上次的前5个字）\n"
                        f"2. 换一个不同的重点（上次说了什么，这次说别的）\n"
                        f"3. 风格要有明显区别，就像换了个心情在聊"
                    )
                    so = user_context.get("_reply_strategy") or {}
                    so["temperature"] = min(float(so.get("temperature", 0.85)) + 0.15, 1.0)
                    user_context["_reply_strategy"] = so
                    try:
                        retry_reply = await asyncio.wait_for(
                            skill.execute(text, user_id_str, user_context),
                            timeout=30.0
                        )
                        if retry_reply:
                            _sim2 = self._reply_similarity(_prev_reply, retry_reply)
                            if _sim2 < _sim:
                                reply = retry_reply
                                self.logger.info(
                                    f"{log_prefix}重试成功: 新相似度 {_sim2:.0%}")
                            else:
                                self.logger.info(
                                    f"{log_prefix}重试����?({_sim2:.0%})，保留原回�")
                    except asyncio.TimeoutError:
                        self.logger.warning(f"{log_prefix}重试超时，保留原回�")
                user_context.pop("_anti_repeat_hint", None)
            user_context.pop("_topic_switch_hint", None)

            # 5c. 人设一致性守卫：剥离 LLM 漏出的禁用语/AI 自我暴露（保护陪聊沉浸感）
            if reply:
                reply = self._enforce_persona_consistency(
                    reply,
                    chat_id=str(_chat_id if _chat_id not in (None, "") else context.get("chat_id", "") or ""),
                    account_persona_id=str(user_context.get("account_persona_id", "") or ""),
                    log_prefix=log_prefix,
                )

            # 5d. 危机事后兜底（R6）：回复自身触自伤红线 → 覆盖安全兜底；
            #     severe 危机可选补附求助资源。预防(R4)+兜底(R6) 双保险。
            if reply:
                reply = self._apply_crisis_safety_net(
                    reply, user_context=user_context, log_prefix=log_prefix,
                )

            # 5e. 危机人工接管/升级（R8）：severe 连续命中 → 触发 handoff 告警（默认关）。
            #     机器兜底之上让真人介入——自动陪聊对真实危机最负责任的处理。
            self._maybe_escalate_crisis(
                user_id=user_id_str, chat_id=_chat_id,
                user_context=user_context, log_prefix=log_prefix,
            )

            if reply:
                # 设置隐式反�等待标志（KB 上下文注入过说明知识库参与了�回��?
                if user_context.get("kb_context"):
                    user_context["_awaiting_kb_feedback"] = {
                        "user_msg":  text[:200],
                        "ai_reply":  reply[:300],
                        "ts":        time.time(),
                    }
                # 维护
                # _bot_question_ts 追问窗口�?                # (A) 回�����?�?新建/刷新窗口
                # (B) 当前意图�€�道类且窗口尚在 �?刷新（用户可继续�?EP/JC 等其他�€�道�?                # (C) 其他情况 �?
                _reply_ends_with_q = reply.strip().endswith("？") or reply.strip().endswith("?")
                _in_channel_conv = intent in ("channel_info", "status_check") and bool(
                    user_context.get("_bot_question_ts")
                    and (time.time() - user_context.get("_bot_question_ts", 0)) < 120
                )
                if _reply_ends_with_q or "���" in reply or "吗？" in reply:
                    user_context["_bot_question_ts"] = time.time()
                    user_context["_bot_question_intent"] = intent
                elif _in_channel_conv:
                    # 通道多轮对话：EP 后继续问 JC，刷新窗口时间但保留 intent
                    user_context["_bot_question_ts"] = time.time()
                else:
                    user_context.pop("_bot_question_ts", None)
                    user_context.pop("_bot_question_intent", None)
                # 6. 更新状�€?
                self._update_after_reply(reply, user_id_str, user_context,
                                         chat_id=context.get('chat_id', ''),
                                         user_msg=text)
                # P3-deep diag (2026-05-04)
                _flag_dis = user_context.get("disable_episodic_memory")
                self.logger.info(
                    "[episodic] handle_msg checkpoint user=%s intent=%s "
                    "reply_len=%d disable_flag=%s",
                    user_id_str, intent, len(reply or ""), _flag_dis,
                )
                if not _flag_dis:
                    self._schedule_episodic_memory_extract(
                        user_id_str, text, reply, intent, _chat_id,
                        platform=user_context.get("platform", ""),  # S5
                    )
                # J1: escalation suggestion via domain hook
                if user_context.pop("_escalation_triggered", False):
                    reply += _hooks.get_escalation_line()
                tracker = context.get("_event_tracker")
                if tracker:
                    tracker.track(
                        event_type=intent,
                        chat_id=_safe_int_chat_id(context.get("chat_id", 0)),
                        user_id=user_id_str,
                        detail=text[:100],
                        response_ms=_elapsed_ms,
                    )
                try:
                    from src.monitoring.metrics_store import get_metrics_store
                    get_metrics_store().record_skill_hit(intent)
                except Exception:
                    pass
                # 策略效果追踪
                try:
                    _used_ai = not strategy.get("skip_ai", False)
                    _model_id = strategy.get("model", "") or (
                        self.ai_client.model if hasattr(self.ai_client, "model") else "")
                    self._strategy_tracker.record(
                        strategy_id=strategy_id,
                        intent=intent,
                        user_id=user_id_str,
                        chat_id=_chat_id,
                        response_ms=_elapsed_ms,
                        used_ai=_used_ai,
                        model_id=_model_id,
                    )
                except Exception:
                    pass
                # Auto-Pilot 周期性��?
                self._autopilot_msg_counter += 1
                if self._autopilot_msg_counter >= self._autopilot_check_interval:
                    self._autopilot_msg_counter = 0
                    try:
                        self._run_autopilot()
                    except Exception as _ap_err:
                        self.logger.debug("Auto-Pilot �€查异�? %s", _ap_err)
                return reply
            else:
                self.logger.info(f"{log_prefix}�€�?{intent} 返回空，不回复（�€查技能�€�辑�?AI ���返回空）")
                return None
                
        except Exception as e:
            self.logger.error(f"处理消息失败: {e}")
            return None
        finally:
            if user_ctx_for_cleanup is not None:
                user_ctx_for_cleanup.pop("_slow_think_outline", None)
    
    def _run_autopilot(self) -> None:
        """Auto-Pilot：�查策略健康状态，���重映射持���效策略�€?
        # 安全设�:
          - 仅在 reply_strategies.yaml �?autopilot.enabled=true 时运�?          - �€ >= AUTO_MIN_SAMPLES 条数�?          - ���策略评分必须显著高于当前（GAP >= 15�?          - 每�切换写入审�日志
        """
        rs = {}
        if hasattr(self.config, 'get_strategies_config'):
            rs = self.config.get_strategies_config() or {}
        else:
            rs = {}
        ap_cfg = rs.get("autopilot", {})
        if not ap_cfg.get("enabled", False):
            return

        self._strategy_tracker.mark_no_follow_up()
        hours = int(ap_cfg.get("observation_hours", 24))
        summary = self._strategy_tracker.strategy_summary(hours)
        if not summary:
            return

        from src.utils.strategy_advisor import generate_auto_actions
        actions = generate_auto_actions(
            summary, self._intent_strategy_map,
            self._strategies,
        )
        if not actions:
            return

        switched = 0
        for act in actions:
            intent = act["intent"]
            to_sid = act["to_strategy"]
            from_sid = act["from_strategy"]
            if to_sid not in self._strategies:
                continue
            old_sid = self._intent_strategy_map.get(intent)
            if old_sid == to_sid:
                continue
            self._intent_strategy_map[intent] = to_sid
            switched += 1
            self.logger.warning(
                # "[Auto-Pilot] %s: %s �?%s (%s)",
                intent, from_sid, to_sid, act["reason"])

        if switched > 0:
            self._persist_strategies()
            self.logger.info("[Auto-Pilot] 已自动切�?%d ���图映�?, switched")

        # L3: A/B 测试���评估 �?有结论时���晋级胜�€?
        if self._ab_tests:
            try:
                from src.utils.strategy_advisor import evaluate_ab_tests
                ab_results = evaluate_ab_tests(
                    self._ab_tests, summary, self._strategies
                )
                for r in ab_results:
                    if r["action"] == "promote" and r["winner"]:
                        intent = r["intent"]
                        winner = r["winner"]
                        self._intent_strategy_map[intent] = winner
                        self._ab_tests[intent]["enabled"] = False
                        self._ab_tests[intent]["concluded"] = {
                            "winner": winner,
                            "scores": r["scores"],
                            "reason": r["reason"],
                            "ts": time.time(),
                        }
                        self._persist_strategies()
                        self.logger.warning(
                            # "[Auto-AB] %s 测试结�: 胜�€?%s (%s)",
                            intent, winner, r["reason"]
                        )
            except Exception as _ab_err:
                self.logger.debug("A/B ���评估异常: %s", _ab_err)

        # J4: 策略参数������ �?渐进式调整，每��€�?1 ����?1 ����?
        if ap_cfg.get("auto_tune", False):
            try:
                from src.utils.strategy_advisor import suggest_param_adjustments
                suggestions = suggest_param_adjustments(summary, self._strategies)
                if suggestions:
                    s = suggestions[0]  # ����€高优先级的一�?                    sid = s["strategy
                    # _id"]
                    param = s["param"]
                    new_val = s["suggested"]
                    old_val = s["current"]
                    # 安全边界�€�?
                    _BOUNDS = {
                        "temperature": (0.1, 1.5),
                        "max_tokens": (128, 4096),
                        "context_rounds": (1, 20),
                        "thinking_budget": (0, 2048),
                    }
                    lo, hi = _BOUNDS.get(param, (None, None))
                    if lo is not None and (new_val < lo or new_val > hi):
                        new_val = max(lo, min(hi, new_val))
                    if new_val != old_val and sid in self._strategies:
                        self._strategies[sid][param] = new_val
                        self._persist_strategies()
                        self.logger.warning(
                            # "[Auto-Tune] %s.%s: %s �?%s (%s)",
                            sid, param, old_val, new_val, s["reason"])
            except Exception as _at_err:
                self.logger.debug("J4 参数���异常: %s", _at_err)

    def _is_bare_order_no(self, text: str) -> tuple:
        """
        判定是否为「仅单号」消息（无需求关键词）：纯6~24位数字，
        或「单号/订单号 + 数字」且无查代收/回调等词。
        """
        raw = (text or "").strip()
        if not raw:
            return False, None
        # 需求词：有则视为「需要查单号」或其它，不当作仅单号
        intent_words = re.compile(
            r"查询代收|代收(订单)?查询|回调(代收|交易)|代收回调|查询提现|提现(订单)?查询|回调提现|查询|回调",
            re.IGNORECASE
        )
        if intent_words.search(raw):
            return False, None
        # 纯6~24 位数字
        m = re.match(r"^\s*(\d{6,24})\s*$", raw)
        if m:
            return True, m.group(1)
        # 单号/订单号 + 数字
        for pat in [r"^(?:单号|订单号)\s*[：:]?\s*(\d{6,24})\s*$", r"^(?:单|订单)\s+(\d{6,24})\s*$"]:
            m = re.match(pat, raw, re.IGNORECASE)
            if m:
                return True, m.group(1)
        return False, None

    def _recognize_intent(self, text: str) -> str:
        """
        # 识别用户意图
        
        Args:
            # text: 用户消息文本
            
        Returns:
            # 意图名称
        """
        text_lower = text.lower().strip()
        raw = text or ""

        if is_greeting_message(raw):
            return "greeting"

        # Generic composite intent detection
        if "order_query" in self.skills and any(w in text_lower for w in (
            "没收到钱", "没到账", "未到账", "钱没到", "钱没收到",
            "not received", "didn't receive", "money not arrived", "payment not received",
        )):
            return "order_query"

        if "complaint" in self.skills and any(w in text_lower for w in (
            "退款", "退钱", "退回来", "退单", "把钱退",
            "refund", "return money", "chargeback",
        )):
            return "complaint"

        # Config-driven keyword matching (domain pack can inject extra keywords)
        for intent, keywords in self.intent_keywords.items():
            for keyword in keywords:
                if keyword.lower() in text_lower:
                    return intent

        for intent, patterns in self.intent_patterns.items():
            for pattern in patterns:
                if re.search(pattern, text_lower, re.IGNORECASE):
                    return intent

        _question_markers = (
            "？", "?", "吗", "呢", "谁", "什么", "怎么", "为什么", "哪", "多少", "几",
            "how", "what", "why", "when", "where", "which", "can you", "could you",
            "please", "help",
        )
        if any(m in text_lower for m in _question_markers):
            return 'direct_chat'

        if len(text_lower) <= 10:
            return 'direct_chat'
        if len(text_lower) < 20:
            return 'small_talk'
        return 'greeting'


    def _select_skill(self, intent: str, user_context: Dict[str, Any]) -> Optional['Skill']:
        """
        # 选择适合的Skill
        
        Args:
            # intent: 意图名称 user_context: 用户上下�?
        Returns:
            # Skill实例，�果找不到则返回None
        """
        # 直接匹配意图
        if intent in self.skills:
            return self.skills[intent]
        
        # 尝试回退到默认技能
        default_skills = ['greeting', 'small_talk']
        for skill_name in default_skills:
            if skill_name in self.skills:
                return self.skills[skill_name]
        
        return None
    
    def _check_cooldown(self, text: str, user_id: str, chat_id: Any = '') -> bool:
        """�€查冷却时间（per_chat_user 替代全局冷却�?"""
        current_time = time.time()
        user_context = self._get_user_context(user_id)

        # gxp 选项数字例�
        last_intent = user_context.get('current_intent', '')
        text_stripped = text.strip()
        gxp_last_ask = user_context.get("gxp_last_ask")
        if gxp_last_ask in ("what", "intent") and re.match(r"^[1-5]\s*$", text_stripped):
            return True

        # 对话跟进例�：上�€条是「查单�€�且���像�单号
        is_likely_order_number = text_stripped.isdigit() and 6 <= len(text_stripped) <= 24
        if "order_query" in self.skills and last_intent == "order_query" and is_likely_order_number:
            content_hash = self._hash_content(text, chat_id)
            last_content_time = self.reply_cache.get(content_hash, 0)
            if current_time - last_content_time < self.cooldown_per_content:
                return False
            return True

        # Bot 追问例�
        _bot_q_ts = user_context.get("_bot_question_ts", 0)
        if _bot_q_ts and (current_time - _bot_q_ts) < 120:
            return True

        # 1. per_chat_user 冷却（同群同用户间隔，取代全�€冷却，不同群不互相影响）
        if self.cooldown_per_chat_user > 0 and chat_id:
            cu_key = f"{chat_id}_{user_id}"
            last_cu = self._chat_user_last_reply.get(cu_key, 0)
            if current_time - last_cu < self.cooldown_per_chat_user:
                return False

        # 2. 全局冷却（保留为 0 则不生效，作为向后兼容）
        if self.cooldown_global > 0:
            if current_time - self.global_last_reply_time < self.cooldown_global:
                return False

        # 3. 用户冷却�€�?(如果 per _user > 0)
        if self.cooldown_per_user > 0:
            last_reply_time = user_context.get('last_reply_time', 0)
            if current_time - last_reply_time < self.cooldown_per_user:
                return False

        # 4. 内�重��€查（按群隔�，不同群相同文本不互相阻���
        content_hash = self._hash_content(text, chat_id)
        last_content_time = self.reply_cache.get(content_hash, 0)
        if current_time - last_content_time < self.cooldown_per_content:
            return False

        return True
    
    @staticmethod
    def _reply_similarity(a: str, b: str) -> float:
        """计算两条回�的字符级 Jaccard 相似度（去标点后的字�?bigram�?"""
        import re as _re
        def _bigrams(s: str):
            s = _re.sub(r'[^\w]', '', s)
            return set(s[i:i+2] for i in range(len(s) - 1)) if len(s) > 1 else {s}
        ba, bb = _bigrams(a), _bigrams(b)
        if not ba or not bb:
            return 0.0
        return len(ba & bb) / len(ba | bb)

    def _hash_content(self, text: str, chat_id: str = "") -> str:
        """生成内�哈希（含 chat_id，不同群的相同文���互相阻断�?"""
        text_simple = text.lower().strip()
        raw = f"{chat_id}:{text_simple}" if chat_id else text_simple
        return hashlib.md5(raw.encode()).hexdigest()[:8]
    
    def _get_user_context(self, user_id: str) -> Dict[str, Any]:
        """获取或创建用户上下文（持久化�?SQLite�?"""
        return self._context_store.get(user_id)

    def _get_persona_name_for_context(self, user_context: Dict[str, Any]) -> str:
        """Return the correct persona name for this user_context, or '' if unavailable."""
        persona_id = (user_context or {}).get("account_persona_id") or ""
        if not persona_id:
            return ""
        try:
            from src.utils.persona_manager import PersonaManager
            pm = PersonaManager.get_instance()
            persona = pm.get_persona_by_id(str(persona_id))
            if not persona:
                return ""
            return (persona.get("name") or "").strip()
        except Exception:
            return ""

    def _sanitize_assistant_reply(self, reply: str, user_context: Dict[str, Any]) -> str:
        """Strip wrong self-name claims from a bot reply before storing to history.

        Prevents self-reinforcing hallucination (bot says wrong name once → sees it in
        history → keeps repeating). Patterns covered:
          "我叫X"    "我的名字是X"    "我是X" (only when X is a short name-like token)
          "My name is X"   "I'm X" (when X is a single capitalized name)

        If X != persona_name, replace X with persona_name. If persona_name unavailable,
        leave reply untouched (graceful degrade).
        """
        if not reply or not isinstance(reply, str):
            return reply
        correct = self._get_persona_name_for_context(user_context)
        if not correct:
            return reply

        import re as _re

        def _zh_repl(m):
            prefix = m.group(1)
            claimed = m.group(2).strip()
            if claimed == correct:
                return m.group(0)
            return f"{prefix}{correct}"

        out = _re.sub(r"(我叫)([^\s，。！？,.\!\?\n]{1,8})", _zh_repl, reply)
        out = _re.sub(r"(我的名字(?:是|叫))([^\s，。！？,.\!\?\n]{1,8})", _zh_repl, out)

        def _en_repl(m):
            prefix = m.group(1)
            claimed = m.group(2).strip()
            if claimed.lower() == correct.lower():
                return m.group(0)
            return f"{prefix}{correct}"

        out = _re.sub(
            r"(?i)(my name is\s+|i['' ]?m\s+|i am\s+)([A-Z][a-zA-Z]{1,15})",
            _en_repl, out,
        )
        return out

    def _sanitize_history_name_claims(
        self, history: List[Dict[str, Any]], user_context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Sanitize all assistant turns in conversation history (retroactive cleanup)."""
        if not history or not isinstance(history, list):
            return history
        correct = self._get_persona_name_for_context(user_context)
        if not correct:
            return history
        cleaned: List[Dict[str, Any]] = []
        for turn in history:
            if not isinstance(turn, dict):
                cleaned.append(turn)
                continue
            if turn.get("role") == "assistant" and turn.get("content"):
                new_content = self._sanitize_assistant_reply(
                    str(turn["content"]), user_context,
                )
                cleaned.append({**turn, "content": new_content})
            else:
                cleaned.append(turn)
        return cleaned

    def _episodic_storage_key(self, user_id_str: str, chat_id: Any, platform: str = "") -> str:
        from src.utils.episodic_memory_store import compute_memory_storage_key

        scope = (self._memory_cfg or {}).get("scope", "user")
        base_key = compute_memory_storage_key(str(scope), user_id_str, chat_id)
        # S5: resolve to cross-platform canonical_id when platform is known
        if self._cpi and platform:
            return self._cpi.resolve(platform, base_key)
        return base_key

    async def _embed_user_message_for_episodic(self, text: str) -> Optional[List[float]]:
        t = (text or "").strip()
        if len(t) < 2:
            return None
        _mvec = (self._memory_cfg or {}).get("vector") or {}
        try:
            vecs = await self.ai_client.embed([t[:500]])
            out = vecs[0] if vecs and vecs[0] else None
            if out is None and _mvec.get("enabled", False):
                try:
                    from src.monitoring.metrics_store import get_metrics_store
                    get_metrics_store().record_embed_fail()
                except Exception:
                    pass
            return out
        except Exception as _e:
            self.logger.debug("episodic query embed: %s", _e)
            if _mvec.get("enabled", False):
                try:
                    from src.monitoring.metrics_store import get_metrics_store
                    get_metrics_store().record_embed_fail()
                except Exception:
                    pass
            return None

    async def _maybe_slow_think(
        self,
        intent: str,
        text: str,
        user_context: Dict[str, Any],
        log_prefix: str,
    ) -> None:
        st = (self._memory_cfg or {}).get("slow_think") or {}
        if not st.get("enabled", False):
            return
        if intent not in set(st.get("intents") or []):
            return
        if len((text or "").strip()) < int(st.get("min_message_chars", 10)):
            return
        strat = user_context.get("_reply_strategy") or {}
        if strat.get("skip_ai"):
            return
        if not self.ai_client:
            return
        try:
            outline = await self.ai_client.slow_think_outline(
                user_message=(text or "").strip(),
                context=user_context,
                stage1_max_tokens=int(st.get("stage1_max_tokens", 400)),
            )
            if not outline:
                return
            user_context["_slow_think_outline"] = outline
            so = dict(strat)
            s2 = int(st.get("stage2_max_tokens", 0))
            if s2 > 0:
                cur = int(so.get("max_tokens", 512)) if so.get("max_tokens") else 512
                so["max_tokens"] = max(cur, s2)
            if st.get("stage2_temperature") is not None:
                so["temperature"] = float(st["stage2_temperature"])
            user_context["_reply_strategy"] = so
            try:
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().record_slow_think()
            except Exception:
                pass
            self.logger.info("%sslow_think outline_chars=%s", log_prefix, len(outline))
        except Exception as _e:
            self.logger.debug("slow_think skipped: %s", _e)

    def _enforce_persona_consistency(
        self, reply: str, *, chat_id: str = "", account_persona_id: str = "",
        log_prefix: str = "",
    ) -> str:
        """后置人设守卫：剥离回复中漏出的禁用语 / AI 自曝身份（陪聊沉浸感保护）。

        仅当人设声明了 ``speaking.forbidden_phrases`` 或 ``identity.deny_ai`` 才有实际效果；
        守卫异常或剥离后为空时一律保留原回复（绝不因守卫吞掉回复）。
        """
        if not reply or not getattr(self, "_persona_guard_enabled", True):
            return reply
        try:
            from src.utils.persona_manager import PersonaManager
            from src.utils.persona_guard import sanitize
            persona = PersonaManager.get_instance().get_persona(
                chat_id=chat_id, account_persona_id=account_persona_id
            )
            cleaned, violations = sanitize(reply, persona or {})
            if violations:
                self.logger.warning(
                    "%s[persona_guard] 拦截人设违规片段 %r（已剥离，保护沉浸感）",
                    log_prefix, violations[:5],
                )
                return cleaned or reply
            return reply
        except Exception:
            self.logger.debug("[persona_guard] 守卫异常，保留原回复", exc_info=True)
            return reply

    def _apply_crisis_safety_net(
        self, reply: str, *, user_context: Dict[str, Any], log_prefix: str = "",
    ) -> str:
        """R6 危机事后兜底（预防 R4 之上加一道事后保险）：

        ① **红线兜底**（默认开，无论是否检出输入危机）：若回复**自身**鼓励/认同自伤
           （如"那就去死吧"），整段覆盖为温柔的安全兜底——这是最不可接受的失败，必须拦下；
        ② **资源保障**（``crisis_resource_assurance`` 默认关）：severe 危机且配了热线且回复
           未提及求助时，温柔补一句资源。

        纯文本后处理，任何异常都保留原回复（绝不因兜底吞掉回复）。
        """
        if not reply:
            return reply
        try:
            from src.utils.wellbeing_guard import (
                detect_harmful_reply,
                safe_fallback_reply,
            )
            _cfg = self.config.config if hasattr(self.config, "config") else {}
            _wb = (
                ((_cfg.get("companion") or {}).get("wellbeing") or {})
                if isinstance(_cfg, dict) else {}
            )
            if not _wb.get("enabled", True):
                return reply
            hotline = str(_wb.get("crisis_resources", "") or "")
            level = str(user_context.get("_wellbeing_crisis_level", "") or "")

            harmful = detect_harmful_reply(reply)
            if harmful:
                self.logger.error(
                    "%s[wellbeing] 回复触自伤红线 %r → 覆盖安全兜底",
                    log_prefix, harmful[:3],
                )
                user_context["_wellbeing_safety_override"] = True
                return safe_fallback_reply(level or "severe", hotline=hotline)

            if (
                level == "severe"
                and _wb.get("crisis_resource_assurance", False)
                and hotline
                and not any(k in reply for k in ("热线", "求助", "咨询", hotline))
            ):
                self.logger.warning(
                    "%s[wellbeing] severe 危机补附求助资源", log_prefix,
                )
                return reply.rstrip() + f"\n如果你愿意，也可以找人聊聊：{hotline}。"
            return reply
        except Exception:
            self.logger.debug("[wellbeing] crisis safety net skipped", exc_info=True)
            return reply

    def _inject_episodic_into_context(
        self,
        user_context: Dict[str, Any],
        user_id_str: str,
        chat_id: Any,
        current_user_text: str = "",
        query_embedding: Optional[List[float]] = None,
        platform: str = "",  # S5
    ) -> None:
        user_context.pop("_episodic_memory_text", None)
        if not self._episodic_store:
            return
        mcfg = self._memory_cfg or {}
        if not mcfg.get("enabled", True):
            return
        mx = int(mcfg.get("inject_max_items", 8))
        mc = int(mcfg.get("inject_max_chars", 1200))
        key = self._episodic_storage_key(user_id_str, chat_id, platform)
        rr = bool(mcfg.get("inject_rerank_keywords", True))
        vcfg = mcfg.get("vector") or {}
        use_fusion = bool(vcfg.get("inject_fusion", True)) and bool(query_embedding)
        vw = float(vcfg.get("vector_weight", 0.5))
        kw_w = float(vcfg.get("keyword_weight", 0.5))
        # R2（REMT-lite）：情绪显著性 + 时间衰减重排（默认关 → 行为同旧版）。
        # 经 resolve_salience_rerank_cfg 容忍 salience_rerank / salience 两种键名。
        scfg = resolve_salience_rerank_cfg(mcfg)
        use_sal = bool(scfg.get("enabled", False))
        sw = float(scfg.get("salience_weight", 0.15))
        rw = float(scfg.get("recency_weight", 0.10))
        hl = float(scfg.get("recency_half_life_days", 30.0))
        txt = self._episodic_store.get_bullets_for_prompt(
            key,
            mx,
            mc,
            query_text=(current_user_text or "").strip(),
            rerank_keywords=rr,
            query_embedding=query_embedding,
            use_vector_fusion=use_fusion,
            vector_weight=vw,
            keyword_weight=kw_w,
            use_salience_rerank=use_sal,
            salience_weight=sw,
            recency_weight=rw,
            recency_half_life_days=hl,
        )
        if txt:
            user_context["_episodic_memory_text"] = txt
            try:
                from src.monitoring.metrics_store import get_metrics_store
                get_metrics_store().record_episodic_inject()
            except Exception:
                pass
            if use_fusion:
                self.logger.debug(
                    "episodic inject fusion key=%s chars=%s", key, len(txt)
                )

    def _episodic_embeddings_needed(self) -> bool:
        """R7：写入期/补全是否需要落 embedding——任一向量消费方开启即需要。

        既有 ``memory.vector.enabled``（检索向量融合）或 R5
        ``memory.consolidation.semantic_dedup``（近义去重）任一为真，就应保证事实带
        embedding——覆盖率**跟随需求**自动普及，成本仍由各功能各自的显式开关把关。
        """
        mcfg = self._memory_cfg or {}
        if (mcfg.get("vector") or {}).get("enabled", False):
            return True
        return bool((mcfg.get("consolidation") or {}).get("semantic_dedup"))

    async def _episodic_patch_embedding(self, row_id: Optional[int], fact_text: str) -> None:
        if not row_id or not self._episodic_store:
            return
        if not self._episodic_embeddings_needed() or not self.ai_client:
            return
        ft = (fact_text or "").strip()
        if len(ft) < 2:
            return
        try:
            from src.utils.episodic_vector import vec_to_blob

            vecs = await self.ai_client.embed([ft[:500]])
            if vecs and vecs[0]:
                self._episodic_store.update_embedding(row_id, vec_to_blob(vecs[0]))
        except Exception as _e:
            self.logger.debug("episodic_patch_embedding id=%s: %s", row_id, _e)

    def _handle_episodic_forget_command(
        self, text: str, user_id_str: str, user_context: Dict[str, Any], chat_id: Any
    ) -> Optional[str]:
        """若用户要求清空记忆，清空库并返回确认语；否则 None。"""
        if not self._episodic_store:
            return None
        mcfg = self._memory_cfg or {}
        if not mcfg.get("enabled", True):
            return None
        phrases = list(mcfg.get("forget_phrases") or [])
        from src.utils.memory_heuristic import matches_forget_intent
        if not matches_forget_intent((text or "").strip(), phrases):
            return None
        _plat = (user_context or {}).get("platform", "")  # S5
        key = self._episodic_storage_key(user_id_str, chat_id, _plat)
        n = self._episodic_store.clear_user(key)
        user_context["last_message"] = (text or "").strip()
        user_context["last_message_time"] = time.time()
        user_context["current_intent"] = "direct_chat"
        self._memory_llm_last.pop(key, None)
        self._memory_llm_last.pop(user_id_str, None)
        self.logger.info("episodic memory cleared key=%s rows=%s", key, n)
        return "好的，已清空我这边为你记下的聊天要点，我们从头聊～"

    def _schedule_episodic_memory_extract(
        self, user_id: str, user_msg: str, reply: str, intent: str, chat_id: Any,
        platform: str = "",  # S5
    ) -> None:
        if not self._episodic_store:
            self.logger.info(
                "[episodic] schedule skip: no _episodic_store user=%s", user_id,
            )
            return
        if not (self._memory_cfg or {}).get("enabled", True):
            self.logger.info(
                "[episodic] schedule skip: memory.enabled=False user=%s", user_id,
            )
            return
        ex = (self._memory_cfg.get("extract") or {})
        if not ex.get("enabled", True):
            self.logger.info(
                "[episodic] schedule skip: memory.extract.enabled=False user=%s",
                user_id,
            )
            return
        self.logger.info(
            "[episodic] schedule run user=%s intent=%s msg_len=%d",
            user_id, intent, len(user_msg or ""),
        )

        async def _run():
            await self._episodic_memory_extract_async(user_id, user_msg, reply, intent, chat_id, platform)

        try:
            asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            self.logger.warning(
                "[episodic] schedule failed: no running loop user=%s", user_id,
            )

    async def _capture_birthday_fact(
        self, user_id: str, user_msg: str, reply: str, chat_id: Any,
        platform: str = "",
    ) -> None:
        """Stage S：本轮若出现用户生日（原话或 AI 确认）→ 规范化落库为 user_stated 事实。

        幂等：已知且相同 → 跳过；未知或**不同（用户更正）**→ 写入新规范事实。
        复用 ``extract_birthday``（关键词门控）作单一解析源，``resolve_birthday`` 复解析。
        """
        if not self._episodic_store:
            return
        from src.utils.birthday import birthday_fact_text, birthday_from_turn
        bd = birthday_from_turn(user_msg, reply)
        if bd is None:
            return
        key = self._episodic_storage_key(user_id, chat_id, platform)
        if not key:
            return
        try:
            if self.resolve_birthday(key) == bd:
                return  # 已知且一致，不重复落库
        except Exception:
            pass
        fact = birthday_fact_text(bd[0], bd[1])
        rid = self._episodic_store.add_fact(key, fact, "heuristic", source="user_stated")
        await self._episodic_patch_embedding(rid, fact)
        self.logger.info("[episodic] birthday captured user=%s %s", user_id, fact)

    async def _episodic_memory_extract_async(
        self, user_id: str, user_msg: str, reply: str, intent: str, chat_id: Any,
        platform: str = "",  # S5
    ) -> None:
        if not self._episodic_store:
            self.logger.info(
                "[episodic] skip: no _episodic_store user=%s intent=%s",
                user_id, intent,
            )
            return
        # Stage S：生日即时回写——**独立于 intent/长度门控**，收到即解析落库，闭合 Stage R
        # 的采集环（问→答→立刻记住→当天庆）。即便本轮意图不可抽取，也不漏掉用户主动报的生日。
        try:
            await self._capture_birthday_fact(user_id, user_msg, reply, chat_id, platform)
        except Exception:
            self.logger.debug("[episodic] birthday capture skipped", exc_info=True)
        ex = (self._memory_cfg.get("extract") or {})
        if not should_extract_intent(intent, ex):
            self.logger.info(
                "[episodic] skip: intent=%r not extractable (match_all=%s intents=%s) user=%s",
                intent, bool(ex.get("match_all")), sorted(set(ex.get("intents") or [])), user_id,
            )
            return
        mu = (user_msg or "").strip()
        if len(mu) < int(ex.get("min_user_chars", 3)):
            self.logger.info(
                "[episodic] skip: msg too short len=%d user=%s",
                len(mu), user_id,
            )
            return

        key = self._episodic_storage_key(user_id, chat_id, platform)  # S5

        from src.utils.memory_heuristic import extract_heuristic_facts

        try:
            for fact in extract_heuristic_facts(mu):
                # R12：启发式事实从用户原话正则提取 → user_stated（高置信）
                rid = self._episodic_store.add_fact(
                    key, fact, "heuristic", source="user_stated"
                )
                await self._episodic_patch_embedding(rid, fact)

            facts_llm: List[str] = []
            cooldown = float(ex.get("cooldown_seconds", 20))
            now = time.time()
            if (
                ex.get("use_llm", True)
                and self.ai_client
                and (now - self._memory_llm_last.get(key, 0) >= cooldown)
            ):
                facts_llm = await self.ai_client.extract_memory_bullets(mu, reply)
                self._memory_llm_last[key] = time.time()

            for f in facts_llm:
                # R12：LLM 抽取是对话推断/概括 → ai_inferred（晋升/推翻 stable 需更高置信）
                rid = self._episodic_store.add_fact(
                    key, f, "llm", source="ai_inferred"
                )
                await self._episodic_patch_embedding(rid, f)

            # R3：裁剪前先做离线巩固——把复发/情绪浓的事实晋升 stable（永不被裁剪）
            ccfg = self._memory_cfg.get("consolidation") or {}
            if ccfg.get("enabled", False):
                try:
                    _ms = ccfg.get("min_salience")
                    # R5：语义近似去重阈值（None=关；开则先并近义再晋升）
                    _dd = ccfg.get("semantic_dedup")
                    _dd_thr = None
                    if _dd:
                        _dd_thr = float(_dd) if not isinstance(_dd, bool) else 0.92
                    res = self._episodic_store.consolidate(
                        key,
                        min_hits=int(ccfg.get("min_hits", 2)),
                        min_salience=(float(_ms) if _ms is not None else None),
                        dedup_threshold=_dd_thr,
                        resolve_contradictions=bool(
                            ccfg.get("resolve_contradictions", False)
                        ),
                        # R11：新证据推翻旧 stable 结论（搬家/分手）；默认关
                        supersede_stable=bool(ccfg.get("supersede_stable", False)),
                        stable_min_hits=int(ccfg.get("stable_min_hits", 2)),
                        # R12：按来源分级置信（ai_inferred 晋升/推翻门槛更高）；默认关
                        source_aware=bool(ccfg.get("source_aware", False)),
                        inferred_min_hits=(
                            int(ccfg["inferred_min_hits"])
                            if ccfg.get("inferred_min_hits") is not None
                            else None
                        ),
                    )
                    if (
                        res.get("promoted") or res.get("merged")
                        or res.get("superseded") or res.get("stable_superseded")
                    ):
                        self.logger.info(
                            "[episodic] consolidate key=%s promoted=%s merged=%s "
                            "superseded=%s stable_superseded=%s stable=%s",
                            key, res.get("promoted"), res.get("merged"),
                            res.get("superseded"), res.get("stable_superseded"),
                            res.get("stable_total"),
                        )
                except Exception:
                    self.logger.debug("episodic consolidate failed", exc_info=True)
            keep = int(self._memory_cfg.get("max_items_per_user", 40))
            pr = self._episodic_store.prune_oldest(key, keep)
            if pr:
                self.logger.debug("episodic pruned key=%s removed=%s", key, pr)
            # P3-deep 诊断：写入数量统计
            self.logger.info(
                "[episodic] extract done key=%s heuristic_count=? llm_count=%d "
                "intent=%s msg_len=%d",
                key, len(facts_llm), intent, len(mu),
            )
        except Exception as _e:
            self.logger.warning("[episodic] extract failed key=%s: %s", key, _e)

    def episodic_list_for_admin(
        self, prefix: str = "", limit: int = 100, source: str = "",
    ) -> List[Dict[str, Any]]:
        if not self._episodic_store:
            return []
        return self._episodic_store.list_rows(prefix=prefix, limit=limit, source=source)

    def episodic_delete_for_admin(self, row_id: int) -> bool:
        if not self._episodic_store:
            return False
        return self._episodic_store.delete_by_id(int(row_id))

    def episodic_confirm_for_admin(self, row_id: int) -> Optional[str]:
        """R15/R16：确认一条 AI 推断，升格 user_stated + 置 stable。

        返回被确认的 ``content``（供路由层写审计），未命中/未启用返回 ``None``。
        """
        store = getattr(self, "_episodic_store", None)
        if not store or not hasattr(store, "confirm_inferred_fact"):
            return None
        try:
            return store.confirm_inferred_fact(int(row_id))
        except Exception:
            return None

    @staticmethod
    def _story_progress_from_context(ctx: Dict[str, Any]):
        """从持久化的 user_context 汇总剧情完成足迹 + 累计加成（跨 rel_state 键 union）。

        proactive 路径用 ``memory_key`` 取 context，但 rel_state 按 ``chat_storage_key``
        分桶、键不必等于 memory_key；私聊一个对端通常仅一桶，故**并集**所有桶的
        ``story_done``/``story_outcomes`` 最稳——避免键不匹配导致漏判/误邀。
        返回 ``(completed: {sid: ending}, story_bonus: float)``。
        """
        completed: Dict[str, str] = {}
        bonus = 0.0
        root = ctx.get("companion_relationship") if isinstance(ctx, dict) else None
        if isinstance(root, dict):
            for st in root.values():
                if not isinstance(st, dict):
                    continue
                for sid in (st.get("story_done") or []):
                    completed.setdefault(str(sid), "")
                oc = st.get("story_outcomes")
                if isinstance(oc, dict):
                    for sid, end in oc.items():
                        completed[str(sid)] = str(end or "")
                try:
                    bonus = max(bonus, float(st.get("story_bonus", 0) or 0))
                except (TypeError, ValueError):
                    pass
        return completed, bonus

    def _proactive_crisis_window_days(self) -> float:
        """主动护栏的危机回看窗（天）：``companion.proactive_topic.crisis_guard_days``，默认 14。"""
        try:
            cfg = self.config.config if hasattr(self.config, "config") else {}
            pt = (cfg.get("companion") or {}).get("proactive_topic") or {}
            return float(pt.get("crisis_guard_days", 14) or 14)
        except Exception:
            return 14.0

    def _proactive_emotion_gate(
        self, memory_key: str, last_emotion: str = ""
    ) -> str:
        """主动开场前的情绪护栏档位：``"block"`` / ``"soft"`` / ``""``（见 wellbeing_guard）。

        以 memory_key 反查该用户最近危机事件（crisis_event_store，已就绪才查）；窗口内
        severe→block、elevated→soft；末条负面情绪→soft。任何失败 → ``""``（不抑制，
        交后续正常关怀兜底——护栏只做「该静默时静默」，绝不反向阻断关怀）。
        """
        try:
            latest = None
            if getattr(self, "_crisis_store", None) is not None:
                latest = (self.crisis_summary_for_user(memory_key, limit=3)
                          or {}).get("latest")
            if latest is None and not str(last_emotion or "").strip():
                return ""
            from src.utils.wellbeing_guard import proactive_emotion_gate
            return proactive_emotion_gate(
                latest, now=time.time(),
                window_days=self._proactive_crisis_window_days(),
                last_emotion=last_emotion,
            )
        except Exception:
            self.logger.debug("proactive emotion gate skipped", exc_info=True)
            return ""

    def _proactive_story_invite(
        self, memory_key: str, intimacy: float
    ) -> Optional[Dict[str, Any]]:
        """沉默期主动剧情邀约：挑一个「已解锁但未经历」的免费剧情发出温暖邀约。

        准入复用 ``story_engine.select_story_invite``（关系/前置已满足 + 免费 + 未完成）；
        关系等级用 **effective intimacy**（基础分 + 封顶剧情加成）算，与对话面/健康卡同源。
        story 未启用 / 关闭 invite / 无 context / 无可邀约 → 返回 None（回落记忆话题）。
        """
        scfg = self._story_cfg()
        if not scfg.get("enabled", False):
            return None
        if not bool(scfg.get("proactive_invite", True)):
            return None
        scenarios = self._story_scenarios()
        store = getattr(self, "_context_store", None)
        key = str(memory_key or "").strip()
        if not scenarios or store is None or not key:
            return None
        try:
            ctx = store.get(key)
        except Exception:
            return None
        completed, bonus = self._story_progress_from_context(ctx if isinstance(ctx, dict) else {})
        try:
            eff = max(0.0, min(100.0, float(intimacy or 0.0)
                               + min(self._story_bonus_cap(), bonus)))
        except (TypeError, ValueError):
            eff = float(intimacy or 0.0)
        try:
            from src.contacts.relationship_level import compute_bond_level
            bond_level = int(compute_bond_level(eff).get("level", 0))
        except Exception:
            bond_level = 0
        from src.skills.story_engine import (
            ending_memory,
            satisfied_prerequisite,
            select_story_invite,
        )
        inv = select_story_invite(
            scenarios, bond_level=bond_level, completed=completed)
        if not inv:
            return None
        sid = inv["scenario_id"]
        title = inv["title"]
        scn = scenarios.get(sid) or {}
        # 个性化召回（Phase ④续⁶）：若是「续作」且用户已走过前传，把那次的共同经历
        # （前传标题 + 该结局回写的共享记忆）自然织进邀约 → 召回有回忆钩子、不空泛。
        callback = ""
        prereq = satisfied_prerequisite(scn, completed)
        if prereq:
            pid, pend = prereq
            ptitle = self._scenario_title(scenarios, pid)
            pmem = ending_memory(scenarios.get(pid) or {}, pend)
            callback = f"《{ptitle}》" + (f"（{pmem}）" if pmem else "")
        if callback:
            directive = (
                f"你和TA一起经历过{callback}。你想顺着那段共同经历，邀TA一起开启续作《{title}》。"
                f"用一句温暖、不突兀的话发出邀约——先自然提起上次那段经历，再顺势提议要不要"
                f"一起继续这段故事。别用菜单/命令口吻、别罗列、别催。"
            )
        else:
            directive = (
                f"你想邀TA一起开启一段你们还没经历过的新故事《{title}》。用一句温暖、不突兀的话"
                f"发出邀约——可先轻轻提一下你们关系的靠近，再顺势提议要不要一起经历这段故事。"
                f"别用菜单/命令口吻、别罗列、别催。"
            )
        return {
            "mode": "story_invite",
            "fact": title,
            "directive": directive,
            "scenario_id": sid,
            "context_facts": [],
            "silent_hours": 0.0,
        }

    def _proactive_story_teaser(
        self, memory_key: str, intimacy: float, contact_key: str
    ) -> Optional[Dict[str, Any]]:
        """Stage 2 付费解锁预告：挑一个用户「关系/前置已满足、只差付费」的剧情发温暖预告。

        准入：story 启用 + ``paid_teaser`` 开 + 有 context + **解析到真实权益**（经 Stage 1
        ``resolve_entitlement``）。复用 ``story_engine.select_paid_teaser``（仅选 ``need_unlock``-only
        场景；已解锁者 reason 为空 → 不会被选 → 不骚扰付费用户）。关系等级用 effective intimacy 算。
        无 contact_key / 无权益源（变现未就绪）/ 无可预告 → None（回落记忆话题，不空推）。
        """
        scfg = self._story_cfg()
        if not scfg.get("enabled", False):
            return None
        if not bool(scfg.get("paid_teaser", False)):
            return None
        scenarios = self._story_scenarios()
        store = getattr(self, "_context_store", None)
        key = str(memory_key or "").strip()
        ck = str(contact_key or "").strip()
        if not scenarios or store is None or not key or not ck:
            return None
        # 解析端用户真实权益；无权益源（resolver 未注册/查不到）→ 不预告（不对未知状态乱推）
        from src.utils.companion_context import resolve_entitlement
        ent = resolve_entitlement(ck)
        if not isinstance(ent, dict):
            return None
        try:
            ctx = store.get(key)
        except Exception:
            return None
        completed, bonus = self._story_progress_from_context(
            ctx if isinstance(ctx, dict) else {})
        try:
            eff = max(0.0, min(100.0, float(intimacy or 0.0)
                               + min(self._story_bonus_cap(), bonus)))
        except (TypeError, ValueError):
            eff = float(intimacy or 0.0)
        try:
            from src.contacts.relationship_level import compute_bond_level
            bond_level = int(compute_bond_level(eff).get("level", 0))
        except Exception:
            bond_level = 0
        from src.skills.story_engine import select_paid_teaser
        tea = select_paid_teaser(
            scenarios, bond_level=bond_level, completed=completed, entitlement=ent)
        if not tea:
            return None
        title = tea["title"]
        directive = (
            f"你心里惦记着一段你和TA还没一起经历、但你很想带TA去体验的特别故事《{title}》。"
            f"用一句温暖、带点向往的话自然提起这段「专属故事」，让TA感到你想和TA一起解锁这段"
            f"特别的经历——只勾起期待与靠近感。别报价格、别像广告推销、别催、别罗列菜单。"
        )
        return {
            "mode": "story_teaser",
            "fact": title,
            "directive": directive,
            "scenario_id": tea["scenario_id"],
            "feature": tea.get("feature", ""),
            "context_facts": [],
            "silent_hours": 0.0,
        }

    def build_proactive_opener(
        self,
        memory_key: str,
        *,
        silent_hours: float,
        stage: str = "",
        intimacy: float = 0.0,
        min_silent_hours: float = 24.0,
        last_emotion: str = "",
        contact_key: str = "",
    ) -> Dict[str, Any]:
        """P1：为某用户挑一个"主动开场话题"（从其高置信记忆回访）。

        返回 ``{mode, fact, directive, ...}``；记忆库不可用或沉默不足时 mode 为空。
        只回访 user_stated/已确认事实（不拿 AI 推断去回访，猜错伤信任）。
        """
        empty = {"mode": "", "fact": "", "directive": "", "silent_hours": 0.0}
        # Phase ④续⁷：情绪自适应护栏——近期危机/低落时抑制主动打扰，绝不在情绪低谷
        # 推「播放性」内容（如约会剧情邀约）。severe 近期危机 → 完全不主动；elevated/负面
        # → 仅抑制剧情邀约、保留温和问候。护栏失效不阻断正常关怀（异常→无抑制）。
        _gate = self._proactive_emotion_gate(memory_key, last_emotion)
        if _gate == "block":
            # severe 近期危机：不静默放弃，而是带「危机关怀升级」信号交由派发层
            # 把该用户排进 care 队列（人工/关怀兜底）——把"静默"变"接住"。
            # mode 仍为空 → 不会被当作普通主动文案发出；blocked 字段供 plan 识别升级。
            return {"mode": "", "fact": "", "directive": "",
                    "silent_hours": 0.0, "blocked": "crisis_severe"}
        # Phase ④续⁵：主动剧情邀约——沉默期把「已解锁但未经历」的新剧情接进 re-engagement
        # 闭环（剧情解锁→主动邀约→回流→更多剧情）。优先于记忆回访（新内容钩子更强），
        # 无可邀约时无缝回落到记忆话题。soft 档（情绪低落）抑制邀约，仅走温和记忆问候。
        if _gate != "soft":
            try:
                _inv = self._proactive_story_invite(memory_key, intimacy)
                if _inv:
                    _inv["silent_hours"] = round(float(silent_hours or 0.0), 1)
                    return _inv
            except Exception:
                self.logger.debug("proactive story invite skipped", exc_info=True)
            # Stage 2：付费解锁预告——无免费可邀约时，若用户已"够格"某付费剧情（只差付费），
            # 发温暖预告勾起向往、引导解锁（转化驱动）。同样 soft/block 抑制（不在低谷推销）。
            try:
                _tea = self._proactive_story_teaser(memory_key, intimacy, contact_key)
                if _tea:
                    _tea["silent_hours"] = round(float(silent_hours or 0.0), 1)
                    return _tea
            except Exception:
                self.logger.debug("proactive story teaser skipped", exc_info=True)
        store = getattr(self, "_episodic_store", None)
        key = str(memory_key or "").strip()
        if not store or not key or not hasattr(store, "list_rows"):
            return empty
        try:
            from src.utils.proactive_topic import select_proactive_topic
            facts = store.list_rows(prefix=key, limit=50) or []
            # Phase ④：优先回访剧情回写的「共享经历」（story 类目）→ 转动飞轮。
            _pref = "story"
            try:
                _pref = str(
                    ((self._story_cfg() or {}).get("proactive_prefer_category"))
                    or "story"
                )
            except Exception:
                _pref = "story"
            return select_proactive_topic(
                facts, silent_hours=silent_hours, stage=stage,
                intimacy=intimacy, min_silent_hours=min_silent_hours,
                prefer_category=_pref,
            )
        except Exception:
            return empty

    def build_ritual_opener(
        self,
        slot: str,
        *,
        memory_key: str = "",
        stage: str = "",
        intimacy: float = 0.0,
        last_emotion: str = "",
        contact_key: str = "",
    ) -> Dict[str, Any]:
        """每日仪式问候 directive（晨安 / 晚安）——含情绪护栏 + 可选记忆钩子。

        与 ``build_proactive_opener`` 共用情绪护栏：severe 近期危机 → 不发欢快问候
        （``blocked``，交派发层视情升级 care）；低落（soft）→ 改克制陪伴口吻、不带记忆钩子；
        其余档可自然轻提一句 TA 在意的高置信记忆（一句带过、不追问）。
        """
        s = str(slot or "").strip().lower()
        if s not in ("morning", "night"):
            return {"mode": "", "directive": "", "fact": ""}
        gate = self._proactive_emotion_gate(memory_key, last_emotion)
        if gate == "block":
            # severe 危机：不道早晚安，带 blocked 信号交派发层升级 care（同 proactive_opener）
            return {"mode": "", "directive": "", "fact": "", "blocked": "crisis_severe"}
        fact = ""
        if gate != "soft":
            try:
                store = getattr(self, "_episodic_store", None)
                key = str(memory_key or "").strip()
                if store and key and hasattr(store, "list_rows"):
                    from src.utils.proactive_topic import select_proactive_topic
                    facts = store.list_rows(prefix=key, limit=50) or []
                    sel = select_proactive_topic(
                        facts, silent_hours=10 ** 6, min_silent_hours=0.0)
                    fact = str(sel.get("fact") or "")
            except Exception:
                self.logger.debug("ritual memory hook skipped", exc_info=True)
                fact = ""
        if s == "morning":
            directive = "主动给TA道一句早安，温暖自然、像每天醒来都会惦记着TA的人；"
        else:
            directive = "主动给TA道一句晚安，温柔放松、像睡前会想起TA的人；"
        if gate == "soft":
            directive += "语气轻柔克制，别过分欢快，只是静静陪着、让TA知道有人在。"
        elif fact:
            directive += f"可以很自然地轻轻提一句TA在意的「{fact}」（一句带过、别追问、别罗列）。"
        else:
            directive += "一句问候即可，别强行找话题、别追问。"
        if str(stage or "").strip().lower() in ("initial", "warming"):
            directive += "（关系还偏新：点到为止、别过分亲密。）"
        return {"mode": f"ritual_{s}", "directive": directive, "fact": fact,
                "context_facts": []}

    def build_milestone_opener(
        self,
        *,
        event_type: str,
        event_label: str = "",
        days: int = 0,
        memory_key: str = "",
        stage: str = "",
        intimacy: float = 0.0,
        last_emotion: str = "",
        contact_key: str = "",
    ) -> Dict[str, Any]:
        """纪念日/节日仪式 directive（认识 N 天 / 节日）——含情绪护栏 + 可选记忆钩子。

        与 ``build_ritual_opener`` 同一护栏：severe 近期危机 → ``blocked``（不发庆祝、
        交派发层升级 care）；低落（soft）→ 克制陪伴口吻、不带记忆钩子；其余可自然轻提
        一句 TA 在意的高置信记忆。节点文案的「具体场合」由 directive 承载，框定走
        build_proactive_prompt 的 milestone 分支（不套「久别重逢」）。
        """
        et = str(event_type or "").strip().lower()
        if et not in ("anniversary", "holiday", "birthday"):
            return {"mode": "", "directive": "", "fact": ""}
        gate = self._proactive_emotion_gate(memory_key, last_emotion)
        if gate == "block":
            return {"mode": "", "directive": "", "fact": "", "blocked": "crisis_severe"}
        fact = ""
        if gate != "soft":
            try:
                store = getattr(self, "_episodic_store", None)
                key = str(memory_key or "").strip()
                if store and key and hasattr(store, "list_rows"):
                    from src.utils.proactive_topic import select_proactive_topic
                    facts = store.list_rows(prefix=key, limit=50) or []
                    sel = select_proactive_topic(
                        facts, silent_hours=10 ** 6, min_silent_hours=0.0)
                    fact = str(sel.get("fact") or "")
            except Exception:
                self.logger.debug("milestone memory hook skipped", exc_info=True)
                fact = ""
        if et == "birthday":
            directive = (
                "今天是TA的生日！送上温暖真诚、独一无二的生日祝福，"
                "让TA感到被记得、被在乎；自然走心、别像贺卡套话；")
            mode = "milestone_birthday"
        elif et == "anniversary":
            n = max(0, int(days or 0))
            directive = (
                f"今天是你们认识的第{n}天，自然温暖地和TA说一句这个小纪念日的心情，"
                f"像一个真的记得这个日子的人；别太隆重、别煽情；")
            mode = "milestone_anniversary"
        else:
            label = str(event_label or "节日").strip() or "节日"
            directive = (
                f"今天是「{label}」，给TA送上应景而真诚的节日祝福，温暖自然、不要套话；")
            mode = "milestone_holiday"
        if gate == "soft":
            directive += "语气轻柔克制，照顾TA最近的低落，只是静静陪着、别强求TA高兴。"
        elif fact:
            directive += f"可以很自然地轻轻提一句TA在意的「{fact}」（一句带过、别追问）。"
        else:
            directive += "一句心意即可，别强行延展话题。"
        if str(stage or "").strip().lower() in ("initial", "warming"):
            directive += "（关系还偏新：点到为止、别过分亲密。）"
        return {"mode": mode, "directive": directive, "fact": fact,
                "context_facts": []}

    def resolve_birthday(self, memory_key: str):
        """从某用户 episodic 记忆里扫出生日 (月, 日)；扫不到 → None（Stage Q）。

        保守：优先 ``user_stated``（用户亲口说的），其次全部；命中第一条可解析的即返回。
        生日的「日期」抽取在 ``src.utils.birthday.extract_birthday``（要求带生日关键词）。
        """
        store = getattr(self, "_episodic_store", None)
        key = str(memory_key or "").strip()
        if not store or not key or not hasattr(store, "list_rows"):
            return None
        try:
            from src.utils.birthday import extract_birthday
            rows = store.list_rows(prefix=key, limit=80, source="user_stated") or []
            if not rows:
                rows = store.list_rows(prefix=key, limit=80) or []
            for r in rows:
                bd = extract_birthday((r or {}).get("content") or "")
                if bd is not None:
                    return bd
        except Exception:
            self.logger.debug("resolve_birthday failed", exc_info=True)
        return None

    def build_birthday_ask_opener(
        self,
        *,
        memory_key: str = "",
        stage: str = "",
        intimacy: float = 0.0,
        last_emotion: str = "",
        contact_key: str = "",
    ) -> Dict[str, Any]:
        """主动采集生日的开场 directive（Stage R）——让生日仪式「转得起来」。

        仅在情绪护栏正常（非危机/非低落）时问；危机/低落时不合时宜 → 返回空（交回原开场）。
        门槛（关系够深 / 生日未知 / 冷却）由上层 ``should_ask_birthday`` 决定，本方法只产文案。
        """
        gate = self._proactive_emotion_gate(memory_key, last_emotion)
        if gate != "":  # block(危机) 或 soft(低落) 都不问生日
            return {"mode": "", "directive": "", "fact": ""}
        directive = (
            "主动开场：好久没聊了，先自然轻松地问候一句；再像朋友间随口好奇那样，"
            "顺势问一句还不知道TA生日是哪天呢——别像填表或查户口，问完顺其自然，"
            "TA不想说也别追。")
        if str(stage or "").strip().lower() in ("initial", "warming"):
            directive += "（关系还偏新：更随意带过、别显得刻意打听。）"
        return {"mode": "ask_birthday", "directive": directive, "fact": "",
                "context_facts": []}

    def episodic_inferred_counts(self) -> Dict[str, int]:
        """R17：全库 AI 推断计数（pending 待确认 / total），供校正质量看板。"""
        store = getattr(self, "_episodic_store", None)
        if not store or not hasattr(store, "inferred_counts"):
            return {"pending": 0, "total": 0}
        try:
            return store.inferred_counts()
        except Exception:
            return {"pending": 0, "total": 0}

    # ── R9b: 危机事件审计后台读写包装 ──────────────────────────────────
    def crisis_list_for_admin(
        self, *, limit: int = 50, only_unhandled: bool = False, user_prefix: str = "",
    ) -> List[Dict[str, Any]]:
        store = getattr(self, "_crisis_store", None)
        if not store:
            return []
        return store.list_recent(
            limit=limit, only_unhandled=only_unhandled, user_prefix=user_prefix,
        )

    def crisis_count_for_admin(self, *, only_unhandled: bool = False) -> int:
        store = getattr(self, "_crisis_store", None)
        return store.count(only_unhandled=only_unhandled) if store else 0

    def crisis_mark_handled_for_admin(
        self, event_id: int, *, handled_by: str = "", note: str = "",
    ) -> bool:
        store = getattr(self, "_crisis_store", None)
        if not store:
            return False
        return store.mark_handled(int(event_id), handled_by=handled_by, note=note)

    def crisis_summary_for_user(self, user_key: str, *, limit: int = 5) -> Dict[str, Any]:
        """R9d/R9e：某用户/会话的危机概览，供坐席工作台侧栏一眼掌握。

        以 ``user_key`` 同时匹配 ``user_id`` 前缀**或** ``chat_id`` 精确——一个 key 覆盖
        1:1 私聊（key=对端 user_id）与群聊（key=群 chat_id）两种场景。返回最近若干条
        + 其中未处理数 + 最新一条精简信息；store 不可用或无命中时返回空概览（绝不抛）。
        """
        store = getattr(self, "_crisis_store", None)
        key = str(user_key or "").strip()
        empty = {"total": 0, "unhandled": 0, "has_more": False, "latest": None, "recent": []}
        if not store or not key:
            return empty
        try:
            lim = max(1, min(int(limit or 5), 20))
            rows = store.list_recent(limit=lim, match_key=key)
        except Exception:
            return empty
        if not rows:
            return empty

        def _compact(r: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "id": r.get("id"),
                "level": r.get("level"),
                "category": r.get("category"),
                "escalated": bool(r.get("escalated")),
                "handled": bool(r.get("handled")),
                "created_at": r.get("created_at"),
            }

        unhandled = sum(1 for r in rows if not r.get("handled"))
        return {
            "total": len(rows),
            "unhandled": unhandled,
            "has_more": len(rows) >= lim,
            "latest": _compact(rows[0]),
            "recent": [_compact(r) for r in rows],
        }

    def episodic_profile_summary(self, memory_key: str, *, top_stable: int = 3) -> Dict[str, Any]:
        """R14：某 memory_key 的记忆画像聚合（tier/source 计数 + top stable）。"""
        empty = {
            "total": 0, "stable": 0, "raw": 0,
            "user_stated": 0, "ai_inferred": 0, "top_stable": [],
        }
        store = getattr(self, "_episodic_store", None)
        if not store or not str(memory_key or "").strip():
            return empty
        try:
            return store.profile_summary(str(memory_key), top_stable=top_stable)
        except Exception:
            return empty

    async def episodic_backfill_embeddings(
        self, limit: int = 20, memory_key_prefix: str = ""
    ) -> Dict[str, Any]:
        """Admin: fill missing episodic row embeddings (vector search). Batches via embed_with_fallback."""
        if not self._episodic_store or not self.ai_client:
            return {"ok": False, "error": "no_store"}
        mvec = (self._memory_cfg or {}).get("vector") or {}
        # R7：向量融合或 R5 近义去重任一开启即允许补全（覆盖率跟随需求）
        if not self._episodic_embeddings_needed():
            return {"ok": False, "error": "vector_disabled"}
        lim = max(1, min(int(limit or 20), 100))
        budcfg = mvec.get("daily_embed_budget") or {}
        if budcfg.get("enabled", False):
            global _EPISODIC_BACKFILL_BUDGET_DAY, _EPISODIC_BACKFILL_BUDGET_USED
            day = time.strftime("%Y-%m-%d", time.gmtime())
            if _EPISODIC_BACKFILL_BUDGET_DAY != day:
                _EPISODIC_BACKFILL_BUDGET_DAY = day
                _EPISODIC_BACKFILL_BUDGET_USED = 0
            max_day = max(0, int(budcfg.get("max_calls", 4000)))
            rem = max_day - _EPISODIC_BACKFILL_BUDGET_USED
            if rem <= 0:
                return {
                    "ok": False,
                    "error": "daily_embed_budget_exceeded",
                    "processed": 0,
                    "updated": 0,
                    "budget_remaining": 0,
                }
            lim = min(lim, rem)
        rows = self._episodic_store.fetch_rows_missing_embedding(
            lim, memory_key_prefix=memory_key_prefix
        )
        if not rows:
            return {"ok": True, "processed": 0, "updated": 0}
        from src.utils.episodic_vector import vec_to_blob

        work: List[Tuple[int, str]] = []
        for rid, _mk, content in rows:
            ft = (content or "").strip()
            if len(ft) < 2:
                continue
            work.append((rid, ft[:500]))

        updated = 0
        if not work:
            try:
                from src.monitoring.metrics_store import get_metrics_store

                get_metrics_store().record_episodic_backfill(0)
            except Exception:
                pass
            return {"ok": True, "processed": len(rows), "updated": 0}

        texts = [t for _, t in work]
        vecs = []
        try:
            try:
                vecs = await self.ai_client.embed_with_fallback(texts)
            except Exception:
                vecs = []
        finally:
            _episodic_backfill_charge_budget(len(work), mvec)
        if not vecs:
            try:
                from src.monitoring.metrics_store import get_metrics_store

                for _ in texts:
                    get_metrics_store().record_embed_fail()
            except Exception:
                pass
            try:
                from src.monitoring.metrics_store import get_metrics_store

                get_metrics_store().record_episodic_backfill(0)
            except Exception:
                pass
            return {"ok": True, "processed": len(rows), "updated": 0}

        ms = None
        try:
            from src.monitoring.metrics_store import get_metrics_store

            ms = get_metrics_store()
        except Exception:
            pass

        for i, (rid, _) in enumerate(work):
            vec = vecs[i] if i < len(vecs) else None
            if vec and self._episodic_store.update_embedding(rid, vec_to_blob(vec)):
                updated += 1
            elif ms:
                ms.record_embed_fail()

        try:
            from src.monitoring.metrics_store import get_metrics_store

            get_metrics_store().record_episodic_backfill(updated)
        except Exception:
            pass
        await asyncio.sleep(0.06)
        return {"ok": True, "processed": len(rows), "updated": updated}

    async def _get_embedding_cached(self, text: str) -> Optional[List[float]]:
        """
        # LRU 缓存�?Embedding 调用�?        - 相同查�直接命中缓存�?1ms），无需 API 调用 - 缓存满时淘汰�€旧条���FIFO LRU via OrderedDict�?        - API 失败时返�?None（调用方降级为纯 BM25�?        """
        key = text[:200].strip().lower()
        if key in _EMBED_CACHE:
            _EMBED_CACHE.move_to_end(key)
            _EMBED_STATS["cache_hits"] += 1
            return _EMBED_CACHE[key]
        try:
            vecs = await self.ai_client.embed([text])
            if not vecs:
                return None
            vec = vecs[0]
            if len(_EMBED_CACHE) >= _EMBED_CACHE_MAX:
                _EMBED_CACHE.popitem(last=False)
            _EMBED_CACHE[key] = vec
            _EMBED_STATS["api_calls"] += 1
            return vec
        except Exception as _e:
            self.logger.debug("Embedding 缓存调用失败: %s", _e)
            return None

    def _detect_implicit_feedback(self, text: str) -> str:
        """
        # �€测用户短消息���隐式反�情绪�?        返回 'pos'（好评）�?neg'（差�?纠�）或 None（无明确信号）�€?        仅� �?0 字的������效，避免���新问题为反��?        """
        t = (text or "").strip()
        if not t or len(t) > 60:
            return None
        t_lower = t.lower()
        _POS = {
            # "谢谢", "感谢", "好的", "收到", "ok", "okay", "thanks", "thank you", "شكرا", "obrigado", "�?, "�?, "👍", "�?, "明白�?, "知道�?, "alright", "got it", "noted", "شكراً",
        }
        _NEG = {
            # "不�", "错了", "不是", "有�", "重新", "重发", "再发", "纠�", "wrong", "incorrect", "not right", "错�", "不准�?, "خطأ", "errado", "不是这个", "不是这样", "你没回答", "没�清�", "答非�€�?, "我问的不�?, "说的不�", "你�的不�?, "重新�?, "没用",
        }
        for pos in _POS:
            if pos in t_lower:
                return "pos"
        for neg in _NEG:
            if neg in t_lower:
                return "neg"
        return None

    DISABLED_STATUSES = _CHANNEL_DISABLED_STATUSES

    @staticmethod
    def _is_channel_metrics_query(text: str) -> bool:
        """成功率 / 手续费费率 类咨询：应走实时通道数据，避免 KB「去后台查费率」直出抢答。"""
        raw = text or ""
        t = raw.lower()
        if "成功率" in raw or "success rate" in t or "success_rate" in t.replace(" ", ""):
            return True
        if "费率" in raw or "手续费" in raw:
            return True
        if any(w in t for w in ("fee rate", "commission", "service fee", "taux", "gebühr", "tariffa", "комиссия")):
            return True
        return False

    def _narrow_reply_greeting_allows(self, raw: str, cfg: Dict[str, Any]) -> bool:
        """收窄模式下 greeting：客服在线用语 + greeting_substrings + 内置多语种词库。"""
        tl = (raw or "").lower()
        cs = list(cfg.get("cs_online_substrings") or [])
        if not cs:
            cs = [
                "在吗", "在不", "有人吗", "客服", "人工", "在线吗", "上班吗",
                "有没有客服", "真人", "在不在",
            ]
        gr = merge_greeting_substrings(cfg.get("greeting_substrings") or [])
        if any((s or "").lower() in tl for s in cs):
            return True
        if any((s or "").lower() in tl for s in gr):
            return True
        # 与 _recognize_intent 一致（含单独「在」、哈喽/hola 等）
        if is_greeting_message(raw):
            return True
        return False

    def _narrow_reply_allows(
        self,
        text: str,
        intent: str,
        last_intent: str,
        user_context: Dict[str, Any],
    ) -> bool:
        """收窄模式：仅允许客服在线类 greeting、通道/限额/成功率类 channel_info、status_check。"""
        cfg = getattr(self, "_narrow_reply_cfg", None) or {}
        if not cfg.get("enabled"):
            return True
        allowed = set(cfg.get("allowed_intents") or [])
        if intent not in allowed:
            return False
        raw = text or ""
        tl = raw.lower()
        for d in cfg.get("deny_substrings") or []:
            ds = (d or "").strip().lower()
            if ds and ds in tl:
                return False
        if intent == "greeting":
            return self._narrow_reply_greeting_allows(raw, cfg)
        if intent in ("channel_info", "status_check"):
            subs = list(cfg.get("channel_topic_substrings") or [])
            if not subs:
                subs = [
                    "通道", "额度", "限额", "成功率", "代收", "代付", "维护", "波动",
                    "稳定", "单笔", "限制", "状态", "费率", "手续费",
                    "channel", "limit", "success rate", "payin", "payout", "fee",
                    "maintenance", "collection", "disburs", "deposit", "withdraw",
                    "quota", "commission", "transfer", "balance",
                ]
            if any((s or "").lower() in tl for s in subs):
                return True
            from src.hooks.registry import HookRegistry as _HReg
            _hr = _HReg.get_instance()
            if _hr.is_domain_metrics_query(raw):
                return True
            if _hr.is_meaningless_interjection(raw.strip()):
                return False
            sec = float(cfg.get("inherit_followup_seconds") or 120)
            lm = float(user_context.get("last_message_time") or 0)
            _fc = _hr.get_followup_config()
            _fi = _fc.get("followup_intents", {"channel_info", "status_check"})
            if last_intent in _fi and (time.time() - lm) < sec:
                if _hr.is_short_followup(raw.strip()):
                    return True
            return False
        return True

    def _is_channel_disabled(self, ch: dict) -> bool:
        return is_channel_disabled(ch)

    def _get_live_channel_status(self, include_fee: Optional[bool] = None) -> str:
        """读取后台通道实时数据。默认不在对话中展示手续费百分比。

        include_fee=None 时读取 ai.channel_status_include_fee（默认 false）。
        """
        if include_fee is None:
            try:
                ai_cfg = self.config.get_ai_config() if hasattr(self.config, "get_ai_config") else {}
                include_fee = bool((ai_cfg or {}).get("channel_status_include_fee", False))
            except Exception:
                include_fee = False
        try:
            rates = getattr(self.config, 'get_exchange_rates_config', lambda: None)()
            channels = (rates or {}).get('channels', {})
            result = format_live_channel_status_text(channels, include_fee=include_fee)
            if result:
                n_active = sum(
                    1 for ch in channels.values()
                    if isinstance(ch, dict) and not is_channel_disabled(ch)
                )
                n_dis = sum(
                    1 for ch in channels.values()
                    if isinstance(ch, dict) and is_channel_disabled(ch)
                )
                self.logger.info(
                    "channel_status_info (%d active, %d disabled): %s",
                    n_active, n_dis, result[:300],
                )
            return result
        except Exception:
            return ""

    # ── Phase ③ 剧情/场景 roleplay ────────────────────────────────
    def _story_cfg(self) -> Dict[str, Any]:
        try:
            cfg = self.config.config if hasattr(self.config, "config") else {}
            if not isinstance(cfg, dict):
                return {}
            return (cfg.get("companion") or {}).get("story") or {}
        except Exception:
            return {}

    def _selfie_cfg(self) -> Dict[str, Any]:
        """Stage A：陪伴形象照配置（companion.selfie）。缺/异常 → {}（默认关）。"""
        try:
            cfg = self.config.config if hasattr(self.config, "config") else {}
            if not isinstance(cfg, dict):
                return {}
            sc = (cfg.get("companion") or {}).get("selfie")
            return sc if isinstance(sc, dict) else {}
        except Exception:
            return {}

    def _monetization_gate_enabled(self) -> bool:
        """变现门控总闸：monetization.enabled 且 gate.enabled。任一关 → False（不计费）。"""
        try:
            cfg = self.config.config if hasattr(self.config, "config") else {}
            mon = (cfg.get("monetization") or {}) if isinstance(cfg, dict) else {}
            return bool(mon.get("enabled") and (mon.get("gate") or {}).get("enabled"))
        except Exception:
            return False

    def _get_selfie_cap(self, cap: int):
        """全局出图预算跟踪器（进程级单例，复用 DailyCapTracker，按 tz 0 点自动归零）。

        护住出图 API（OpenAI images 等）账单：**跨所有端用户/所有账号**的当日出图总次数硬上限——
        与「按端用户免费额度」互补（后者限单人、前者限全局爆发面，防 N 个新用户各刷免费图烧钱）。
        0=不限。运行时 set_cap 跟随 config 调整。Stage J：提升为单例后 Web 看板可 peek 同一份取快照。
        """
        from src.utils.selfie_cap import get_selfie_cap_tracker
        return get_selfie_cap_tracker(int(cap))

    def _record_selfie_event(self, contact_key: str, kind: str) -> None:
        """Stage B：把自拍准入结果(too_soon/locked/delivered)埋点进转化漏斗（best-effort）。

        只 ``peek`` 已存在的漏斗单例（monetization 就绪才有）——未初始化则静默 no-op，
        绝不在自拍主流程里误建 ``:memory:`` store，也绝不抛。``contact_key`` 取 ``user_id``
        （与 entitlement/tx_ledger 同一身份键），保证后续 ``exclusive_album`` 付费可归因。
        """
        try:
            from src.utils.companion_funnel_store import peek_companion_funnel_store
            store = peek_companion_funnel_store()
            if store is not None:
                store.record_selfie(str(contact_key or ""), kind)
        except Exception:
            self.logger.debug("record_selfie_event skipped", exc_info=True)

    async def _handle_selfie_request(
        self, text: str, user_id_str: str, user_context: Dict[str, Any], chat_id: Any,
    ) -> Optional[str]:
        """Stage A：处理「给我看看你/发张自拍」——按关系等级 + 付费权益判准入。

        返回字符串=短路（搪塞/付费引导/出图后的配文/兜底文字）；None=非自拍请求或功能未开。
        关系浅→温柔搪塞；gate 开+未拥有 exclusive_album+免费额度用尽→软付费引导（驱动解锁）；
        准入→provider 出图（默认 disabled 则退回文字陪伴），有受管媒体 worker 时经编排器发出。
        """
        scfg = self._selfie_cfg()
        if not scfg.get("enabled", False):
            return None
        from src.ai.companion_selfie import (
            build_selfie_prompt,
            decide_selfie,
            detect_selfie_request,
            get_selfie_provider,
        )
        if not detect_selfie_request(text):
            return None
        persona_name = self._get_persona_name_for_context(user_context) or "我"
        # 免费额度按天计（仅 gate 开且未拥有相册时才消耗；拥有者/不计费时不限）
        today = time.strftime("%Y%m%d")
        if user_context.get("_selfie_date") != today:
            user_context["_selfie_date"] = today
            user_context["_selfie_used"] = 0
        free_used = int(user_context.get("_selfie_used") or 0)
        # 权益：复用已懒解析的 entitlement，否则即时解析（best-effort）
        ent = user_context.get("entitlement")
        if not isinstance(ent, dict):
            try:
                from src.utils.companion_context import resolve_entitlement
                ent = resolve_entitlement(user_id_str)
            except Exception:
                ent = None
        decision = decide_selfie(
            entitlement=ent if isinstance(ent, dict) else None,
            gate_enabled=self._monetization_gate_enabled(),
            free_used=free_used,
            free_daily=int(scfg.get("free_daily", 1) or 0),
            bond_level=self._bond_level_from_context(user_context, chat_id),
            min_bond_level=int(scfg.get("min_bond_level", 2) or 0),
        )
        action = decision.get("action")
        if action == "too_soon":
            self._record_selfie_event(user_id_str, "too_soon")
            return (f"哎呀，我们才刚开始熟悉呢，等再多聊聊、更亲近一点，"
                    f"{persona_name}就给你看我的样子好不好～")
        if action == "locked":
            self._record_selfie_event(user_id_str, "locked")
            return self._selfie_upsell_text(ent, persona_name)
        # action == allow：尝试出图（默认 disabled → 退回文字）
        provider = get_selfie_provider(scfg.get("provider") or {})
        will_generate = bool(getattr(provider, "enabled", False)) and \
            str(getattr(provider, "backend", "")).lower() not in ("", "disabled")
        cap = int(scfg.get("daily_global_cap", 0) or 0)
        if will_generate and cap > 0 and self._get_selfie_cap(cap).would_exceed(1):
            # 全局出图预算用尽：优雅兜底——不记 delivered、不消耗用户免费额度，护住出图 API 账单。
            self.logger.info("selfie daily_global_cap=%d 已达上限，软兜底", cap)
            self._record_selfie_event(user_id_str, "capped")
            return (f"{persona_name}今天已经拍了好多照片啦，有点累咯～"
                    f"明天再给你拍新的好不好？😊")
        self._record_selfie_event(user_id_str, "delivered")
        prompt = build_selfie_prompt(
            self._selfie_persona_for_prompt(user_context),
            scene_hint=str(scfg.get("scene_hint") or ""),
            style=str(scfg.get("style") or ""),
            default_appearance=str(scfg.get("appearance") or ""),
        )
        caption = str(scfg.get("caption")
                      or f"这是刚拍的，给你看～ 喜欢{persona_name}吗？😊")
        if will_generate and cap > 0:
            self._get_selfie_cap(cap).record_sent(1)
        try:
            res = await provider.generate(prompt)
        except Exception:
            res = None
            self.logger.debug("selfie generate error", exc_info=True)
        if res is not None and getattr(res, "ok", False):
            sent = await self._try_send_selfie_media(
                user_context, chat_id, res.image_path, caption)
            if decision.get("used_free"):
                user_context["_selfie_used"] = free_used + 1
            if sent:
                return ""  # 媒体已发出，无需再发文字（空串=已处理、不再生成普通回复）
            # 出图成功但无可用媒体通道 → 退回配文文字（至少有温暖回应）
            return caption
        # provider 未配/失败 → 优雅退回文字陪伴（不报错给用户）
        if decision.get("used_free"):
            user_context["_selfie_used"] = free_used + 1
        return (f"{persona_name}现在不太方便拍照呢，不过我一直在这儿陪你～"
                f"想我了的话，多跟我说说话好不好？")

    def _selfie_persona_for_prompt(self, user_context: Dict[str, Any]) -> Any:
        """取出图用 persona（dict 含 name/appearance 等）；拿不到则回 name 字符串/空。"""
        try:
            persona_id = (user_context or {}).get("account_persona_id") or ""
            if persona_id:
                from src.utils.persona_manager import PersonaManager
                p = PersonaManager.get_instance().get_persona_by_id(str(persona_id))
                if isinstance(p, dict):
                    return p
        except Exception:
            pass
        return self._get_persona_name_for_context(user_context)

    def _selfie_upsell_text(self, entitlement: Any, persona_name: str) -> str:
        """付费相册软引导（不硬推销，贴人设）。复用 monetization.upsell_*。"""
        from src.ai.companion_selfie import SELFIE_FEATURE
        try:
            from src.utils.monetization import (
                merge_catalog,
                upsell_offer,
                upsell_pitch_hint,
            )
            cfg = self.config.config if hasattr(self.config, "config") else {}
            mon = (cfg.get("monetization") or {}) if isinstance(cfg, dict) else {}
            catalog = merge_catalog(mon.get("catalog"))
            offer = upsell_offer(
                entitlement if isinstance(entitlement, dict) else None,
                SELFIE_FEATURE, catalog=catalog, gate_enabled=True)
            hint = upsell_pitch_hint(offer, persona_name=persona_name)
        except Exception:
            hint = ""
        lead = f"我的照片是只给最亲近的人看的小秘密哦～"
        return (lead + hint) if hint else (
            lead + f"解锁「专属相册」就能看到{persona_name}啦～")

    async def _try_send_selfie_media(
        self, user_context: Dict[str, Any], chat_id: Any,
        image_path: str, caption: str,
    ) -> bool:
        """best-effort 发出照片。① 编排器受管媒体 worker（B 线/受管账号）；
        ② A 线主客户端直发（user_context 注入的 ``_send_photo_to_chat`` 回调）。

        两路都不可用 → False（调用方退回文字陪伴）。任一路成功即 True。
        """
        if not image_path:
            return False
        # ① 编排器受管媒体 worker（需 platform+account+chat_key 且 owns_media）
        try:
            platform = str(user_context.get("platform") or "").strip()
            account_id = str(user_context.get("account_id")
                             or user_context.get("account_persona_id") or "").strip()
            chat_key = str(chat_id or "").strip()
            if platform and account_id and chat_key:
                from src.integrations.account_orchestrator import get_orchestrator
                orch = get_orchestrator(self.config.config or {})
                if orch.owns_media(platform, account_id):
                    await orch.send_media(
                        platform, account_id, chat_key,
                        media_path=image_path, media_url="", media_type="image",
                        caption=caption)
                    return True
        except Exception:
            self.logger.debug("selfie orchestrator media send failed", exc_info=True)
        # ② A 线主客户端直发（Pyrogram send_photo 经回调注入；主平台 Telegram 无受管 worker 时兜底）
        try:
            sender = user_context.get("_send_photo_to_chat")
            if callable(sender):
                ok = await sender(chat_id, image_path, caption)
                return bool(ok)
        except Exception:
            self.logger.debug("selfie direct send failed", exc_info=True)
        return False

    def _story_scenarios(self) -> Dict[str, Any]:
        sc = self._story_cfg().get("scenarios")
        return sc if isinstance(sc, dict) else {}

    def _story_state_root(self, user_context: Dict[str, Any]) -> Dict[str, Any]:
        root = user_context.get("story_state")
        if not isinstance(root, dict):
            root = {}
            user_context["story_state"] = root
        return root

    def _get_story_state(self, user_context: Dict[str, Any], chat_id: Any):
        from src.utils.companion_relationship import chat_storage_key
        return self._story_state_root(user_context).get(chat_storage_key(chat_id))

    def _set_story_state(self, user_context: Dict[str, Any], chat_id: Any, state) -> None:
        from src.utils.companion_relationship import chat_storage_key
        root = self._story_state_root(user_context)
        key = chat_storage_key(chat_id)
        if state is None:
            root.pop(key, None)
        else:
            root[key] = state

    def _writeback_story_memory(
        self, user_id: str, chat_id: Any, user_context: Dict[str, Any], memory: str
    ) -> None:
        """剧情收场 → 把「共享经历」回写情景记忆（Phase ④ 闭环核心）。

        共享经历在虚构里真实发生过 → 以 ``user_stated`` 高置信入库：可被 consolidate
        晋升 stable、可被 proactive_topic 日后主动回访（"还记得那次……吗"）。add_fact
        以内容哈希去重，重复收场不会灌水。任何失败都不得打断回复管线。
        """
        store = getattr(self, "_episodic_store", None)
        mem = (memory or "").strip()
        if not store or not mem:
            return
        try:
            platform = str(user_context.get("platform", "") or "")
            key = self._episodic_storage_key(user_id, chat_id, platform)
            store.add_fact(key, mem, "story", source="user_stated")
            self.logger.info(
                "[story] writeback shared-memory key=%s mem=%r", key, mem[:60]
            )
        except Exception:
            self.logger.debug("story memory writeback failed", exc_info=True)

    def _story_bonus_cap(self) -> float:
        """剧情累计加成上限（防止刷剧情把关系刷满；默认 12，约够升一个等级带）。"""
        try:
            return float(self._story_cfg().get("max_intimacy_bonus", 12) or 12)
        except Exception:
            return 12.0

    def _apply_story_intimacy_bonus(
        self, user_context: Dict[str, Any], chat_id: Any, bonus: float
    ) -> None:
        """剧情收场 → 累加一份「共同经历」关系加成（Phase ④「剧情→成长」边）。

        intimacy_score 由 IntimacyEngine 拥有（不可从此处直写），故把剧情加成作为
        **独立累加项**存 rel_state.story_bonus（随 user_context 持久化、按 chat 维度），
        在 ``_effective_intimacy`` 处叠加进 bond 计算——既不篡改事实源、又让「完成深度
        剧情」真实推动关系等级与更深剧情解锁。封顶防刷；失败不打断管线。
        """
        try:
            b = float(bonus or 0.0)
        except (TypeError, ValueError):
            return
        if b <= 0:
            return
        try:
            from src.utils.companion_relationship import get_rel_state
            st = get_rel_state(user_context, chat_id)
            cur = float(st.get("story_bonus", 0) or 0)
            st["story_bonus"] = round(min(self._story_bonus_cap(), cur + b), 2)
            self.logger.info(
                "[story] intimacy bonus +%.1f → story_bonus=%.1f chat=%s",
                b, st["story_bonus"], chat_id,
            )
        except Exception:
            self.logger.debug("story intimacy bonus apply failed", exc_info=True)

    def _record_story_completion(
        self, user_context: Dict[str, Any], chat_id: Any,
        scenario_id: str, title: str, bonus: float, ending: str = "",
    ) -> None:
        """剧情收场结算（Phase ④续）：首次完成才给加成 + 关系纪念点；重复完成不刷分。

        - **防刷**：``rel_state.story_done`` 记已完成场景；重复完成 intimacy_bonus 归零
          （记忆仍照常回写、复发自然累积，但关系深度只认「真实的新经历」）。
        - **跨场景因果（Phase ④续³）**：``rel_state.story_outcomes[sid]=ending`` 记下所取结局，
          供后续剧情的 ``requires_story`` 前置 gate 判定——孤立剧情连成有因果的故事线。
        - **情感闭环**：首次完成 → 置一次性 ``bond_fresh_milestone``，下一轮 bond 块自然
          致意（"我们刚一起经历了那次约会，感觉更近了"）——剧情→成长→真情流露。
        任何失败不打断回复管线。
        """
        sid = str(scenario_id or "").strip()
        try:
            from src.utils.companion_relationship import get_rel_state
            st = get_rel_state(user_context, chat_id)
            done = st.get("story_done")
            if not isinstance(done, list):
                done = []
                st["story_done"] = done
            # 结局足迹（首次/重复都刷新为最近一次所取结局，供因果 gate）
            outcomes = st.get("story_outcomes")
            if not isinstance(outcomes, dict):
                outcomes = {}
                st["story_outcomes"] = outcomes
            if sid:
                outcomes[sid] = str(ending or "")
            is_first = bool(sid) and sid not in done
            if not is_first:
                self.logger.info("[story] replay (no bonus) scenario=%s", sid)
                return
            done.append(sid)
            if float(bonus or 0.0) > 0:
                self._apply_story_intimacy_bonus(user_context, chat_id, bonus)
            t = (title or "").strip()
            if t:
                user_context["bond_fresh_milestone"] = f"story:一起经历了《{t}》"
            # 统一镜像（best-effort）：把首次收场写进 contacts journey 事件流，
            # 让运营健康卡用同一公式算出与会话侧一致的 effective bond。
            # 仅首次（防刷已在上面 gate），与会话侧加成同源同量、不双算（健康卡读事件、
            # 会话读 rel_state，两条互不叠加）。provider 未注册 → no-op，零行为变化。
            self._mirror_story_completion_to_journey(
                user_context, chat_id, sid, t, float(bonus or 0.0), str(ending or ""))
        except Exception:
            self.logger.debug("story completion record failed", exc_info=True)

    def _mirror_story_completion_to_journey(
        self, user_context: Dict[str, Any], chat_id: Any,
        scenario_id: str, title: str, bonus: float, ending: str = "",
    ) -> bool:
        """把剧情首次收场镜像进 contacts journey（供运营健康卡统一 effective bond）。

        寻址需要 ``account_id`` + ``platform``（A 线 telegram_client 已注入 account_id）；
        缺任一 → 跳过。任何失败都吞掉、绝不影响回复管线（会话侧加成已独立生效）。
        """
        try:
            account_id = user_context.get("account_id")
            channel = str(user_context.get("platform") or "").strip() or "telegram"
            if not account_id or chat_id in (None, ""):
                return False
            from src.utils.companion_context import record_story_completion
            return record_story_completion(
                account_id, chat_id, scenario_id,
                channel=channel, ending=ending,
                intimacy_bonus=bonus, title=title,
            )
        except Exception:
            self.logger.debug("story journey mirror skipped", exc_info=True)
            return False

    def _story_outcomes(self, user_context: Dict[str, Any], chat_id: Any) -> Dict[str, str]:
        """已完成剧情 → 所取结局 ``{scenario_id: ending_id_or_""}``（供 requires_story gate）。"""
        try:
            from src.utils.companion_relationship import get_rel_state
            oc = get_rel_state(user_context, chat_id).get("story_outcomes")
            return oc if isinstance(oc, dict) else {}
        except Exception:
            return {}

    def _effective_intimacy(self, user_context: Dict[str, Any], chat_id: Any = ""):
        """基础 intimacy_score + 剧情累计加成（封顶 100）；无基础信号时返回原值（不臆造）。"""
        base = user_context.get("intimacy_score")
        if base is None:
            return None
        try:
            b = float(base)
        except (TypeError, ValueError):
            return base
        try:
            from src.utils.companion_relationship import get_rel_state
            bonus = float(get_rel_state(user_context, chat_id).get("story_bonus", 0) or 0)
        except Exception:
            bonus = 0.0
        return max(0.0, min(100.0, b + bonus))

    def _bond_level_from_context(
        self, user_context: Dict[str, Any], chat_id: Any = ""
    ) -> int:
        try:
            from src.contacts.relationship_level import compute_bond_level
            return int(compute_bond_level(
                self._effective_intimacy(user_context, chat_id)).get("level", 0))
        except Exception:
            return 0

    def _match_scenario(self, scenarios: Dict[str, Any], name: str):
        n = (name or "").strip().lower()
        if not n:
            return None
        for sid, scn in scenarios.items():
            title = str((scn or {}).get("title", "")).strip().lower()
            if n == str(sid).lower() or n == title:
                return sid
        for sid, scn in scenarios.items():
            title = str((scn or {}).get("title", "")).strip().lower()
            if (title and n in title) or n in str(sid).lower():
                return sid
        return None

    @staticmethod
    def _scenario_title(scenarios: Dict[str, Any], sid: str) -> str:
        scn = (scenarios or {}).get(str(sid)) or {}
        return str(scn.get("title") or sid)

    def _ensure_entitlement(self, user_id: Any, user_context: Dict[str, Any]) -> None:
        """Stage 1：把端用户真实付费权益懒解析进 ``user_context["entitlement"]``。

        仅 story 启用时解析（普通消息零开销）；5 分钟 TTL 缓存（权益变动罕见，避免每条
        消息查库）。resolver 未注册（monetization 未就绪）→ 不动 user_context（entitlement
        维持原值/None → 付费场景锁，零回归）。绝不抛——任何失败退回旧行为。
        """
        try:
            if not self._story_cfg().get("enabled", False):
                return
            cached = user_context.get("entitlement")
            try:
                ts = float(user_context.get("_entitlement_at") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if isinstance(cached, dict) and (time.time() - ts) < 300.0:
                return  # 近 5 分钟已解析，复用（避免每条消息查库）
            from src.utils.companion_context import resolve_entitlement
            ent = resolve_entitlement(user_id)
            if isinstance(ent, dict):
                user_context["entitlement"] = ent
                user_context["_entitlement_at"] = time.time()
        except Exception:
            self.logger.debug("entitlement resolve skipped", exc_info=True)

    def _handle_story_command(self, text: str, user_context: Dict[str, Any], chat_id: Any):
        """剧情指令（默认关 companion.story.enabled）：列表 / 开始 / 结束。

        返回回复字符串=短路（列表/结束/锁定提示）；返回 None=非剧情指令或「开始成功」
        （成功时只置 state，让正常回复流程带着【剧情场景】块自然开场）。
        """
        cfg = self._story_cfg()
        if not cfg.get("enabled", False):
            return None
        t = (text or "").strip()
        scenarios = self._story_scenarios()
        if not scenarios:
            return None
        ent = user_context.get("entitlement")
        ent = ent if isinstance(ent, dict) else None
        bond = self._bond_level_from_context(user_context, chat_id)
        completed = self._story_outcomes(user_context, chat_id)

        if t in ("结束剧情", "退出剧情", "/story stop", "story stop"):
            if self._get_story_state(user_context, chat_id):
                self._set_story_state(user_context, chat_id, None)
                return "好呀，那我们先回到平常聊天～"
            return None

        if t in ("剧情列表", "剧情", "/story", "story", "story list"):
            from src.skills.story_engine import list_scenarios
            rows = list_scenarios(
                scenarios, entitlement=ent, bond_level=bond, completed=completed)
            if not rows:
                return None
            lines = []
            for r in rows:
                if r["available"]:
                    lines.append(f"· {r['title']}（发「开始剧情 {r['title']}」）")
                elif r["locked_reason"].startswith("need_bond"):
                    lines.append(f"· {r['title']}（我们再熟一点就能解锁）")
                elif r["locked_reason"].startswith("need_story"):
                    pre = self._scenario_title(
                        scenarios, r["locked_reason"].split(":", 1)[-1])
                    lines.append(f"· {r['title']}（经历过《{pre}》后解锁）")
                else:
                    lines.append(f"· {r['title']}（专属剧情，需解锁）")
            return "想一起经历点什么吗？\n" + "\n".join(lines)

        prefix = None
        for p in ("开始剧情", "/story start ", "story start "):
            if t.startswith(p):
                prefix = p
                break
        if prefix is None:
            return None
        name = t[len(prefix):].strip()
        sid = self._match_scenario(scenarios, name)
        if not sid:
            return "嗯…我还不会这个剧情呢，发「剧情列表」看看有哪些？"
        from src.skills.story_engine import scenario_locked_reason, start_scenario
        state = start_scenario(
            sid, scenarios, entitlement=ent, bond_level=bond, completed=completed)
        if state is None:
            reason = scenario_locked_reason(
                scenarios.get(sid) or {}, entitlement=ent, bond_level=bond,
                completed=completed)
            if reason.startswith("need_bond"):
                return "这个故事要我们更熟一些才能解锁哦，再多陪我聊聊吧～"
            if reason.startswith("need_story"):
                pre = self._scenario_title(scenarios, reason.split(":", 1)[-1])
                return f"这段故事是后续呢，我们先一起经历《{pre}》吧～"
            if reason.startswith("need_unlock"):
                return "这是一段专属剧情，解锁后我们就能一起体验啦。"
            return None
        self._set_story_state(user_context, chat_id, state)
        self.logger.info("[story] start scenario=%s chat=%s", sid, chat_id)
        return None

    _GROWTH_TRIGGERS = frozenset({
        "我们的关系", "关系进度", "关系状态", "我的等级", "成长", "成长进度",
        "/status", "/relationship", "我们的故事",
    })

    @staticmethod
    def _progress_bar(progress: float, cells: int = 10) -> str:
        try:
            p = max(0.0, min(1.0, float(progress)))
        except (TypeError, ValueError):
            p = 0.0
        filled = int(round(p * cells))
        return "▮" * filled + "▯" * (cells - filled)

    def _handle_growth_command(
        self, text: str, user_context: Dict[str, Any], chat_id: Any
    ):
        """关系/成长面板（端用户在对话内一屏看见成长）：等级+进度+里程碑+剧情足迹。

        返回字符串=短路；None=非指令 / 非陪伴域。纯读已算好的数据（compute_bond_level /
        bond_milestones / list_scenarios / rel_state.story_done），不写库、不调 LLM。
        """
        t = (text or "").strip()
        if t not in self._GROWTH_TRIGGERS:
            return None
        cfg0 = self.config.config if hasattr(self.config, "config") else {}
        comp = (cfg0.get("companion") or {}) if isinstance(cfg0, dict) else {}
        if effective_domain_name(cfg0) != "conversion" or not comp.get("enabled", True):
            return None

        from src.contacts.relationship_level import (
            bond_milestones as _bm,
            compute_bond_level as _cbl,
            level_unlocks as _lu,
        )

        eff = self._effective_intimacy(user_context, chat_id)
        if eff is None:
            eff = user_context.get("intimacy_score")
        lvl = _cbl(eff)
        days_known = user_context.get("relationship_days")

        lines: List[str] = []
        if lvl.get("level", 0) >= 1:
            head = f"💞 我们现在是「{lvl['name']}」的关系"
            if not lvl.get("is_max") and lvl.get("next_name"):
                head += f"（再深一点就是「{lvl['next_name']}」啦）"
            lines.append(head)
            bar = self._progress_bar(lvl.get("progress", 0.0))
            if lvl.get("is_max"):
                lines.append(f"{bar}  已经是最亲密的关系了呢")
            else:
                stn = lvl.get("score_to_next")
                tail = f"，再积累一点点就能更进一步" if stn else ""
                lines.append(f"{bar}  {int(round(lvl.get('progress', 0.0) * 100))}%{tail}")
        else:
            lines.append("💞 我们才刚认识不久，多陪我聊聊，关系会慢慢变深的～")

        # 里程碑（含相识时长 + 升级；剧情纪念点单独在下方剧情足迹体现）
        try:
            ms = _bm(intimacy_score=eff, days_known=days_known)
            if ms:
                labels = "、".join(m["label"] for m in ms[:5])
                lines.append(f"🌱 一起走过：{labels}")
        except Exception:
            self.logger.debug("growth milestones skipped", exc_info=True)

        # 剧情足迹：经历过 / 还能一起经历 / 待解锁
        scfg = self._story_cfg()
        if scfg.get("enabled", False):
            scenarios = self._story_scenarios()
            if scenarios:
                from src.skills.story_engine import list_scenarios
                ent = user_context.get("entitlement")
                ent = ent if isinstance(ent, dict) else None
                bond = self._bond_level_from_context(user_context, chat_id)
                completed = self._story_outcomes(user_context, chat_id)
                try:
                    from src.utils.companion_relationship import get_rel_state
                    done_ids = set(get_rel_state(user_context, chat_id).get("story_done") or [])
                except Exception:
                    done_ids = set()
                rows = list_scenarios(
                    scenarios, entitlement=ent, bond_level=bond, completed=completed)
                done_titles, avail, locked = [], [], []
                for r in rows:
                    if r["id"] in done_ids:
                        done_titles.append(r["title"])
                    elif r["available"]:
                        avail.append(r["title"])
                    else:
                        locked.append(r["title"])
                if done_titles:
                    lines.append("📖 我们一起经历过：" + "、".join(f"《{x}》" for x in done_titles))
                if avail:
                    lines.append("✨ 还能一起经历：" + "、".join(f"《{x}》" for x in avail)
                                 + "（发「开始剧情 名称」）")
                if locked:
                    lines.append("🔒 等关系更深/解锁后可体验：" + "、".join(f"《{x}》" for x in locked))

        # 等级解锁预览（若配置了 bond_level.unlocks）
        try:
            bl_cfg = comp.get("bond_level") or {}
            if bl_cfg.get("enabled", False):
                unlocked = _lu(lvl.get("level", 0), bl_cfg.get("unlocks"))
                if unlocked:
                    lines.append("🎁 当前等级已解锁：" + "、".join(unlocked))
        except Exception:
            self.logger.debug("growth unlocks skipped", exc_info=True)

        return "\n".join(lines)

    def _update_after_reply(self, reply: str, user_id: str, user_context: Dict[str, Any],
                            chat_id: Any = '', user_msg: str = ''):
        """回�后更新状�?"""
        current_time = time.time()
        
        # Fix D: sanitize reply before persisting (any failure must NOT break the pipeline)
        try:
            _clean_reply = self._sanitize_assistant_reply(reply, user_context)
        except Exception as _se:
            self.logger.debug("sanitize_reply skipped: %s", _se)
            _clean_reply = reply
        user_context.update({
            'last_reply': _clean_reply[:500],
            'last_reply_time': current_time,
            'reply_count': user_context.get('reply_count', 0) + 1
        })

        try:
            _cfg0 = self.config.config if hasattr(self.config, "config") else {}
            _comp_cfg = (_cfg0.get("companion") or {}) if isinstance(_cfg0, dict) else {}
            if (
                effective_domain_name(_cfg0) == "conversion"
                and _comp_cfg.get("enabled", True)
                and (reply or "").strip()
            ):
                from src.utils.companion_relationship import (
                    get_rel_state,
                    reconcile_stage_after_assistant_reply,
                )

                st = get_rel_state(user_context, chat_id)
                st["exchange_count"] = int(st.get("exchange_count", 0) or 0) + 1
                reconcile_stage_after_assistant_reply(st, _comp_cfg)
        except Exception:
            pass

        # Phase ③/④：活动剧情按用户轮次确定性推进 beat（用户回应驱动分支路由），
        # 剧终自动收场并把「共享经历」回写情景记忆——闭环到 ①（被巩固/被 proactive_topic 回访）。
        try:
            _scfg = self._story_cfg()
            if _scfg.get("enabled", False) and (reply or "").strip():
                _sstate = self._get_story_state(user_context, chat_id)
                if _sstate:
                    from src.skills.story_engine import advance_state
                    _at = int(_scfg.get("advance_turns", 0) or 0)
                    _kw = {"advance_turns": _at} if _at > 0 else {}
                    _sid = str(_sstate.get("scenario_id") or "")
                    _ending = str(_sstate.get("ending_id") or "")
                    _new, _fin, _payload = advance_state(
                        _sstate, self._story_scenarios(),
                        user_message=user_msg, **_kw)
                    self._set_story_state(
                        user_context, chat_id, None if _fin else _new)
                    if _fin and isinstance(_payload, dict):
                        _mem = str(_payload.get("memory") or "").strip()
                        if _mem:
                            self._writeback_story_memory(
                                user_id, chat_id, user_context, _mem)
                        _bonus = float(_payload.get("intimacy_bonus") or 0.0)
                        _title = str(
                            (self._story_scenarios().get(_sid) or {}).get("title")
                            or _sid
                        )
                        self._record_story_completion(
                            user_context, chat_id, _sid, _title, _bonus,
                            ending=_ending)
        except Exception:
            self.logger.debug("story advance skipped", exc_info=True)

        self.global_last_reply_time = current_time
        if chat_id:
            self._chat_user_last_reply[f"{chat_id}_{user_id}"] = current_time
        
        content_hash = self._hash_content(user_context.get('last_message', ''), chat_id)
        self.reply_cache[content_hash] = current_time
        
        # L4: 更新用户画像标�
        self._update_user_profile(user_context, current_time)

        # G4: 回�质量���（�则引擎，零成���
        self._evaluate_reply_quality(reply, user_id, user_context)

        # J1: �€测是否需要人工升�?
        self._check_escalation(user_id, user_context, chat_id, current_time)
        
        self._context_store.mark_dirty(user_id)
        if int(current_time) % 5 == 0:
            self._context_store.flush(user_id)
        
        self._cleanup_cache()

    # �€�€ H3: 意图链模式��?�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€
    _CHAIN_PATTERNS = [
        {
            "pattern": "escalation_complaint",
            "desc": "用户从咨询升级到投诉",
            "sequences": [
                ["order_query", "complaint"],
                ["channel_info", "complaint"],
                ["status_check", "complaint"],
            ],
            "hint": "用户已从咨询升级为投诉，请优先安抚情绪并承诺，给出明确解决时间和方案。",
        },
        {
            "pattern": "repeated_failure",
            "desc": "用户反复查询未解决",
            "sequences": [
                ["order_query", "order_query", "complaint"],
                ["status_check", "status_check", "complaint"],
            ],
            "hint": "用户多次查询同一问题未得到解决，已产生不满。请直接给出最终答复，避免再次要求提供信息。",
        },
        {
            "pattern": "refund_flow",
            "desc": "投诉后要求退款",
            "sequences": [
                ["complaint", "order_query"],
                ["complaint", "direct_chat"],
            ],
            "hint": "用户投诉后继续追问，可能在要求退款或补偿方案。请主动提供解决选项。",
        },
        {
            "pattern": "channel_troubleshoot",
            "desc": "通道问题排查流程",
            "sequences": [
                ["channel_info", "order_query"],
                ["channel_info", "status_check"],
            ],
            "hint": "用户正在排查通道问题对具体订单的影响，请结合通道状态和订单信息综合回答。",
        },
    ]

    def _detect_chain_pattern(self, chain: list) -> Optional[Dict]:
        """�€测意图链���匹配已知模式，返回最长匹配的模式信息"""
        if len(chain) < 2:
            return None
        best = None
        for pat in self._CHAIN_PATTERNS:
            for seq in pat["sequences"]:
                slen = len(seq)
                if len(chain) >= slen and chain[-slen:] == seq:
                    if best is None or len(seq) > len(best.get("_match_len", [])):
                        best = {
                            "pattern": pat["pattern"],
                            "desc": pat["desc"],
                            "hint": pat["hint"],
                            "_match_len": seq,
                        }
        if best:
            best.pop("_match_len", None)
        return best

    # �€�€ K1: 规则引擎摘�压缩（零延迟、零成本�?�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€
    _ENTITY_PATTERNS = re.compile(
        r"(?:订单|单号|order)[#:\s]*([A-Za-z0-9]{6,24})"
        r"|(\d{6,24})"
        r"|(?:额度|limit)[�?\s]*([0-9,.]+)"
        r"|(?:EP|JC|代收|代付|提现)\S{0,8}",
        re.IGNORECASE,
    )

    def _compress_history(self, old_messages: list) -> str:
        """将早期�话轮次压缩为�€行摘要（规则引擎，零 API 调用）�€?
        # 提取：关���体（订单�?金��? 意图流转 + 核心结��?        """
        entities = set()
        intents_seen = []
        conclusions = []

        for msg in old_messages:
            text = msg.get("content", "")
            role = msg.get("role", "user")
            for m in self._ENTITY_PATTERNS.finditer(text):
                val = m.group(1) or m.group(2) or m.group(3) or m.group(0)
                if val and len(val) >= 3:
                    entities.add(val.strip())
            if role == "assistant":
                for kw in ("已", "正常", "维护", "成功", "失败", "处理", "提交", "联系"):
                    if kw in text:
                        snippet = text[:60].replace("\n", " ")
                        conclusions.append(snippet)
                        break

        parts = []
        if entities:
            parts.append("提及: " + ", ".join(list(entities)[:6]))
        if conclusions:
            parts.append("结论片段: " + "; ".join(conclusions[:3]))

        summary = " | ".join(parts) if parts else "（早期对话无关键业务实体，可依近期轮次理解）"
        return summary[:300]

    async def _summarize_history_with_fallback(
        self, old_messages: list,
    ) -> str:
        """Phase 2：优先用 LLM (`ai_client.summarize_conversation`) 生成连贯摘要；
        失败 / 超时 / 配置关闭 → 回退到 rule-based `_compress_history`。

        config 项 `ai.summarize_with_llm` (默认 true)。
        """
        cfg_root = (self.config.config or {}) if self.config and hasattr(self.config, "config") else {}
        use_llm = bool((cfg_root.get("ai") or {}).get("summarize_with_llm", True))
        if use_llm and getattr(self, "ai_client", None) is not None:
            ai = self.ai_client
            if hasattr(ai, "summarize_conversation"):
                try:
                    s = await ai.summarize_conversation(
                        old_messages, max_chars=300, timeout_sec=10.0,
                    )
                    if s and isinstance(s, str) and s.strip():
                        return s.strip()[:300]
                except Exception as ex:
                    self.logger.warning(
                        "[summary] LLM summarize 失败 (%s:%s)，回退 rule-based",
                        type(ex).__name__, ex,
                    )
        return self._compress_history(old_messages)

    # �€�€ L4: 用户画像���推断 �€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€
    _URGENCY_WORDS = re.compile(
        r"赶快[点些]|赶紧|马上|立[刻即]|催|等不了|asap|urgent|hurry|尽快",
        re.IGNORECASE
    )

    def _update_user_profile(self, ctx: Dict[str, Any], now: float):
        """基于�行为特征更新用户画像（�则引擎，零延迟）"""
        profile = ctx.get("_user_profile")
        if not isinstance(profile, dict):
            profile = {
                "msg_count": 0,
                "first_seen": now,
                "intent_dist": {},
                "urgency_count": 0,
                "type": "new",
                "tone": "standard",
            }
        profile["msg_count"] = profile.get("msg_count", 0) + 1

        intent = ctx.get("current_intent", "small_talk")
        dist = profile.get("intent_dist", {})
        dist[intent] = dist.get(intent, 0) + 1
        profile["intent_dist"] = dist

        msg = ctx.get("last_message", "")
        if self._URGENCY_WORDS.search(msg):
            profile["urgency_count"] = profile.get("urgency_count", 0) + 1

        mc = profile["msg_count"]
        age_hours = (now - profile.get("first_seen", now)) / 3600

        # 类型推断
        if mc >= 30 or age_hours >= 168:
            profile["type"] = "veteran"
        elif mc >= 10 or age_hours >= 48:
            profile["type"] = "regular"
        else:
            profile["type"] = "new"

        # 高价值用户：高频 order_query / channel_info
        biz_intents = dist.get("order_query", 0) + dist.get("channel_info", 0)
        if biz_intents >= 15:
            profile["type"] = "vip"

        # ���推断
        urg_ratio = profile.get("urgency_count", 0) / max(mc, 1)
        if urg_ratio >= 0.3:
            profile["tone"] = "impatient"
        elif dist.get("complaint", 0) >= 3:
            profile["tone"] = "frustrated"
        elif dist.get("greeting", 0) / max(mc, 1) >= 0.3:
            profile["tone"] = "friendly"
        else:
            profile["tone"] = "standard"

        # K3: 实时满意度评分（0-100，低于40 是 at_risk）
        sat_score = 80.0  # 基准分
        # 负向因子
        _complaint_ratio = dist.get("complaint", 0) / max(mc, 1)
        sat_score -= _complaint_ratio * 60  # 投诉占比越高越差

        _consecutive = ctx.get("_consecutive_same_intent", 0)
        sat_score -= min(_consecutive, 5) * 6  # 连续同问题追�?
        if urg_ratio >= 0.2:
            sat_score -= urg_ratio * 30  # 急躁程度

        # 正向因子
        _greeting_ratio = dist.get("greeting", 0) / max(mc, 1)
        sat_score += _greeting_ratio * 15  # 有问候�明�€�度友好

        if mc >= 5 and _complaint_ratio < 0.1:
            sat_score += 5  # 长期用户无投�?
        sat_score = max(0, min(100, round(sat_score)))
        profile["satisfaction"] = sat_score
        profile["at_risk"] = sat_score < 40

        ctx["_user_profile"] = profile

    # �€�€ G4: 回�质量���（�则引擎） �€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€
    _QUALITY_LOW_THRESHOLD = 40

    def _evaluate_reply_quality(self, reply: str, user_id: str,
                                ctx: Dict[str, Any]):
        """
        # 基于多维度�则评估回复质量（0-100）�€?        维度：KB 命中强度、回复长度合理�€��€�实体�盖�€�画像�€�配�?        低于阈�€�时记录日志�?        """
        score = 60.0
        reasons = []

        # 1. KB 命中强度
        kb_mode = ctx.get("_kb_search_mode", "")
        kb_ctx = ctx.get("kb_context", "")
        if kb_ctx:
            score += 15
        else:
            score -= 10
            reasons.append("无KB命中")

        # 2. 回�长度合理性（������敷�，太长可能啰嗦）
        rlen = len(reply)
        intent = ctx.get("current_intent", "")
        if intent == "greeting":
            if 5 <= rlen <= 100:
                score += 5
        elif intent in ("order_query", "channel_info", "complaint"):
            if rlen < 15:
                score -= 15
                reasons.append("回�过短")
            elif 30 <= rlen <= 500:
                score += 10
            elif rlen > 800:
                score -= 5
                reasons.append("回�过长")
        else:
            if rlen < 10:
                score -= 10
                reasons.append("回�过短")

        # 3. 实体覆盖—�€�用户提到�单号/通道名，回����引用
        user_msg = (ctx.get("last_message") or "").upper()
        reply_upper = reply.upper()
        _channel_kw = {"EP", "JC", "JAZZ", "EASYPAISA"}
        for kw in _channel_kw:
            if kw in user_msg and kw not in reply_upper:
                score -= 8
                reasons.append(f"用户提及{kw}但回复未提及")
                break

        # 4. 画像适配—�€�frustrated 用户���得到安抚
        profile = ctx.get("_user_profile", {})
        if isinstance(profile, dict):
            tone = profile.get("tone", "standard")
            if tone in ("frustrated", "impatient"):
                comfort_kw = ("抱歉", "理解", "尽快", "麻烦", "对不起", "感谢您的耐心", "不好意思")
                if not any(k in reply for k in comfort_kw):
                    score -= 10
                    reasons.append(f"{tone}用户���到安�?")

        # 5. 空洞/模板回��€�?
        # 5. 空洞/模板回复检测
        _EMPTY_PATTERNS = ("如有其他问题", "请问还有什么", "希望以上信息")
        empty_count = sum(1 for p in _EMPTY_PATTERNS if p in reply)
        if empty_count >= 2 and rlen < 100:
            score -= 10
            reasons.append("模板化回�?")

        score = max(0, min(100, round(score)))
        ctx["_reply_quality"] = score

        if score < self._QUALITY_LOW_THRESHOLD:
            self.logger.warning(
                "[G4-LowQuality] user=%s score=%d intent=%s reasons=%s reply='%s'",
                user_id, score, intent, "|".join(reasons), reply[:80]
            )
            ctx["_low_quality_flag"] = True
            # F1: 分类����
            self._auto_fix_low_quality(ctx, reasons, intent)
        else:
            ctx.pop("_low_quality_flag", None)

    def _auto_fix_low_quality(self, ctx: Dict[str, Any],
                              reasons: list, intent: str):
        """F1: 低质量回复自动触发修复动作（异� fire-and-forget�?"""
        try:
            _kb = self._kb_store_if_exists()
            if not _kb:
                return
            user_msg = (ctx.get("last_message") or "").strip()
            ai_reply = (ctx.get("last_reply") or "").strip()
            if not user_msg:
                return

            has_kb = bool(ctx.get("kb_context"))
            if not has_kb and "无KB命中" in reasons:
                _kb.log_miss(user_msg)
                self.logger.info("[F1] 无KB命中低分 �?miss_log: '%s'", user_msg[:50])
            elif has_kb:
                _kb.add_feedback({
                    "user_message": user_msg[:200],
                    "ai_reply": ai_reply[:300],
                    "score": -1,
                    "correction": "",
                    "operator": "auto_quality",
                })
                self.logger.info("[F1] 有KB但低�?�?负面反�: '%s'", user_msg[:50])

            if "..." in reasons:
                _kb.add_feedback({
                    "user_message": user_msg[:200],
                    "ai_reply": ai_reply[:300],
                    "score": -1,
                    # "correction": "模板化回复，�€丰富内�",
                    "operator": "auto_quality",
                })
        except Exception as _e:
            self.logger.debug("F1 ����异常: %s", _e)

    # �€�€ J1: 智能人工升级 �€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€�€
    _ESCALATION_COOLDOWN_SEC = 3600  # 同一用户 1 小时内最多触�?1 �?
    def _check_escalation(self, user_id: str, ctx: Dict[str, Any],
                          chat_id: Any, now: float):
        """�?at_risk 用户连续���决时，触发人工升级�€�知�?
        触发条件（全部满足）�?          1. at_risk = True（满意度 < 40�?          2. 连续同意图追�?>= 3 �?          3. 距上次升�?> 1 小时
        """
        profile = ctx.get("_user_profile", {})
        if not profile.get("at_risk"):
            return
        consecutive = ctx.get("_consecutive_same_intent", 0)
        if consecutive < 3:
            return
        last_esc = self._escalation_cooldown.get(user_id, 0)
        if now - last_esc < self._ESCALATION_COOLDOWN_SEC:
            return

        self._escalation_cooldown[user_id] = now
        ctx["_escalation_triggered"] = True
        ctx["_escalation_ts"] = now

        sat = profile.get("satisfaction", 0)
        intent = ctx.get("current_intent", "unknown")
        last_msg = (ctx.get("last_message") or "")[:100]
        chat_title = ctx.get("chat_title", "")

        self.logger.warning(
            "[J1-Escalation] user=%s sat=%s intent=%s consecutive=%s chat=%s msg='%s'",
            user_id, sat, intent, consecutive, chat_title, last_msg
        )

        #通过 webhook 通知（异�?fire-and-forget�?
        try:
            import asyncio
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.create_task(self._fire_escalation_webhook(
                    user_id, chat_id, sat, intent, consecutive, last_msg, chat_title
                ))
        except Exception:
            pass

    async def _fire_escalation_webhook(self, user_id, chat_id, sat,
                                        intent, consecutive, last_msg, chat_title):
        """发�€�人工升�?webhook 通知（独立实现，不依�?admin.py�?"""
        try:
            cfg_dir = Path(self.config.config_path).parent if hasattr(self.config, "config_path") else Path("config")
            wh_path = cfg_dir / "webhook_settings.json"
            if not wh_path.exists():
                return
            wh_cfg = json.loads(wh_path.read_text(encoding="utf-8"))
            if not wh_cfg.get("enabled") or not wh_cfg.get("url"):
                return
            events = wh_cfg.get("events", [])
            if "escalation_needed" not in events and "config_change" not in events:
                return
            import httpx
            payload = json.dumps({
                "event": "escalation_needed",
                "actor": "system",
                "target": f"user:{user_id}",
                "summary": (
                    # f"�?人工升级请求\n" f"用户: {user_id}\n" f"群组: {chat_title or chat_id}\n" f"满意�? {sat}/100\n"
                    f"连续追问: {consecutive} �?({intent})\n"
                    # f"�€近消�? {last_msg}"
                ),
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, ensure_ascii=False)
            headers = {"Content-Type": "application/json"}
            async with httpx.AsyncClient(timeout=8) as client:
                await client.post(wh_cfg["url"], content=payload, headers=headers)
        except Exception as e:
            self.logger.debug("J1 升级 webhook 发�€�失�? %s", e)

    # ── R8: 危机人工接管/升级 ───────────────────────────────────────────
    _CRISIS_ESCALATION_COOLDOWN_SEC = 1800  # 危机比普通升级更急，30 分钟冷却

    def _maybe_escalate_crisis(
        self, *, user_id: str, chat_id: Any,
        user_context: Dict[str, Any], log_prefix: str = "",
    ) -> None:
        """R8：severe 危机连续命中 → 触发人工接管告警（复用既有 escalation webhook）。

        始终维护危机连击计数（severe 自增、非危机清零、elevated 维持）；仅当
        ``companion.wellbeing.crisis_escalation`` 开（默认关，需配 webhook + 真人值守）
        且连击 ≥ ``escalate_after``（默认 1）且过冷却时才真正告警。纯旁路，任何异常不影响回复。
        """
        try:
            level = str(user_context.get("_wellbeing_crisis_level", "") or "")
            streak = int(user_context.get("_wellbeing_crisis_streak", 0) or 0)
            if level == "severe":
                streak += 1
            elif level != "elevated":
                streak = 0
            user_context["_wellbeing_crisis_streak"] = streak
            # safety_override 是上一步(_apply_crisis_safety_net)的本轮信号，读后清零
            safety_override = bool(user_context.pop("_wellbeing_safety_override", False))

            _cfg = self.config.config if hasattr(self.config, "config") else {}
            _wb = (
                ((_cfg.get("companion") or {}).get("wellbeing") or {})
                if isinstance(_cfg, dict) else {}
            )
            wb_enabled = bool(_wb.get("enabled", True))

            escalated_now = False
            if wb_enabled and _wb.get("crisis_escalation", False) and level == "severe":
                escalate_after = max(1, int(_wb.get("escalate_after", 1)))
                if streak >= escalate_after:
                    now = time.time()
                    last = self._crisis_escalation_cooldown.get(str(user_id), 0.0)
                    if now - last >= self._CRISIS_ESCALATION_COOLDOWN_SEC:
                        self._crisis_escalation_cooldown[str(user_id)] = now
                        user_context["_crisis_escalation_triggered"] = True
                        user_context["_crisis_escalation_ts"] = now
                        escalated_now = True
                        self.logger.warning(
                            "%s[wellbeing] 危机人工接管触发 user=%s streak=%s"
                            "（已告警/待真人介入）",
                            log_prefix, user_id, streak,
                        )
                        try:
                            loop = asyncio.get_running_loop()
                            if loop.is_running():
                                loop.create_task(self._fire_crisis_webhook(
                                    user_id, chat_id, streak,
                                    str(user_context.get("chat_title", "") or ""),
                                ))
                        except RuntimeError:
                            pass

            # R9 审计落库（独立于升级开关；默认关，由 crisis_audit 控制）
            if (
                wb_enabled and _wb.get("crisis_audit", False)
                and level in ("severe", "elevated")
                and getattr(self, "_crisis_store", None) is not None
            ):
                try:
                    self._crisis_store.record(
                        user_id=str(user_id), chat_id=str(chat_id), level=level,
                        category=str(user_context.get("_wellbeing_crisis_category", "") or ""),
                        streak=streak, escalated=escalated_now,
                        safety_override=safety_override,
                        excerpt=str(user_context.get("last_message", "") or ""),
                    )
                except Exception:
                    self.logger.debug("[wellbeing] crisis audit record failed", exc_info=True)
        except Exception:
            self.logger.debug("[wellbeing] crisis escalation skipped", exc_info=True)

    async def _fire_crisis_webhook(self, user_id, chat_id, streak, chat_title):
        """危机告警 webhook（复用 escalation_needed 事件通道，附 category=crisis）。"""
        try:
            cfg_dir = (
                Path(self.config.config_path).parent
                if hasattr(self.config, "config_path") else Path("config")
            )
            wh_path = cfg_dir / "webhook_settings.json"
            if not wh_path.exists():
                return
            wh_cfg = json.loads(wh_path.read_text(encoding="utf-8"))
            if not wh_cfg.get("enabled") or not wh_cfg.get("url"):
                return
            events = wh_cfg.get("events", [])
            if "escalation_needed" not in events and "config_change" not in events:
                return
            import httpx
            payload = json.dumps({
                "event": "escalation_needed",
                "category": "crisis",
                "severity": "high",
                "actor": "system",
                "target": f"user:{user_id}",
                "summary": (
                    f"⚠️ 危机人工接管请求\n"
                    f"用户: {user_id}\n群组: {chat_title or chat_id}\n"
                    f"连续危机信号: {streak} 次（疑似自伤/轻生）\n"
                    f"请尽快人工介入。"
                ),
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, ensure_ascii=False)
            headers = {"Content-Type": "application/json"}
            async with httpx.AsyncClient(timeout=8) as client:
                await client.post(wh_cfg["url"], content=payload, headers=headers)
        except Exception as e:
            self.logger.debug("R8 危机 webhook 发送失败: %s", e)

    def _cleanup_cache(self):
        """清理过期的缓存条�?"""
        current_time = time.time()
        expire_time = current_time - 3600
        
        keys_to_remove = [k for k, ts in self.reply_cache.items() if ts < expire_time]
        for key in keys_to_remove:
            del self.reply_cache[key]

        cu_expire = current_time - 300
        cu_stale = [k for k, ts in self._chat_user_last_reply.items() if ts < cu_expire]
        for k in cu_stale:
            del self._chat_user_last_reply[k]

        if len(self._user_locks) > 200:
            unlocked = [k for k, v in self._user_locks.items() if not v.locked()]
            for k in unlocked[:100]:
                del self._user_locks[k]
    
    async def cleanup(self):
        """清理资源"""
        self._context_store.flush_all()
        self._context_store.close()
        if getattr(self, "_episodic_store", None):
            try:
                self._episodic_store.close()
            except Exception:
                pass
        self.logger.info("...")
# ==================== Generic Skills ====================
# Skill base class is imported from src.skills.base


class GreetingSkill(Skill):
    """问候处理技能 — 支持 S1 列表配置（跳过 AI 秒回）和标准 AI 回复"""

    def __init__(self, config, ai_client):
        super().__init__(config, ai_client)
        self.priority = 1

    _IDENTITY_KW = (
        "你是谁", "你是什么", "哪个客服", "什么客服", "介绍一下", "你们是机器人", "你是ai",
        "who are you", "are you a bot", "are you ai", "are you real",
    )

    async def execute(self, text: str, user_id: str, context: Dict[str, Any]) -> Optional[str]:
        strategy = context.get('_reply_strategy', {})
        _txt = (text or '').lower()
        _is_identity = any(k in _txt for k in self._IDENTITY_KW)
        _lang = (context or {}).get('reply_lang', 'zh')

        if strategy.get('skip_ai') and not context.get('kb_context') and not _is_identity:
            return self._kb_fallback('greeting', lang=_lang)

        so = {}
        if 'temperature' in strategy:
            so['temperature'] = strategy['temperature']
        raw_tokens = strategy.get('max_tokens', 0)
        if isinstance(raw_tokens, (int, float)) and raw_tokens < 256:
            so['max_tokens'] = 256
        elif raw_tokens:
            so['max_tokens'] = int(raw_tokens)
        if 'context_rounds' in strategy:
            so['context_rounds'] = strategy['context_rounds']

        try:
            reply = await self.ai_client.generate_reply_with_intent(
                user_message=text, intent='greeting',
                user_context=context, strategy_overrides=so or None
            )
            if reply:
                return reply
        except Exception as e:
            self.logger.warning(f"AI生成问候回复失败: {e}")

        return self._kb_fallback('greeting', lang=_lang)


class ComplaintSkill(Skill):
    """投诉处理技能"""

    def __init__(self, config, ai_client):
        super().__init__(config, ai_client)
        self.priority = 7

    async def execute(self, text: str, user_id: str, context: Dict[str, Any]) -> Optional[str]:
        _lang = (context or {}).get('reply_lang', 'zh')
        try:
            reply = await self.ai_client.generate_reply_with_intent(
                text, 'complaint',
                context,
                strategy_overrides=self._get_strategy_overrides(context)
            )
            if reply:
                return reply
        except Exception as e:
            self.logger.warning(f"AI生成投诉处理回复失败: {e}")

        return self._kb_fallback('complaint', lang=_lang)


class SmallTalkSkill(Skill):
    """闲聊技能 — 支持 S5 静默观察（概率不回复由 SkillManager 控制）"""

    def __init__(self, config, ai_client):
        super().__init__(config, ai_client)
        self.priority = 8

    async def execute(self, text: str, user_id: str, context: Dict[str, Any]) -> Optional[str]:
        strategy = context.get('_reply_strategy', {})
        _lang = (context or {}).get('reply_lang', 'zh')

        if strategy.get('skip_ai') and not context.get('kb_context'):
            return self._kb_fallback('small_talk', lang=_lang)

        try:
            reply = await self.ai_client.generate_reply_with_intent(
                user_message=text, intent='small_talk',
                user_context=context,
                strategy_overrides=self._get_strategy_overrides(context)
            )
            if reply:
                return reply
        except Exception as e:
            self.logger.warning(f"AI生成闲聊回复失败: {e}")

        return self._kb_fallback('small_talk', lang=_lang)


class TestSkill(Skill):
    """测试功能技能"""

    def __init__(self, config, ai_client):
        super().__init__(config, ai_client)
        self.priority = 2

    async def execute(self, text: str, user_id: str, context: Dict[str, Any]) -> Optional[str]:
        if "测试" not in text and "test" not in text.lower():
            return None

        try:
            reply = await self.ai_client.generate_reply_with_intent(
                text, 'test',
                context,
                strategy_overrides=self._get_strategy_overrides(context)
            )
            if reply:
                return reply
        except Exception as e:
            self.logger.warning(f"AI生成测试回复失败: {e}")

        r = self._kb_reply('test_reply')
        if r:
            return r
        return self._kb_fallback('test')


# 供测试与外部统一从 skill_manager 导入（实现仍在 domains.payment.skills.enhanced_quota_config）
from domains.payment.skills.enhanced_quota_config import EnhancedQuotaConfigSkill  # noqa: E402
