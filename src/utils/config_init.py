"""场景预设脚手架（P0-2）— 把「1190 行 YAML」变成「选场景 + 填 3 空」。

``config/presets/*.yaml`` 是按业务场景精简、重注释的开局配置；本模块提供：

- :func:`list_presets` — 扫描可用预设
- :func:`load_preset` — 读取某预设（含元信息）
- :func:`apply_overrides` — 按点分键写入（如 ``ai.api_key=sk-...``）
- :func:`scaffold_config` — 复制预设到目标 + 应用覆盖 + 跑 P0-1 自检，闭环

核心为纯逻辑（无交互、无全局状态），交互式 ``--init`` 在 ``main.py`` 薄封装。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from src.utils.config_check import Issue, check_config


def _default_presets_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "config" / "presets"


def list_presets(presets_dir: Optional[Any] = None) -> List[str]:
    """返回可用预设名（不含 .yaml），按字母序。"""
    d = Path(presets_dir) if presets_dir else _default_presets_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.yaml") if p.is_file())


def preset_path(name: str, presets_dir: Optional[Any] = None) -> Path:
    d = Path(presets_dir) if presets_dir else _default_presets_dir()
    return d / f"{name}.yaml"


def load_preset(name: str, presets_dir: Optional[Any] = None) -> Dict[str, Any]:
    """读取预设为 dict；不存在或非字典抛 ValueError。"""
    p = preset_path(name, presets_dir)
    if not p.exists():
        avail = ", ".join(list_presets(presets_dir)) or "（无）"
        raise ValueError(f"预设不存在: {name}；可用: {avail}")
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"预设 {name} 不是有效的 YAML 字典")
    return data


def describe_preset(name: str, presets_dir: Optional[Any] = None) -> str:
    """取预设文件首段注释作为一句话说明（给 --init 列表展示）。"""
    p = preset_path(name, presets_dir)
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("# 场景预设"):
                    return s.lstrip("# ").strip()
    except Exception:
        pass
    return ""


def apply_overrides(config: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    """按点分键就地写入覆盖值（``ai.api_key`` → config['ai']['api_key']）。返回同一 dict。"""
    for dotted, value in (overrides or {}).items():
        keys = dotted.split(".")
        cur = config
        for k in keys[:-1]:
            nxt = cur.get(k)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[k] = nxt
            cur = nxt
        cur[keys[-1]] = value
    return config


def parse_set_args(pairs: Optional[List[str]]) -> Dict[str, Any]:
    """把 CLI 的 ``--set a.b=c`` 列表解析为 {"a.b": "c"}。非法项忽略。"""
    out: Dict[str, Any] = {}
    for item in pairs or []:
        if "=" not in item:
            continue
        key, _, val = item.partition("=")
        key = key.strip()
        if key:
            out[key] = val.strip()
    return out


def scaffold_config(
    preset: str,
    dest: Any,
    *,
    overrides: Optional[Dict[str, Any]] = None,
    force: bool = False,
    presets_dir: Optional[Any] = None,
) -> Tuple[bool, str, List[Issue]]:
    """用预设生成配置文件。

    返回 ``(ok, message, issues)``：
    - ``ok=False`` 且 issues 为空：写入被拒（目标已存在且非 force，或预设错误）
    - ``ok=True``：已写入 dest，issues 为对生成结果跑 :func:`check_config` 的结论
    """
    dest_path = Path(dest)
    if dest_path.exists() and not force:
        return False, f"目标已存在: {dest_path}（加 --force 覆盖）", []
    try:
        config = load_preset(preset, presets_dir)
    except ValueError as exc:
        return False, str(exc), []

    apply_overrides(config, overrides or {})

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_path.with_suffix(dest_path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        with open(tmp, "r", encoding="utf-8") as f:
            yaml.safe_load(f)  # 写后校验可解析
        tmp.replace(dest_path)
    except Exception as exc:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False, f"写入失败: {exc}", []

    issues = check_config(config, config_path=dest_path)
    return True, f"已生成 {dest_path}", issues
