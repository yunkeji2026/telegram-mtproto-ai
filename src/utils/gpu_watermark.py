"""LAN GPU 显存水位（Ollama ``/api/ps`` 聚合）——「备机过载」提前可见。

背景：140(4070,12G) 兼任嵌入双活备点 + 视觉备点，176(5090,32G) 主力跑
对话兜底/MT/VL/ASR。哪台被同时压上多个模型会挤爆（Ollama 静默换入换出 →
延迟毛刺），此前只能 SSH 上去 ``ollama ps`` 肉眼看。本模块把各主机
``/api/ps``（Ollama 原生接口，报每模型 ``size_vram`` 字节）聚成水位。

口径说明：统计的是 **Ollama 管理的模型显存**，不是 nvidia-smi 全卡占用
（ASR/SER 等独立进程不计入）——对「模型会不会挤爆 Ollama 预算」这个问题
是准确口径；卡上另有他用时 total_gb 可在配置里按可分配额度填小。

纯函数（summarize_host / summarize_fleet）+ 探针（probe_hosts，30s TTL）分离。
配置 ``ops.gpu_watermark``（新子系统默认 enabled:false）::

    ops:
      gpu_watermark:
        enabled: true
        hosts:
          - {name: "176-5090", base_url: "http://192.168.0.176:11434", vram_gb: 32}
          - {name: "140-4070", base_url: "http://192.168.0.140:11434", vram_gb: 12}
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 水位分级阈值（占 vram_gb 百分比）
WARN_PCT = 75.0
HIGH_PCT = 90.0


def parse_hosts(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 config 取启用的主机列表（enabled + 至少一台合法主机才非空）。"""
    ops = (config.get("ops") or {}) if isinstance(config, dict) else {}
    gw = ops.get("gpu_watermark") or {}
    if not gw.get("enabled", False):
        return []
    out: List[Dict[str, Any]] = []
    for h in gw.get("hosts") or []:
        if not isinstance(h, dict):
            continue
        base = str(h.get("base_url") or "").strip().rstrip("/")
        if not base or "://" not in base:
            continue
        out.append({
            "name": str(h.get("name") or base),
            "base_url": base,
            "vram_gb": float(h.get("vram_gb") or 0),
        })
    return out


def summarize_host(name: str, vram_gb: float,
                   ps_payload: Optional[Dict[str, Any]],
                   *, error: str = "") -> Dict[str, Any]:
    """把一台主机的 /api/ps 响应聚成水位行（纯函数）。

    ps_payload=None 表示探测失败 → reachable:false（error 附原因）。
    """
    if ps_payload is None:
        return {"name": name, "reachable": False, "error": error[:120],
                "total_gb": vram_gb, "used_gb": None, "used_pct": None,
                "level": "unknown", "models": []}
    models = []
    used_bytes = 0
    for m in (ps_payload.get("models") or []):
        if not isinstance(m, dict):
            continue
        sv = int(m.get("size_vram") or 0)
        used_bytes += sv
        models.append({
            "name": str(m.get("name") or m.get("model") or "?"),
            "size_gb": round(sv / 1e9, 1),
            "until": str(m.get("expires_at") or ""),
        })
    used_gb = used_bytes / 1e9
    pct = (used_gb / vram_gb * 100.0) if vram_gb > 0 else 0.0
    level = "high" if pct >= HIGH_PCT else ("warn" if pct >= WARN_PCT else "ok")
    # 按占用降序，最大头一眼可见
    models.sort(key=lambda x: -x["size_gb"])
    return {"name": name, "reachable": True, "error": "",
            "total_gb": vram_gb, "used_gb": round(used_gb, 1),
            "used_pct": round(pct, 1), "level": level, "models": models}


def summarize_fleet(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """整队汇总：整体 level 取最差（unknown 视作 warn——探不到该报修不该装绿）。"""
    rank = {"ok": 0, "warn": 1, "unknown": 1, "high": 2}
    worst = "ok"
    for r in rows:
        lv = str(r.get("level") or "unknown")
        if rank.get(lv, 1) > rank.get(worst, 0):
            worst = lv
    return {"level": worst if rows else "ok", "hosts": rows}


# ── 探针（30s TTL 缓存；看板轮询不打爆 LAN） ─────────────────────────
_CACHE: Dict[str, Any] = {"ts": 0.0, "key": "", "result": None}
_TTL_SEC = 30.0


async def probe_hosts(config: Dict[str, Any], *, force: bool = False) -> Optional[Dict[str, Any]]:
    """并发探测全部主机 /api/ps 并聚合。未启用 → None。"""
    hosts = parse_hosts(config)
    if not hosts:
        return None
    key = "|".join(h["base_url"] for h in hosts)
    now = time.time()
    if (not force and _CACHE["result"] is not None and _CACHE["key"] == key
            and now - _CACHE["ts"] < _TTL_SEC):
        return _CACHE["result"]

    import asyncio

    import httpx

    async def _one(h: Dict[str, Any]) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=3.0) as hc:
                resp = await hc.get(h["base_url"] + "/api/ps")
                resp.raise_for_status()
                return summarize_host(h["name"], h["vram_gb"], resp.json())
        except Exception as e:
            return summarize_host(h["name"], h["vram_gb"], None, error=str(e))

    rows = list(await asyncio.gather(*(_one(h) for h in hosts)))
    result = summarize_fleet(rows)
    _CACHE.update({"ts": now, "key": key, "result": result})
    return result
