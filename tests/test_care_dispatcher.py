"""Phase O3：主动关怀派发器单测。

覆盖：quiet_hours 顺延纯函数 + 派发成功→mark_sent + 引用 topic 进 prompt +
无上下文 skip + already_discussed skip + LLM 空 skip + 身份泄露 skip +
send 失败留 pending + max_per_tick 限流 + dry_run + 未到期不发。
"""
from datetime import datetime

from src.contacts.care_commitment import CareCommitment
from src.contacts.care_dispatcher import CareDispatcher, shift_out_of_quiet_hours
from src.contacts.care_schedule import CareScheduleStore

NOW = datetime(2026, 6, 17, 10, 0, 0).timestamp()  # 周三 10:00（非安静时段）


def _store_with(n=1, topic="面试", due_offset_days=-0.1):
    s = CareScheduleStore(":memory:")
    for i in range(n):
        due = NOW + due_offset_days * 86400
        c = CareCommitment(due_at=due, event_at=due, topic=f"{topic}{i}" if n > 1 else topic,
                           sentiment="neutral", anchor_text="x",
                           source_text="明天面试好紧张", confidence=0.85)
        s.add_commitment(c, contact_key=f"tg:u{i}", platform="telegram",
                         account_id="default", chat_key=f"u{i}")
    return s


class _AI:
    def __init__(self, reply="你之前说的面试怎么样啦？😊"):
        self.reply = reply
        self.prompts = []

    async def chat(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


def _sender(record, row_id=123):
    async def _send(channel, account_id, chat_name, reply, defer_until, reason, staleness, extra):
        record.append({"channel": channel, "account_id": account_id, "chat_name": chat_name,
                       "reply": reply, "defer_until": defer_until, "reason": reason,
                       "extra": extra})
        return row_id
    return _send


# ── quiet hours 纯函数 ──────────────────────────────────────────────────
def test_quiet_hours_no_window():
    assert shift_out_of_quiet_hours(NOW, start_hour=8, end_hour=8) == NOW


def test_quiet_hours_daytime_not_shifted():
    # 10:00 不在 23-8 安静窗 → 原样
    assert shift_out_of_quiet_hours(NOW, start_hour=23, end_hour=8) == NOW


def test_quiet_hours_late_night_shifts_to_morning():
    late = datetime(2026, 6, 17, 23, 30, 0).timestamp()
    out = shift_out_of_quiet_hours(late, start_hour=23, end_hour=8)
    d = datetime.fromtimestamp(out)
    assert (d.month, d.day, d.hour) == (6, 18, 8)  # 次日 08:00


def test_quiet_hours_early_morning_shifts_same_day():
    early = datetime(2026, 6, 17, 3, 0, 0).timestamp()
    out = shift_out_of_quiet_hours(early, start_hour=23, end_hour=8)
    d = datetime.fromtimestamp(out)
    assert (d.month, d.day, d.hour) == (6, 17, 8)  # 当日 08:00


# ── 派发器 ──────────────────────────────────────────────────────────────
async def test_dispatch_success_marks_sent():
    s = _store_with()
    rec = []
    ai = _AI()
    d = CareDispatcher(store=s, ai_client=ai, send_callback=_sender(rec),
                       context_provider=lambda ck: "上次聊到她准备面试", default_lang="zh")
    n = await d.run_once(now=NOW)
    assert n == 1
    assert s.count(status="sent") == 1 and s.count(status="pending") == 0
    assert len(rec) == 1 and rec[0]["channel"] == "telegram" and rec[0]["chat_name"] == "u0"
    assert rec[0]["extra"]["care"] is True
    # prompt 引用了 topic
    assert "面试" in ai.prompts[0]


async def test_run_once_expires_overdue_first():
    # 一条逾期太久（10 天前）的 pending → run_once 先 expire 之，不派发
    s = CareScheduleStore(":memory:")
    old = CareCommitment(due_at=NOW - 10 * 86400, event_at=NOW - 10 * 86400,
                         topic="面试", sentiment="neutral", anchor_text="x",
                         source_text="y", confidence=0.85)
    s.add_commitment(old, contact_key="tg:u1", platform="telegram", chat_key="u1")
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx", expire_grace_days=1.0)
    n = await d.run_once(now=NOW)
    assert n == 0 and not rec
    assert s.count(status="expired") == 1 and s.count(status="pending") == 0


async def test_skip_when_no_context():
    s = _store_with()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "", skip_if_no_context=True)
    n = await d.run_once(now=NOW)
    assert n == 0 and not rec
    assert s.count(status="skipped") == 1


