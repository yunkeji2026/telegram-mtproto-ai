# 翻译回译质量周批：本地 MT(temp=0 可复现) + 语义轨，摘要追加 JSONL 攒趋势。
# 注册：schtasks /Create /TN TranslationEvalWeekly /SC WEEKLY /D SAT /ST 06:30 /F ^
#   /TR "powershell -ExecutionPolicy Bypass -File d:\workspace\telegram-mtproto-ai\scripts\translation_eval_weekly.ps1"
# 端点/模型不可达时 run_eval 自动跳过(exit 0)，本脚本不报警——趋势线缺一周点即可见。
$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:PYTHONIOENCODING = "utf-8"
$trend = "logs/eval/translation_trend.jsonl"
$log = "logs/eval/weekly_run.log"
New-Item -ItemType Directory -Force -Path "logs/eval" | Out-Null
$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $log -Value "[$ts] weekly translation eval start"

# 默认 10 样本集（与门禁同口径，看窄集趋势）
python -m scripts.run_eval --translation --xlate-engine ollama_mt `
    --out-jsonl $trend *>> $log
# 宽语种 44 样本集（17 语双向覆盖，看弱语对趋势；summary.by_pair 随行携带，
# 弱语对→per_lang_order 覆写决策直接从趋势行读数）
python -m scripts.run_eval --translation --xlate-engine ollama_mt `
    --dataset config/eval/translation_samples_hymt.yaml `
    --out-jsonl $trend *>> $log
# 宽语料交叉回译口径（回译走 ai/DeepSeek，消同引擎自洽虚高；行内 back_engine=ai
# 与上一行区分）。ai 不可达时 run_eval 自动跳过，趋势缺点即可见。
python -m scripts.run_eval --translation --xlate-engine ollama_mt `
    --xlate-back-engine ai `
    --dataset config/eval/translation_samples_hymt.yaml `
    --out-jsonl $trend *>> $log

$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $log -Value "[$ts] weekly translation eval done"
