# BOUNDLESS brand mark size generator (ASCII-only source; PowerShell 5.1 mis-parses
# non-ASCII bytes in UTF-8-no-BOM files, so keep this file ASCII-only).
#
# Pipeline:
#   0) Key out the solid-white background of boundless-mark-white.png (AI 3D mark on
#      pure white) -> boundless-mark.png (real alpha transparency, the master).
#   1) boundless-mark-256.png  256x256 transparent  -> navbar / footer / OG image
#   2) pwa-192.png / pwa-512.png transparent         -> PWA manifest icons
#   3) boundless-avatar.png 512x512 dark-bg          -> Telegram bot/channel/group photo
#      (TG renders transparency as black, so bake a dark gradient, circle-crop friendly)
#
# Re-run from website/:  powershell -ExecutionPolicy Bypass -File scripts/build-boundless-marks.ps1

Add-Type -AssemblyName System.Drawing
$ErrorActionPreference = "Stop"

$root      = Split-Path -Parent $PSScriptRoot
$logos     = Join-Path $root "public\brand\logos"
$whiteSrc  = Join-Path $logos "boundless-mark-white.png"
$src       = Join-Path $logos "boundless-mark.png"

# Turn near-white background pixels transparent (LockBits for speed; BGRA order).
# Hard cut at >=250 = transparent; 235..250 = partial alpha (soft edge, kills white halo).
function Build-Master($inPath, $outPath) {
  # Source PNG is 24bpp (no alpha channel); LockBits alpha edits would be discarded on
  # write-back. So redraw it into a true 32bppArgb bitmap first, then key out the white.
  $srcImg = [System.Drawing.Image]::FromFile($inPath)
  $bmp = New-Object System.Drawing.Bitmap($srcImg.Width, $srcImg.Height, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
  $gg = [System.Drawing.Graphics]::FromImage($bmp)
  $gg.DrawImage($srcImg, 0, 0, $srcImg.Width, $srcImg.Height)
  $gg.Dispose(); $srcImg.Dispose()
  $rect = New-Object System.Drawing.Rectangle(0, 0, $bmp.Width, $bmp.Height)
  $data = $bmp.LockBits($rect, [System.Drawing.Imaging.ImageLockMode]::ReadWrite, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
  $count = $data.Stride * $bmp.Height
  $buf = New-Object byte[] $count
  [System.Runtime.InteropServices.Marshal]::Copy($data.Scan0, $buf, 0, $count)
  for ($i = 0; $i -lt $count; $i += 4) {
    $b = $buf[$i]; $g = $buf[$i + 1]; $r = $buf[$i + 2]
    $min = [Math]::Min($r, [Math]::Min($g, $b))
    if ($min -ge 250) {
      $buf[$i + 3] = 0
    } elseif ($min -ge 235) {
      $buf[$i + 3] = [byte]([int]((250 - $min) / 15.0 * 255))
    }
  }
  [System.Runtime.InteropServices.Marshal]::Copy($buf, 0, $data.Scan0, $count)
  $bmp.UnlockBits($data)
  $bmp.Save($outPath, [System.Drawing.Imaging.ImageFormat]::Png)
  Write-Host ("  -> " + (Split-Path -Leaf $outPath) + " (white keyed out)")
  $bmp.Dispose()
}

function New-Canvas([int]$w, [int]$h) {
  $bmp = New-Object System.Drawing.Bitmap($w, $h, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
  $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $g.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
  return @($bmp, $g)
}

function Save-Png($bmp, $path) {
  $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
  Write-Host ("  -> " + (Split-Path -Leaf $path))
}

# contain-fit centered, padRatio = margin ratio on each side
function Draw-Contained($g, $img, [int]$size, [double]$padRatio) {
  $pad = [int]($size * $padRatio)
  $box = $size - 2 * $pad
  $scale = [Math]::Min($box / $img.Width, $box / $img.Height)
  $w = [int]($img.Width * $scale)
  $h = [int]($img.Height * $scale)
  $x = [int](($size - $w) / 2)
  $y = [int](($size - $h) / 2)
  $g.DrawImage($img, $x, $y, $w, $h)
}

function Build-Transparent([int]$size, $outPath) {
  $img = [System.Drawing.Image]::FromFile($src)
  $c = New-Canvas $size $size
  $bmp = $c[0]; $g = $c[1]
  $g.Clear([System.Drawing.Color]::Transparent)
  Draw-Contained $g $img $size 0.06
  Save-Png $bmp $outPath
  $g.Dispose(); $bmp.Dispose(); $img.Dispose()
}

function Build-Avatar([int]$size, $outPath) {
  $img = [System.Drawing.Image]::FromFile($src)
  $c = New-Canvas $size $size
  $bmp = $c[0]; $g = $c[1]
  $rect = New-Object System.Drawing.Rectangle(0, 0, $size, $size)
  $top = [System.Drawing.Color]::FromArgb(26, 29, 58)
  $bot = [System.Drawing.Color]::FromArgb(5, 6, 15)
  $bg = New-Object System.Drawing.Drawing2D.LinearGradientBrush($rect, $top, $bot, 90)
  $g.FillRectangle($bg, $rect)
  Draw-Contained $g $img $size 0.18
  Save-Png $bmp $outPath
  $bg.Dispose(); $g.Dispose(); $bmp.Dispose(); $img.Dispose()
}

Write-Host "Building BOUNDLESS marks ..."
Build-Master $whiteSrc $src
Build-Transparent 256 (Join-Path $logos "boundless-mark-256.png")
Build-Transparent 192 (Join-Path $logos "pwa-192.png")
Build-Transparent 512 (Join-Path $logos "pwa-512.png")
Build-Avatar      512 (Join-Path $logos "boundless-avatar.png")
Write-Host "Done."
