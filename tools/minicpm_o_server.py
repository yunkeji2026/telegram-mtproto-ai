r"""MiniCPM-o 4.5 语音主机 — 实时共情语音 + 一次性克隆合成（已对齐真实 API）。

本文件**部署在 GPU 机**，实现 telegram-mtproto-ai 网关约定的契约，即插即用：
    GET  /health         → {"model_loaded": bool, "loading": bool, "vram_allocated_gb": ..}
    GET  /v1/model/status→ 同 /health（前端轮询开关状态）
    POST /v1/model/load  → 按需把模型载入显存（冷载 ~8~12s；幂等）
    POST /v1/model/unload→ 释放显存（幂等；权重 ~21GB 还回，CUDA context 残留须退进程）
    POST /v1/tts/clone   → 文本 + 参考音(base64) + instructions(情感) → WAV（消息渠道情感语音条/Track A）
    WS   /v1/realtime    → 全双工(半双工流式)实时通话（事件协议见 src/ai/realtime_voice.py）

显存按需：**默认惰性**（启动不占显存），按「启动引擎」(/v1/model/load) 才载入、
「释放显存」(/v1/model/unload) 卸载——适合与别的 AI 服务共用同一张卡。--eager 可启动即载。
未载入时 /v1/tts/clone 返 409、/v1/realtime 回 error:model_not_loaded（前端提示先启动）。

真实推理 API 已据 RTX 5090 实测对齐（transformers 4.51 + minicpmo-utils）：
  克隆 TTS：model.chat(msgs=[sys(含参考音 np16k), user], use_tts_template=True,
            generate_audio=True, output_audio_path=...) → 写 24kHz wav；返回值是文本。
  流式：    reset_session(reset_token2wav_cache=True) → init_token2wav_cache(prompt_speech_16k=ref)
            → streaming_prefill(system) → streaming_prefill(用户 1s 分块, is_last_chunk)
            → streaming_generate(...) 生成器逐块 yield (wav_chunk[1×N@24k], text_chunk)
            → reset_session(reset_token2wav_cache=False)。输入 16k 单声道，输出 24k。

两种引擎：
    --mock      纯 CPU 占位，先打通 浏览器↔网关↔主机 全链路（无需 GPU）。
    （默认）    加载真实 MiniCPM-o 4.5（见 MiniCPMOEngine）。**单模型实例 → 推理串行化**
                （一个全局锁；高并发需多实例/排队，MVP 不做）。

部署（Windows + conda，实测环境）：
    conda activate minicpmo
    # 实测：fastapi 0.138 + starlette 1.3 会误判 Request/WebSocket 参数 → 钉到已验证组合：
    pip install "fastapi==0.115.6" "starlette==0.41.3" "uvicorn[standard]" websockets numpy soundfile librosa
    # （transformers/torch 已在 minicpmo 环境内；实时段自测请用「真实问句语音」，勿拿参考音当用户输入）
    set PYTHONUTF8=1
    python tools/minicpm_o_server.py --model-path D:\miniCPM\models\MiniCPM-o-4_5 \
        --device cuda --host 0.0.0.0 --port 7860
    # 先打通链路（任意机器，无 GPU）：python tools/minicpm_o_server.py --mock --port 7860
    # 放行防火墙（管理员 PowerShell，跨机访问必需）：
    #   New-NetFirewallRule -DisplayName "MiniCPM-o 7860" -Direction Inbound -Action Allow \
    #     -Protocol TCP -LocalPort 7860 -Profile Private

进程级启停（推荐生产，与别的 AI 服务共用同卡）——看守 --supervisor 自身 0 显存，按需
spawn/kill worker 子进程（worker 绑 127.0.0.1:7861，仅由看守反代；释放=杀进程=连 CUDA
context 一起还，不留残渣）。对外契约（/health、/v1/model/{load,unload,status}、/v1/tts/clone、
WS /v1/realtime）与单体完全一致 → **网关/前端零改动**：
    python tools/minicpm_o_server.py --supervisor --model-path D:\miniCPM\models\MiniCPM-o-4_5 \
        --device cuda --host 0.0.0.0 --port 7860 --worker-port 7861
用 NSSM 常驻看守（开机自启 + 崩溃重启；看守退出会一并收掉 worker）：
    nssm install MiniCPM-o "<minicpmo env python.exe>" "<abs>\minicpm_o_server.py --supervisor \
        --model-path D:\miniCPM\models\MiniCPM-o-4_5 --device cuda --host 0.0.0.0 --port 7860 --worker-port 7861"
    nssm set MiniCPM-o AppDirectory D:\miniCPM\scripts
    nssm set MiniCPM-o AppEnvironmentExtra PYTHONUTF8=1
    nssm set MiniCPM-o Start SERVICE_AUTO_START
    nssm start MiniCPM-o
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import math
import os
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional

# 必须在模块顶层 import：`from __future__ import annotations` 会把路由注解变字符串，
# FastAPI 用「模块全局」解析 Request/WebSocket；只在 build_app 里局部 import 会致 422/连不上。
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("minicpm_o_server")

SR_OUT_DEFAULT = 24000   # MiniCPM-o（CosyVoice2 解码）输出 24k


# ── WAV / PCM 小工具 ─────────────────────────────────────────────────────────
def pcm16_to_wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    n = len(pcm)
    buf = io.BytesIO()
    buf.write(b"RIFF"); buf.write(struct.pack("<I", 36 + n)); buf.write(b"WAVE")
    buf.write(b"fmt "); buf.write(struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                                              sample_rate * 2, 2, 16))
    buf.write(b"data"); buf.write(struct.pack("<I", n)); buf.write(pcm)
    return buf.getvalue()


def sine_pcm16(seconds: float, freq: float = 220.0, sample_rate: int = SR_OUT_DEFAULT,
               amp: float = 0.25) -> bytes:
    total = int(seconds * sample_rate); out = bytearray()
    for i in range(total):
        env = min(1.0, i / (sample_rate * 0.05)) * max(0.0, 1.0 - i / total)
        v = int(amp * env * 32767 * math.sin(2 * math.pi * freq * i / sample_rate))
        out += struct.pack("<h", v)
    return bytes(out)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def from_b64(s: str) -> bytes:
    try:
        return base64.b64decode(s or "")
    except Exception:
        return b""


def pcm_energy(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    acc = 0
    for v in struct.unpack("<%dh" % n, pcm[: n * 2]):
        acc += v * v
    return math.sqrt(acc / n) / 32768.0


# ── 引擎抽象 ─────────────────────────────────────────────────────────────────
class EngineNotLoaded(RuntimeError):
    """模型未载入显存时拒绝推理（前端据此提示先按「启动引擎」）。"""


class BaseEngine:
    model_loaded = False
    loading = False
    last_load_seconds = 0.0
    out_sample_rate = SR_OUT_DEFAULT

    def load(self) -> None:
        """按需把模型载入显存（幂等）。默认无操作（mock / 已常驻）。"""

    def unload(self) -> None:
        """释放显存（幂等）。默认无操作。"""

    def synth_clone(self, text: str, reference_audio: bytes, *, reference_text: str = "",
                    language: str = "zh", instructions: str = "") -> bytes:
        raise NotImplementedError

    def new_session(self, init: Dict[str, Any]) -> "BaseSession":
        raise NotImplementedError

    def health(self) -> Dict[str, Any]:
        return {"model_loaded": bool(self.model_loaded), "loading": bool(self.loading),
                "last_load_seconds": float(self.last_load_seconds),
                "out_sample_rate": self.out_sample_rate}


class BaseSession:
    def feed_audio(self, pcm: bytes) -> None: ...
    def end_turn(self) -> None: ...
    def interrupt(self) -> None: ...
    async def events(self): ...
    def close(self) -> None: ...


class _QueueSession(BaseSession):
    """共享：用 asyncio.Queue 把后台线程的事件推给 WS。"""
    def __init__(self):
        self._q: "asyncio.Queue[Dict[str,Any]]" = asyncio.Queue()
        # 会话常经 asyncio.to_thread 在工作线程创建（无运行 loop）；由 WS 路由在主线程回绑。
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._closed = False

    def _emit(self, ev: Dict[str, Any]) -> None:
        try:
            self._loop.call_soon_threadsafe(self._q.put_nowait, ev)
        except Exception:
            pass

    async def events(self):
        while not self._closed:
            ev = await self._q.get()
            yield ev

    def close(self) -> None:
        self._closed = True
        self._emit({"type": "turn.end"})


# ── Mock 引擎（无 GPU，打通链路用）───────────────────────────────────────────
class MockEngine(BaseEngine):
    def __init__(self):
        self.model_loaded = True    # mock 无真实显存；load/unload 仅翻转此标志以验证开关
        self.loading = False
        self.last_load_seconds = 0.0

    def load(self) -> None:
        self.model_loaded = True

    def unload(self) -> None:
        self.model_loaded = False

    def synth_clone(self, text, reference_audio, *, reference_text="", language="zh",
                    instructions="") -> bytes:
        dur = max(0.6, min(6.0, len(str(text)) * 0.12))
        return pcm16_to_wav_bytes(sine_pcm16(dur, freq=240), SR_OUT_DEFAULT)

    def new_session(self, init):
        return MockSession(init)


class MockSession(_QueueSession):
    def __init__(self, init: Dict[str, Any]):
        super().__init__()
        self.sr_in = int(init.get("sample_rate") or 16000)
        self.lang = init.get("language") or "zh"
        self._spoke = False; self._silence_ms = 0.0; self._talk_ms = 0.0

    def feed_audio(self, pcm: bytes) -> None:
        if self._closed:
            return
        frame_ms = (len(pcm) / 2) / self.sr_in * 1000.0
        if pcm_energy(pcm) > 0.02:
            self._talk_ms += frame_ms; self._silence_ms = 0.0; self._spoke = True
        else:
            self._silence_ms += frame_ms
        if self._spoke and self._talk_ms > 250 and self._silence_ms >= 600:
            self._spoke = False; self._talk_ms = 0.0; self._silence_ms = 0.0
            self._respond()

    def end_turn(self) -> None:
        if self._spoke:
            self._spoke = False; self._respond()

    def interrupt(self) -> None:
        pass

    def _respond(self) -> None:
        msg = "嗯，我在听呢，你继续说～" if self.lang == "zh" else "Mhm, I'm here, go on."
        self._emit({"type": "transcript.assistant", "text": msg, "final": True})
        pcm = sine_pcm16(1.0, freq=200, sample_rate=SR_OUT_DEFAULT)
        self._emit({"type": "output_audio", "audio_b64": b64(pcm), "sample_rate": SR_OUT_DEFAULT})
        self._emit({"type": "turn.end"})


# ── 真实 MiniCPM-o 4.5 引擎（已对齐 RTX 5090 实测 API）────────────────────────
class MiniCPMOEngine(BaseEngine):
    out_sample_rate = SR_OUT_DEFAULT

    def __init__(self, model_path: str, device: str = "cuda", lazy: bool = True):
        self.model_path = model_path
        self.device = device
        self.model = None
        self.model_loaded = False
        self.loading = False
        self.last_load_seconds = 0.0
        self._lock = threading.Lock()        # 单实例推理串行化
        self._load_lock = threading.Lock()   # load/unload 串行（防并发重复载入）
        if not lazy:
            self.load()

    def load(self) -> None:
        """把 MiniCPM-o 载入显存（幂等）。冷载约 8~12s。"""
        with self._load_lock:
            if self.model is not None:
                return
            self.loading = True
            try:
                import torch
                from transformers import AutoModel
                logger.info("loading MiniCPM-o 4.5 from %s (%s, bf16, sdpa) ...",
                            self.model_path, self.device)
                t0 = time.time()
                model = AutoModel.from_pretrained(
                    self.model_path, trust_remote_code=True, attn_implementation="sdpa",
                    torch_dtype=torch.bfloat16,
                    init_vision=False, init_audio=True, init_tts=True,   # TTS-only 省显存
                ).eval().to(self.device)
                model.init_tts()
                self.model = model
                self.model_loaded = True
                self.last_load_seconds = round(time.time() - t0, 1)
                logger.info("MiniCPM-o ready in %.1fs", self.last_load_seconds)
            finally:
                self.loading = False

    def unload(self) -> None:
        """释放显存（幂等）。权重 ~21GB 会还回；CUDA context 残留 ~1GB 须退进程才清。"""
        with self._load_lock:
            if self.model is None:
                return
            logger.info("unloading MiniCPM-o (releasing VRAM) ...")
            try:
                del self.model
            finally:
                self.model = None
                self.model_loaded = False
            import gc
            gc.collect()
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    torch.cuda.ipc_collect()
            except Exception:
                pass
            logger.info("MiniCPM-o unloaded.")

    def _require_loaded(self) -> None:
        if self.model is None:
            raise EngineNotLoaded("model_not_loaded")

    def _load_ref(self, ref_bytes: bytes):
        """参考音 bytes → np.float32 16k mono（克隆音色用）。"""
        import librosa, numpy as np, tempfile, os
        if not ref_bytes:
            return None
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(ref_bytes); rp = f.name
        try:
            audio, _ = librosa.load(rp, sr=16000, mono=True)
            return audio.astype(np.float32)
        finally:
            try: os.unlink(rp)
            except Exception: pass

    def synth_clone(self, text, reference_audio, *, reference_text="", language="zh",
                    instructions="") -> bytes:
        """一次性克隆：参考音进 system content，instructions 作语气；写 24k wav 读回。"""
        import os, tempfile
        self._require_loaded()
        ref = self._load_ref(reference_audio)
        style = (instructions.strip() or "请用这种声音风格自然地说话。")
        sys_content: List[Any] = ["模仿音频样本的音色并生成新的内容。"]
        if ref is not None:
            sys_content.append(ref)
        sys_content.append(style + " 直接作答，不要有冗余内容")
        sys_msg = {"role": "system", "content": sys_content}
        user_msg = {"role": "user", "content": ["请朗读以下内容。 " + str(text)]}
        out_path = tempfile.mktemp(suffix=".wav")
        with self._lock:
            self.model.chat(
                msgs=[sys_msg, user_msg], do_sample=True, max_new_tokens=512,
                use_tts_template=True, generate_audio=True, temperature=0.1,
                output_audio_path=out_path)
        try:
            with open(out_path, "rb") as f:
                return f.read()
        finally:
            try: os.unlink(out_path)
            except Exception: pass

    def new_session(self, init):
        self._require_loaded()
        return MiniCPMOSession(self, init)


class MiniCPMOSession(_QueueSession):
    """半双工流式会话：服务端 VAD 判定用户说完 → 流式生成边出边推（TTFT≈1s）。

    barge-in：用户在助手说话时插话 → 网关发 interrupt → 置 stop 标志，生成循环协作式停。
    """
    def __init__(self, engine: "MiniCPMOEngine", init: Dict[str, Any]):
        super().__init__()
        self.engine = engine
        self.sid = "rt-%d" % int(time.time() * 1000)
        self.sr_in = int(init.get("sample_rate") or 16000)
        self.lang = init.get("language") or "zh"
        self.system_prompt = str(init.get("system_prompt") or "")
        self.ref = engine._load_ref(from_b64(init.get("voice_ref_b64") or ""))
        self._buf = bytearray()
        self._spoke = False; self._talk_ms = 0.0; self._silence_ms = 0.0
        self._busy = False
        self._stop = threading.Event()
        self._init_voice()

    def _init_voice(self) -> None:
        with self.engine._lock:
            try:
                self.engine.model.reset_session(reset_token2wav_cache=True)
                if self.ref is not None:
                    self.engine.model.init_token2wav_cache(prompt_speech_16k=self.ref)
            except Exception:
                logger.warning("init_token2wav_cache 失败（音色克隆可能退化）", exc_info=True)

    def feed_audio(self, pcm: bytes) -> None:
        if self._closed or self._busy:
            return   # 生成中丢弃新帧；打断走 interrupt()
        self._buf += pcm
        frame_ms = (len(pcm) / 2) / self.sr_in * 1000.0
        if pcm_energy(pcm) > 0.02:
            self._talk_ms += frame_ms; self._silence_ms = 0.0; self._spoke = True
        else:
            self._silence_ms += frame_ms
        if self._spoke and self._talk_ms > 300 and self._silence_ms >= 700:
            self._kick_turn()

    def end_turn(self) -> None:
        if self._spoke and not self._busy:
            self._kick_turn()

    def _kick_turn(self) -> None:
        self._spoke = False; self._busy = True
        turn = bytes(self._buf); self._buf = bytearray()
        self._talk_ms = 0.0; self._silence_ms = 0.0
        self._stop.clear()
        threading.Thread(target=self._generate, args=(turn,), daemon=True).start()

    def interrupt(self) -> None:
        self._stop.set()   # 协作式停；生成循环每块检查

    def _generate(self, pcm_bytes: bytes) -> None:
        import numpy as np
        try:
            audio = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32) / 32768.0
            if audio.size < 16000:
                audio = np.pad(audio, (0, 16000 - audio.size))
            with self.engine._lock:
                m = self.engine.model
                if m is None:
                    raise EngineNotLoaded("model_unloaded_midcall")
                sys_msg = {"role": "system",
                           "content": ([self.system_prompt] if self.system_prompt else ["你是温暖真诚的陪伴者。"])}
                m.streaming_prefill(session_id=self.sid, msgs=[sys_msg],
                                    omni_mode=False, is_last_chunk=True)
                step = 16000
                nchunks = max(1, (audio.size + step - 1) // step)
                for i in range(nchunks):
                    ch = audio[i * step:(i + 1) * step]
                    m.streaming_prefill(
                        session_id=self.sid, msgs=[{"role": "user", "content": [ch]}],
                        omni_mode=False, is_last_chunk=(i == nchunks - 1))
                for wav_chunk, text_chunk in m.streaming_generate(
                        session_id=self.sid, generate_audio=True, use_tts_template=True,
                        enable_thinking=False, do_sample=True, max_new_tokens=512,
                        length_penalty=1.1):
                    if self._stop.is_set() or self._closed:
                        break
                    if text_chunk:
                        self._emit({"type": "transcript.assistant", "text": text_chunk})
                    if wav_chunk is not None:
                        arr = np.clip(wav_chunk.float().cpu().numpy().reshape(-1), -1, 1)
                        pcm = (arr * 32767).astype("<i2").tobytes()
                        self._emit({"type": "output_audio", "audio_b64": b64(pcm),
                                    "sample_rate": SR_OUT_DEFAULT})
                try:
                    m.reset_session(reset_token2wav_cache=False)
                except Exception:
                    pass
            self._emit({"type": "transcript.assistant", "text": "", "final": True})
            self._emit({"type": "turn.end"})
        except Exception as ex:  # noqa: BLE001
            logger.warning("streaming_generate 失败: %s", ex, exc_info=True)
            self._emit({"type": "error", "error": "gen_failed:%s" % str(ex)[:160]})
            self._emit({"type": "turn.end"})   # 退化输入(如把参考音当用户语音)也让客户端干净收尾
        finally:
            self._busy = False


# ── FastAPI 应用 ─────────────────────────────────────────────────────────────
def build_app(engine: BaseEngine, api_key: str = ""):
    app = FastAPI(title="MiniCPM-o voice host")

    def _authed(headers) -> bool:
        if not api_key:
            return True
        return headers.get("authorization", "") == f"Bearer {api_key}"

    def _health_with_vram() -> Dict[str, Any]:
        info = engine.health()
        try:
            import torch
            if torch.cuda.is_available():
                info["vram_allocated_gb"] = round(torch.cuda.memory_allocated() / 1e9, 2)
        except Exception:
            pass
        return info

    @app.get("/health")
    async def health():
        return _health_with_vram()

    @app.get("/v1/model/status")
    async def model_status():
        return _health_with_vram()

    @app.post("/v1/model/load")
    async def model_load(request: Request):
        if not _authed(request.headers):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        await asyncio.to_thread(engine.load)   # 冷载 ~8~12s（在线程里跑，不阻塞事件循环）
        return _health_with_vram()

    @app.post("/v1/model/unload")
    async def model_unload(request: Request):
        if not _authed(request.headers):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        await asyncio.to_thread(engine.unload)
        return _health_with_vram()

    @app.post("/v1/tts/clone")
    async def clone(request: Request):
        if not _authed(request.headers):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        text = str(body.get("text") or "")
        ref = from_b64(body.get("reference_audio_b64") or "")
        if not text or not ref:
            return JSONResponse({"error": "text 与 reference_audio_b64 必填"}, status_code=400)
        try:
            wav = await asyncio.to_thread(
                engine.synth_clone, text, ref,
                reference_text=str(body.get("reference_text") or ""),
                language=str(body.get("language") or "zh"),
                instructions=str(body.get("instructions") or ""))
        except EngineNotLoaded:
            return JSONResponse({"error": "model_not_loaded"}, status_code=409)
        if body.get("return_base64"):
            return {"audio_base64": b64(wav), "format": "wav"}
        return Response(content=wav, media_type="audio/wav")

    @app.websocket("/v1/realtime")
    async def realtime(ws: WebSocket):
        await ws.accept()
        try:
            init = json.loads(await ws.receive_text())
        except Exception:
            await ws.close(); return
        if init.get("type") != "session.init":
            await ws.send_text(json.dumps({"type": "error", "error": "expect_session_init"}))
            await ws.close(); return
        try:
            session = await asyncio.to_thread(engine.new_session, init)
        except EngineNotLoaded:
            await ws.send_text(json.dumps({"type": "error", "error": "model_not_loaded"}))
            await ws.close(); return
        if hasattr(session, "_loop"):
            session._loop = asyncio.get_running_loop()   # 回绑主 loop（会话在工作线程创建）

        async def pump_out():
            try:
                async for ev in session.events():
                    await ws.send_text(json.dumps(ev, ensure_ascii=False))
            except Exception:
                pass

        out_task = asyncio.ensure_future(pump_out())
        try:
            while True:
                ev = json.loads(await ws.receive_text())
                t = ev.get("type")
                if t == "input_audio":
                    session.feed_audio(from_b64(ev.get("audio_b64") or ""))
                elif t == "interrupt":
                    session.interrupt()
                elif t == "input_done":
                    session.end_turn()
                elif t == "session.close":
                    break
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            session.close(); out_task.cancel()
            try: await ws.close()
            except Exception: pass

    return app


# ── 进程级看守（0 显存常驻；spawn/kill 重模型 worker，全释放显存）──────────────
class WorkerSupervisor:
    """看守重模型 worker 子进程：load=spawn+等就绪，unload=杀进程树（含 CUDA context 全还）。

    本进程不导入 torch、不占显存；worker 绑 127.0.0.1（仅本看守反代），只 7860 对外。
    """
    def __init__(self, worker_cmd: List[str], worker_base_url: str, *, ready_timeout: float = 150.0):
        self.worker_cmd = worker_cmd
        self.worker_base_url = worker_base_url.rstrip("/")   # http://127.0.0.1:7861
        self.ready_timeout = ready_timeout
        self.proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def worker_ws_url(self) -> str:
        b = self.worker_base_url
        if b.startswith("https://"):
            b = "wss://" + b[len("https://"):]
        elif b.startswith("http://"):
            b = "ws://" + b[len("http://"):]
        return b + "/v1/realtime"

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _worker_health(self, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
        try:
            with urllib.request.urlopen(self.worker_base_url + "/health", timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None

    def status(self) -> Dict[str, Any]:
        running = self.is_running()
        h = self._worker_health() if running else None
        loaded = bool(h and h.get("model_loaded"))
        out: Dict[str, Any] = {
            "supervisor": True,
            "worker_running": running,
            "model_loaded": loaded,
            "loading": bool(running and not loaded),   # 进程在、health 未就绪 = 冷载中
            "out_sample_rate": (h or {}).get("out_sample_rate", SR_OUT_DEFAULT),
        }
        if h and "vram_allocated_gb" in h:
            out["vram_allocated_gb"] = h["vram_allocated_gb"]
        return out

    def start(self) -> Dict[str, Any]:
        with self._lock:
            if not self.is_running():
                logger.info("[supervisor] spawning worker: %s", " ".join(self.worker_cmd))
                flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                self.proc = subprocess.Popen(self.worker_cmd, creationflags=flags)
        deadline = time.time() + self.ready_timeout
        while time.time() < deadline:
            if not self.is_running():
                logger.warning("[supervisor] worker exited during load (载入失败，看 worker 日志)")
                break
            h = self._worker_health()
            if h and h.get("model_loaded"):
                logger.info("[supervisor] worker ready")
                break
            time.sleep(1.0)
        return self.status()

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            if self.is_running():
                pid = self.proc.pid
                logger.info("[supervisor] killing worker pid=%s (releasing all VRAM)", pid)
                self._kill_tree(pid)
            self.proc = None
        return self.status()

    def _kill_tree(self, pid: int) -> None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            else:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except Exception:
                    self.proc.kill()
        except Exception:
            logger.warning("[supervisor] kill worker failed", exc_info=True)


def build_supervisor_app(sup: "WorkerSupervisor", api_key: str = ""):
    app = FastAPI(title="MiniCPM-o supervisor")

    def _authed(headers) -> bool:
        if not api_key:
            return True
        return headers.get("authorization", "") == f"Bearer {api_key}"

    @app.get("/health")
    async def health():
        return sup.status()

    @app.get("/v1/model/status")
    async def model_status():
        return sup.status()

    @app.post("/v1/model/load")
    async def model_load(request: Request):
        if not _authed(request.headers):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await asyncio.to_thread(sup.start)

    @app.post("/v1/model/unload")
    async def model_unload(request: Request):
        if not _authed(request.headers):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await asyncio.to_thread(sup.stop)

    @app.post("/v1/tts/clone")
    async def clone(request: Request):
        if not _authed(request.headers):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not sup.is_running():
            return JSONResponse({"error": "model_not_loaded"}, status_code=409)
        body = await request.body()
        ctype = request.headers.get("content-type", "application/json")

        def _fwd():
            req = urllib.request.Request(sup.worker_base_url + "/v1/tts/clone",
                                         data=body, method="POST", headers={"Content-Type": ctype})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read(), r.headers.get("Content-Type", "application/json")

        try:
            content, rctype = await asyncio.to_thread(_fwd)
        except Exception as ex:  # noqa: BLE001
            return JSONResponse({"error": "worker_clone_failed:%s" % str(ex)[:120]}, status_code=502)
        return Response(content=content, media_type=rctype)

    @app.websocket("/v1/realtime")
    async def realtime(ws: WebSocket):
        await ws.accept()
        if not sup.is_running():
            await ws.send_text(json.dumps({"type": "error", "error": "model_not_loaded"}))
            await ws.close(); return
        try:
            import websockets
        except Exception:
            await ws.send_text(json.dumps({"type": "error", "error": "supervisor_missing_websockets"}))
            await ws.close(); return
        try:
            up = await websockets.connect(sup.worker_ws_url(), max_size=None)
        except Exception as ex:  # noqa: BLE001
            await ws.send_text(json.dumps({"type": "error", "error": "worker_unreachable:%s" % str(ex)[:80]}))
            await ws.close(); return

        async def c2u():
            while True:
                await up.send(await ws.receive_text())

        async def u2c():
            while True:
                m = await up.recv()
                await ws.send_text(m if isinstance(m, str) else m.decode("utf-8", "ignore"))

        t1 = asyncio.ensure_future(c2u())
        t2 = asyncio.ensure_future(u2c())
        try:
            await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (t1, t2):
                t.cancel()
            try: await up.close()
            except Exception: pass
            try: await ws.close()
            except Exception: pass

    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=7860)
    ap.add_argument("--model-path", default="openbmb/MiniCPM-o-4_5")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--mock", action="store_true", help="无 GPU 占位引擎，先打通链路")
    ap.add_argument("--eager", action="store_true",
                    help="启动即载入模型；默认惰性（显存留空，等 /v1/model/load 再载）")
    ap.add_argument("--supervisor", action="store_true",
                    help="看守模式：本进程 0 显存，按需 spawn/kill worker（进程级启停=全释放显存，配 NSSM 常驻）")
    ap.add_argument("--worker-port", type=int, default=0,
                    help="worker 端口（默认 = --port+1；worker 仅绑 127.0.0.1，由看守反代）")
    ap.add_argument("--ready-timeout", type=float, default=150.0,
                    help="载入等待上限秒（worker 冷启就绪）")
    args = ap.parse_args()

    import uvicorn
    if args.supervisor:
        worker_port = args.worker_port or (args.port + 1)
        worker_cmd = [sys.executable, os.path.abspath(__file__),
                      "--model-path", args.model_path, "--device", args.device,
                      "--host", "127.0.0.1", "--port", str(worker_port), "--eager"]
        sup = WorkerSupervisor(worker_cmd, "http://127.0.0.1:%d" % worker_port,
                               ready_timeout=args.ready_timeout)
        import atexit
        atexit.register(sup.stop)
        for _sig in ("SIGTERM", "SIGINT", "SIGBREAK"):
            s = getattr(signal, _sig, None)
            if s is not None:
                try:
                    signal.signal(s, lambda *_a: (sup.stop(), sys.exit(0)))
                except Exception:
                    pass
        app = build_supervisor_app(sup, api_key=args.api_key)
    elif args.mock:
        app = build_app(MockEngine(), api_key=args.api_key)
    else:
        engine = MiniCPMOEngine(args.model_path, args.device, lazy=not args.eager)
        app = build_app(engine, api_key=args.api_key)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
