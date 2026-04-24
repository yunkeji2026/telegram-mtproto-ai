"""P2-2 验证 extra_peers 解析。"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.integrations.messenger_rpa.combined_vision import (
    _parse_combined, _parse_guard_dict, _parse_peer_dict,
)

raw = json.dumps({
    "guard": {"type": "none", "action": "none", "title": "", "confidence": "high"},
    "peer": {"role": "peer", "kind": "text",
             "content": "pls help me with my order", "desc": ""},
    "extra_peers": [
        {"kind": "text", "content": "u there?", "desc": ""},
        {"kind": "text", "content": "hi", "desc": ""},
    ],
}, ensure_ascii=False)

parsed = _parse_combined(raw)
assert parsed, "parse failed"

guard = _parse_guard_dict(parsed["guard"])
peer = _parse_peer_dict(parsed["peer"], raw)
extras = []
for it in parsed.get("extra_peers") or []:
    d = {"role": "peer", **it}
    pm = _parse_peer_dict(d, raw)
    if pm and pm.role == "peer":
        extras.append(pm)

print(f"guard: {guard.type}")
print(f"peer : role={peer.role} kind={peer.kind} content={peer.content!r}")
print(f"extras: {len(extras)}")
for e in extras:
    print(f"  - kind={e.kind} content={e.content!r}")

assert guard.type == "none"
assert peer.role == "peer" and peer.kind == "text"
assert len(extras) == 2
assert extras[0].content == "u there?"
assert extras[1].content == "hi"
print("\n=== P2-2 EXTRA_PEERS PARSING OK ===")
