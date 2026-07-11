# Install torch(cu128)+funasr into the existing ASR venv on 176 (run ON 176).
# RTX 5090 is sm_120 -> needs cu128 builds; wheels come from download.pytorch.org.
$ErrorActionPreference = 'Continue'
$py = 'C:\aitr_asr\venv\Scripts\python.exe'
$uv = "$env:USERPROFILE\.local\bin\uv.exe"
if (-not (Test-Path $uv)) { $uv = 'uv' }

Write-Output "== [1/3] torch+torchaudio cu128 =="
& $uv pip install -q --python $py torch torchaudio --index-url https://download.pytorch.org/whl/cu128
Write-Output "torch_install_exit=$LASTEXITCODE"

Write-Output "== [2/3] funasr + modelscope =="
& $uv pip install -q --python $py funasr modelscope
Write-Output "funasr_install_exit=$LASTEXITCODE"

Write-Output "== [3/3] sanity =="
& $py -c "import torch; print('torch', torch.__version__, 'cuda_ok', torch.cuda.is_available()); print('gpu', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none')"
& $py -c "import funasr, modelscope; print('funasr', funasr.__version__, 'modelscope', modelscope.__version__)"
Write-Output "DONE"