# ── K2b：变现配额门控回调 ────────────────────────────────────────────────
async def test_paywall_blocks_over_quota_free_user():
    s = _store_with()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx",
                       proactive_allowed=lambda ck: False)  # 模拟免费超额
    n = await d.run_once(now=NOW)
    assert n == 0 and not rec
    assert s.count(status="skipped") == 1
    # 跳过原因可见
    item = s.list_recent(status="skipped")[0]
    assert item["note"] == "paywall_quota"


async def test_paywall_allows_when_callback_true():
    s = _store_with()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx",
                       proactive_allowed=lambda ck: True)
    n = await d.run_once(now=NOW)
    assert n == 1 and len(rec) == 1
    assert s.count(status="sent") == 1


async def test_paywall_none_callback_no_change():
    # 未注入回调（gate 关）→ 行为与原来完全一致
    s = _store_with()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx", proactive_allowed=None)
    n = await d.run_once(now=NOW)
    assert n == 1 and s.count(status="sent") == 1


async def test_already_discussed_skips():
    s = _store_with()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx",
                       already_discussed=lambda ck, topic: True)
    n = await d.run_once(now=NOW)
    assert n == 0 and not rec
    assert s.count(status="skipped") == 1


async def test_llm_empty_skips():
    s = _store_with()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(reply="  "), send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx")
    assert await d.run_once(now=NOW) == 0
    assert s.count(status="skipped") == 1


async def test_identity_leak_skips():
    s = _store_with()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(reply="作为AI助手我提醒你面试"),
                       send_callback=_sender(rec), context_provider=lambda ck: "ctx")
    assert await d.run_once(now=NOW) == 0
    assert s.count(status="skipped") == 1


async def test_send_failure_keeps_pending():
    s = _store_with()

    async def _bad_send(*a, **k):
        return 0  # enqueue 失败（如 gate 拦）
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_bad_send,
                       context_provider=lambda ck: "ctx")
    assert await d.run_once(now=NOW) == 0
    assert s.count(status="pending") == 1  # 留待下个 tick 重试


async def test_max_per_tick_limits():
    s = _store_with(n=5)
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx", max_per_tick=2)
    n = await d.run_once(now=NOW)
    assert n == 2 and len(rec) == 2
    assert s.count(status="pending") == 3


async def test_dry_run_no_send_but_marks_sent():
    s = _store_with()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx", dry_run=True)
    n = await d.run_once(now=NOW)
    assert n == 1 and not rec  # 没真发
    assert s.count(status="sent") == 1


async def test_not_due_not_dispatched():
    s = _store_with(due_offset_days=3.0)  # 3 天后才到期
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx")
    assert await d.run_once(now=NOW) == 0
    assert s.count(status="pending") == 1


# ── Phase O 质量闭环：dislike 黑名单防重 + dry_run 样本 ──────────────────
class _AISeq:
    """按序返回不同 reply（模拟重生成）。"""
    def __init__(self, replies):
        self._replies = list(replies)
        self.prompts = []

    async def chat(self, prompt, **kw):
        self.prompts.append(prompt)
        if self._replies:
            return self._replies.pop(0)
        return ""


def _reset_dislike():
    from src.monitoring.metrics_store import get_metrics_store
    ms = get_metrics_store()
    ms._reactivation_disliked_replies.clear()
    ms._care_dry_samples.clear()
    return ms


