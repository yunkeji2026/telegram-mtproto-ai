"""
同一问题多次重复 → 追加 @人工客服 文案（与 HumanEscalationStore 配合）。
支持多名客服、分组 agent_teams（每队可选排班）、工作时间段、值班模式。
"""

from __future__ import annotations

import hashlib
import html
import logging
import random
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.utils.work_schedule import is_within_work_hours

# 与 main.py 中 ai_chat_assistant 使用同一日志树，否则 INFO 不会写入 logs/app.log（仅挂在根名上的 Handler 收不到 src.utils.*）
_logger = logging.getLogger("ai_chat_assistant.human_escalation")

_ZW_RE = re.compile(r"[\u200b-\u200f\ufeff\u2060-\u2064]")

# 句末标点（可重复），用于归一化「同一问句」：避免「订单有没有收到」与「订单有没有收到？」算成两个问题
_TRAIL_PUNCT = re.compile(r'[。．.!！?？,，;；:：、…～~·]+$')


def normalize_user_question(text: str) -> str:
    s = (text or "").strip()
    s = unicodedata.normalize("NFKC", s)
    s = _ZW_RE.sub("", s)
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    # 循环去掉句末标点，使重复检测对「仅标点不同」的文案一致
    for _ in range(8):
        t = _TRAIL_PUNCT.sub("", s).strip()
        t = re.sub(r"\s+", " ", t)
        if t == s:
            break
        s = t
    return s


def _norm_key(norm: str) -> str:
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]


def _escalation_cooldown_scope_legacy(cfg: Dict[str, Any]) -> bool:
    """
    True: 沿用旧行为，整群+用户共一条转接冷却（任一问题 @ 后，其它问题也在冷却内）。
    False（默认）: 按归一化问句维度冷却，不同问题独立。
    """
    s = (cfg.get("escalation_cooldown_scope") or "per_normalized_question").strip().lower()
    return s in ("per_user_chat", "global", "legacy")


def _resolve_duty_mode(cfg: Dict[str, Any]) -> str:
    dm = cfg.get("duty_mode")
    if isinstance(dm, str) and dm in (
        "always",
        "manual",
        "schedule",
        "schedule_or_manual",
        "schedule_and_manual",
    ):
        return dm
    if cfg.get("only_when_shift_online"):
        return "manual"
    return "always"


def _parse_uid(raw: Any) -> int:
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_username(raw: Any) -> str:
    return (raw or "").strip().lstrip("@")


