"""一键查全况 — 守夜测试结果摘要

用法: python tools/quick_status.py
"""
import sqlite3
import time
import sys
import re
from pathlib import Path
from collections import Counter, defaultdict


def hr(title=""):
    print("\n" + "=" * 70)
    if title:
        print(title)
        print("-" * 70)


def main():
    repo = Path(__file__).resolve().parent.parent
    db = repo / "config" / "bot.db"
    log = repo / "logs" / "app.log"
    state_db = repo / "data" / "messenger_rpa_state.db"
    portrait_db = repo / "data" / "contacts.db"

    # ── 1) Episodic memory 累积 ──
    hr("1) 长期记忆 (episodic_memory)")
    if db.exists():
        c = sqlite3.connect(str(db))
        n = c.execute("SELECT COUNT(*) FROM episodic_memory").fetchone()[0]
        rec = c.execute(
            "SELECT COUNT(*) FROM episodic_memory "
            "WHERE created_at > strftime('%s','now')-86400"
        ).fetchone()[0]
        rec24 = c.execute(
            "SELECT COUNT(*) FROM episodic_memory WHERE user_id LIKE '%vwnj%' "
            "AND created_at > strftime('%s','now')-86400"
        ).fetchone()[0]
        print(f"  总条数: {n}   过去 24h 新增: {rec}   acc_vwnj_test 新增: {rec24}")
        print("  最近 8 条:")
        for r in c.execute(
            "SELECT datetime(created_at,'unixepoch','+8 hours') ts, category, user_id, content "
            "FROM episodic_memory ORDER BY created_at DESC LIMIT 8"
        ):
            u_short = r[2][:40] if len(r[2]) > 40 else r[2]
            ct_short = (r[3] or "")[:60]
            print(f"    {r[0]}  {r[1]:<10}  {u_short}  {ct_short}")
        c.close()

    # ── 2) 最近 cycle 状态分布 ──
    hr("2) 最近 200 cycle step 分布")
    if log.exists():
        steps = Counter()
        ms_dist = []
        with open(log, "rb") as f:
            f.seek(max(0, log.stat().st_size - 200_000))
            data = f.read().decode("utf-8", errors="replace")
        for m in re.finditer(r"step=(\w+).*?ms=(\d+)", data):
            steps[m.group(1)] += 1
            ms_dist.append(int(m.group(2)))
        for s, c in steps.most_common():
            print(f"  {s:30s}  {c:5d}")
        if ms_dist:
            avg = sum(ms_dist) / len(ms_dist)
            ms_dist.sort()
            print(f"\n  cycle ms: avg={avg:.0f}  median={ms_dist[len(ms_dist)//2]}  p90={ms_dist[int(len(ms_dist)*0.9)]}")

    # ── 3) reply_decided 累积 + hint 分布 ──
    hr("3) Reply 累积 + hint 分布")
    if log.exists():
        replies = []
        hint_counter = Counter()
        with open(log, "rb") as f:
            f.seek(max(0, log.stat().st_size - 500_000))
            data = f.read().decode("utf-8", errors="replace")
        for line in data.split("\n"):
            if "reply decided" in line and "hints=" in line:
                replies.append(line)
                # 抽 hints
                m = re.search(r"hints=([^\n]+)", line)
                if m:
                    for h in m.group(1).split(","):
                        base = h.strip().split(":", 1)[0]
                        if base:
                            hint_counter[base] += 1
        print(f"  最近 reply_decided: {len(replies)}")
        if replies:
            print(f"  最后一条: {replies[-1][:200]}")
        print("\n  Top hints:")
        for h, c in hint_counter.most_common(20):
            print(f"    {c:5d}  {h}")

    # ── 4) Episodic 链路诊断日志 ──
    hr("4) Episodic 链路日志")
    if log.exists():
        with open(log, "rb") as f:
            f.seek(max(0, log.stat().st_size - 200_000))
            data = f.read().decode("utf-8", errors="replace")
        ep_lines = [l for l in data.split("\n") if "[episodic]" in l]
        print(f"  最近 episodic log 条数: {len(ep_lines)}")
        for l in ep_lines[-10:]:
            print(f"    {l[-200:]}")

    # ── 5) Voice 相关日志 ──
    hr("5) Voice/Audio 相关")
    if log.exists():
        with open(log, "rb") as f:
            f.seek(max(0, log.stat().st_size - 200_000))
            data = f.read().decode("utf-8", errors="replace")
        for kw in ["voice_grabber", "voice_input", "voice_output", "transcribe",
                   "TTS", "send_voice", "kind=voice", "peer_kind=voice"]:
            cnt = data.count(kw)
            if cnt:
                print(f"  {kw}: {cnt}")

    # ── 6) 媒体相关（image/sticker） ──
    hr("6) 媒体处理")
    if log.exists():
        with open(log, "rb") as f:
            f.seek(max(0, log.stat().st_size - 200_000))
            data = f.read().decode("utf-8", errors="replace")
        for kw in ["media ack", "sticker_category", "image_caption",
                   "kind=image", "kind=sticker", "media_handling"]:
            cnt = data.count(kw)
            if cnt:
                print(f"  {kw}: {cnt}")

    # ── 7) 异常: send_failed, wrong_chat, camera ──
    hr("7) 异常事件")
    if log.exists():
        with open(log, "rb") as f:
            f.seek(max(0, log.stat().st_size - 500_000))
            data = f.read().decode("utf-8", errors="replace")
        for kw in ["send_failed", "wrong_chat_rollback", "camera",
                   "device_unhealthy", "Traceback", "ALERT"]:
            cnt = data.count(kw)
            if cnt:
                print(f"  {kw}: {cnt}")

    # ── 8) Portrait/contacts ──
    hr("8) 用户画像 (portrait)")
    if portrait_db.exists():
        c = sqlite3.connect(str(portrait_db))
        try:
            n = c.execute("SELECT COUNT(*) FROM portrait_snapshot").fetchone()[0]
            print(f"  portrait_snapshot 总数: {n}")
            for r in c.execute(
                "SELECT contact_id, datetime(created_at, 'unixepoch', '+8 hours'), "
                "language, tone FROM portrait_snapshot ORDER BY created_at DESC LIMIT 5"
            ):
                print(f"    {r[1]}  {r[0][:30]}  lang={r[2]} tone={r[3]}")
        except Exception as e:
            print(f"  (portrait DB schema 不一致: {e})")
        c.close()
    else:
        print("  contacts.db 不存在")

    # ── 9) Metrics ──
    hr("9) messenger_rpa metrics（累计 hint）")
    try:
        from src.monitoring.metrics_store import get_metrics_store
        ms = get_metrics_store().get_messenger_rpa_metrics()
        if not ms:
            print("  (空 — 进程刚启动 / 无 reply 触发)")
        else:
            for k, v in sorted(ms.items(), key=lambda kv: -kv[1])[:20]:
                print(f"  {v:5d}  {k}")
    except Exception as e:
        print(f"  metrics 加载失败: {e}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    main()
