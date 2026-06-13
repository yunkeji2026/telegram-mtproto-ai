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

  Write-Host '[3/4] 服务器侧原子部署 (后台执行 deploy.sh + 轮询日志，避免长构建占用 SSH 通道超时) ...'
  $s = New-SSHSession -ComputerName $VpsHost -Credential $cred -AcceptKey -ConnectionTimeout 30
  try {
    # sed: 规避 Windows CRLF 致 bash 解析失败；nohup+&+</dev/null: 完全脱离终端后台运行
    $launch = "sed -i 's/\r$//' $RemoteDir/deploy.sh; cd $RemoteDir && rm -f deploy.log && nohup bash deploy.sh $RemoteDir/website-deploy.tar.gz >deploy.log 2>&1 </dev/null & echo launched"
    Invoke-SSHCommand -SessionId $s.SessionId -Command $launch | Out-Null

    $deadline = (Get-Date).AddMinutes(10); $done = $false
    do {
      Start-Sleep -Seconds 8
      # pgrep '[b]ash' 括号技巧避免匹配到 pgrep 自身命令行
      $p = (Invoke-SSHCommand -SessionId $s.SessionId -Command "tail -4 $RemoteDir/deploy.log 2>/dev/null; pgrep -f '[b]ash deploy.sh' >/dev/null && echo __RUN__ || echo __STOP__").Output -join "`n"
      Write-Host ('    ' + (($p -replace '__RUN__|__STOP__','').Trim() -replace "`n","`n    "))
      if ($p -match 'DONE @')                     { $done = $true; break }
      if ($p -match 'deploy ERROR|rolling back')  { throw "服务器部署失败(已尝试自动回滚)，详见服务器 $RemoteDir/deploy.log" }
      if ($p -match '__STOP__') {
        $final = (Invoke-SSHCommand -SessionId $s.SessionId -Command "tail -25 $RemoteDir/deploy.log").Output -join "`n"
        if ($final -match 'DONE @') { $done = $true; break }
        throw "deploy.sh 已退出但未见 DONE，疑似中断：`n$final"
      }
    } until ((Get-Date) -gt $deadline)
    if (-not $done) { throw "部署轮询超时(>10min)，请上服务器查看 $RemoteDir/deploy.log" }
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
