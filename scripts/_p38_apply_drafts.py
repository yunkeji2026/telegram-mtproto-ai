"""P38：把 drafts_routes.py 的 70 处硬编码中文响应文案套用到共享/草稿域 err.* key。

运行：``python -m scripts._p38_apply_drafts``（幂等：再跑一次 total_replaced=0）。
映射经 curate：权限门（含 17 个功能前缀变体）统一归并到 err.perm.supervisor_required；
inbox/kb/草稿服务就绪 → err.svc.*；请求体解析 → err.req.bad_body；其余草稿域独立键。
"""
from __future__ import annotations

from pathlib import Path

from scripts.i18n_routeconv import convert_file

_ROOT = Path(__file__).resolve().parents[1]
_FILE = _ROOT / "src" / "web" / "routes" / "drafts_routes.py"

_PERM = 'HTTPException(403, tr(request, "err.perm.supervisor_required"))'
_INBOX = 'HTTPException(503, tr(request, "err.svc.inbox_not_ready"))'

# —— curated old→new（old 为源码精确片段，命中即全替）——
MAPPING = {
    # 权限门：明文 + 17 个「功能名+需要主管权限」变体，全部归并到通用权限键
    'HTTPException(403, "需要主管权限")': _PERM,
    'HTTPException(403, "批量处置需要主管权限")': _PERM,
    'HTTPException(403, "指标查看需要主管权限")': _PERM,
    'HTTPException(403, "术语库管理需要主管权限")': _PERM,
    'HTTPException(403, "趋势数据需要主管权限")': _PERM,
    'HTTPException(403, "A/B 测试管理需要主管权限")': _PERM,
    'HTTPException(403, "trace 列表需要主管权限")': _PERM,
    'HTTPException(403, "异常检测查看需要主管权限")': _PERM,
    'HTTPException(403, "工作负荷查看需要主管权限")': _PERM,
    'HTTPException(403, "KB 统计需要主管权限")': _PERM,
    'HTTPException(403, "质量统计需要主管权限")': _PERM,
    'HTTPException(403, "工作区管理需要主管权限")': _PERM,
    'HTTPException(403, "知识库归档需要主管权限")': _PERM,
    'HTTPException(403, "排行榜查看需要主管权限")': _PERM,
    'HTTPException(403, "广播需要主管权限")': _PERM,
    'HTTPException(403, "简报查看需要主管权限")': _PERM,
    'HTTPException(403, "数据导出需要主管权限")': _PERM,
    'HTTPException(403, "需要主管权限才能强制放行 L4 草稿")':
        'HTTPException(403, tr(request, "err.perm.supervisor_force_l4"))',
    'HTTPException(403, "坐席只能查看自己的绩效数据")':
        'HTTPException(403, tr(request, "err.perm.agent_self_only"))',
    # 服务就绪
    'HTTPException(503, "inbox_store 未就绪")': _INBOX,
    'HTTPException(503, "InboxStore 未挂载")': _INBOX,
    'HTTPException(503, "kb_store 未就绪")':
        'HTTPException(503, tr(request, "err.svc.kb_not_ready"))',
    'HTTPException(503, "草稿服务未启用")':
        'HTTPException(503, tr(request, "err.svc.draft_service_disabled"))',
    # 请求体
    'HTTPException(400, "请求体解析失败")': 'HTTPException(400, tr(request, "err.req.bad_body"))',
    'HTTPException(400, "请求体 JSON 解析失败")': 'HTTPException(400, tr(request, "err.req.bad_body"))',
    # 草稿域
    'HTTPException(404, "草稿不存在")': 'HTTPException(404, tr(request, "err.draft.not_found"))',
    'HTTPException(400, "action 须为 approve 或 reject")':
        'HTTPException(400, tr(request, "err.draft.bad_action"))',
    'HTTPException(400, "name / template_a_id / template_b_id 不能为空")':
        'HTTPException(400, tr(request, "err.draft.ab_fields_required"))',
    'HTTPException(400, "type 字段不能为空")':
        'HTTPException(400, tr(request, "err.draft.type_required"))',
    'HTTPException(400, "rec_id 不能为空")':
        'HTTPException(400, tr(request, "err.draft.rec_id_required"))',
    'HTTPException(400, "workspace_id 不能为空")':
        'HTTPException(400, tr(request, "err.draft.workspace_id_required"))',
    'HTTPException(400, "title 不能为空")':
        'HTTPException(400, tr(request, "err.draft.title_required"))',
    '"error": "草稿文本为空，无需翻译"':
        '"error": tr(request, "err.draft.text_empty_no_translate")',
    'HTTPException(404, f"测试 {test_id} 不存在或已结束")':
        'HTTPException(404, tr(request, "err.draft.test_not_found", id=test_id))',
    'HTTPException(404, f"trace_id={trace_id} 未找到")':
        'HTTPException(404, tr(request, "err.draft.trace_not_found", id=trace_id))',
    'HTTPException(500, f"KB 写入失败: {e}")':
        'HTTPException(500, tr(request, "err.draft.kb_write_failed", err=e))',
    'HTTPException(500, f"EventBus 发布失败: {e}")':
        'HTTPException(500, tr(request, "err.draft.eventbus_failed", err=e))',
    # note 字段（不在棘轮门禁范围，但同属用户可见，一并收口）
    '"note": "AutosendWorker 未启用"': '"note": tr(request, "err.draft.autosend_worker_off")',
}


def main() -> int:
    rep = convert_file(_FILE, MAPPING)
    print(f"[p38] total_replaced={rep['total_replaced']} import_added={rep['import_added']} "
          f"changed={rep['changed']}")
    if rep["unmatched"]:
        print(f"[p38] !! unmatched (map 脱节，请核对): {rep['unmatched']}")
        return 1
    print("[p38] OK — 所有 curate 片段均命中并替换。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
