#!/usr/bin/env python3
"""厂商侧授权工具：生成密钥对 + 签发授权码（离线）。

用法
====
1) 生成厂商密钥对（一次性，私钥离线保管、切勿入库）::

     python scripts/license_tool.py genkeys --out config/.vendor_license_private.pem

   会打印 public_hex —— 将其替换到
   ``src/licensing/license_manager.py::DEFAULT_VENDOR_PUBLIC_KEY_HEX``。

2) 签发授权码::

     python scripts/license_tool.py issue \\
         --priv config/.vendor_license_private.pem \\
         --sub "示例客户公司" --plan pro --days 30 \\
         --seats 10 --channels telegram,line,web \\
         --features l4,white_label \\
         --out config/license.key

   把生成的 license.key 交付给客户放到其 ``config/license.key`` 即可。
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.licensing import generate_keypair, issue_license  # noqa: E402


def _cmd_genkeys(args: argparse.Namespace) -> int:
    kp = generate_keypair()
    Path(args.out).write_text(kp["private_hex"], encoding="utf-8")
    print(f"私钥已写入：{args.out}（请离线保管，切勿入库）")
    print(f"public_hex = {kp['public_hex']}")
    print("→ 将上面 public_hex 替换到 src/licensing/license_manager.py 的 "
          "DEFAULT_VENDOR_PUBLIC_KEY_HEX")
    return 0


def _cmd_issue(args: argparse.Namespace) -> int:
    priv_hex = Path(args.priv).read_text(encoding="utf-8").strip()
    payload = {
        "sub": args.sub,
        "plan": args.plan,
        "iat": int(time.time()),
        "seats": int(args.seats),
        "channels": [c.strip() for c in (args.channels or "").split(",") if c.strip()],
        "features": {f.strip(): True for f in (args.features or "").split(",") if f.strip()},
    }
    if args.days and int(args.days) > 0:
        payload["exp"] = int(time.time()) + int(args.days) * 86400
    if args.lic_id:
        payload["lic_id"] = args.lic_id
    token = issue_license(payload, priv_hex)
    if args.out:
        Path(args.out).write_text(token, encoding="utf-8")
        print(f"授权码已写入：{args.out}")
    else:
        print(token)
    print("payload:", json.dumps(payload, ensure_ascii=False))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="厂商授权工具（离线签发）")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("genkeys", help="生成 Ed25519 密钥对")
    g.add_argument("--out", default="config/.vendor_license_private.pem",
                   help="私钥输出路径")
    g.set_defaults(func=_cmd_genkeys)

    i = sub.add_parser("issue", help="签发授权码")
    i.add_argument("--priv", required=True, help="厂商私钥文件路径")
    i.add_argument("--sub", required=True, help="客户标识（公司名）")
    i.add_argument("--plan", default="pro",
                   choices=["community", "basic", "pro", "flagship"])
    i.add_argument("--days", default="0", help="有效天数（0=永久）")
    i.add_argument("--seats", default="0", help="最大坐席席位（0=不限）")
    i.add_argument("--channels", default="", help="允许渠道，逗号分隔")
    i.add_argument("--features", default="", help="功能位，逗号分隔（如 l4,white_label）")
    i.add_argument("--lic-id", dest="lic_id", default="", help="授权编号")
    i.add_argument("--out", default="", help="授权码输出路径（默认打印）")
    i.set_defaults(func=_cmd_issue)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
