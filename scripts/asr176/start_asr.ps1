# Start ASR server (run by scheduled task AITR_ASR_176 at boot, as SYSTEM).
$base = 'C:\aitr_asr'
$env:AITR_ASR_MODEL = 'large-v3-turbo'
$env:AITR_ASR_DEVICE = 'cuda'
$env:AITR_ASR_COMPUTE = 'float16'
$env:AITR_ASR_PORT = '8765'
# Speech emotion (emotion2vec on CUDA, /v1/audio/emotion). Set 'off' to disable.
# Local dir (downloaded on 117, scp'd over): both modelscope (179kB/s) and hf-mirror
# (connect fail) proved unusable from this host — never rely on hub downloads here.
$env:AITR_SER_MODEL = 'C:\aitr_asr\models\emotion2vec_plus_large'
$env:AITR_SER_DEVICE = 'cuda'
# 176 has a pre-existing machine-wide HF cache on D: with large-v3-turbo already in it
# (SYSTEM has Full control). New models: prefetch via prefetch_model.py /
# prefetch_emotion.py as the interactive user, don't rely on SYSTEM-context
# downloads (its network context proved flaky).
$env:HF_HOME = 'D:\cache\huggingface'
$env:MODELSCOPE_CACHE = 'D:\cache\modelscope'
if (-not $env:HF_ENDPOINT) { $env:HF_ENDPOINT = 'https://hf-mirror.com' }
# cuDNN/cuBLAS pip wheels: put their DLL dirs on PATH for ctranslate2
$nv = Get-ChildItem "$base\venv\Lib\site-packages\nvidia" -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { Join-Path $_.FullName 'bin' } | Where-Object { Test-Path $_ }
if ($nv) { $env:Path = ($nv -join ';') + ';' + $env:Path }

$log = "$base\logs\asr_$(Get-Date -Format yyyyMMdd).log"
& "$base\venv\Scripts\python.exe" "$base\asr_server.py" *>> $log
