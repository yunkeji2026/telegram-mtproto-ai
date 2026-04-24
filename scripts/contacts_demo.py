#!/usr/bin/env python3
"""跨平台 Contacts 子系统——业务流现场演示。

运行：
    python scripts/contacts_demo.py

演示内容：
    1) 一个 Messenger 陌生人的 5 天对话 → intimacy 爬高
    2) 告别场景触发 Readiness 开窗
    3) 系统签发 token + 选话术 + 合规巡检
    4) 我们"假装"在 Messenger 发出了这条话术
    5) 用户加 LINE 发首条带 token
    6) Gateway 自动合并 Contact
    7) 打印完整的跨平台 Journey Timeline + Funnel 统计

**不依赖任何外部服务**，跑完会在 /tmp 下建一个临时的 contacts.db。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

# 允许从项目根 import src.*
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.contacts import bootstrap_contacts_subsystem
from src.contacts.models import CHANNEL_MESSENGER


def _box(title: str) -> None:
    bar = "━" * max(6, len(title) + 4)
    print(f"\n{bar}\n  {title}\n{bar}")


def _step(n, title: str) -> None:
    print(f"\n【步骤 {n}】{title}")


def _kv(key: str, val: str) -> None:
    print(f"  {key:<18} {val}")


def main() -> None:
    _box("跨平台 Contacts 业务流 DEMO")

    # 准备临时配置目录（复制 yaml）
    tmp = Path(tempfile.mkdtemp(prefix="contacts_demo_"))
    (tmp / "handoff_scripts.yaml").write_bytes(
        (REPO_ROOT / "config" / "handoff_scripts.yaml").read_bytes())
    (tmp / "handoff_compliance.yaml").write_bytes(
        (REPO_ROOT / "config" / "handoff_compliance.yaml").read_bytes())

    cfg = {
        "contacts": {
            "enabled": True,
            "daily_cap": 3,
            "token_ttl_hours": 72,
            "readiness_threshold": 70,
            "line_ids_by_account": {"acc-A": "@my_line_acc_A"},
        }
    }

    sub = bootstrap_contacts_subsystem(cfg, tmp)
    assert sub is not None, "bootstrap failed"
    gw = sub.gateway

    try:
        _run_demo(sub)
    finally:
        sub.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _run_demo(sub) -> None:
    gw = sub.gateway
    store = sub.store

    # ── Step 1. 首次看到用户 ──
    _step(1, "Alice 在 Messenger 上首次出现")
    ctx = gw.on_peer_seen(
        channel=CHANNEL_MESSENGER, account_id="acc-A", external_id="fb_alice_1001",
        display_name="Alice", language_hint="zh", timezone_hint="Asia/Shanghai",
    )
    _kv("Contact ID", ctx.contact.contact_id[:16] + "…")
    _kv("Journey 阶段", ctx.journey.funnel_stage)
    _kv("is_new", str(ctx.is_new))

    # 同步元数据
    store.update_contact(ctx.contact.contact_id, primary_name="Alice",
                          language_hint="zh", timezone_hint="Asia/Shanghai")

    # ── Step 2. 模拟 5 天对话（直接伪造 events 加速演示）──
    _step(2, "模拟 5 天对话（每天 4 条来回 = 40 条消息）")
    jid = ctx.journey.journey_id
    now = int(time.time())
    with store._lock:
        for d in range(5):
            for i in range(4):
                for et in ("msg_in", "msg_out"):
                    store._conn.execute(
                        "INSERT INTO journey_events (event_id, journey_id, trace_id, event_type, payload_json, ts) "
                        "VALUES (?, ?, '', ?, '{}', ?)",
                        (uuid.uuid4().hex, jid, et, now - d * 86400 - i * 60),
                    )
        store._conn.execute(
            "UPDATE journeys SET funnel_stage='ENGAGED', updated_at=? WHERE journey_id=?",
            (now, jid))
        store._conn.commit()

    # 刷 intimacy
    bd = sub.intimacy_engine.refresh_journey_intimacy(jid)
    _kv("Intimacy 分", f"{bd.score:.1f} / 100")
    _kv("贡献细节", str(bd.contributions))
    _kv("近 7 天活跃", f"{bd.active_days_7d} 天")
    _kv("双向对称度", f"{bd.turn_count_in} 收 / {bd.turn_count_out} 发")

    # ── Step 3. 没 goodbye → Readiness 不开窗 ──
    _step(3, "Alice 只是日常聊天——Readiness 不开窗")
    attempt = gw.maybe_issue_handoff(
        messenger_ci_id=ctx.channel_identity.channel_identity_id,
        latest_in_text="你今天吃了啥呀",
    )
    _kv("成功？", str(attempt.success))
    _kv("原因", attempt.reason)
    _kv("Readiness 分", f"{attempt.readiness_score:.1f}")

    # ── Step 3.5. dry_run 预览——UI 可以先拿给人工看一眼 ──
    _step("3.5", "dry_run 预览：Alice 说晚安，但我们只想 UI 预览（无副作用）")
    preview = gw.maybe_issue_handoff(
        messenger_ci_id=ctx.channel_identity.channel_identity_id,
        latest_in_text="我去睡啦 晚安～",
        dry_run=True,
    )
    _kv("dry_run token", preview.token)
    _kv("预览文案", preview.text)
    _kv("剩余配额（未扣）", f"{preview.remaining_today} / 3")
    _kv("Stage（未推）", store.get_journey(jid).funnel_stage)
    assert preview.token == "dry_rn" and preview.success, "dry_run 预览应成功且不动副作用"

    # ── Step 4. Alice 说晚安 → Readiness 开窗 ──
    _step(4, "Alice 说晚安——Readiness 开窗，系统决定引流")
    attempt = gw.maybe_issue_handoff(
        messenger_ci_id=ctx.channel_identity.channel_identity_id,
        latest_in_text="我去睡啦 晚安～",
    )
    _kv("成功？", "✅ 是" if attempt.success else "❌ 否")
    _kv("选中话术", attempt.script_id)
    _kv("token", attempt.token)
    _kv("渲染文本", attempt.text)
    _kv("剩余配额", f"{attempt.remaining_today} / 3 今日")
    _kv("readiness", f"{attempt.readiness_score:.1f}")
    assert attempt.success, "demo: expected success at step 4"
    token = attempt.token

    # 模拟"runner 已经真的把话术发出去了"
    gw.on_handoff_sent(
        messenger_ci_id=ctx.channel_identity.channel_identity_id, token=token,
    )
    _kv("Journey 推进到", store.get_journey(jid).funnel_stage)

    # ── Step 5. Alice 加了 LINE 发首条 ──
    _step(5, "Alice 在 LINE 加你了，发首条带 token")
    outcome = gw.on_line_first_text(
        account_id="acc-A", external_id="line_alice_xy",
        text=f"Hi 我加你了哈 {token}",
        display_name="Alice", language_hint="zh",
    )
    _kv("合并成功？", "✅ 是" if outcome.merged else "❌ 否")
    _kv("via", outcome.via)
    _kv("confidence", f"{outcome.confidence:.2f}")
    _kv("合并后 Contact", outcome.contact_id[:16] + "…")

    # ── Step 6. 最终状态 ──
    _step(6, "合并后的 Journey 与 Contact")
    j = store.get_journey_by_contact(outcome.contact_id)
    _kv("Funnel 阶段", j.funnel_stage)
    _kv("Intimacy", f"{j.intimacy_score:.1f}")
    cis = store.list_channel_identities_of(outcome.contact_id)
    _kv("跨平台身份", str([f"{c.channel}:{c.external_id}" for c in cis]))

    # ── Step 7. Timeline ──
    _step(7, "Journey Timeline（最近 15 条事件）")
    events = store.list_events(j.journey_id, limit=15)
    for e in reversed(events):
        ts = time.strftime("%m-%d %H:%M:%S", time.localtime(e["ts"]))
        payload = {k: v for k, v in (e["payload"] or {}).items() if k != "preview"}
        print(f"  {ts}  {e['event_type']:<28}  {payload}")

    # ── Step 8. Funnel 统计 ──
    _step(8, "Funnel 统计")
    _kv("总 Contact 数", str(store.count_contacts()))
    _kv("by_stage", str(store.count_journeys_by_stage()))
    _kv("by_channel", str(store.count_channel_identities_by_channel()))

    # ── Step 9. Cap 用量 ──
    _step(9, "账号配额用量（cap=3/天）")
    counts = sub.limiter.get_counts("acc-A")
    _kv("今日已用", f"{counts['account_count']} / {counts['daily_cap']}")
    _kv("今日剩余", str(counts["account_remaining"]))

    _box("✅ 业务流演示完成")
    print("\n  结论：")
    print("   - Messenger 聊 5 天 → intimacy 70+")
    print("   - 告别场景触发 Readiness → 签 token + 渲染话术")
    print("   - LINE 加过来一说 token → 跨平台 Contact 合并成功")
    print("   - 全链路 9 步，无任何外部依赖；单机可跑。")


if __name__ == "__main__":
    main()
