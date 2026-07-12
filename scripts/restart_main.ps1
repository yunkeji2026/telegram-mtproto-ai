# restart_main.ps1 — 标准化重启 main.py（就绪探测 + 窗口耗时报告 + 多实例防护）
#
# 为什么用它而不是手动 kill+start：
#   1. 重启窗口（进程拉起→web 后台可服务）约 15-30s，期间工作台请求全部失败——
#      本脚本轮询探测就绪并报告实际窗口时长，重启质量可度量；
#   2. **多实例防护**：先杀掉所有 main.py 并确认 18799 端口真正释放才起新进程。
#      2026-07-12 曾出现 3 个 main.py 并发（多条开发线各自起服务）——后起进程绑
#      18799/19199 失败（WinError 10048）成「半死实例」，且多个 Telegram client
#      并发消费同一 session 有会话冲突风险；
#   3. 起失败会大声报错（探测超时 exit 1），不会静默留下死机；
#   4. 统一日志命名（logs\restart_*.out/err.log），事后可追溯每次重启。
#
# ⚠ 先想清楚是否真的需要重启：
#   - 纯模板改动（src/web/templates/**.html 的 HTML/JS）**免重启**——Jinja2
#     auto_reload 已开（src/web/admin.py），刷新浏览器即生效；
#   - config.local.yaml 的多数运营开关由消费方每 tick 重读，热生效；
#   - .py / web_i18n.py 键 / config 主档改动才需要重启。
#
# 用法: powershell -ExecutionPolicy Bypass -File scripts\restart_main.ps1

$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot -Parent)

$port = 18799
$probeUrl = "http://127.0.0.1:$port/login"   # 无需鉴权的最轻页面
$t0 = Get-Date

# 1) 停掉**所有** main.py 实例（排除 IndexTTS2 等其他 python 常驻）
$old = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*main.py*' -and $_.CommandLine -notlike '*aitr_indextts2*' })
foreach ($p in $old) {
    Write-Host "[restart] stopping main.py PID=$($p.ProcessId)"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
if (-not $old.Count) { Write-Host "[restart] no running main.py found (cold start)" }
# 预期重启不算「无痕死亡」：Stop-Process -Force 是 TerminateProcess，进程内 atexit
# 不会执行 → 哨兵残留 → 下次启动误报「非正常死亡」。本脚本知道这是预期重启，
# 替进程清哨兵——残留语义从此纯净（只剩 OOM/手工 taskkill/崩溃/断电等真意外）。
if ($old.Count) {
    Start-Sleep -Milliseconds 500
    Remove-Item "logs\run_sentinel.json" -Force -ErrorAction SilentlyContinue
}

# 2) 确认端口真正释放（防「进程表没找到但端口仍被占」的幽灵实例；上限 20s）
$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline) {
    $own = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    if (-not $own.Count) { break }
    $pids = ($own | Select-Object -ExpandProperty OwningProcess -Unique)
    Write-Host "[restart] port $port still held by PID(s)=$($pids -join ',') — stopping"
    foreach ($holder in $pids) { Stop-Process -Id $holder -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}
$own = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
if ($own.Count) {
    Write-Host "[restart] ABORT: port $port cannot be freed" -ForegroundColor Red
    exit 1
}

# 3) 起新进程（日志命名与历史 restart_*.log 一致）
# 用 Win32_Process.Create 而非 Start-Process：后者让 python 继承本 shell 的
# stdout/stderr 管道句柄（即便已 -Redirect* 到文件）——凡是**捕获本脚本输出**的调用方
# （CI/自动化工具）会因管道永不 EOF 而挂死等待，只能手动杀 runner。
# CIM Create 不继承句柄，重定向经 cmd 落文件，本脚本打印 READY 后自然退出。
New-Item -ItemType Directory -Force -Path logs | Out-Null
$ts  = Get-Date -Format "yyyyMMdd_HHmmss"
$out = "logs\restart_$ts.out.log"
$err = "logs\restart_$ts.err.log"
$cwd = (Get-Location).Path
$cmdline = "cmd.exe /c python main.py > `"$cwd\$out`" 2> `"$cwd\$err`""
$res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = $cmdline; CurrentDirectory = $cwd }
if (-not $res -or $res.ReturnValue -ne 0) {
    Write-Host "[restart] ABORT: process create failed rv=$($res.ReturnValue)" -ForegroundColor Red
    exit 1
}
Write-Host "[restart] launched (host pid=$($res.ProcessId)), log=$out"

# 4) 就绪探测：轮询 /login 到 200（上限 120s）
$deadline = (Get-Date).AddSeconds(120)
$ready = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-WebRequest -Uri $probeUrl -TimeoutSec 4 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
}

$elapsed = [int](New-TimeSpan -Start $t0 -End (Get-Date)).TotalSeconds
if ($ready) {
    Write-Host "[restart] READY in ${elapsed}s (window includes stop+boot+bind)"
    exit 0
}
Write-Host "[restart] NOT READY after ${elapsed}s - check $err / logs\app.log" -ForegroundColor Red
exit 1
