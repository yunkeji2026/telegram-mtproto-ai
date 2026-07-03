# install-autostart.ps1 - register a scheduled task to auto-start the Baileys service at logon.
# Usage (current user, no admin needed): powershell -ExecutionPolicy Bypass -File install-autostart.ps1
# Uninstall: Unregister-ScheduledTask -TaskName "WhatsApp-Baileys-Service" -Confirm:$false
# Notes: starts start.ps1 hidden at logon; auto-restart on crash; no run-time limit (long-lived).
#        Decoupled from the main app - the main app connects via platform_login.whatsapp.baileys_url.

$ErrorActionPreference = "Stop"

$taskName = "WhatsApp-Baileys-Service"
$svcDir   = "D:\workspace\telegram-mtproto-ai\services\whatsapp-baileys"
$startPs1 = Join-Path $svcDir "start.ps1"
$psExe    = (Get-Command powershell.exe).Source

if (-not (Test-Path $startPs1)) { throw "start.ps1 not found: $startPs1" }

$action = New-ScheduledTaskAction `
    -Execute $psExe `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startPs1`"" `
    -WorkingDirectory $svcDir

# Start at logon (current user; normal rights are enough to bind high port 8790 + write user dirs).
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# Idempotent: unregister first if it already exists.
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "WhatsApp (Baileys) protocol QR login microservice: auto-start at logon, restart on crash, long-lived." | Out-Null

Write-Host "[install] Registered scheduled task: $taskName (AtLogOn, hidden window, restart-on-crash)"
