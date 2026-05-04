param(
    [switch]$ListDevices,
    [string]$Device = "",
    [int]$Seconds = 20,
    [string]$Output = "voice_samples/my_voice.wav",
    [double]$MinVolumeDb = -45.0
)

$ErrorActionPreference = "Stop"

$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpeg) {
    throw "ffmpeg not found. Install ffmpeg or add it to PATH."
}

if ($ListDevices) {
    & ffmpeg -hide_banner -list_devices true -f dshow -i dummy
    exit 0
}

if (-not $Device.Trim()) {
    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $deviceList = & ffmpeg -hide_banner -list_devices true -f dshow -i dummy 2>&1
    $ErrorActionPreference = $oldErrorActionPreference
    foreach ($line in $deviceList) {
        $s = [string]$line
        if ($s -match '"(.+)" \(audio\)') {
            $Device = $Matches[1]
            break
        }
    }
}

if (-not $Device.Trim()) {
    throw "No DirectShow audio device found. Run with -ListDevices and pass -Device."
}

$outPath = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Output))
$outDir = Split-Path -Parent $outPath
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

Write-Host ""
Write-Host "Recording voice sample for authorized personal voice cloning."
Write-Host "Device : $Device"
Write-Host "Seconds: $Seconds"
Write-Host "Output : $outPath"
Write-Host ""
Write-Host "Read naturally in Japanese. Keep the room quiet and keep your mouth 15-25cm from the mic."
Write-Host "Suggested text: Konnichiwa. Message arigatou gozaimasu. Ima kakunin shite imasu node, sukoshi dake matte kudasai ne."
Write-Host ""
Write-Host "Recording starts in 3 seconds..."
Start-Sleep -Seconds 3

& ffmpeg `
    -hide_banner `
    -y `
    -f dshow `
    -t $Seconds `
    -i "audio=$Device" `
    -vn `
    -ac 1 `
    -ar 24000 `
    -sample_fmt s16 `
    $outPath

if ($LASTEXITCODE -ne 0) {
    throw "ffmpeg recording failed with exit code $LASTEXITCODE"
}

$meta = [System.IO.Path]::ChangeExtension($outPath, ".consent.txt")
@(
    "speaker_id=my_voice"
    "owner_consent=true"
    "purpose=Messenger Japanese TTS replies using my own authorized voice"
    "created_at=$(Get-Date -Format o)"
    "audio_path=$outPath"
) | Set-Content -Encoding UTF8 -Path $meta

$volLines = & ffmpeg -hide_banner -i $outPath -af volumedetect -f null - 2>&1
$meanVolume = $null
$maxVolume = $null
foreach ($line in $volLines) {
    $s = [string]$line
    if ($s -match "mean_volume:\s+(-?inf|-?\d+(\.\d+)?) dB") {
        $meanVolume = $Matches[1]
    }
    if ($s -match "max_volume:\s+(-?inf|-?\d+(\.\d+)?) dB") {
        $maxVolume = $Matches[1]
    }
}

Write-Host ""
Write-Host "Volume check:"
Write-Host "mean_volume: $meanVolume dB"
Write-Host "max_volume : $maxVolume dB"

if ($null -eq $maxVolume -or $maxVolume -eq "-inf") {
    throw "Recorded file is silent. Select the headset microphone with -Device."
}

$maxVolumeNumber = [double]$maxVolume
if ($maxVolumeNumber -lt $MinVolumeDb) {
    throw "Recorded file is too quiet: max_volume=$maxVolume dB. Move closer to the mic or select another device."
}

Write-Host ""
Write-Host "Saved voice sample:"
Write-Host $outPath
Write-Host "Saved consent note:"
Write-Host $meta
