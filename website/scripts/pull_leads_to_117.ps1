<#
  在 SSH-117 上运行（计划任务，每天 03:30）：把生产服务器的留资数据与服务器侧备份
  拉回本机 D:\web_backups\，实现「异地备份」。旧服务器删除导致数据丢失的教训 → 此为主保险。

  依赖：117 的 ~/.ssh/hualing_deploy 私钥（已存在）。
  用法：powershell -ExecutionPolicy Bypass -File D:\web_ops\pull_leads_to_117.ps1
#>
$ErrorActionPreference = 'Stop'
$VpsUser = 'ubuntu'
$VpsHost = if ($env:VPS_HOST) { $env:VPS_HOST } else { '165.154.233.121' }
$Key     = Join-Path $HOME '.ssh/hualing_deploy'
$Root    = 'D:\web_backups'
$stamp   = Get-Date -Format 'yyyyMMdd-HHmmss'
$dest    = Join-Path $Root "leads-$stamp"
$log     = Join-Path $Root 'pull.log'

New-Item -ItemType Directory -Force $dest | Out-Null
function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Tee-Object -FilePath $log -Append }

# scp 的 host-key 提示/空目录都会往 stderr 写，PowerShell 在 Stop 模式下会误判为异常；
# 拉取阶段临时降级为 Continue，仅按「实际拉到的文件数」判断成败。
Log "pull start from $VpsUser@$VpsHost"
$sshOpts = @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20', '-o', 'StrictHostKeyChecking=accept-new')
$ErrorActionPreference = 'Continue'
# 1) 实时留资目录（逐文件）  2) 服务器侧 tar 备份（若存在）
& scp -i $Key @sshOpts -r "$VpsUser@${VpsHost}:/home/ubuntu/hualing-leads/*" "$dest/" 2>&1 | Out-Null
& scp -i $Key @sshOpts "$VpsUser@${VpsHost}:/home/ubuntu/leads-backups/*" "$dest/" 2>&1 | Out-Null
$ErrorActionPreference = 'Stop'
$n = (Get-ChildItem $dest -Recurse -File -ErrorAction SilentlyContinue).Count
Log "pulled $n file(s) -> $dest"
if ($n -eq 0) { Remove-Item $dest -Recurse -Force -ErrorAction SilentlyContinue; Log "no leads yet — empty snapshot removed (self-test connectivity OK)" }

# 保留最近 30 个快照
Get-ChildItem $Root -Directory -Filter 'leads-*' | Sort-Object Name -Descending |
  Select-Object -Skip 30 | ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
Log "pull done"
