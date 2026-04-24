"""Messenger 全自动聊天 · 一键启动。

使用方法：
    python start_messenger_auto.py                      # 用 config.yaml 里的 adb_serial
    python start_messenger_auto.py 192.168.0.113:5555   # 显式指定
    python start_messenger_auto.py --check              # 只做自检不启动
    python start_messenger_auto.py --once               # 跑一次 run_once 后退出

流程（全部自动，无需人工介入）：

    1. ADB 健康检查（online + screen on + unlocked）；不健康就自愈
       （disconnect→connect / KEYCODE_WAKEUP / swipe 解锁）
    2. AdbKeyboard 已装？没装就 adb install tools/ADBKeyboard.apk + IME enable
    3. 坐标校准（像素级扫 Inbox 头像峰）；没校准就做一次
    4. 文本注入能力自检（unicode_ok?）
    5. 全部 OK → 常驻 service 循环；否则打印哪一条前置没过 + 建议

运维观察：浏览器打开 http://<host>:PORT/messenger-rpa 看设备 + 审批 + run 历史。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.utils.config_manager import ConfigManager  # noqa: E402

logger = logging.getLogger("msgr_auto")


def _print(step: str, ok: bool, detail: str = "") -> None:
    mark = "[OK]" if ok else "[!! ]"
    sys.stdout.write(f"{mark} {step:<28} {detail}\n")
    sys.stdout.flush()


async def preflight(
    serial: str, *, cfg: Dict[str, Any], auto_fix: bool = True
) -> Dict[str, Any]:
    """前置体检 + 自修复。返回每一步的状态。"""
    from src.integrations.messenger_rpa.device_health import (
        ensure_device_ready, probe_devices,
    )
    from src.integrations.line_rpa import adb_helpers as adb
    from src.integrations.messenger_rpa.text_input import precheck_text_input

    report: Dict[str, Any] = {"serial": serial, "steps": []}
    hard_fail = False

    # STEP 1. 设备健康
    probe = probe_devices([serial]).get(serial, {})
    if not probe.get("present"):
        if not auto_fix:
            _print("device online", False, "不在线（且未授权自修复）")
            report["steps"].append(("device", False, "not_present"))
            return {**report, "ok": False, "hard_fail": True}
        _print("device online", False, "尝试自修复（disconnect+connect / wake / unlock）...")
        ok, info = ensure_device_ready(
            serial,
            try_reconnect=bool(cfg.get("auto_reconnect", True)),
            try_wake=bool(cfg.get("auto_wake", True)),
            try_unlock_swipe=bool(cfg.get("auto_unlock_swipe", True)),
            max_attempts=int(cfg.get("device_max_attempts", 3)),
        )
        if not ok:
            last = (info.get("attempts") or [{}])[-1]
            _print(
                "device online", False,
                f"自修复失败：{last.get('error','')[:200]}",
            )
            report["steps"].append(("device", False, info))
            return {**report, "ok": False, "hard_fail": True}
        _print("device online", True, f"恢复 ({info.get('total_ms')}ms)")
        report["steps"].append(("device", True, info))
    else:
        _print(
            "device online", True,
            f"screen={probe.get('screen_on')} locked={probe.get('locked')}",
        )
        report["steps"].append(("device", True, probe))

    # STEP 2. AdbKeyboard
    if adb.is_adbkeyboard_installed(serial):
        _print("adbkeyboard", True, "已安装")
        report["steps"].append(("adbkeyboard", True, "installed"))
    else:
        if auto_fix:
            _print("adbkeyboard", False, "未装，自动 install tools/ADBKeyboard.apk...")
            info = adb.ensure_adbkeyboard_installed(
                serial,
                package=cfg.get("adb_keyboard_package")
                or "com.android.adbkeyboard",
                ime_component=cfg.get("adb_keyboard_ime")
                or "com.android.adbkeyboard/.AdbIME",
                auto_enable=True,
            )
            ok = bool(info.get("installed"))
            _print(
                "adbkeyboard",
                ok,
                f"steps={' → '.join(info.get('steps', []))}"
                f" {('err:' + info.get('error','')) if info.get('error') else ''}",
            )
            report["steps"].append(("adbkeyboard", ok, info))
            if not ok:
                # 不 hard fail：ASCII guard 会把含中文回复降级到审批
                pass
        else:
            _print("adbkeyboard", False, "未装（--check 模式不自修）")
            report["steps"].append(("adbkeyboard", False, "not_installed"))

    # STEP 3. 文本注入能力
    cap = precheck_text_input(
        serial,
        adb_keyboard_package=cfg.get("adb_keyboard_package")
        or "com.android.adbkeyboard",
        auto_install_adbkeyboard=False,  # 上一步已处理
    )
    _print(
        "text inject",
        bool(cap.get("unicode_ok")),
        f"paths={cap.get('available_paths', [])}",
    )
    report["steps"].append(("text_inject", bool(cap.get("unicode_ok")), cap))

    # STEP 4. Messenger 是否装 + 可启动
    pkg = cfg.get("messenger_package") or "com.facebook.orca"
    r = adb.run_adb(
        ["shell", "pm", "path", pkg], serial=serial, timeout=8.0
    )
    has_pkg = r.returncode == 0 and bool((r.stdout or "").strip())
    _print("messenger pkg", has_pkg, f"{pkg} {'installed' if has_pkg else 'MISSING'}")
    report["steps"].append(("messenger_pkg", has_pkg, r.stdout))
    if not has_pkg:
        hard_fail = True

    # STEP 5. Vision API 可用
    try:
        from src.vision_client import VisionClient  # noqa: F401
        vcfg = cfg.get("vision") or {}
        merged_vision = {**(cfg.get("_global_vision") or {}), **vcfg}
        key = merged_vision.get("api_key") or ""
        ok = bool(key)
        _print("vision api", ok, f"provider={merged_vision.get('provider','?')} key={'set' if ok else 'MISSING'}")
        report["steps"].append(("vision", ok, {"provider": merged_vision.get("provider")}))
        if not ok:
            hard_fail = True
    except Exception as ex:
        _print("vision api", False, f"import fail: {ex}")
        report["steps"].append(("vision", False, f"{ex}"))
        hard_fail = True

    # STEP 6. 校准文件
    workspace = ROOT
    calib_dir = workspace / "tmp_messenger_rpa" / "calibrations"
    calib_file = calib_dir / f"{serial.replace(':','_').replace('.','_')}.json"
    _print(
        "calibration",
        calib_file.exists(),
        str(calib_file) if calib_file.exists() else "缺失（首跑会自动扫）",
    )
    report["steps"].append(("calibration", calib_file.exists(), str(calib_file)))

    # STEP 7. 通知授权（可选）
    r = adb.run_adb(
        ["shell", "dumpsys", "notification_manager", "--nsl", pkg],
        serial=serial, timeout=8.0,
    )
    out = (r.stdout or "")
    notif_enabled = (
        r.returncode == 0 and "importance=0" not in out
    )
    _print(
        "notif listener",
        notif_enabled,
        "Messenger 通知可被监听（dumpsys 可读）" if notif_enabled else "dumpsys 无法读（通知唤起可能降级）",
    )
    report["steps"].append(("notif", notif_enabled, "ok" if notif_enabled else "dumpsys_unavailable"))

    report["ok"] = not hard_fail
    report["hard_fail"] = hard_fail
    return report


async def _build_skill_manager(cm: ConfigManager):
    """照搬 main.py 的初始化：SkillManager(config, ai_client) + await initialize()。"""
    from src.skills.skill_manager import SkillManager
    from src.ai.ai_client import AIClient

    ai = AIClient(cm)
    init = getattr(ai, "initialize", None)
    if callable(init):
        r = init()
        if hasattr(r, "__await__"):
            await r
    sk = SkillManager(cm, ai)
    init = getattr(sk, "initialize", None)
    if callable(init):
        r = init()
        if hasattr(r, "__await__"):
            await r
    return sk


async def run_auto_service(cm: ConfigManager) -> None:
    """拉起 service，阻塞运行直到 Ctrl+C。"""
    from src.integrations.messenger_rpa.service import MessengerRpaService

    skill = await _build_skill_manager(cm)

    svc = MessengerRpaService(
        config_manager=cm,
        skill_manager=skill,
        messenger_rpa_cfg=cm.get_messenger_rpa_config() or {},
    )
    started = await svc.start()
    if not started:
        _print("service.start", False, "启动失败（enabled/autostart 都 True 才会起）")
        return
    _print("service.start", True, "常驻中（Ctrl+C 退出）")

    try:
        while True:
            await asyncio.sleep(30)
            st = svc.status()
            ev = st.get("notif_event_count", 0)
            empty = st.get("consecutive_empty", 0)
            sys.stdout.write(
                f"[{time.strftime('%H:%M:%S')}] notif_events={ev} "
                f"empty_runs={empty} running={st.get('running')}\n"
            )
            sys.stdout.flush()
    except KeyboardInterrupt:
        print("\n[exit] Ctrl+C 收到，停止 service...")
    finally:
        await svc.stop()


async def run_once_only(cm: ConfigManager) -> Dict[str, Any]:
    """只跑一次 run_once 验证链路，不常驻。"""
    from src.integrations.messenger_rpa.service import MessengerRpaService

    skill = await _build_skill_manager(cm)
    svc = MessengerRpaService(
        config_manager=cm,
        skill_manager=skill,
        messenger_rpa_cfg=cm.get_messenger_rpa_config() or {},
    )
    r = await svc.trigger_once()
    print("\n=== run_once 结果 ===")
    for k in ("ok", "step", "chat_name", "peer_text", "reply_text", "error", "total_ms"):
        v = r.get(k)
        if v is not None:
            print(f"  {k:<12}: {str(v)[:200]}")
    return r


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    ap = argparse.ArgumentParser()
    ap.add_argument("serial", nargs="?", help="设备 adb serial（空=config.yaml）")
    ap.add_argument("--check", action="store_true", help="只自检不启动")
    ap.add_argument("--once", action="store_true", help="跑一次 run_once 后退出")
    ap.add_argument("--no-auto-fix", action="store_true", help="自检失败时不自动修复")
    args = ap.parse_args()

    cm = ConfigManager()
    if not await cm.load():
        _print("config.yaml", False, "加载失败")
        return 2
    cfg = cm.get_messenger_rpa_config() or {}
    cfg["_global_vision"] = (cm.config or {}).get("vision", {})

    serial = (args.serial or cfg.get("adb_serial") or "").strip()
    if not serial:
        _print("adb_serial", False, "config.yaml 未配 + 命令行未给")
        return 3

    sys.stdout.write(
        f"\n=== Messenger 全自动聊天 · 自检 · serial={serial} ===\n"
    )
    report = await preflight(serial, cfg=cfg, auto_fix=not args.no_auto_fix)
    print()

    if args.check:
        return 0 if report["ok"] else 1

    if report.get("hard_fail"):
        print("! 硬依赖未满足（设备/Messenger 包/vision key），拒绝启动 service。")
        print("  请先解决上面标 [!!] 的项再重试。")
        return 4

    # 软依赖（AdbKeyboard 未装等）允许继续 — ASCII guard 会兜
    if args.once:
        r = await run_once_only(cm)
        return 0 if r.get("ok") else 1

    print(">>> 启动常驻 service（reply_mode=auto，有新消息自动回）")
    print(">>> Web 控制台：浏览器打开 /messenger-rpa")
    print(">>> Ctrl+C 退出\n")
    await run_auto_service(cm)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
