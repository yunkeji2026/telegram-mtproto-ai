"""平台自动化档位上限（P0-5 ``inbox.auto_draft.platform_modes``）契约。

比一刀切 ``skip_platforms`` 更细的降级手段：平台链路不稳（如 Messenger 网页 RPA）时
把该平台**封顶**到 ``review`` —— AI 仍拟稿、强制人审、绝不自动发；坐席显式 ``manual``
的会话不受影响（封顶取「较不激进」一侧，绝不把档位抬高）。

关键安全不变量：封顶到 ``review`` 后，无论会话原档位/风险等级如何，
``is_autosend_allowed`` 恒为 False（自动真发被彻底关死）。
"""

from __future__ import annotations

import pytest

from src.inbox.drafts import cap_automation_mode, is_autosend_allowed


# ── 纯函数矩阵 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("conv,ceiling,expected", [
    # review 上限：auto_ai/multi_choice 被压到 review；review/manual 不动
    ("auto_ai", "review", "review"),
    ("multi_choice", "review", "review"),
    ("review", "review", "review"),
    ("manual", "review", "manual"),
    # manual 上限：全部压到 manual（等效整平台停拟稿）
    ("auto_ai", "manual", "manual"),
    ("review", "manual", "manual"),
    # auto_ai 上限：等于不设限
    ("auto_ai", "auto_ai", "auto_ai"),
    ("review", "auto_ai", "review"),
    ("manual", "auto_ai", "manual"),
    # multi_choice 上限
    ("auto_ai", "multi_choice", "multi_choice"),
    ("review", "multi_choice", "review"),
])
def test_cap_matrix(conv, ceiling, expected):
    assert cap_automation_mode(conv, ceiling) == expected


def test_cap_never_raises_mode():
    """封顶只降不升：任何组合下结果的激进度 ≤ 会话原档位。"""
    from src.inbox.drafts import _MODE_RANK
    for conv in _MODE_RANK:
        for ceil in _MODE_RANK:
            out = cap_automation_mode(conv, ceil)
            assert _MODE_RANK[out] <= _MODE_RANK[conv]


@pytest.mark.parametrize("ceiling", [None, "", "bogus", "AUTO"])
def test_no_or_invalid_ceiling_is_noop(ceiling):
    for conv in ("manual", "review", "multi_choice", "auto_ai"):
        assert cap_automation_mode(conv, ceiling) == conv


def test_unknown_conv_mode_defaults_review_then_capped():
    assert cap_automation_mode("weird", "review") == "review"
    assert cap_automation_mode("weird", "manual") == "manual"
    assert cap_automation_mode("", "auto_ai") == "review"  # 空档位按默认 review


def test_case_insensitive():
    assert cap_automation_mode("AUTO_AI", "Review") == "review"


# ── 安全不变量：review 封顶 ⇒ 自动真发恒关 ───────────────────────────────────

def test_review_ceiling_kills_autosend_for_all_risk_levels():
    for conv in ("auto_ai", "multi_choice", "review", "manual"):
        eff = cap_automation_mode(conv, "review")
        for risk in ("low", "medium", "high"):
            assert is_autosend_allowed(risk, eff) is False


def test_without_ceiling_l2_autosend_still_works():
    """回归护栏：不设上限时 low+auto_ai 仍是 L2 可自动发（别把全自动改坏）。"""
    eff = cap_automation_mode("auto_ai", None)
    assert is_autosend_allowed("low", eff) is True


# ── 工作台页面注入：平台降级对坐席可见 ───────────────────────────────────────

def test_workspace_page_ctx_carries_platform_caps():
    """/workspace 渲染上下文携带 platform_mode_caps / platform_draft_skips
    （unified_inbox.html 顶部提示条数据源；坐席能看见「Messenger 已降为人审」）。"""
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.middleware.sessions import SessionMiddleware

    from src.web.routes.unified_inbox_workspace_pages_routes import (
        register_workspace_pages_routes,
    )

    captured = {}

    class _Tpl:
        def TemplateResponse(self, request, name, ctx):
            from fastapi.responses import HTMLResponse
            captured[name] = ctx
            return HTMLResponse("")

    cm = SimpleNamespace(config={
        "inbox": {"auto_draft": {
            "platform_modes": {"Messenger": "REVIEW"},
            "skip_platforms": ["LINE"],
        }},
    })
    def _auth() -> None:  # 无参依赖（lambda request 会被 FastAPI 当 query 参数）
        return None

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    register_workspace_pages_routes(
        app, page_auth=_auth, templates=_Tpl(), config_manager=cm)
    c = TestClient(app)
    r = c.get("/workspace")
    assert r.status_code == 200
    ctx = captured.get("unified_inbox.html") or {}
    assert ctx.get("platform_mode_caps") == {"messenger": "review"}  # 归一小写
    assert ctx.get("platform_draft_skips") == ["line"]
