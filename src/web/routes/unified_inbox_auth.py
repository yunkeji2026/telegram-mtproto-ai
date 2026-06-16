"""统一收件箱——坐席身份 / 主管权限 / 跟进事件基座（巨石拆分 slice 3）。

从 ``unified_inbox_routes.py`` 抽出的**会话身份与权限基座**：解析 session 坐席、
主管门槛判定与守卫、跟进事件发布、WebUserStore 惰性加载。

这一层被大量上层 helper（关系/Copilot/SLA 上下文构建）依赖，故按依赖顺序**先于**
那些上层 helper 外移——它本身只依赖 fastapi/stdlib + 惰性跨模块 import，是 leaf。
routes.py 等价重导出，对外引用路径保持不变。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

_SUPERVISOR_ROLES = {"master", "admin"}


def _session_agent(request: Request) -> Dict[str, str]:
    """从 session 解析当前坐席身份（无 SessionMiddleware 时回落 agent）。"""
    sess: Dict[str, Any] = {}
    try:
        if "session" in request.scope:
            sess = dict(request.session)
    except Exception:
        sess = {}
    uid = str(sess.get("user_id") or sess.get("username") or "agent")
    name = sess.get("display_name") or sess.get("username") or uid
    role = str(sess.get("role") or "")
    return {"agent_id": uid, "display_name": str(name or uid), "role": role}


def _is_supervisor(request: Request) -> bool:
    """主管能力 = 角色属于 master/admin（管理向功能的统一门槛）。"""
    return _session_agent(request).get("role", "") in _SUPERVISOR_ROLES


def _require_supervisor(request: Request) -> None:
    """主管专属端点守卫；非主管抛 403。"""
    if not _is_supervisor(request):
        raise HTTPException(403, "需要主管权限")


def _publish_follow_up(action: str, *, contact_id: str = "", task_id: str = "",
                       assignee: str = "") -> None:
    """发布跟进任务变更事件（SSE 实时刷新待办徽标）。失败静默。"""
    try:
        from src.integrations.shared.event_bus import get_event_bus
        get_event_bus().publish("follow_up", {
            "action": action, "contact_id": contact_id,
            "task_id": task_id, "assignee": assignee,
        })
    except Exception:
        logger.debug("follow_up 事件发布失败（已忽略）", exc_info=True)


def _agent_from_request(request: Request) -> Tuple[str, str]:
    agent_id = str(request.session.get("user_name") or request.session.get("username") or "")
    agent_name = str(request.session.get("display_name") or agent_id)
    return agent_id, agent_name


def _user_store_from_config(config_manager: Any) -> Any:
    """P48：惰性加载 WebUserStore（与 admin 同路径）。"""
    if config_manager is None:
        return None
    try:
        from src.utils.web_user_store import WebUserStore
        cfg_dir = config_manager.config_path.parent
        return WebUserStore(cfg_dir / "web_users.db")
    except Exception:
        return None
