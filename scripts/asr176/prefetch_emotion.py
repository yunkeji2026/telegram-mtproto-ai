"""GPU smoke test for emotion2vec on 176 (run ON 176 after the model dir is in place).

Model acquisition: hub downloads are unusable from this host (modelscope 179kB/s,
hf-mirror connect-fail) — download on 117 via huggingface_hub and scp the folder to
C:\\aitr_asr\\models\\emotion2vec_plus_large instead. This script only verifies the
local dir loads on CUDA and one inference runs.
"""
import math
import os
import struct
import time
import wave

from funasr import AutoModel

MODEL = os.environ.get("AITR_SER_MODEL", r"C:\aitr_asr\models\emotion2vec_plus_large")

t0 = time.time()
m = AutoModel(model=MODEL, device="cuda", disable_update=True)
print("LOAD_S=", round(time.time() - t0, 1))

wav = r"C:\aitr_asr\_ser_test.wav"
with wave.open(wav, "w") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    frames = b"".join(
        struct.pack("<h", int(3000 * math.sin(i * 0.05))) for i in range(16000 * 3)
    )
    w.writeframes(frames)

for tag in ("cold", "warm"):
    t1 = time.time()
    res = m.generate(wav, granularity="utterance", extract_embedding=False)
    print(f"INFER_{tag}_S=", round(time.time() - t1, 2))
item = res[0] if isinstance(res, (list, tuple)) and res else res
print("LABELS=", (item or {}).get("labels"))
print("SCORES=", [round(float(s), 3) for s in ((item or {}).get("scores") or [])])
print("PREFETCH_OK")
