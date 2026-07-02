"""Stage N 发送入口自检：固化 Stage M 审计成果，防新增发送路径漏挂护栏（回归闸）。

不变量：``src/`` 里**每一处物理裸发送**（``<x>.client.send_message/photo/voice/...``）都必须在
下方**已分类白名单**里，每条注明它为何安全（经统一护栏 / 经编排器中心护栏 / 管理员告警有意不限）。

新增一处裸 ``.client.send_*`` 而未登记 → 本测试**失败**，逼迫作者要么改走受护栏的发送入口
（``orchestrator.send`` / mixin ``send_message`` / RPA ``rpa_send_blocked``），要么把它登记进白名单并写明理由。
这正是把 Stage M「一键急停/反封号覆盖所有外发」从一次性人工审计变成**持续回归保障**。
"""

from __future__ import annotations

import ast
import pathlib
from typing import Dict, Set

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"

# 物理发送方法（Pyrogram client.*）；命中 ``<x>.client.<method>(...)`` 即视为「裸发送点」。
_SEND_METHODS = {
    "send_message", "send_photo", "send_voice", "send_video",
    "send_document", "send_audio", "send_animation", "send_sticker",
    "send_media_group",
}

# 分类：每条裸发送点为何安全。新增路径必须落入某类并写明理由。
GUARDED = "guarded"          # 经统一发送护栏（mixin presend / 编排器中心 send_blocked）
ADMIN_ALERT = "admin_alert"  # 发给管理员/坐席的运维告警，有意不受 Kill-Switch 约束（冻结期更要送达）
LEGACY_RELAY = "legacy"      # 订单追踪/定时命令等历史小功能：非陪伴内容，已记录、后续可再纳管

_VALID_CATEGORIES = {GUARDED, ADMIN_ALERT, LEGACY_RELAY}

# key = "<相对 src 的 posix 路径>::<外层 类.方法 限定名>"
ALLOWLIST: Dict[str, tuple] = {
    # ── A 线 mixin：发送护栏本体（presend Kill-Switch + 反封号 + 节流 + 记账）──
    "client/sender.py::TelegramSenderMixin._send_reply":
        (GUARDED, "自动回复主路径，_presend_blocked/pace/record/mirror"),
    "client/sender.py::TelegramSenderMixin.send_message":
        (GUARDED, "Stage M：主动外发纳入统一发送栈（presend 护栏+节流+记账）"),
    "client/sender.py::TelegramSenderMixin.send_photo":
        (GUARDED, "Stage G：形象照直发纳入统一发送栈"),
    # ── 编排器受管 worker：物理发送在 worker，护栏在 orchestrator.send/send_media（Stage M）──
    "integrations/account_orchestrator.py::TelegramProtocolWorker.send":
        (GUARDED, "经 AccountOrchestrator.send → send_blocked 中心护栏后才派发"),
    "integrations/account_orchestrator.py::TelegramProtocolWorker.send_media":
        (GUARDED, "经 AccountOrchestrator.send_media → send_blocked 中心护栏后才派发"),
    "integrations/telegram_companion_worker.py::TelegramCompanionWorker.send":
        (GUARDED, "调 mixin send_message（已护栏）+ 经 orchestrator.send 中心护栏，双层"),
    # ── 管理员/坐席运维告警：有意不受 Kill-Switch 约束（冻结/风控期反而更需送达）──
    "client/sender.py::TelegramSenderMixin._send_escalation_private_jump_hint":
        (ADMIN_ALERT, "人工转接：发给坐席的群内消息定位提示，非客户内容"),
    "client/telegram_client.py::TelegramClient._check_success_rate_alert":
        (ADMIN_ALERT, "通道成功率告警 → admin_chat"),
    "client/telegram_client.py::TelegramClient._send_reload_notification":
        (ADMIN_ALERT, "配置热重载通知 → admin_chat"),
    "integrations/messenger_rpa/runner.py::MessengerRpaRunner._notify_risk._send":
        (ADMIN_ALERT, "RPA 风控告警 → 管理员 TG"),
    "integrations/messenger_rpa/runner.py::MessengerRpaRunner._notify_escalation_telegram._send":
        (ADMIN_ALERT, "RPA 人工升级告警 → 管理员 TG"),
    "integrations/messenger_rpa/service.py::MessengerRpaService._notify_sla_overdue":
        (ADMIN_ALERT, "SLA 超时告警 → 管理员 TG"),
    # ── 历史小功能（非陪伴内容）：已记录，后续可考虑纳管 ──
    "client/telegram_client.py::TelegramClient._gxp_timeout_check":
        (LEGACY_RELAY, "GXP 订单查询追踪：超时提醒转告，非陪伴内容"),
    "client/telegram_client.py::TelegramClient._handle_gxp_bot_reply":
        (LEGACY_RELAY, "GXP 订单查询追踪：结果转告，非陪伴内容"),
    "client/telegram_client.py::TelegramClient._scheduled_send":
        (LEGACY_RELAY, "定时任务发送预设命令（如 /cgl 查询），非陪伴内容"),
}


_SCAN_CACHE: Dict[str, Set[str]] = {}


