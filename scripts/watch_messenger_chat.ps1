# Watch live Messenger RPA chat events with color coding.
# Usage:
#   pwsh scripts/watch_messenger_chat.ps1
# or:
#   powershell -ExecutionPolicy Bypass -File scripts/watch_messenger_chat.ps1
#
# Color legend:
#   Cyan     - persona pick: which profile was selected, match source, forbidden_n
#   Green    - reply decided: final reply preview (check for () or [] markers)
#   Yellow   - peer_quiet_window: smart wait when peer is sending multiple msgs
#   Magenta  - guardrail: AI identity ask intercept / output filter
#   Red      - skip / failure / wrong_chat / not in thread
#   DkCyan   - title correction / trust_xml_tap_target_name (MIUI dump dead fallback)

$logPath = Join-Path $PSScriptRoot "..\logs\app.log"
$resolved = Resolve-Path $logPath -ErrorAction SilentlyContinue
if ($resolved) { $logPath = $resolved.Path }

if (-not (Test-Path $logPath)) {
    Write-Host "Log file not found: $logPath" -ForegroundColor Red
    Write-Host "Start main.py first so it creates logs/app.log." -ForegroundColor Yellow
    exit 1
}

Write-Host "===== watching $logPath =====" -ForegroundColor White
Write-Host "Ctrl+C to quit" -ForegroundColor DarkGray
Write-Host ""

$pattern = 'persona pick|reply decided|peer_quiet_window|guardrail|run_once|wrong_chat|trust_xml_tap_target_name|inbox_self_sent_skip|skill_no_reply|caption_source|multi_peer|extra_peers|verify thread title|sent reply|step=not_in_thread|step=send'

Get-Content $logPath -Tail 0 -Wait | ForEach-Object {
    $line = $_
    if ($line -notmatch $pattern) { return }

    if     ($line -match "persona pick")              { Write-Host $line -ForegroundColor Cyan }
    elseif ($line -match "reply decided")             { Write-Host $line -ForegroundColor Green }
    elseif ($line -match "peer_quiet_window")         { Write-Host $line -ForegroundColor Yellow }
    elseif ($line -match "guardrail")                 { Write-Host $line -ForegroundColor Magenta }
    elseif ($line -match "wrong_chat|skill_no_reply|step=not_in_thread") { Write-Host $line -ForegroundColor Red }
    elseif ($line -match "trust_xml")                 { Write-Host $line -ForegroundColor DarkCyan }
    elseif ($line -match "sent reply|step=send")     { Write-Host $line -ForegroundColor Green }
    elseif ($line -match "run_once")                  { Write-Host $line -ForegroundColor DarkGray }
    else                                              { Write-Host $line }
}
