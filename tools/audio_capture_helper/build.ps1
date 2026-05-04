$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Build = Join-Path $Root "build"
$Gen = Join-Path $Build "gen"
$Cls = Join-Path $Build "classes"
$Dex = Join-Path $Build "dex"
$Pkg = "com.codex.mrpaudiobridge"
$Sdk = $env:ANDROID_HOME
if (-not $Sdk) { $Sdk = $env:ANDROID_SDK_ROOT }
if (-not $Sdk) { throw "ANDROID_HOME / ANDROID_SDK_ROOT is not set" }

$Bt = Join-Path $Sdk "build-tools\34.0.0"
$AndroidJar = Join-Path $Sdk "platforms\android-34\android.jar"
$Aapt2 = Join-Path $Bt "aapt2.exe"
$D8 = Join-Path $Bt "d8.bat"
$Zipalign = Join-Path $Bt "zipalign.exe"
$Apksigner = Join-Path $Bt "apksigner.bat"
$Jdk17 = "C:\Program Files\Microsoft\jdk-17.0.18.8-hotspot"
if (Test-Path (Join-Path $Jdk17 "bin\java.exe")) {
  $env:JAVA_HOME = $Jdk17
  $env:Path = (Join-Path $Jdk17 "bin") + ";" + $env:Path
}
$Javac = Join-Path $env:JAVA_HOME "bin\javac.exe"
$KeyStore = Join-Path $Root "debug.keystore"

function Check-Last($Step) {
  if ($LASTEXITCODE -ne 0) {
    throw "$Step failed with exit code $LASTEXITCODE"
  }
}

Remove-Item -Recurse -Force $Build -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $Build, $Gen, $Cls, $Dex | Out-Null

& $Aapt2 compile --dir (Join-Path $Root "res") -o (Join-Path $Build "res.zip")
Check-Last "aapt2 compile"
& $Aapt2 link `
  -o (Join-Path $Build "base.apk") `
  -I $AndroidJar `
  --manifest (Join-Path $Root "AndroidManifest.xml") `
  --java $Gen `
  (Join-Path $Build "res.zip")
Check-Last "aapt2 link"

$Sources = Get-ChildItem (Join-Path $Root "src") -Recurse -Filter *.java | ForEach-Object { $_.FullName }
& $Javac -encoding UTF-8 -source 1.8 -target 1.8 -classpath $AndroidJar -d $Cls @Sources
Check-Last "javac"
Push-Location $Cls
try {
  & jar cf (Join-Path $Build "classes.jar") .
  Check-Last "jar classes"
} finally {
  Pop-Location
}
& $D8 --lib $AndroidJar --output $Dex (Join-Path $Build "classes.jar")
Check-Last "d8"

Copy-Item (Join-Path $Build "base.apk") (Join-Path $Build "unsigned.apk")
Push-Location $Dex
try {
  & jar uf (Join-Path $Build "unsigned.apk") classes.dex
  Check-Last "jar dex"
} finally {
  Pop-Location
}

& $Zipalign -f 4 (Join-Path $Build "unsigned.apk") (Join-Path $Build "aligned.apk")
Check-Last "zipalign"

if (-not (Test-Path $KeyStore)) {
  & keytool -genkeypair -v -keystore $KeyStore -storepass android -alias androiddebugkey `
    -keypass android -keyalg RSA -keysize 2048 -validity 10000 `
    -dname "CN=Android Debug,O=Codex,C=US" | Out-Null
}

& $Apksigner sign --ks $KeyStore --ks-pass pass:android --key-pass pass:android `
  --out (Join-Path $Build "MrpAudioBridge.apk") (Join-Path $Build "aligned.apk")
Check-Last "apksigner"

Write-Host "Wrote $(Join-Path $Build 'MrpAudioBridge.apk')"
