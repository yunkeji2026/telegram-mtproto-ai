"""One-shot script: inject telegram.voice_reply into config.yaml."""
import yaml

VOICE_REPLY_BLOCK = """\
  voice_reply:
    enabled: true
    trigger: when_peer_voice
    backend: voice_clone_command
    out_dir: tmp_voice_replies
    max_text_chars: 200
    max_seconds: 30
    timeout_sec: 60
    send_text_summary: false
    voice_profile:
      enabled: true
      owner_consent: true
      reference_audio_path: D:/workspace/telegram-mtproto-ai/voice_samples/my_voice.wav
      backend: voice_clone_command
      command_args:
        - python
        - D:/workspace/telegram-mtproto-ai/tools/qwen_tts_wrapper.py
        - --region
        - cn
        - --text
        - "{text}"
        - --out
        - "{out}"
        - --voice-profile
        - D:/workspace/telegram-mtproto-ai/voice_samples/qwen_my_voice.json
        - --language-type
        - Japanese
      command_timeout_sec: 120

"""

cfg_path = "config/config.yaml"

with open(cfg_path, encoding="utf-8") as f:
    lines = f.readlines()

# Check already exists
if any("voice_reply:" in l for l in lines):
    print("voice_reply already present — skipping.")
else:
    # Find insertion point: first top-level key after telegram:
    in_tg = False
    insert_at = -1
    for i, line in enumerate(lines):
        if line.startswith("telegram:"):
            in_tg = True
        elif in_tg and line and line[0].isalpha() and line[0] != " ":
            insert_at = i
            break

    if insert_at == -1:
        print("ERROR: could not locate end of telegram: block")
        raise SystemExit(1)

    lines.insert(insert_at, VOICE_REPLY_BLOCK)
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"Inserted voice_reply before line {insert_at+1}")

# Verify
cfg = yaml.safe_load(open(cfg_path, encoding="utf-8"))
vr = (cfg.get("telegram") or {}).get("voice_reply")
print("voice_reply.enabled :", vr.get("enabled") if vr else "MISSING")
print("voice_reply.trigger :", vr.get("trigger") if vr else "MISSING")
print("voice_reply.backend :", vr.get("backend") if vr else "MISSING")
cmd = ((vr or {}).get("voice_profile") or {}).get("command_args")
print("command_args[0:2]   :", cmd[:2] if cmd else "MISSING")
