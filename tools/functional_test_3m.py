"""Phase 3M 功能测试脚本（本地运行，无需服务）。"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.contacts.relationship_stager import stage_directive

# ── 1. RelationshipStager 指令输出 ──
print("=== RelationshipStager 指令检验 ===")
cases = [
    ("INITIAL", None),
    ("HANDOFF_SENT", 15),
    ("LINE_ACCEPTED", 40),
    ("LINE_ENGAGED", 70),
    ("BONDED", 85),
    ("CONVERTED", 90),
    ("LOST_LINE_SILENT", None),
    ("NEEDS_MANUAL_MERGE", None),
]
all_ok = True
for stage, score in cases:
    d = stage_directive(stage, score)
    tag = "✅" if ("【关系阶段】" in d or d == "") else "❌"
    if tag == "❌":
        all_ok = False
    print(f"  {tag} {stage:25s} score={str(score):4s} -> {d[:60] or '(empty)'}")

# ── 2. rpa_hooks 协议完整性 ──
print("\n=== rpa_hooks 协议完整性 ===")
from src.contacts.rpa_hooks import NoopContactHooks, GatewayContactHooks
n = NoopContactHooks()
res = n.get_journey_funnel_stage(channel="line", account_id="a", external_id="e")
print(f"  {'✅' if res is None else '❌'} NoopContactHooks.get_journey_funnel_stage → {res}")
has_method = hasattr(GatewayContactHooks, "get_journey_funnel_stage")
print(f"  {'✅' if has_method else '❌'} GatewayContactHooks has get_journey_funnel_stage")

# ── 3. context_store NON_PERSIST ──
print("\n=== context_store NON_PERSIST ===")
from src.utils.context_store import _NON_PERSIST
for key in ("_funnel_directive", "funnel_stage"):
    ok = key in _NON_PERSIST
    print(f"  {'✅' if ok else '❌'} '{key}' in _NON_PERSIST")
    if not ok:
        all_ok = False

# ── 4. API endpoint live check ──
print("\n=== API 端点检验 (http://localhost:18787) ===")
import urllib.request
def api_get(path):
    try:
        with urllib.request.urlopen(f"http://localhost:18787{path}", timeout=5) as r:
            return r.status, json.loads(r.read())
    except Exception as e:
        return None, str(e)

endpoints = [
    "/api/drafts/quality?days=7",
    "/api/drafts/eval-scheduler/status",
    "/api/funnel/stats",
    "/api/contacts?limit=5",
]
for ep in endpoints:
    code, body = api_get(ep)
    if code == 200:
        # 3N: winning_variant field check
        if ep.startswith("/api/drafts/quality"):
            wv_ok = "winning_variant" in body
            print(f"  ✅ {ep} [200] winning_variant={'present' if wv_ok else 'MISSING!'}")
        elif ep.startswith("/api/drafts/eval-scheduler"):
            avail = body.get("available")
            print(f"  ✅ {ep} [200] available={avail}")
        else:
            print(f"  ✅ {ep} [200]")
    else:
        print(f"  ❌ {ep} [{code}] {str(body)[:80]}")
        all_ok = False

print(f"\n{'✅ ALL PASS' if all_ok else '❌ SOME FAILURES'}")
