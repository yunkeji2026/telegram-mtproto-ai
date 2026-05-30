"""S4: Audit all personas in profiles_runtime.yaml and platform bindings."""
import yaml

with open('config/profiles_runtime.yaml', encoding='utf-8') as f:
    pr = yaml.safe_load(f)

profiles = pr.get('profiles', {})
print(f"=== profiles_runtime.yaml: {len(profiles)} personas ===")
for pid, p in profiles.items():
    ident = p.get('identity', {})
    bnd = p.get('boundaries', {})
    print(f"  [{pid}]")
    print(f"    name={p.get('name')}, role={str(p.get('role',''))[:60]}")
    print(f"    claim_human={ident.get('claim_human')}, deny_ai={ident.get('deny_ai')}")
    print(f"    forbidden_phrases count={len(p.get('speaking',{}).get('forbidden_phrases',[]))}")
    print()

with open('config/config.yaml', encoding='utf-8') as f:
    cfg = yaml.safe_load(f)

print("=== Platform Bindings ===")
for path in ['telegram', 'messenger_rpa', 'whatsapp_rpa', 'line_rpa']:
    section = cfg.get(path, {})
    # top-level persona_ids
    pid = section.get('persona_ids')
    if pid:
        print(f"  {path}.persona_ids = {pid}")
    # per-account
    for acc in section.get('accounts', []):
        apid = acc.get('persona_ids')
        aid = acc.get('account_id', acc.get('id', '?'))
        if apid:
            print(f"  {path}.accounts[{aid}].persona_ids = {apid}")
