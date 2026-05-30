"""P5 RPA 跨平台运营漏斗 partial —— 结构 + 集成测试。

测试目标（不调 web service，只验证 partial 组件本身和它的接入点）：

- partial 文件存在且 Jinja 可解析
- partial 暴露的元素 ID / JS 函数名稳定（被调用方依赖）
- 4 个 RPA 平台页 + overview 页都正确 include 了 partial
- partial 独立渲染时输出包含必需的 DOM 钩子（IDs + script）

不重复测试 `/api/funnel/stats` 的契约——该端点已由
`tests/test_contacts_routes.py::TestFunnelStats` 覆盖；本文件只测前端
partial 的结构 + 4 个页面的接入完整性。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "src" / "web" / "templates"
PARTIAL_NAME = "_rpa_shared_funnel.html"
PARTIAL_PATH = TEMPLATES_DIR / PARTIAL_NAME


# ════════════════════════════════════════════════════════════════════════
# Partial 文件存在 + 关键钩子稳定
# ════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def partial_text() -> str:
    assert PARTIAL_PATH.exists(), f"partial missing: {PARTIAL_PATH}"
    return PARTIAL_PATH.read_text(encoding="utf-8")


def test_partial_file_exists(partial_text: str):
    assert len(partial_text) > 200  # 非空


def test_partial_exposes_required_dom_ids(partial_text: str):
    """4 个调用方依赖这些 ID（rpa.funnel.render 会写入它们）。

    一旦改名，回归就会爆：测试自身就是文档。
    """
    required_ids = [
        "rpa-funnel-wrap",
        "rpa-funnel-total",
        "rpa-funnel-rates",
        "rpa-funnel-bars",
    ]
    for elem_id in required_ids:
        assert f'id="{elem_id}"' in partial_text, (
            f"partial 必须包含 id={elem_id}（被 rpa.funnel.render 引用）"
        )


def test_partial_exposes_required_js_api(partial_text: str):
    """window.rpa.funnel 需要暴露的 3 个公开方法。

    每个调用方都按这个接口集成（init / refresh / render），改签名前
    必须先改所有调用方。
    """
    for fn in ["F.init", "F.refresh", "F.render"]:
        assert fn in partial_text, f"partial 必须定义 window.rpa.funnel.{fn[2:]}"
    assert "window.rpa.funnel" in partial_text


def test_partial_lists_all_10_journey_stages(partial_text: str):
    """STAGES 数组覆盖 10 个 Journey 阶段（与 contacts 模型对齐）。"""
    expected_stages = [
        "INITIAL", "ENGAGED", "HANDOFF_READY", "HANDOFF_SENT",
        "LINE_ADDED", "LINE_ACCEPTED", "LINE_ENGAGED", "BONDED",
        "LOST_HANDOFF", "LOST_LINE_SILENT",
    ]
    for stage in expected_stages:
        assert f"'{stage}'" in partial_text, (
            f"partial STAGES 必须列出阶段 {stage}（与 contacts.JourneyStage 对齐）"
        )


def test_partial_calls_funnel_stats_endpoint(partial_text: str):
    """partial 依赖跨平台共享端点 /api/funnel/stats。"""
    assert "/api/funnel/stats" in partial_text


def test_partial_handles_disabled_contacts_subsystem(partial_text: str):
    """contacts 关闭时显示 disabled 提示而非崩溃。"""
    # render 入口里 disabled flag 的代码路径
    assert "disabled" in partial_text
    assert "rpa-funnel-disabled" in partial_text  # 灰显样式存在


# ════════════════════════════════════════════════════════════════════════
# 4 个页面都正确接入 partial（include + init 调用）
# ════════════════════════════════════════════════════════════════════════


# (template_name, init_call_marker)
INTEGRATIONS = [
    ("line_rpa.html",       "window.rpa.funnel.init"),
    ("whatsapp_rpa.html",   "window.rpa.funnel.init"),
    ("telegram.html",       "window.rpa.funnel.init"),
    ("rpa_overview.html",   "window.rpa.funnel.init"),
]


@pytest.mark.parametrize("template_name,init_marker", INTEGRATIONS)
def test_template_includes_partial(template_name: str, init_marker: str):
    """每个目标页都 include 了 partial 并调用了 init。"""
    path = TEMPLATES_DIR / template_name
    assert path.exists(), f"template missing: {path}"
    text = path.read_text(encoding="utf-8")
    # Jinja include 标记
    assert (
        '{% include "_rpa_shared_funnel.html" %}' in text
        or "{% include '_rpa_shared_funnel.html' %}" in text
    ), f"{template_name} 必须 include _rpa_shared_funnel.html"
    # init 调用（确保 partial 被启动而非只挂在 DOM 里）
    assert init_marker in text, (
        f"{template_name} 必须调用 {init_marker} 启动 partial"
    )


@pytest.mark.parametrize("template_name,_marker", INTEGRATIONS)
def test_template_has_funnel_pane_anchor(template_name: str, _marker: str):
    """每个页面 partial 必须挂在一个明确的 pane 容器里（避免飘在页面顶上）。

    LINE / WhatsApp / overview 用 .st-pane；Telegram 用 .st-pane 也（修
    改后 TAB_NAMES 已包含 'funnel'）。
    """
    path = TEMPLATES_DIR / template_name
    text = path.read_text(encoding="utf-8")
    # 至少一个 pane 容器把 partial 包起来
    has_pane = any(
        marker in text
        for marker in [
            'id="pane-funnel"',          # line / telegram / overview
            'id="pane-wa-funnel"',       # whatsapp
            'id="ov-tab-funnel"',        # overview alt
        ]
    )
    assert has_pane, (
        f"{template_name} 必须把 partial 放在 pane-funnel / pane-wa-funnel / "
        "ov-tab-funnel 容器里"
    )


# ════════════════════════════════════════════════════════════════════════
# Partial 独立渲染 → 输出有完整 DOM 骨架
# ════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def jinja_env() -> Environment:
    return Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))


def test_partial_renders_standalone(jinja_env: Environment):
    """partial 不依赖任何 context 变量；零参数渲染应得到完整 HTML 骨架。"""
    tmpl = jinja_env.get_template(PARTIAL_NAME)
    html = tmpl.render()
    # 关键 DOM 节点
    assert 'id="rpa-funnel-wrap"' in html
    assert 'id="rpa-funnel-bars"' in html
    assert 'id="rpa-funnel-rates"' in html
    assert 'id="rpa-funnel-total"' in html
    # CSS / Script 块
    assert "<style>" in html and "</style>" in html
    assert "<script>" in html and "</script>" in html
    # 视觉结构
    assert "rpa-funnel-row" in html  # CSS 类（表示 bar 行布局已声明）


def test_partial_safe_to_include_twice(jinja_env: Environment):
    """同一页面意外 include 两次时 JS 端有 `if(window.rpa.funnel) return;`
    防御；这里只验证 partial 文本里这条防御存在（运行期不 crash 即可）。
    """
    text = (TEMPLATES_DIR / PARTIAL_NAME).read_text(encoding="utf-8")
    assert "if(window.rpa.funnel) return" in text


# ════════════════════════════════════════════════════════════════════════
# Messenger 不受影响（保留自家加强版 funnel）
# ════════════════════════════════════════════════════════════════════════


def test_messenger_keeps_own_funnel():
    """Messenger 自家 funnel 增强了 variants/handoff/ab_conclusions，
    不应被 shared partial 替换；其页面仍调用 /api/messenger-rpa/funnel。
    """
    path = TEMPLATES_DIR / "messenger_rpa.html"
    text = path.read_text(encoding="utf-8")
    # Messenger 自己的 funnel 端点仍在
    assert "/api/messenger-rpa/funnel" in text
    # 自家 CSS class（mr-funnel-row）保留
    assert "mr-funnel-row" in text
