# HuaLing Tech brand asset generator (ASCII-only source; CJK via code points so the
# file's on-disk encoding can never corrupt drawn text).
#
# Reads public/brand/logos/*-mark.png (AI 3D marks) and produces:
#   1) Lockups: horizontal/vertical x zh/en x light/dark theme  = 24 transparent PNGs
#        {brand}-{h|v}-{zh|en}-{light|dark}.png
#        - light theme: dark text, for white / light backgrounds
#        - dark  theme: white text, for dark backgrounds / Telegram profile pages
#   2) Avatars: {brand}-avatar.png  = 512x512 dark-bg square (circle-crop friendly),
#      for Telegram bot/channel/group profile photos (transparency -> black in TG, so
#      we bake a dark gradient background).
#
# Re-run from website/:  powershell -ExecutionPolicy Bypass -File scripts/build-logo-lockups.ps1

Add-Type -AssemblyName System.Drawing
$ErrorActionPreference = "Stop"

$root  = Split-Path -Parent $PSScriptRoot
$logos = Join-Path $root "public\brand\logos"

# CJK helper: build a string from Unicode code points (encoding-safe)
function U([int[]]$cp) { -join ($cp | ForEach-Object { [char]$_ }) }
$zhHuaLing = U @(0x534E,0x7075,0x79D1,0x6280)   # HuaLing Keji
$zhHuaYing = U @(0x534E,0x5F71)                  # HuaYing
$zhLingXi  = U @(0x7075,0x7280)                  # LingXi

$brands = @(
  @{ key = "hualing"; mark = "hualing-mark.png"; zh = $zhHuaLing; en = "HuaLing Tech"; zhSub = "HUALING TECH"; enSub = $zhHuaLing },
  @{ key = "huaying"; mark = "huaying-mark.png"; zh = $zhHuaYing; en = "LiveAvatar";   zhSub = "LiveAvatar";   enSub = $zhHuaYing },
  @{ key = "lingxi";  mark = "lingxi-mark.png";  zh = $zhLingXi;  en = "SoulSync";     zhSub = "SoulSync";     enSub = $zhLingXi }
)

# theme -> primary / secondary text color
$themes = @{
  light = @{ primary = [System.Drawing.Color]::FromArgb(11, 30, 63);   secondary = [System.Drawing.Color]::FromArgb(124, 58, 237) }
  dark  = @{ primary = [System.Drawing.Color]::FromArgb(245, 248, 255); secondary = [System.Drawing.Color]::FromArgb(169, 182, 255) }
}