async def test_dislike_similarity_regenerates():
    # 首条 reply 命中黑名单 → 重生成一条不同的 → 发重生成版
    ms = _reset_dislike()
    bad = "你之前说的面试怎么样啦？😊"
    ms.add_disliked_reply(bad)
    good = "记得你提过想换工作，最近有眉目了吗？"
    s = _store_with()
    rec = []
    ai = _AISeq([bad, good])
    d = CareDispatcher(store=s, ai_client=ai, send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx", default_lang="zh")
    n = await d.run_once(now=NOW)
    assert n == 1 and len(rec) == 1
    assert rec[0]["reply"] == good  # 发的是重生成版
    assert s.count(status="sent") == 1


async def test_dislike_similarity_skips_when_regen_still_bad():
    # 两次都命中黑名单 → mark_skipped(disliked_similarity)，不发
    ms = _reset_dislike()
    bad = "你之前说的面试怎么样啦？😊"
    ms.add_disliked_reply(bad)
    s = _store_with()
    rec = []
    ai = _AISeq([bad, bad])
    d = CareDispatcher(store=s, ai_client=ai, send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx")
    n = await d.run_once(now=NOW)
    assert n == 0 and not rec
    assert s.count(status="skipped") == 1


async def test_dry_run_records_sample():
    ms = _reset_dislike()
    s = _store_with()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(reply="记得你提过面试，顺利吗？"),
                       send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx", dry_run=True)
    n = await d.run_once(now=NOW)
    assert n == 1 and not rec
    samples = ms.care_dry_samples(limit=10)
    assert len(samples) == 1
    assert samples[0]["topic"] == "面试"
    assert samples[0]["platform"] == "telegram"
    assert "面试" in samples[0]["reply_text"]


# ── Phase ④续¹⁰：危机来源关怀「克制陪伴」专线 ──────────────────────────────
from src.contacts.care_schedule import CRISIS_CARE_TOPIC


def _crisis_store(contact="tg:u1", chat="u1"):
    s = CareScheduleStore(":memory:")
    c = CareCommitment(due_at=NOW - 60, event_at=NOW - 60, topic=CRISIS_CARE_TOPIC,
                       sentiment="negative", anchor_text="",
                       source_text="近期危机信号，主动护栏拦下打扰，转关怀回访", confidence=1.0)
    s.add_commitment(c, contact_key=contact, platform="telegram",
                     account_id="default", chat_key=chat)
    return s


async def test_crisis_care_uses_restrained_prompt():
    """危机来源关怀 → 用克制陪伴模板（不寒暄/不追问），且不引用 topic「情绪关怀」字样。"""
    s = _crisis_store()
    rec = []
    ai = _AI(reply="我这两天一直想着你，不用急着回我，我都在。")
    d = CareDispatcher(store=s, ai_client=ai, send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx", default_lang="zh")
    n = await d.run_once(now=NOW)
    assert n == 1 and len(rec) == 1
    p = ai.prompts[0]
    # 用的是克制模板：含陪伴措辞、明令不追问；不把保留 topic 当"具体事"塞进话术
    assert "不带任何压力" in p and "汇报近况" in p
    assert "情绪关怀" not in p
    assert rec[0]["reason"] == "care:crisis"
    assert rec[0]["extra"]["crisis_care"] is True


async def test_crisis_care_sends_even_without_context():
    """无对话上下文也照发（陪伴本身即目的，不走 no_context skip）。"""
    s = _crisis_store()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(reply="我在，你不是一个人。"),
                       send_callback=_sender(rec),
                       context_provider=lambda ck: "", skip_if_no_context=True)
    n = await d.run_once(now=NOW)
    assert n == 1 and len(rec) == 1
    assert s.count(status="sent") == 1


async def test_crisis_care_bypasses_paywall():
    """免费超额也不掐断危机关怀（伦理优先于变现）。"""
    s = _crisis_store()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(reply="我在，慢慢来，我陪着你。"),
                       send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx",
                       proactive_allowed=lambda ck: False)  # 模拟免费超额
    n = await d.run_once(now=NOW)
    assert n == 1 and len(rec) == 1
    assert s.count(status="sent") == 1


async def test_crisis_care_does_not_inject_dialogue_context():
    """克制模板不注入对话要点（避免回放低谷内容）→ context_provider 不被调用。"""
    s = _crisis_store()
    called = []
    d = CareDispatcher(store=s, ai_client=_AI(reply="我在呢。"),
                       send_callback=_sender([]),
                       context_provider=lambda ck: called.append(ck) or "ctx")
    await d.run_once(now=NOW)
    assert called == []  # 危机关怀不取对话上下文


async def test_normal_care_still_uses_generic_prompt():
    """普通关怀（非危机 topic）行为不变：仍用通用模板、仍引用 topic。"""
    s = _store_with(topic="复查")
    rec = []
    ai = _AI(reply="记得你说要去复查，结果还好吗？")
    d = CareDispatcher(store=s, ai_client=ai, send_callback=_sender(rec),
                       context_provider=lambda ck: "上次聊到复查", default_lang="zh")
    n = await d.run_once(now=NOW)
    assert n == 1
    assert "复查" in ai.prompts[0]
    assert "不带任何压力" not in ai.prompts[0]  # 不是克制模板
    assert rec[0]["reason"].startswith("care:复查")


async def test_quiet_hours_defers_send_time():
    # now 在深夜 → defer_until 应被顺延到早 8 点以后
    late = datetime(2026, 6, 17, 23, 50, 0).timestamp()
    s = CareScheduleStore(":memory:")
    c = CareCommitment(due_at=late - 3600, event_at=late - 3600, topic="面试",
                       sentiment="neutral", anchor_text="x", source_text="y", confidence=0.85)
    s.add_commitment(c, contact_key="tg:u1", platform="telegram", chat_key="u1")
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx",
                       quiet_start_hour=23, quiet_end_hour=8)
    await d.run_once(now=late)
    assert len(rec) == 1
    dd = datetime.fromtimestamp(rec[0]["defer_until"])
    assert dd.hour >= 8 and dd.day == 18  # 顺延到次日 08:00 之后
