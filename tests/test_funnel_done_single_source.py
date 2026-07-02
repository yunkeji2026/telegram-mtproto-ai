"""「成交/完成」阶段单一来源门禁（P5-2）。

收件箱 done 筛选 / 「已成交」KPI / 经营看板「已成交」卡片三处口径必须一致，
历史上靠各写一份硬编码集合，极易漂移。本门禁锁死：

1. 权威集合 `FUNNEL_DONE_STAGES` 存在、非空、成员都是合法 Journey 阶段常量、含核心成交态。
2. 页面壳路由把它经 `funnel_done_stages` 注入模板 context（前端单一数据源）。
3. 收件箱 / 看板两模板确实**消费注入变量**（`funnel_done_stages`），而非退回硬编码。

纯 import + 文件扫描 → 常驻门禁。
"""
import ast
import re
from pathlib import Path

from src.contacts import models

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates"
_SRC = _ROOT / "src"
_ROUTE = _ROOT / "src" / "web" / "routes" / "unified_inbox_workspace_pages_routes.py"


def _all_stage_values() -> set[str]:
    return {v for k, v in vars(models).items()
            if k.startswith("STAGE_") and isinstance(v, str)}


def test_funnel_done_stages_wellformed():
    s = models.FUNNEL_DONE_STAGES
    assert isinstance(s, frozenset) and s, "FUNNEL_DONE_STAGES 应为非空 frozenset"
    valid = _all_stage_values()
    unknown = s - valid
    assert not unknown, f"FUNNEL_DONE_STAGES 含非法阶段值：{unknown}"
    # 核心「实际成交」态必须在集合内（狭义成交）
    assert models.STAGE_BONDED in s
    assert models.STAGE_CONVERTED in s


def test_page_ctx_injects_funnel_done_stages():
    src = _ROUTE.read_text(encoding="utf-8")
    assert "FUNNEL_DONE_STAGES" in src, "路由未引用权威常量"
    assert '"funnel_done_stages"' in src, "路由未把 funnel_done_stages 注入 page ctx"


def test_templates_consume_injected_funnel_done_stages():
    for name in ("unified_inbox.html", "workspace_dashboard.html"):
        html = (_TPL / name).read_text(encoding="utf-8")
        assert "funnel_done_stages" in html, (
            f"{name} 未消费注入的 funnel_done_stages（疑似退回硬编码，会漂移）"
        )


def test_inbox_done_set_matches_authority():
    """收件箱回落硬编码集合（注入缺失时的兜底）应与权威集合一致，防兜底漂移。"""
    html = (_TPL / "unified_inbox.html").read_text(encoding="utf-8")
    m = re.search(r"funnel_done_stages\s+or\s+(\[[^\]]*\])", html)
    assert m, "未找到收件箱 _FUNNEL_DONE 的注入+兜底表达式"
    fallback = set(re.findall(r"'([A-Z_]+)'", m.group(1)))
    assert fallback == set(models.FUNNEL_DONE_STAGES), (
        f"收件箱兜底集合与权威集合漂移：fallback={fallback} vs authority={set(models.FUNNEL_DONE_STAGES)}"
    )


# ── P5-2b：狭义「实际成交」WON_STAGES 单一来源 ──────────────────────────

def test_won_stages_wellformed_and_subset():
    w = models.WON_STAGES
    assert isinstance(w, frozenset) and w, "WON_STAGES 应为非空 frozenset"
    assert w == {models.STAGE_BONDED, models.STAGE_CONVERTED}
    # won（实际成交）必是 done（成功桶）的子集，语义自洽
    assert w <= models.FUNNEL_DONE_STAGES


def test_page_ctx_injects_won_stages():
    src = _ROUTE.read_text(encoding="utf-8")
    assert "WON_STAGES" in src and '"won_stages"' in src, "路由未注入 won_stages"


def test_inbox_won_set_matches_authority():
    html = (_TPL / "unified_inbox.html").read_text(encoding="utf-8")
    m = re.search(r"won_stages\s+or\s+(\[[^\]]*\])", html)
    assert m, "未找到收件箱 _WON_STAGES 的注入+兜底表达式"
    fallback = set(re.findall(r"'([A-Z_]+)'", m.group(1)))
    assert fallback == set(models.WON_STAGES), (
        f"收件箱 won 兜底与权威漂移：{fallback} vs {set(models.WON_STAGES)}"
    )


