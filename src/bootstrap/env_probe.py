"""启动期环境/配置探测辅助（纯函数，无副作用）。

2026-07-12 从 main.py 原样抽取（行为不变），作为 God-file 拆分的 Stage 1.5：
  - _resolve_mobile_auto_openclaw_db: 解析 mobile-auto0423 的 openclaw.db 路径
  - _telegram_configured: 判断 Telegram 协议号是否已真实配置（区别占位/缺省）
  - _is_desktop_mode: 桌面/自包含模式开关（env AITR_DESKTOP_MODE 或 config app.desktop_mode）

main.py 以 ``from src.bootstrap.env_probe import ...`` 引入，保持 ``main._xxx`` 可访问
（tests/test_desktop_boot_gate.py 依赖此命名）。
"""
from __future__ import annotations

import os
from pathlib import Path


def _resolve_mobile_auto_openclaw_db(config, config_path) -> str:
    """Resolve mobile-auto0423 openclaw.db with workspace-adjacent defaults."""
    root = config if isinstance(config, dict) else {}
    bridge_cfg = root.get("mobile_bridge") if isinstance(root.get("mobile_bridge"), dict) else {}
    explicit = str(bridge_cfg.get("openclaw_db_path") or "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_absolute():
            p = Path(config_path).parent / p
        return str(p)

    mr_cfg = root.get("messenger_rpa") if isinstance(root.get("messenger_rpa"), dict) else {}
    ma_cfg = mr_cfg.get("mobile_auto") if isinstance(mr_cfg.get("mobile_auto"), dict) else {}
    candidates = []
    ma_db = str(ma_cfg.get("openclaw_db_path") or "").strip()
    if ma_db:
        candidates.append(Path(ma_db).expanduser())
    ma_root = str(ma_cfg.get("root_path") or mr_cfg.get("mobile_auto_root") or "").strip()
    if ma_root:
        candidates.append(Path(ma_root).expanduser() / "data" / "openclaw.db")

    cfg_path = Path(config_path).resolve()
    repo_root = cfg_path.parent.parent
    workspace_root = repo_root.parent
    candidates.extend([
        workspace_root / "mobile-auto0423" / "data" / "openclaw.db",
        repo_root / "mobile-auto0423" / "data" / "openclaw.db",
    ])

    for p in candidates:
        try:
            if p.exists():
                return str(p)
        except Exception:
            continue
    return str(candidates[0]) if candidates else str(
        workspace_root / "mobile-auto0423" / "data" / "openclaw.db"
    )


def _telegram_configured(tg_cfg) -> bool:
    """判断 Telegram 协议号是否「已真实配置」（区别于占位/缺省）。

    打包/桌面自包含部署常用 config.example.yaml 自播种，其 telegram 为占位（YOUR_*）；
    此时应跳过协议客户端初始化（否则用占位 api_id 连接会失败/挂起，挡住整个进程启动），
    而统一收件箱 / 内嵌网页翻译 / RPA / QR 扫码登录协议号 均不依赖此 config 账号。
    """
    if not isinstance(tg_cfg, dict):
        return False
    api_id = str(tg_cfg.get("api_id") or "").strip()
    api_hash = str(tg_cfg.get("api_hash") or "").strip()
    phone = str(tg_cfg.get("phone_number") or "").strip()
    if not api_id or not api_hash or not phone:
        return False
    # 占位值（example 模板）视为未配置
    for v in (api_id, api_hash):
        if v.upper().startswith("YOUR_"):
            return False
    return True


def _is_desktop_mode(config_obj) -> bool:
    """桌面/自包含模式开关：env ``AITR_DESKTOP_MODE`` 或 config ``app.desktop_mode``。

    打开后强制跳过 config-Telegram 协议号初始化（即便填了真凭证），用于「纯桌面收件箱/翻译」形态。
    """
    if str(os.environ.get("AITR_DESKTOP_MODE") or "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    app_cfg = (config_obj or {}).get("app") if isinstance(config_obj, dict) else None
    return bool(isinstance(app_cfg, dict) and app_cfg.get("desktop_mode", False))
