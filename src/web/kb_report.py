"""
知识库分析报告生成器
输出自包含 HTML（嵌入 CSS + SVG 图表），可用浏览器打印为 PDF。
无任何外部依赖。
"""
import html
import json
import time
from typing import Optional


def _esc(s) -> str:
    return html.escape(str(s or ""))


def _svg_bar_chart(data: list, width: int = 520, height: int = 100,
                   color: str = "#5b7cf6") -> str:
    """将 (label, value) 列表渲染为 SVG 横向柱状图"""
    if not data:
        return "<p style='color:#999;font-size:.8rem'>暂无数据</p>"
    max_v = max(v for _, v in data) or 1
    bar_h = max(8, (height - 4 * len(data)) // len(data))
    bars = []
    for i, (label, val) in enumerate(data):
        y   = i * (bar_h + 4)
        w   = max(1, int(val / max_v * (width - 160)))
        bars.append(
            f'<text x="0" y="{y + bar_h - 3}" font-size="11" fill="#555" text-anchor="start">{_esc(label[:20])}</text>'
            f'<rect x="150" y="{y}" width="{w}" height="{bar_h}" rx="3" fill="{color}" opacity=".85"/>'
            f'<text x="{150 + w + 4}" y="{y + bar_h - 3}" font-size="11" fill="#555">{val}</text>'
        )
    total_h = len(data) * (bar_h + 4)
    return f'<svg width="{width}" height="{total_h}" xmlns="http://www.w3.org/2000/svg">{"".join(bars)}</svg>'


def _svg_donut(value: int, max_value: int, color: str = "#5b7cf6",
               size: int = 80) -> str:
    """渲染一个简单的环形进度图"""
    pct = min(1.0, value / max_value) if max_value else 0
    r   = (size - 10) / 2
    cx  = cy = size / 2
    circumference = 2 * 3.14159 * r
    dash = pct * circumference
    return (
        f'<svg width="{size}" height="{size}" xmlns="http://www.w3.org/2000/svg">'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#e5e7eb" stroke-width="8"/>'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" stroke-width="8" '
        f'stroke-dasharray="{dash:.1f} {circumference:.1f}" '
        f'stroke-dashoffset="{circumference * .25:.1f}" stroke-linecap="round"/>'
        f'<text x="{cx}" y="{cy + 5}" text-anchor="middle" font-size="14" font-weight="700" fill="#111">'
        f'{int(pct * 100)}%</text>'
        f'</svg>'
    )


def build_kb_report(kb_store, audit_store=None) -> str:
    """构建完整 HTML 报告字符串"""
    now_str  = time.strftime("%Y年%m月%d日 %H:%M")
    ts_gen   = time.strftime("%Y-%m-%dT%H:%M:%S")

    # ── 数据采集 ──────────────────────────────────────────────
    stats = {}
    try:
        stats = kb_store.stats() or {}
    except Exception:
        pass

    total_entries = stats.get("total_entries", 0)
    enabled       = stats.get("enabled_entries", 0)
    disabled      = total_entries - enabled

    # 健康诊断
    advice_data = {}
    try:
        advice_data = kb_store.get_maintenance_advice() or {}
    except Exception:
        pass
    health_score = advice_data.get("score", 0)
    health_grade = advice_data.get("grade", "—")

    # 命中率（今日）
    hit_data = {}
    try:
        hit_data = kb_store.get_today_hit_rate() or {}
    except Exception:
        pass
    today_queries = hit_data.get("total", 0)
    today_hits    = hit_data.get("hit", 0)
    today_hit_pct = hit_data.get("hit_rate", 0)

    # 7 天命中率趋势
    analytics_7d = {}
    try:
        analytics_7d = kb_store.get_query_analytics(hours=168) or {}
    except Exception:
        pass

    hourly = analytics_7d.get("hourly", [])
    # 聚合为天级（每 24 个小时桶合并）
    day_rates: list = []
    if hourly:
        import datetime as _dt
        day_map: dict = {}
        for row in hourly:
            day = str(row.get("hour", ""))[:10]
            if day not in day_map:
                day_map[day] = {"total": 0, "hit": 0}
            day_map[day]["total"] += row.get("total", 0)
            day_map[day]["hit"]   += row.get("hit", 0)
        for day in sorted(day_map.keys())[-7:]:
            t = day_map[day]["total"]
            h = day_map[day]["hit"]
            pct = round(h / t * 100) if t else 0
            day_rates.append((day[5:], pct))  # "MM-DD", pct

    # Top 10 使用最多条目
    top_entries: list = []
    try:
        with kb_store._conn() as c:
            rows = c.execute(
                "SELECT id, title, category, use_count FROM kb_entries "
                "WHERE enabled=1 ORDER BY use_count DESC LIMIT 10"
            ).fetchall()
            top_entries = [dict(r) for r in rows]
    except Exception:
        pass

    # Miss log
    miss_entries: list = []
    try:
        with kb_store._conn() as c:
            rows = c.execute(
                "SELECT query, cnt FROM kb_miss_log ORDER BY cnt DESC LIMIT 15"
            ).fetchall()
            miss_entries = [dict(r) for r in rows]
    except Exception:
        pass

    # 翻译覆盖率
    trans_coverage: dict = {}
    try:
        with kb_store._conn() as c:
            for lang in ("en", "ur", "pt", "ar"):
                n = c.execute(
                    "SELECT COUNT(DISTINCT entry_id) FROM kb_translations WHERE lang=?",
                    (lang,),
                ).fetchone()[0]
                trans_coverage[lang] = round(n / total_entries * 100) if total_entries else 0
    except Exception:
        pass

    # Embedding 覆盖率
    embed_pct = 0
    try:
        ec = kb_store.embedding_coverage()
        embed_pct = ec.get("coverage_pct", 0)
    except Exception:
        pass

    # 最近操作（审计日志）
    recent_ops: list = []
    if audit_store:
        try:
            recent_ops = audit_store.query(limit=5) or []
        except Exception:
            pass

    # ── 维护建议渲染 ─────────────────────────────────────────
    advice_list = advice_data.get("advice", [])
    advice_html = ""
    if advice_list:
        items = []
        for a in advice_list[:10]:
            pri = a.get("priority", "low")
            col = {"high": "#ef4444", "medium": "#f59e0b", "low": "#6b7280"}.get(pri, "#6b7280")
            icon = {"high": "🔴", "medium": "🟡", "low": "🔵"}.get(pri, "●")
            items.append(
                f'<tr><td>{icon}</td>'
                f'<td style="color:{col};font-size:.75rem">{_esc(a.get("priority",""))}</td>'
                f'<td style="font-weight:600">{_esc(a.get("title",""))}</td>'
                f'<td style="color:#555;font-size:.8rem">{_esc(a.get("message",""))}</td></tr>'
            )
        advice_html = (
            '<table class="data-table">'
            '<thead><tr><th></th><th>级别</th><th>标题</th><th>说明</th></tr></thead>'
            '<tbody>' + "".join(items) + '</tbody></table>'
        )
    else:
        advice_html = '<p style="color:#10b981">✅ 知识库状态良好，暂无维护建议</p>'

    # ── Top 10 条目表格 ──────────────────────────────────────
    top_html = ""
    if top_entries:
        rows_html = "".join(
            f'<tr><td>{i+1}</td>'
            f'<td><span class="cat-badge">{_esc(e["category"])}</span></td>'
            f'<td>{_esc(e["title"])}</td>'
            f'<td style="font-weight:700;color:#5b7cf6">{e["use_count"]}</td></tr>'
            for i, e in enumerate(top_entries)
        )
        top_html = (
            '<table class="data-table">'
            '<thead><tr><th>#</th><th>分类</th><th>标题</th><th>使用次数</th></tr></thead>'
            '<tbody>' + rows_html + '</tbody></table>'
        )
    else:
        top_html = '<p style="color:#999;font-size:.85rem">暂无使用记录</p>'

    # ── Miss Log 表格 ────────────────────────────────────────
    miss_html = ""
    if miss_entries:
        rows_html = "".join(
            f'<tr><td>{_esc(m["query"])}</td>'
            f'<td style="font-weight:700;color:#ef4444">{m["cnt"]}</td></tr>'
            for m in miss_entries
        )
        miss_html = (
            '<table class="data-table">'
            '<thead><tr><th>未命中查询词</th><th>次数</th></tr></thead>'
            '<tbody>' + rows_html + '</tbody></table>'
        )
    else:
        miss_html = '<p style="color:#10b981;font-size:.85rem">✅ 暂无未命中记录</p>'

    # ── 命中率趋势图（SVG）───────────────────────────────────
    if day_rates:
        trend_svg = _svg_bar_chart(
            [(d, v) for d, v in day_rates], width=460, height=min(200, len(day_rates) * 28), color="#5b7cf6"
        )
    else:
        trend_svg = "<p style='color:#999;font-size:.85rem'>暂无足够数据</p>"

    # ── 翻译覆盖率环形图 ─────────────────────────────────────
    lang_labels = {"en": "英语", "ur": "乌尔都语", "pt": "葡萄牙语", "ar": "阿拉伯语"}
    trans_html = "".join(
        f'<div style="text-align:center;padding:.5rem">'
        f'{_svg_donut(trans_coverage.get(lang, 0), 100)}'
        f'<div style="font-size:.7rem;color:#555;margin-top:.2rem">{lang_labels.get(lang, lang)}'
        f'<br><b>{trans_coverage.get(lang, 0)}%</b></div></div>'
        for lang in ("en", "ur", "pt", "ar")
    )

    # ── 健康评分颜色 ─────────────────────────────────────────
    score_color = "#10b981" if health_score >= 80 else "#f59e0b" if health_score >= 60 else "#ef4444"

    # ────────────────────────────────────────────────────────────────────────
    # 组装 HTML
    # ────────────────────────────────────────────────────────────────────────
    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>知识库分析报告 · {_esc(now_str)}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Inter','PingFang SC','Microsoft YaHei',system-ui,sans-serif;
  color:#111827;background:#f8fafc;line-height:1.6;font-size:14px}}
.page{{max-width:900px;margin:0 auto;padding:2rem 1.5rem 4rem}}
h1{{font-size:1.6rem;font-weight:800;margin-bottom:.2rem}}
h2{{font-size:1.05rem;font-weight:700;margin-bottom:.8rem;padding-bottom:.4rem;
  border-bottom:2px solid #5b7cf6;color:#1e293b;display:flex;align-items:center;gap:.4rem}}
h2 svg{{width:16px;height:16px;flex-shrink:0}}
.meta{{color:#6b7280;font-size:.8rem;margin-bottom:2rem}}
.section{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;
  padding:1.4rem 1.6rem;margin-bottom:1.2rem;box-shadow:0 1px 4px rgba(0,0,0,.06)}}
/* KPI cards */
.kpi-row{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:.8rem;margin-bottom:1.2rem}}
.kpi{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:.9rem 1rem;text-align:center}}
.kpi-val{{font-size:1.6rem;font-weight:800;color:#5b7cf6;line-height:1}}
.kpi-lbl{{font-size:.72rem;color:#6b7280;margin-top:.25rem}}
.kpi.ok .kpi-val{{color:#10b981}}.kpi.warn .kpi-val{{color:#f59e0b}}.kpi.err .kpi-val{{color:#ef4444}}
/* table */
.data-table{{width:100%;border-collapse:collapse;font-size:.82rem}}
.data-table th{{background:#f1f5f9;padding:.5rem .7rem;text-align:left;font-weight:600;
  color:#475569;border-bottom:2px solid #e5e7eb;font-size:.75rem}}
.data-table td{{padding:.5rem .7rem;border-bottom:1px solid #f1f5f9;vertical-align:top}}
.data-table tr:last-child td{{border-bottom:none}}
.data-table tbody tr:hover{{background:#f8fafc}}
.cat-badge{{display:inline-block;background:#ede9fe;color:#6d28d9;border-radius:20px;
  padding:.12rem .55rem;font-size:.7rem;font-weight:600;white-space:nowrap}}
.health-score{{display:inline-flex;align-items:center;gap:.6rem;padding:.5rem 1rem;
  border-radius:8px;background:{score_color}1a;border:1px solid {score_color}44}}
.health-score .score-num{{font-size:2rem;font-weight:800;color:{score_color};line-height:1}}
.score-grade{{font-size:1rem;font-weight:700;color:{score_color}}}
.trans-row{{display:flex;gap:.5rem;flex-wrap:wrap}}
.print-btn{{position:fixed;bottom:1.5rem;right:1.5rem;padding:.6rem 1.2rem;background:#5b7cf6;
  color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:.85rem;font-weight:600;
  box-shadow:0 4px 12px rgba(91,124,246,.4);transition:.2s;z-index:100}}
.print-btn:hover{{background:#4366e0;transform:translateY(-1px)}}
@media print{{.print-btn{{display:none}};body{{background:#fff}};.section{{box-shadow:none}}}}
</style>
</head>
<body>
<button class="print-btn" onclick="window.print()">🖨 打印 / 导出 PDF</button>
<div class="page">
  <h1>📊 知识库分析报告</h1>
  <div class="meta">生成时间：{_esc(now_str)} · 数据截止时间：{_esc(ts_gen)}</div>

  <!-- KPI 卡片 -->
  <div class="kpi-row">
    <div class="kpi"><div class="kpi-val">{total_entries}</div><div class="kpi-lbl">知识条目总数</div></div>
    <div class="kpi ok"><div class="kpi-val">{enabled}</div><div class="kpi-lbl">启用中</div></div>
    <div class="kpi {'warn' if disabled>0 else ''}"><div class="kpi-val">{disabled}</div><div class="kpi-lbl">已停用</div></div>
    <div class="kpi"><div class="kpi-val">{today_queries}</div><div class="kpi-lbl">今日查询次数</div></div>
    <div class="kpi {'ok' if today_hit_pct>=70 else 'warn' if today_hit_pct>=40 else 'err'}">
      <div class="kpi-val">{today_hit_pct}%</div><div class="kpi-lbl">今日命中率</div></div>
    <div class="kpi"><div class="kpi-val">{embed_pct}%</div><div class="kpi-lbl">向量化覆盖率</div></div>
  </div>

  <!-- 健康评分 -->
  <div class="section">
    <h2>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
      知识库健康评分
    </h2>
    <div style="display:flex;align-items:center;gap:1.5rem;flex-wrap:wrap;margin-bottom:1rem">
      <div class="health-score">
        <span class="score-num">{health_score}</span>
        <div>
          <div class="score-grade">/ 100 · {_esc(health_grade)}</div>
          <div style="font-size:.72rem;color:#6b7280">综合健康评分</div>
        </div>
      </div>
    </div>
    {advice_html}
  </div>

  <!-- 命中率趋势 -->
  <div class="section">
    <h2>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
      命中率趋势（近 7 天，%）
    </h2>
    {trend_svg}
  </div>

  <!-- Top 10 条目 -->
  <div class="section">
    <h2>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>
      Top 10 使用最多条目
    </h2>
    {top_html}
  </div>

  <!-- 未命中词云 -->
  <div class="section">
    <h2>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      未命中高频查询（知识空缺提示）
    </h2>
    {miss_html}
  </div>

  <!-- 翻译覆盖率 -->
  <div class="section">
    <h2>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 8l6 6"/><path d="M4 14l6-6 2-3"/><path d="M2 5h7"/><path d="M7 2h1"/><path d="M22 22l-5-10-5 10"/><path d="M14 18h6"/></svg>
      多语言翻译覆盖率
    </h2>
    <div class="trans-row">{trans_html}</div>
  </div>

  <div style="text-align:center;color:#9ca3af;font-size:.75rem;margin-top:2rem;border-top:1px solid #e5e7eb;padding-top:1rem">
    本报告由 AI智能客服系统 自动生成 · {_esc(now_str)}
  </div>
</div>
</body>
</html>
"""
    return html_content
