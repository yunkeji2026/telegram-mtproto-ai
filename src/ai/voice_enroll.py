"""声纹自助登记（Qwen / 阿里云百炼 voice clone）+ 音色写回工具。

本模块是「上传参考音频 → 登记克隆声纹 → 写回人设 voice_profile → 收件箱可选」闭环的核心。
DashScope REST 直连（不依赖 SDK）。CLI 见 tools/qwen_voice_clone.py（薄封装本模块）。

可单测的纯函数（无网络/IO）：
  - build_enroll_payload / parse_voice_id   — 登记请求体 / 响应解析
  - qwen_profile_json_dict                  — qwen_tts_wrapper 消费的 voice-profile JSON
  - build_qwen_voice_profile                — 写回人设的 voice_profile dict（含 command_args）
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

DEFAULT_TARGET_MODEL = "qwen3-tts-vc-2026-01-22"
ENROLLMENT_MODEL = "qwen-voice-enrollment"


def sanitize_preferred_name(name: str) -> str:
    """规整 Qwen 声纹登记的 preferred_name。

    DashScope 声纹登记要求该字段仅含小写字母与数字、长度 ≤10、且以字母开头
    （中文/空格/符号会被云端判为 InvalidParameter）。这里只用于云端请求的
    前缀，人设展示名/审计/本地 JSON 仍保留用户原始输入。

    例：'victor'→'victor'；'小习'→'voice'；'Voice 2'→'voice2'；'123'→'v123'。
    """
    s = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    if not s:
        return "voice"
    if not s[0].isalpha():
        s = "v" + s
    return s[:10]


# ── secrets / endpoint helpers ───────────────────────────────────────────────
def load_local_secret(name: str) -> str:
    """按 env → .env.local → config/secrets.local.json 顺序取密钥（与 tools 保持一致）。"""
    if os.getenv(name):
        return os.getenv(name, "")
    root = Path(__file__).resolve().parents[2]
    candidates = [
        Path(".env.local"),
        Path("config/secrets.local.json"),
        root / ".env.local",
        root / "config" / "secrets.local.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        if path.suffix == ".json":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            value = data.get(name) or data.get(name.lower())
            if value:
                return str(value).strip()
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == name:
                return v.strip().strip('"').strip("'")
    return ""


def _endpoint(region: str) -> str:
    if str(region).strip().lower() in ("cn", "china", "beijing", "mainland"):
        return "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"
    return "https://dashscope-intl.aliyuncs.com/api/v1/services/audio/tts/customization"


def _mime(path: Path) -> str:
    mt, _ = mimetypes.guess_type(str(path))
    if mt:
        return mt
    suf = path.suffix.lower()
    if suf == ".wav":
        return "audio/wav"
    if suf == ".m4a":
        return "audio/mp4"
    return "audio/mpeg"


def audio_data_uri(audio_path: str) -> str:
    p = Path(audio_path)
    return f"data:{_mime(p)};base64,{base64.b64encode(p.read_bytes()).decode()}"


# ── pure helpers (unit-testable) ─────────────────────────────────────────────
def build_enroll_payload(
    *, data_uri: str, preferred_name: str, target_model: str = DEFAULT_TARGET_MODEL,
) -> Dict[str, Any]:
    return {
        "model": ENROLLMENT_MODEL,
        "input": {
            "action": "create",
            "target_model": target_model,
            # 云端只接受 小写字母+数字、≤10、字母开头；中文等需先清洗
            "preferred_name": sanitize_preferred_name(preferred_name),
            "audio": {"data": data_uri},
        },
    }


def parse_voice_id(resp: Dict[str, Any]) -> str:
    return str(((resp or {}).get("output") or {}).get("voice") or "")


def build_delete_payload(voice: str) -> Dict[str, Any]:
    """Qwen 声纹删除请求体（action=delete, voice=<音色名>）。"""
    return {"model": ENROLLMENT_MODEL, "input": {"action": "delete", "voice": voice}}


def qwen_profile_json_dict(
    *, voice: str, target_model: str, reference_audio_path: str,
    region: str, preferred_name: str,
) -> Dict[str, Any]:
    """qwen_tts_wrapper.py --voice-profile 消费的 JSON。"""
    return {
        "provider": "qwen",
        "voice": voice,
        "target_model": target_model or DEFAULT_TARGET_MODEL,
        "reference_audio_path": reference_audio_path,
        "region": region,
        "preferred_name": preferred_name,
    }


def build_qwen_voice_profile(
    *, voice: str, reference_audio_path: str, voice_profile_json_path: str,
    speaker_id: str, region: str = "intl", target_model: str = DEFAULT_TARGET_MODEL,
    language_type: str = "Japanese", python_exe: str = "python",
    wrapper_path: str = "tools/qwen_tts_wrapper.py", command_timeout_sec: int = 120,
) -> Dict[str, Any]:
    """登记成功后写回人设的 voice_profile（TTSPipeline.voice_clone_command 消费）。

    用 command_args（避免 Windows 路径/引号问题）；{text}/{out} 由 TTSPipeline 在运行时填充。
    enabled+owner_consent+reference_audio_path 齐备 → /api/voice/profiles 标记为 ready。
    """
    return {
        "enabled": True,
        "owner_consent": True,
        "backend": "voice_clone_command",
        "speaker_id": speaker_id,
        "voice": voice,
        "reference_audio_path": reference_audio_path,
        "target_model": target_model or DEFAULT_TARGET_MODEL,
        "command_args": [
            python_exe, wrapper_path,
            "--region", region,
            "--text", "{text}",
            "--out", "{out}",
            "--voice-profile", voice_profile_json_path,
            "--language-type", language_type,
        ],
        "command_timeout_sec": command_timeout_sec,
    }


def build_lan_voice_profile(
    *, reference_audio_path: str, speaker_id: str,
    base_url: str, language: str = "zh", reference_text: str = "",
    clone_path: str = "/v1/tts/clone",
) -> Dict[str, Any]:
    """局域网零样本登记成功后写回人设的 voice_profile（fish_speech）。

    无云端音色 ID：合成时由 TTSPipeline 的「LAN 优先」直接拿 reference_audio
    去局域网主机零样本克隆。reference_text（参考音频原文）填了克隆效果更好。
    enabled+owner_consent+reference_audio_path 齐备 → /api/voice/profiles 标 ready。
    """
    vp: Dict[str, Any] = {
        "enabled": True,
        "owner_consent": True,
        "backend": "voice_clone_lan",
        "source": "lan_zeroshot",
        "speaker_id": speaker_id,
        "voice": "",
        "reference_audio_path": reference_audio_path,
        "base_url": base_url,
        "language": language,
        "clone_path": clone_path,
    }
    if reference_text:
        vp["reference_text"] = reference_text
    return vp


def without_voice_profile(persona: Dict[str, Any]) -> Dict[str, Any]:
    """返回去掉 voice_profile 的人设副本（解绑音色用）。"""
    p = dict(persona or {})
    p.pop("voice_profile", None)
    return p


def copy_voice_profile(src: Dict[str, Any], dst: Dict[str, Any]) -> Dict[str, Any]:
    """把 src 人设的 voice_profile 复制到 dst 人设副本（改绑/复用已登记音色，免重复上传）。"""
    out = dict(dst or {})
    vp = (src or {}).get("voice_profile")
    if isinstance(vp, dict):
        out["voice_profile"] = dict(vp)
    return out


def normalize_cloud_voice_entry(item: Any) -> Optional[Dict[str, Any]]:
    """把 DashScope voice_list 单项规范为 {voice, ...meta}；无法识别则 None。"""
    if not isinstance(item, dict):
        return None
    voice = str(item.get("voice") or item.get("voice_id") or "").strip()
    if not voice:
        return None
    out = dict(item)
    out["voice"] = voice
    return out


def normalize_cloud_voice_list(voice_list: Any) -> List[Dict[str, Any]]:
    """解析云端 list 响应中的 voice_list 数组。"""
    out: List[Dict[str, Any]] = []
    for item in voice_list or []:
        norm = normalize_cloud_voice_entry(item)
        if norm:
            out.append(norm)
    return out


def collect_local_voice_refs(
    personas: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """从人设列表收集 voice_profile.voice → [{persona_id, name, ready}, ...]。"""
    refs: Dict[str, List[Dict[str, Any]]] = {}
    for row in personas or []:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("persona_id") or row.get("id") or "").strip()
        if not pid:
            continue
        persona = row.get("persona")
        if not isinstance(persona, dict):
            persona = row
        vp = persona.get("voice_profile")
        if not isinstance(vp, dict):
            continue
        voice = str(vp.get("voice") or "").strip()
        if not voice:
            continue
        ref = str(vp.get("reference_audio_path") or "").strip()
        ready = bool(vp.get("owner_consent")) and bool(ref)
        refs.setdefault(voice, []).append({
            "persona_id": pid,
            "name": str(row.get("name") or persona.get("name") or pid),
            "ready": ready,
        })
    return refs


def reconcile_voice_assets(
    cloud_entries: List[Dict[str, Any]],
    local_refs: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """云端声纹 × 本地人设引用对账。

    - orphans：云端有、本地无人引用（可安全回收配额）
    - shared：多人设共用同一 voice id（改绑复制产生）
    - linked：云端有且恰有 1 个本地引用
    - dangling：本地引用但云端 list 中不存在（可能已删或分页未覆盖）
    """
    cloud_ids = {e["voice"] for e in cloud_entries}
    local_ids = set(local_refs.keys())

    orphans: List[Dict[str, Any]] = []
    shared: List[Dict[str, Any]] = []
    linked: List[Dict[str, Any]] = []
    dangling: List[Dict[str, Any]] = []

    for entry in cloud_entries:
        vid = entry["voice"]
        personas = local_refs.get(vid, [])
        item = {"voice": vid, "cloud": entry, "ref_count": len(personas), "personas": personas}
        if len(personas) == 0:
            orphans.append(item)
        elif len(personas) > 1:
            shared.append(item)
        else:
            linked.append(item)

    for vid in sorted(local_ids - cloud_ids):
        personas = local_refs.get(vid, [])
        dangling.append({"voice": vid, "ref_count": len(personas), "personas": personas})

    return {
        "orphans": orphans,
        "shared": shared,
        "linked": linked,
        "dangling": dangling,
        "summary": {
            "cloud_total": len(cloud_entries),
            "local_voice_ids": len(local_ids),
            "orphan_count": len(orphans),
            "shared_count": len(shared),
            "linked_count": len(linked),
            "dangling_count": len(dangling),
        },
    }


def purge_guard(
    voice: str,
    local_refs: Dict[str, List[Dict[str, Any]]],
    *,
    force: bool = False,
) -> Dict[str, Any]:
    """删除前引用计数保护：有引用且未 force → blocked。"""
    personas = local_refs.get(voice, [])
    if personas and not force:
        return {
            "allowed": False,
            "reason": "in_use",
            "ref_count": len(personas),
            "personas": personas,
        }
    return {"allowed": True, "ref_count": len(personas), "personas": personas}


# ── network calls (thin; not unit-tested) ────────────────────────────────────
def _post(payload: Dict[str, Any], *, api_key: str, region: str, timeout: float) -> Dict[str, Any]:
    resp = requests.post(
        _endpoint(region),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"qwen_voice_api_failed:{resp.status_code}:{resp.text[:500]}")
    return resp.json()


def enroll_voice(
    *, audio_path: str, preferred_name: str, api_key: str = "", region: str = "intl",
    target_model: str = DEFAULT_TARGET_MODEL, timeout: float = 120.0,
) -> Dict[str, Any]:
    """登记一个克隆声纹。返回 {voice, request_id, target_model, region, raw}。"""
    key = api_key or load_local_secret("DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError("missing DASHSCOPE_API_KEY")
    p = Path(audio_path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    payload = build_enroll_payload(
        data_uri=audio_data_uri(str(p)), preferred_name=preferred_name, target_model=target_model)
    resp = _post(payload, api_key=key, region=region, timeout=timeout)
    voice = parse_voice_id(resp)
    if not voice:
        raise RuntimeError(f"missing voice in response:{json.dumps(resp, ensure_ascii=False)[:300]}")
    return {
        "voice": voice, "request_id": resp.get("request_id", ""),
        "target_model": target_model, "region": region, "raw": resp,
    }


def list_cloned_voices(
    *, api_key: str = "", region: str = "intl", page_size: int = 10,
    page_index: int = 0, timeout: float = 120.0,
) -> Dict[str, Any]:
    key = api_key or load_local_secret("DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError("missing DASHSCOPE_API_KEY")
    payload = {
        "model": ENROLLMENT_MODEL,
        "input": {"action": "list", "page_size": int(page_size), "page_index": int(page_index)},
    }
    return _post(payload, api_key=key, region=region, timeout=timeout)


def delete_cloned_voice(
    *, voice: str, api_key: str = "", region: str = "intl", timeout: float = 60.0,
) -> Dict[str, Any]:
    """永久删除一个已登记的 Qwen 克隆声纹（不可恢复）。"""
    key = api_key or load_local_secret("DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError("missing DASHSCOPE_API_KEY")
    if not voice:
        raise ValueError("missing voice")
    return _post(build_delete_payload(voice), api_key=key, region=region, timeout=timeout)


def list_all_cloned_voices(
    *, api_key: str = "", region: str = "intl", page_size: int = 50,
    max_pages: int = 20, timeout: float = 120.0,
) -> List[Dict[str, Any]]:
    """分页拉取全部云端声纹（对账用；max_pages 防止无限循环）。"""
    all_entries: List[Dict[str, Any]] = []
    for page_index in range(max_pages):
        resp = list_cloned_voices(
            api_key=api_key, region=region, page_size=page_size,
            page_index=page_index, timeout=timeout)
        output = resp.get("output") or {}
        batch = normalize_cloud_voice_list(
            output.get("voice_list") or output.get("voices"))
        all_entries.extend(batch)
        total = output.get("total_count")
        if not batch:
            break
        if total is not None and len(all_entries) >= int(total):
            break
    return all_entries