function New-Canvas([int]$w, [int]$h) {
  $bmp = New-Object System.Drawing.Bitmap($w, $h, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
  $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $g.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
  $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
  $g.Clear([System.Drawing.Color]::Transparent)
  return @($bmp, $g)
}

function Build-Lockup($brand, $orient, $lang, $themeName) {
  $mark = [System.Drawing.Image]::FromFile((Join-Path $logos $brand.mark))
  if ($lang -eq "zh") { $primary = $brand.zh; $secondary = $brand.zhSub }
  else                { $primary = $brand.en; $secondary = $brand.enSub }

  $famP = New-Object System.Drawing.FontFamily -ArgumentList "Microsoft YaHei"
  $famS = New-Object System.Drawing.FontFamily -ArgumentList "Segoe UI"
  $brushP = [System.Drawing.SolidBrush]::new($themes[$themeName].primary)
  $brushS = [System.Drawing.SolidBrush]::new($themes[$themeName].secondary)

  if ($orient -eq "h") {
    $W = 860; $H = 260; $m = 220
    $c = New-Canvas $W $H; $bmp = $c[0]; $g = $c[1]
    $g.DrawImage($mark, (New-Object System.Drawing.Rectangle -ArgumentList 16, 20, $m, $m))
    $pf = New-Object System.Drawing.Font -ArgumentList $famP, 72, ([System.Drawing.FontStyle]::Bold), ([System.Drawing.GraphicsUnit]::Pixel)
    $sf = New-Object System.Drawing.Font -ArgumentList $famS, 30, ([System.Drawing.FontStyle]::Regular), ([System.Drawing.GraphicsUnit]::Pixel)
    $pSize = $g.MeasureString($primary, $pf)
    $sSize = $g.MeasureString($secondary, $sf)
    $textH = $pSize.Height + 12 + $sSize.Height
    $tx = $m + 56
    $ty = ($H - $textH) / 2
    $g.DrawString($primary, $pf, $brushP, $tx, $ty)
    $g.DrawString($secondary, $sf, $brushS, ($tx + 3), ($ty + $pSize.Height + 12))
  }
  else {
    $W = 480; $H = 560; $m = 320
    $c = New-Canvas $W $H; $bmp = $c[0]; $g = $c[1]
    $g.DrawImage($mark, (New-Object System.Drawing.Rectangle -ArgumentList ([int](($W - $m) / 2)), 20, $m, $m))
    $pf = New-Object System.Drawing.Font -ArgumentList $famP, 80, ([System.Drawing.FontStyle]::Bold), ([System.Drawing.GraphicsUnit]::Pixel)
    $sf = New-Object System.Drawing.Font -ArgumentList $famS, 34, ([System.Drawing.FontStyle]::Regular), ([System.Drawing.GraphicsUnit]::Pixel)
    $fmt = New-Object System.Drawing.StringFormat
    $fmt.Alignment = [System.Drawing.StringAlignment]::Center
    $pSize = $g.MeasureString($primary, $pf)
    $py = $m + 44
    $g.DrawString($primary, $pf, $brushP, (New-Object System.Drawing.RectangleF -ArgumentList 0, $py, $W, 110), $fmt)
    $g.DrawString($secondary, $sf, $brushS, (New-Object System.Drawing.RectangleF -ArgumentList 0, ($py + $pSize.Height + 8), $W, 50), $fmt)
  }

  $out = Join-Path $logos ("{0}-{1}-{2}-{3}.png" -f $brand.key, $orient, $lang, $themeName)
  $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
  $g.Dispose(); $bmp.Dispose(); $mark.Dispose()
  Write-Output ("  + " + (Split-Path -Leaf $out))
}

# Render a mark centered on a deep-space dark background (circle-crop / maskable friendly).
# Used for Telegram avatars + Apple/PWA icons (transparency -> black in those contexts).
function Render-OnDarkBg([string]$markPath, [int]$S, [double]$scale, [string]$out) {
  $mark = [System.Drawing.Image]::FromFile($markPath)
  $bmp = New-Object System.Drawing.Bitmap($S, $S, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
  $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $g.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
  $g.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceOver

  # deep-space background — solid base fill guarantees full opacity
  $rect = New-Object System.Drawing.Rectangle -ArgumentList 0, 0, $S, $S
  $base = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(255, 8, 12, 24))  # #080C18
  $g.FillRectangle($base, $rect)

  # soft brand glow behind the mark (additive light over the solid base)
  $ge = [int]($S * 0.11)
  $gs = [int]($S * 0.78)
  $glowPath = New-Object System.Drawing.Drawing2D.GraphicsPath
  $glowPath.AddEllipse($ge, $ge, $gs, $gs)
  $glow = New-Object System.Drawing.Drawing2D.PathGradientBrush -ArgumentList $glowPath
  $glow.CenterColor = [System.Drawing.Color]::FromArgb(120, 96, 130, 255)
  $glow.SurroundColors = @([System.Drawing.Color]::FromArgb(0, 8, 12, 24))
  $g.FillPath($glow, $glowPath)

  $m = [int]($S * $scale); $off = [int](($S - $m) / 2)
  $g.DrawImage($mark, (New-Object System.Drawing.Rectangle -ArgumentList $off, $off, $m, $m))

  $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
  $g.Dispose(); $bmp.Dispose(); $mark.Dispose()
  Write-Output ("  * " + (Split-Path -Leaf $out))
}

function Build-Avatar($brand) {
  Render-OnDarkBg (Join-Path $logos $brand.mark) 512 0.78 (Join-Path $logos ("{0}-avatar.png" -f $brand.key))
}

# Clean old un-themed lockups from the previous revision (theme suffix is now required)
Get-ChildItem $logos -Filter "*-*-??.png" | Where-Object { $_.Name -match '-(h|v)-(zh|en)\.png$' } | Remove-Item -Force

foreach ($b in $brands) {
  foreach ($o in @("h", "v")) {
    foreach ($l in @("zh", "en")) {
      foreach ($t in @("light", "dark")) { Build-Lockup $b $o $l $t }
    }
  }
  Build-Avatar $b
}

# Transparent downscale (web nav / OG / favicon use the mark on its own).
function Downscale-Transparent([string]$markPath, [int]$S, [string]$out) {
  $mark = [System.Drawing.Image]::FromFile($markPath)
  $bmp = New-Object System.Drawing.Bitmap($S, $S, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $g.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceOver
  $g.Clear([System.Drawing.Color]::Transparent)
  $g.DrawImage($mark, (New-Object System.Drawing.Rectangle -ArgumentList 0, 0, $S, $S))
  $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
  $g.Dispose(); $bmp.Dispose(); $mark.Dispose()
  Write-Output ("  - " + (Split-Path -Leaf $out))
}

$masterMark = Join-Path $logos "hualing-mark.png"
$appDir = Join-Path $root "app"

# Web/favicon (transparent)
Downscale-Transparent $masterMark 256 (Join-Path $logos "hualing-mark-256.png")
Downscale-Transparent $masterMark 64  (Join-Path $appDir "icon.png")

# App / PWA icons from the master mark (dark bg; maskable-safe padding for PWA sizes)
Render-OnDarkBg $masterMark 180 0.78 (Join-Path $appDir "apple-icon.png")
Render-OnDarkBg $masterMark 192 0.72 (Join-Path $logos "pwa-192.png")
Render-OnDarkBg $masterMark 512 0.72 (Join-Path $logos "pwa-512.png")

Write-Output "done"
