"""W3-3H：reunion 草稿 prompt 加载器 + 渲染 + variant 路由。

设计要点：
  - **配置驱动**：``config/reunion_prompts.yaml`` 按 ``variant × lang`` 索引
  - **inline default 兜底**：config 缺失或损坏时仍能跑（不会因 yaml 错误把 3F/3G 弄废）
  - **persona 注入**：模板里的 ``{persona_name}`` 等占位符在渲染期被填充
  - **A/B 路由**：``select_variant(jid)`` 用 ``hash(jid) % len(variants)`` 做
    deterministic 50/50（同一 journey 始终拿同一 variant，便于追踪）
  - **safe_format**：模板字段缺失时不抛 KeyError，留空白避免运行时 500
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

# 内置 fallback：config 完全失败时仍可工作（与 3G 之前的 inline prompt 等价）
_INLINE_DEFAULT: Dict[str, Dict[str, str]] = {
    "v1": {
        "zh": (
            "你是一位陪伴型 AI 助手。下面这位朋友很久没主动找你了，"
            "请帮我起一条 30 字以内的中文短消息，给运营审核后发给对方。\n\n"
            "【情境数据】沉默 {silent_days} 天；功能阶段曾达 {funnel_stage}；"
            "近期亲密度衰减到 {intim:.0f}/100。\n"
            "{last_inbound_block}"
            "\n【要求】\n"
            "- 像久违的朋友重逢，自然问候\n"
            "- 不要直接接续上次话题、不要撒娇梗、不要刻意热络\n"
            "- 先关心「最近怎么样」，给对方主导节奏\n"
            "- emoji 最多 1 个，不要署名\n"
            "- 只输出消息正文，不要任何解释或引号"
        ),
        "en": (
            "You're a companion AI assistant. The following friend hasn't "
            "reached out in a while. Draft a casual reach-out message "
            "(≤25 words) for operator review.\n\n"
            "[Context] silent {silent_days} days; funnel stage was {funnel_stage}; "
            "recent intimacy decayed to {intim:.0f}/100.\n"
            "{last_inbound_block}"
            "\n[Rules]\n"
            "- Like an old friend reconnecting; warm but not clingy\n"
            "- Don't continue past topics; don't be flirty or over-eager\n"
            "- Ask how they've been; let them lead\n"
            "- ≤1 emoji; no signature\n"
            "- Output ONLY the message body, no quotes or explanation"
        ),
        "ja": (
            "あなたは寄り添い型のAIアシスタントです。下記の相手は長らく"
            "連絡をくれていません。運営レビュー用に30字以内の短いメッセージを起案してください。\n\n"
            "【状況】沈黙 {silent_days} 日；機能ステージ {funnel_stage} まで到達；"
            "最近の親密度は {intim:.0f}/100 まで減衰。\n"
            "{last_inbound_block}"
            "\n【要件】\n"
            "- 久しぶりの友達のような自然な挨拶\n"
            "- 前の話題を続けない；甘えすぎず、押しつけがましくない\n"
            "- 「最近どう？」のように相手にペースを譲る\n"
            "- 絵文字は最大1つ；署名なし\n"
            "- 本文のみ出力。引用符・説明文は不要"
        ),
    },
}
_INLINE_DEFAULT_VARIANT = "v1"


def _config_path() -> Path:
    """优先级：env REUNION_PROMPTS_PATH > 项目内 config/reunion_prompts.yaml。"""
    env = os.environ.get("REUNION_PROMPTS_PATH", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent.parent / "config" / "reunion_prompts.yaml"


class ReunionPromptRegistry:
    """加载 + 缓存 + 渲染 prompts。线程安全（只读，加载一次）。"""

    def __init__(self, *, path: Optional[Path] = None) -> None:
        self._path = path or _config_path()
        self._variants: Dict[str, Dict[str, str]] = {}
        self._default_variant: str = _INLINE_DEFAULT_VARIANT
        self._loaded_mtime: float = 0.0   # W3-3I.3：用于 hot-reload
        self._load_lock = threading.Lock()
        self._load()

    def _current_mtime(self) -> float:
        try:
            return self._path.stat().st_mtime
        except (OSError, FileNotFoundError):
            return 0.0

    def maybe_reload(self) -> bool:
        """W3-3I.3：检测 yaml mtime 变化并热重载。返回是否真的重载了。

        线程安全：load_lock 保证多请求并发时只重载一次。
        失败时静默 —— 老配置继续生效（不会因为运营改坏 yaml 把生产打挂）。
        """
        cur = self._current_mtime()
        if cur == 0.0 or cur <= self._loaded_mtime:
            return False
        with self._load_lock:
            # double-check：避免多线程同时重载
            if cur <= self._loaded_mtime:
                return False
            old_variants = dict(self._variants)
            old_default = self._default_variant
            try:
                self._load()
                logger.info(
                    "reunion prompts hot-reloaded: %d variants (default=%s)",
                    len(self._variants), self._default_variant,
                )
                return True
            except Exception as e:
                # 极端情况 _load 抛了：回滚到老的，记录错误
                logger.warning("reunion prompts hot-reload failed: %s", e)
                self._variants = old_variants
                self._default_variant = old_default
                return False

    def _load(self) -> None:
        # W3-3I.3：每次 _load 结束（无论成功 / 兜底）都记录 mtime，避免
        # 反复重试加载坏 yaml；运营修好之后 mtime 会再变 → 触发重载
        self._loaded_mtime = self._current_mtime()
        try:
            if not self._path.exists():
                logger.info(
                    "reunion prompts config not found at %s; using inline default",
                    self._path,
                )
                self._variants = dict(_INLINE_DEFAULT)
                return
            with open(self._path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            variants = data.get("variants") or {}
            if not isinstance(variants, dict) or not variants:
                logger.warning(
                    "reunion prompts: variants block missing/empty in %s; using inline",
                    self._path,
                )
                self._variants = dict(_INLINE_DEFAULT)
                return
            # 浅校验：每个 variant 至少要有 zh（兜底语言）
            cleaned: Dict[str, Dict[str, str]] = {}
            for vid, langs in variants.items():
                if not isinstance(langs, dict):
                    continue
                if not langs.get("zh"):
                    logger.warning(
                        "reunion prompts: variant %r missing 'zh' lang; skipping",
                        vid,
                    )
                    continue
                cleaned[str(vid)] = {
                    k: str(v) for k, v in langs.items()
                    if isinstance(v, str) and v.strip()
                }
            if not cleaned:
                logger.warning(
                    "reunion prompts: no valid variants in %s; using inline",
                    self._path,
                )
                self._variants = dict(_INLINE_DEFAULT)
                return
            self._variants = cleaned
            self._default_variant = str(
                data.get("default_variant", "v1")
            )
            if self._default_variant not in self._variants:
                # default 不存在 → 退到第一个可用变体
                self._default_variant = next(iter(self._variants.keys()))
            logger.info(
                "reunion prompts loaded: %d variants from %s",
                len(self._variants), self._path,
            )
        except Exception as e:
            logger.warning(
                "reunion prompts load failed (%s); using inline default", e,
            )
            self._variants = dict(_INLINE_DEFAULT)

    @property
    def variants(self) -> List[str]:
        return list(self._variants.keys())

    @property
    def default_variant(self) -> str:
        return self._default_variant

    def promote_default_variant(self, variant_id: str) -> bool:
        """W3-3J.1：把 ``variant_id`` 写成 yaml 的 ``default_variant``。

        安全约束：
          - ``variant_id`` 必须在当前 loaded variants 里（防拼写错误把 yaml 写坏）
          - 写操作原子性：先写 tmp 文件再 rename（防进程中途崩导致 yaml 损坏）
          - 文件不存在（inline-only 模式）时返回 False，不写

        成功后强制 reload，让 ``select_variant`` 立即用新 default 路由。
        返回 True=成功写入并 reload；False=文件不存在或 variant 不合法。
        """
        if variant_id not in self._variants:
            logger.warning(
                "promote_default_variant: variant %r not found in %s; skip",
                variant_id, list(self._variants.keys()),
            )
            return False
        if not self._path.exists():
            logger.warning(
                "promote_default_variant: config file %s not found; "
                "cannot persist (inline-only mode)", self._path,
            )
            return False
        with self._load_lock:
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                data["default_variant"] = variant_id
                # 原子写：tmp → rename
                tmp = self._path.with_suffix(".yaml.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
                tmp.replace(self._path)
                # 强制 reload（会更新 _loaded_mtime 防止 maybe_reload 重复跑）
                self._load()
                logger.info(
                    "reunion prompts: promoted default_variant → %r", variant_id,
                )
                return True
            except Exception as e:
                logger.error("promote_default_variant failed: %s", e)
                return False

    def select_variant(self, journey_id: str) -> str:
        """W3-3H.3：deterministic A/B 路由。

        相同 journey 永远拿到相同 variant，便于追踪「这个用户曾收到 v2 prompt」。
        变体数量变化时分配会洗牌——这是预期的（A/B 框架重置）。
        """
        if not self._variants:
            return _INLINE_DEFAULT_VARIANT
        keys = sorted(self._variants.keys())
        # 用 SHA-256 而不是 hash() —— Python `hash()` 在不同进程会变，破坏路由
        h = hashlib.sha256(journey_id.encode("utf-8")).digest()
        idx = int.from_bytes(h[:4], "big") % len(keys)
        return keys[idx]

    def render(
        self,
        *,
        variant: str,
        lang: str,
        persona_name: str = "",
        persona_role: str = "",
        forbidden_phrases: Optional[List[str]] = None,
        silent_days: int = 0,
        funnel_stage: str = "",
        intim: float = 0.0,
        last_inbound: str = "",
    ) -> Tuple[str, str, str]:
        """渲染 prompt。返回 ``(prompt, resolved_variant, resolved_lang)``。

        resolved_* 反映实际用了哪个 variant/lang（用户传 ``v3`` 不存在 → 落 default）。
        """
        # ── 解析 variant ─────────
        if variant not in self._variants:
            variant = self._default_variant
        variant_block = self._variants.get(variant) or {}
        # ── 解析 lang ────────────
        if lang not in variant_block:
            # variant 内 lang 不存在 → 兜底 zh
            lang = "zh" if "zh" in variant_block else next(iter(variant_block.keys()))
        template = variant_block.get(lang) or ""
        if not template:
            # 极端情况：连 zh 都没了 → 用 inline 兜底
            template = _INLINE_DEFAULT["v1"].get(lang) or _INLINE_DEFAULT["v1"]["zh"]
            variant = "v1_fallback"
        # ── 构造 last_inbound block（可能为空）─────
        if last_inbound:
            if lang == "en":
                last_inbound_block = f"[Last message from them] {last_inbound}\n"
            elif lang == "ja":
                last_inbound_block = f"【相手の最後の一言】{last_inbound}\n"
            else:
                last_inbound_block = f"【对方最后一句话】{last_inbound}\n"
        else:
            last_inbound_block = ""
        # ── 构造 forbidden block ──
        forbidden = (forbidden_phrases or [])[:6]  # 限 6 条避免 prompt 膨胀
        if forbidden:
            joined = "、".join(f"「{p}」" for p in forbidden)
            if lang == "en":
                forbidden_block = f"- Do NOT use these phrases: {joined}\n"
            elif lang == "ja":
                forbidden_block = f"- 以下の表現は使わない：{joined}\n"
            else:
                forbidden_block = f"- 不要使用以下表述：{joined}\n"
        else:
            forbidden_block = ""
        # ── 准备 persona 默认值 ──
        if not persona_name:
            persona_name = {"en": "you", "ja": "あなた"}.get(lang, "你")
        if not persona_role:
            persona_role = {
                "en": "companion AI",
                "ja": "寄り添い型AI",
            }.get(lang, "陪伴型 AI 助手")
        # ── safe format（缺字段不抛错）──
        try:
            prompt = template.format(
                persona_name=persona_name,
                persona_role=persona_role,
                silent_days=silent_days,
                funnel_stage=funnel_stage or "INITIAL",
                intim=float(intim),
                last_inbound_block=last_inbound_block,
                forbidden_block=forbidden_block,
            )
        except (KeyError, IndexError, ValueError) as e:
            logger.warning("reunion prompt format failed (%s); using inline", e)
            template = _INLINE_DEFAULT["v1"].get(lang) or _INLINE_DEFAULT["v1"]["zh"]
            prompt = template.format(
                silent_days=silent_days,
                funnel_stage=funnel_stage or "INITIAL",
                intim=float(intim),
                last_inbound_block=last_inbound_block,
            )
            variant = "v1_fallback"
        return prompt, variant, lang


# Singleton（避免每个请求都重读 yaml）
_registry: Optional[ReunionPromptRegistry] = None


def hash_prompt(prompt: str) -> str:
    """W3-3I.5：把 prompt 文本算 stable hash（前 16 hex char）。

    用于 ``draft_log.prompt_snapshot_hash``，便于运营在 yaml 改了之后
    回溯「这条 draft 当时用的是哪段 prompt 文本」。
    16 hex char = 8 字节熵，对回溯场景足够（不是密码学场景）。
    """
    if not prompt:
        return ""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def get_registry() -> ReunionPromptRegistry:
    """W3-3H：取 singleton；W3-3I.3：每次取时检查 yaml mtime 看要不要 reload。

    mtime 检查只 stat() 一次（廉价），不会拖慢 hot path。
    """
    global _registry
    if _registry is None:
        _registry = ReunionPromptRegistry()
    else:
        _registry.maybe_reload()
    return _registry


def reset_registry() -> None:
    """W3-3H：测试用 — 强制重新加载 yaml。"""
    global _registry
    _registry = None


def load_persona_for_prompt(*, journey=None) -> Tuple[str, str, List[str]]:
    """W3-3H.1 + W3-3I.2：从 ``PersonaManager`` 取活跃 persona 给 prompt 用。

    解析顺序（PersonaManager 内部 3-tier）：
      1. ``journey.persona_id`` 作为 ``account_persona_id`` → profile store 命中
      2. ``set_domain_persona``（运营在 web 后台改的默认人设）
      3. ``_DEFAULT_PERSONA``（hardcoded "Assistant"）

    返回 ``(name, role, forbidden_phrases)``。任意环节挂掉退到空字符串
    （render() 会用 lang-aware fallback 默认值）。
    """
    try:
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        # journey.persona_id 如果设置了，作为 account_persona_id 传入
        # （reactivation 场景没具体 chat_id；journey 级 persona 是次优解）
        account_pid = ""
        if journey is not None:
            account_pid = (getattr(journey, "persona_id", "") or "").strip()
        p = pm.get_persona("", account_pid)
        name = (p.get("name") or "").strip()
        role = (p.get("role") or "").strip()
        forbidden = list((p.get("speaking") or {}).get("forbidden_phrases") or [])
        return name, role, forbidden
    except Exception as e:
        logger.debug("persona pull for prompt failed: %s", e)
        return "", "", []
