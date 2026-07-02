"""i18n 覆盖门禁。

工作台模板里用到的每个 i18n key（data-i18n 锚点 + JS 的 T()）必须在 zh / en
两套字典都存在，否则前端会静默回落成 key 字符串（半成品翻译上线、无人报错）。
纯函数 / 纯文件扫描 → 常驻门禁，秒级。
"""
import pytest

from scripts.i18n_scan import (
    _count_untagged_cjk,
    _iter_used_keys,
    _strip_comments,
    scan_workspace_i18n,
)


def test_strip_comments_removes_comment_cjk_keeps_strings():
    """注释里的中文应被剥离；字符串里的中文与 URL 的 // 应保留。"""
    assert "注释" not in _strip_comments("foo(); // 注释\n")
    assert "块" not in _strip_comments("/* 块\n注释 */code")
    assert "中文" not in _strip_comments("<!-- 中文 -->ok")
    assert "提示" not in _strip_comments("{# 提示 #}code")
    # URL 的 // 前置是 ':'，不应被当行注释吞掉
    assert "https://x" in _strip_comments("var u='https://x/p';\n")


def test_strip_comments_regex_literal_with_quote_not_desynced():
    """回归：JS 正则字面量里的引号曾让 token 器误判字符串、吞掉后续可见中文。

    正则删除法不 token 化字符串，故行尾注释被删、下一行用户可见字符串保留。
    """
    src = "x.replace(/'/g,''); // 注\ny='可见';\n"
    out = _strip_comments(src)
    assert "注" not in out
    assert "可见" in out


def test_count_untagged_excludes_comments_and_hooks():
    assert _count_untagged_cjk("foo(); // 中文注释\n") == 0          # 纯注释行
    assert _count_untagged_cjk("el.textContent='你好';\n") == 1      # 裸用户可见串 → 计
    assert _count_untagged_cjk('<span data-i18n="x">你好</span>') == 0  # 有 i18n 锚点
    assert _count_untagged_cjk("x=window.T('a.b'); // 你好\n") == 0   # 有 T() 调用


def test_count_untagged_strips_statement_tail_comment():
    """``stmt();// 中文`` 语句尾行注释（`;` 紧贴 `//`）也应被剥离，不计入裸串。"""
    assert _count_untagged_cjk("let s=new Set();// 已提醒过的群\n") == 0
    # URL 的 `://` 前置是 `:`，不在 [\\s;] 集合内 → 不误删
    assert "https://x" in _strip_comments("var u='https://x';\n")


def test_count_untagged_skips_multiline_data_i18n():
    """多行开标签：``data-i18n`` 在上一行、文本续到下一行时，下一行中文不应误判未接。"""
    multiline = (
        '<button class="b" title="t"\n'
        '  data-i18n="inbox.rt.media" data-i18n-title="inbox.rt.media_t"\n'
        '  onclick="go()">🖼 媒体</button>\n'
    )
    assert _count_untagged_cjk(multiline) == 0
    # 反例：单行元素但无 data-i18n → 仍应计为裸串（不被误跳过）
    assert _count_untagged_cjk('<button onclick="go()">媒体</button>\n') == 1


def test_iter_used_keys_recognizes_server_side_i18n():
    """服务端 Jinja i18n（``i18n.get('key',…)`` / ``i18n['key']``）也算「用到的 key」，纳入门禁校验。"""
    keys = set(_iter_used_keys(
        "<title>{{ i18n.get('inbox.page_title', '聊天工作台') }}</title>\n"
        "<x>{{ i18n['dash.page_title'] }}</x>\n"
    ))
    assert "inbox.page_title" in keys
    assert "dash.page_title" in keys
    # 整包注入 ``window.WS_I18N = {{ i18n|tojson }}`` 不应被误当成具体 key
    assert not any("tojson" in k or "|" in k for k in _iter_used_keys("x = {{ i18n|tojson }};\n"))


def test_count_untagged_skips_server_side_i18n_title():
    """``{% block title %}{{ i18n.get('k','中文') }}{% endblock %}`` 的内联中文回落不算未接 i18n。"""
    assert _count_untagged_cjk(
        "{% block title %}{{ i18n.get('inbox.page_title', '聊天工作台') }}{% endblock %}\n"
    ) == 0


def test_count_untagged_recognizes_defensive_i18n_idiom():
    """③-S5：admin 页防御式 ``(i18n or {}).get('k','中文')`` 的回落中文不算 untagged
    （此前扫描器只认裸 ``i18n.get(``，把 base.html/cases.html 的回落串误计为裸 CJK）。"""
    assert _count_untagged_cjk(
        "<b>{{ (i18n or {}).get('cases_stat_total','总 Case') }}</b>\n"
    ) == 0
    # key 也应被正确抽取（纳入中英齐备校验），而非漏抽。
    keys = set(_iter_used_keys("<b>{{ (i18n or {}).get('cases_risk','高风险') }}</b>\n"))
    assert "cases_risk" in keys


def test_workspace_i18n_keys_translated_in_both_langs():
    rep = scan_workspace_i18n()
    assert rep["used_count"] > 0, "未扫到任何 i18n key —— 扫描器或模板可能回退了"
    assert not rep["missing_zh"], f"以下 key 缺 zh 翻译: {rep['missing_zh']}"
    assert not rep["missing_en"], f"以下 key 缺 en 翻译: {rep['missing_en']}"


def test_sealed_templates_no_untagged_regression():
    """已完成 i18n 的工作台模板「防回潮」上限：裸 CJK（无 data-i18n / T() / 服务端 i18n）
    不得超过既定基线。当前四张全部已收敛到 0——任何新写死中文都会让 CI 立刻点名，
    逼迫新功能同步接 i18n（而非悄悄上线半成品翻译）。
    """
    ceilings = {
        "workspace_base.html": 0,
        "workspace_dashboard.html": 0,
        "unified_inbox.html": 0,
        "draft_review.html": 0,
        # ③-S9i：坐席绩效看板收口——静态 (i18n or {}).get + JS window.T/Tf，裸 CJK 归零。
        "agent_perf.html": 0,
        # ③-S9v：工作台经营看板两页（extends workspace_base.html）。
        "workspace_roi.html": 0,
        "workspace_usage.html": 0,
    }
    rep = scan_workspace_i18n()
    per = rep["per_template"]
    offenders = {
        name: per[name]["untagged_cjk"]
        for name, cap in ceilings.items()
        if per.get(name, {}).get("exists") and per[name]["untagged_cjk"] > cap
    }
    assert not offenders, (
        f"以下模板出现新的未接 i18n 裸中文（超过基线）: {offenders}。"
        "请用 data-i18n / window.T() / 服务端 i18n.get() 接上翻译键。"
    )


