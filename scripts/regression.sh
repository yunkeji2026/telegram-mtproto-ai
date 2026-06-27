#!/usr/bin/env bash
# 回归运行器（防陈旧字节码幽灵 flaky）。
#
# 背景：曾出现随机序下 test_*_event_alias 偶发失败——根因是旧 __pycache__/*.pyc
# （模块级常量与当前源码不一致）被加载。本脚本在跑测前清掉 src/tests 的
# __pycache__，并设 PYTHONDONTWRITEBYTECODE 禁止本次写新 .pyc，从源头杜绝污染。
#
# 用法：
#   scripts/regression.sh                  # 全量（-n auto，带超时兜底）
#   scripts/regression.sh tests/test_x.py  # 透传额外参数给 pytest
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo"
export PYTHONDONTWRITEBYTECODE=1
# 隔离运营态 web env：本机若设过 AITR_WEB_TOKEN/HOST/PORT（如手动起后端对齐桌面），
# config_manager 会读它覆盖测试令牌 → 误报一片 401。回归进程里清掉。
unset AITR_WEB_TOKEN AITR_WEB_HOST AITR_WEB_PORT 2>/dev/null || true
find src tests -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
if [ "$#" -gt 0 ]; then
    python -m pytest "$@" -q --timeout=90 --timeout-method=thread
else
    python -m pytest tests/ -n auto -q --timeout=90 --timeout-method=thread
fi
