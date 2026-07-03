"""LINE for PC 桌面客户端 UIAutomation 控件树探测脚本。

用途：LINE 官方**没有可嵌入的网页聊天端**，"官方一致"的多端只有「LINE for PC」桌面客户端
（手机扫码登录那个原生 Windows 应用）。要自动化它（会话列表 / 消息气泡 / 输入框 / 发送），
必须先知道它的 UIAutomation 控件树（ControlType / Name / AutomationId / ClassName / 坐标）。
这些**只能在装了 LINE for PC 并登录的本机、开着窗口时探测**，无法凭空盲写。

前置：
  1. 本机安装 LINE for PC 并用手机扫码登录，保持窗口打开（最好先点开一个会话）。
  2. 依赖 ``uiautomation``（本机已装）；缺失则 ``pip install uiautomation``。

用法（在仓库根目录）：
  python scripts/line_desktop_probe.py                     # 自动找 LINE 窗口，导出控件树
  python scripts/line_desktop_probe.py --depth 16          # 加深遍历（默认 14）
  python scripts/line_desktop_probe.py --title LINE        # 指定窗口标题匹配（默认自动）
  python scripts/line_desktop_probe.py --out logs/line_tree.txt

产出：控件树写到 ``logs/line_control_tree.txt``（含每个控件的类型/名称/AutomationId/坐标），
把这份文件回贴给我，我据此建 ``line_desktop_login.py`` 桌面自动化驱动（登录检测 + 收发）。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, List, Optional


def _find_line_windows(title_match: str) -> List[Any]:
    """找到 LINE 顶层窗口（优先按进程名 LINE.exe，回落按标题匹配）。"""
    import uiautomation as auto  # type: ignore

    wins: List[Any] = []
    root = auto.GetRootControl()
    for w in root.GetChildren():
        try:
            name = (w.Name or "")
            cls = (w.ClassName or "")
        except Exception:
            continue
        # 进程名兜底判断
        is_line_proc = False
        try:
            import psutil  # type: ignore
            pid = w.ProcessId
            if pid:
                pname = psutil.Process(pid).name().lower()
                is_line_proc = pname.startswith("line")
        except Exception:
            is_line_proc = False
        if is_line_proc or (title_match and title_match.lower() in name.lower()) \
                or cls in ("Qt5152QWindowIcon", "Qt5QWindowIcon") and name:
            wins.append(w)
    return wins


def _fmt(ctrl: Any) -> str:
    try:
        rect = ctrl.BoundingRectangle
        r = f"({rect.left},{rect.top},{rect.right},{rect.bottom})"
    except Exception:
        r = "()"
    parts = [
        ctrl.ControlTypeName if hasattr(ctrl, "ControlTypeName") else "",
        f"Name={ctrl.Name!r}" if getattr(ctrl, "Name", "") else "",
        f"AutomationId={ctrl.AutomationId!r}" if getattr(ctrl, "AutomationId", "") else "",
        f"ClassName={ctrl.ClassName!r}" if getattr(ctrl, "ClassName", "") else "",
        f"rect={r}",
    ]
    return " ".join(p for p in parts if p)


def _walk(ctrl: Any, depth: int, max_depth: int, lines: List[str], cap: List[int]) -> None:
    if depth > max_depth or cap[0] <= 0:
        return
    lines.append("  " * depth + _fmt(ctrl))
    cap[0] -= 1
    try:
        children = ctrl.GetChildren()
    except Exception:
        children = []
    for ch in children:
        _walk(ch, depth + 1, max_depth, lines, cap)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="LINE for PC 控件树探测")
    ap.add_argument("--title", default="LINE", help="窗口标题匹配（默认 LINE）")
    ap.add_argument("--depth", type=int, default=14, help="最大遍历深度（默认 14）")
    ap.add_argument("--max-nodes", type=int, default=6000, help="节点上限（防超大 dump）")
    ap.add_argument("--out", default=os.path.join("logs", "line_control_tree.txt"))
    args = ap.parse_args(argv)

    try:
        import uiautomation  # noqa: F401
    except Exception:
        print("缺少依赖 uiautomation：请先 `pip install uiautomation`", file=sys.stderr)
        return 2

    wins = _find_line_windows(args.title)
    if not wins:
        print("未找到 LINE 窗口。请确认：①已安装 LINE for PC；②已扫码登录；③窗口处于打开状态。",
              file=sys.stderr)
        return 1

    lines: List[str] = []
    for i, w in enumerate(wins):
        header = f"===== LINE window #{i}: {_fmt(w)} ====="
        print(header)
        lines.append(header)
        cap = [args.max_nodes]
        _walk(w, 0, args.depth, lines, cap)
        lines.append("")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n控件树已导出 → {args.out}（{len(lines)} 行）。请把该文件回贴给开发继续建驱动。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
