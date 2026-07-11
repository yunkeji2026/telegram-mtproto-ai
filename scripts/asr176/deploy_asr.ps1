# Deploy faster-whisper GPU ASR on this machine (run ON 192.168.0.176).
# Idempotent. Creates C:\aitr_asr\{venv,logs,hf}, installs deps, firewall rule,
# scheduled task (ONSTART, SYSTEM), then starts the service.
$ErrorActionPreference = 'Stop'
$env:Path = "C:\Users\user\.local\bin;$env:Path"
$base = 'C:\aitr_asr'

New-Item -ItemType Directory -Force -Path $base, "$base\logs", "$base\hf" | Out-Null

# 1) uv-managed Python 3.12 venv
if (-not (Test-Path "$base\venv\Scripts\python.exe")) {
    uv venv --python 3.12 "$base\venv"
    if ($LASTEXITCODE -ne 0) { throw "uv venv failed" }
}

# 2) deps (faster-whisper pulls ctranslate2; cuDNN/cuBLAS wheels for CUDA)
uv pip install --python "$base\venv\Scripts\python.exe" `
    faster-whisper fastapi "uvicorn[standard]" python-multipart `
    nvidia-cublas-cu12 nvidia-cudnn-cu12
if ($LASTEXITCODE -ne 0) { throw "uv pip install failed" }

# 3) firewall (needs admin token; SSH session for 'user' has one)
if (-not (Get-NetFirewallRule -DisplayName 'AITR ASR 8765' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName 'AITR ASR 8765' -Direction Inbound `
        -Action Allow -Protocol TCP -LocalPort 8765 | Out-Null
    Write-Output 'firewall: rule created'
} else {
    Write-Output 'firewall: rule exists'
}

# 4) scheduled task: autostart at boot as SYSTEM (GPU compute works under SYSTEM)
schtasks /Create /F /TN 'AITR_ASR_176' /SC ONSTART /RU SYSTEM /RL HIGHEST `
    /TR "powershell -NoProfile -ExecutionPolicy Bypass -File $base\start_asr.ps1" | Out-Null
Write-Output 'task: AITR_ASR_176 registered'

# 5) (re)start now via the task so it runs in the same context as after reboot
schtasks /End /TN 'AITR_ASR_176' 2>$null | Out-Null
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'asr_server' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
schtasks /Run /TN 'AITR_ASR_176' | Out-Null
Write-Output 'task: started'
