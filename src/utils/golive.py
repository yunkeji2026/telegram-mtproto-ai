"""上线自检清单（P2-1）— 把各向导的「就绪信号」聚成一张上线红绿灯。

老板/运营在上线前最想要一个明确答复：「现在能开张了吗？」本模块把前面各阶段的
就绪信号——AI 已配置、渠道就绪、配置自检、知识库非空、坐席在线——聚合成一份
带总体红绿灯的清单，每项给出可直达的修复入口。

设计：:func:`build_checklist` 为**纯函数**（入参已是各子系统算好的轻量结果），
便于单测；采集这些入参的 I/O 留给路由层。状态三态：``ok`` / ``warn`` / ``fail``。
总体灯：任一 fail→red；无 fail 有 warn→yellow；全 ok→green。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

_PLACEHOLDER = ("your_", "<", "changeme", "xxxx", "请填写", "填写")


def _is_placeholder(val: Any) -> bool:
    s = str(val if val is not None else "").strip()
    if not s:
        return True
    low = s.lower()
    return any(t in low for t in _PLACEHOLDER)


def _check(id_: str, name: str, status: str, detail: str,
           action_url: str = "", action_label: str = "") -> Dict[str, Any]:
    return {"id": id_, "name": name, "status": status, "detail": detail,
            "action_url": action_url, "action_label": action_label}


def build_checklist(
    *,
    config: Dict[str, Any],
    channel_statuses: List[Dict[str, Any]],
    config_errors: int,
    config_warnings: int,
    kb_ready: Dict[str, Any],
    online_agents: int,
) -> Dict[str, Any]:
    """聚合上线清单（纯函数）。返回 {ok, light, ready, checks:[...], summary}。"""
    config = config or {}
    checks: List[Dict[str, Any]] = []

    # 1) AI 大模型已配置（provider + api_key 非占位）——硬性
    ai = (config.get("ai") or {})
    provider = str(ai.get("provider") or "").strip()
    api_key = ai.get("api_key")
    # 本地 Ollama 允许任意非空 key；这里只判「非占位」
    if provider and not _is_placeholder(api_key):
        checks.append(_check("ai", "AI 大模型已配置", "ok",
                             f"provider={provider}"))
    elif provider:
        checks.append(_check("ai", "AI 大模型已配置", "fail",
                             "已选 provider 但 api_key 为空/占位",
                             "/workspace/setup", "去配置"))
    else:
        checks.append(_check("ai", "AI 大模型已配置", "fail",
                             "未指定 AI provider",
                             "/workspace/setup", "去配置"))

    # 2) 至少一个渠道就绪——硬性
    ready_channels = [c for c in (channel_statuses or []) if c.get("ready")]
    if ready_channels:
        names = "、".join(c.get("name", c.get("id", "")) for c in ready_channels)
        checks.append(_check("channels", "至少一个渠道就绪", "ok",
                             f"已就绪：{names}"))
    else:
        configured = [c for c in (channel_statuses or []) if c.get("configured")]
        if configured:
            checks.append(_check("channels", "至少一个渠道就绪", "warn",
                                 "已填凭证但尚未启用/登录",
                                 "/workspace/setup", "去接入"))
        else:
            checks.append(_check("channels", "至少一个渠道就绪", "fail",
                                 "尚无渠道接入",
                                 "/workspace/setup", "去接入"))

    # 3) 配置自检通过——error 硬性 / warn 软性
    if config_errors > 0:
        checks.append(_check("config", "配置自检通过", "fail",
                             f"{config_errors} 个错误待修复",
                             "/workspace/setup", "查看"))
    elif config_warnings > 0:
        checks.append(_check("config", "配置自检通过", "warn",
                             f"{config_warnings} 个警告（可上线，建议修复）",
                             "/workspace/setup", "查看"))
    else:
        checks.append(_check("config", "配置自检通过", "ok", "无错误/警告"))

    # 4) 知识库非空——软性（纯 AI 也可答，但有私域知识更佳）
    if not kb_ready or not kb_ready.get("available"):
        checks.append(_check("kb", "知识库已就绪", "warn",
                             "知识库不可用或未初始化",
                             "/workspace/kb-start", "去播种"))
    elif kb_ready.get("is_cold"):
        checks.append(_check("kb", "知识库已就绪", "warn",
                             f"知识偏少（{kb_ready.get('enabled_entries', 0)} 条）",
                             "/workspace/kb-start", "去播种"))
    else:
        checks.append(_check("kb", "知识库已就绪", "ok",
                             f"{kb_ready.get('enabled_entries', 0)} 条知识"))

    # 5) 至少一名坐席在线——软性（AI 自动应答可无人值守）
    if online_agents > 0:
        checks.append(_check("agents", "坐席在线", "ok",
                             f"{online_agents} 人在线"))
    else:
        checks.append(_check("agents", "坐席在线", "warn",
                             "当前无坐席在线（纯 AI 自动应答仍可运行）"))

    # 6) N 线扫码陪聊就绪——仅当启用时纳入总表（开关一致性 + 反封号护栏，N2）
    try:
        from src.ops.companion_preflight import build_companion_preflight
        pf = build_companion_preflight(config)
        if pf.get("applicable"):
            s = pf.get("summary") or {}
            if pf.get("light") == "red":
                checks.append(_check("companion", "扫码陪聊就绪", "fail",
                                     f"{s.get('fail', 0)} 项阻断（开关一致性/凭证）",
                                     "/rpa", "查看"))
            elif pf.get("light") == "yellow":
                checks.append(_check("companion", "扫码陪聊就绪", "warn",
                                     f"{s.get('warn', 0)} 项建议（反封号闸门/代理）",
                                     "/rpa", "查看"))
            else:
                checks.append(_check("companion", "扫码陪聊就绪", "ok", "开关一致 + 护栏就绪"))
    except Exception:
        pass

    fails = sum(1 for c in checks if c["status"] == "fail")
    warns = sum(1 for c in checks if c["status"] == "warn")
    oks = sum(1 for c in checks if c["status"] == "ok")
    light = "red" if fails else ("yellow" if warns else "green")
    ready = fails == 0
    return {
        "ok": True,
        "light": light,
        "ready": ready,
        "checks": checks,
        "summary": {"ok": oks, "warn": warns, "fail": fails,
                    "total": len(checks)},
    }
