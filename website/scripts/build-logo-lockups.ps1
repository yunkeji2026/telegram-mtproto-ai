# HuaLing Tech brand logo lockup generator (ASCII-only source; CJK via code points
# so the file's on-disk encoding can never corrupt the drawn text).
# Reads public/brand/logos/*-mark.png (AI 3D marks), overlays crisp text, and outputs
# horizontal/vertical x zh/en = 12 transparent PNGs. Dark text, for light/white backgrounds.
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

$INK = [System.Drawing.Color]::FromArgb(11, 30, 63)
$SUB = [System.Drawing.Color]::FromArgb(124, 58, 237)

$brands = @(
  @{ key = "hualing"; mark = "hualing-mark.png"; zh = $zhHuaLing; en = "HuaLing Tech"; zhSub = "HUALING TECH"; enSub = $zhHuaLing },
  @{ key = "huaying"; mark = "huaying-mark.png"; zh = $zhHuaYing; en = "LiveAvatar";   zhSub = "LiveAvatar";   enSub = $zhHuaYing },
  @{ key = "lingxi";  mark = "lingxi-mark.png";  zh = $zhLingXi;  en = "SoulSync";     zhSub = "SoulSync";     enSub = $zhLingXi }
)

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

function Build-Lockup($brand, $orient, $lang) {
  $mark = [System.Drawing.Image]::FromFile((Join-Path $logos $brand.mark))
  if ($lang -eq "zh") { $primary = $brand.zh; $secondary = $brand.zhSub }
  else                { $primary = $brand.en; $secondary = $brand.enSub }

  $famP = New-Object System.Drawing.FontFamily -ArgumentList "Microsoft YaHei"
  $famS = New-Object System.Drawing.FontFamily -ArgumentList "Segoe UI"
  $brushP = [System.Drawing.SolidBrush]::new($INK)
  $brushS = [System.Drawing.SolidBrush]::new($SUB)

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

  $out = Join-Path $logos ("{0}-{1}-{2}.png" -f $brand.key, $orient, $lang)
  $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
  $g.Dispose(); $bmp.Dispose(); $mark.Dispose()
  Write-Output ("  + " + (Split-Path -Leaf $out))
}

foreach ($b in $brands) {
  foreach ($o in @("h", "v")) {
    foreach ($l in @("zh", "en")) { Build-Lockup $b $o $l }
  }
}
Write-Output "done"