def test_inbox_isdone_uses_single_source():
    """`_isDone` 必须走单源 `_isWon(c)`，不得再退回硬编码 BONDED/CONVERTED 比较。"""
    html = (_TPL / "unified_inbox.html").read_text(encoding="utf-8")
    assert "const _isDone=_isWon(c);" in html, "_isDone 未走单源 _isWon"
    assert "c.funnel_stage==='BONDED'||c.funnel_stage==='CONVERTED'" not in html, (
        "_isDone 仍存在硬编码成交判定（应改为 _isWon）"
    )


# ── P5-2b：漏斗阶段标签前后端 parity（防阶段增删漂移） ─────────────────

def test_funnel_stage_labels_parity_backend_vs_frontend():
    from src.web.routes.unified_inbox_helpers import FUNNEL_STAGE_LABELS

    html = (_TPL / "unified_inbox.html").read_text(encoding="utf-8")
    m = re.search(r"const FUNNEL = \{(.*?)\};", html)
    assert m, "未找到前端 FUNNEL 阶段标签对象"
    fe_keys = set(re.findall(r"([A-Z_]+):window\.T\('inbox\.funnel\.", m.group(1)))
    be_keys = set(FUNNEL_STAGE_LABELS.keys())
    assert fe_keys == be_keys, (
        "漏斗阶段前后端不一致（新增/删阶段漏改一处）：\n"
        f"  仅后端有：{be_keys - fe_keys}\n  仅前端有：{fe_keys - be_keys}"
    )


# ── P5-2c：后端硬编码扫描门禁（防再复制一份权威成交集合） ─────────────────
#
# 「单一来源」在前端由注入 + parity 门禁保证；后端同样需要固化：任何模块若把
# {BONDED,CONVERTED}（WON_STAGES）或 {LINE_ACCEPTED,LINE_ENGAGED,BONDED,CONVERTED}
# （FUNNEL_DONE_STAGES）**原样再硬编码一份**（set/list/tuple/frozenset(...) 字面量），
# 就是新的漂移源，必须改为 `from src.contacts.models import ...`。
#
# 设计取舍（吸取「哑按钮门禁」教训——宁可精确也不要满屏误报）：
# - 只标记**恰好等于**某权威集合的字面量；自定义子集（如 {LINE_ENGAGED,BONDED,CONVERTED}
#   或含 HANDOFF_SENT 的更大集合）语义不同，**不**误报。
# - 走 AST 而非正则，稳；SQL 字符串片段/`by.get("BONDED")` 计数读取/标签 dict 天然不匹配。
# - `# stage-source-allow` 行内标记豁免（models import 失败兜底等非判定用途）。

_AUTHORITY_STAGE_SETS = {
    "WON_STAGES": frozenset(models.WON_STAGES),
    "FUNNEL_DONE_STAGES": frozenset(models.FUNNEL_DONE_STAGES),
}


def _iter_str_literal_collections(tree):
    """产出 (node, frozenset[str]) —— set/list/tuple 字面量或 set()/frozenset() 包裹，
    且元素**全为字符串常量**（≥2 个）。非纯字符串集合直接跳过。"""
    for node in ast.walk(tree):
        elts = None
        if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
            elts = node.elts
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("set", "frozenset")
            and len(node.args) == 1
            and isinstance(node.args[0], (ast.Set, ast.List, ast.Tuple))
        ):
            elts = node.args[0].elts
        if elts is None or len(elts) < 2:
            continue
        vals = []
        ok = True
        for e in elts:
            if isinstance(e, ast.Constant) and isinstance(e.value, str):
                vals.append(e.value)
            else:
                ok = False
                break
        if ok:
            yield node, frozenset(vals)


def test_no_backend_rehardcoded_won_or_done_sets():
    offenders = []
    for py in _SRC.rglob("*.py"):
        # 权威定义处豁免
        if py.name == "models.py" and py.parent.name == "contacts":
            continue
        text = py.read_text(encoding="utf-8")
        lines = text.splitlines()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node, elems in _iter_str_literal_collections(tree):
            for name, aset in _AUTHORITY_STAGE_SETS.items():
                if elems != aset:
                    continue
                ln = getattr(node, "lineno", 0)
                line_txt = lines[ln - 1] if 0 < ln <= len(lines) else ""
                if "stage-source-allow" in line_txt:
                    continue
                rel = py.relative_to(_ROOT).as_posix()
                offenders.append(
                    f"{rel}:{ln} 原样硬编码 {name}={sorted(elems)}"
                    f"（应 `from src.contacts.models import {name}`）"
                )
    assert not offenders, (
        "后端存在重复硬编码的成交/完成阶段集合，破坏单一来源（P5-2c）：\n"
        + "\n".join(offenders)
        + "\n（若确为非判定用途的兜底，行尾加 `# stage-source-allow` 豁免）"
    )


