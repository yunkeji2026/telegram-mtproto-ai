"""GPU audio service for 192.168.0.176 (RTX 5090) — ASR + speech emotion.

Contract:
    POST /v1/audio/transcriptions   multipart/form-data   (OpenAI-compatible,
        matches src/voice_transcriber.py::OpenAITranscriber)
        file:            audio file (ogg/opus from Telegram, wav, mp3, ...)
        model:           ignored (single loaded model serves all requests)
        language:        optional ISO code; omit/auto -> autodetect
        response_format: "text" -> text/plain body; "json"/default -> {"text": ...}
    POST /v1/audio/emotion          multipart/form-data
        file: audio file -> {"labels":[...9 emotion2vec labels...],
                             "scores":[...], "model":..., "latency_ms":...}
        Raw label/score arrays only; canonical mapping stays client-side in
        src/ai/speech_emotion.py (single source of truth).
    GET /health -> {"status":"ok","model":...,"device":...,"ser_model":...}

Runs faster-whisper large-v3-turbo + funasr emotion2vec on CUDA. Env overrides:
    AITR_ASR_MODEL   (default large-v3-turbo)
    AITR_ASR_DEVICE  (default cuda)
    AITR_ASR_COMPUTE (default float16; fallback int8_float16 on OOM is manual)
    AITR_ASR_PORT    (default 8765)
    AITR_SER_MODEL   (default iic/emotion2vec_plus_large; "off" disables endpoint)
    AITR_SER_DEVICE  (default cuda)
    AITR_WARMUP      (default 1: preload ASR+SER models in background at boot,
                      removing the ~15s/~6s first-request cold start after restarts)
    MODELSCOPE_CACHE should point at the machine-wide cache (start_asr.ps1 sets
    D:\\cache\\modelscope); prefetch via prefetch_emotion.py as the interactive
    user — SYSTEM-context downloads are unreliable on this host.

Design notes:
- Single global model per task, requests serialized through locks: RTX 5090
  handles a 10s clip in well under 1s, so queues are simpler and safer than
  multi-instance VRAM juggling alongside Ollama models.
- VAD filter on for ASR: Telegram voice notes often carry leading/trailing
  silence; VAD trims it, which also suppresses whisper hallucination on silence.
- Emotion model load/inference failures degrade to HTTP 500; the client
  (SpeechEmotionRecognizer) falls back to local CPU funasr on any error.
"""

import asyncio
import logging
import os
import tempfile
import time

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("asr176")

MODEL_NAME = os.environ.get("AITR_ASR_MODEL", "large-v3-turbo")
DEVICE = os.environ.get("AITR_ASR_DEVICE", "cuda")
COMPUTE = os.environ.get("AITR_ASR_COMPUTE", "float16")
# SER 模型默认取**本地目录**（117 下载后 scp 过来；两边 hub 在本机网络都不可靠：
# modelscope 实测 179kB/s、hf-mirror 直接失败）。目录不存在时 funasr 会按 hub 名解析。
SER_MODEL = os.environ.get("AITR_SER_MODEL", r"C:\aitr_asr\models\emotion2vec_plus_large")
SER_HUB = os.environ.get("AITR_SER_HUB", "hf")   # 仅当 SER_MODEL 非本地路径时生效
SER_DEVICE = os.environ.get("AITR_SER_DEVICE", "cuda")

app = FastAPI(title="aitr-asr-176")
_model = None
_model_lock = asyncio.Lock()
_infer_lock = asyncio.Lock()
_ser_model = None
_ser_model_lock = asyncio.Lock()
_ser_infer_lock = asyncio.Lock()


async def _get_model():
    global _model
    if _model is not None:
        return _model
    async with _model_lock:
        if _model is not None:
            return _model
        from faster_whisper import WhisperModel

        t0 = time.time()
        log.info("loading %s device=%s compute=%s ...", MODEL_NAME, DEVICE, COMPUTE)
        _model = await asyncio.to_thread(
            WhisperModel, MODEL_NAME, device=DEVICE, compute_type=COMPUTE
        )
        log.info("model loaded in %.1fs", time.time() - t0)
        return _model


def _transcribe_sync(model, path: str, language):
    segments, info = model.transcribe(
        path,
        language=language,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300},
    )
    text = "".join(s.text for s in segments).strip()
    return text, info


