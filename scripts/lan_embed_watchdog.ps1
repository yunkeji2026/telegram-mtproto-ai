# lan_embed_watchdog.ps1 - cross-host embedding failover (client 192.168.1.44 -> LAN bge-m3 host 192.168.1.43)
#
# Purpose: this client (telegram-mtproto-ai) periodically probes the LAN Ollama/bge-m3 embedding
#   endpoint; on repeated failure it SSHes into 192.168.1.43 and brings Ollama back up
#   (best-effort kill of a stuck process + trigger the OllamaServe scheduled task), then re-checks.
#
# This is a SECOND safety net on top of .43's own self-healing (OllamaServe crash-restart +
#   OllamaHealth per-minute self-check). It covers "self-heal on .43 failed" or "unreachable from
#   the client's point of view". A same-LAN / connectivity classifier avoids mistaking a network
#   outage for a dead service (no pointless SSH restart when it's really a network / different-WiFi issue).
#
# Usage:
#   Health+heal : powershell -ExecutionPolicy Bypass -File scripts\lan_embed_watchdog.ps1
#   Net check   : powershell -ExecutionPolicy Bypass -File scripts\lan_embed_watchdog.ps1 -Preflight
#
# Prereq: ~/.ssh/config has `Host lan-embed` (-> 192.168.1.43, User Administrator,
#   IdentityFile lan_embed_43), and that public key is authorized on .43 (passwordless login works).
#
# NOTE: ASCII-only on purpose. PowerShell 5.1 mis-decodes UTF-8-without-BOM files as the system
#   codepage (GBK), which corrupts CJK string literals and breaks parsing. Keep this file ASCII.

param(
    [string]$EmbedHost   = "192.168.1.43",
    [int]   $EmbedPort   = 11434,
    [string]$EmbedUrl    = "http://192.168.1.43:11434/api/tags",
    [string]$SshHost     = "lan-embed",
    [int]   $Retries     = 3,
    [int]   $RetryGapSec = 5,
    [int]   $TimeoutSec  = 6,
    [switch]$Preflight,
    [string]$LogPath     = "$PSScriptRoot\..\logs\lan_embed_watchdog.log"
)

$ErrorActionPreference = "SilentlyContinue"

function Write-Log([string]$level, [string]$msg) {
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $level, $msg
    Add-Content -Path $LogPath -Value $line
    Write-Host $line
    # simple rotation: trim to last 2000 lines once over 3000
    try {
        $c = @(Get-Content $LogPath)
        if ($c.Count -gt 3000) { Set-Content -Path $LogPath -Value ($c[-2000..-1]) }
    } catch {}
}

