<#
  华灵网站 · 本地一键部署 (Windows / PowerShell)
  流程: 打包 website/ -> SCP 上传(部署包 + deploy.sh) -> 服务器侧 deploy.sh 原子部署 -> 公网体检
  绝不在脚本中存放密码：从 $env:VPS_PASS 读取，缺失则安全提示输入(SecureString)。

  用法:
    cd website
    $env:VPS_PASS = '服务器密码'            # 可选；不设则运行时按提示输入
    ./scripts/deploy.ps1                     # 用默认主机/用户
    ./scripts/deploy.ps1 -VpsHost 1.2.3.4 -User ubuntu

  依赖: 本机 tar(Win10 自带) + Posh-SSH 模块(Install-Module Posh-SSH)
#>
param(
  [string]$VpsHost      = $(if ($env:VPS_HOST) { $env:VPS_HOST } else { '165.154.203.182' }),
  [string]$User         = $(if ($env:VPS_USER) { $env:VPS_USER } else { 'ubuntu' }),
  [string]$RemoteDir    = '/home/ubuntu',
  [string]$SiteUrl      = $(if ($env:SITE_URL) { $env:SITE_URL } else { 'https://usdt2026.cc' })
)

$ErrorActionPreference = 'Stop'
Import-Module Posh-SSH -ErrorAction Stop

$WebRoot = Split-Path -Parent $PSScriptRoot   # scripts/ 的上级 = website/
$tar = Join-Path $env:TEMP 'website-deploy.tar.gz'

Push-Location $WebRoot
try {
  Write-Host '[1/4] 打包 website/ (排除 node_modules/.next/.git/.env.local/临时文件) ...'
  if (Test-Path $tar) { Remove-Item $tar -Force }
  tar -czf $tar --exclude=node_modules --exclude=.next --exclude=.git --exclude=.env.local `
      "--exclude=*.tsbuildinfo" "--exclude=*.log" --exclude=og-test.png --exclude=test-fill.png .
  if ($LASTEXITCODE -ne 0) { throw '打包失败' }
  Write-Host ("    包大小 {0:N1} MB" -f ((Get-Item $tar).Length / 1MB))

  if ($env:VPS_PASS) { $sec = ConvertTo-SecureString $env:VPS_PASS -AsPlainText -Force }
  else { $sec = Read-Host "VPS 密码 ($User@$VpsHost)" -AsSecureString }
  $cred = New-Object System.Management.Automation.PSCredential($User, $sec)

  Write-Host '[2/4] 上传部署包 + deploy.sh ...'
  Set-SCPItem -ComputerName $VpsHost -Credential $cred -Path $tar -Destination $RemoteDir -AcceptKey -Force
  Set-SCPItem -ComputerName $VpsHost -Credential $cred -Path (Join-Path $PSScriptRoot 'deploy.sh') -Destination $RemoteDir -AcceptKey -Force

  Write-Host '[3/4] 服务器侧原子部署 (deploy.sh) ...'
  $s = New-SSHSession -ComputerName $VpsHost -Credential $cred -AcceptKey -ConnectionTimeout 30
  try {
    # 规避 Windows CRLF 导致 bash 解析失败
    $r = Invoke-SSHCommand -SessionId $s.SessionId -Command "sed -i 's/\r$//' $RemoteDir/deploy.sh && bash $RemoteDir/deploy.sh $RemoteDir/website-deploy.tar.gz"
    $r.Output
    if ($r.ExitStatus -ne 0) { throw "服务器部署返回非 0: $($r.ExitStatus)`n$($r.Error)" }
  } finally { Remove-SSHSession -SessionId $s.SessionId | Out-Null }

  Write-Host '[4/4] 公网体检 ...'
  $h = Invoke-RestMethod "$SiteUrl/api/health" -TimeoutSec 20
  Write-Host ("    healthy={0}  webhookSecret={1}  adminKey={2}  deepseek={3}" -f `
      $h.healthy, $h.checks.env.webhookSecret, $h.checks.env.adminKey, $h.checks.env.deepseekKey)
  if (-not $h.healthy) { throw '公网健康检查未通过' }
  Write-Host '部署完成 [OK]'
}
finally {
  Pop-Location
  if (Test-Path $tar) { Remove-Item $tar -Force -ErrorAction SilentlyContinue }
}
