"""Q1+Q2：批量审批 + 键盘快捷键 —— 结构 + 集成测试。

测试目标（静态分析 + Jinja 模板 inspection，不开浏览器）：

- `_rpa_shared_scripts.html` 暴露 `window.rpa.bulk.{register, onPendingRendered,
  getSelectedIds, selectAll, clearSelection, toggleAt, approveSelected,
  rejectSelected, approveFocused, rejectFocused}` API
- bulk 模块有完整的键盘 keymap（j/k/↑/↓ + a/r + Shift+A/R + Space/x + Esc）
- bulk 模块在 input/textarea/select 焦点时跳过（不误触发）
- bulk 模块的批量操作用 Promise.allSettled（部分失败不阻塞其他）
- LINE / WhatsApp 都正确注册了 rpa.bulk + 配了正确的 endpoint + 渲染了 checkbox
- Telegram 没有接入（直发模式无 approval queue）
- Messenger 没有接入共享 bulk（保留自家 batch endpoint 走自己的 UI）
- `_rpa_shared_styles.html` 有配套的 CSS（.rpa-bulk-focus / .rpa-bulk-pick）
"""

from __future__ import annotations

from pathlib import Path

import pytest


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "src" / "web" / "templates"
SCRIPTS = TEMPLATES_DIR / "_rpa_shared_scripts.html"
STYLES = TEMPLATES_DIR / "_rpa_shared_styles.html"


# ════════════════════════════════════════════════════════════════════════
# rpa.bulk 命名空间结构
# ════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def scripts_text() -> str:
    return SCRIPTS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def styles_text() -> str:
    return STYLES.read_text(encoding="utf-8")


def test_bulk_namespace_exposed(scripts_text: str):
    assert "rpa.bulk" in scripts_text


@pytest.mark.parametrize("api", [
    "register",
    "onPendingRendered",
    "getSelectedIds",
    "selectAll",
    "clearSelection",
    "toggleAt",
    "approveSelected",
    "rejectSelected",
    "approveFocused",
    "rejectFocused",
])
def test_bulk_exposes_required_apis(scripts_text: str, api: str):
    """这些 API 是平台模板调用的契约，改名前先改所有调用方。"""
    assert f"{api}:" in scripts_text or f"{api}(" in scripts_text, (
        f"rpa.bulk 必须定义 {api}"
    )


# ════════════════════════════════════════════════════════════════════════
# 键盘 keymap 必须覆盖的快捷键
# ════════════════════════════════════════════════════════════════════════


class TestKeymap:
    def test_navigation_keys(self, scripts_text: str):
        """j/k + 上下箭头都要支持（vim 用户和方向键用户都要照顾到）。"""
        for k in ["'j'", "'k'", "'ArrowDown'", "'ArrowUp'"]:
            assert k in scripts_text, f"keymap 必须支持 {k}"

    def test_single_row_actions(self, scripts_text: str):
        """a/r 操作当前焦点行（单条审批）。"""
        # case 'a' 和 case 'r' 必须存在（双引号 / 单引号都接受）
        assert "case 'a':" in scripts_text
        assert "case 'r':" in scripts_text

    def test_bulk_actions_with_shift(self, scripts_text: str):
        """Shift+A / Shift+R 批量操作；区分大小写让单条和批量不混淆。"""
        assert "case 'A':" in scripts_text
        assert "case 'R':" in scripts_text
        assert "e.shiftKey" in scripts_text

    def test_toggle_checkbox_keys(self, scripts_text: str):
        """空格 / x 都能切换 checkbox。"""
        assert "case ' ':" in scripts_text
        assert "case 'x':" in scripts_text

    def test_escape_clears_selection(self, scripts_text: str):
        """Esc 必须清空选中 + 取消焦点，是误操作的安全出口。"""
        assert "case 'Escape':" in scripts_text

    def test_skips_when_input_focused(self, scripts_text: str):
        """运营在 textarea 编辑回复时按 a 不应该触发审批，否则会丢字 + 误批准。"""
        # 必须检查 input / textarea / select / isContentEditable
        for guard in ["'input'", "'textarea'", "isContentEditable"]:
            assert guard in scripts_text, f"keymap 必须防御 {guard} 焦点"

    def test_skips_when_container_hidden(self, scripts_text: str):
        """切到非 pending tab 时（容器不可见），keymap 不应该触发，否则用户
        在"设置"tab 按 a 会莫名其妙批准了"审核队列"tab 的某条。"""
        assert "offsetParent" in scripts_text

    def test_skips_modifier_keys(self, scripts_text: str):
        """Ctrl+S / Cmd+K 等系统快捷键不应该被 bulk 吞掉。"""
        assert "ctrlKey" in scripts_text
        assert "metaKey" in scripts_text


# ════════════════════════════════════════════════════════════════════════
# 并发执行 + 部分失败
# ════════════════════════════════════════════════════════════════════════


class TestBatchSemantics:
    def test_uses_promise_allSettled(self, scripts_text: str):
        """allSettled（不是 all）—— 1 条失败不能让其余 49 条全部丢失。"""
        assert "Promise.allSettled" in scripts_text, (
            "批量操作必须用 allSettled，避免单条失败炸掉整批"
        )

    def test_reports_partial_failure_with_toast(self, scripts_text: str):
        """部分失败必须 toast 显示"成功 X / 失败 Y"，不能静默。"""
        # 关键字：成功 X 失败 Y 的提示
        assert "成功" in scripts_text and "失败" in scripts_text

    def test_failure_details_logged_to_console(self, scripts_text: str):
        """失败的具体 ID 必须打 console.warn，让运营 F12 能查到原因。"""
        assert "console.warn" in scripts_text