# ── admin 内容页「裸 CJK = 0」防回潮（③-S5 起逐页并入；与 SEALED_PAGES 渲染门禁互补：
#    此处源码层秒级、定位到行，渲染层证明实际 EN 输出零泄漏）──
_ADMIN_SEALED_PAGES = {"cases.html": 0, "logs.html": 0, "analytics.html": 0, "personas.html": 0,
                       # ③-S9a：RPA 家族共享 partial（被 5 张 RPA 页 include；自身不独立渲染，
                       # 故只入源码 cap-0 + own-script 门禁，渲染零泄漏由各 include 页把关）。
                       "_rpa_shared_styles.html": 0, "_rpa_shared_funnel.html": 0,
                       "_rpa_shared_scripts.html": 0,
                       # ③-S9a-2：RPA 跨平台总览（静态 168 处 → Jinja get + JS 441 键 → window.T/Tf）。
                       "rpa_overview.html": 0,
                       # ③-S9b：Messenger RPA 运营台（静态 468 处 → Jinja get；JS 1102 唯一裸串 →
                       # window.T/Tf，msg_s*/msg_js* 键；CSS ::before content + 日期解析正则 \u53f7 收口）。
                       "messenger_rpa.html": 0,
                       # ③-S9c：WhatsApp RPA 运营台（静态 221 处 → Jinja get + 158 wa_s* 键；JS 227 处 →
                       # window.T + 13 window.Tf，109 wa_js*/wa_js_p* 键；余 118 处按 zh 复用既有键，
                       # 全站复用率 52%——用 scripts/i18n_htmlconv + i18n_jsconv 两把可复用扫描器收口）。
                       "whatsapp_rpa.html": 0,
                       # ③-S9d：LINE RPA 运营台（静态 172 处 → Jinja get + 70 ln_s* 键；JS 160 处 →
                       # window.T + 7 window.Tf，81 ln_js*/ln_js_p* 键；余 142 处按 zh 复用既有键，
                       # 静/动复用率 60%/53%——同两把扫描器收口；title site_name 默认复用 msg_s444）。
                       "line_rpa.html": 0,
                       # ③-S9e：Telegram 原生（mtproto）运营台（静态 161 处 → Jinja get + 130 tg_s* 键；
                       # JS 70 处 → window.T + 8 window.Tf，43 tg_js*/tg_js_p* 键 + 单/双声道 2 键；
                       # 基线复用仅 21%（原生 console 术语独立）；日期解析正则内 CJK(小時分秒)→\u 转义收口）。
                       "telegram.html": 0,
                       # ③-S9f：落地首屏 Dashboard（静态 104 处 → Jinja get + 51 db_s* 键；JS 167 处（5 个
                       # 分散 <script>）→ window.T + 4 window.Tf，102 db_js*/db_js_p* 键；title {% if %}双态
                       # 分支 + 成功判定逻辑 '成功' in msg → \u 转义、status 默认值复用 status_running；
                       # 修得 htmlconv 掩码 sentinel bug（{% else %} 曾被 RUN 跨桥污染键）；'中' 同形词纠为 Medium）。
                       "dashboard.html": 0,
                       # ③-S9g：系统设置 Settings（剩余单页最大 756 → 静态 380 处 → Jinja get + 294 set_s* 键（2 批）；
                       # JS 126 处（4 script）→ window.T + 1 window.Tf，94 set_js*/set_js_p* 键；title {% if %}双态。
                       # 首次启用同形词复用告警（jsconv/htmlconv _senses_map → _*_ambig.json）：'完整'→Full 经复审确认对，
                       # 未标红的复用皆单义、按构造安全）。Telegram AI 人设/人工转接/排班配置全量本地化）。
                       "settings.html": 0,
                       # ③-S9h：知识库管理 Knowledge（静态 163 kb_s* 键 + JS 172 kb_js* + 23 window.Tf 短语；
                       # recon 新增 data-default 中文扫描（X.get(k,'中文')/|default('中文')）→ 迁移前收口 site_name；
                       # 同形词复用告警连抓 3 处误用：'关闭'→Close(非 Disable)/'运行'→Run(非 Active)/'确认'→Confirm(非 Ack)；
                       # 度量词（次/条/分）粘 count 的内联串按语义改短语或加前导空格（HTML 折叠双空格）；
                       # '次'复用 ov_js_times_unit='runs' 语义不符 → 改 kb_js_174=' times'）。
                       "knowledge.html": 0,
                       # ③-S9k：ops 运营家族三页——升级为 Jinja + 服务端 (i18n or {}).get(ops.*)（与 admin
                       # 家族同口径：直出当前语言、无 data-i18n 换字），故裸 CJK 源码 cap-0 亦适用。
                       # merge_reviews(10)/contacts(35)/mobile_handoffs(70) 全量收口；nav 抽 ops/_ops_nav
                       # partial（ops.nav.* 键）；候选/目标行 + 度量词短语（条结果/共 N 条/xx前）用 window.Tf。
                       "ops/merge_reviews.html": 0, "ops/contacts.html": 0, "ops/mobile_handoffs.html": 0,
                       # ③-S9l：AI 工作室（旗舰运营页，extends base；静态 144 处 → Jinja get + 71 as_s* 键；
                       # JS 215 处 → window.T + 33 window.Tf，54 as_js* + 39 as_*(短语/信号) 键。P10 随页收口
                       # backend 标签：signals_human[].label 改 JS 侧按 it.key 派生 window.T('as_sig_'+key)
                       # （API 契约不变）；_scoreToStage/_STAGE_LBL 页内映射早随 jsconv 键化；orphan 全角括号
                       # （（「）静态层转 ASCII 保 EN 排版（非 Han，不入密封但清之保质量）。
                       "ai_studio.html": 0,
                       # ③-S9m：ai_studio 内嵌 iframe 两页（记忆/知识审核）——episodic_memory（静态 39→em_s*
                       # + JS 29→em_js*，记忆校正质量看板/筛选/补全向量/每日确认趋势）；learner（静态 53→lr_s*
                       # + JS 64→lr_js*/lr_*，知识草稿审核/批量通过拒绝/相似去重）。title site_name default 复用
                       # msg_s444；label 动词经 window.T 派生入 Tf；次→kb_js_174(' times') 纠反 runs 复用。
                       "episodic_memory.html": 0, "learner.html": 0,
                       # ③-S9n：关系/转化运营两页——relations_health（静态 40→rh_s* + JS 78→rh_js*，流失预警榜/
                       # 跨域归档回填/单人健康卡）；monetization（静态 114→mo_s* + JS 111→mo_js*，端变现营收/
                       # 挽回榜/漏斗/出图预算/权益开通/门控预览）。title site_name default 复用 msg_s444；
                       # JS 拼接式短语 jsconv 自动分片 window.T；单引号 glue 内全角标点→ASCII 收口英文排印。
                       "relations_health.html": 0, "monetization.html": 0,
                       # ③-S9o：客户 360 详情 + 策略 A/B 分析两页——contact360（静态 42→c3_s* + JS 94→c3_js*，
                       # 客户全景/时间轴/关系演进/合并拆分/跟进任务；阶段演进动词 进阶/降级/回暖/对齐 模板侧
                       # window.T 派生）；strategy_analytics（静态 106→sa_s* + JS 25→sa_js*/sa_*，A/B 追踪/会话
                       # 统计/模型对比/分群/诊断；Jinja set 段位 label + display_model 默认回落均 (i18n).get 包）。
                       # 次→kb_js_174(' times') 纠反 runs；单引号 glue 全角标点/、/：→ASCII 收口英文排印。
                       "contact360.html": 0, "strategy_analytics.html": 0,
                       # ③-S9p：运营总览（standalone HTML + {% include _i18n_bootstrap %}，非 extends base）——
                       # 静态 39→ov2_s*（含 title/h2 单键化，避免 EN 冗余「Ops Overview · Ops Overview」）；
                       # JS 139→ov2_js*（KPI 卡/运维事件表/TTS/翻译引擎/语音语种五大数据驱动块，42 拼接短语
                       # 自动分片 window.T）。次→kb_js_174；确认(incident ack)→rpa_fn_ack；单引号 glue
                       # 全角 ：；「」？，（）→ASCII 收口。
                       "ops_overview.html": 0,
                       # ③-S9q：模板管理域两页——templates（静态 89→tp_s* + JS 18→tp_js*/tp_*，话术模板列表/
                       # 编辑侧栏/变量插入；ui_mode simple/full title 双分支键化；name_map 37 项 intent→展示名经
                       # (i18n).get 收口，同源喂 L136 服务端渲染 + L236 JS tojson；msg-ok 判定改 msg_ok bool
                       # 由 admin 路由下传，去中文子串 '成功' in msg 逻辑耦合并顺修其 vs '已保存' 旧 bug）；
                       # template_mgmt（静态 51→tm_s* + JS 29→tm_js*，模板库 CRUD/搜索/场景筛选/删除确认）。
                       "templates.html": 0, "template_mgmt.html": 0,
                       # ③-S9r：配置导入/导出（静态 68→im_s* + JS 13→im_js*，覆盖/合并模式 + ZIP 拖拽 +
                       # 变更通知 webhook；title/page_title 键化；msg-ok 判定改 msg_ok bool 由 /import 路由下传
                       # msg_ok=bool(restored)/False，去 '成功' in msg 语言耦合逻辑）。
                       "import.html": 0,
                       # ③-S9s：开发者工具（静态 237→dv_s* + JS 67→dv_js*/dv_*，AI 接口/模式切换/语音模型/
                       # 意图路由/管理安全/通知 webhook/Bot 行为/回复逻辑四层触发；title/page_title 键化；
                       # 私聊拒绝话术默认串 dv_s_reject_default 经 (i18n).get 兜底；四层触发标签括号/引号/顿号/
                       # 全角冒号 glue 全 ASCII 化；GXP 命令拼接补空格）。
                       "developer.html": 0,
                       # ③-S9t：工作流/策略两页——workflows（静态 190→wf_s* + JS 77→wf_js*，自定义动作库/
                       # 工作链/分流路由/剧本话题/执行监控；title 键化；工作链 ID「」引用、平台顿号、阶段对齐
                       # 全角冒号 glue 全 ASCII 化，关系阶段句重构去冗余）；strategies（静态 77→st_s* + JS 21→
                       # st_js*/st_*，AI 回复策略参数 + 意图路由映射；ui_mode simple/full title 三分支键化；
                       # 批量启停用 {label} 走 window.Tf 语法安全插值）。
                       "workflows.html": 0, "strategies.html": 0,
                       # ③-S9u：用户/审计两页——users（静态 46→us_s* + JS 33→us_js*，子帐号 CRUD/角色/
                       # 活跃会话踢出；title 键化；角色 display 从 role_<code> 键派生（补 role_agent）；
                       # 删除确认 ？/IP：全角 glue ASCII 化，踢出计数走 window.Tf；msg 成败改 msg_ok 布尔）；
                       # audit（静态 54→au_s* + JS 21→au_js*，操作热力图/快捷筛选预设/时间轴/快照 diff；
                       # title 键化；_presets emoji+中文标签键化 au_preset_*；周几轴/次操作/最高单日键化）。
                       "users.html": 0, "audit.html": 0,
                       # ③-S9v：工作台经营看板两页——workspace_roi（静态 21→wr_s* + JS 42→wr_js*，
                       # 9 处数字插值卡片/关系指标/首响·留资走 wr_tf_*；spark tooltip + 健康 issue 全角冒号 ASCII）；
                       # workspace_usage（静态 27→wu_s* + JS 24→wu_js*，账单 meta/席位/合计/spark 6 处走 wu_tf_*；
                       # 口径定义句 A=B 结构键化；社区模式括号折进键）。
                       "workspace_roi.html": 0, "workspace_usage.html": 0,
                       # ③-S9w：AI 回复质量看板（extends workspace_base）——对外可信质量门面：处置构成
                       # （批准/改写/放行/拦截）+ 自动通过率/改写率/拒绝率/高风险占比 KPI + 通过率趋势 spark +
                       # 知识库改进候选（AI 答错被改写/拒绝→一键沉淀）。静态 26→aq_s*，JS 35→aq_js*，
                       # spark tooltip 数字插值走 aq_tf_spark；「人工纠正过」引导句折为单键去游离「，（AI 初稿）折进键。
                       "ai_quality.html": 0,
                       # ③-S9x：运维实时两页——queue_monitor（extends workspace_base；实时队列看板：待处理/未读/
                       # 超时/等待 KPI + 卡片·表格视图 + 重新分配浮层）：静态 27→qm_s*，JS 30→qm_js*，分配数字插值走
                       # qm_tf_*，时长单位「分」补 qm_js_min（误复用 msg_js_1982='pts' 已纠正），「将…分配给」跨 strong
                       # 拆前后缀键（误复用 wa_s137='Raise' 已纠正）。care_schedule（extends base；主动关怀待办：
                       # 手动加条/立即发/取消 + 待办列表）：静态 32→cs_s*，JS 25→cs_js*，条数插值走 cs_tf_count。
                       "queue_monitor.html": 0, "care_schedule.html": 0,
                       # ③-S9y：危机审计（extends base；安全合规高敏页——危机事件留痕/未处理合计/连击/
                       # 标记处置）：静态 27→ca_s*，JS 19→ca_js*，事件#·用户 插值走 ca_tf_evt_user，
                       # 升级状态徽章补 ca_js006='Escalated'（原复用 draft.pf.escalated='escalated' 与
                       # 'Not escalated' 大小写不一致，安全页统一大写）；两处 placeholder 键化。
                       "crisis_audit.html": 0,
                       # ③-S9z：首次接入引导两页（新用户/海外部署第一触点）——setup（standalone HTML +
                       # {% include _i18n_bootstrap %}，非 extends：首个 master 账户 + AI 接口初始化；静态 20→su_s*
                       # + JS 6→su_js*；title/site_name 双 default 复用 su_s000/msg_s444；账户创建插值走 su_tf_created，
                       # 请求失败前缀 su_js_reqfail 拼 e.message；步骤徽章「完成」复用 tour_done='Done'）；setup_wizard
                       # （extends workspace_base；渠道接入向导：逐渠道填凭证/即时校验/保存生效）：静态 8→sw_s*
                       # + JS 8→sw_js*，缺N项/当前值/已就绪进度三处数字插值走 sw_tf_*，（可选）全角括号折进 sw_js_opt，
                       # config.local.yaml 句跨 <code> 拆前后缀键去游离全角逗号）。
                       "setup.html": 0, "setup_wizard.html": 0,
                       # ③-S10a：安全线补齐——escalation_log（extends workspace_base；升级历史/接管时延：
                       # 升级总数/已接管率/平均接管时延 KPI + 逐条升级记录）：静态 2→el_s*（升级历史·标题/
                       # 「升级历史 / 接管时延」合键，余复用 base.nav.overview/dash.range.*/dash.loading），
                       # JS 9→el_js*（认领原因 map/未接管/收件箱未启用等），接管·等待·原认领三处数字/人名插值
                       # 走 el_tf_*（加载失败复用 rpa_load_failed 复审确认）。与 crisis_audit 组成安全审计家族双语齐备。
                       "escalation_log.html": 0,
                       # ③-S10b：登录页（standalone HTML，非 extends；首触点最高频页）——静态 5→lg_s*
                       # （title 登录 lg_s000 避与「管理控制台」lg_s001 撞号，Token/账密切换/切换深浅色/访问令牌
                       # placeholder；用户名/密码复用 set_js_075/us_s012），既有 login_btn/login_title 双语键沿用；
                       # JS 层零 CJK 无需转换；site_name 双 default 复用 msg_s444；<html lang> 随 ui_lang 动态化。
                       "login.html": 0,
                       # ③-S11：全库扫描扫尾第一批——draft_audit_page（extends workspace_base；草稿审计日志：
                       # 时间/动作/等级/坐席/草稿ID/会话/风险等级/原因表 + 天数/动作过滤）：静态 6→da_s*，
                       # 5 过滤项 code（中文gloss）全标签折进 da_o_*（EN 去冗余 gloss 与 badge 对齐），
                       # 「共 N 条记录」走 da_tf_count，JS badge 动作全过去式对齐（批准/拒绝/取消 覆写为
                       # dash.mp.approved/rejected + inbox.autolog.cancelled 防 Approve/Reject/Cancel 时态漂移）。
                       "draft_audit_page.html": 0,
                       # ③-S11：配置版本对比（extends base；快照时间轴 + A/B 选版 + diff 视图 + 回滚）：静态 16→diff_s*
                       # （版本对比复用既有 diff 键，「旧/新版本（A/B — Before/After）」全角括号折进 diff_s_lblA/B），
                       # JS 7→diff_js*，「{label}：最新两版本/最新 vs 当前」+ 回滚确认弹窗（含 {id} 与 <br>）走 diff_tf_*，
                       # 已回滚前缀 diff_js_rolledback，PREFIX_LABELS/自动手动/未知/网络错误等复用。
                       "diff.html": 0,
                       # ③-S12：接入向导后续两页——kb_cold_start（extends workspace_base；知识库冷启动：
                       # 状态 KPI/场景包播种/播种后建议跨链）：静态 5→ks_s* + 手动 ks_s006/010/011/008/009 跨 <a> 拆句，
                       # JS 7→ks_js*，包计数/播种结果数字插值走 ks_tf_*，返回接入向导与 setup_wizard 共用 ks_s002。
                       "kb_cold_start.html": 0,
                       # ③-S12：上线自检（extends workspace_base；红黄绿灯 + 检查项列表）：静态 4 全复用
                       # （title→base.nav.golive、返回→ks_s002、检测中→kb_js_105、重新检测→db_s050），
                       # JS 6→gl_js*（LIGHT 三态标题/副文案），汇总 KPI 走 gl_tf_sum，默认动作按钮 gl_js_action 避 dash.lb.handled。
                       "golive_checklist.html": 0,
                       # ③-S13：工作台联系人两页——tasks（extends workspace_base；跟进待办：
                       # 范围/到期筛选 + 卡片 snooze/改派/完成）：静态 7→tk_s*，JS 4→tk_js*（逾期/无期限/空态等），
                       # 计数条/延期按钮走 tk_tf_*（+1天/+3天/+1周 整键避 wa_s158 days 拼接），
                       # 联系人子系统未启用与 contacts_list 共用 tk_js_004。
                       "tasks.html": 0,
                       # ③-S13：客户列表（extends workspace_base；搜索/筛选/分页/导出 CSV）：
                       # 静态 8→cl_s* + 过滤项 cl_o_due 全标签折进键（待跟进(到期)），
                       # JS 2→cl_js* + 复用 c3_js002/rpa_js_intimacy/tk_js_004 等，统计/分页走 cl_tf_*。
                       "contacts_list.html": 0,
                       # ③-S14：TTS 预览文件仪表盘（standalone HTML + {% include _i18n_bootstrap %}；
                       # 非 extends，Admin 运维页）：静态 9→atd_s* + 手动 atd_s000/h1/back/disk/刷新/清理按钮/前缀图例，
                       # JS 3→atd_js*，清理结果数字插值走 atd_tf_cleaned，<html lang> 随 ui_lang 动态化。
                       "admin_tts_dashboard.html": 0,
                       # ③-S15：帮助 & 指令参考（extends base；help_commands 注册表 stable key +
                       # tier 枚举驱动图标/pro-only，分区/指令/示例/权限/搜索/培训横幅全 hp_* 键化；
                       # 废弃 ``commands`` 中文 dict + ``'管理' in section`` 逻辑耦合）。
                       "help.html": 0,
                       # P33b：实时语音试拨页（standalone + i18n bootstrap；静态 rvc_* + JS rvc_js_*/rvc_tf_* 全键化）。
                       "voice_call.html": 0,
                       # P34：品牌 lockup partial（login/setup/sidebar/workspace 四变体；alt/默认回落接 i18n）。
                       "_brand_lockup.html": 0}