def _agents_from_list(raw_list: List[Any]) -> List[Dict[str, Any]]:
    """将配置中的 agents 数组规范为内部结构。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(raw_list, list):
        return out
    n = len(raw_list)
    for i, a in enumerate(raw_list):
        if not isinstance(a, dict):
            continue
        uid = _parse_uid(a.get("user_id"))
        uname = _normalize_username(a.get("username"))
        disp = (a.get("display_name") or a.get("label") or "").strip()
        if not disp:
            disp = f"人工客服{i + 1}" if n > 1 else "人工客服"
        if uid or uname:
            out.append(
                {"user_id": uid, "username": uname, "display_name": disp}
            )
    return out


def _dedupe_agents(agents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for a in agents:
        uid = _parse_uid(a.get("user_id"))
        un = _normalize_username(a.get("username")).lower()
        key = (uid, un)
        if key == (0, ""):
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def _resolve_agents(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    解析顺序：
    1) 若配置了 agent_teams（或 teams）非空：按当前时间筛选「本队排班命中」的客服并合并去重；
       若无人命中且 team_fallback_to_global 为 true（默认），回退到全局 agents / 单客服字段。
    2) 否则使用 agents / human_username + human_user_id。
    """
    teams = cfg.get("agent_teams")
    if teams is None:
        teams = cfg.get("teams")
    tz = (cfg.get("timezone") or "UTC").strip() or "UTC"
    wh_g = cfg.get("work_hours") if isinstance(cfg.get("work_hours"), dict) else {}
    wex_g = cfg.get("work_exceptions") if isinstance(cfg.get("work_exceptions"), dict) else {}
    now = datetime.now(timezone.utc)
    fallback = bool(cfg.get("team_fallback_to_global", True))
    pick = (cfg.get("team_pick_mode") or "union").strip().lower()
    if pick not in ("union", "first_match"):
        pick = "union"

    if isinstance(teams, list) and len(teams) > 0:
        if pick == "first_match":
            for t in teams:
                if not isinstance(t, dict):
                    continue
                tagents = t.get("agents")
                if not isinstance(tagents, list) or not tagents:
                    continue
                wh_t = t.get("work_hours")
                if wh_t is None or not isinstance(wh_t, dict):
                    wh_t = wh_g
                wex_t = t.get("work_exceptions")
                if wex_t is None or not isinstance(wex_t, dict):
                    wex_t = wex_g
                try:
                    active = is_within_work_hours(now, tz, wh_t, wex_t)
                except Exception as e:
                    _logger.warning("分组排班判断异常: %s", e)
                    active = False
                if active:
                    sub = _agents_from_list(tagents)
                    if sub:
                        return sub
            if not fallback:
                return []
        else:
            collected: List[Dict[str, Any]] = []
            for t in teams:
                if not isinstance(t, dict):
                    continue
                tagents = t.get("agents")
                if not isinstance(tagents, list) or not tagents:
                    continue
                wh_t = t.get("work_hours")
                if wh_t is None or not isinstance(wh_t, dict):
                    wh_t = wh_g
                wex_t = t.get("work_exceptions")
                if wex_t is None or not isinstance(wex_t, dict):
                    wex_t = wex_g
                try:
                    active = is_within_work_hours(now, tz, wh_t, wex_t)
                except Exception as e:
                    _logger.warning("分组排班判断异常: %s", e)
                    active = False
                if active:
                    collected.extend(_agents_from_list(tagents))
            if collected:
                return _dedupe_agents(collected)
            if not fallback:
                return []

    agents = cfg.get("agents")
    if isinstance(agents, list) and agents:
        out = _agents_from_list(agents)
        if out:
            return out

    uid = _parse_uid(cfg.get("human_user_id"))
    uname = _normalize_username(cfg.get("human_username"))
    if not uid and not uname:
        return []
    disp = (cfg.get("human_display_name") or "").strip() or "人工客服"
    return [{"user_id": uid, "username": uname, "display_name": disp}]


