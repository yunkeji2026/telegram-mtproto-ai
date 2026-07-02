"""voice_smoke.py — 语音通话「记忆闭环」冒烟检查（无需麦克风）。

两段检查：
  A) 记忆库直读（不占 GPU）：按 memory_key 取 bullets，打印 count_memory_bullets ——
     这就是接通后前端「已带入 N 条记忆」会显示的 N（与网关同一调用、同一 key、同一计数器）。
  B) 网关 WS 握手：连 ws://HOST/api/voice/live 发 hello，打印 ready(memory_count) 或 error ——
     验证整条 浏览器→网关→GPU 主机 握手（需引擎已载入；加 --load 可顺带载入再跑完整握手）。

用法：
  python tools/voice_smoke.py                               # 只做 A（安全、瞬时）
  python tools/voice_smoke.py --ws                          # A + WS 握手（引擎未载入会优雅报 host_unreachable）
  python tools/voice_smoke.py --ws --load --unload          # 载入引擎→完整 ready 握手→释放（占 GPU 显存）
  python tools/voice_smoke.py --memory-key telegram:5433982810 --persona lin_xiaoyu
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:18799"


def check_memory(memory_key: str) -> int:
    from src.utils.episodic_memory_store import EpisodicMemoryStore
    from src.web.routes.voice_live_routes import count_memory_bullets
    db = Path("config/bot.db")
    if not db.is_file():
        print(f"[A] 记忆库不存在: {db}")
        return -1
    bullets = EpisodicMemoryStore(db).get_bullets_for_prompt(memory_key, max_items=8) or ""
    n = count_memory_bullets(bullets)
    print(f"[A] memory_key={memory_key!r} → 接通将带入 {n} 条记忆")
    for ln in bullets.splitlines():
        if ln.strip():
            print("      ·", ln.strip()[:80])
    return n


def _post(path: str):
    # 带与服务一致的 Origin 复刻浏览器，过 CSRF 同源校验（否则 application/json POST 被 403）。
    req = urllib.request.Request(BASE + path, method="POST", data=b"{}",
                                 headers={"Content-Type": "application/json", "Origin": BASE})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read().decode())


def ensure_loaded(timeout: int = 240) -> bool:
    st = _get("/api/voice/engine/status")
    if st.get("model_loaded"):
        print("[load] 引擎已就绪")
        return True
    print("[load] 触发载入（占 GPU 显存，模型加载较慢，请稍候）…")
    try:
        _post("/api/voice/engine/load")
    except Exception as ex:  # load 端点可能阻塞到模型加载完才返回→客户端超时属正常，转轮询状态
        print(f"[load] 载入请求未即时返回（{type(ex).__name__}），改轮询状态…")
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(3)
        try:
            st = _get("/api/voice/engine/status")
        except Exception:
            continue
        if st.get("model_loaded"):
            print(f"[load] 就绪（{int(time.time() - t0)}s，VRAM {st.get('vram_allocated_gb', '?')}GB）")
            return True
        print(f"   …loading {int(time.time() - t0)}s worker={st.get('worker_running')}")
    print("[load] 超时未就绪")
    return False


async def ws_handshake(persona: str, memory_key: str, chat_key: str,
                       token: str, language: str):
    import websockets
    url = BASE.replace("http", "ws") + "/api/voice/live"
    print(f"[B] 连 {url} …")
    # 网关对 WS 做同源校验：无 Origin 会被 403 拒（安全特性）。真浏览器自带页面 Origin，
    # 此处显式带上与服务一致的 Origin 头复刻浏览器行为。
    async with websockets.connect(url, max_size=None,
                                  additional_headers={"Origin": BASE}) as ws:
        await ws.send(json.dumps({"type": "hello", "persona_id": persona,
                                  "memory_key": memory_key, "chat_key": chat_key,
                                  "token": token, "language": language}))
        try:
            for _ in range(6):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                t = ev.get("type")
                print("   ←", t, {k: v for k, v in ev.items() if k != "type"})
                if t == "ready":
                    print(f"[B] ✅ ready · memory_count={ev.get('memory_count')} · language={ev.get('language')}")
                    return ev
                if t == "error":
                    print(f"[B] ⚠️ error · {ev.get('error')}（引擎未载入时这是正常的——加 --load 跑完整握手）")
                    return ev
        except asyncio.TimeoutError:
            print("[B] 等待事件超时")
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--memory-key", default="telegram:7340576921")
    ap.add_argument("--chat-key", default="")
    ap.add_argument("--persona", default="lin_xiaoyu")
    ap.add_argument("--token", default="")
    ap.add_argument("--language", default="zh")
    ap.add_argument("--ws", action="store_true", help="跑网关 WS 握手")
    ap.add_argument("--load", action="store_true", help="握手前先载入引擎（占 GPU 显存）")
    ap.add_argument("--unload", action="store_true", help="结束后释放引擎")
    a = ap.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    check_memory(a.memory_key)
    if a.ws or a.load:
        if a.load and not ensure_loaded():
            return
        asyncio.run(ws_handshake(a.persona, a.memory_key, a.chat_key, a.token, a.language))
        if a.unload:
            print("[unload] 释放引擎…")
            _post("/api/voice/engine/unload")


if __name__ == "__main__":
    main()