def test_crmw_duration_unit_keys_defined():
    """CRMW 时长单位走 window.Tf(crmw.unit.*) —— 须中英齐备，否则 EN 工作台仍显示「5分」。"""
    from src.web.web_i18n import get_translations
    zh, en = get_translations("zh"), get_translations("en")
    keys = ("crmw.unit.sec", "crmw.unit.min", "crmw.unit.min_dec",
            "crmw.unit.hour", "crmw.unit.hour_dec", "crmw.unit.day")
    bad = [k for k in keys if k not in zh or k not in en]
    assert not bad, f"CRMW 时长单位 key 缺翻译: {bad}"


def test_voice_call_static_html_no_untagged_cjk():
    """P33/P33b：voice_call.html 静态 HTML（剥离 script/style）裸 CJK 须为 0。"""
    import re as _re
    from pathlib import Path

    text = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "voice_call.html"
    text = text.read_text(encoding="utf-8")
    static = _re.sub(r"<script[\s\S]*?</script>", "", text, flags=_re.I)
    static = _re.sub(r"<style[\s\S]*?</style>", "", static, flags=_re.I)
    n = _count_untagged_cjk(static)
    assert n == 0, f"voice_call.html 静态层仍有 {n} 处未接 i18n 的裸 CJK"


def test_shared_partials_no_untagged_cjk():
    """P34：共享 partial（_*.html）裸 CJK 须为 0；由各 include 页 EN 渲染门禁二次把关。"""
    from pathlib import Path

    tdir = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
    offenders = {}
    for p in sorted(tdir.rglob("_*.html")):
        n = _count_untagged_cjk(p.read_text(encoding="utf-8"))
        if n:
            offenders[p.relative_to(tdir).as_posix()] = n
    assert not offenders, f"共享 partial 仍有裸 CJK: {offenders}"


