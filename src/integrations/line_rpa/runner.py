"""个人 LINE RPA：读 UI → Skill/AI → 回发。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.integrations.line_rpa import adb_helpers as adb
from src.integrations.line_rpa import group_policy
from src.integrations.line_rpa import screen_ocr
from src.integrations.line_rpa import screen_state as ss
from src.integrations.line_rpa import ui_hierarchy as ui
from src.integrations.line_rpa.chat_list_scanner import UnreadRow
from src.integrations.line_rpa.failure_shots import (
    FailureShotsConfig,
    save_failure_shot,
)
from src.integrations.line_rpa.human_pacing import (
    PacingConfig,
    jitter_ms,
    split_message,
    typing_duration_sec,
)
from src.integrations.line_rpa.navigator import Navigator

logger = logging.getLogger(__name__)

# ── Vision 结构化 prompt（替代旧纯文字版本） ────────────────────────────────
# 覆盖文字气泡 / 贴图 / 图片 / 语音等所有对方消息类型；
# 返回单行合法 JSON，下游用 _parse_vision_msg() 解析。
_CHAT_VISION_PROMPT = (
    "你在分析一张 LINE 手机聊天截图（可能已裁掉顶部标题栏和底部输入栏）。\n"
    "\n"
    "信息分类（极其重要）：\n"
    "A) 对方消息（role=peer）候选：\n"
    "   - 左侧白色圆角气泡内的文字（中文 / 英文 / 表情符号均算 text）\n"
    "   - 左侧一张 LINE 贴图（卡通角色，如棕熊 Brown / 白兔 Cony / 黄鸡 Sally，\n"
    "     浮在聊天背景上，无白色气泡框，左上方有圆形头像缩略图；算 sticker）\n"
    "   - 左侧一张照片 / 截图缩略图（有矩形边框的真实图片；算 image）\n"
    "   - 左侧语音条（蓝色音波图标；算 voice）\n"
    "   - 左侧文件卡片；算 file\n"
    "B) 己方消息（role=self）：右侧绿色气泡 / 右侧贴图 / 右侧图片\n"
    "C) 必须忽略（不是任何一方消息）：\n"
    "   - 屏幕中部居中的灰色胶囊日期分隔符（「今天」「昨天」「2026/4/18」）\n"
    "   - 橙色 / 绿色浮窗带 × 关闭按钮的 LINE 系统提示（「点击即可将贴图添加至收藏夹」等）\n"
    "   - 顶部标题栏、底部输入栏、手机系统状态栏\n"
    "\n"
    "步骤：\n"
    "1. 先在内心列出当前截图里**从上到下所有可见的 A/B 类消息**\n"
    "2. 选出**垂直坐标最大（最靠近屏幕底部输入框）那一条**，判断它属于 A 还是 B\n"
    "\n"
    "严格按下列 JSON 格式输出（只输出一行合法 JSON，不要 markdown 包裹、不要注释）：\n"
    '{"role":"peer|self|none","kind":"text|image|sticker|voice|file|other",'
    '"content":"...","desc":"..."}\n'
    "字段规则：\n"
    "- role=peer → 最底部对方消息；role=self → 最底部是己方；role=none → 找不到\n"
    "- kind=text：content 填原文（保留全部标点和 emoji），desc 空串\n"
    "- kind=sticker：content 空串，desc 用 ≤15 中文字描述贴图形象和动作（如 '棕熊欢呼撒彩纸'）\n"
    "- kind=image：content 空串，desc 用 ≤15 中文字描述图片内容（如 '键盘快捷键列表截图'）\n"
    "- kind=voice/file：content 空串，desc 简述（如 '2秒语音' 或 '未知文件'）\n"
)


def _parse_vision_msg(raw: str) -> dict:
    """解析 vision 返回的 JSON；失败时返回 role=none。"""
    s = (raw or "").strip()
    # 去 markdown 围栏
    if s.startswith("```"):
        lines = [ln for ln in s.splitlines() if not ln.strip().startswith("```")]
        s = "\n".join(lines).strip()
    try:
        d = json.loads(s)
        return {
            "role": str(d.get("role") or "none").lower(),
            "kind": str(d.get("kind") or "other").lower(),
            "content": str(d.get("content") or ""),
            "desc": str(d.get("desc") or ""),
            "_raw": raw,
        }
    except Exception:
        # 模型偶尔直接返回纯文本（旧模型兜底）
        if s and s.upper() != "NONE":
            return {"role": "peer", "kind": "text", "content": s, "desc": "", "_raw": raw}
        return {"role": "none", "kind": "other", "content": "", "desc": "", "_raw": raw}


def _vision_msg_to_peer_text(v: dict) -> Optional[str]:
    """把 vision 结构化结果转换为 AI 可直接使用的 peer_text。

    - text → 原文
    - sticker/image/voice/file → '[类型标注] 描述'（AI 会据此自然回应）
    - role!=peer 或解析失败 → None
    """
    if (v.get("role") or "none") != "peer":
        return None
    kind = v.get("kind", "text")
    content = (v.get("content") or "").strip()
    desc = (v.get("desc") or "").strip()
    if kind == "text":
        return content if content else None
    labels = {
        "sticker": "LINE贴图",
        "image": "图片消息",
        "voice": "语音消息",
        "file": "文件消息",
    }
    tag = labels.get(kind, "消息")
    return f"[{tag}] {desc}" if desc else f"[{tag}]"


def _default_state_path(config_path: Path) -> Path:
    return config_path.parent / "line_rpa_state.json"


def _numeric_chat_id(chat_key: str) -> int:
    """SkillManager 部分逻辑要求 context.chat_id 为 int（与 Telegram 对齐）。"""
    h = hashlib.md5(f"line_rpa:{chat_key}".encode("utf-8")).hexdigest()
    return int(h[:12], 16) % (2**31 - 1)


class LineRpaRunner:
    def __init__(
        self,
        *,
        config_manager: Any,
        skill_manager: Any,
        line_rpa_cfg: Dict[str, Any],
        state_store: Any = None,
    ) -> None:
        self._cm = config_manager
        self._sm = skill_manager
        self._cfg = line_rpa_cfg or {}
        self._serial: Optional[str] = None
        self._state_store = state_store  # 可选 SQLite 版；None 则回退 JSON
        self._pacing = PacingConfig.from_dict(self._cfg.get("human_pacing") or {})
        # W4-Runner：ContactHooks 由 main.py 后置注入，None 时所有调用静默跳过
        self._contact_hooks: Optional[Any] = None
        self._tts_semaphore: Optional[asyncio.Semaphore] = None  # P13-A: 防止 TTS 并发合成

    def set_contact_hooks(self, hooks: Optional[Any]) -> None:
        self._contact_hooks = hooks

    def _emit_contact_message(
        self, *, chat_key: str, direction: str, text: str, trace_id: str = "",
    ) -> None:
        """所有 LINE inbound/outbound 都汇聚到这个静默出口。

        - inbound 统一走 `on_line_first_text`——gateway 内部会 dedup 首条 vs 后续，
          免去 runner 侧维护"是不是第一条"的状态
        - outbound 走 `on_message(direction='out')`
        """
        hooks = self._contact_hooks
        if hooks is None or not chat_key:
            return
        account_id = str(self._cfg_get("account_id", "default") or "default")
        try:
            if direction == "in":
                hooks.on_line_first_text(
                    account_id=account_id,
                    external_id=chat_key,
                    text=text or "",
                    display_name=chat_key,
                    language_hint=str(
                        self._cfg_get("default_reply_lang", "") or ""),
                    trace_id=trace_id,
                )
            else:
                hooks.on_message(
                    channel="line",
                    account_id=account_id,
                    external_id=chat_key,
                    direction="out",
                    text_preview=(text or "")[:120],
                    display_name=chat_key,
                    trace_id=trace_id,
                )
        except Exception:
            logger.debug(
                "contact_hooks line %s 异常", direction, exc_info=True)

    def _lookup_intimacy_score(self, chat_key: str) -> Optional[float]:
        """W3-3A.1：LINE 入库后查 IntimacyEngine 的最新 score。
        失败/无 hooks/无 journey 时返回 None，调用方静默跳过 fusion。
        """
        hooks = self._contact_hooks
        if hooks is None or not chat_key:
            return None
        getter = getattr(hooks, "get_journey_intimacy", None)
        if getter is None:
            return None
        account_id = str(self._cfg_get("account_id", "default") or "default")
        try:
            score = getter(channel="line", account_id=account_id, external_id=chat_key)
            return float(score) if score is not None else None
        except Exception:
            logger.debug("[line_rpa] _lookup_intimacy_score failed", exc_info=True)
            return None

    def _lookup_funnel_stage(self, chat_key: str) -> Optional[str]:
        """W3-3M：查联系人 journey 的 funnel_stage，供 RelationshipStager 注入。"""
        hooks = self._contact_hooks
        if hooks is None or not chat_key:
            return None
        getter = getattr(hooks, "get_journey_funnel_stage", None)
        if getter is None:
            return None
        account_id = str(self._cfg_get("account_id", "default") or "default")
        try:
            stage = getter(channel="line", account_id=account_id, external_id=chat_key)
            return str(stage) if stage else None
        except Exception:
            logger.debug("[line_rpa] _lookup_funnel_stage failed", exc_info=True)
            return None

    def reconfigure(self, new_cfg: Dict[str, Any]) -> None:
        """热更新配置（LineRpaService 使用）。"""
        self._cfg = dict(new_cfg or {})
        self._pacing = PacingConfig.from_dict(self._cfg.get("human_pacing") or {})

    def _cfg_get(self, key: str, default: Any = None) -> Any:
        return self._cfg.get(key, default)

    def _resolve_line_reply_lang(self, chat_key: str, peer_text: str = "") -> str:
        """语言优先级链：全局 force > 对话锁定 forced_lang > 客户消息语言检测 > default_reply_lang。

        既往链条止于 default_reply_lang（默认 'zh'），缺「消息级检测」一层，导致运营未显式
        force/lock 时，外语客户也被回中文。本次补上「跟随客户语言」——用统一检测器
        ``translation_service.detect_language`` 检测当前（或最近）客户消息语言。
        force / per-chat forced_lang 仍优先，保留运营强制开关；检测不出（短句/emoji）时
        才回落 default_reply_lang。
        """
        # 1. Global force (operator override via config)
        _force = str(self._cfg.get("force_reply_lang") or "").strip().lower()
        if _force and _force not in ("auto", "detect", ""):
            return _force
        # 2. Per-chat forced_lang from state_store
        if self._state_store is not None:
            try:
                _cs = self._state_store.get_chat_state(chat_key) or {}
                _fl = str(_cs.get("forced_lang") or "").strip().lower()
                if _fl and _fl not in ("auto", "detect"):
                    return _fl
            except Exception:
                pass
        # 3. 跟随客户语言：检测当前消息；为空则回落 state_store 里最近一条客户消息
        _txt = (peer_text or "").strip()
        if not _txt and self._state_store is not None:
            try:
                _cs = self._state_store.get_chat_state(chat_key) or {}
                _txt = str(_cs.get("last_peer_text") or "").strip()
            except Exception:
                _txt = ""
        if _txt:
            try:
                from src.ai.translation_service import detect_language as _detect
                _d = str(_detect(_txt) or "").strip().lower()
                if _d and _d != "unknown":
                    return _d
            except Exception:
                logger.debug("[line] 客户语言检测失败，回落 default_reply_lang", exc_info=True)
        # 4. Config default
        return str(self._cfg_get("default_reply_lang", "zh") or "zh").lower()

    # ── P6-C: LINE TTS approval-only ─────────────────────────────────────
    # AIClient lang code → XTTS-v2 lang code
    _AILANGS_TO_XTTS: Dict[str, str] = {
        "zh": "zh-cn", "en": "en", "de": "de", "ja": "ja", "ko": "ko",
        "fr": "fr", "es": "es", "ar": "ar", "ru": "ru", "hi": "hi",
        "it": "it", "pt": "pt", "nl": "nl", "pl": "pl", "tr": "tr",
        "cs": "cs", "hu": "hu",
    }

    async def _maybe_generate_tts_for_pending(
        self,
        pending_id: int,
        reply_text: str,
        reply_lang: str,
    ) -> None:
        """P6-C/P14-B: 异步为 approval-mode pending 行生成 TTS 预览音频。"""
        vo = self._cfg.get("voice_output") or {}
        if not vo.get("enabled") or self._state_store is None:
            return
        if self._tts_semaphore is None:
            self._tts_semaphore = asyncio.Semaphore(1)
        from src.integrations.shared.tts_preview import generate_approval_tts
        await generate_approval_tts(
            pending_id, reply_text, reply_lang,
            voice_cfg=dict(vo), state_store=self._state_store,
            semaphore=self._tts_semaphore, fname_prefix="line-tts",
        )

    def _account_persona_id(self, is_group: bool = False) -> str:
        """Select account-level persona_id for 3-tier routing.

        - Private chats: persona_ids[0]
        - Group chats: persona_ids[1] if configured, else persona_ids[0]
        """
        ids = self._cfg_get("persona_ids") or []
        if not isinstance(ids, list) or not ids:
            return ""
        if is_group and len(ids) > 1:
            return str(ids[1])
        return str(ids[0])

    def _resolve_serial(self) -> Optional[str]:
        pref = (self._cfg_get("adb_serial") or "").strip()
        return adb.pick_serial(
            preferred=pref,
            prefer_line_installed=bool(self._cfg_get("prefer_line_device", True)),
            line_pkg=str(self._cfg_get("line_package", "jp.naver.line.android")),
        )

    def _state_path(self) -> Path:
        p = self._cfg_get("state_file")
        if p:
            return Path(p)
        return _default_state_path(Path(self._cm.config_path))

    def _load_state(self) -> Dict[str, Any]:
        """兼容层：JSON 态 → dict。有 state_store 时返回其 per-chat 记录的"扁平视图"。"""
        if self._state_store is not None:
            ck = str(self._cfg_get("chat_key", "line_rpa:default"))
            row = self._state_store.get_chat_state(ck)
            if not row:
                return {}
            return {
                "last_peer_text": row.get("last_peer_text") or "",
                "last_reply": row.get("last_reply") or "",
                "last_screen_crop_sha256": row.get("last_screen_sha256") or "",
            }
        sp = self._state_path()
        if not sp.exists():
            return {}
        try:
            return json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self, st: Dict[str, Any]) -> None:
        """兼容层：将扁平 dict 回写 JSON 或 state_store。"""
        if self._state_store is not None:
            ck = str(self._cfg_get("chat_key", "line_rpa:default"))
            self._state_store.update_chat_state(
                ck,
                last_peer_text=st.get("last_peer_text"),
                last_reply=st.get("last_reply"),
                last_screen_sha256=st.get("last_screen_crop_sha256"),
            )
            return
        sp = self._state_path()
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")

    async def _vision_fallback_read(self, png_bytes: bytes) -> Tuple[Optional[str], str]:
        """OCR 失败或关闭 Tesseract 时，用多模态读聊天界面。

        使用结构化 JSON prompt，覆盖文字气泡 / 贴图 / 图片 / 语音等所有类型。
        返回 (peer_text_for_ai, debug)。
        """
        vcfg = self._cfg_get("vision_read_fallback") or {}
        if not isinstance(vcfg, dict) or not vcfg.get("enabled"):
            return None, "vision_disabled"
        root_cfg = getattr(self._cm, "config", None) or {}
        gv = root_cfg.get("vision")
        global_v: Dict[str, Any] = gv if isinstance(gv, dict) else {}
        merged: Dict[str, Any] = {**global_v, **vcfg}
        from src.vision_client import VisionClient, has_any_vision_backend

        if not has_any_vision_backend(merged, global_v):
            return None, "vision_no_backend"

        # 若 config 里显式写了自定义 prompt，使用旧文本模式（向后兼容）；
        # 否则使用结构化 JSON prompt（支持贴图/图片等非文字消息）
        vcfg_lp = (vcfg.get("prompt") or "").strip() if isinstance(vcfg, dict) else ""
        use_structured = not vcfg_lp
        prompt = _CHAT_VISION_PROMPT if use_structured else vcfg_lp

        fd, path = tempfile.mkstemp(suffix=".png")
        try:
            os.close(fd)
            Path(path).write_bytes(png_bytes)
            loop_txt, vtag = await VisionClient.describe_image_with_ollama_zhipu_fallback(
                merged,
                global_v,
                path,
                prompt=prompt,
            )
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        if not use_structured:
            # 旧模式：直接用 normalize 抽文本
            norm = screen_ocr.normalize_vision_peer_line(loop_txt or "")
            if not norm:
                return None, f"vision_empty:{vtag}:{loop_txt!r}"
            return norm, f"vision_read_fallback:{vtag}"

        # 结构化模式：解析 JSON → 转 peer_text
        v = _parse_vision_msg(loop_txt or "")
        peer_text = _vision_msg_to_peer_text(v)
        kind_tag = f"kind={v.get('kind')} desc={v.get('desc')!r}"
        if not peer_text:
            role = v.get("role", "none")
            return None, f"vision_no_peer:{vtag}:role={role}:{kind_tag}"
        return peer_text, f"vision_structured:{vtag}:{kind_tag}"

    def _dump_ui_xml(self) -> tuple[Optional[bytes], str]:
        serial = self._serial
        if not serial:
            return None, "no_serial"
        primary = str(
            self._cfg_get("dump_remote_path", "/sdcard/line_rpa_dump.xml")
        )
        fallbacks = self._cfg_get("dump_remote_path_fallbacks") or [
            "/sdcard/line_rpa_dump.xml",
            "/data/local/tmp/line_rpa_hierarchy.xml",
        ]
        paths = [primary] + [p for p in fallbacks if p != primary]

        try_root = bool(self._cfg_get("try_root_ui_dump", True))

        for remote in paths:
            r = adb.dump_ui_hierarchy_xml(serial, remote)
            out = r.stdout or ""
            if "<?xml" in out and "hierarchy" in out:
                xml_start = out.find("<?xml")
                return out[xml_start:].encode("utf-8"), f"ok:{remote}"
            logger.debug("dump try %s rc=%s head=%s", remote, r.returncode, out[:80])

            if try_root:
                rr = adb.dump_ui_hierarchy_xml_as_root(serial, remote)
                outr = rr.stdout or ""
                if "<?xml" in outr and "hierarchy" in outr:
                    xml_start = outr.find("<?xml")
                    return outr[xml_start:].encode("utf-8"), f"ok_su:{remote}"
                logger.debug(
                    "dump su try %s rc=%s head=%s",
                    remote,
                    rr.returncode,
                    outr[:80],
                )

        # 回退：分步 dump + exec-out cat（旧逻辑）
        for remote in paths:
            adb.uiautomator_dump(serial, remote)
            r2 = adb.cat_remote_file(serial, remote)
            if r2.stdout and "<?xml" in r2.stdout:
                return r2.stdout.encode("utf-8"), f"ok_cat:{remote}"

        return None, "no_ui_xml_all_paths_failed"

    def _navigation_enabled(self) -> bool:
        nav = self._cfg.get("navigation") or {}
        return bool(isinstance(nav, dict) and nav.get("enabled"))

    async def run_once(
        self,
        *,
        dry_run: bool = False,
        force_reply: bool = False,
        peer_text_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        单次：
          - 若 navigation.enabled=true：
              前台 LINE → 回到聊天列表 → 扫未读 → 逐个进入 → 读/回/返
          - 否则（兼容老行为）：
              前台 LINE → dump → 解析对方末条 → 回复 → 发送
        """
        result: Dict[str, Any] = {"ok": False, "step": "init"}
        t_run0 = time.perf_counter()
        try:
            if (
                self._navigation_enabled()
                and peer_text_override is None
            ):
                return await self._run_once_multi(
                    result, t_run0, dry_run=dry_run, force_reply=force_reply
                )
            return await self._run_once_impl(
                result,
                t_run0,
                dry_run=dry_run,
                force_reply=force_reply,
                peer_text_override=peer_text_override,
            )
        finally:
            result["timings_ms"] = {
                "run_total": round((time.perf_counter() - t_run0) * 1000.0, 2),
            }
            try:
                from src.monitoring.metrics_store import get_metrics_store

                get_metrics_store().record_line_rpa_run(
                    step=str(result.get("step") or ""),
                    ok=bool(result.get("ok")),
                    total_ms=float(result["timings_ms"]["run_total"]),
                )
            except Exception:
                pass
            # 写入 per-chat 运行历史（若启用 SQLite 状态存储）
            if self._state_store is not None:
                try:
                    self._state_store.record_run(
                        chat_key=str(self._cfg_get("chat_key", "line_rpa:default")),
                        ok=bool(result.get("ok")),
                        step=str(result.get("step") or ""),
                        peer_text=result.get("peer_text"),
                        reply_text=result.get("reply_text"),
                        reader_path=str(result.get("dump") or ""),
                        total_ms=float(result["timings_ms"]["run_total"]),
                        error=str(result.get("error") or ""),
                        reply_lang=str(result.get("reply_lang") or ""),
                    )
                except Exception:
                    logger.debug("state_store.record_run 失败", exc_info=True)
            mj = (self._cfg_get("metrics_jsonl") or "").strip()
            if mj:
                try:
                    rec = {
                        "ts": time.time(),
                        "serial": self._serial,
                        "step": result.get("step"),
                        "ok": result.get("ok"),
                        "peer_len": len((result.get("peer_text") or "") or ""),
                        "timings_ms": result.get("timings_ms"),
                    }
                    Path(mj).parent.mkdir(parents=True, exist_ok=True)
                    with open(mj, "a", encoding="utf-8") as fp:
                        fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
                except OSError as e:
                    logger.warning("metrics_jsonl append failed: %s", e)

    async def _run_once_impl(
        self,
        result: Dict[str, Any],
        t_run0: float,
        *,
        dry_run: bool,
        force_reply: bool,
        peer_text_override: Optional[str],
    ) -> Dict[str, Any]:
        _ = t_run0
        crop_fp = ""

        self._serial = self._resolve_serial()
        if not self._serial:
            result["error"] = "no_adb_device"
            return result

        st = self._load_state()

        pkg = str(self._cfg_get("line_package", "jp.naver.line.android"))
        splash = str(
            self._cfg_get(
                "splash_activity",
                "jp.naver.line.android/.activity.SplashActivity",
            )
        )

        if not dry_run:
            fg = await asyncio.to_thread(adb.ensure_line_foreground, self._serial, pkg, splash)
            if fg.returncode != 0:
                logger.warning("foreground: %s", fg.stderr[:200])
            await asyncio.sleep(float(self._cfg_get("after_launch_sleep_sec", 1.2)))

        xml: Optional[bytes] = None

        if peer_text_override is not None:
            peer_text = (peer_text_override or "").strip()
            result["peer_debug"] = "override"
            result["peer_text"] = peer_text
            ax, hs = await asyncio.to_thread(self._dump_ui_xml)
            xml = ax
            result["dump"] = f"peer_override+{hs}"
        else:
            xml, how = await asyncio.to_thread(self._dump_ui_xml)
            result["dump"] = how
            peer_text: Optional[str] = None
            dbg = ""

            if xml:
                left_ratio = float(self._cfg_get("peer_left_ratio", 0.42))
                peer_text, dbg = ui.pick_last_peer_text(xml, left_ratio=left_ratio)
            else:
                mode = str(self._cfg_get("read_fallback", "none")).strip().lower()
                ocr_cfg = screen_ocr.resolve_screenshot_ocr_cfg(
                    self._cfg_get("screenshot_ocr") or {}
                )
                if not isinstance(ocr_cfg, dict):
                    ocr_cfg = {}
                ocr_on = bool(ocr_cfg.get("enabled", True))
                if mode == "screenshot_ocr" and ocr_on:
                    cropped, fp, pst = await asyncio.to_thread(
                        screen_ocr.capture_and_prepare_crop,
                        self._serial,
                        adb,
                        ocr_cfg,
                    )
                    if cropped is None:
                        result["error"] = pst
                        result["dump"] = f"{how}|{pst}"
                        return result
                    crop_fp = fp
                    result["crop_fingerprint"] = fp
                    if (
                        bool(ocr_cfg.get("skip_if_unchanged", True))
                        and not force_reply
                        and fp
                        and st.get("last_screen_crop_sha256") == fp
                    ):
                        result["ok"] = True
                        result["step"] = "screen_unchanged_skipped"
                        result["peer_debug"] = "fingerprint_match"
                        result["peer_text"] = None
                        result["dump"] = f"{how}|unchanged:{fp[:16]}"
                        return result

                    use_tesseract = bool(ocr_cfg.get("use_tesseract", True))
                    vrf = self._cfg_get("vision_read_fallback") or {}
                    vision_on = isinstance(vrf, dict) and vrf.get("enabled")

                    if use_tesseract:
                        peer_text, dbg = await asyncio.to_thread(
                            screen_ocr.ocr_peer_from_crop,
                            cropped,
                            ocr_cfg,
                        )
                        if (
                            not (peer_text or "").strip()
                            and vision_on
                        ):
                            vpeer, vdbg = await self._vision_fallback_read(cropped)
                            dbg = f"{dbg}|{vdbg}"
                            if vpeer:
                                peer_text = vpeer
                    else:
                        dbg = "ocr_skipped_tesseract_disabled"
                        peer_text = None
                        if vision_on:
                            vpeer, vdbg = await self._vision_fallback_read(cropped)
                            dbg = f"{dbg}|{vdbg}"
                            if vpeer:
                                peer_text = vpeer
                        else:
                            dbg = "ocr_skipped_no_vision_fallback"
                    result["dump"] = f"{how}|{dbg}"
                elif mode not in ("none", ""):
                    result["error"] = f"unknown_read_fallback:{mode}"
                    return result
                else:
                    result["error"] = "no_ui_xml"
                    return result

            result["peer_debug"] = dbg
            result["peer_text"] = peer_text

        if not peer_text or not peer_text.strip():
            result["ok"] = True
            result["step"] = "no_peer_text"
            return result

        last = (st.get("last_peer_text") or "").strip()
        if not force_reply and last == peer_text.strip():
            result["ok"] = True
            result["step"] = "duplicate_peer_skipped"
            return result

        chat_key = str(self._cfg_get("chat_key", "line_rpa:default"))
        cid = _numeric_chat_id(chat_key)
        req_id = f"rpa-{uuid.uuid4().hex[:12]}"
        use_backend_persona = bool(self._cfg_get("use_backend_persona", True))
        _rh = str(self._cfg_get("reply_style_hint") or "").strip()
        # 人设由 PersonaManager + AI 系统提示统一注入；此处仅传可选「LINE 补充」
        line_style = _rh if use_backend_persona else (_rh or "")
        ctx: Dict[str, Any] = {
            "chat_id": cid,
            "request_id": req_id,
            "channel": "line_rpa",
            "platform": "line_rpa",  # S5: CrossPlatformIdentity
            "reply_lang": self._resolve_line_reply_lang(chat_key, peer_text),
            "line_rpa_chat_key": chat_key,
            "line_rpa_style_hint": line_style,
            "account_persona_id": self._account_persona_id(),  # private path
        }
        out["reply_lang"] = ctx["reply_lang"]

        # W4-Runner: inbound 入库（失败静默）
        self._emit_contact_message(
            chat_key=chat_key, direction="in",
            text=peer_text.strip(), trace_id=req_id,
        )
        # W3-3A.1：把 IntimacyEngine 写回 journey 的最新 score 透传给 skill_manager
        # → companion_relationship 双信号融合（沉默衰减触发自动降级 + reunion 提示）
        _intim = self._lookup_intimacy_score(chat_key)
        if _intim is not None:
            ctx["intimacy_score"] = _intim
        # W3-3M：漏斗阶段 → RelationshipStager 语气指令注入
        _fstage = self._lookup_funnel_stage(chat_key)
        if _fstage:
            ctx["funnel_stage"] = _fstage

        reply_text: Optional[str] = None
        try:
            reply_text = await self._sm.process_message(
                peer_text.strip(),
                user_id=str(cid),
                context=ctx,
            )
        except Exception as e:
            logger.exception("process_message: %s", e)
            result["error"] = str(e)
            result["step"] = "skill_error"
            return result

        result["reply_text"] = reply_text
        if not reply_text:
            result["ok"] = True
            result["step"] = "empty_reply"
            return result

        if dry_run:
            result["ok"] = True
            result["step"] = "dry_run_done"
            if crop_fp:
                st["last_screen_crop_sha256"] = crop_fp
                self._save_state(st)
            return result

        send_res = await self._pace_and_send(xml, str(reply_text))
        result["send"] = send_res
        if send_res.get("ok"):
            # W4-Runner: outbound 入库
            self._emit_contact_message(
                chat_key=chat_key, direction="out",
                text=str(reply_text or ""), trace_id=req_id,
            )
            st["last_peer_text"] = peer_text.strip()
            st["last_reply"] = str(reply_text)[:2000]
            if crop_fp:
                st["last_screen_crop_sha256"] = crop_fp
            self._save_state(st)
            result["ok"] = True
            result["step"] = "sent"
        else:
            result["step"] = "send_failed"
        return result

    async def _pace_and_send(
        self, xml_bytes: Optional[bytes], text: str
    ) -> Dict[str, Any]:
        """拟人节奏封装：读停顿 → 分条 → 逐条发送（条间抖动）。

        返回汇总结果：ok 需全部成功；parts 记录每条发送结果与耗时。
        """
        # G1 全局 Kill-Switch（Phase C：RPA 覆盖）：紧急冻结时跳过物理发送
        try:
            from src.integrations.shared.rpa_send_guard import rpa_send_blocked
            _ks_on, _ks_scope = rpa_send_blocked(
                "line", self._cfg_get("account_id", "default"))
            if _ks_on:
                logger.warning("[line_rpa][kill-switch] 冻结发送，跳过（scope=%s）", _ks_scope)
                return {"ok": False, "error": "kill_switch", "scope": _ks_scope, "parts": []}
        except Exception:
            pass
        pacing = self._pacing
        # 1) 读停顿（让对方看到"对方在打字/思考"的感觉）
        if pacing.enabled:
            await asyncio.sleep(
                jitter_ms(pacing.read_pause_ms_lo, pacing.read_pause_ms_hi)
            )

        # 2) 分条
        parts = (
            split_message(text, pacing) if pacing.enabled else [text.strip()]
        )
        if not parts:
            return {"ok": False, "error": "empty_after_split", "parts": []}

        results: list = []
        overall_ok = True
        last_xml = xml_bytes
        redump_before_send = bool(self._cfg_get("redump_before_send", True))

        for idx, piece in enumerate(parts):
            # 3) "整段"模式下：把"打字时间"折算成发送前停顿（模拟人在打字）
            if pacing.enabled and not pacing.slow_type:
                await asyncio.sleep(
                    min(3.5, typing_duration_sec(piece, pacing))
                )
            # 4) 发送前若允许，重新 dump 一次 UI，避免界面迁移后 EditText 失效
            if idx > 0 and redump_before_send:
                redump_xml, _ = await asyncio.to_thread(self._dump_ui_xml)
                if redump_xml:
                    last_xml = redump_xml

            t0 = time.time()
            send_res = await asyncio.to_thread(self._send_text, last_xml, piece)
            if not send_res.get("ok"):
                await asyncio.sleep(1.5)
                redump_xml, _ = await asyncio.to_thread(self._dump_ui_xml)
                send_res = await asyncio.to_thread(
                    self._send_text, redump_xml or last_xml, piece
                )
                send_res["retried"] = True
            send_res["took_ms"] = int((time.time() - t0) * 1000)
            send_res["text"] = piece
            results.append(send_res)
            if not send_res.get("ok"):
                overall_ok = False
                break
            # 5) 条间间隔（最后一条不再 sleep）
            if pacing.enabled and idx < len(parts) - 1:
                await asyncio.sleep(
                    jitter_ms(pacing.inter_msg_ms_lo, pacing.inter_msg_ms_hi)
                )

        return {
            "ok": overall_ok,
            "parts": results,
            "parts_count": len(parts),
        }

    def _send_text(self, xml_bytes: Optional[bytes], text: str) -> Dict[str, Any]:
        serial = self._serial
        assert serial
        out: Dict[str, Any] = {"ok": False}

        fe = self._cfg_get("fallback_edit_tap") or []
        fs = self._cfg_get("fallback_send_tap") or []

        ed: Optional[tuple[int, int]] = None
        if xml_bytes:
            ed = ui.find_edittext_bottom_center(xml_bytes)
        if not ed and isinstance(fe, (list, tuple)) and len(fe) >= 2:
            ed = (int(fe[0]), int(fe[1]))
        if not ed:
            out["error"] = "no_edittext_or_fallback_edit_tap"
            return out
        ex, ey = ed
        adb.input_tap(serial, ex, ey)

        clear_del = int(self._cfg_get("clear_input_del_count", 96))
        adb.input_keyevent(serial, "123")  # KEYCODE_MOVE_END
        for _ in range(clear_del):
            adb.input_keyevent(serial, "67")  # DEL

        use_ime = bool(self._cfg_get("use_adb_keyboard", True))
        ime_component = str(
            self._cfg_get("adb_keyboard_ime", "com.android.adbkeyboard/.AdbIME")
        )
        if use_ime:
            adb.ime_set_adb_keyboard(serial, ime_component)
            adb.wait_for_adb_keyboard_ready(serial)
            use_b64 = bool(self._cfg_get("adb_keyboard_prefer_b64", True))
            pkg = (self._cfg_get("adb_keyboard_package") or "").strip()
            br = adb.adb_keyboard_input_text(
                serial,
                text,
                use_base64=use_b64,
                package=pkg or None,
            )
            out["broadcast_rc"] = br.returncode
            if br.returncode != 0:
                out["broadcast_err"] = br.stderr[:300]
                out["ime_broadcast_failed"] = True  # P6-A3: 供 classify_alerts 识别
        else:
            if not text.isascii():
                out["error"] = "non_ascii_needs_adb_keyboard"
                return out
            tr = adb.input_text_ascii(serial, text)
            out["input_rc"] = tr.returncode

        import time

        time.sleep(float(self._cfg_get("after_text_sleep_sec", 0.35)))

        xml_for_send = xml_bytes
        if bool(self._cfg_get("redump_before_send", True)):
            nx, sl = self._dump_ui_xml()
            if nx:
                xml_for_send = nx
                out["send_hierarchy"] = sl

        send_xy: Optional[tuple[int, int]] = None
        if xml_for_send:
            send_xy = ui.find_send_button_center(
                xml_for_send,
                line_pkg=str(self._cfg_get("line_package", "jp.naver.line.android")),
            )
        if not send_xy and isinstance(fs, (list, tuple)) and len(fs) >= 2:
            send_xy = (int(fs[0]), int(fs[1]))
        if send_xy:
            adb.input_tap(serial, send_xy[0], send_xy[1])
        else:
            adb.input_keyevent(serial, "66")  # ENTER

        out["ok"] = True
        return out

    # ────────────────────────────────────────────────────
    #        多会话模式（navigation.enabled=true）
    # ────────────────────────────────────────────────────

    def _nav_cfg(self) -> Dict[str, Any]:
        n = self._cfg.get("navigation") or {}
        return n if isinstance(n, dict) else {}

    def _failure_shots_cfg(self) -> FailureShotsConfig:
        return FailureShotsConfig.from_dict(self._cfg.get("failure_shots"))

    async def _capture_failure_shot(
        self, *, step: str, chat_key: str,
    ) -> Optional[str]:
        """按需截图并持久化失败现场；返回单层文件名或 None。

        仅当 `failure_shots.enabled=True` 且 step 命中 `on_steps` 时触发。
        """
        fs = self._failure_shots_cfg()
        if not fs.enabled or step not in fs.on_steps:
            return None
        if not self._serial:
            return None
        try:
            png = await asyncio.to_thread(
                screen_ocr.capture_screen_png, self._serial, adb
            )
        except Exception:
            png = None
        return save_failure_shot(
            cfg=fs, step=step, chat_key=chat_key, png=png,
        )

    def _build_navigator(self) -> "Navigator":
        nav_cfg = self._nav_cfg()
        tab = nav_cfg.get("chat_list_tab_tap") or None
        tab_tuple: Optional[Tuple[int, int]] = None
        if isinstance(tab, (list, tuple)) and len(tab) >= 2:
            try:
                tab_tuple = (int(tab[0]), int(tab[1]))
            except (TypeError, ValueError):
                tab_tuple = None

        async def _dump_async() -> Tuple[Optional[bytes], str]:
            return await asyncio.to_thread(self._dump_ui_xml)

        rd_cfg = nav_cfg.get("red_dot_fallback") or None

        # P6-B2: 构建 vision_scan_func（OOM 机型回退）
        vs_cfg = self._cfg_get("vision_scan") or {}
        vs_enabled = (
            isinstance(vs_cfg, dict)
            and bool(vs_cfg.get("enabled"))
            and bool((self._cfg_get("vision_read_fallback") or {}).get("enabled"))
        )
        vision_scan_func = None
        if vs_enabled:
            # 构造 vision 配置
            root_cfg = getattr(self._cm, "config", None) or {}
            gv = root_cfg.get("vision") or {}
            vrf = self._cfg_get("vision_read_fallback") or {}
            merged_v: Dict[str, Any] = {**gv, **vrf}
            prompt_ov = str((vs_cfg if isinstance(vs_cfg, dict) else {}).get("list_prompt_override") or "")

            # 估算分辨率（尽量从 adb 取；失败用默认 720x1600）
            try:
                from src.integrations.line_rpa import adb_helpers as _adb
                sz = _adb.screen_size(self._serial)
                dev_w, dev_h = sz if sz else (720, 1600)
            except Exception:
                dev_w, dev_h = 720, 1600

            from src.integrations.line_rpa.chat_list_scanner import parse_unread_rows_vision

            async def _vision_scan(png_bytes: bytes, max_rows: int) -> Tuple[list, str]:
                return await asyncio.to_thread(
                    parse_unread_rows_vision,
                    png_bytes,
                    vision_cfg=merged_v,
                    global_vision_cfg=gv,
                    max_rows=max_rows,
                    screen_w=int(dev_w),
                    screen_h=int(dev_h),
                    prompt_override=prompt_ov,
                )

            vision_scan_func = _vision_scan

        budget = float((vs_cfg if isinstance(vs_cfg, dict) else {}).get("scan_budget_sec", 30.0) or 30.0)

        assert self._serial is not None
        return Navigator(
            serial=self._serial,
            line_pkg=str(self._cfg_get("line_package", "jp.naver.line.android")),
            splash_activity=str(
                self._cfg_get(
                    "splash_activity",
                    "jp.naver.line.android/.activity.SplashActivity",
                )
            ),
            dump_func=_dump_async,
            after_tap_sleep_sec=float(nav_cfg.get("after_tap_sleep_sec", 0.8)),
            after_launch_sleep_sec=float(
                self._cfg_get("after_launch_sleep_sec", 1.2)
            ),
            chat_list_tab_tap=tab_tuple,
            red_dot_cfg=rd_cfg if isinstance(rd_cfg, dict) else None,
            vision_scan_func=vision_scan_func,
            vision_scan_budget_sec=budget,
        )

    @staticmethod
    def _row_allowed(
        row: UnreadRow,
        allow_list: Any,
        deny_list: Any,
    ) -> Tuple[bool, str]:
        name = (row.name or "").strip()
        if isinstance(deny_list, (list, tuple)):
            for d in deny_list:
                if not d:
                    continue
                if str(d).strip() and str(d).strip() in name:
                    return False, f"deny:{d}"
        if isinstance(allow_list, (list, tuple)) and allow_list:
            for a in allow_list:
                if not a:
                    continue
                if str(a).strip() and str(a).strip() in name:
                    return True, f"allow:{a}"
            return False, "not_in_allow_list"
        return True, ""

    async def _process_chat_room(
        self,
        *,
        dry_run: bool,
        force_reply: bool,
        xml_in_room: Optional[bytes],
        fallback_chat_key: str,
    ) -> Dict[str, Any]:
        """在已进入的会话页中读对方末条、生成并发送回复。复用老路径所需状态保存逻辑。"""
        out: Dict[str, Any] = {"ok": False, "step": "entered_room"}
        xml = xml_in_room
        if xml is None:
            xml, _how = await asyncio.to_thread(self._dump_ui_xml)

        if not xml:
            # P6-A2：uiautomator OOM / 失败 → 改用截图 + vision 读消息
            vrf = self._cfg_get("vision_read_fallback") or {}
            vision_room_ok = isinstance(vrf, dict) and bool(vrf.get("enabled"))
            if not vision_room_ok:
                out["step"] = "no_xml_in_room"
                out["error"] = "no_xml_in_room"
                shot = await self._capture_failure_shot(
                    step="no_xml_in_room", chat_key=fallback_chat_key,
                )
                if shot:
                    out["screenshot_path"] = shot
                return out

            # 截图 → vision 读结构化消息
            png = None
            try:
                png = await asyncio.to_thread(
                    screen_ocr.capture_screen_png, self._serial, adb
                )
            except Exception as e:  # noqa: BLE001
                out["step"] = "no_xml_in_room"
                out["error"] = f"screencap_failed:{e}"
                return out
            if not png:
                out["step"] = "no_xml_in_room"
                out["error"] = "screencap_empty"
                return out

            vpeer, vdbg = await self._vision_fallback_read(png)
            out["peer_debug"] = f"vision_room:{vdbg}"
            out["peer_text"] = vpeer
            if not vpeer or not vpeer.strip():
                out["ok"] = True
                out["step"] = "no_peer_text"
                return out

            # 跳过 XML 相关步骤，走快速路径：去重 → AI → 发送
            chat_key = fallback_chat_key
            # 以下逻辑与 xml 路径复用（不重复，直接设 peer_text 并 goto AI）
            out["chat_key"] = chat_key
            out["topbar_debug"] = "vision_room_fallback"
            out["peer_text"] = vpeer
            out["peer_bubbles"] = []
            out["is_group"] = False
            out["mentioned"] = False
            out["group_debug"] = "vision_room:no_xml"
            out["mention_debug"] = "vision_room:no_xml"

            # 去重（state_store）
            if self._state_store is not None:
                prev = self._state_store.get_chat_state(chat_key) or {}
                if (
                    not force_reply
                    and (prev.get("last_peer_text") or "").strip() == vpeer.strip()
                ):
                    out["ok"] = True
                    out["step"] = "duplicate_peer_skipped"
                    return out

            cid = _numeric_chat_id(chat_key)
            req_id = f"rpa-{uuid.uuid4().hex[:12]}"
            ctx: Dict[str, Any] = {
                "chat_id": cid,
                "request_id": req_id,
                "channel": "line_rpa",
                "platform": "line_rpa",  # S5: CrossPlatformIdentity
                "reply_lang": self._resolve_line_reply_lang(chat_key, vpeer),
                "line_rpa_chat_key": chat_key,
                "line_rpa_style_hint": str(self._cfg_get("reply_style_hint") or ""),
                "is_group": False,
                "mentioned": False,
                "vision_room": True,
                "account_persona_id": self._account_persona_id(),  # vision branch private
            }
            out["reply_lang"] = ctx["reply_lang"]
            # W4-Runner: inbound 入库（vision-peer 分支）
            self._emit_contact_message(
                chat_key=chat_key, direction="in",
                text=vpeer.strip(), trace_id=req_id,
            )
            # W3-3A.1：vision-peer 分支同样透传 intimacy_score
            _intim = self._lookup_intimacy_score(chat_key)
            if _intim is not None:
                ctx["intimacy_score"] = _intim
            # W3-3M：vision-peer 分支同样透传 funnel_stage
            _fstage = self._lookup_funnel_stage(chat_key)
            if _fstage:
                ctx["funnel_stage"] = _fstage

            reply_text: Optional[str] = None
            try:
                reply_text = await self._sm.process_message(
                    vpeer.strip(), user_id=str(cid), context=ctx,
                )
            except Exception as e:  # noqa: BLE001
                out["step"] = "skill_error"
                out["error"] = str(e)
                return out
            out["reply_text"] = reply_text
            if not reply_text:
                out["ok"] = True
                out["step"] = "empty_reply"
                return out
            if dry_run:
                out["ok"] = True
                out["step"] = "dry_run_done"
                return out

            # reply_mode 也适用
            reply_mode = str(self._cfg_get("reply_mode", "auto") or "auto").lower()
            if reply_mode == "off":
                out["ok"] = True
                out["step"] = "reply_disabled"
                return out
            if reply_mode == "approve" and self._state_store is not None:
                try:
                    pid = self._state_store.insert_pending(
                        chat_key=chat_key,
                        chat_name=chat_key,
                        peer_text=str(vpeer),
                        draft_reply=str(reply_text),
                    )
                    out["pending_id"] = pid
                    out["ok"] = True
                    out["step"] = "awaiting_approval"
                    # P6-C: fire-and-forget TTS preview (non-blocking)
                    asyncio.create_task(self._maybe_generate_tts_for_pending(
                        pid, str(reply_text),
                        # P10-A: 回退链 forced_lang > default，而非直接跳到 default
                        str(ctx.get("reply_lang") or self._resolve_line_reply_lang(chat_key)),
                    ))
                    return out
                except Exception as e:  # noqa: BLE001
                    logger.warning("vision_room insert_pending 失败: %s", e)

            # 发送（vision 模式：用 fallback_edit_tap + fallback_send_tap）
            send_res = await self._pace_and_send(None, str(reply_text))
            out["send"] = send_res
            if send_res.get("ok"):
                # W4-Runner: outbound 入库（vision-peer 分支）
                self._emit_contact_message(
                    chat_key=chat_key, direction="out",
                    text=str(reply_text or ""), trace_id=req_id,
                )
                out["ok"] = True
                out["step"] = "sent"
                if self._state_store is not None:
                    try:
                        self._state_store.update_chat_state(
                            chat_key,
                            last_peer_text=vpeer.strip(),
                            last_reply=str(reply_text)[:2000],
                        )
                    except Exception:
                        logger.debug("update_chat_state(vision) 失败", exc_info=True)
            else:
                out["step"] = "send_failed"
            return out

        # 从顶栏抽 chat_key（稳定对外可见），回退到 fallback
        topbar, topbar_dbg = ui.find_topbar_title(
            xml,
            line_pkg=str(self._cfg_get("line_package", "jp.naver.line.android")),
        )
        chat_key = (topbar or fallback_chat_key).strip() or fallback_chat_key
        out["chat_key"] = chat_key
        out["topbar_debug"] = topbar_dbg

        # 读对方消息（P3-3：优先连续气泡聚合；关闭或失败时回退 pick_last_peer_text）
        left_ratio = float(self._cfg_get("peer_left_ratio", 0.42))
        multi_cfg = self._cfg_get("peer_multi_bubble") or {}
        if not isinstance(multi_cfg, dict):
            multi_cfg = {}
        multi_enabled = bool(multi_cfg.get("enabled", True))
        peer_text: Optional[str] = None
        peer_dbg = ""
        bubbles: list = []
        if multi_enabled:
            bubbles, bdbg = ui.pick_last_peer_bubbles(
                xml,
                left_ratio=left_ratio,
                max_gap_px=int(multi_cfg.get("max_gap_px", 220) or 220),
                max_count=int(multi_cfg.get("max_count", 6) or 6),
                left_cx_tol_px=int(multi_cfg.get("left_cx_tol_px", 140) or 140),
            )
            if bubbles:
                joiner = str(multi_cfg.get("joiner", "\n") or "\n")
                # P4-1：给最新一条加标记，帮助 AI 判断时序
                mark_latest = bool(multi_cfg.get("mark_latest", True))
                if mark_latest and len(bubbles) >= 2:
                    latest_tag = str(multi_cfg.get("latest_tag", "[最新] ") or "[最新] ")
                    parts = list(bubbles[:-1]) + [latest_tag + bubbles[-1]]
                    peer_text = joiner.join(parts)
                else:
                    peer_text = joiner.join(bubbles)
                peer_dbg = f"multi_bubble:{bdbg}"
        if not peer_text:
            peer_text, peer_dbg_single = ui.pick_last_peer_text(
                xml, left_ratio=left_ratio
            )
            peer_dbg = peer_dbg or peer_dbg_single
        out["peer_text"] = peer_text
        out["peer_bubbles"] = bubbles  # 便于 Web / 日志排障
        out["peer_debug"] = peer_dbg
        if not peer_text or not peer_text.strip():
            out["ok"] = True
            out["step"] = "no_peer_text"
            shot = await self._capture_failure_shot(
                step="no_peer_text", chat_key=chat_key,
            )
            if shot:
                out["screenshot_path"] = shot
            return out

        # P2-4：群聊 @我 提权 / 群聊回复策略（委托给 group_policy 模块）
        verdict = group_policy.evaluate(
            xml=xml,
            peer_text=peer_text,
            line_pkg=str(self._cfg_get("line_package", "jp.naver.line.android")),
            self_names=list(self._cfg_get("self_names") or []),
            group_reply_policy=self._cfg_get("group_reply_policy", "all"),
            default_style_hint=str(self._cfg_get("reply_style_hint") or ""),
            mentioned_style_hint=str(
                self._cfg_get("reply_style_hint_mentioned") or ""
            ),
        )
        out["is_group"] = verdict.is_group
        out["group_debug"] = verdict.group_debug
        out["mentioned"] = verdict.mentioned
        out["mention_debug"] = verdict.mention_debug
        if not verdict.should_reply:
            out["ok"] = True
            out["step"] = verdict.skip_step or "group_skip"
            return out

        # 动态 chat_key 的 per-chat 去重（state_store 存在时）
        dup = False
        if self._state_store is not None:
            prev = self._state_store.get_chat_state(chat_key) or {}
            if (
                not force_reply
                and (prev.get("last_peer_text") or "").strip()
                == peer_text.strip()
            ):
                dup = True
        if dup:
            out["ok"] = True
            out["step"] = "duplicate_peer_skipped"
            return out

        # 构造 AI 上下文（cid 来自 chat_key 稳定哈希，Web/日志可识别）
        cid = _numeric_chat_id(chat_key)
        req_id = f"rpa-{uuid.uuid4().hex[:12]}"
        ctx: Dict[str, Any] = {
            "chat_id": cid,
            "request_id": req_id,
            "channel": "line_rpa",
            "platform": "line_rpa",  # S5: CrossPlatformIdentity
            "reply_lang": self._resolve_line_reply_lang(chat_key, peer_text),
            "line_rpa_chat_key": chat_key,
            "line_rpa_style_hint": verdict.style_hint,
            "is_group": verdict.is_group,
            "mentioned": verdict.mentioned,
            "account_persona_id": self._account_persona_id(is_group=verdict.is_group),
        }
        out["reply_lang"] = ctx["reply_lang"]
        # W4-Runner: inbound 入库（nav-scan 分支）
        self._emit_contact_message(
            chat_key=chat_key, direction="in",
            text=peer_text.strip(), trace_id=req_id,
        )
        # W3-3A.1：nav-scan 分支同样透传 intimacy_score
        _intim = self._lookup_intimacy_score(chat_key)
        if _intim is not None:
            ctx["intimacy_score"] = _intim
        # W3-3M：nav-scan 分支同样透传 funnel_stage
        _fstage = self._lookup_funnel_stage(chat_key)
        if _fstage:
            ctx["funnel_stage"] = _fstage

        try:
            reply_text = await self._sm.process_message(
                peer_text.strip(), user_id=str(cid), context=ctx,
            )
        except Exception as e:  # noqa: BLE001
            out["step"] = "skill_error"
            out["error"] = str(e)
            shot = await self._capture_failure_shot(
                step="skill_error", chat_key=chat_key,
            )
            if shot:
                out["screenshot_path"] = shot
            return out
        out["reply_text"] = reply_text
        if not reply_text:
            out["ok"] = True
            out["step"] = "empty_reply"
            return out
        if dry_run:
            out["ok"] = True
            out["step"] = "dry_run_done"
            return out

        # P4-3：审核模式 — 不真发，写入 pending 队列
        reply_mode = str(self._cfg_get("reply_mode", "auto") or "auto").lower()
        # 联调档位 1：reply_mode=off 只读取 / 生成草稿，绝不发送 / 不入审核
        if reply_mode == "off":
            out["ok"] = True
            out["step"] = "reply_disabled"
            return out
        if reply_mode == "approve" and self._state_store is not None:
            try:
                pid = self._state_store.insert_pending(
                    chat_key=chat_key,
                    chat_name=str(topbar or chat_key),
                    peer_text=str(peer_text or ""),
                    draft_reply=str(reply_text),
                )
                out["pending_id"] = pid
                out["ok"] = True
                out["step"] = "awaiting_approval"
                # P6-C: fire-and-forget TTS preview (non-blocking)
                asyncio.create_task(self._maybe_generate_tts_for_pending(
                    pid, str(reply_text),
                    # P10-A: 回退链 forced_lang > default，而非直接跳到 default
                    str(ctx.get("reply_lang") or self._resolve_line_reply_lang(chat_key)),
                ))
                return out
            except Exception as e:  # noqa: BLE001
                logger.warning("insert_pending 失败，回退为普通发送: %s", e)

        send_res = await self._pace_and_send(xml, str(reply_text))
        out["send"] = send_res
        if send_res.get("ok"):
            # W4-Runner: outbound 入库（nav-scan 分支）
            self._emit_contact_message(
                chat_key=chat_key, direction="out",
                text=str(reply_text or ""), trace_id=req_id,
            )
            out["ok"] = True
            out["step"] = "sent"
            # 写 per-chat 状态（动态 chat_key）
            if self._state_store is not None:
                try:
                    self._state_store.update_chat_state(
                        chat_key,
                        last_peer_text=peer_text.strip(),
                        last_reply=str(reply_text)[:2000],
                    )
                except Exception:
                    logger.debug("update_chat_state 失败", exc_info=True)
        else:
            out["step"] = "send_failed"
            shot = await self._capture_failure_shot(
                step="send_failed", chat_key=chat_key,
            )
            if shot:
                out["screenshot_path"] = shot
        return out

    async def _run_once_multi(
        self,
        result: Dict[str, Any],
        t_run0: float,
        *,
        dry_run: bool,
        force_reply: bool,
    ) -> Dict[str, Any]:
        """多会话一轮：导航→(循环: 重扫→挑顶部未读→进入→读/回→返)→返回聚合。

        P2-1 重构：不再"一次 scan + 遍历缓存坐标"（LINE 回复后会重排，坐标会失效）。
        改为每轮都重扫，每次只处理"最靠上、未处理过、未被策略排除"的那条。
        """
        _ = t_run0
        nav_cfg = self._nav_cfg()
        max_chats = int(nav_cfg.get("max_chats_per_run", 3) or 3)
        max_scan_rows = int(nav_cfg.get("max_scan_rows", 10) or 10)
        max_scroll_attempts = int(nav_cfg.get("max_scroll_attempts", 0) or 0)
        scroll_to_top_attempts = int(nav_cfg.get("scroll_to_top_attempts", 0) or 0)
        allow_list = nav_cfg.get("allow_list") or []
        deny_list = nav_cfg.get("deny_list") or []
        between_chats_ms = nav_cfg.get("between_chats_ms") or [900, 2200]
        # P6-C1: vision 多屏扫描最大页数（从 vision_scan.max_pages 取）
        vs_cfg = self._cfg_get("vision_scan") or {}
        vision_max_pages = int((vs_cfg if isinstance(vs_cfg, dict) else {}).get("max_pages", 5) or 5)
        # 是否在处理完后将列表归位到顶部
        scroll_top_after_done = bool(nav_cfg.get("scroll_top_after_done", True))

        self._serial = self._resolve_serial()
        if not self._serial:
            result["error"] = "no_adb_device"
            result["step"] = "no_adb_device"
            return result

        # 强制竖屏：横屏下 uiautomator dump 坐标系错误会导致导航失败
        await asyncio.to_thread(adb.force_portrait, self._serial)

        nav = self._build_navigator()

        # 1) 回到聊天列表
        gl = await nav.goto_chat_list(max_steps=6)
        result["nav_state"] = gl.state
        result["nav_reason"] = gl.reason
        result["nav_attempts"] = gl.attempts
        if not gl.ok:
            result["step"] = "nav_chat_list_failed"
            result["error"] = gl.reason
            return result

        # P4-2：可选滚到顶部，避免"用户手动滑到下半部分 → 最新未读被遮"
        if scroll_to_top_attempts > 0:
            try:
                done = await nav.scroll_chat_list_to_top(max_attempts=scroll_to_top_attempts)
                result["scroll_to_top_attempts"] = done
            except Exception:
                logger.debug("scroll_chat_list_to_top 失败", exc_info=True)

        per_chat_results: list = []
        # P6-C2：去重键升级为 (name, preview[:8]) 二级签名，防止同名不同内容被跳过；
        # vision 扫描有 preview，XML 扫描 preview="" 则回退到 y_bucket 区分。
        handled_keys: set = set()
        def _row_key(r: "UnreadRow") -> tuple:
            pv = (r.preview or "")[:8]
            if pv:
                return (r.name or "", pv)
            # XML 路径：无 preview，用 y_bucket 区分同名不同位
            return (r.name or "", int(r.tap_y) // 150)

        # P3-4：scroll 防抖签名 — 基于"第一页首/末行的名字+位置"，相同则判定滑动无效
        def _scroll_sig(rs: list) -> tuple:
            if not rs:
                return ("empty",)
            first = rs[0]
            last = rs[-1]
            return (
                first.name or "",
                int(first.tap_y) // 20,
                last.name or "",
                int(last.tap_y) // 20,
                len(rs),
            )
        last_scroll_sig: Optional[tuple] = None

        processed = 0
        scrolled = 0
        total_unread_seen = 0
        t_cycle0 = time.time()
        budget_sec = float(nav_cfg.get("cycle_budget_sec", 60.0) or 60.0)

        while processed < max_chats:
            if (time.time() - t_cycle0) > budget_sec:
                per_chat_results.append({"step": "cycle_budget_exhausted"})
                break

            # 每轮都重扫（LINE 回复后列表会重排）
            rows, dbg, _ = await nav.scan_unread_rows(max_rows=max_scan_rows)
            if processed == 0:
                result["scan_debug"] = dbg  # 只保留首次的扫描调试
            # P7-2：记录 vision 列表扫描（供 Web status 展示）
            if dbg and ("vision_fallback" in dbg or "vision_state_fallback" in dbg):
                result["vision_list_scan"] = {
                    "ts": time.time(),
                    "dbg": (dbg or "")[:400],
                }
            total_unread_seen = max(total_unread_seen, len(rows))

            # 挑"下一个要处理的 row"：跳过已处理名+位置 + 黑白名单
            next_row = None
            policy_skipped_this_iter: list = []
            for row in rows:
                if _row_key(row) in handled_keys:
                    continue
                allowed, why = self._row_allowed(row, allow_list, deny_list)
                if not allowed:
                    policy_skipped_this_iter.append({
                        "name": row.name,
                        "unread_count": row.unread_count,
                        "step": "skipped_policy",
                        "reason": why,
                    })
                    handled_keys.add(_row_key(row))
                    continue
                next_row = row
                break

            # 把本轮策略跳过的条目写入结果，方便 Web 看到"为什么没回"
            if policy_skipped_this_iter:
                per_chat_results.extend(policy_skipped_this_iter)

            if next_row is None:
                # 屏幕上已无可处理的：尝试滚动揭示更多（带防抖）
                if scrolled >= max_scroll_attempts:
                    break
                cur_sig = _scroll_sig(rows)
                if last_scroll_sig is not None and cur_sig == last_scroll_sig:
                    # 上次滑过后扫描结果一模一样 → 到底了或滑动未生效，停止
                    result["scroll_abort_reason"] = "no_content_change"
                    break
                last_scroll_sig = cur_sig
                scrolled += 1
                await nav.swipe_chat_list_down()
                continue

            # 进入会话
            op = await nav.open_unread_chat(next_row)
            if not op.ok:
                shot = await self._capture_failure_shot(
                    step="open_fail",
                    chat_key=f"list:{next_row.name}",
                )
                per_chat_results.append({
                    "name": next_row.name,
                    "unread_count": next_row.unread_count,
                    "step": "open_fail",
                    "reason": op.reason,
                    "screenshot_path": shot,
                })
                if self._state_store is not None:
                    try:
                        self._state_store.record_run(
                            chat_key=str(next_row.name),
                            ok=False,
                            step="open_fail",
                            peer_text=None,
                            reply_text=None,
                            reader_path="multi_chat",
                            total_ms=0.0,
                            error=str(op.reason or "")[:200],
                            screenshot_path=shot,
                        )
                    except Exception:
                        logger.debug("record_run(open_fail) 失败", exc_info=True)
                handled_keys.add(_row_key(next_row))
                await nav.back_to_chat_list()
                continue

            pr = await self._process_chat_room(
                dry_run=dry_run,
                force_reply=force_reply,
                xml_in_room=op.xml,
                fallback_chat_key=f"line_rpa:name:{next_row.name[:40]}",
            )
            pr["name"] = next_row.name
            pr["unread_count"] = next_row.unread_count
            per_chat_results.append(pr)
            handled_keys.add(_row_key(next_row))

            if self._state_store is not None:
                try:
                    self._state_store.record_run(
                        chat_key=str(pr.get("chat_key") or next_row.name),
                        ok=bool(pr.get("ok")),
                        step=str(pr.get("step") or ""),
                        peer_text=pr.get("peer_text"),
                        reply_text=pr.get("reply_text"),
                        reader_path="multi_chat",
                        total_ms=0.0,
                        error=str(pr.get("error") or ""),
                        screenshot_path=pr.get("screenshot_path"),
                        reply_lang=str(pr.get("reply_lang") or ""),
                    )
                except Exception:
                    logger.debug("record_run 失败", exc_info=True)

            processed += 1

            # 回列表
            back = await nav.back_to_chat_list()
            if not back.ok:
                recovery = await nav.goto_chat_list(max_steps=3)
                if not recovery.ok:
                    per_chat_results[-1]["back_failed"] = recovery.reason
                    break

            # 会话间间隔（拟人）
            if (
                processed < max_chats
                and isinstance(between_chats_ms, (list, tuple))
                and len(between_chats_ms) >= 2
            ):
                try:
                    await asyncio.sleep(
                        jitter_ms(int(between_chats_ms[0]), int(between_chats_ms[1]))
                    )
                except (TypeError, ValueError):
                    pass

        # P6-C1: 处理完成后将聊天列表归位到顶部，确保下一轮从最新消息开始扫描
        if scroll_top_after_done and processed > 0 and scroll_to_top_attempts > 0:
            try:
                done_top = await nav.scroll_chat_list_to_top(max_attempts=scroll_to_top_attempts)
                result["scroll_top_after_done"] = done_top
            except Exception:
                logger.debug("scroll_top_after_done 失败", exc_info=True)

        result["per_chat_results"] = per_chat_results
        result["chats_processed"] = processed
        result["unread_count"] = total_unread_seen
        result["scrolled"] = scrolled
        # 聚合状态：至少一个成功 sent/dry_run_done 视为 ok
        any_sent = any(
            r.get("step") in ("sent", "dry_run_done") for r in per_chat_results
        )
        any_unread_handled = any(
            r.get("step") in (
                "sent", "dry_run_done", "empty_reply",
                "no_peer_text", "duplicate_peer_skipped",
            ) for r in per_chat_results
        )
        if any_sent:
            result["ok"] = True
            result["step"] = "multi_sent"
        elif any_unread_handled:
            result["ok"] = True
            result["step"] = "multi_handled_no_send"
        elif total_unread_seen == 0 and processed == 0:
            result["ok"] = True
            result["step"] = "no_unread"
        else:
            result["step"] = "multi_all_failed"
        # 面向 state_store.record_run：给"主轮"也暴露 peer_text/reply_text（第一条有效）
        for pr in per_chat_results:
            if pr.get("peer_text"):
                result.setdefault("peer_text", pr["peer_text"])
                result.setdefault("reply_text", pr.get("reply_text"))
                break
        return result

    # ── P4-3：投递已审批回复 ─────────────────────────────────
    async def run_pending_deliveries(self, max_deliver: int = 3) -> Dict[str, Any]:
        """扫描 state_store 里 status=approved 的 pending，尝试逐条定位会话并发送。

        - 只处理最近 N 条（默认 3），控制单轮耗时
        - 会话定位：goto_chat_list → `find_chat_row_by_name` 匹配 final_reply 目标
        - 成功 → `mark_pending_sent(sent)`；失败 → 累加 send_attempts，`last_error` 写入
        - 同一 chat_key 同一轮只发"最新一条 approved"，其它同 chat_key approved 顺延
        """
        from src.integrations.line_rpa import chat_list_scanner as cls_mod

        out: Dict[str, Any] = {
            "delivered": 0,
            "failed": 0,
            "skipped": 0,
            "details": [],
        }
        if self._state_store is None:
            out["error"] = "no_state_store"
            return out

        try:
            pendings = self._state_store.list_pending(status="approved", limit=20)
        except Exception as e:  # noqa: BLE001
            out["error"] = f"list_pending_fail:{e}"
            return out
        if not pendings:
            return out

        # 同 chat_key 只保留最新一条
        seen_keys: set = set()
        queue: list = []
        for p in pendings:
            k = str(p.get("chat_key") or "")
            if k in seen_keys:
                continue
            seen_keys.add(k)
            queue.append(p)
            if len(queue) >= max_deliver:
                break

        nav = self._build_navigator()
        gl = await nav.goto_chat_list(max_steps=4)
        if not gl.ok:
            out["error"] = f"nav_chat_list_failed:{gl.reason}"
            return out

        for p in queue:
            pid = int(p.get("id") or 0)
            name = str(p.get("chat_name") or p.get("chat_key") or "")
            final_reply = str(p.get("final_reply") or "")
            detail = {"id": pid, "name": name}

            if not final_reply.strip():
                self._state_store.mark_pending_sent(pid, error="empty_final_reply")
                detail["step"] = "empty_final_reply"
                out["failed"] += 1
                out["details"].append(detail)
                continue

            # 取当前聊天列表 XML
            dump_xml, _ = await nav._dump()  # type: ignore[attr-defined]
            if not dump_xml:
                self._state_store.mark_pending_sent(pid, error="no_list_xml")
                detail["step"] = "no_list_xml"
                out["failed"] += 1
                out["details"].append(detail)
                continue

            row = cls_mod.find_chat_row_by_name(dump_xml, name)
            if row is None:
                # 尝试滚动 1 次再找
                await nav.swipe_chat_list_down()
                dump_xml, _ = await nav._dump()  # type: ignore[attr-defined]
                row = cls_mod.find_chat_row_by_name(dump_xml or b"", name) if dump_xml else None

            if row is None:
                self._state_store.mark_pending_sent(pid, error="chat_not_found")
                detail["step"] = "chat_not_found"
                out["failed"] += 1
                out["details"].append(detail)
                continue

            op = await nav.open_unread_chat(row)
            if not op.ok:
                self._state_store.mark_pending_sent(pid, error=f"open_fail:{op.reason}")
                detail["step"] = "open_fail"
                detail["reason"] = op.reason
                out["failed"] += 1
                out["details"].append(detail)
                await nav.back_to_chat_list()
                continue

            # P5-1：防陈旧 — 发前重新计算当前对方文本 hash，与入队时对比
            stale_cfg = self._cfg_get("approve_stale_check") or {}
            if not isinstance(stale_cfg, dict):
                stale_cfg = {}
            stale_enabled = bool(stale_cfg.get("enabled", True))
            stored_hash = str(p.get("peer_hash") or "")
            if stale_enabled and stored_hash:
                left_ratio = float(self._cfg_get("peer_left_ratio", 0.42))
                cur_bubbles, _ = ui.pick_last_peer_bubbles(
                    op.xml or b"", left_ratio=left_ratio,
                )
                if not cur_bubbles:
                    cur_text_single, _ = ui.pick_last_peer_text(
                        op.xml or b"", left_ratio=left_ratio,
                    )
                    cur_text = cur_text_single or ""
                else:
                    cur_text = "\n".join(cur_bubbles)
                cur_hash = self._state_store.compute_peer_hash(cur_text)
                if cur_hash and cur_hash != stored_hash:
                    # 对方又发了新消息；取消当前 pending（不发），让下一轮重起草
                    self._state_store.cancel_pending_with_reason(
                        pid, reason="stale_peer", by="auto:stale",
                    )
                    detail["step"] = "stale_peer"
                    detail["prev_hash"] = stored_hash
                    detail["cur_hash"] = cur_hash
                    out["skipped"] += 1
                    out["details"].append(detail)
                    await nav.back_to_chat_list()
                    continue

            send_res = await self._pace_and_send(op.xml or b"", final_reply)
            if send_res.get("ok"):
                # W4-Runner: outbound 入库（pending-approval 分支）
                self._emit_contact_message(
                    chat_key=str(p.get("chat_key") or ""), direction="out",
                    text=str(final_reply or ""),
                )
                self._state_store.mark_pending_sent(pid)
                detail["step"] = "sent"
                out["delivered"] += 1
            else:
                self._state_store.mark_pending_sent(
                    pid, error=f"send_failed:{send_res.get('error') or ''}"[:200]
                )
                detail["step"] = "send_failed"
                out["failed"] += 1
            out["details"].append(detail)

            # 回列表继续处理下一条
            await nav.back_to_chat_list()

        return out

    # ── P28：手动发送队列投递 ────────────────────────────────────
    async def run_send_queue_deliveries(self, max_deliver: int = 3) -> Dict[str, Any]:
        """弹出并投递手动发送队列任务（P28 hook；P29 实现 UI 自动化）。"""
        out: Dict[str, Any] = {
            "delivered": 0, "failed": 0, "skipped": 0, "details": [],
        }
        if self._state_store is None:
            out["error"] = "no_state_store"
            return out

        for _ in range(max_deliver):
            item = self._state_store.pop_send_queue_item()
            if item is None:
                break
            item_id = int(item.get("id") or 0)
            detail: Dict[str, Any] = {
                "id": item_id, "chat_key": item.get("chat_key"),
            }
            try:
                res = await self._handle_queued_send(item)
                if res.get("ok"):
                    self._state_store.mark_send_queue_item(item_id, "sent")
                    detail["step"] = "sent"
                    out["delivered"] += 1
                else:
                    err = str(res.get("error") or "send_failed")[:200]
                    self._state_store.mark_send_queue_item(item_id, "failed", error=err)
                    detail["step"] = "failed"
                    detail["error"] = err
                    out["failed"] += 1
            except Exception as exc:
                err = str(exc)[:200]
                self._state_store.mark_send_queue_item(item_id, "failed", error=err)
                detail["step"] = "exception"
                detail["error"] = err
                out["failed"] += 1
            out["details"].append(detail)

        return out

    async def _handle_queued_send(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """P28 stub — P29 将实现完整的 UI 导航 + 发送逻辑。

        Returns {"ok": bool, "error": str | None}.
        """
        # P29-TODO: implement LINE UI navigation and send for chat_key
        return {"ok": False, "error": "not_implemented_p29"}

    # ── 自动接受好友申请 ──────────────────────────────────────
    async def maybe_auto_accept_friends(self, max_accept: int = 5) -> Dict[str, Any]:
        """在当前屏幕 XML 中寻找"接受好友申请"按钮并点击。

        设计原则：
        - 只作用于当前可见界面，**不主动导航**到好友申请页
        - 若当前不在好友申请页则无副作用
        - 由 service._loop() 在 health_check 同级节流下调用
        """
        aa_cfg = self._cfg_get("auto_accept") or {}
        if not isinstance(aa_cfg, dict) or not aa_cfg.get("enabled"):
            return {"skipped": True, "reason": "disabled"}

        serial = self._serial
        if not serial:
            return {"skipped": True, "reason": "no_serial"}

        out: Dict[str, Any] = {"tapped": 0, "coords": []}
        try:
            xml_bytes, _ = await asyncio.to_thread(self._dump_ui_xml)
            if not xml_bytes:
                return {"skipped": True, "reason": "no_xml"}

            coords = ui.find_accept_button_coords(xml_bytes)
            if not coords:
                return {"skipped": True, "reason": "no_accept_buttons"}

            for cx, cy in coords[:max_accept]:
                adb.input_tap(serial, cx, cy)
                await asyncio.sleep(0.6)
                out["tapped"] += 1
                out["coords"].append([cx, cy])
                logger.info("auto_accept_friends: tapped (%d,%d)", cx, cy)

            if out["tapped"] and self._state_store is not None:
                try:
                    self._state_store.insert_alert(
                        kind="friend_accepted",
                        severity="info",
                        message=f"自动接受好友申请 {out['tapped']} 个",
                        detail={"coords": out["coords"]},
                        dedup_window_sec=300,
                    )
                except Exception:
                    pass
        except Exception as e:
            out["error"] = str(e)
            logger.debug("maybe_auto_accept_friends 失败: %s", e, exc_info=True)
        return out
