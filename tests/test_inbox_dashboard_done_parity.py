"""收件箱 KPI「已成交」与经营看板「已成交」卡片可视对齐门禁（P5-3）。

P5-2/2b/2c 已在**数据层**把「成交/完成」阶段收敛到单一来源（`models.FUNNEL_DONE_STAGES`
经 `funnel_done_stages` 注入两模板）。但既有单源门禁只校验「模板里出现了注入变量」，
**没有锁死「屏幕上那个数字到底怎么算出来的」**——有人若在 `_updateTodayStrip` 里私加一句
`if(c.funnel_stage==='CONVERTED') done++;`，注入变量依旧在别处出现，旧门禁照样绿，
用户却会看到收件箱 KPI 与看板两个不一致的「已成交」数。

本门禁把「单源」从代码延伸到**用户可见的取数路径**：收件箱 KPI 计数 / done 筛选计数 /
done 筛选应用，以及看板 done 卡片，都必须**唯一地**经由各自的单源判定
（`_isFunnelDone` / `_doneCountFromStages` over `_FUNNEL_DONE_SET`），且两模板的兜底口径
与后端权威集合一致——从而两处数字必然同口径。

纯文件扫描（内容匹配，抗行号漂移）→ 常驻门禁。
"""
import re
from pathlib import Path

from src.contacts import models

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates"
_INBOX = _TPL / "unified_inbox.html"
_DASH = _TPL / "workspace_dashboard.html"


def _fallback_stage_set(html: str, var_expr: str) -> set:
    """从 `{{ <var_expr> or [ '...','...' ] }}` 里抽出兜底阶段集合。"""
    m = re.search(re.escape(var_expr) + r"\s+or\s+(\[[^\]]*\])", html)
    assert m, f"未找到 `{var_expr} or [...]` 注入+兜底表达式"
    return set(re.findall(r"'([A-Z_]+)'", m.group(1)))


# ── 收件箱：done 取数唯一经由 _isFunnelDone（单源判定） ─────────────────

def test_inbox_done_source_defined_once():
    html = _INBOX.read_text(encoding="utf-8")
    # 权威集合注入唯一一次
    assert html.count("const _FUNNEL_DONE=new Set(") == 1, (
        "_FUNNEL_DONE 注入定义应恰好一次（多份=潜在口径漂移）"
    )
    # 集合只有 _isFunnelDone 一个读取者：`_FUNNEL_DONE.has(` 只出现一次
    assert html.count("_FUNNEL_DONE.has(") == 1, (
        "_FUNNEL_DONE 应只由 _isFunnelDone 读取；出现第二处直接读取集合即为旁路取数"
    )


def test_inbox_kpi_and_filter_use_single_source():
    html = _INBOX.read_text(encoding="utf-8")
    # KPI 数字（today strip）计数走单源判定
    assert "if(_isFunnelDone(c)) done++;" in html, (
        "收件箱「已成交」KPI 计数未走单源 _isFunnelDone（疑似私自重算）"
    )
    # done 筛选计数 + done 筛选应用均走单源判定
    assert "if(f==='done') return base.filter(c=>_isFunnelDone(c)).length;" in html, (
        "done 筛选计数未走单源 _isFunnelDone"
    )
    assert "if(chatFilter==='done' && !_isFunnelDone(c)) return false;" in html, (
        "done 筛选应用未走单源 _isFunnelDone"
    )


def test_inbox_done_counter_has_no_bypass():
    """任何 `done++`（KPI 计数自增）都必须被单源判定 `_isFunnelDone` 守卫——
    杜绝新增旁路计数（这是旧单源门禁抓不到的真实漂移点）。"""
    html = _INBOX.read_text(encoding="utf-8")
    bad = [ln.strip() for ln in html.splitlines()
           if "done++" in ln and "_isFunnelDone" not in ln]
    assert not bad, (
        "存在未经单源判定守卫的「已成交」计数自增（会与看板口径漂移）：\n"
        + "\n".join(bad)
    )


# ── 看板：done 卡片数字唯一经由 _doneCountFromStages over _FUNNEL_DONE_SET ──