def test_no_hardcoded_brand_product_default():
    """P35 防回潮：模板里 ``product_name|default(...)`` 的回落值须走 i18n（``(i18n or {}).get('brand.product',…)``），
    不得直写裸中文 ``'智聊'``——否则 EN 用户在缺 product_name 覆盖时会看到中文品牌名。"""
    import re as _re
    from pathlib import Path

    tdir = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
    # 命中 product_name|default('…') 里 default 参数为纯中文字面量（非 i18n.get 表达式）的写法
    bad_pat = _re.compile(r"product_name\s*\|\s*default\(\s*['\"][^'\"]*[\u4e00-\u9fff]")
    offenders = []
    for p in sorted(tdir.rglob("*.html")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if bad_pat.search(line):
                offenders.append(f"{p.relative_to(tdir).as_posix()}:{i}")
    assert not offenders, (
        "以下位置 product_name|default() 直写裸中文品牌名，应改走 "
        "(i18n or {}).get('brand.product', '智聊'): " + ", ".join(offenders)
    )


@pytest.mark.parametrize("name,cap", sorted(_ADMIN_SEALED_PAGES.items()))
def test_admin_content_pages_no_untagged_regression(name, cap):
    """已 i18n 收口的 admin 内容页（extends base.html）裸 CJK 不得超基线（逐页一个用例，失败即点名）。
    base.html 外壳含双语 TERM_DICT(zh 半合法)，不入此源码计数门禁，改由 SEALED_PAGES 渲染门禁把关。"""
    from scripts.i18n_scan import _count_untagged_cjk, _TPL_DIR

    n = _count_untagged_cjk((_TPL_DIR / name).read_text(encoding="utf-8"))
    assert n <= cap, (
        f"{name} 出现新的未接 i18n 裸中文（{n} 处 > 基线 {cap}）。"
        "请用 (i18n or {}).get() / window.T() 接上翻译键。"
    )


# ── ③-S8b：JS 层「自有 <script> 内零裸 CJK」严门禁，补两道既有门禁的共同盲区：
#    ① _count_untagged_cjk 会整行跳过「同时含 T() 调用的行」→ 与 T() 同行的中文字面量漏检；
#    ② SEALED_PAGES 渲染门禁整块剥离 <script> → 脚本内中文永不入可见 HTML，也漏检。
#    收录页须按 personas 的「无 fallback、全键化」约定迁移（window.T('k') 而非 T('k','中文默认')）——
#    键由 key-def 门禁保证存在，故无需中文兜底。cases/logs/analytics 走 T(key,'中文默认') 防御式
#    默认值写法（属另一种可接受约定），不在本门禁；如日后亦改全键化可并入。
_SCRIPT_CJK_ZERO_PAGES = ("personas.html", "_rpa_shared_funnel.html", "_rpa_shared_scripts.html",
                          "rpa_overview.html", "messenger_rpa.html", "whatsapp_rpa.html",
                          "line_rpa.html", "telegram.html", "dashboard.html", "settings.html",
                          "knowledge.html",
                          # ③-S9i：坐席绩效看板（工作台家族首页）JS 层零裸 CJK（ap_js*/ap_js_p* 键）。
                          "agent_perf.html",
                          # ③-S9j：工作台家族其余三页 JS 层零裸 CJK。这三页早已高度 i18n（源码 cap-0），
                          # 本轮只清 workspace_dashboard 两处「JS 拼串里 data-i18n span 的中文兜底」
                          # （改直接 T('dash.loading') 拼入，免动态节点还要再跑一次 wsApplyI18n）。
                          "unified_inbox.html", "workspace_dashboard.html", "draft_review.html",
                          # ③-S9k：ops 运营家族三页（原 raw-served standalone，改走 Jinja + {% include
                          # _i18n_bootstrap %}；静态 (i18n or {}).get(ops.*)、JS window.T/Tf）。
                          "ops/merge_reviews.html", "ops/contacts.html", "ops/mobile_handoffs.html",
                          # ③-S9l：AI 工作室单 <script>（1053 行）全键化，window.T/Tf 无中文兜底。
                          "ai_studio.html",
                          # ③-S9m：ai_studio 内嵌 iframe 两页 JS 层零裸 CJK（em_js*/lr_js* 全键化）。
                          "episodic_memory.html", "learner.html",
                          # ③-S9n：关系/转化运营两页 JS 层零裸 CJK（rh_js*/mo_js* 全键化）。
                          "relations_health.html", "monetization.html",
                          # ③-S9o：客户 360 + 策略分析两页 JS 层零裸 CJK（c3_js*/sa_js* 全键化）。
                          "contact360.html", "strategy_analytics.html",
                          # ③-S9p：运营总览 JS 层零裸 CJK（ov2_js* 全键化）。
                          "ops_overview.html",
                          # ③-S9q：模板管理域两页 JS 层零裸 CJK（tp_js*/tm_js* 全键化）。
                          "templates.html", "template_mgmt.html",
                          # ③-S9r：配置导入页 JS 层零裸 CJK（im_js* 全键化）。
                          "import.html",
                          # ③-S9s：开发者工具 JS 层零裸 CJK（dv_js* 全键化）。
                          "developer.html",
                          # ③-S9t：工作流/策略两页 JS 层零裸 CJK（wf_js*/st_js* 全键化）。
                          "workflows.html", "strategies.html",
                          # ③-S9u：用户/审计两页 JS 层零裸 CJK（us_js*/au_js* 全键化）。
                          "users.html", "audit.html",
                          # ③-S9v：工作台经营看板两页 JS 层零裸 CJK（wr_js*/wr_tf_* / wu_js*/wu_tf_* 全键化）。
                          "workspace_roi.html", "workspace_usage.html",
                          # ③-S9w：AI 回复质量看板 JS 层零裸 CJK（aq_js*/aq_tf_* 全键化）。
                          "ai_quality.html",
                          # ③-S9x：运维实时两页 JS 层零裸 CJK（qm_js*/qm_tf_* / cs_js*/cs_tf_* 全键化）。
                          "queue_monitor.html", "care_schedule.html",
                          # ③-S9y：危机审计 JS 层零裸 CJK（ca_js*/ca_tf_* 全键化）。
                          "crisis_audit.html",
                          # ③-S9z：首次接入引导两页 JS 层零裸 CJK（su_js*/su_tf_* / sw_js*/sw_tf_* 全键化；
                          # 请求失败/账户创建/缺项/当前值/进度插值均走 window.T/Tf 无中文兜底）。
                          "setup.html", "setup_wizard.html",
                          # ③-S10a/b：升级历史 JS 层零裸 CJK（el_js*/el_tf_* 全键化）；登录页 JS 层本就零 CJK。
                          "escalation_log.html", "login.html",
                          # ③-S11：草稿审计 + 版本对比 JS 层零裸 CJK（da_js*/da_tf_*、diff_js*/diff_tf_* 全键化）。
                          "draft_audit_page.html", "diff.html",
                          # ③-S12：知识库冷启动 + 上线自检 JS 层零裸 CJK（ks_js*/ks_tf_*、gl_js*/gl_tf_* 全键化）。
                          "kb_cold_start.html", "golive_checklist.html",
                          # ③-S13：跟进待办 + 客户列表 JS 层零裸 CJK（tk_js*/tk_tf_*、cl_js*/cl_tf_* 全键化）。
                          "tasks.html", "contacts_list.html",
                          # ③-S14：TTS 预览文件仪表盘 JS 层零裸 CJK（atd_js*/atd_tf_* 全键化）。
                          "admin_tts_dashboard.html",
                          # P33b：实时语音试拨页 JS 层零裸 CJK（rvc_js_*/rvc_tf_*；_toneLabel 中文正则用 \\u 转义）。
                          "voice_call.html",
                          # ③-S15：帮助页 JS 层零裸 CJK（hp_js_copied 复制 toast）。
                          "help.html")


@pytest.mark.parametrize("name", _SCRIPT_CJK_ZERO_PAGES)
def test_sealed_pages_no_cjk_in_own_scripts(name):
    """收录页自有 <script> 体内（剥注释后）不得残留任何 CJK 字面量。
    专治「与 window.T() 同行的中文串」——这类行被 _count_untagged_cjk 跳过、又被渲染门禁随
    <script> 整块剥离，二者皆盲。本门禁源码层逐 <script> 扫描，失败即点名行内上下文。"""
    import re as _re
    from scripts.i18n_scan import _strip_comments, _TPL_DIR

    src = (_TPL_DIR / name).read_text(encoding="utf-8")
    leaks = []
    for body in _re.findall(r"<script\b[^>]*>(.*?)</script>", src, flags=_re.S | _re.I):
        stripped = _strip_comments(body)
        for m in _re.finditer(r"[\u4e00-\u9fff]+", stripped):
            ctx = stripped[max(0, m.start() - 40):m.end() + 15]
            leaks.append(ctx.replace("\n", " ").strip())
    assert not leaks, (
        f"{name} 自有 <script> 残留 {len(leaks)} 处裸 CJK（应改用 window.T/Tf 键，禁中文 fallback）:\n"
        + "\n".join(f"  - ...{c}..." for c in leaks[:15])
    )


def test_sealed_templates_no_legacy_date_formatters():
    """③-R/③-S2 防回潮：外壳页 + 共享 RPA 脚本不得再用 toTimeString().slice /
    toLocale*('zh-CN') / 内联 ``new Date(...).toLocale*()``（绕过 ui_lang 的浏览器 locale）等旧写法。

    ③-S2 起靶面由 4 张密封页扩到「所有继承外壳的页」(shelled_templates 自动发现) + _rpa_shared_scripts，
    全部统一走 window.wsFmt*。内联正则不会误命中数值 ``(v.tokens).toLocaleString()``（无 new Date）。
    """
    import re as _re
    from scripts.i18n_scan import shelled_templates, _TPL_DIR

    targets = sorted(set(shelled_templates()) | {"_rpa_shared_scripts.html"})
    banned_sub = (
        "toTimeString().slice",
        "toLocaleString('zh-CN'", 'toLocaleString("zh-CN"',
        "toLocaleDateString('zh-CN'", 'toLocaleDateString("zh-CN"',
        "toLocaleTimeString('zh-CN'", 'toLocaleTimeString("zh-CN"',
    )
    re_inline = _re.compile(r"new Date\([^\n]*?\)\.toLocale")
    offenders = []
    for name in targets:
        text = (_TPL_DIR / name).read_text(encoding="utf-8")
        for pat in banned_sub:
            if pat in text:
                offenders.append(f"{name}: contains {pat!r}")
        if re_inline.search(text):
            offenders.append(f"{name}: 内联 new Date().toLocale* → 改用 window.wsFmt*")
    assert not offenders, "legacy date formatters:\n" + "\n".join(offenders)


def test_en_values_no_cjk_structural_punctuation():
    """③-S9j：全 EN 译表不得含「CJK 结构标点」——全角括号/冒号/逗号/分号/问号/叹号 + 顿号/句号/
    表意空格/全角句点（（）：；，？！、。）。这类标点是「机器粘接 / 从中文照抄未改写」EN 串的铁证
    （英文该用 ASCII 标点），过 CJK 汉字门禁却读着别扭；本轮 agent_perf 的 13 处串接短语正是靠把
    这类中间全角标点的拼串改成 window.Tf 完整句才消除的。

    **刻意豁免**两类合法字符，避免误伤：
    - 弯引号 U+2018–201D（'' ""）：正宗英文排版（Don't、“quotes”），非缺陷；
    - 全角加号 ＋ U+FF0B：中英两侧同用、镜像界面上的 ＋ 按钮图标（如「＋ Add via QR」），设计一致
      而非翻译问题。
    实测当前全表 0 命中 → 常驻零门禁：日后任何新键漏进 CJK 结构标点，CI 立刻点名。
    """
    import re as _re
    from src.web.web_i18n import get_translations

    en = get_translations("en")
    bad_punct = _re.compile(r"[\uff08\uff09\uff0c\uff1a\uff1b\uff1f\uff01\u3001\u3002\u3000\uff0e]")
    offenders = {
        k: v for k, v in en.items()
        if isinstance(v, str) and bad_punct.search(v)
    }
    assert not offenders, (
        "以下 EN 译文含 CJK 结构标点（应改用 ASCII 标点或重写为完整英文句，疑似机器粘接/照抄中文）:\n"
        + "\n".join(f"  {k} = {v!r}" for k, v in list(offenders.items())[:20])
    )


def test_foundation_keys_present():
    """地基首批关键 key 不能丢（tab / 核心卡 / 区块标题 / 语言切换 / 看板标题）。"""
    from src.web.web_i18n import get_translations

    zh, en = get_translations("zh"), get_translations("en")
    for k in [
        "dash.tab.today",
        "dash.card.waiting",
        "dash.sec.esc",
        "lang_toggle",
        "dash.title",
    ]:
        assert k in zh, f"zh 丢了 key: {k}"
        assert k in en, f"en 丢了 key: {k}"


def test_translation_dict_zh_en_key_parity():
    """③-S12：全库 zh/en 键集合必须完全一致——防只写一侧（如 ov2_localtts_* 仅 zh）漏配 EN。"""
    from src.web.web_i18n import get_translations

    zh, en = get_translations("zh"), get_translations("en")
    zh_only = sorted(set(zh) - set(en))
    en_only = sorted(set(en) - set(zh))
    assert not zh_only, f"以下 key 仅 zh 有、缺 en: {zh_only[:30]}"
    assert not en_only, f"以下 key 仅 en 有、缺 zh: {en_only[:30]}"


def _duplicate_i18n_keys_by_lang():
    """AST 扫 web_i18n.py 源码，返回 {lang: {key: 次数}}（只列出现>1 的 key）。

    运行期 dict 会静默去重（后值覆盖前值，静默改错文案——Phase 2 的 set_s032 撞号即此），
    唯有解析源码字面量才能抓出同一语言块内的重复键。
    """
    import ast
    from collections import Counter
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "web" / "web_i18n.py"
    tree = ast.parse(src.read_text(encoding="utf-8"), filename=str(src))

    trans_dict = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_TRANSLATIONS" for t in node.targets
        ):
            if isinstance(node.value, ast.Dict):
                trans_dict = node.value
            break
    assert trans_dict is not None, "未能在 web_i18n.py 定位 _TRANSLATIONS 字面量"

    out = {}
    for lang_key, lang_val in zip(trans_dict.keys, trans_dict.values):
        if not (isinstance(lang_key, ast.Constant) and isinstance(lang_key.value, str)):
            continue
        if not isinstance(lang_val, ast.Dict):
            continue
        keys = [
            k.value for k in lang_val.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        ]
        dups = {k: c for k, c in Counter(keys).items() if c > 1}
        if dups:
            out[lang_key.value] = dups
    return out


def test_translation_dict_no_duplicate_keys_within_lang():
    """③-S13：同一语言块内不得有重复 i18n key。

    Python dict 字面量遇重复键会静默取最后一个——历史上 set_s032 在 zh 块被定义两次
    （品牌区 vs 演示数据区），导致品牌标签渲染成演示区文案且**测试全绿无感**。
    源码级去重扫描是唯一能提前抓住此类「静默覆盖」的门禁。"""
    dups = _duplicate_i18n_keys_by_lang()
    assert not dups, (
        "web_i18n.py 存在同语言块内重复 key（后值静默覆盖前值，会改错文案）：\n"
        + "\n".join(f"  [{lang}] {d}" for lang, d in dups.items())
    )


# ③-S3：base.html 共享侧栏导航原硬编码 span 收口的 key（中英都要在，且大多需真翻译非照抄）。
_ADMIN_CHROME_NAV_KEYS = [
    "personas", "workspace_inbox", "rpa_overview", "telegram_settings",
    "line_rpa", "messenger_rpa", "whatsapp_rpa", "episodic", "crisis_audit",
    "care", "relations_health", "monetization", "ai_studio",
    "viewer_badge", "viewer_badge_title",
    # 命令面板（Ctrl+K）专属 JS 文案
    "cmd_reload", "switch_theme", "cmd_no_match",
]


# ── i18n key 定义门禁覆盖的模板（base.html=外壳；其余=已逐页密封的内容页，③-S5 起追加）──
_KEY_SCAN_TEMPLATES = ("base.html", "cases.html", "logs.html", "analytics.html", "personas.html",
                       # ③-S9a：RPA 共享 partial 的 rpa_* 键也须中英齐备（源码扫描，不依赖渲染）。
                       "_rpa_shared_styles.html", "_rpa_shared_funnel.html", "_rpa_shared_scripts.html",
                       # ③-S9a-2：RPA 总览 ov_*/ov_js_* 键（441 个）中英齐备。
                       "rpa_overview.html")


