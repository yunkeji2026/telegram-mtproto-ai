"""全站模板「静态重复 DOM id」门禁。

同一渲染页里出现两个相同 `id="literal"` → `getElementById(id)` 只命中第一个，第二个元素
静默失效（badge 不更新 / 输入读不到 / 按钮拿不到 spinner）。是一类隐蔽的「哑元素」bug。

只看**静态字面 id**（跳过 `<script>` 内 JS 生成的、`{{ }}`/`{% %}` Jinja 动态、以及含 `+`/`${` 拼接的 id），
并剥离 HTML/Jinja 注释——静态检测无法评估 Jinja 分支，故 `{% if %}/{% else %}` 互斥出现的同 id 是
**假阳性**，连同「有意的响应式镜像导航」一起进 `_ACCEPTED_DUP_IDS`（附原因）；确属 bug 但待产品决策的
进 `_PENDING_DUP_IDS`。两张表都有 `test_..._not_stale` 防其过期（修好后强制回收，恢复门禁强度）。
"""
import re
from collections import Counter
from pathlib import Path

_TPL_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
_ALL = sorted(_TPL_DIR.rglob("*.html"))

_SCRIPT = re.compile(r"<script\b.*?</script>", re.S | re.I)
_HTML_CMT = re.compile(r"<!--.*?-->", re.S)
_JINJA_CMT = re.compile(r"\{#.*?#\}", re.S)
_ID = re.compile(r"""\bid\s*=\s*(['"])([^'"]*)\1""")

# 可接受的重复（非 bug）：附原因，修法变动时由 not_stale 检查提醒。
_ACCEPTED_DUP_IDS = {
    # 响应式：桌面侧栏 + 移动抽屉是两套镜像导航、共用同 badge id；base.html 的 setBadge()
    # 已改用 querySelectorAll('[id=..]') 同时更新两套（见该函数注释）。
    "base.html": {"badge-cases", "badge-crisis", "badge-learner"},
    # 互斥 Jinja 分支：{% if has_users %} 密码登录 vs {% else %} 令牌登录，渲染时只出其一，
    # DOM 里永远只有一个 pwd-inp/eye-icon。静态扫描不评估分支 → 假阳性。
    "login.html": {"pwd-inp", "eye-icon"},
}

# 确属 bug、待产品决策（保留哪套 / 改唯一 id）——CI 保绿但债务可见。当前为空：
# whatsapp「对话」pane 的 P7-A 内联检索与「运维」pane 的 P11-B 共享组件检索原共用 wa-hist-q/wa-hist-results，
# 两 pane 同在 DOM → 碰撞；已给 P11-B 换独立 id（wa-ops-hist-*），两套各自独立工作。
_PENDING_DUP_IDS = {}


def _static_dup_ids(html: str) -> set:
    body = _JINJA_CMT.sub("", _HTML_CMT.sub("", _SCRIPT.sub("", html)))
    ids = []
    for m in _ID.finditer(body):
        v = m.group(2)
        if not v or "{{" in v or "{%" in v or "+" in v or "${" in v:
            continue  # 动态/拼接 id 不纳入静态判定
        ids.append(v)
    return {k for k, c in Counter(ids).items() if c >= 2}


def test_no_unexpected_duplicate_static_ids():
    failures = {}
    for f in _ALL:
        dup = _static_dup_ids(f.read_text(encoding="utf-8"))
        allowed = _ACCEPTED_DUP_IDS.get(f.name, set()) | _PENDING_DUP_IDS.get(f.name, set())
        unexpected = sorted(dup - allowed)
        if unexpected:
            failures[f.name] = unexpected
    assert not failures, (
        "有模板存在**重复 DOM id**（getElementById 只命中第一个 → 第二个元素静默失效）：\n"
        + "\n".join(f"  {k}: {v}" for k, v in failures.items())
        + "\n修法：改唯一 id / 删冗余元素；若确为响应式镜像或互斥 Jinja 分支，登记进 _ACCEPTED_DUP_IDS 并注明原因。"
    )


def test_dup_id_allowlist_not_stale():
    """两张表里的项须仍是真重复；若已修（不再重复），提示回收以恢复门禁强度。"""
    stale = {}
    for table in (_ACCEPTED_DUP_IDS, _PENDING_DUP_IDS):
        for name, ids in table.items():
            f = _TPL_DIR / name
            if not f.exists():
                continue
            dup = _static_dup_ids(f.read_text(encoding="utf-8"))
            gone = sorted(set(ids) - dup)
            if gone:
                stale.setdefault(name, []).extend(gone)
    assert not stale, (
        "以下 id 已不再重复（可能已修复），请从 _ACCEPTED_DUP_IDS/_PENDING_DUP_IDS 移除：\n"
        + "\n".join(f"  {k}: {v}" for k, v in stale.items())
    )


def test_scanner_self_check():
    """自测：识别真重复、跳过 <script>/注释/Jinja 动态 id，防扫描器失效变假绿。"""
    good = '<div id="a"></div><div id="b"></div><span id="{{x}}"></span>'
    assert _static_dup_ids(good) == set()

    dup = '<div id="dupe"></div><input id="dupe">'
    assert _static_dup_ids(dup) == {"dupe"}

    # <script> 内 JS 生成的 id、HTML/Jinja 注释里的 id 不算
    masked = (
        '<div id="real"></div>'
        '<script>x.innerHTML=\'<div id="real"></div>\';</script>'
        '<!-- <div id="real"></div> -->'
        "{# <div id=\"real\"></div> #}"
    )
    assert _static_dup_ids(masked) == set()
