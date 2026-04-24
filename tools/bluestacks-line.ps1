# BlueStacks 5 (Pie64) + LINE pinned version helper
# Pinned: LINE 15.2.1 (jp.naver.line.android) - do not update from Play Store.
#
# Usage:
#   .\tools\bluestacks-line.ps1 -Action Connect
#   .\tools\bluestacks-line.ps1 -Action Status
#   .\tools\bluestacks-line.ps1 -Action Launch
#
# Uses adb server port 5038 to avoid conflict with other adb clients.
# Enable ADB: BlueStacks Settings - Advanced - Android Debug Bridge (bst.enable_adb_access=1 on this PC).
# BlueStacks adb shell may drop after one command; chain shell with semicolons.

param(
    [ValidateSet('Connect', 'Status', 'Launch')]
    [string]$Action = 'Status',

    [string]$AdbHost = '127.0.0.1',
    [int]$BlueStacksAdbPort = 5555,
    [int]$AdbServerPort = 5038,
    [string]$Serial = '127.0.0.1:5555',
    [string]$LinePackage = 'jp.naver.line.android',
    [string]$PinnedLineVersion = '15.2.1',
    [string]$SplashActivity = 'jp.naver.line.android/.activity.SplashActivity'
)

$ErrorActionPreference = 'Stop'
$env:ANDROID_ADB_SERVER_PORT = "$AdbServerPort"

function Invoke-Adb {
    param([string[]]$ArgumentList)
    & adb @ArgumentList
}

function Connect-BlueStacks {
    Invoke-Adb @('connect', "${AdbHost}:${BlueStacksAdbPort}") | Write-Host
    $lines = @(Invoke-Adb @('devices', '-l'))
    $lines | Write-Host
    $esc = [regex]::Escape($Serial)
    $found = $false
    foreach ($line in $lines) { if ($line -match $esc) { $found = $true; break } }
    if (-not $found) {
        throw "Device $Serial not found. Enable ADB in BlueStacks Settings - Advanced."
    }
}

function Get-LineStatusOneShot {
    $script = 'dumpsys package ' + $LinePackage + ' | grep versionName'
    Invoke-Adb @('-s', $Serial, 'shell', $script)
}

function Start-LineApp {
    $script = 'dumpsys package ' + $LinePackage + ' | grep versionName; am start -n ' + $SplashActivity + '; echo DONE'
    Invoke-Adb @('-s', $Serial, 'shell', $script)
}

Invoke-Adb @('start-server') | Out-Null
Connect-BlueStacks

switch ($Action) {
    'Connect' {
        Write-Host "ADB connected: $Serial (server port $AdbServerPort)."
    }
    'Status' {
        Write-Host "=== Pinned LINE version (do not upgrade): $PinnedLineVersion ==="
        Get-LineStatusOneShot
    }
    'Launch' {
        Write-Host "=== Starting LINE $PinnedLineVersion ==="
        Start-LineApp
    }
}