@pytest.mark.parametrize("name", _KEY_SCAN_TEMPLATES)
def test_base_chrome_i18n_keys_all_defined(name):
    """每个 (i18n or {}).get('KEY', …) / window.T('KEY') 引用的 KEY 都须中英齐备——
    否则 en 会静默回落到中文 fallback（③-S3 实测踩坑：sidebar_toggle 未定义，en 也显示中文）。
    结构性门禁：base.html 覆盖全部外壳页；cases.html 起逐页并入内容页（③-S5），各页独立用例。"""
    import re as _re
    from pathlib import Path
    from src.web.web_i18n import get_translations

    tdir = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
    zh, en = get_translations("zh"), get_translations("en")
    text = (tdir / name).read_text(encoding="utf-8")
    # 同时匹配 `(i18n or {}).get('k'` 与 `i18n.get('k'`；dp.get(...) 等非 i18n 取值不会命中。
    keys = set(_re.findall(
        r"i18n(?:\s+or\s+\{\}\))?\.get\(\s*['\"]([A-Za-z0-9_.]+)['\"]", text
    ))
    # 客户端 window.T('k') / window.Tf('k')（仅取第一个串=key，回落文案不入网）也须齐备。
    keys |= set(_re.findall(r"window\.Tf?\(\s*['\"]([A-Za-z0-9_.]+)['\"]", text))
    assert keys, f"未能从 {name} 提取到任何 i18n key（正则失效？）"
    bad = sorted(k for k in keys if k not in zh or k not in en)
    assert not bad, f"{name} 引用了未定义的 i18n key（en 将回落中文）: {bad}"


def test_base_tour_and_help_bilingual():
    """③-S4：引导(tour) + 全局 tooltip 引擎 + nav_* 帮助词条已双语。
    这些是 JS 内联数据（中英并存、显示按 WS_LANG 客户端切换、缺英文回落中文），
    故此处做「数据层完整性 + 引擎确实按语言取 en 字段」的断言。"""
    import re as _re
    from pathlib import Path

    base = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "base.html"
    text = base.read_text(encoding="utf-8")
    # tooltip / tour 引擎须按语言取 en 字段（缺则回落中文）
    for needle in ("d.en", "d.desc_en", "d.usage_en", "_TOUR_EN"):
        assert needle in text, f"tooltip/tour 引擎未接语言开关: {needle}"
    # 引导英文文案在位（证明 tour 已双语）
    for needle in ("Command Palette (Ctrl+K)", "This is your control center"):
        assert needle in text, f"tour 英文缺失: {needle!r}"
    # ③-S4b：全部 TERM_DICT 词条（不止 nav_*）须含 en + desc_en（术语表整表收口）。
    # 词条形如 "key":{"zh":...}，内部无嵌套花括号，故 [^{}]* 可精确截取条目体。
    entries = _re.findall(r'"([A-Za-z0-9_]+)":\{"zh":([^{}]*)\}', text)
    assert len(entries) >= 80, f"TERM_DICT 词条数异常（仅 {len(entries)}，正则可能失效）"
    bad = [k for k, body in entries if '"en":' not in body or '"desc_en":' not in body]
    assert not bad, f"TERM_DICT 词条缺英文字段（en/desc_en）: {bad}"


def test_base_static_html_no_untagged_cjk():
    """P32：base.html 静态 HTML（剥离 script/style）裸 CJK 须为 0。
    TERM_DICT / tour / command palette 等双语 inline 数据在 <script> 内，不入 _ADMIN_SEALED_PAGES
    cap=0 源码门禁，改由本用例 + SEALED_PAGES 英文渲染门禁双锁。"""
    import re as _re
    from pathlib import Path

    text = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "base.html"
    text = text.read_text(encoding="utf-8")
    static = _re.sub(r"<script[\s\S]*?</script>", "", text, flags=_re.I)
    static = _re.sub(r"<style[\s\S]*?</style>", "", static, flags=_re.I)
    n = _count_untagged_cjk(static)
    assert n == 0, f"base.html 静态层仍有 {n} 处未接 i18n 的裸 CJK"


# ── 页面级「英文渲染零中文泄漏」密封名单（③-S4b base.html → ③-S5 起逐页追加内容页）──
# 每项 = (模板, 额外 ctx)。剥离 <script>/<style> 后，EN 渲染的可见 HTML 不得残留中文。
# 品牌名传英文以排除「品牌不翻译」噪声；JS 内的中文回落串在 <script> 内被剥离，
# 其运行时取 en 由 test_base_chrome_i18n_keys_all_defined（key 中英齐备）保证。
_SEAL_BASE_CTX = dict(
    ui_lang="en", site_name="Boundless · ChatX", product_name="ChatX", company_name="Boundless",
    username="admin", display_name="Admin",
    user_role="master", domain_web_pages=[], active="cases", embedded=False,
)
# settings.html 的路由级上下文（config 段 + 派生 JSON 串），此处以「空配置」镜像 /settings 契约，
# 使 _render_en 直渲不缺变量；空 dict 下模板自带默认走空串 → 若有中文数据默认仍会被泄漏检出。
_SETTINGS_SEAL_CTX = dict(
    ai={}, wb={}, tg={}, notif={}, he={},
    he_agents_json="[]", he_work_hours_json="{}",
    he_work_exceptions_json="{}", he_agent_teams_json="[]",
)
# knowledge.html 的路由级上下文（/knowledge 传 categories + stats）；空列表/空 dict 镜像契约，
# 使 {% for cat in categories %} 不迭代 undefined、stats.* 走宽松 Undefined 空串。
_KNOWLEDGE_SEAL_CTX = dict(categories=[], stats={})
from src.web.help_commands import get_help_sections as _get_help_sections