# ════════════════════════════════════════════════════════════════════════
# CSS 配套
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("cls", [
    ".rpa-bulk-toolbar",
    ".rpa-bulk-pick",
    ".rpa-bulk-focus",
    ".rpa-bulk-hint",
    ".rpa-bulk-count",
])
def test_shared_styles_has_bulk_css(styles_text: str, cls: str):
    """共享样式必须有 bulk 系列 CSS，否则平台模板里的 toolbar 没有视觉。"""
    assert cls in styles_text, f"_rpa_shared_styles.html 缺少 {cls}"


# ════════════════════════════════════════════════════════════════════════
# 平台接入：LINE + WhatsApp
# ════════════════════════════════════════════════════════════════════════


INTEGRATIONS = [
    {
        "name":           "line",
        "template":       "line_rpa.html",
        "container_id":   "lr-pending-body",
        "row_class":      "lr-pend",
        "approve_endpoint":"/api/line-rpa/pending/",
    },
    {
        "name":           "whatsapp",
        "template":       "whatsapp_rpa.html",
        "container_id":   "wa-pending-list",
        "row_class":      "rpa-pend",
        "approve_endpoint":"/api/whatsapp-rpa/pending/",
    },
]


@pytest.mark.parametrize("spec", INTEGRATIONS, ids=lambda s: s["name"])
def test_platform_registers_bulk(spec):
    """每个平台都必须 register rpa.bulk + 传正确的 containerId / rowClass / endpoint。"""
    text = (TEMPLATES_DIR / spec["template"]).read_text(encoding="utf-8")
    assert "rpa.bulk.register" in text, (
        f"{spec['template']} 必须 register rpa.bulk"
    )
    assert f"containerId: '{spec['container_id']}'" in text or \
           f"containerId:'{spec['container_id']}'" in text, (
        f"{spec['template']} containerId 应为 {spec['container_id']}"
    )
    assert f"rowClass:    '{spec['row_class']}'" in text or \
           f"rowClass: '{spec['row_class']}'" in text or \
           f"rowClass:'{spec['row_class']}'" in text, (
        f"{spec['template']} rowClass 应为 {spec['row_class']}"
    )
    assert spec["approve_endpoint"] in text, (
        f"{spec['template']} 必须调用 {spec['approve_endpoint']}*/resolve"
    )


@pytest.mark.parametrize("spec", INTEGRATIONS, ids=lambda s: s["name"])
def test_platform_renders_bulk_pick_checkbox(spec):
    """每行必须有 rpa-bulk-pick checkbox，否则 bulk 选不出 ID。"""
    text = (TEMPLATES_DIR / spec["template"]).read_text(encoding="utf-8")
    assert "rpa-bulk-pick" in text, (
        f"{spec['template']} 必须渲染 .rpa-bulk-pick checkbox"
    )


@pytest.mark.parametrize("spec", INTEGRATIONS, ids=lambda s: s["name"])
def test_platform_renders_bulk_toolbar(spec):
    """toolbar 必须有"批量批准"按钮（disabled 初始态）+ 快捷键提示。"""
    text = (TEMPLATES_DIR / spec["template"]).read_text(encoding="utf-8")
    assert "rpa-bulk-toolbar" in text
    assert "批量批准" in text
    assert "批量拒绝" in text
    assert "rpa-bulk-hint" in text, (
        f"{spec['template']} 应该展示键盘快捷键提示气泡"
    )


@pytest.mark.parametrize("spec", INTEGRATIONS, ids=lambda s: s["name"])
def test_platform_calls_onPendingRendered_after_refresh(spec):
    """每次平台 refresh pending list 后必须调 onPendingRendered，让焦点跟过来。"""
    text = (TEMPLATES_DIR / spec["template"]).read_text(encoding="utf-8")
    assert "onPendingRendered" in text, (
        f"{spec['template']} 必须在 refresh pending list 后调 onPendingRendered"
    )


# ════════════════════════════════════════════════════════════════════════
# 边界：Telegram / Messenger / Overview 不接入
# ════════════════════════════════════════════════════════════════════════


def test_telegram_does_not_register_bulk():
    """Telegram 直发模式无 approval queue，不接 bulk。"""
    text = (TEMPLATES_DIR / "telegram.html").read_text(encoding="utf-8")
    assert "rpa.bulk.register" not in text


def test_messenger_keeps_own_batch_implementation():
    """Messenger 已有自家批量审批（P2-6 / P6-3 / batch endpoint /
    dry_run / pacing_sec / reject_reason 等丰富参数），不接 shared bulk。
    """
    text = (TEMPLATES_DIR / "messenger_rpa.html").read_text(encoding="utf-8")
    assert "rpa.bulk.register" not in text, (
        "Messenger 不应该接入 shared bulk —— 它有自家更丰富的 batch endpoint"
    )
    # 自家 batch endpoint 仍在
    assert "/api/messenger-rpa/approvals/batch" in text


def test_overview_does_not_register_bulk():
    """Overview 是"跨平台查看"页，不做审批，bulk 不应接入。"""
    text = (TEMPLATES_DIR / "rpa_overview.html").read_text(encoding="utf-8")
    assert "rpa.bulk.register" not in text