def test_all_stages_classified_into_exactly_one_bucket():
    """阶段完备性门禁（P5-2c 延伸）：每个 STAGE_* 必属且仅属一个语义桶。

    四桶 = 成功桶(FUNNEL_DONE) / 进行中(IN_PROGRESS) / 流失(LOST) / 系统态(SPECIAL)，
    须对全部 STAGE_* **无重无漏**。新增阶段忘归类 → 门禁点名，杜绝被 KPI/漏斗分析静默漏算。
    （WON_STAGES 是 FUNNEL_DONE 的子分类，正交，不入本划分。）
    """
    all_stages = _all_stage_values()
    buckets = {
        "FUNNEL_DONE_STAGES": set(models.FUNNEL_DONE_STAGES),
        "IN_PROGRESS_STAGES": set(models.IN_PROGRESS_STAGES),
        "LOST_STAGES": set(models.LOST_STAGES),
        "SPECIAL_STAGES": set(models.SPECIAL_STAGES),
    }
    union = set().union(*buckets.values())

    unclassified = all_stages - union
    assert not unclassified, (
        f"新增 STAGE_* 未归类进任何语义桶（会被 KPI/漏斗分析静默漏算）：{unclassified}\n"
        "请把它归入 models 的 FUNNEL_DONE_STAGES / IN_PROGRESS_STAGES / LOST_STAGES / SPECIAL_STAGES 之一"
    )
    extra = union - all_stages
    assert not extra, f"分区桶含非法（非 STAGE_* 常量）阶段值：{extra}"

    names = list(buckets)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            overlap = buckets[names[i]] & buckets[names[j]]
            assert not overlap, (
                f"阶段桶重叠——一个阶段只能属一个桶：{names[i]} ∩ {names[j]} = {overlap}"
            )


def _iter_eq_string_boolchains(tree):
    """产出 (node, frozenset[str]) —— `x=='A' or x=='B' ...` 这类全为 `==字符串常量` 的 Or 链。

    这是集合字面量之外的另一种绕过单一来源写法（把 {BONDED,CONVERTED} 拆成 == 或链）。
    """
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)):
            continue
        strs, ok = [], True
        for v in node.values:
            if not (isinstance(v, ast.Compare) and len(v.ops) == 1
                    and isinstance(v.ops[0], ast.Eq)):
                ok = False
                break
            operands = [v.left] + list(v.comparators)
            const = [o.value for o in operands
                     if isinstance(o, ast.Constant) and isinstance(o.value, str)]
            if len(const) != 1:
                ok = False
                break
            strs.append(const[0])
        if ok and strs:
            yield node, frozenset(strs)


def test_no_backend_rehardcoded_won_or_done_bool_chains():
    """P5-2c 收口：禁止用 `==` 或链把权威成交集合拆写一份（另一种漂移源）。

    与集合字面量门禁同口径——只精确匹配**恰好等于**某权威集合的或链，自定义或链不误报。
    """
    offenders = []
    for py in _SRC.rglob("*.py"):
        if py.name == "models.py" and py.parent.name == "contacts":
            continue
        text = py.read_text(encoding="utf-8")
        lines = text.splitlines()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node, elems in _iter_eq_string_boolchains(tree):
            for name, aset in _AUTHORITY_STAGE_SETS.items():
                if elems != aset:
                    continue
                ln = getattr(node, "lineno", 0)
                line_txt = lines[ln - 1] if 0 < ln <= len(lines) else ""
                if "stage-source-allow" in line_txt:
                    continue
                rel = py.relative_to(_ROOT).as_posix()
                offenders.append(
                    f"{rel}:{ln} 用 == 或链重复判定 {name}={sorted(elems)}"
                    f"（应 `stage in models.{name}`）"
                )
    assert not offenders, (
        "后端存在用 `==` 或链重复判定成交/完成阶段，绕过集合单一来源（P5-2c）：\n"
        + "\n".join(offenders)
        + "\n（改用 `stage in models.WON_STAGES / FUNNEL_DONE_STAGES`）"
    )
