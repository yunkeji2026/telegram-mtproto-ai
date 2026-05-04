# Dump recent Messenger RPA key chat events from logs/app.log to clipboard,
# ready to paste back to Claude for analysis.
# Usage:
#   pwsh scripts/dump_recent_chat.ps1              # last 80 events
#   pwsh scripts/dump_recent_chat.ps1 -Last 150    # 150 events
#   pwsh scripts/dump_recent_chat.ps1 -ToFile out.txt
#   pwsh scripts/dump_recent_chat.ps1 -Chat 神沢   # filter by chat name
param(
    [int]$Last = 80,
    [string]$Chat = "",
    [string]$ToFile = ""
)

$logPath = Join-Path $PSScriptRoot "..\logs\app.log"
$resolved = Resolve-Path $logPath -ErrorAction SilentlyContinue
if ($resolved) { $logPath = $resolved.Path }

if (-not (Test-Path $logPath)) {
    Write-Host "Log file not found: $logPath" -ForegroundColor Red
    exit 1
}

# Key events regex (English keywords only — covers all P0/P1/P2 events).
$pattern = 'persona pick|reply decided|peer_quiet_window|guardrail|run_once|wrong_chat|trust_xml_tap_target_name|inbox_self_sent_skip|skill_no_reply|caption_source|multi_peer|extra_peers|verify thread title|sent reply|step=not_in_thread|step=send'

Write-Host "Scanning: $logPath" -ForegroundColor DarkGray

# Read last 1500 lines and filter by pattern.
$lines = Get-Content $logPath -Tail 1500 | Select-String $pattern
if ($Chat) {
    $lines = $lines | Where-Object { $_.Line -match [regex]::Escape($Chat) }
}
$out = $lines | Select-Object -Last $Last | ForEach-Object { $_.Line }

if ($null -eq $out -or $out.Count -eq 0) {
    Write-Host "No matching events in last 1500 log lines." -ForegroundColor Yellow
    Write-Host "Tips:" -ForegroundColor DarkGray
    Write-Host "  - Make sure main.py is running and writing to logs/app.log" -ForegroundColor DarkGray
    Write-Host "  - Make sure messenger_rpa.enabled=true and at least one run_once has happened" -ForegroundColor DarkGray
    exit 0
}

$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$header = "# Messenger Chat key events dump`n# time: $timestamp`n# count: $($out.Count)`n# filter: $(if ($Chat) { "chat=$Chat" } else { "all chats" })`n"
$body = $header + "`n" + ($out -join "`n")

if ($ToFile) {
    Set-Content -Path $ToFile -Value $body -Encoding UTF8
    Write-Host "Written $($out.Count) events to $ToFile" -ForegroundColor Green
} else {
    $body | Set-Clipboard
    Write-Host "Copied $($out.Count) key events to clipboard." -ForegroundColor Green
    Write-Host "Just Ctrl+V to paste back to Claude." -ForegroundColor Cyan
    Write-Host ""
    Write-Host "-- last 10 lines preview --" -ForegroundColor DarkGray
    $out | Select-Object -Last 10 | ForEach-Object { Write-Host $_ -ForegroundColor DarkGray }
}