def test_dashboard_done_card_uses_single_source():
    html = _DASH.read_text(encoding="utf-8")
    assert html.count("var FUNNEL_DONE = ") == 1, "看板 FUNNEL_DONE 注入应恰好一次"
    # done 集合由注入 FUNNEL_DONE 派生
    assert "FUNNEL_DONE.forEach(function(s){ _FUNNEL_DONE_SET[s]=1; });" in html, (
        "看板 _FUNNEL_DONE_SET 未从注入的 FUNNEL_DONE 派生"
    )
    # 计数器读取该集合
    assert "if(_FUNNEL_DONE_SET[k]) n+=sc[k]||0;" in html, (
        "看板 _doneCountFromStages 未读取单源 _FUNNEL_DONE_SET"
    )
    # done 卡片数字唯一经由 _doneCountFromStages（不硬算阶段和）
    assert "_doneCard(_doneCountFromStages(" in html, (
        "看板「已成交」卡片数字未走单源 _doneCountFromStages"
    )


# ── 跨模板：两侧兜底口径彼此一致且都 == 后端权威集合 ─────────────────────

def test_cross_template_done_basis_identical_and_authoritative():
    inbox_html = _INBOX.read_text(encoding="utf-8")
    dash_html = _DASH.read_text(encoding="utf-8")
    authority = set(models.FUNNEL_DONE_STAGES)

    inbox_fb = _fallback_stage_set(inbox_html, "funnel_done_stages")
    dash_fb = _fallback_stage_set(dash_html, "funnel_done_stages")

    assert inbox_fb == authority, (
        f"收件箱 done 兜底与权威漂移：{inbox_fb} vs {authority}"
    )
    assert dash_fb == authority, (
        f"看板 done 兜底与权威漂移：{dash_fb} vs {authority}"
    )
    # 二者互等（同口径的必要条件；正常路径走同一后端注入，兜底也须一致）
    assert inbox_fb == dash_fb, (
        f"收件箱与看板 done 兜底口径不一致：inbox={inbox_fb} dash={dash_fb}"
    )


# ── P5-6：狭义「实际成交」(won) 小数字同样单源、两侧同口径 ──────────────────

def test_inbox_won_number_uses_single_source():
    html = _INBOX.read_text(encoding="utf-8")
    # KPI won 小数字计数走单源 _isWon（P5-2b 已保证 _isWon 消费注入 won_stages）
    assert "if(_isWon(c)) won++;" in html, "收件箱「成交」小数字计数未走单源 _isWon"
    assert "set('ts-won',won);" in html, "收件箱未把 won 写入 ts-won"


def test_inbox_won_counter_has_no_bypass():
    """任何 `won++` 自增都必须被单源判定 `_isWon` 守卫，杜绝旁路重算。"""
    html = _INBOX.read_text(encoding="utf-8")
    bad = [ln.strip() for ln in html.splitlines()
           if "won++" in ln and "_isWon" not in ln]
    assert not bad, "存在未经单源判定守卫的「成交」计数自增：\n" + "\n".join(bad)


def test_dashboard_won_number_uses_single_source():
    html = _DASH.read_text(encoding="utf-8")
    assert html.count("var WON_STAGES = ") == 1, "看板 WON_STAGES 注入应恰好一次"
    assert "WON_STAGES.forEach(function(s){ _WON_SET[s]=1; });" in html, (
        "看板 _WON_SET 未从注入的 WON_STAGES 派生"
    )
    assert "if(_WON_SET[k]) n+=sc[k]||0;" in html, (
        "看板 _wonCountFromStages 未读取单源 _WON_SET"
    )
    # done 卡片的 won 小数字唯一经由 _wonCountFromStages
    assert "_wonCountFromStages(d.stage_counts)" in html, (
        "看板「成交」小数字未走单源 _wonCountFromStages"
    )


def test_cross_template_won_basis_identical_and_authoritative():
    inbox_html = _INBOX.read_text(encoding="utf-8")
    dash_html = _DASH.read_text(encoding="utf-8")
    authority = set(models.WON_STAGES)

    inbox_fb = _fallback_stage_set(inbox_html, "won_stages")
    dash_fb = _fallback_stage_set(dash_html, "won_stages")

    assert inbox_fb == authority, f"收件箱 won 兜底与权威漂移：{inbox_fb} vs {authority}"
    assert dash_fb == authority, f"看板 won 兜底与权威漂移：{dash_fb} vs {authority}"
    assert inbox_fb == dash_fb, (
        f"收件箱与看板 won 兜底口径不一致：inbox={inbox_fb} dash={dash_fb}"
    )
