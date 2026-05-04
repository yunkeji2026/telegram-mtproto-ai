"""ContactsSubsystem 启动组装。

main.py 只需调 `bootstrap_contacts_subsystem(config, cfg_dir)` 拿回 ContactsSubsystem
或 None（feature flag 关时）。Web 挂载 / runner 注入 / 定时任务都基于返回值做。

配置示例（config.yaml）::

    contacts:
      enabled: false                  # 总开关，默认关
      db_path: config/contacts.db     # 可选
      daily_cap: 15
      global_cap: 0                   # 0=不启用全局
      token_ttl_hours: 72
      readiness_threshold: 70
      min_silent_days: 3
      min_intimacy_for_reactivation: 40
      scripts_path: config/handoff_scripts.yaml
      compliance_path: config/handoff_compliance.yaml
      line_ids_by_account:
        acc-A: '@handle_A'
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .gateway import ContactGateway
from .handoff import HandoffTokenService
from .journey_fsm import apply_silence_decay
from .merge import MergeService
from .rpa_hooks import GatewayContactHooks
from .store import ContactStore

logger = logging.getLogger(__name__)


@dataclass
class ContactsSubsystem:
    """所有相关服务的单例容器。"""
    store: ContactStore
    handoff_svc: HandoffTokenService
    merge_svc: MergeService
    gateway: ContactGateway
    hooks: GatewayContactHooks
    renderer: Any = None          # HandoffRenderer or None
    compliance: Any = None        # HandoffComplianceChecker or None
    limiter: Any = None           # AccountLimiter or None
    readiness_scorer: Any = None  # HandoffReadinessScorer or None
    intimacy_engine: Any = None   # IntimacyEngine or None
    reactivation: Any = None      # ReactivationScheduler or None
    config_snapshot: Dict[str, Any] = None   # 启动时的配置，Web 可查
    # W4-定时：后台 asyncio 任务集合（start_background_tasks 后填充）
    _bg_tasks: list = field(default_factory=list)

    def close(self) -> None:
        self.stop_background_tasks()
        try:
            self.store.close()
        except Exception:
            pass

    # ── W4-Cap-Alert：把告警回调接到项目已有的 WebhookNotifier ─────
    def wire_cap_alert_webhook(self, notifier: Any) -> bool:
        """main.py 在 WebhookNotifier 就绪后调：把 cap 阈值事件转发到 webhook。

        返回 True 表示真的接上了；False 表示 limiter 不存在或阈值配置为空。
        """
        lim = self.limiter
        if lim is None:
            return False
        thresholds = getattr(lim, "_thresholds", None) or []
        if not thresholds:
            return False
        if notifier is None or not getattr(notifier, "enabled", False):
            logger.info(
                "contacts cap_alert: webhook 未启用，阈值检测 no-op"
            )
            return False

        def _cb(account_id: str, pct: int, count: int, cap: int) -> None:
            try:
                notifier.notify("contacts.cap_alert", {
                    "account_id": account_id,
                    "pct": pct,
                    "count": count,
                    "cap": cap,
                })
            except Exception:
                logger.debug(
                    "cap_alert webhook dispatch failed", exc_info=True)

        lim.set_on_threshold_crossed(_cb)
        logger.info(
            "contacts cap_alert 已接到 webhook：thresholds=%s", thresholds)
        return True

    # ── W4-Hooks-Flag：按 channel 查 hook 是否应接入 ─────
    def is_rpa_hook_enabled(self, channel: str) -> bool:
        """main.py 用这个判断某路 runner 是否要接 ContactHooks。

        config 格式：
            contacts:
              rpa_hooks:
                messenger: true   # 默认 true（当前行为）
                line: true

        未配置时按 true（保持向后兼容）；显式 `false` 才跳过。
        """
        flags = (self.config_snapshot or {}).get("rpa_hooks") or {}
        key = (channel or "").strip().lower()
        if key not in flags:
            return True
        return bool(flags.get(key))

    # ── W4 定时任务 ────────────────────────────────────────
    def start_background_tasks(self) -> None:
        """启动 decay 等周期任务；按 config_snapshot 里的参数决定是否跑。

        - `decay_interval_minutes` (默认 30, 0=关)：周期性把沉默超期的 journey
          降到 LOST_* 状态；跑在 asyncio.to_thread，不会阻塞事件循环。

        幂等：重复调用只会忽略已在跑的任务。
        """
        if self._bg_tasks:
            return
        cfg = self.config_snapshot or {}
        try:
            interval_min = int(cfg.get("decay_interval_minutes", 30) or 0)
        except (TypeError, ValueError):
            interval_min = 30
        if interval_min <= 0:
            logger.info("contacts 后台任务：decay_interval_minutes=0，已禁用")
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "contacts 后台任务：当前无运行中的 event loop，跳过启动")
            return
        t = loop.create_task(
            self._decay_loop(interval_sec=interval_min * 60),
            name="contacts-silence-decay",
        )
        self._bg_tasks.append(t)
        logger.info(
            "contacts 后台任务：silence_decay 每 %d 分钟跑一次", interval_min)

    def stop_background_tasks(self) -> None:
        for t in list(self._bg_tasks):
            if not t.done():
                t.cancel()
        self._bg_tasks.clear()

    async def _decay_loop(self, *, interval_sec: int) -> None:
        # 启动后延迟 60s 再跑第一次，避免和 bootstrap/首屏竞争
        try:
            await asyncio.sleep(min(60, interval_sec))
        except asyncio.CancelledError:
            return
        while True:
            try:
                count = await asyncio.to_thread(apply_silence_decay, self.store)
                if count > 0:
                    logger.info(
                        "contacts silence_decay 迭代完成：降级 %d 个 journey",
                        count)
                else:
                    logger.debug("contacts silence_decay 空跑（0 个）")
            except asyncio.CancelledError:
                return
            except Exception:
                logger.warning(
                    "contacts silence_decay 异常（继续跑下一轮）",
                    exc_info=True)
            try:
                await asyncio.sleep(interval_sec)
            except asyncio.CancelledError:
                return

    def health(self) -> Dict[str, Any]:
        """各可选服务是否就绪 + 触发异常的原因（启动时抓下来）。运营能看。"""
        return {
            "enabled": True,
            "services": {
                "renderer": self.renderer is not None,
                "compliance": self.compliance is not None,
                "limiter": self.limiter is not None,
                "intimacy_engine": self.intimacy_engine is not None,
                "readiness_scorer": self.readiness_scorer is not None,
                "reactivation": self.reactivation is not None,
            },
            "config_snapshot": dict(self.config_snapshot or {}),
            "db_path": str(getattr(self.store, "_db_path", "")),
        }


def bootstrap_contacts_subsystem(
    config: Any,
    cfg_dir: Path,
) -> Optional[ContactsSubsystem]:
    """如果 config.contacts.enabled 为 true，组装全部服务并返回。否则 None。

    config 可以是 ConfigManager 也可以是 dict 风格（有 `.config` 属性或 `.get()`）。
    """
    contacts_cfg = _get_contacts_cfg(config)
    if not contacts_cfg:
        return None
    if not bool(contacts_cfg.get("enabled")):
        logger.info("contacts subsystem disabled by config (contacts.enabled=false)")
        return None

    cfg_dir = Path(cfg_dir)
    db_path = Path(contacts_cfg.get("db_path") or (cfg_dir / "contacts.db"))
    if not db_path.is_absolute():
        db_path = cfg_dir / db_path

    # ── 核心三件套 ────────────────────────────────────
    store = ContactStore(db_path=db_path)
    token_ttl_s = int(contacts_cfg.get("token_ttl_hours") or 72) * 3600
    handoff_svc = HandoffTokenService(store, ttl_seconds=token_ttl_s)
    merge_svc = MergeService(store)

    # ── 业务服务（按可用性装） ───────────────────────
    renderer = _safe_init_renderer(cfg_dir, contacts_cfg)
    compliance = _safe_init_compliance(cfg_dir, contacts_cfg)
    limiter = _safe_init_limiter(store, contacts_cfg)
    intimacy_engine = _safe_init_intimacy(store)
    readiness_scorer = _safe_init_scorer(store, intimacy_engine, contacts_cfg)
    reactivation = _safe_init_reactivation(store, contacts_cfg)

    # ── line_id 查询回调 ─────────────────────────────
    line_ids_map = contacts_cfg.get("line_ids_by_account") or {}
    default_line_id = contacts_cfg.get("default_line_id") or "@our_line"

    def _line_id_provider(account_id: str) -> str:
        return line_ids_map.get(account_id, default_line_id)

    gateway = ContactGateway(
        store, handoff_svc, merge_svc,
        renderer=renderer, limiter=limiter, compliance=compliance,
        readiness_scorer=readiness_scorer,
        line_id_provider=_line_id_provider,
    )
    # ★ W3-D1.1：把 intimacy_engine 接入 gateway，让 msg_in 自动 refresh intimacy_score
    # 修复 bug：之前 intimacy_engine 是孤儿组件，所有 chat 的 intimacy 永远 0
    if intimacy_engine is not None:
        try:
            gateway.set_intimacy_engine(intimacy_engine)
            # 用 warning 级别确保被根 logger（最低 WARNING）抓到 → 写入 app.log
            logger.warning(
                "ContactGateway 已接入 intimacy_engine（msg_in 自动 refresh）"
            )
        except Exception:
            logger.warning("set_intimacy_engine 失败", exc_info=True)
    # W4-Handoff-Auto-Inject：hooks 按 config 决定是否允许主动触发
    auto_inject_cfg = contacts_cfg.get("handoff_auto_inject") or {}
    hooks = GatewayContactHooks(
        gateway,
        auto_inject_enabled=bool(auto_inject_cfg.get("enabled", False)),
        inject_separator=str(auto_inject_cfg.get("separator") or "\n\n"),
    )

    logger.info(
        "contacts subsystem bootstrapped: db=%s daily_cap=%s ttl=%sh readiness=%s",
        db_path, contacts_cfg.get("daily_cap"),
        contacts_cfg.get("token_ttl_hours", 72),
        contacts_cfg.get("readiness_threshold"),
    )
    return ContactsSubsystem(
        store=store,
        handoff_svc=handoff_svc,
        merge_svc=merge_svc,
        gateway=gateway,
        hooks=hooks,
        renderer=renderer,
        compliance=compliance,
        limiter=limiter,
        readiness_scorer=readiness_scorer,
        intimacy_engine=intimacy_engine,
        reactivation=reactivation,
        config_snapshot=dict(contacts_cfg),
    )


# ── 内部工厂 ────────────────────────────────────────
def _get_contacts_cfg(config: Any) -> Dict[str, Any]:
    """兼容 ConfigManager / dict / plain 对象。"""
    if config is None:
        return {}
    root: Any = None
    # ConfigManager 风格：.config 是 dict
    if hasattr(config, "config") and isinstance(config.config, dict):
        root = config.config
    elif isinstance(config, dict):
        root = config
    else:
        try:
            root = config.get("") or {}
        except Exception:
            return {}
    return (root or {}).get("contacts") or {}


def _safe_init_renderer(cfg_dir: Path, contacts_cfg: Dict[str, Any]):
    scripts_path = contacts_cfg.get("scripts_path") or "config/handoff_scripts.yaml"
    p = Path(scripts_path)
    if not p.is_absolute():
        p = cfg_dir.parent / p if cfg_dir.name == "config" else cfg_dir / p
    # config/ 下也是常见位置
    if not p.exists():
        alt = cfg_dir / Path(scripts_path).name
        if alt.exists():
            p = alt
    try:
        from src.skills.handoff_renderer import HandoffRenderer
        return HandoffRenderer(p)
    except Exception as e:
        logger.warning("HandoffRenderer init skipped: %s", e)
        return None


def _safe_init_compliance(cfg_dir: Path, contacts_cfg: Dict[str, Any]):
    comp_path = contacts_cfg.get("compliance_path") or "config/handoff_compliance.yaml"
    p = Path(comp_path)
    if not p.is_absolute():
        p = cfg_dir.parent / p if cfg_dir.name == "config" else cfg_dir / p
    if not p.exists():
        alt = cfg_dir / Path(comp_path).name
        if alt.exists():
            p = alt
    try:
        from src.skills.handoff_compliance import HandoffComplianceChecker
        return HandoffComplianceChecker(config_path=p)
    except Exception as e:
        logger.warning("HandoffComplianceChecker init skipped: %s", e)
        return None


def _safe_init_limiter(store, contacts_cfg: Dict[str, Any]):
    try:
        from src.skills.account_limiter import AccountLimiter
        # W4-Cap-Alert：config 里读阈值；callback 由 main.py 后置注入
        alert_cfg = contacts_cfg.get("cap_alert") or {}
        thresholds = list(alert_cfg.get("thresholds_pct") or [])
        if not bool(alert_cfg.get("enabled", False)):
            thresholds = []
        return AccountLimiter(
            store,
            daily_cap=int(contacts_cfg.get("daily_cap") or 15),
            global_cap=int(contacts_cfg.get("global_cap") or 0),
            alert_thresholds_pct=thresholds,
        )
    except Exception as e:
        logger.warning("AccountLimiter init skipped: %s", e)
        return None


def _safe_init_intimacy(store):
    try:
        from src.skills.intimacy_engine import IntimacyEngine
        return IntimacyEngine(store)
    except Exception as e:
        logger.warning("IntimacyEngine init skipped: %s", e)
        return None


def _safe_init_scorer(store, intim, contacts_cfg: Dict[str, Any]):
    if intim is None:
        return None
    try:
        from src.skills.handoff_readiness import HandoffReadinessScorer
        return HandoffReadinessScorer(
            store, intim,
            turn_saturation=int(contacts_cfg.get("turn_saturation") or 3),
            open_threshold=float(contacts_cfg.get("readiness_threshold") or 70),
            llm_rapport_threshold=int(
                contacts_cfg.get("llm_rapport_threshold") or 65),
            llm_min_turns=int(
                contacts_cfg.get("llm_min_turns") or 5),
        )
    except Exception as e:
        logger.warning("HandoffReadinessScorer init skipped: %s", e)
        return None


def _safe_init_reactivation(store, contacts_cfg: Dict[str, Any]):
    try:
        from src.skills.reactivation_scheduler import ReactivationScheduler
        # ★ W2-D7.6：active_stages 可配置（默认含 ENGAGED + LINE_*；可在 config 收紧）
        cand_stages = contacts_cfg.get("reactivation_active_stages")
        if isinstance(cand_stages, list) and cand_stages:
            active_stages = [str(s).strip().upper() for s in cand_stages if s]
        else:
            active_stages = None  # 走 scheduler 默认（含 ENGAGED）
        return ReactivationScheduler(
            store,
            min_silent_days=float(contacts_cfg.get("min_silent_days") or 3),
            min_intimacy=float(contacts_cfg.get("min_intimacy_for_reactivation") or 40),
            cooldown_days=float(contacts_cfg.get("reactivation_cooldown_days") or 7),
            active_stages=active_stages,
        )
    except Exception as e:
        logger.warning("ReactivationScheduler init skipped: %s", e)
        return None
