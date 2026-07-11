"""命令行入口：``--check`` 配置体检、``--init`` 场景预设脚手架。

2026-07-11 从 main.py 原样抽取（行为不变），作为 God-file 拆分的 Stage 1。
仅被 main.py 的 ``__main__`` 调用。
"""
from __future__ import annotations

import sys
from pathlib import Path


def run_config_check(config_path: str = None) -> int:
    """``python main.py --check`` 干跑模式：仅加载并体检配置，不启动任何服务。

    返回进程退出码（有 error 级问题 → 1，否则 0），便于 CI / 部署脚本 gate。
    """
    import yaml

    from src.utils.config_check import check_config, format_report, has_errors
    from src.utils.config_manager import ConfigManager

    cm = ConfigManager(config_path)
    path = cm.config_path
    if not Path(path).exists():
        print(f"✗ 配置文件不存在: {path}")
        print("  → 复制 config/config.example.yaml 为 config/config.yaml 并填写")
        return 1
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        print(f"✗ YAML 解析失败: {path}\n  → {exc}")
        return 1

    print(f"配置文件: {path}")
    issues = check_config(config, config_path=path)
    print(format_report(issues, config=config if isinstance(config, dict) else None))
    return 1 if has_errors(issues) else 0


def run_init(preset: str, config_path: str = None, set_pairs=None, force: bool = False) -> int:
    """``python main.py --init [PRESET]`` 场景预设脚手架。

    无 PRESET → 列出可用预设并退出；有 PRESET → 生成 config.yaml + 跑自检闭环。
    """
    from src.utils.config_init import (
        describe_preset,
        list_presets,
        parse_set_args,
        scaffold_config,
    )
    from src.utils.config_check import format_report, has_errors
    from src.utils.config_manager import ConfigManager

    presets = list_presets()
    if not preset:
        print("可用场景预设（python main.py --init <名称>）:")
        for name in presets:
            desc = describe_preset(name)
            print(f"  - {name:<12} {desc}")
        if not presets:
            print("  （config/presets/ 下暂无预设）")
        return 0

    if preset not in presets:
        print(f"✗ 未知预设: {preset}；可用: {', '.join(presets) or '（无）'}")
        return 1

    dest = Path(config_path) if config_path else ConfigManager().config_path
    if str(dest).endswith("config.example.yaml"):
        # 默认路径在 config.yaml 不存在时会回落 example；--init 应写 config.yaml
        dest = Path(dest).parent / "config.yaml"

    overrides = parse_set_args(set_pairs)
    # 交互补填关键空位（仅 TTY；非交互/CI 走 --set）
    if sys.stdin.isatty():
        if "ai.api_key" not in overrides:
            ans = input("AI api_key（回车跳过，稍后手填）: ").strip()
            if ans:
                overrides["ai.api_key"] = ans

    ok, msg, issues = scaffold_config(preset, dest, overrides=overrides, force=force)
    print(msg)
    if not ok:
        return 1
    print(format_report(issues, config=None))
    print("\n下一步: 编辑上面文件填好必填项，再运行 `python main.py --check` 复核。")
    return 1 if has_errors(issues) else 0