def _scan_raw_client_sends() -> Dict[str, Set[str]]:
    """扫描 src/ 全量，返回 ``{key: {命中的 send 方法}}``。key 见模块头。（结果缓存，扫一次）"""
    if _SCAN_CACHE:
        return _SCAN_CACHE
    found: Dict[str, Set[str]] = {}
    for path in _SRC.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rel = path.relative_to(_SRC).as_posix()
        stack = []

        class _V(ast.NodeVisitor):
            def visit_ClassDef(self, n):
                stack.append(n.name); self.generic_visit(n); stack.pop()

            def visit_FunctionDef(self, n):
                stack.append(n.name); self.generic_visit(n); stack.pop()

            def visit_AsyncFunctionDef(self, n):
                stack.append(n.name); self.generic_visit(n); stack.pop()

            def visit_Call(self, n):
                f = n.func
                if (isinstance(f, ast.Attribute) and f.attr in _SEND_METHODS
                        and isinstance(f.value, ast.Attribute)
                        and f.value.attr == "client"):
                    key = f"{rel}::{'.'.join(stack)}"
                    found.setdefault(key, set()).add(f.attr)
                self.generic_visit(n)

        _V().visit(tree)
    _SCAN_CACHE.update(found)
    return found


def test_no_unexpected_raw_client_sends():
    """新增未登记的裸 .client.send_* → 失败（必须改走护栏入口或登记白名单）。"""
    discovered = set(_scan_raw_client_sends())
    unexpected = sorted(discovered - set(ALLOWLIST))
    assert not unexpected, (
        "发现未登记的裸物理发送点（绕过统一发送护栏的风险）：\n  "
        + "\n  ".join(unexpected)
        + "\n\n请改走受护栏的发送入口（orchestrator.send / mixin send_message / "
        "RPA rpa_send_blocked），或若确为管理员告警/系统消息，登记进 "
        "tests/test_send_path_audit.py::ALLOWLIST 并写明理由。"
    )


def test_no_stale_allowlist_entries():
    """白名单里已不存在的发送点 → 失败（保持白名单与代码同步、不留僵尸条目）。"""
    discovered = set(_scan_raw_client_sends())
    stale = sorted(set(ALLOWLIST) - discovered)
    assert not stale, (
        "白名单存在已删除/改名的发送点，请清理：\n  " + "\n  ".join(stale)
    )


def test_allowlist_entries_categorized_with_reason():
    """每条白名单都须有合法分类 + 非空理由（强制写清为何安全）。"""
    bad = [
        k for k, v in ALLOWLIST.items()
        if (not isinstance(v, tuple) or len(v) != 2
            or v[0] not in _VALID_CATEGORIES or not str(v[1]).strip())
    ]
    assert not bad, f"白名单条目缺分类或理由：{bad}"


def test_companion_send_paths_are_guarded():
    """关键陪伴外发入口必须归类 guarded（主动问候/自动回复/编排器派发）。"""
    must_guarded = [
        "client/sender.py::TelegramSenderMixin.send_message",
        "client/sender.py::TelegramSenderMixin._send_reply",
        "client/sender.py::TelegramSenderMixin.send_photo",
        "integrations/account_orchestrator.py::TelegramProtocolWorker.send",
        "integrations/account_orchestrator.py::TelegramProtocolWorker.send_media",
        "integrations/telegram_companion_worker.py::TelegramCompanionWorker.send",
    ]
    for k in must_guarded:
        assert k in ALLOWLIST, f"关键发送入口从白名单消失（被改名/删除？）：{k}"
        assert ALLOWLIST[k][0] == GUARDED, f"关键陪伴发送入口必须 guarded：{k}"


def test_postsend_mirror_forwards_real_msg_id():
    """治本幂等键：_postsend_mirror_and_record 把发送 API 返回的真实 message.id 透传给
    出站镜像 _emit_inbox（→ platform_msg_id），乐观镜像行与回显共用主键精确去重。"""
    from src.client.sender import TelegramSenderMixin

    obj = TelegramSenderMixin.__new__(TelegramSenderMixin)
    captured = {}
    obj._emit_inbox = lambda **kw: captured.update(kw)  # type: ignore[attr-defined]
    obj._postsend_mirror_and_record(12345, "hello", msg_id=778)
    assert captured.get("direction") == "out"
    assert captured.get("msg_id") == "778"   # 真实 id 被字符串化透传


def test_scanner_detects_synthetic_raw_send(tmp_path):
    """元测试：扫描器确实能抓到裸 .client.send_*（否则审计形同虚设）。"""
    code = (
        "class W:\n"
        "    async def send(self, t):\n"
        "        await self.client.send_message(1, t)\n"
    )
    tree = ast.parse(code)
    hits = []
    stack = []

    class _V(ast.NodeVisitor):
        def visit_ClassDef(self, n):
            stack.append(n.name); self.generic_visit(n); stack.pop()

        def visit_AsyncFunctionDef(self, n):
            stack.append(n.name); self.generic_visit(n); stack.pop()

        def visit_Call(self, n):
            f = n.func
            if (isinstance(f, ast.Attribute) and f.attr in _SEND_METHODS
                    and isinstance(f.value, ast.Attribute)
                    and f.value.attr == "client"):
                hits.append(".".join(stack))
            self.generic_visit(n)

    _V().visit(tree)
    assert hits == ["W.send"]