def active_teams_status(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """供 API：各分组当前是否在其排班窗口内（与 _resolve_agents 逻辑一致）。"""
    teams = cfg.get("agent_teams")
    if teams is None:
        teams = cfg.get("teams")
    if not isinstance(teams, list) or not teams:
        return []
    tz = (cfg.get("timezone") or "UTC").strip() or "UTC"
    wh_g = cfg.get("work_hours") if isinstance(cfg.get("work_hours"), dict) else {}
    wex_g = cfg.get("work_exceptions") if isinstance(cfg.get("work_exceptions"), dict) else {}
    now = datetime.now(timezone.utc)
    rows: List[Dict[str, Any]] = []
    for t in teams:
        if not isinstance(t, dict):
            continue
        tid = t.get("id") or t.get("name") or ""
        tagents = t.get("agents")
        n = len(tagents) if isinstance(tagents, list) else 0
        wh_t = t.get("work_hours")
        if wh_t is None or not isinstance(wh_t, dict):
            wh_t = wh_g
        wex_t = t.get("work_exceptions")
        if wex_t is None or not isinstance(wex_t, dict):
            wex_t = wex_g
        try:
            active = is_within_work_hours(now, tz, wh_t, wex_t)
        except Exception:
            active = False
        rows.append(
            {
                "id": tid,
                "name": t.get("name") or tid,
                "in_schedule": active,
                "agent_count": n,
            }
        )
    return rows


def build_telegram_message_link(
    chat_id: Any,
    message_id: Optional[int],
    chat_username: Optional[str] = None,
) -> Optional[str]:
    """
    生成可点击跳转到指定会话、指定消息的定位链接（供人工客服从转接文案跳转）。

    - 有群/频道 username： https://t.me/username/msg_id
    - 无 username 的超级群： https://t.me/c/<internal_id>/msg_id（-100xxxxxxxxxx → xxxxxxxxxx）
    - 私聊（正数 chat_id）： tg://openmessage?chat_id=...&message_id=...
    - 传统群（负且非 -100）： https://t.me/c/<abs(chat_id)>/msg_id（尽力而为，部分老群可能需客户端兼容）
    """
    if message_id is None or int(message_id) <= 0:
        return None
    mid = int(message_id)
    un = _normalize_username(chat_username)
    if un:
        return f"https://t.me/{un}/{mid}"
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        return None
    s = str(cid)
    if s.startswith("-100"):
        internal = s[4:]
        if internal.isdigit():
            return f"https://t.me/c/{internal}/{mid}"
        return None
    if cid > 0:
        return f"tg://openmessage?chat_id={cid}&message_id={mid}"
    abs_id = abs(cid)
    return f"https://t.me/c/{abs_id}/{mid}"


def _format_escalation_user_question_html(
    cfg: Dict[str, Any],
    user_text: Optional[str],
    chat_id: Any,
    user_message_id: Optional[int],
    chat_username: Optional[str],
) -> str:
    """
    返回要追加到转接后缀的「用户问句」HTML 片段（含可点击定位链接），无内容则返回 ""。
    """
    if not cfg.get("include_user_question_link", True):
        return ""
    raw = (user_text or "").strip()
    if not raw:
        return ""
    max_len = max(8, int(cfg.get("user_question_max_len", 200) or 200))
    q = re.sub(r"\s+", " ", raw)
    if len(q) > max_len:
        q = q[:max_len] + "…"
    q_esc = html.escape(q)
    prefix = (cfg.get("user_question_line_prefix") or "原文：").strip() or "原文："
    prefix_esc = html.escape(prefix)

    url = build_telegram_message_link(chat_id, user_message_id, chat_username)
    if url:
        href = html.escape(url, quote=True)
        return f"\n\n{prefix_esc}<a href=\"{href}\">{q_esc}</a>"
    return f"\n\n{prefix_esc}{q_esc}"


def _format_mentions_line(agents: List[Dict[str, Any]], joiner: str = " ") -> str:
    parts: List[str] = []
    for a in agents:
        uid = int(a.get("user_id") or 0)
        uname = _normalize_username(a.get("username"))
        disp = (a.get("display_name") or "人工客服").strip()
        disp_esc = html.escape(disp)
        if uid:
            parts.append(f'<a href="tg://user?id={uid}">{disp_esc}</a>')
        elif uname:
            # 仅 username 时：用 t.me 可点链接，比裸文本 @ 更易触发客户端通知（仍建议配置 user_id）
            href = html.escape(f"https://t.me/{uname}", quote=True)
            at_vis = html.escape(f"@{uname}")
            parts.append(f'<a href="{href}">{at_vis}</a>')
    return joiner.join(parts) if parts else ""


def _duty_allows(cfg: Dict[str, Any], store: Any) -> bool:
    mode = _resolve_duty_mode(cfg)
    manual = bool(store.get_shift_on_duty())
    if mode == "always":
        return True
    if mode == "manual":
        return manual

    tz = (cfg.get("timezone") or "UTC").strip() or "UTC"
    wh = cfg.get("work_hours") if isinstance(cfg.get("work_hours"), dict) else {}
    wex = cfg.get("work_exceptions")
    wex = wex if isinstance(wex, dict) else {}
    now = datetime.now(timezone.utc)
    try:
        schedule_ok = is_within_work_hours(now, tz, wh, wex)
    except Exception as e:
        _logger.warning("工作时间段判断异常: %s", e)
        schedule_ok = False

    if mode == "schedule":
        return schedule_ok
    if mode == "schedule_or_manual":
        return schedule_ok or manual
    if mode == "schedule_and_manual":
        return schedule_ok and manual
    return True


def duty_allows(cfg: Dict[str, Any], store: Any) -> bool:
    """供 API / 状态页使用：与转接后缀相同的值班判定。"""
    return _duty_allows(cfg, store)


def _agent_private_notify_targets(mention_agents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """供私信/转发：每位客服一个目标，至少要有 user_id 或可解析的 username。"""
    out: List[Dict[str, Any]] = []
    for a in mention_agents:
        uid = int(a.get("user_id") or 0)
        un = _normalize_username(a.get("username"))
        if uid > 0:
            out.append({"user_id": uid, "username": un})
        elif un:
            out.append({"user_id": 0, "username": un})
    return out


def _select_agents_for_mention(
    cfg: Dict[str, Any],
    agents: List[Dict[str, Any]],
    store: Any,
    chat_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """mention_mode: all | single_round_robin | single_random"""
    if len(agents) <= 1:
        return agents
    mode = (cfg.get("mention_mode") or "all").strip().lower()
    if mode not in ("all", "single_round_robin", "single_random"):
        mode = "all"
    if mode == "all":
        return agents
    if mode == "single_random":
        return [random.choice(agents)]
    if mode == "single_round_robin":
        scope = (cfg.get("mention_round_robin_scope") or "global").strip().lower()
        rr_chat = scope == "per_chat" and chat_id
        idx = store.round_robin_next_index(
            len(agents), str(chat_id) if rr_chat else None
        )
        return [agents[idx]]
    return agents


@dataclass
class HumanEscalationOutcome:
    """
    人工转接判断结果。
    suffix：追加到群内 AI 回复的片段（含 @ 与可选用户问句链接）。
    forward_spec：非空时应在群内发送成功后，将用户原消息转发给所列客服私聊。
    """

    suffix: Optional[str] = None
    forward_spec: Optional[Dict[str, Any]] = None


class HumanEscalationHelper:
    def __init__(self, config: Dict[str, Any], store: Any):
        self._config = config or {}
        self._store = store

    def _cfg(self) -> Dict[str, Any]:
        c = self._config.get("human_escalation")
        return c if isinstance(c, dict) else {}

    def reload_config(self, config: Dict[str, Any]) -> None:
        self._config = config or {}

    def record_streak(self, chat_id: Any, user_id: Any, user_text: str) -> Tuple[int, str]:
        """
        每条用户文本消息调用一次：更新重复计数，返回 (当前计数, norm_key)。
        未启用或文本过短则返回 (0, "").
        """
        cfg = self._cfg()
        if not cfg.get("enabled", False):
            return 0, ""

        min_len = int(cfg.get("min_message_len", 6) or 0)
        norm = normalize_user_question(user_text)
        if len(norm) < min_len:
            return 0, ""

        window_sec = float(cfg.get("repeat_window_sec", 600) or 600)
        nk = _norm_key(norm)
        cnt = self._store.record_repeat(str(chat_id), str(user_id), nk, window_sec)
        return cnt, nk

    def format_suffix_if_needed(
        self,
        chat_id: Any,
        user_id: Any,
        streak_count: int,
        norm_key: str,
        *,
        user_message_id: Optional[int] = None,
        user_text: Optional[str] = None,
        chat_username: Optional[str] = None,
        chat_title: Optional[str] = None,
    ) -> HumanEscalationOutcome:
        """
        在已有 bot 回复正文时调用：若达到阈值且通过冷却/值班检查，返回 suffix 等。

        user_message_id / user_text / chat_username：用于在转接文案中附带「用户问句」及可点击定位链接
        （公开群用 t.me，私聊用 tg://openmessage）。

        成功 @ 人工时，若开启 forward_user_message_to_agents 且存在 message_id，
        则 forward_spec 会描述「把用户该条群消息转发给哪些客服」，由客户端在群内发送成功后执行。
        chat_title：写入 forward_spec，供私聊跟进消息展示群名。
        """
        cfg = self._cfg()
        if not cfg.get("enabled", False) or not norm_key or streak_count <= 0:
            return HumanEscalationOutcome()

        threshold = max(2, int(cfg.get("repeat_threshold", 3) or 3))
        if streak_count < threshold:
            return HumanEscalationOutcome()

        _logger.info(
            "人工转接检查: 已达重复阈值 streak=%s threshold=%s chat=%s user=%s",
            streak_count,
            threshold,
            chat_id,
            user_id,
        )

        cooldown_sec = float(cfg.get("cooldown_sec", 300) or 300)
        legacy_cd = _escalation_cooldown_scope_legacy(cfg)

        agents = _resolve_agents(cfg)
        if not agents:
            _logger.info(
                "人工转接未追加: 未解析到任何客服（agents / human_username 等为空或无效）"
            )
            return HumanEscalationOutcome()

        ck = str(chat_id)
        uk = str(user_id)

        if legacy_cd:
            ok_cool, remain = self._store.cooldown_remaining(ck, uk, cooldown_sec)
        else:
            ok_cool, remain = self._store.cooldown_remaining_norm(
                ck, uk, norm_key, cooldown_sec
            )
        if not ok_cool:
            _logger.info(
                "人工转接未追加: 转接冷却中 chat=%s user=%s norm_tail=%s scope=%s 剩余约 %.0fs（cooldown_sec=%s）",
                ck,
                uk,
                norm_key[-8:] if norm_key else "",
                "per_user_chat" if legacy_cd else "per_normalized_question",
                remain,
                cooldown_sec,
            )
            return HumanEscalationOutcome()

        shift_on = self._store.get_shift_on_duty()
        duty_ok = _duty_allows(cfg, self._store)

        if not duty_ok:
            msg = (cfg.get("message_off_shift") or "").strip()
            if msg:
                if legacy_cd:
                    self._store.mark_escalation(ck, uk)
                else:
                    self._store.mark_escalation_norm(ck, uk, norm_key)
                self._store.reset_repeat_key(ck, uk, norm_key)
                return HumanEscalationOutcome(suffix="\n\n" + msg)
            _logger.info(
                "人工转接未追加: 当前不在值班时段且 message_off_shift 为空（静默跳过）"
            )
            return HumanEscalationOutcome()

        line = (cfg.get("escalation_line") or "").strip()
        if not line:
            line = "亲，如果刚才的自动回复没帮到您，可以联系人工同事再看看，谢谢。"

        joiner = (cfg.get("mention_joiner") or " ").strip() or " "
        mention_agents = _select_agents_for_mention(cfg, agents, self._store, ck)
        mention_block = _format_mentions_line(mention_agents, joiner=joiner)
        if not mention_block:
            _logger.info(
                "人工转接未追加: mention 文案为空（客服需配置 user_id 或 username）"
            )
            return HumanEscalationOutcome()

        suffix = f"\n\n────────\n{line}\n{mention_block}"
        suffix += _format_escalation_user_question_html(
            cfg, user_text, chat_id, user_message_id, chat_username
        )
        if legacy_cd:
            self._store.mark_escalation(ck, uk)
        else:
            self._store.mark_escalation_norm(ck, uk, norm_key)
        self._store.reset_repeat_key(ck, uk, norm_key)
        _logger.info(
            "人工转接触发: chat=%s user=%s streak=%s threshold=%s manual_shift=%s duty_ok=%s agents=%s mention_mode=%s",
            ck,
            uk,
            streak_count,
            threshold,
            shift_on,
            duty_ok,
            len(mention_agents),
            (cfg.get("mention_mode") or "all"),
        )

        forward_spec: Optional[Dict[str, Any]] = None
        try:
            mid = int(user_message_id) if user_message_id is not None else 0
        except (TypeError, ValueError):
            mid = 0

        if bool(cfg.get("forward_user_message_to_agents", True)) and mid > 0:
            targets = _agent_private_notify_targets(mention_agents)
            if targets:
                cu = _normalize_username(chat_username)
                ct = (chat_title or "").strip() or None
                forward_spec = {
                    "from_chat_id": chat_id,
                    "message_id": mid,
                    "targets": targets,
                    "chat_username": cu or None,
                    "chat_title": ct,
                }
            else:
                _logger.info(
                    "人工转接: 已配置 forward_user_message_to_agents 但无可用客服 peer"
                )

        group_cfg = cfg.get("forward_to_group")
        if isinstance(group_cfg, dict) and group_cfg.get("enabled"):
            group_id = group_cfg.get("group_id") or ""
            group_link = group_cfg.get("group_link") or ""
            if group_id or group_link:
                if forward_spec is None:
                    forward_spec = {
                        "from_chat_id": chat_id,
                        "message_id": mid if mid > 0 else 0,
                        "targets": [],
                        "chat_username": _normalize_username(chat_username) or None,
                        "chat_title": (chat_title or "").strip() or None,
                    }
                forward_spec["group_target"] = {
                    "group_id": str(group_id).strip() if group_id else "",
                    "group_link": str(group_link).strip() if group_link else "",
                }
                _logger.info(
                    "人工转接: 已配置转发到客服群 group_id=%s group_link=%s",
                    group_id, group_link,
                )

        return HumanEscalationOutcome(suffix=suffix, forward_spec=forward_spec)
