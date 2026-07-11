"""全自动发送「安全视图」CLI —— 对真号运营的只读体检，零副作用、零服务依赖。

直接读活库（account_registry.db / account_sends.db / inbox.db，全部只读）复用
``src.inbox.send_health.compute_send_health`` 的同一 core，打印每号一行：
今日发量/占 cap、投递成功/失败（含失败归因）、健康灯、回复率、综合判词。

用法：
    python -m scripts.send_health_report            # 表格
    python -m scripts.send_health_report --json      # 机读
    python -m scripts.send_health_report --hours 48  # 自定义窗口

安全：全部 ``mode=ro`` 只读连接，不与运行中的 main.py 争写；WAL 下并发读安全。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.inbox.send_health import (  # noqa: E402
    compute_send_health, is_sender_account, reply_stats,
)
from src.skills.account_signals import build_account_signals  # noqa: E402


def _load_merged_config() -> dict:
    import yaml
    def load(p):
        fp = _ROOT / p
        if not fp.exists():
            return {}
        with open(fp, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    def deep(a, b):
        for k, v in (b or {}).items():
            a[k] = deep(a.get(k, {}), v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
        return a
    return deep(load("config/config.yaml"), load("config/config.local.yaml"))


def _ro_conn(rel_path: str):
    """只读 sqlite 连接（URI mode=ro）；库不存在返回 None。"""
    fp = _ROOT / rel_path
    if not fp.exists():
        return None
    return sqlite3.connect(f"file:{fp.as_posix()}?mode=ro", uri=True, timeout=10)


class _RoRegistry:
    """只读账号注册表（复用活库 account_registry.db）。"""
    def __init__(self, conn):
        self._c = conn
    def list(self):
        if self._c is None:
            return []
        self._c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in self._c.execute("SELECT * FROM platform_accounts")]
        except Exception:
            return []
    def get(self, platform, account_id):
        if self._c is None:
            return {}
        self._c.row_factory = sqlite3.Row
        try:
            r = self._c.execute(
                "SELECT * FROM platform_accounts WHERE platform=? AND account_id=?",
                (platform, account_id)).fetchone()
            if not r:
                return {}
            d = dict(r)
            import json as _j
            try:
                d["meta"] = _j.loads(d.get("meta_json") or "{}")
            except Exception:
                d["meta"] = {}
            return d
        except Exception:
            return {}


class _RoLimiter:
    """只读发送计数（复用活库 account_sends.db）：只实现 snapshot 的 day_used。"""
    def __init__(self, conn):
        self._c = conn
    def snapshot(self, account_key, now=None):
        now = now if now is not None else time.time()
        day = hour = 0
        if self._c is not None:
            try:
                day = int(self._c.execute(
                    "SELECT COUNT(*) FROM account_sends WHERE account_key=? AND ts>=?",
                    (account_key, now - 86400.0)).fetchone()[0] or 0)
            except Exception:
                day = 0
        return {"day_used": day, "hour_used": hour, "circuit_open": False}


def _audit_24h(inbox_conn, since):
    if inbox_conn is None:
        return []
    inbox_conn.row_factory = sqlite3.Row
    try:
        rows = inbox_conn.execute(
            "SELECT action, conversation_id, reason FROM draft_audit_log "
            "WHERE ts>=? AND action IN ('autosend','autosend_failed')",
            (float(since),)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _make_inbound_exists(inbox_conn):
    def _fn(conversation_id, since_ts):
        if inbox_conn is None:
            return False
        try:
            r = inbox_conn.execute(
                "SELECT 1 FROM messages WHERE conversation_id=? AND direction='in' "
                "AND ts>=? LIMIT 1", (conversation_id, float(since_ts))).fetchone()
            return r is not None
        except Exception:
            return False
    return _fn


def build_report(hours: float = 24.0, with_reply: bool = True) -> dict:
    now = time.time()
    since = now - max(1.0, hours) * 3600.0
    cfg = _load_merged_config()
    reg = _RoRegistry(_ro_conn("config/account_registry.db"))
    lim = _RoLimiter(_ro_conn("config/account_sends.db"))
    inbox = _ro_conn("config/inbox.db")

    accounts = []
    for r in reg.list():
        plat, acc = r.get("platform"), r.get("account_id")
        if not acc or not is_sender_account(r.get("status", "")):
            continue
        sig = build_account_signals(plat, acc, registry=reg, limiter=lim, now=now)
        sig["platform"] = str(plat or "").lower()
        sig["status"] = r.get("status", "")
        accounts.append(sig)

    audit = _audit_24h(inbox, since)
    reply_by = None
    if with_reply and inbox is not None:
        try:
            reply_by = reply_stats(audit, since, _make_inbound_exists(inbox))
        except Exception:
            reply_by = None

    return compute_send_health(accounts=accounts, audit_24h=audit,
                               config=cfg, reply_by_account=reply_by, now=now)


_LIGHT_ICON = {"green": "🟢", "amber": "🟡", "red": "🔴"}
_LEVEL_ICON = {"ok": "✅", "watch": "⚠️ ", "risk": "🛑"}


def _print_table(rep: dict) -> None:
    s = rep["summary"]
    print(f"\n全自动发送安全视图  队列级别={rep['fleet_level'].upper()}  "
          f"（近24h：{s['sends_today']} 发 / {s['delivered_24h']} 达 / {s['failed_24h']} 败）")
    print("=" * 96)
    hdr = f"{'级别':<5} {'账号':<28} {'灯':<3} {'今日/上限':<11} {'达/败':<9} {'失败归因':<20} {'回复率':<8}"
    print(hdr)
    print("-" * 96)
    for a in rep["accounts"]:
        acct = f"{a['platform']}:{a['account_id']}"
        if len(acct) > 27:
            acct = acct[:26] + "…"
        cap_pct = f"{int(a['cap_pct']*100)}%" if a.get("cap_pct") is not None else "-"
        cap_cell = f"{a['sends_today']}/{a['recommended_cap']} {cap_pct}"
        da_cell = f"{a['delivered_24h']}/{a['failed_24h']}"
        fc = a["fail_by_cat"]
        fail_cell = ""
        if a["failed_24h"] > 0:
            parts = []
            if fc["gate"]:
                parts.append(f"闸{fc['gate']}")
            if fc["platform"]:
                parts.append(f"台{fc['platform']}")
            if fc["permanent"]:
                parts.append(f"永{fc['permanent']}")
            if fc["other"]:
                parts.append(f"他{fc['other']}")
            fail_cell = ",".join(parts)
        rep_cell = "-"
        if a.get("reply"):
            rep_cell = f"{int(a['reply']['reply_rate']*100)}% ({a['reply']['replied_convs']}/{a['reply']['autosent_convs']})"
        print(f"{_LEVEL_ICON.get(a['level'],'  '):<4} {acct:<28} "
              f"{_LIGHT_ICON.get(a['light'],'?'):<2} {cap_cell:<11} {da_cell:<9} "
              f"{fail_cell:<20} {rep_cell:<8}")
        if a["level"] != "ok":
            print(f"      └ {a['reason']}")
    print("-" * 96)
    print("失败归因：闸=反封号闸门/急停(预期节流,非故障)  台=平台报错(真故障,查通道)  "
          "永=会话失效/被拉黑  他=其它")
    print("提示：闸门拦截是「安全在起作用」，平台报错才需排查。回复率低+发量高 = 可能在骚扰，宜降 cap。\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="全自动发送安全视图（只读）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--hours", type=float, default=24.0, help="统计窗口小时数（默认24）")
    ap.add_argument("--no-reply", action="store_true", help="跳过回复率计算（更快）")
    args = ap.parse_args()
    rep = build_report(hours=args.hours, with_reply=not args.no_reply)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        _print_table(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
