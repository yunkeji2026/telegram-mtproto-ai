"""AI 安全/质量总览聚合（InboxStore.ai_safety_summary）单测。

复用 draft_audit_log 的处置动作 + 风险分级，锁定「AI 发得靠不靠谱」的质量口径不变量：
- adopt/edit/reject 率以「人工审过总数(approved+edit_send+rejected)」为分母；
- autosend / blocked 为独立计数（AI 自动发 / 风控拦截转人工）；
- high_risk 只命中非低危分级（elevated/severe…），none/low 不计；
- since_ts 时间窗过滤生效，零分母不炸。
"""
import time

from src.inbox.store import InboxStore
from src.inbox.models import InboxConversation


def test_quality_rates_and_counts(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    # 人工审：4 approved + 4 edit_send + 2 rejected = reviewed 10
    for i in range(4):
        store.record_draft_audit(f"a{i}", action="approved", ts=now)
    for i in range(4):
        store.record_draft_audit(f"e{i}", action="edit_send", ts=now)
    for i in range(2):
        store.record_draft_audit(f"r{i}", action="rejected", ts=now)
    # AI 自动发 5 + 风控拦截 3（带高危分级）
    for i in range(5):
        store.record_draft_audit(f"s{i}", action="autosend", ts=now)
    for i in range(3):
        store.record_draft_audit(f"b{i}", action="blocked", risk_level="severe", ts=now)

    d = store.ai_safety_summary(since_ts=now - 3600)
    assert d["reviewed"] == 10
    assert (d["approved"], d["edited"], d["rejected"]) == (4, 4, 2)
    assert d["adopt_rate"] == 0.4
    assert d["edit_rate"] == 0.4
    assert d["reject_rate"] == 0.2
    assert d["autosend"] == 5
    assert d["blocked"] == 3
    assert d["high_risk"] == 3  # severe 计入高危
    store.close()


def test_window_filter_and_zero_denominator(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    # 窗口外（2 天前）不计
    store.record_draft_audit("old", action="approved", ts=now - 2 * 86400)
    d = store.ai_safety_summary(since_ts=now - 3600)
    assert d["reviewed"] == 0
    assert d["adopt_rate"] == 0.0  # 零分母不抛
    # 窗口内
    store.record_draft_audit("new", action="approved", ts=now)
    d2 = store.ai_safety_summary(since_ts=now - 3600)
    assert d2["reviewed"] == 1 and d2["adopt_rate"] == 1.0
    store.close()


def test_low_risk_not_counted_as_high(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    store.record_draft_audit("x1", action="blocked", risk_level="none", ts=now)
    store.record_draft_audit("x2", action="autosend", risk_level="low", ts=now)
    store.record_draft_audit("x3", action="blocked", risk_level="elevated", ts=now)
    d = store.ai_safety_summary(since_ts=now - 3600)
    assert d["high_risk"] == 1  # 仅 elevated 计入；none/low 不计
    assert d["blocked"] == 2
    store.close()


def test_trend_only_when_requested(tmp_path):
    """include_trend=False 不带 trend（环比第二次调用免重复日聚合）；True 时按日带采纳率。"""
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    d0 = store.ai_safety_summary(since_ts=now - 7 * 86400)
    assert "trend" not in d0
    store.record_draft_audit("a", action="approved", ts=now)
    store.record_draft_audit("e", action="edit_send", ts=now)
    store.record_draft_audit("s", action="autosend", ts=now)
    d = store.ai_safety_summary(since_ts=now - 7 * 86400, include_trend=True)
    assert isinstance(d.get("trend"), list) and len(d["trend"]) >= 1
    today = d["trend"][-1]
    assert today["autosend"] == 1
    assert today["reviewed"] == 2  # approved + edit_send
    assert today["adopt_rate"] == 0.5
    store.close()


def test_top_blocked_conversations(tmp_path):
    """Top-N 被拦会话下钻：按次数降序、回填名/平台、reason 取最近一条 blocked、
    无会话行则名回落 conversation_id、窗口外/非 blocked 不计、limit 生效。"""
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    store.upsert_conversation(InboxConversation(
        conversation_id="convA", platform="telegram", display_name="Alice"))
    for i, rz in enumerate(["r_old", "r_mid", "severe_selfharm"]):
        store.record_draft_audit(f"da{i}", action="blocked", reason=rz,
                                 conversation_id="convA", ts=now - (3 - i))
    for i in range(2):
        store.record_draft_audit(f"db{i}", action="blocked", reason="pii",
                                 conversation_id="convB", ts=now)
    store.record_draft_audit("dc", action="blocked", reason="",
                             conversation_id="convC", ts=now)
    # 非 blocked / 窗口外不计
    store.record_draft_audit("dn", action="autosend", conversation_id="convA", ts=now)
    store.record_draft_audit("do", action="blocked", conversation_id="convA", ts=now - 10 * 86400)

    top = store.top_blocked_conversations(since_ts=now - 3600, limit=8)
    assert [t["conversation_id"] for t in top] == ["convA", "convB", "convC"]  # 次数降序
    a = top[0]
    assert a["count"] == 3
    assert a["name"] == "Alice" and a["platform"] == "telegram"  # 回填会话名/平台
    assert a["reason"] == "severe_selfharm"  # 最近一条 blocked 原因
    assert a["last_ts"] == now - 1  # MAX(ts) 落窗口内最近
    c = top[-1]
    assert c["name"] == "convC" and c["platform"] == ""  # 无会话行→名回落 cid
    assert len(store.top_blocked_conversations(since_ts=now - 3600, limit=2)) == 2  # limit 生效
    store.close()


def test_deep_link_stats(tmp_path):
    """E3 下钻观测：opens=点击总数、convs=命中去重会话、processed=点后确有处置的去重会话；
    source/窗口过滤生效、空 event 忽略、处置须晚于首次下钻才算转化。"""
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    store.record_ui_event("deep_link_opened", source="ai_safety", conversation_id="convA", ts=now - 100)
    store.record_ui_event("deep_link_opened", source="ai_safety", conversation_id="convA", ts=now - 40)
    store.record_ui_event("deep_link_opened", source="ai_safety", conversation_id="convB", ts=now - 50)
    # convA 在首次下钻(now-100)之后处置 → 算转化；convB 处置早于下钻(now-50) → 不算
    store.record_draft_audit("da", action="edit_send", conversation_id="convA", ts=now - 10)
    store.record_draft_audit("db", action="approved", conversation_id="convB", ts=now - 200)
    # 别的 source / 窗口外不计
    store.record_ui_event("deep_link_opened", source="other", conversation_id="convC", ts=now - 10)
    store.record_ui_event("deep_link_opened", source="ai_safety", conversation_id="convD", ts=now - 10 * 86400)

    st = store.deep_link_stats(source="ai_safety", since_ts=now - 3600)
    assert st["opens"] == 3        # convA×2 + convB×1（other/窗口外不计）
    assert st["convs"] == 2        # convA, convB
    assert st["processed"] == 1    # 仅 convA（convB 处置早于下钻）
    store.record_ui_event("", source="ai_safety", conversation_id="x")  # 空 event 忽略
    assert store.deep_link_stats(source="ai_safety", since_ts=now - 3600)["opens"] == 3
    store.close()


def test_ai_quality_daily_series(tmp_path):
    """F2b 校准数据源：按日原始计数，口径同 ai_safety_summary（reviewed=approved+edit_send+
    rejected；high_risk=非低危分级计数，与 action 正交），按天分桶升序、超窗口不计。"""
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    day = 86400
    # 今天：2 approved（其一 elevated）+ 1 edit_send + 1 rejected + 1 autosend + 1 blocked(severe)
    store.record_draft_audit("a1", action="approved", ts=now)
    store.record_draft_audit("a2", action="approved", risk_level="elevated", ts=now)  # 高危+1
    store.record_draft_audit("e1", action="edit_send", ts=now)
    store.record_draft_audit("r1", action="rejected", ts=now)
    store.record_draft_audit("s1", action="autosend", ts=now)
    store.record_draft_audit("b1", action="blocked", risk_level="severe", ts=now)     # 高危+1
    # 昨天：1 approved
    store.record_draft_audit("y1", action="approved", ts=now - day)
    # 40 天前：超 days=30 窗口，不计
    store.record_draft_audit("old", action="approved", ts=now - 40 * day)

    series = store.ai_quality_daily_series(days=30)
    assert len(series) == 2  # 今天 + 昨天（40 天前被窗口排除）
    assert series[0]["reviewed"] == 1  # 升序：昨天在前
    today = series[-1]
    assert (today["approved"], today["edited"], today["rejected"]) == (2, 1, 1)
    assert today["reviewed"] == 4
    assert today["autosend"] == 1 and today["blocked"] == 1
    assert today["high_risk"] == 2  # elevated(approved) + severe(blocked)，与 action 正交
    store.close()
