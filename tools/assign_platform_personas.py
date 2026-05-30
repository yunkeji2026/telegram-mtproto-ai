#!/usr/bin/env python3
"""
tools/assign_platform_personas.py
----------------------------------
一键将 5 个示例人设分配给 TG / Messenger / WhatsApp / LINE 平台账号。

使用方法（在项目根目录运行）：
    python tools/assign_platform_personas.py

效果：
  - 读取 config/config.yaml
  - 将 persona_ids 写入各平台账号配置
  - 原文件备份为 config/config.yaml.bak
  - 重启服务器后生效

人设分配方案：
  LINE        → lin_xiaoyu     (22岁 大学生 林小雨  — 活泼 emoji 风)
  Messenger   → chen_meiling   (35岁 营销总监 陈美玲 — 简洁专业)
  WhatsApp    → haruko_traveler(28岁 旅行博主 晴子   — 感性文艺)
  Telegram    → marcus_wei     (42岁 金融顾问 Marcus — 严谨理性)
              → zhao_laoshi    (58岁 退休教师 赵老师 — 温和耐心，用于群聊)
"""

import sys
import shutil
from pathlib import Path

# ── 人设分配映射 ────────────────────────────────────────────────
PLATFORM_PERSONA_MAP = {
    "line_rpa":      ["lin_xiaoyu"],
    "messenger_rpa": ["chen_meiling"],
    "whatsapp_rpa":  ["haruko_traveler"],
    "telegram":      ["marcus_wei"],   # 私聊默认
    # 群聊账号可再加 zhao_laoshi（见下方注释）
}

# TG 多账号时：第一个账号 → marcus_wei，其余账号 → zhao_laoshi
TG_FALLBACK_PERSONA = "zhao_laoshi"

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"


def _load_yaml(path: Path):
    try:
        import yaml
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except ImportError:
        print("❌ 需要 pyyaml：pip install pyyaml")
        sys.exit(1)
    except FileNotFoundError:
        print(f"❌ 找不到配置文件：{path}")
        sys.exit(1)


def _dump_yaml(data, path: Path):
    import yaml
    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _backup(path: Path):
    bak = path.with_suffix(".yaml.bak")
    shutil.copy2(path, bak)
    print(f"  ✅ 已备份原文件 → {bak.name}")


def assign_telegram(cfg: dict) -> int:
    tg = cfg.get("telegram") or {}
    changed = 0
    accounts = tg.get("accounts") or []
    if accounts:
        for i, acc in enumerate(accounts):
            pid = PLATFORM_PERSONA_MAP["telegram"][0] if i == 0 else TG_FALLBACK_PERSONA
            old = acc.get("persona_ids") or []
            if old != [pid]:
                acc["persona_ids"] = [pid]
                changed += 1
                print(f"  TG 账号[{i}] {acc.get('id','?')} → {pid}")
    else:
        # 单账号模式：在 telegram 顶层写 persona_ids
        old = tg.get("persona_ids") or []
        pid = PLATFORM_PERSONA_MAP["telegram"][0]
        if old != [pid]:
            tg["persona_ids"] = [pid]
            cfg["telegram"] = tg
            changed += 1
            print(f"  TG 单账号 → {pid}")
    return changed


def assign_messenger(cfg: dict) -> int:
    mrpa = cfg.get("messenger_rpa") or {}
    if not mrpa:
        print("  ⚠️  config.yaml 中未找到 messenger_rpa，跳过")
        return 0
    changed = 0
    accounts = mrpa.get("accounts") or []
    pid = PLATFORM_PERSONA_MAP["messenger_rpa"][0]
    if accounts:
        for i, acc in enumerate(accounts):
            old = acc.get("persona_ids") or []
            if old != [pid]:
                acc["persona_ids"] = [pid]
                changed += 1
                print(f"  Messenger 账号[{i}] {acc.get('account_id','?')} → {pid}")
    else:
        old = mrpa.get("persona_ids") or []
        if old != [pid]:
            mrpa["persona_ids"] = [pid]
            cfg["messenger_rpa"] = mrpa
            changed += 1
            print(f"  Messenger（顶层）→ {pid}")
    return changed


def assign_whatsapp(cfg: dict) -> int:
    wa = cfg.get("whatsapp_rpa") or {}
    if not wa or not wa.get("enabled"):
        print("  ⚠️  config.yaml 中 whatsapp_rpa 未启用或不存在，跳过")
        return 0
    changed = 0
    accounts = wa.get("accounts") or []
    pid = PLATFORM_PERSONA_MAP["whatsapp_rpa"][0]
    if accounts:
        for i, acc in enumerate(accounts):
            old = acc.get("persona_ids") or []
            if old != [pid]:
                acc["persona_ids"] = [pid]
                changed += 1
                print(f"  WhatsApp 账号[{i}] {acc.get('account_id','?')} → {pid}")
    else:
        old = wa.get("persona_ids") or []
        if old != [pid]:
            wa["persona_ids"] = [pid]
            cfg["whatsapp_rpa"] = wa
            changed += 1
            print(f"  WhatsApp（顶层）→ {pid}")
    return changed


def assign_line(cfg: dict) -> int:
    # LINE RPA 通过 bindings_runtime.yaml 绑定（已写入），此处仅作提示
    line_cfg = cfg.get("line_rpa") or {}
    chat_key = line_cfg.get("chat_key", "line_rpa:default")
    use_backend = line_cfg.get("use_backend_persona", True)
    pid = PLATFORM_PERSONA_MAP["line_rpa"][0]
    print(f"  LINE RPA chat_key={chat_key!r}  use_backend_persona={use_backend}")
    print(f"  → 已通过 bindings_runtime.yaml 绑定 {pid}（无需修改 config.yaml）")
    return 0


def main():
    print(f"\n{'='*52}")
    print("  人设平台分配工具 — assign_platform_personas.py")
    print(f"{'='*52}")
    print(f"\n  读取配置：{CONFIG_PATH}\n")

    cfg = _load_yaml(CONFIG_PATH)
    _backup(CONFIG_PATH)

    total = 0
    print("【Telegram】")
    total += assign_telegram(cfg)
    print("【Messenger RPA】")
    total += assign_messenger(cfg)
    print("【WhatsApp RPA】")
    total += assign_whatsapp(cfg)
    print("【LINE RPA】")
    total += assign_line(cfg)

    if total > 0:
        _dump_yaml(cfg, CONFIG_PATH)
        print(f"\n  ✅ 共修改 {total} 处，已写回 {CONFIG_PATH.name}")
        print("  ⚡ 重启服务器后人设绑定生效：python main.py")
    else:
        print(f"\n  ✅ 配置已是最新，无需修改")

    print("\n人设概要：")
    profiles = {
        "lin_xiaoyu":      ("林小雨",   22, "大学生",     "LINE"),
        "chen_meiling":    ("陈美玲",   35, "营销总监",   "Messenger"),
        "haruko_traveler": ("晴子",     28, "旅行博主",   "WhatsApp"),
        "marcus_wei":      ("Marcus Wei", 42, "金融顾问", "Telegram (私聊)"),
        "zhao_laoshi":     ("赵老师",   58, "退休教师",   "Telegram (群聊/备用)"),
    }
    for pid, (name, age, role, plat) in profiles.items():
        print(f"  {pid:<22} {name:<12} {age}岁  {role:<10}  → {plat}")
    print()


if __name__ == "__main__":
    main()