_HELP_SEAL_CTX = dict(ui_mode="full", help_sections=_get_help_sections())
SEALED_PAGES = (
    ("base.html", {"ui_mode": "full"}),
    ("base.html", {"ui_mode": "simple"}),  # 简洁模式 onboard-modal（③-S5 补漏）
    ("cases.html", {"ui_mode": "simple"}),  # simple 模式才渲染「操作说明」引导卡
    ("cases.html", {"ui_mode": "full"}),
    ("logs.html", {"ui_mode": "simple"}),  # simple 模式渲染「高级功能」引导条（③-S6）
    ("logs.html", {"ui_mode": "full"}),
    # analytics.html（③-S7）：正文无服务端 ui_mode 分支，一态即可；给空数据 ctx
    # 以触发全部「暂无…」空态串 + 刷新栏/时段/统计卡/运营 Copilot 的英文渲染。
    ("analytics.html", {"ui_mode": "full", "hours": 24, "total": 0,
                        "hourly": [], "resp_dist": None,
                        "cmd_stats": [], "top_users": []}),
    # personas.html（③-S8a 静态层 + ③-S8b JS 层）：topbar/tabs/dashboard/profiles/bindings/
    # rules/抽屉/默认/picker/行内标签编辑器（静态）+ 全部动态生成 HTML/toast/confirm/prompt（JS）
    # 已全量接 i18n（window.T / window.Tf，psn_* / psn_js_* 键）。正文无 ui_mode 分支，一态即可。
    # ③-S8b 完工后已并入 _ADMIN_SEALED_PAGES（源码 cap 0），与本渲染门禁双锁防回潮。
    ("personas.html", {"ui_mode": "full"}),
    # rpa_overview.html（③-S9a-2）：标题/检索/KPI/意图字典/设备管理/统计/SSE 全量接 i18n
    # （静态 Jinja get + JS window.T/Tf，ov_*/ov_js_* 键）。正文无 ui_mode 分支，一态即可。
    ("rpa_overview.html", {"ui_mode": "full"}),
    # messenger_rpa.html（③-S9b）：Hero/KPI/六大 tab（总览/线索/人设/账号/审批/数据）/策略配置/
    # 应急停发/设备抽屉/人设编辑 modal 全量接 i18n（静态 Jinja get + JS window.T/Tf，msg_s*/msg_js* 键）。
    # 正文无 ui_mode 分支，一态即可。
    ("messenger_rpa.html", {"ui_mode": "full"}),
    # whatsapp_rpa.html（③-S9c）：Hero/KPI/五大 tab（对话/待审/模板分析/配置/运维）/策略轮换/语音·媒体/
    # 表情控制/设备抽屉/语言锁定 全量接 i18n（静态 Jinja get + JS window.T/Tf，wa_s*/wa_js* 键）。
    # 正文无 ui_mode 分支，一态即可。
    ("whatsapp_rpa.html", {"ui_mode": "full"}),
    # line_rpa.html（③-S9d）：Hero/KPI/会话流/通知栏对账/时间轴/审计/审批/节奏与服务配置/语言锁定
    # 全量接 i18n（静态 Jinja get + JS window.T/Tf，ln_s*/ln_js* 键）。正文无 ui_mode 分支，一态即可。
    ("line_rpa.html", {"ui_mode": "full"}),
    # telegram.html（③-S9e）：Telegram 原生运营台——场景预设/消息处理/语音识别(ASR)/语音回复(TTS)/
    # 声音克隆/Edge TTS 声线/账号状态/配置摘要/版本快照 全量接 i18n（静态 Jinja get + JS window.T/Tf，
    # tg_s*/tg_js* 键）。正文无 ui_mode 分支，一态即可。
    ("telegram.html", {"ui_mode": "full"}),
    # dashboard.html（③-S9f）：落地首屏——KPI 卡/运维可靠性/系统告警/快捷入口/实时性能/待审话术/
    # 主动消息质量/触发器决策/知识库状态/回复质量 全量接 i18n（静态 Jinja get + JS window.T/Tf，
    # db_s*/db_js* 键）。title/page_title 有 ui_mode 双态分支 → 两态都渲染验证。
    ("dashboard.html", {"ui_mode": "full"}),
    ("dashboard.html", {"ui_mode": "simple"}),
    # settings.html（③-S9g）：系统设置——品牌白标/演示数据/Telegram AI 人设/人工转接/值班排班/
    # 周模板/例外日/展示与分隔 全量接 i18n（静态 Jinja get + JS window.T/Tf，set_s*/set_js* 键）。
    # title/page_title 有 ui_mode 双态分支 → 两态都渲染验证。
    ("settings.html", dict(ui_mode="full", **_SETTINGS_SEAL_CTX)),
    ("settings.html", dict(ui_mode="simple", **_SETTINGS_SEAL_CTX)),
    # knowledge.html（③-S9h）：知识库管理——KB 条目/错误码字典/对话示例/沙盒测试/翻译审核/效果反馈/
    # 向量化/维护诊断/编辑抽屉 全量接 i18n（静态 kb_s* + JS window.T/Tf kb_js*/kb_js_p* 键）。
    # simple 模式渲染顶部「操作说明」引导卡 → 两态都渲染验证。
    ("knowledge.html", dict(ui_mode="full", **_KNOWLEDGE_SEAL_CTX)),
    ("knowledge.html", dict(ui_mode="simple", **_KNOWLEDGE_SEAL_CTX)),
    # ── 工作台家族（extends workspace_base.html）：客户端 data-i18n 换字 + window.T，切语言经
    #    /set_lang 整页重载后服务端重渲。本渲染门禁经 _strip_client_swapped 把「加载时必被换成
    #    英文」的 data-i18n 中文剥掉后，可见 HTML 仍须零中文（未挂 data-i18n 的裸串照样点名）。──
    ("workspace_base.html", {"ui_mode": "full"}),  # 工作台外壳（对 workspace 家族 ≈ base.html）
    # agent_perf.html（③-S9i）：坐席绩效看板——KPI/每日趋势/坐席明细/首响绩效/质检评分/副驾采纳/
    # 翻译引擎健康/多模态后端/术语库/批量触达预览/流失预警/热力时段 全量接 i18n。静态层走服务端
    # (i18n or {}).get(ap_s*)（直出当前语言、无换字闪烁、免 JS 亦可读），JS 层走 window.T/Tf
    # (ap_js*/ap_js_p*)；13 处 '..'+v+'..' 串接短语（度量词/语序/中间全角标点）改 window.Tf。
    # 正文无 ui_mode 分支，一态即可。
    ("agent_perf.html", dict(ui_mode="full", user_name="admin", user_display_name="Admin")),
    # ③-S9j：工作台家族其余三页（客户端 data-i18n 换字 + window.T）。早已高度 i18n，本轮经
    # _strip_client_swapped（data-i18n 感知）纳入渲染门禁后收口：unified_inbox 补了 2 处漏网的
    # title 提示（copilot App 切换 + iframe 面板 title，属性带 CJK 却无 data-i18n-title → EN 渲染
    # 泄漏，源码 cap-0 看不见），workspace_dashboard 清 2 处 JS 拼串中文兜底。三页均无 ui_mode 分支。
    ("unified_inbox.html", dict(ui_mode="full", user_name="admin", user_display_name="Admin",
                                funnel_done_stages=[])),
    ("workspace_dashboard.html", dict(ui_mode="full", user_name="admin", user_display_name="Admin",
                                      funnel_done_stages=[])),
    ("draft_review.html", dict(ui_mode="full", user_name="admin", user_display_name="Admin")),
    # ③-S9k：ops 运营家族三页——原 contacts_routes._load_ops_html 直出原始 HTML（不过 Jinja），本轮
    # 升级为 templates.TemplateResponse 渲染（i18n_render 注入 i18n/ui_lang）+ {% include
    # _i18n_bootstrap %}（拿 window.T/Tf + wsFmt*，退役 /static/ops_locale.js）+ {% include
    # ops/_ops_nav %}（导航 i18n + active 高亮）。静态层走服务端 (i18n or {}).get(ops.*)（直出当前
    # 语言、无换字闪烁），JS 层走 window.T/Tf（度量词/语序短语用 Tf 占位符）。无 ui_mode 分支，一态即可。
    ("ops/merge_reviews.html", dict(ui_mode="full", active="/ops/merge-reviews")),
    ("ops/contacts.html", dict(ui_mode="full", active="/ops/contacts")),
    ("ops/mobile_handoffs.html", dict(ui_mode="full", active="/ops/mobile-handoffs")),
    # ③-S9l：AI 工作室（extends base.html，旗舰运营页）——人设/记忆/知识审核/关系(阶段分布·亲密度
    # 趋势·平台×人设效果·跨平台合并候选·沉默唤醒·prompt A/B)/身份绑定 全量接 i18n（静态 as_s* +
    # JS window.T/Tf as_js*/as_* 键）。数据经 JS fetch，正文无服务端 ui_mode 分支，一态即可。
    ("ai_studio.html", {"ui_mode": "full"}),
    # ③-S9m：ai_studio 内嵌的 iframe 两页（extends base.html）——episodic_memory（记忆校正质量/长期
    # 记忆检索/补全向量/每日确认趋势）+ learner（知识草稿审核/批量通过·拒绝/相似去重）。数据经 JS
    # fetch，正文无服务端 ui_mode 分支，一态即可。
    ("episodic_memory.html", {"ui_mode": "full"}),
    ("learner.html", {"ui_mode": "full"}),
    ("relations_health.html", {"ui_mode": "full"}),
    ("monetization.html", {"ui_mode": "full"}),
    ("contact360.html", {"ui_mode": "full", "contact_id": "demo"}),
    ("strategy_analytics.html", {"ui_mode": "full"}),
    ("ops_overview.html", {"ui_mode": "full"}),
    ("templates.html", {"ui_mode": "simple", "templates": {}}),  # simple 只读引导卡 + 双分支 title；templates 供 tojson
    ("templates.html", {"ui_mode": "full", "templates": {}}),
    ("template_mgmt.html", {"ui_mode": "full"}),
    ("import.html", {"ui_mode": "full"}),
    ("developer.html", {"ui_mode": "full"}),
    ("workflows.html", {"ui_mode": "full"}),
    ("strategies.html", {"ui_mode": "simple"}),
    ("strategies.html", {"ui_mode": "full"}),
    ("users.html", {"ui_mode": "full"}),
    ("audit.html", {"ui_mode": "full"}),
    # ③-S9v：工作台家族两张经营看板（extends workspace_base.html）——workspace_roi（经营 ROI：
    # 关系黏性/D7 留存/AI 自动应答占比/节省人力·金额/引流·首响·留资/配置健康）+ workspace_usage
    # （用量计量：每日趋势/消息·调用·坐席卡/月度配额/计费对账单）。静态走 Jinja get（wr_s*/wu_s*），
    # JS 走 window.T + 大量数字插值短语改 window.Tf（wr_tf_*/wu_tf_*），健康 issue/spark tooltip 全角冒号 ASCII 化。
    ("workspace_roi.html", dict(ui_mode="full", user_name="admin", user_display_name="Admin")),
    ("workspace_usage.html", dict(ui_mode="full", user_name="admin", user_display_name="Admin")),
    # ③-S9w：AI 回复质量看板（extends workspace_base）——静态 aq_s* + JS aq_js*/aq_tf_*。
    ("ai_quality.html", dict(ui_mode="full", user_name="admin", user_display_name="Admin")),
    # ③-S9x：运维实时两页——queue_monitor（extends workspace_base）+ care_schedule（extends base）。
    ("queue_monitor.html", dict(ui_mode="full", user_name="admin", user_display_name="Admin")),
    ("care_schedule.html", {"ui_mode": "full"}),
    # ③-S9y：危机审计（extends base）——静态 ca_s* + JS ca_js*/ca_tf_*。
    ("crisis_audit.html", {"ui_mode": "full"}),
    # ③-S9z：首次接入引导两页——setup（standalone HTML + {% include _i18n_bootstrap %}，
    # 首个 master 账户 + AI 接口初始化）+ setup_wizard（extends workspace_base，渠道接入向导）。
    ("setup.html", {"ai_cfg": {}}),
    ("setup_wizard.html", dict(ui_mode="full", user_name="admin", user_display_name="Admin")),
    # ③-S10a：升级历史（extends workspace_base）——静态 el_s* + JS el_js*/el_tf_*。
    ("escalation_log.html", dict(ui_mode="full", user_name="admin", user_display_name="Admin")),
    # ③-S10b：登录页（standalone HTML）——静态 lg_s* + 既有 login_btn/login_title；has_users 双表单分支。
    ("login.html", {"has_users": True}),
    # ③-S11：草稿审计（extends workspace_base）——静态 da_s*/da_o_* + JS da_js*/da_tf_*。
    ("draft_audit_page.html", dict(ui_mode="full", user_name="admin", user_display_name="Admin")),
    # ③-S11：版本对比（extends base）——静态 diff_s*/diff_s_lbl* + JS diff_js*/diff_tf_*；diff 视图分支需 diff_lines。
    ("diff.html", {"ui_mode": "full"}),
    # ③-S12：知识库冷启动 + 上线自检（extends workspace_base）——静态 ks_s*/gl 复用键 + JS ks_js*/gl_js*/gl_tf_*。
    ("kb_cold_start.html", dict(ui_mode="full", user_name="admin", user_display_name="Admin")),
    ("golive_checklist.html", dict(ui_mode="full", user_name="admin", user_display_name="Admin")),
    # ③-S13：跟进待办 + 客户列表（extends workspace_base）——静态 tk_s*/cl_s* + JS tk_js*/cl_js*/cl_tf_*。
    ("tasks.html", dict(ui_mode="full", user_name="admin", user_display_name="Admin")),
    ("contacts_list.html", dict(ui_mode="full", user_name="admin", user_display_name="Admin")),
    # ③-S14：TTS 预览文件仪表盘（standalone HTML + i18n bootstrap）。
    ("admin_tts_dashboard.html", {}),
    # ③-S15：帮助页（extends base）——help_commands 注册表 + hp_* 键；simple/full page_title 分支。
    ("help.html", _HELP_SEAL_CTX),
    # P33：实时语音试拨页（standalone + i18n bootstrap；静态 rvc_* 已收口，JS rvc_js_*/rvc_tf_* P33b）。
    ("voice_call.html", {}),
)


def _seal_id(item) -> str:
    tmpl, over = item
    return f"{tmpl}-{over.get('ui_mode', 'na')}"


class _SealUndefined:
    """冷渲染专用宽松 Undefined：属性/下标链可续（a.b.c）、迭代为空、|default 生效、渲染为空串。

    收录页多为数据驱动模板（含 ``{% for x in obj.items %}`` / ``{{ a.b.enabled|default(..) }}``）；
    冷渲染门禁只关心「英文可见 HTML 是否残留中文」，故给空 ctx 触发全部空态串即可——不必逐页造数据。
    比逐 page 塞 mock 更稳（新增数据字段不会打破门禁），且只放宽渲染、绝不引入 CJK。"""
    __slots__ = ()

    def __getattr__(self, _n):
        return self
    def __getitem__(self, _k):
        return self
    def __iter__(self):
        return iter(())
    def __call__(self, *a, **k):
        return self
    def __bool__(self):
        return False
    def __len__(self):
        return 0
    def __str__(self):
        return ""
    def __html__(self):
        return ""


def _render_en(template: str, **over) -> str:
    from pathlib import Path
    from jinja2 import Environment, FileSystemLoader

    from jinja2 import Undefined
    from src.web.web_i18n import get_translations

    # 让宽松 Undefined 同时是 jinja Undefined 子类，好让 ``|default`` 过滤器识别并回落。
    class _U(_SealUndefined, Undefined):
        __slots__ = ()

    tdir = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
    env = Environment(loader=FileSystemLoader(str(tdir)), undefined=_U)
    # 复刻 admin.py 注册的自定义过滤器（收录页用到时才需要），使冷渲染门禁与线上一致。
    from src.web.admin import _display_model
    env.filters["display_model"] = _display_model
    ctx = dict(_SEAL_BASE_CTX, i18n=get_translations("en"), **over)
    return env.get_template(template).render(**ctx)


def _strip_client_swapped(html: str) -> str:
    """剥掉「客户端 data-i18n 引擎（_i18n_bootstrap.html）会在加载时替换掉」的中文。

    工作台外壳(workspace_base)与其内容页走客户端换字：HTML 里先写中文 + ``data-i18n[-title|
    -placeholder]`` 锚点，``wsApplyI18n`` 在 DOMContentLoaded 按 WS_I18N[当前语言] 覆盖
    textContent / title / placeholder。故服务端渲染必然含源语言中文，但英文用户实际看到的是换字后
    的英文——这些 data-i18n 命中的中文不是「泄漏」。与源码层 ``_count_untagged_cjk`` 同口径（它也把
    data-i18n 当已接），本函数把这部分「保证会被换掉」的中文剥掉，让渲染门禁对客户端换字页同样成立
    （管理后台 base.html 家族走服务端 i18n.get 直出，本剥离对其为 no-op——无 data-i18n 命中）。
    换字英文确有其值由「key 中英齐备」门禁保证；未挂 data-i18n 的裸中文仍会被下方泄漏扫描抓到。
    """
    import re as _re

    # 1) data-i18n（换 textContent）叶子元素的内文（``data-i18n="k"`` 精确匹配，不含 -title/-placeholder）
    html = _re.sub(r'(<(\w+)\b[^>]*\bdata-i18n="[^"]*"[^>]*>).*?(</\2>)', r"\1\3", html, flags=_re.S)

    # 2) 挂了 data-i18n-title / -placeholder 的标签，其 title / placeholder 属性值加载时会被覆盖
    def _blank(m):
        tag = m.group(0)
        if "data-i18n-title" in tag:
            tag = _re.sub(r'\btitle="[^"]*"', 'title=""', tag)
        if "data-i18n-placeholder" in tag:
            tag = _re.sub(r'\bplaceholder="[^"]*"', 'placeholder=""', tag)
        return tag

    return _re.sub(r"<\w+\b[^>]*>", _blank, html)