function Test-SameLan {
    # Same-LAN / connectivity classifier. Distinguishes a network/subnet problem from a service
    # problem so we do not SSH-restart when the real issue is the network. Classes:
    #   SAME_LAN          local host and embed host in same /24 AND a TCP port is reachable
    #   REACHABLE_XSUBNET port reachable but different subnet (routed/bridged; not same WiFi but OK)
    #   PORT_BLOCKED      same subnet but ports closed (peer firewall / service not listening)
    #   UNREACHABLE       neither ping nor ports (different WiFi / offline / peer powered off)
    $localIps = @((Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' }).IPAddress)
    $embParts = $EmbedHost.Split('.')
    $emb24 = ($embParts[0..2] -join '.') + '.'
    $sameSubnet = [bool]($localIps | Where-Object { $_.StartsWith($emb24) })

    $ping = Test-Connection -ComputerName $EmbedHost -Count 1 -Quiet -ErrorAction SilentlyContinue
    $t22  = (Test-NetConnection -ComputerName $EmbedHost -Port 22 -WarningAction SilentlyContinue).TcpTestSucceeded
    $tEmb = (Test-NetConnection -ComputerName $EmbedHost -Port $EmbedPort -WarningAction SilentlyContinue).TcpTestSucceeded

    if ($tEmb -or $t22) {
        $class = if ($sameSubnet) { "SAME_LAN" } else { "REACHABLE_XSUBNET" }
    } elseif ($sameSubnet) {
        $class = "PORT_BLOCKED"
    } else {
        $class = "UNREACHABLE"
    }
    return [pscustomobject]@{
        LocalIps   = ($localIps -join ',')
        EmbedHost  = $EmbedHost
        SameSubnet = $sameSubnet
        Ping       = $ping
        Port22     = $t22
        PortEmbed  = $tEmb
        Class      = $class
    }
}

function Test-Embed {
    for ($i = 1; $i -le $Retries; $i++) {
        try {
            $r = Invoke-WebRequest -Uri $EmbedUrl -TimeoutSec $TimeoutSec -UseBasicParsing
            if ($r.StatusCode -eq 200 -and $r.Content -match "bge-m3") { return $true }
        } catch {}
        if ($i -lt $Retries) { Start-Sleep -Seconds $RetryGapSec }
    }
    return $false
}

# -- Preflight: only run the connectivity / same-LAN diagnosis and print it (no service action) --
if ($Preflight) {
    $d = Test-SameLan
    Write-Log "PREFLIGHT" ("class={0} same_subnet={1} local={2} ping={3} port22={4} port{5}={6}" -f `
        $d.Class, $d.SameSubnet, $d.LocalIps, $d.Ping, $d.Port22, $EmbedPort, $d.PortEmbed)
    switch ($d.Class) {
        "SAME_LAN"          { Write-Host "[OK]   same LAN, port reachable (network healthy)" }
        "REACHABLE_XSUBNET" { Write-Host "[WARN] different subnet but routable (not same WiFi, still reachable)" }
        "PORT_BLOCKED"      { Write-Host "[WARN] same subnet but port closed (peer firewall / service down)" }
        "UNREACHABLE"       { Write-Host "[FAIL] unreachable (different WiFi / offline / peer down)" }
    }
    exit 0
}

# 1) healthy -> quiet heartbeat and exit
if (Test-Embed) {
    Write-Log "OK" "embedding reachable (bge-m3)"
    exit 0
}

Write-Log "DOWN" "embedding unreachable after $Retries tries"

# 2) classify connectivity first: do not SSH-restart on a pure network problem
$net = Test-SameLan
Write-Log "NET" ("class={0} same_subnet={1} local={2} ping={3} port22={4} port{5}={6}" -f `
    $net.Class, $net.SameSubnet, $net.LocalIps, $net.Ping, $net.Port22, $EmbedPort, $net.PortEmbed)
if ($net.Class -eq "UNREACHABLE") {
    Write-Log "NETFAIL" "peer fully unreachable (likely different WiFi / offline / powered off) - not a service issue, skip SSH restart, wait for network"
    exit 4
}

# 3) probe SSH login (distinguish dead Ollama vs SSH/auth unavailable)
$ping = ssh -o BatchMode=yes -o ConnectTimeout=8 $SshHost "echo ssh_ok" 2>&1
if ($ping -notmatch "ssh_ok") {
    Write-Log "UNREACHABLE" "SSH to $SshHost failed (auth/service issue): $ping"
    exit 2
}

# 4) remote bring-up: best-effort stop stuck process + trigger OllamaServe scheduled task
$remote = 'powershell -NoProfile -Command "Get-Process ollama -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2; schtasks /Run /TN OllamaServe 2>&1 | Out-String"'
$out = ssh -o BatchMode=yes -o ConnectTimeout=10 $SshHost $remote 2>&1
Write-Log "RESTART" "remote action output: $($out -replace '\s+', ' ')"

# 5) give it time to come up, re-check
Start-Sleep -Seconds 12
if (Test-Embed) {
    Write-Log "RECOVERED" "embedding back online after remote restart"
    exit 0
} else {
    Write-Log "FAILED" "still unreachable after remote restart (manual check needed on 192.168.1.43)"
    exit 3
}

# -- Register as a scheduled task (run once in an ADMIN PowerShell on this client) --
# Run under the current logged-in user (whose ~/.ssh has the key), NOT SYSTEM (SYSTEM has no ~/.ssh).
# $act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File D:\workspace\telegram-mtproto-ai\scripts\lan_embed_watchdog.ps1"
# $trg = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration (New-TimeSpan -Days 3650)
# Register-ScheduledTask -TaskName "LanEmbedWatchdog" -Action $act -Trigger $trg -RunLevel Highest -User $env:USERNAME -Force
