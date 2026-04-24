#!/usr/bin/env python3
"""Messenger RPA 回放包检查工具（P3-7）。

用法：
    python scripts/replay_inspect.py <zip_path>
    python scripts/replay_inspect.py --list            # 列最近 20 个包
    python scripts/replay_inspect.py --list 50         # 列最近 50 个包

打印 zip 里 run_result.json 的关键字段 summary，便于开发者快速排障。
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path


def _print_summary(zip_path: Path) -> None:
    if not zip_path.exists():
        print(f"not found: {zip_path}")
        return
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            print(f"=== {zip_path.name} ===")
            print(f"files: {len(names)}")
            for n in names:
                info = zf.getinfo(n)
                print(f"  - {n:40s} {info.file_size} B")
            if "run_result.json" in names:
                with zf.open("run_result.json") as f:
                    r = json.loads(f.read().decode("utf-8"))
                print("\n-- run_result summary --")
                for k in (
                    "ts", "run_id", "step", "error", "chat_key", "chat_name",
                    "peer_kind", "peer_text", "reply_text", "total_ms",
                    "reader_path", "inbox_vision_tag", "thread_vision_tag",
                    "caption_source", "image_caption", "phase_ms",
                    "send_counters", "risk",
                ):
                    if k in r:
                        v = r[k]
                        if isinstance(v, str) and len(v) > 120:
                            v = v[:120] + "…"
                        print(f"  {k:22s} = {v!r}")
                gh = r.get("guard_history") or []
                if gh:
                    print(f"  guard_history: {len(gh)} entries")
                    for i, g in enumerate(gh):
                        print(f"    [{i}] {g}")
            if "meta.json" in names:
                with zf.open("meta.json") as f:
                    m = json.loads(f.read().decode("utf-8"))
                print("\n-- meta --")
                for k in ("ts", "error_class", "error", "step"):
                    if k in m:
                        print(f"  {k:14s} = {m[k]!r}")
    except Exception as ex:
        print(f"error reading {zip_path}: {ex}")


def _list_latest(limit: int = 20) -> None:
    """列出 tmp_messenger_rpa/replays 下最新 N 个包。"""
    base = Path("tmp_messenger_rpa/replays").resolve()
    if not base.exists():
        print(f"no replays dir at {base}")
        return
    items = sorted(
        base.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True,
    )[:limit]
    if not items:
        print(f"no replays in {base}")
        return
    import time as _t
    for z in items:
        ts = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(z.stat().st_mtime))
        print(f"{ts}  {z.stat().st_size:>8} B  {z.name}")


def _rerun_via_api(zip_name: str, api_base: str, token: str) -> None:
    """通过主进程 API 重跑 LLM 并打印对比。需要 main.py 在跑。"""
    import urllib.error
    import urllib.request
    url = api_base.rstrip("/") + "/api/messenger-rpa/replays/rerun"
    body = json.dumps({"zip": zip_name}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')[:500]}")
        return
    except Exception as e:
        print(f"rerun 请求失败: {e}")
        return
    print(f"=== rerun {zip_name} ===")
    print(f"chat_key      : {data.get('chat_key')}")
    print(f"peer_kind     : {data.get('peer_kind')}")
    print(f"peer_text     : {data.get('peer_text')}")
    print(f"text_for_ai   : {data.get('text_for_ai')}")
    print("--- OLD reply ---")
    print(data.get("old_reply") or "(none)")
    print("--- NEW reply ---")
    print(data.get("new_reply") or "(none)")
    print(f"\ndiff_hint     : {data.get('diff_hint')}")
    print(f"elapsed_ms    : {data.get('elapsed_ms')}")


def main(argv: list) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[1] == "--list":
        limit = int(argv[2]) if len(argv) > 2 else 20
        _list_latest(limit)
        return 0
    if argv[1] == "--rerun":
        if len(argv) < 3:
            print("usage: --rerun <zip> [--api URL] [--token T]")
            return 1
        zip_name = argv[2]
        api_base = "http://127.0.0.1:18787"
        token = "admin"
        i = 3
        while i < len(argv):
            if argv[i] == "--api" and i + 1 < len(argv):
                api_base = argv[i + 1]; i += 2
            elif argv[i] == "--token" and i + 1 < len(argv):
                token = argv[i + 1]; i += 2
            else:
                i += 1
        _rerun_via_api(zip_name, api_base, token)
        return 0
    _print_summary(Path(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
