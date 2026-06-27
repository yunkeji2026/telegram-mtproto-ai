# 回归运行器（防陈旧字节码幽灵 flaky）。
#
# 背景：曾出现随机序下 test_*_event_alias 偶发失败——根因是上个会话遗留的旧
# __pycache__/*.pyc（模块级常量与当前源码不一致）被加载。本脚本在跑测前清掉
# src/tests 的 __pycache__，并设 PYTHONDONTWRITEBYTECODE 禁止本次写新 .pyc，
# 从源头杜绝该类污染。
#
# 用法：
#   scripts\regression.ps1                 # 全量（-n auto，带超时兜底）
#   scripts\regression.ps1 tests\test_x.py # 透传额外参数给 pytest
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    $env:PYTHONDONTWRITEBYTECODE = "1"
    # 隔离运营态 web env：本机若设过 AITR_WEB_TOKEN/HOST/PORT（如手动起后端对齐桌面），
    # config_manager 会读它覆盖测试令牌 → 误报一片 401。回归子进程里清掉。
    foreach ($k in "AITR_WEB_TOKEN", "AITR_WEB_HOST", "AITR_WEB_PORT") {
        Remove-Item "Env:$k" -ErrorAction SilentlyContinue
    }
    Get-ChildItem -Recurse -Directory -Filter "__pycache__" -Path src, tests `
        -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    if ($args.Count -gt 0) {
        python -m pytest @args -q --timeout=90 --timeout-method=thread
    } else {
        python -m pytest tests/ -n auto -q --timeout=90 --timeout-method=thread
    }
} finally {
    Pop-Location
}
