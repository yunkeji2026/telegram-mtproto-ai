# health_report_task.ps1 — 计划任务入口：每日健康快照追加到 logs\health_trend.jsonl
# 注册（管理员或当前用户均可）：
#   schtasks /Create /F /TN "AITR_HealthReport" /SC DAILY /ST 09:00 `
#     /TR "powershell -NoProfile -ExecutionPolicy Bypass -File D:\workspace\telegram-mtproto-ai\scripts\health_report_task.ps1"
$ErrorActionPreference = "SilentlyContinue"
Set-Location (Split-Path $PSScriptRoot -Parent)
python -m scripts.health_report --jsonl logs\health_trend.jsonl
