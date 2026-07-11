"""Pre-download the faster-whisper model into HF_HOME so the SYSTEM-context
service finds it in local cache (SYSTEM's network context is unreliable)."""
import os
import time

os.environ.setdefault("HF_HOME", r"C:\aitr_asr\hf")

from faster_whisper.utils import download_model

t0 = time.time()
path = download_model("large-v3-turbo")
print("MODEL_DIR=", path)
print("ELAPSED_S=", round(time.time() - t0, 1))