async def _get_ser_model():
    global _ser_model
    if _ser_model is not None:
        return _ser_model
    async with _ser_model_lock:
        if _ser_model is not None:
            return _ser_model
        from funasr import AutoModel

        t0 = time.time()
        log.info("loading SER %s hub=%s device=%s ...", SER_MODEL, SER_HUB, SER_DEVICE)
        _ser_model = await asyncio.to_thread(
            AutoModel, model=SER_MODEL, hub=SER_HUB, device=SER_DEVICE,
            disable_update=True,
        )
        log.info("SER model loaded in %.1fs", time.time() - t0)
        return _ser_model


def _ser_sync(model, path: str):
    res = model.generate(path, granularity="utterance", extract_embedding=False)
    item = res[0] if isinstance(res, (list, tuple)) and res else res
    labels = list((item or {}).get("labels") or [])
    scores = [float(s) for s in ((item or {}).get("scores") or [])]
    return labels, scores


@app.on_event("startup")
async def _warmup():
    """Preload models in the background so the first real request is already hot.

    Best-effort: any load failure is logged and left for the per-request lazy
    path to retry (e.g. transient GPU/driver hiccup at boot).
    """
    if os.environ.get("AITR_WARMUP", "1").strip().lower() in ("0", "false", "off"):
        return

    async def _bg():
        try:
            await _get_model()
        except Exception:
            log.exception("ASR warmup failed (lazy path will retry)")
        if SER_MODEL.lower() not in ("off", "none", ""):
            try:
                await _get_ser_model()
            except Exception:
                log.exception("SER warmup failed (lazy path will retry)")
        log.info("warmup done asr=%s ser=%s", _model is not None, _ser_model is not None)

    asyncio.get_running_loop().create_task(_bg())


@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL_NAME, "device": DEVICE,
            "compute": COMPUTE,
            "ser_model": "" if SER_MODEL.lower() in ("off", "none", "") else SER_MODEL,
            "asr_loaded": _model is not None, "ser_loaded": _ser_model is not None}


@app.post("/v1/audio/emotion")
async def emotion(file: UploadFile = File(...)):
    if SER_MODEL.lower() in ("off", "none", ""):
        return JSONResponse({"error": {"message": "ser disabled"}}, status_code=503)
    suffix = os.path.splitext(file.filename or "audio.ogg")[1] or ".ogg"
    data = await file.read()
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(data)
        tmp.close()
        m = await _get_ser_model()
        t0 = time.time()
        async with _ser_infer_lock:
            labels, scores = await asyncio.to_thread(_ser_sync, m, tmp.name)
        ms = int((time.time() - t0) * 1000)
        log.info("emotion %s bytes -> top=%s elapsed=%dms",
                 len(data), labels[0] if labels else "?", ms)
        return {"labels": labels, "scores": scores,
                "model": SER_MODEL, "latency_ms": ms}
    except Exception as e:  # noqa: BLE001 - client falls back to local CPU on 500
        log.exception("emotion recognition failed")
        return JSONResponse({"error": {"message": str(e)}}, status_code=500)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    model: str = Form(None),
    language: str = Form(None),
    response_format: str = Form("json"),
):
    lang = (language or "").strip().lower() or None
    if lang in ("auto", "none", "null"):
        lang = None

    suffix = os.path.splitext(file.filename or "audio.ogg")[1] or ".ogg"
    data = await file.read()
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(data)
        tmp.close()
        m = await _get_model()
        t0 = time.time()
        async with _infer_lock:
            text, info = await asyncio.to_thread(_transcribe_sync, m, tmp.name, lang)
        log.info(
            "transcribed %s bytes lang=%s->%s dur=%.1fs elapsed=%.2fs chars=%d",
            len(data), lang or "auto", getattr(info, "language", "?"),
            getattr(info, "duration", 0.0), time.time() - t0, len(text),
        )
    except Exception as e:  # noqa: BLE001 - surface as 500 with reason
        log.exception("transcription failed")
        return JSONResponse({"error": {"message": str(e)}}, status_code=500)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    if (response_format or "").lower() == "text":
        return PlainTextResponse(text)
    return {"text": text}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("AITR_ASR_PORT", "8765")))