def _cjk_leaks(html: str) -> list:
    import re as _re
    from src.web.web_i18n import get_translations

    # HTML 注释 <!-- … --> 不渲染给用户，里头的中文（多为开发标记，如 ``<!-- 运营 Copilot -->``）
    # 不算「可见泄漏」——先剥掉，与源码层 _count_untagged_cjk 同口径（亦剥注释），免假阳。
    visible = _re.sub(r"<!--.*?-->", "", html, flags=_re.S)
    visible = _re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", visible, flags=_re.S | _re.I)
    # 客户端 data-i18n 换字页（工作台家族）：剥掉加载时必被换成英文的中文，避免服务端渲染误报。
    visible = _strip_client_swapped(visible)
    # 语言切换器 / 语言选择项按惯例用「目标语言」母语自称（英文界面里的「中文」「日本語」），与
    # Español/Deutsch/한국어 同理——设计非泄漏（连 Google/FB 的语言选单亦如此）。剔除已知母语名端点
    # 词（endonym）：lang_switch=英文界面切「中文」自称；draft.tpl.lang_ja=语言选单里的「日本語」项。
    _tr_en = get_translations("en") or {}
    for _ek in ("lang_switch", "draft.tpl.lang_ja"):
        _v = _tr_en.get(_ek, "")
        if _v:
            visible = visible.replace(_v, "")
    return _re.findall(r"[\u4e00-\u9fff]+", visible)


@pytest.mark.parametrize("tmpl,over", SEALED_PAGES, ids=[_seal_id(it) for it in SEALED_PAGES])
def test_sealed_pages_en_no_cjk_leak(tmpl, over):
    """密封名单：EN 渲染下，每页「可见 HTML」（剥离脚本/样式）零中文残留（逐页一个用例，失败即点名）。

    base.html(③-S4b 外壳) + cases.html(③-S5) + logs.html(③-S6) + analytics.html(③-S7) 已并入；
    新内容页 i18n 完成即追加进 SEALED_PAGES 即获同等「英文用户看不到一个未翻译汉字」门禁。"""
    leaks = _cjk_leaks(_render_en(tmpl, **over))
    assert not leaks, f"{tmpl}({over.get('ui_mode')}) EN 渲染残留中文（未 i18n 泄漏）: {leaks[:20]}"


def test_admin_chrome_nav_keys_present_and_translated():
    """③-S3：侧栏导航 key 中英齐全；除品牌词(messenger_rpa)外 en 必须真翻译（≠ zh，防照抄中文）。"""
    from src.web.web_i18n import get_translations

    zh, en = get_translations("zh"), get_translations("en")
    same_ok = {"messenger_rpa"}  # 品牌名两语一致，豁免
    missing, untranslated = [], []
    for k in _ADMIN_CHROME_NAV_KEYS:
        if k not in zh or k not in en:
            missing.append(k)
            continue
        if k not in same_ok and zh[k] == en[k]:
            untranslated.append(f"{k}={zh[k]!r}")
    assert not missing, f"侧栏导航 key 缺失: {missing}"
    assert not untranslated, f"en 照抄了中文（未翻译）: {untranslated}"


# ── P36：后端 API 错误文案请求级本地化 ───────────────────────────────
# 前端大量 `d.detail || window.T(fallback)` / `d.error || …` 会 verbatim 直显后端
# 返回的 detail/error，若后端硬编码中文则 EN 用户在报错时仍看到中文。改由
# web_i18n.tr(request, key) 按 request.state.ui_lang 出对应语种译文（复用同一字典）。

_ERR_KB_KEYS = (
    "err.kb.msg_reply_empty", "err.kb.import_bad_format", "err.kb.csv_empty",
    "err.kb.ids_empty", "err.kb.no_update_fields", "err.kb.skill_mgr_unloaded",
    "err.kb.vec_running", "err.kb.vec_all_done", "err.kb.embed_api_fail",
    "err.kb.translate_no_result", "err.kb.query_empty", "err.kb.question_empty",
    "err.kb.img_type", "err.kb.img_size", "err.kb.img_not_found",
    "err.kb.access_denied", "err.kb.topic_empty", "err.kb.ai_not_configured",
    "err.kb.ai_call_failed", "err.kb.ai_bad_format",
)


def test_err_kb_copy_keys_defined_both_langs():
    """P36：kb_routes 用到的 err.kb.* 后端文案 key 须中英齐备（缺 EN=EN 用户看中文报错）。"""
    from src.web.web_i18n import get_translations
    zh, en = get_translations("zh"), get_translations("en")
    bad = [k for k in _ERR_KB_KEYS if k not in zh or k not in en]
    assert not bad, f"后端错误文案 key 缺翻译: {bad}"


def test_tr_localizes_by_request_lang():
    """P36：tr(request,key) 随 request.state.ui_lang 出对应语种；缺 state/未知语种回落 zh；**fmt 生效。"""
    from types import SimpleNamespace
    from src.web.web_i18n import tr

    en_req = SimpleNamespace(state=SimpleNamespace(ui_lang="en"))
    zh_req = SimpleNamespace(state=SimpleNamespace(ui_lang="zh"))
    assert tr(en_req, "err.kb.import_bad_format") == "Invalid import format"
    assert tr(zh_req, "err.kb.import_bad_format") == "无效的导入格式"
    # 无 state / None → 回落默认 zh，不抛
    assert tr(None, "err.kb.csv_empty") == "CSV 内容为空"
    # 未知键回落 default，再回落键名
    assert tr(en_req, "err.kb.__nope__", default="X") == "X"
    assert tr(en_req, "err.kb.__nope__") == "err.kb.__nope__"
    # 占位符格式化（异常吞掉不抛）
    assert tr(en_req, "err.kb.ai_call_failed", err="boom") == "AI call failed: boom"
    # P37：收件箱发送 + 登录家族同口径本地化 + 具名占位符
    assert tr(en_req, "err.inbox.empty_file") == "Empty file"
    assert tr(zh_req, "err.inbox.empty_file") == "空文件"
    assert tr(en_req, "err.login.platform_unsupported", platform="foo") == "Unsupported platform: foo"
    # P38：跨路由共享词表 + 草稿域 f-string 占位符
    assert tr(en_req, "err.perm.supervisor_required") == "Supervisor permission required"
    assert tr(zh_req, "err.svc.inbox_not_ready") == "inbox_store 未就绪"
    assert tr(en_req, "err.draft.test_not_found", id="T9") == "Test T9 not found or already ended"
    # P39：登录/设置域 + 复用既有键 + f-string 参数化收敛（{field}/{err} 具名占位符）
    assert tr(en_req, "err.auth.bad_credentials") == "Incorrect username or password"
    assert tr(zh_req, "err.auth.cannot_kick_self") == "不能踢出自己的当前会话"
    assert tr(en_req, "err.auth.user_exists_or_bad_role", username="bob") == \
        "Username 'bob' already exists or the role is invalid"
    assert tr(en_req, "err.set.json_parse_error", field="agents_json", err="boom") == \
        "agents_json is malformed: boom"
    assert tr(zh_req, "err.set.json_must_be_array", field="agents_json") == "agents_json 必须是 JSON 数组"
    # keywords_format 含**字面**花括号 {intent: ...}：不传 fmt 时原样直出（tr 的 if fmt 护栏），不 KeyError
    assert tr(zh_req, "err.set.keywords_format") == "keywords 必须是 {intent: [kw1, kw2, ...]} 格式"
    # P40：情景记忆域 + RPA 跨平台共享词表（{platform}/{op}/{langs} 参数化）
    assert tr(zh_req, "err.epi.identity_not_ready") == "CrossPlatformIdentity 未就绪"
    assert tr(en_req, "err.rpa.service_not_started", platform="LINE") == "LINE RPA service is not running"
    assert tr(zh_req, "err.rpa.service_not_started", platform="WhatsApp") == "WhatsApp RPA 服务未启动"
    assert tr(en_req, "err.rpa.op_failed", op="proactive_stats", err="boom") == "proactive_stats failed: boom"
    assert tr(zh_req, "err.rpa.lang_unsupported", lang="xx", langs=["zh", "en"]) == \
        "不支持的语言代码: xx。支持: ['zh', 'en']"
    # P41：inbox 工作台域——{field} 参数化 + 复用 err.svc.inbox_not_ready + f-string esc_id
    assert tr(en_req, "err.ws.field_required", field="reason") == "reason is required"
    assert tr(zh_req, "err.ws.field_required", field="conversation_id") == "conversation_id 不能为空"
    assert tr(en_req, "err.ws.escalation_not_found", esc_id="E7") == "Escalation record E7 not found"
    assert tr(zh_req, "err.ws.contact_not_found") == "contact 不存在"
    # P42：inbox 余部批量——服务不可用键 + f-string 套餐/渠道占位符
    assert tr(en_req, "err.svc.config_manager_not_ready") == "config_manager unavailable"
    assert tr(zh_req, "err.ws.unsupported_automation_mode", mode="foo") == "不支持的自动化模式: foo"
    assert tr(en_req, "err.ws.plan_channel_not_included", plan="Free", channel="LINE") == \
        'Your current plan (Free) does not include the "LINE" channel; please upgrade.'
    # P43a：13 个非 inbox 中小文件——svc/rpa/voice/persona/ec/tg/case/cp/ca/page 家族
    assert tr(en_req, "err.svc.skill_manager_not_ready") == "SkillManager not initialized (Bot not running)"
    assert tr(zh_req, "err.rpa.service_not_built", platform="LINE") == "LINE RPA 服务未构建或未启用"
    assert tr(en_req, "err.rpa.device_not_registered", serial="ABC") == "Device ABC is not registered"
    assert tr(zh_req, "err.voice.persona_not_found", persona_id="p1") == "人设 p1 不存在"
    assert tr(en_req, "err.voice.ref_save_failed", err="boom") == "Failed to save reference audio: boom"
    assert tr(en_req, "err.perm.master_only") == "This action can only be performed by the master account"
    assert tr(zh_req, "err.case.not_found", case_id="C3") == "Case C3 不存在"
    assert tr(en_req, "err.ec.tools_disabled") == "E-commerce tools are not enabled"
    assert tr(zh_req, "err.tg.save_config_failed") == "保存配置失败"
    assert tr(en_req, "err.ca.event_not_found") == "Event not found or crisis audit is not enabled"


