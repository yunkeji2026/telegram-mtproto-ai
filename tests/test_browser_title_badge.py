"""Q3：浏览器 title 角标通知 —— 结构 + 接入 + 行为契约测试。

测试目标（不开浏览器，靠静态分析 + Jinja 模板 inspection）：
- `_rpa_shared_scripts.html` 暴露 `window.rpa.notify.{setBadge, reset, flash}` API
- setBadge 是幂等的（用正则剥旧前缀，不依赖 baseline 缓存）
- 4 个生效页面（LINE / WhatsApp / Messenger / Overview）都正确接入了 setBadge
- Telegram **没有** 接入 setBadge（直发模式无 pending queue，刻意不做）
"""

from __future__ import annotations

from pathlib import Path

import pytest


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "src" / "web" / "templates"
SHARED_SCRIPTS = TEMPLATES_DIR / "_rpa_shared_scripts.html"


# ════════════════════════════════════════════════════════════════════════
# shared scripts 暴露 rpa.notify API
# ════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def shared_scripts_text() -> str:
    return SHARED_SCRIPTS.read_text(encoding="utf-8")


def test_rpa_notify_namespace_exposed(shared_scripts_text: str):
    """rpa.notify 命名空间必须存在。"""
    assert "rpa.notify" in shared_scripts_text


@pytest.mark.parametrize("api", ["setBadge", "reset", "flash"])
def test_rpa_notify_exposes_required_apis(shared_scripts_text: str, api: str):
    """3 个公开 API 必须存在。改名前先改所有调用方。"""
    assert f"{api}:" in shared_scripts_text or f"{api}(" in shared_scripts_text, (
        f"rpa.notify 必须定义 {api}"
    )


def test_setBadge_is_idempotent_via_regex_strip(shared_scripts_text: str):
    """setBadge 必须用正则剥旧前缀实现幂等，而不是缓存 baseline。

    缓存 baseline 的实现在以下场景会坏：
    - 页面初始 title 是 'RPA - LINE'，第一次 setBadge(3) → 缓存 'RPA - LINE'
    - 用户在控制台改了 document.title，再 setBadge(0) → 还是回到旧的
    正则剥前缀的实现：每次都从 document.title 现读、现剥、现拼。
    """
    # 关键正则模式必须在文件里
    assert "PREFIX_RE" in shared_scripts_text or "/^\\(\\d+" in shared_scripts_text


def test_flash_respects_visibility_state(shared_scripts_text: str):
    """flash 必须只在 tab 不可见时才闪烁，避免打扰当前页用户。"""
    assert "visibilityState" in shared_scripts_text


def test_visibility_listener_stops_flash(shared_scripts_text: str):
    """用户切回当前 tab 时，flash 必须立即停止。"""
    assert "visibilitychange" in shared_scripts_text


# ════════════════════════════════════════════════════════════════════════
# 4 个生效页面正确接入 setBadge
# ════════════════════════════════════════════════════════════════════════


# (template, expected_label_or_marker)
INTEGRATIONS = [
    ("line_rpa.html",     "LINE 待审"),
    ("whatsapp_rpa.html", "WA 待审"),
    ("messenger_rpa.html", "FB 待审"),
    ("rpa_overview.html", "跨平台待审"),
]


@pytest.mark.parametrize("template,label", INTEGRATIONS)
def test_template_calls_setBadge_with_label(template: str, label: str):
    """每个目标页都必须调 setBadge 且带平台标签。

    标签 ≠ 装饰：当用户同时打开多个 RPA tab，title 角标的标签是用户区
    分"哪个平台来的提醒"的唯一线索。
    """
    text = (TEMPLATES_DIR / template).read_text(encoding="utf-8")
    assert "rpa.notify.setBadge" in text, (
        f"{template} 必须调 rpa.notify.setBadge"
    )
    assert label in text, f"{template} 必须传 label='{label}'"


@pytest.mark.parametrize("template,_label", INTEGRATIONS)
def test_setBadge_guards_against_missing_rpa(template: str, _label: str):
    """加载顺序保险：`if(window.rpa && window.rpa.notify)` 防御。

    理由：partial 渲染顺序变化（比如 _rpa_shared_scripts 被某次重构搬到
    页面底部）时，setBadge 早调用会 ReferenceError 直接整个 callback
    崩掉。这条防御是低成本的"不会坏" 保障。
    """
    text = (TEMPLATES_DIR / template).read_text(encoding="utf-8")
    assert "window.rpa && window.rpa.notify" in text, (
        f"{template} 调 setBadge 前必须做 window.rpa.notify 存在性检查"
    )


# ════════════════════════════════════════════════════════════════════════
# Telegram 故意不接入（直发模式无 approval queue）
# ════════════════════════════════════════════════════════════════════════


def test_telegram_does_not_use_setBadge_because_no_approval_queue():
    """Telegram 是直发模式（消息进 → AI → 立即回复），没有 pending queue。

    如果将来给 Telegram 加 approval queue，再相应启用这个 badge；现在
    刻意不调用，避免误导运营（"Telegram 标题里(0)是什么意思？" → 困惑）。
    """
    text = (TEMPLATES_DIR / "telegram.html").read_text(encoding="utf-8")
    assert "rpa.notify.setBadge" not in text, (
        "Telegram 无 pending queue，不应调用 setBadge；如已加 approval "
        "queue 请同步移除此约束测试"
    )
