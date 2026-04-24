"""探测 uiautomator dump 在 Messenger 上每个页面能拿到哪些 text/content-desc。

用法：
    python scripts/probe_view_tree.py <dump.xml>
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: probe_view_tree.py <dump.xml>", file=sys.stderr)
        return 2
    p = Path(sys.argv[1])
    if not p.exists():
        print(f"not found: {p}", file=sys.stderr)
        return 2
    xml = p.read_text(encoding="utf-8", errors="ignore")

    nodes = re.findall(r"<node[^>]*>", xml)
    text_nodes = [n for n in nodes if re.search(r'text="[^"]+"', n)]
    cd_nodes = [n for n in nodes if re.search(r'content-desc="[^"]+"', n)]

    print(f"file: {p.name}  size={len(xml)} chars  total_nodes={len(nodes)}")
    print(f"nodes with non-empty text:         {len(text_nodes)}")
    print(f"nodes with non-empty content-desc: {len(cd_nodes)}")
    print()

    # class 分布（top 8）
    classes: dict[str, int] = {}
    for n in nodes:
        m = re.search(r'class="([^"]*)"', n)
        if m:
            c = m.group(1)
            classes[c] = classes.get(c, 0) + 1
    print("== top classes ==")
    for c, k in sorted(classes.items(), key=lambda kv: -kv[1])[:8]:
        print(f"  {k:4d}  {c}")
    print()

    print("== first 20 nodes with text=  (cls / text / bounds) ==")
    for n in text_nodes[:20]:
        t = re.search(r'text="([^"]*)"', n)
        cls = re.search(r'class="([^"]*)"', n)
        b = re.search(r'bounds="([^"]*)"', n)
        txt = (t.group(1) if t else "")[:80]
        klass = (cls.group(1) if cls else "?").split(".")[-1]
        bounds = b.group(1) if b else ""
        print(f"  {klass:30s}  text={txt!r:60s}  {bounds}")
    print()

    print("== first 20 nodes with content-desc=  (cls / cd / bounds) ==")
    for n in cd_nodes[:20]:
        c = re.search(r'content-desc="([^"]*)"', n)
        cls = re.search(r'class="([^"]*)"', n)
        b = re.search(r'bounds="([^"]*)"', n)
        cd = (c.group(1) if c else "")[:80]
        klass = (cls.group(1) if cls else "?").split(".")[-1]
        bounds = b.group(1) if b else ""
        print(f"  {klass:30s}  cd={cd!r:60s}  {bounds}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