def test_routeconv_apply_map_and_import():
    """P38：i18n_routeconv 施工器——精确替换命中计数、未命中上报、幂等、import 幂等注入。"""
    from scripts.i18n_routeconv import apply_map, ensure_import

    src = 'raise HTTPException(403, "需要主管权限")\nx = "需要主管权限"\n'
    mapping = {
        'HTTPException(403, "需要主管权限")': 'HTTPException(403, tr(request, "err.perm.supervisor_required"))',
        '"不存在的片段"': 'X',
    }
    out, report = apply_map(src, mapping)
    assert report['HTTPException(403, "需要主管权限")'] == 1
    assert report['"不存在的片段"'] == 0  # 未命中如实上报，不静默
    assert 'tr(request, "err.perm.supervisor_required")' in out
    # 边界字符（引号+括号）护住：裸 "需要主管权限" 字符串不被误替
    assert 'x = "需要主管权限"' in out
    # 幂等：再套一次无变化
    out2, report2 = apply_map(out, mapping)
    assert report2['HTTPException(403, "需要主管权限")'] == 0 and out2 == out
    # import 幂等
    imp = "from src.web.web_i18n import tr"
    t1, added1 = ensure_import("from fastapi import Request\n", imp)
    assert added1 and imp in t1
    t2, added2 = ensure_import(t1, imp)
    assert not added2 and t2 == t1
    # P42：多行括号 import 不被插进括号内（回归 tags 文件语法破坏 bug）——插到语句收尾后
    multi = "from x import (\n    a,\n    b,\n)\nlogger = None\n"
    t3, added3 = ensure_import(multi, imp)
    assert added3
    import ast as _ast
    _ast.parse(t3)  # 不再产出 SyntaxError
    assert t3.index(imp) > t3.index(")")  # 落在括号 import 收尾之后


def test_routeconv_suggest_map_reuse_and_new():
    """P39：suggest_map 键匹配建议——中文精确命中现有键→reuse，未命中→new，f-string 标占位符。"""
    from scripts.i18n_routeconv import suggest_map

    zh = {
        "some.key": "需要主管权限",   # 现成键，供 reuse 命中
    }
    text = (
        'raise HTTPException(403, "需要主管权限")\n'
        'raise HTTPException(400, "全新的错误文案")\n'
        'raise HTTPException(400, f"字段 {name} 无效")\n'
    )
    sugg = {s["literal"]: s for s in suggest_map(text, zh)}
    # 精确命中 → reuse 现有键
    assert sugg['"需要主管权限"']["action"] == "reuse"
    assert sugg['"需要主管权限"']["match_key"] == "some.key"
    # 未命中 → new
    assert sugg['"全新的错误文案"']["action"] == "new"
    assert sugg['"全新的错误文案"']["match_key"] is None
    # f-string 被识别 + 提取占位符（不自动改，仅提示需具名 fmt）
    fstr = sugg['f"字段 {name} 无效"']
    assert fstr["is_fstring"] and fstr["placeholders"] == ["name"] and fstr["action"] == "new"


def test_routeconv_draft_map_scope_safe_and_composed():
    """P40：draft_map 只圈 HTTPException/detail/error 上下文（作用域安全，不产出裸字面量），
    build_draft_map 用 (key, fmt) 组装出带具名占位符的 tr(...) 调用（f-string/参数化统一走此路）。"""
    from scripts.i18n_routeconv import apply_map, build_draft_map, draft_map

    text = (
        'raise HTTPException(400, "服务未启动")\n'
        'raise HTTPException(status_code=404, detail="记录不存在")\n'
        'raise HTTPException(500, f"写入失败: {e}")\n'
        'label = "服务未启动"  # 非响应上下文：不得被 draft 圈进\n'
    )
    ents = {e["body"]: e for e in draft_map(text, {})}
    # 三种响应上下文各被识别；裸字面量（label=…）不产出条目
    assert ents["服务未启动"]["kind"] == "httpexc" and ents["服务未启动"]["count"] == 1
    assert ents["记录不存在"]["kind"] == "kwarg" and ents["记录不存在"]["field"] == "detail"
    assert ents["写入失败: {e}"]["is_fstring"] is True

    def key_for(e):
        return {
            "服务未启动": ("err.rpa.service_not_started", {"platform": '"LINE"'}),
            "记录不存在": "err.epi.record_not_found",
            "写入失败: {e}": ("err.rpa.write_failed", {"err": "e"}),
        }.get(e["body"])

    mapping, pending, fstrings = build_draft_map(draft_map(text, {}), key_for)
    assert pending == [] and fstrings == []
    out, _ = apply_map(text, mapping)
    assert 'HTTPException(400, tr(request, "err.rpa.service_not_started", platform="LINE"))' in out
    assert 'detail=tr(request, "err.epi.record_not_found")' in out
    assert 'HTTPException(500, tr(request, "err.rpa.write_failed", err=e))' in out
    # 作用域安全：非响应上下文的裸中文原样保留
    assert 'label = "服务未启动"' in out


def test_routeconv_draft_coverage_surfaces_gaps():
    """P41：draft_coverage 诚实体检——全 draft 可施工=ratio 1.0；字符串拼接/多参等
    draft 圈不进的账本行被**点名**（ratio<1，出现在 uncovered），供选靶分流。"""
    from scripts.i18n_routeconv import draft_coverage

    text = (
        'raise HTTPException(400, "参数缺失")\n'
        'raise HTTPException(400, "未知 action：" + action)\n'   # 拼接 → draft 圈不进
    )
    cov = draft_coverage(text, {})
    assert cov["ledger"] == 2 and cov["covered"] == 1
    assert cov["ratio"] == 0.5
    assert len(cov["uncovered"]) == 1 and "未知 action" in cov["uncovered"][0][1]


def test_routeconv_coverage_report_sorted():
    """P42：全库覆盖率报表——每行含 file/ledger/covered/ratio，按 ratio 升序（最需人工的在前）。"""
    from scripts.i18n_routeconv import coverage_report
    from src.web.web_i18n import get_translations

    rows = coverage_report(get_translations("zh"))
    assert rows, "应至少有若干仍含硬编码中文的路由文件"
    ratios = [r["ratio"] for r in rows]
    assert ratios == sorted(ratios)  # 升序
    for r in rows:
        assert set(r) >= {"file", "ledger", "covered", "ratio"} and r["ledger"] > 0


# ── P37：后端路由响应文案「棘轮账本」门禁 ────────────────────────────
# scan_routes_response_cjk() 量化每个 routes/*.py 里还剩多少条硬编码中文响应文案
# （HTTPException detail / 返回 dict 的 detail/error）。此账本是**棘轮**：
#   · 任何文件超基线（含未登记的新文件带中文）→ 失败，逼迫新代码走 tr(request,'err.*')；
#   · 任何文件低于基线（localize 了却没更新账本）→ 也失败，逼迫把天花板调低（只减不增）。
# 已收口(=0)的文件显式登记为 0，作为「回潮即点名」的密封线。P36/P37 逐族下降至此。
_ROUTE_CJK_CEILINGS = {
    "messenger_rpa_routes.py": 147,
    # ── 已收口（sealed=0）──
    "kb_routes.py": 0,                    # P36
    "unified_inbox_send_routes.py": 0,    # P37
    "unified_inbox_login_routes.py": 0,   # P37
    "drafts_routes.py": 0,                # P38（shared err.perm/svc/req 词表 + err.draft.*）
    "auth_user_routes.py": 0,             # P39（复用 token_error/su_js_003/base.shell.pwd_min_len + err.auth.*）
    "settings_routes.py": 0,              # P39（f-string 参数化收敛 err.set.*）
    "episodic_identity_routes.py": 0,     # P40（draft_map kwarg 全覆盖 + err.epi.*）
    "line_rpa_routes.py": 0,              # P40（err.rpa.* 共享词表 {platform} 参数化 + 复用 err.set.*）
    "whatsapp_rpa_routes.py": 0,          # P40（同上 + {op} 参数化 err.rpa.op_failed）
    "unified_inbox_workspace_contacts_routes.py": 0,    # P41（err.ws.* + {field} 参数化）
    "unified_inbox_relationship_routes.py": 0,          # P41（复用 err.svc.inbox_not_ready）
    "unified_inbox_workspace_escalation_routes.py": 0,  # P41（inbox 措辞归一 + {esc_id} f-string）
    # ── P42：inbox 余部 21 文件批量收口（共享 curation + err.ws.*/err.svc.* 复用）──
    "unified_inbox_workflow_routes.py": 0, "unified_inbox_collab_mention_routes.py": 0,
    "unified_inbox_template_routes.py": 0, "unified_inbox_batch_notif_routes.py": 0,
    "unified_inbox_stored_read_routes.py": 0, "unified_inbox_account_routes.py": 0,
    "unified_inbox_queue_webhook_routes.py": 0, "unified_inbox_workspace_presence_routes.py": 0,
    "unified_inbox_workspace_tags_routes.py": 0, "unified_inbox_qa_churn_routes.py": 0,
    "unified_inbox_copilot_routes.py": 0, "unified_inbox_intel_profile_routes.py": 0,
    "unified_inbox_setup_routes.py": 0, "unified_inbox_analyze_routes.py": 0,
    "unified_inbox_aux_read_routes.py": 0, "unified_inbox_auth.py": 0,
    "unified_inbox_collab_context_routes.py": 0, "unified_inbox_read_routes.py": 0,
    "unified_inbox_realtime_routes.py": 0, "unified_inbox_routing_search_routes.py": 0,
    "unified_inbox_workspace_prefs_routes.py": 0,
    "unified_inbox_desktop_routes.py": 0,               # P41（含拼接 unknown_action 人工兜底）
    # ── P43a：13 个非 inbox 中小文件批量收口（svc/rpa/voice/persona/ec/tg/case/cp/ca/page）──
    "rpa_overview_routes.py": 0, "voice_routes.py": 0, "persona_routes.py": 0,
    "cases_routes.py": 0, "ecommerce_tools_routes.py": 0, "telegram_routes.py": 0,
    "chat_test_routes.py": 0, "copilot_routes.py": 0, "crisis_audit_routes.py": 0,
    "branding_routes.py": 0, "human_escalation_routes.py": 0, "page_routes.py": 0,
    "strategy_routes.py": 0,
}


def test_route_response_cjk_ledger_ratchet():
    """路由响应文案硬编码中文「棘轮账本」：只减不增。超基线/新增未登记 → 逼接 tr()；
    低于基线（localize 后没更新账本）→ 逼把天花板调低，使账本恒等于实况。"""
    from scripts.i18n_scan import scan_routes_response_cjk

    actual = scan_routes_response_cjk()
    names = set(_ROUTE_CJK_CEILINGS) | set(actual)
    increased, decreased = {}, {}
    for n in sorted(names):
        a = actual.get(n, 0)
        cap = _ROUTE_CJK_CEILINGS.get(n)
        if cap is None:
            increased[n] = a  # 未登记的新路由文件带硬编码中文响应文案
        elif a > cap:
            increased[n] = f"{a} > {cap}"
        elif a < cap:
            decreased[n] = f"{a} < {cap}"
    assert not increased, (
        "以下路由新增/超基线硬编码中文响应文案，请改走 tr(request, 'err.*'): "
        f"{increased}"
    )
    assert not decreased, (
        "以下路由已减少硬编码中文（好事！），请把 _ROUTE_CJK_CEILINGS 对应值调低到实况"
        f"（棘轮只减不增）: {decreased}"
    )
