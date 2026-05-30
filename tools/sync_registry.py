#!/usr/bin/env python3
"""Registry DB 同步工具 — 无 NFS 环境的主从同步。

用法:
    # 从主控机导出 registry（在主控机上运行）:
    python tools/sync_registry.py export --out registry_export.json

    # 从 JSON 导入到本机 registry（在从机上运行）:
    python tools/sync_registry.py import --file registry_export.json

    # 从远程主控 API 拉取并合并（在从机上运行）:
    python tools/sync_registry.py pull --url http://192.168.8.100:18787/api/registry/export

    # 定时同步脚本（cron / Task Scheduler）:
    python tools/sync_registry.py pull --url http://192.168.8.100:18787/api/registry/export --loop 30

设计:
    - export: 导出全量 registry 为 JSON
    - import: 从 JSON 合并到本机 DB（upsert 策略，不删除本地额外条目）
    - pull: 通过 HTTP 从主控拉取 export → 本地 import
    - 支持 --loop N 模式（每 N 秒循环拉取）
    - 合并策略: 以远程数据为准更新已有设备，不删除本地独有设备
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# 确保项目根目录在 path 中
_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))


def cmd_export(args):
    from src.shared.device_registry import get_device_registry

    reg = get_device_registry(args.db or "")
    devices = reg.all()
    data = {"version": 1, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "devices": devices}

    if args.out:
        Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✅ 导出 {len(devices)} 台设备 → {args.out}")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


def _merge_devices(local_reg, remote_devices: list) -> dict:
    """合并远程设备到本地 registry，返回统计。"""
    stats = {"created": 0, "updated": 0, "unchanged": 0, "total": len(remote_devices)}

    for dev in remote_devices:
        serial = dev.get("serial", "")
        if not serial:
            continue

        existing = local_reg.get(serial)
        # 提取可更新字段
        fields = {}
        for k in ("label", "group_name", "number", "wifi_ip", "location",
                  "platform_messenger", "platform_line", "platform_whatsapp",
                  "wallpaper_hash", "wallpaper_updated_at"):
            if k in dev and dev[k] is not None:
                fields[k] = dev[k]

        if existing is None:
            local_reg.upsert(serial, **fields)
            stats["created"] += 1
        else:
            # 检查是否有变化
            changed = False
            for k, v in fields.items():
                if existing.get(k) != v:
                    changed = True
                    break
            if changed:
                local_reg.upsert(serial, **fields)
                stats["updated"] += 1
            else:
                stats["unchanged"] += 1

    return stats


def cmd_import(args):
    from src.shared.device_registry import get_device_registry

    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    devices = data.get("devices", [])
    if not devices:
        print("⚠️  文件中无设备数据")
        return

    reg = get_device_registry(args.db or "")
    stats = _merge_devices(reg, devices)
    print(f"✅ 导入完成: {stats['created']} 新增, {stats['updated']} 更新, "
          f"{stats['unchanged']} 未变 (共 {stats['total']})")


def cmd_pull(args):
    import urllib.request

    def _do_pull():
        url = args.url.rstrip("/")
        req = urllib.request.Request(url, method="GET")
        if args.token:
            req.add_header("Authorization", f"Bearer {args.token}")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        devices = data.get("devices", [])
        if not devices:
            print(f"⚠️  远程返回 0 台设备")
            return

        from src.shared.device_registry import get_device_registry
        reg = get_device_registry(args.db or "")
        stats = _merge_devices(reg, devices)
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] 同步完成: +{stats['created']} ~{stats['updated']} "
              f"={stats['unchanged']} (远程 {stats['total']} 台)")

    if args.loop:
        interval = max(5, int(args.loop))
        print(f"🔄 每 {interval}s 从 {args.url} 拉取...")
        while True:
            try:
                _do_pull()
            except KeyboardInterrupt:
                print("\n⏹  已停止")
                break
            except Exception as e:
                ts = time.strftime("%H:%M:%S")
                print(f"[{ts}] ❌ 拉取失败: {e}")
            time.sleep(interval)
    else:
        _do_pull()


def main():
    parser = argparse.ArgumentParser(description="Registry DB 同步工具")
    sub = parser.add_subparsers(dest="cmd")

    p_export = sub.add_parser("export", help="导出 registry 为 JSON")
    p_export.add_argument("--out", help="输出文件路径（不指定则 stdout）")
    p_export.add_argument("--db", help="registry DB 路径（不指定用默认）", default="")

    p_import = sub.add_parser("import", help="从 JSON 导入到本机 registry")
    p_import.add_argument("--file", required=True, help="JSON 文件路径")
    p_import.add_argument("--db", help="registry DB 路径", default="")

    p_pull = sub.add_parser("pull", help="从远程 API 拉取并合并")
    p_pull.add_argument("--url", required=True, help="远程 export API URL")
    p_pull.add_argument("--token", help="认证 token (可选)")
    p_pull.add_argument("--loop", type=int, help="循环模式: 每 N 秒拉取一次")
    p_pull.add_argument("--db", help="本机 registry DB 路径", default="")

    args = parser.parse_args()
    if args.cmd == "export":
        cmd_export(args)
    elif args.cmd == "import":
        cmd_import(args)
    elif args.cmd == "pull":
        cmd_pull(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
