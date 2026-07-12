"""一键健康报告 — 把「观察一周」变成「每天一条命令」（2026-07 /thread 性能重构收尾）。

聚合四类信号，读数即可判断本轮修复是否稳住、backfill 是否消化完：

1. **实例健康**：main.py 进程数（>1 = 多实例踩踏，当天曾出现 3 个并发）+ 18799 就绪探测；
2. **/thread 延迟采样**：对最近活跃的 N 个会话实测（回归「加载超时」的第一信号）;
3. **入站翻译**：存量未译候选（应趋零）+ 按日漏斗（translated/noop/deferred/failed 近 7 天）;
4. **重启频率**：logs/restart_*/boot_* 按日计数（纪律执行情况，目标 ≤3 次/日）。

用法：
    python -m scripts.health_report            # 人读表格
    python -m scripts.health_report --json     # 机读（接告警/趋势）
默认只读 DB 与日志、对本机 18799 发少量 GET，无副作用。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ai.translation_service import detect_language  # noqa: E402

DB = ROOT / "config" / "inbox.db"
LOGS = ROOT / "logs"
BASE = "http://127.0.0.1:18799"


def _instances() -> dict:
    """main.py 进程数 + web 就绪探测（Windows：经 wmic 替代品 powershell 太重，读端口即可）。"""
    out = {"web_ready": False, "probe_ms": None}
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(f"{BASE}/login", timeout=5) as r:
            out["web_ready"] = (r.status == 200)
        out["probe_ms"] = int((time.monotonic() - t0) * 1000)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    try:
        import subprocess
        # @(...) 强制数组：PS 5.1 下单个对象无 .Count（会输出空串误报 0 实例）
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match 'main\\.py' }).Count"],
            capture_output=True, text=True, timeout=20)
        out["main_py_instances"] = int((r.stdout or "0").strip() or 0)
    except Exception:
        out["main_py_instances"] = -1  # 探测失败（不阻断报告）
    return out


def _thread_latency(token: str, n: int = 5) -> list:
    """对最近活跃 n 个会话实测 /thread 延迟（需 Bearer token；拿不到 token 则跳过）。"""
    if not token:
        return []
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    convs = conn.execute(
        "SELECT platform, account_id, chat_key FROM conversations "
        "WHERE platform != 'web' ORDER BY last_ts DESC LIMIT ?", (n,)).fetchall()
    conn.close()
    out = []
    for c in convs:
        url = (f"{BASE}/api/unified-inbox/thread?platform={c['platform']}"
               f"&account_id={c['account_id']}&chat_key={c['chat_key']}&limit=100")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                ok = (r.status == 200)
            out.append({"conv": f"{c['platform']}:{c['chat_key']}",
                        "ms": int((time.monotonic() - t0) * 1000), "ok": ok})
        except Exception as exc:
            out.append({"conv": f"{c['platform']}:{c['chat_key']}",
                        "ms": int((time.monotonic() - t0) * 1000),
                        "ok": False, "error": type(exc).__name__})
    return out


def _xlate_stock(top_n: int = 20) -> dict:
    """top_n 活跃会话的未译存量（与 backfill 候选同判定；应随 backfill 趋零）。"""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    convs = conn.execute(
        "SELECT conversation_id FROM conversations ORDER BY last_ts DESC LIMIT ?",
        (top_n,)).fetchall()
    total = 0
    by_conv = {}
    for c in convs:
        cid = c["conversation_id"]
        rows = conn.execute(
            "SELECT source_lang, text, translated_text, target_lang FROM messages "
            "WHERE conversation_id=? AND direction='in' ORDER BY ts DESC LIMIT 100",
            (cid,)).fetchall()
        n = 0
        for r in rows:
            text = (r["text"] or "").strip()
            if not text or len(text) > 400:
                continue
            if (r["translated_text"] or "") and r["target_lang"] == "zh":
                continue  # 已处理（含 noop 标记）
            lang = r["source_lang"] or "unknown"
            if not lang or lang == "unknown":
                lang = detect_language(text)
            if lang == "zh":
                continue
            n += 1
        if n:
            by_conv[cid] = n
            total += n
    # 按日漏斗近 7 天
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
    daily = [dict(r) for r in conn.execute(
        "SELECT day, translated, failed, noop, deferred FROM inbound_xlate_daily "
        "WHERE day >= ? ORDER BY day", (since,)).fetchall()]
    conn.close()
    return {"untranslated_stock": total, "by_conv": by_conv, "daily_7d": daily}


def _restart_counts(days: int = 7) -> dict:
    """按日统计 restart_*/boot_* 日志（重启纪律执行情况，目标 ≤3 次/日）。"""
    pat = re.compile(r"^(?:restart|boot)_(\d{8})_\d{6}\.out\.log$")
    counter: Counter = Counter()
    cutoff = time.strftime("%Y%m%d", time.localtime(time.time() - days * 86400))
    for f in LOGS.glob("*.out.log"):
        m = pat.match(f.name)
        if m and m.group(1) >= cutoff:
            counter[m.group(1)] += 1
    return dict(sorted(counter.items()))


def _unclean_deaths_today() -> int:
    """今日「非正常死亡」哨兵告警行数（exit_sentinel 启动检测写入 app.log）。"""
    log = LOGS / "app.log"
    if not log.exists():
        return 0
    today = time.strftime("%Y-%m-%d")
    n = 0
    try:
        with log.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith(f"[{today}") and "非正常死亡" in line:
                    n += 1
    except OSError:
        pass
    return n


def _detect_alerts(report: dict) -> list:
    """异常判定（观察期的「主动上报」规则，供 --alert 弹窗/留痕）。"""
    alerts = []
    inst = report["instances"]
    n_inst = inst.get("main_py_instances")
    if n_inst != 1:
        alerts.append(f"main.py 实例数异常: {n_inst}（0=服务死了，>1=多实例踩踏）")
    if not inst.get("web_ready"):
        alerts.append("web 后台(18799)不可达")
    lat = report["thread_latency"]
    fails = [x["conv"] for x in lat if not x.get("ok")]
    if fails:
        alerts.append(f"/thread 采样失败 {len(fails)}: {fails}")
    worst = max((x["ms"] for x in lat if x.get("ok")), default=0)
    if worst > 5000:
        alerts.append(f"/thread 最慢 {worst}ms（回归「加载超时」前兆）")
    rt = report["restarts_by_day"].get(time.strftime("%Y%m%d"), 0)
    if rt > 3:
        alerts.append(f"今日重启 {rt} 次，超纪律红线(3)")
    deaths = report.get("unclean_deaths_today", 0)
    if deaths:
        alerts.append(f"今日非正常死亡 {deaths} 次（哨兵检出，查 app.log『非正常死亡』行）")
    return alerts


def _emit_alerts(alerts: list) -> None:
    """告警出口：host_alert（弹窗+EventBus 镜像+去抖）+ 专用留痕文件。

    health_report 是独立进程——host_alert 的 logger 无 file handler（进不了
    app.log），故自留 logs/health_alerts.log 一行（计划任务场景无人看 stdout）。
    """
    msg = "\n".join(f"- {a}" for a in alerts)
    try:
        from src.utils.host_alert import notify_host
        notify_host("生产健康告警（health_report）", msg,
                    key="health_report", cooldown_sec=300)
    except Exception:
        pass
    try:
        with (LOGS / "health_alerts.log").open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n{msg}\n")
    except OSError:
        pass


def _hot_reload_counts(days: int = 7) -> dict:
    """按日统计 app.log 里的热重载事件（config / i18n 免重启通道的使用证据）。

    依赖 src.* INFO 落盘修复（2026-07-12）——修复前这些行进不了 app.log。
    只扫当前 app.log（轮转旧档不追溯，日粒度趋势足够）。
    """
    pats = {
        "config": re.compile(r"^\[(\d{4}-\d{2}-\d{2}) .*配置热重载完成"),
        "i18n": re.compile(r"^\[(\d{4}-\d{2}-\d{2}) .*web_i18n 热重载完成"),
    }
    out: dict = {"config": Counter(), "i18n": Counter()}
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    log = LOGS / "app.log"
    if not log.exists():
        return {k: {} for k in out}
    try:
        with log.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                for kind, pat in pats.items():
                    m = pat.match(line)
                    if m and m.group(1) >= cutoff:
                        out[kind][m.group(1)] += 1
                        break
    except OSError:
        pass
    return {k: dict(sorted(v.items())) for k, v in out.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--jsonl", default="",
                    help="单行 JSON 追加到指定文件（计划任务每日跟踪用），成功时静默")
    ap.add_argument("--alert", action="store_true",
                    help="异常时主动告警（host_alert 弹窗 + logs/health_alerts.log 留痕）")
    ap.add_argument("--token", default="", help="web_admin.auth_token（不传则跳过延迟采样）")
    args = ap.parse_args()

    token = args.token
    if not token:
        try:
            import yaml
            cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
            token = str(((cfg.get("web_admin") or {}).get("auth_token")) or "")
        except Exception:
            token = ""

    report = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "instances": _instances(),
        "thread_latency": _thread_latency(token),
        "inbound_xlate": _xlate_stock(),
        "restarts_by_day": _restart_counts(),
        "hot_reloads_by_day": _hot_reload_counts(),
        "unclean_deaths_today": _unclean_deaths_today(),
    }
    alerts = _detect_alerts(report)
    report["alerts"] = alerts
    if args.alert and alerts:
        _emit_alerts(alerts)

    if args.jsonl:
        # 精简行（趋势用）：不携带 by_conv/逐会话明细，只留可画线的聚合数
        lat = report["thread_latency"]
        _today = time.strftime("%Y-%m-%d")
        _hr = report["hot_reloads_by_day"]
        row = {
            "ts": report["ts"],
            "instances": report["instances"].get("main_py_instances"),
            "web_ready": report["instances"].get("web_ready"),
            "thread_worst_ms": max((x["ms"] for x in lat), default=None),
            "thread_fail": sum(1 for x in lat if not x.get("ok")),
            "xlate_stock": report["inbound_xlate"]["untranslated_stock"],
            "restarts_today": report["restarts_by_day"].get(
                time.strftime("%Y%m%d"), 0),
            "hot_reloads_today": (_hr.get("config", {}).get(_today, 0)
                                  + _hr.get("i18n", {}).get(_today, 0)),
            "unclean_deaths_today": report["unclean_deaths_today"],
            "alerts": len(report["alerts"]),
        }
        out = Path(args.jsonl)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return 0

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    inst = report["instances"]
    print(f"== 健康报告 {report['ts']} ==")
    print(f"[实例] main.py x{inst.get('main_py_instances')} · web {'✅' if inst.get('web_ready') else '❌'}"
          f" ({inst.get('probe_ms')}ms)" + (f" · {inst.get('error')}" if inst.get("error") else ""))
    lat = report["thread_latency"]
    if lat:
        worst = max(x["ms"] for x in lat)
        bad = [x for x in lat if not x["ok"]]
        print(f"[/thread] 采样 {len(lat)} 会话 · 最慢 {worst}ms"
              + (f" · 失败 {len(bad)}: {[x['conv'] for x in bad]}" if bad else " · 全部 OK"))
        for x in lat:
            print(f"    {x['conv']:44s} {x['ms']:>6}ms {'OK' if x['ok'] else 'FAIL ' + x.get('error', '')}")
    ix = report["inbound_xlate"]
    print(f"[入站翻译] top20 活跃会话未译存量: {ix['untranslated_stock']}"
          + (f"（{ix['by_conv']}）" if ix["by_conv"] else "（已清空 ✅）"))
    for d in ix["daily_7d"]:
        print(f"    {d['day']}: 译出 {d['translated']} · noop {d['noop']} · 转后台 {d['deferred']} · 失败 {d['failed']}")
    rc = report["restarts_by_day"]
    flagged = {k: v for k, v in rc.items() if v > 3}
    print(f"[重启频率] {rc}" + (f" ⚠ 超纪律（>3/日）: {flagged}" if flagged else " ✅"))
    hr = report["hot_reloads_by_day"]
    print(f"[免重启通道] config 热重载 {hr.get('config') or '{}'} · i18n 热加载 {hr.get('i18n') or '{}'}")
    if report["unclean_deaths_today"]:
        print(f"[退出哨兵] ⚠ 今日非正常死亡 {report['unclean_deaths_today']} 次（详见 app.log『非正常死亡』行）")
    if alerts:
        print("[告警] " + " | ".join(alerts))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
