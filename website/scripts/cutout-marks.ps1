# Make the AI-generated brand marks truly transparent.
# The generator bakes a light-gray (~#EFEFEF) background; this removes it via a flood-fill
# seeded from the image borders (so interior white highlights/sparkles are preserved, unlike
# a naive white->alpha threshold). Run once after (re)generating marks.
#   powershell -ExecutionPolicy Bypass -File scripts/cutout-marks.ps1

$ErrorActionPreference = "Stop"
$root  = Split-Path -Parent $PSScriptRoot
$logos = Join-Path $root "public\brand\logos"

Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @"
using System;
using System.IO;
using System.Drawing;
using System.Drawing.Imaging;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class MarkCutout {
  // brightMin/satMax classify a "background-ish" pixel (bright + low saturation).
  public static void Process(string path, int brightMin, int satMax) {
    // Load via an in-memory clone so the source file isn't locked (allows save-in-place).
    byte[] raw = File.ReadAllBytes(path);
    using (var ms = new MemoryStream(raw))
    using (var src = new Bitmap(ms))
    using (var bmp = new Bitmap(src)) {
      int w = bmp.Width, h = bmp.Height;
      var data = bmp.LockBits(new Rectangle(0,0,w,h), ImageLockMode.ReadWrite, PixelFormat.Format32bppArgb);
      int stride = data.Stride;
      byte[] buf = new byte[stride*h];
      Marshal.Copy(data.Scan0, buf, 0, buf.Length);
      bool[] visited = new bool[w*h];
      var stack = new Stack<int>();
      for (int x=0; x<w; x++) { stack.Push(x); stack.Push((h-1)*w+x); }
      for (int y=0; y<h; y++) { stack.Push(y*w); stack.Push(y*w+(w-1)); }
      while (stack.Count > 0) {
        int idx = stack.Pop();
        if (visited[idx]) continue;
        visited[idx] = true;
        int x = idx % w, y = idx / w;
        int p = y*stride + x*4;
        int b = buf[p], g = buf[p+1], r = buf[p+2];
        int mx = Math.Max(r, Math.Max(g, b));
        int mn = Math.Min(r, Math.Min(g, b));
        if (mx >= brightMin && (mx - mn) <= satMax) {
          buf[p+3] = 0;
          if (x > 0)   stack.Push(idx-1);
          if (x < w-1) stack.Push(idx+1);
          if (y > 0)   stack.Push(idx-w);
          if (y < h-1) stack.Push(idx+w);
        }
      }
      Marshal.Copy(buf, 0, data.Scan0, buf.Length);
      bmp.UnlockBits(data);
      bmp.Save(path, ImageFormat.Png);
    }
  }
}
"@

foreach ($f in "hualing-mark.png", "huaying-mark.png", "lingxi-mark.png") {
  $p = Join-Path $logos $f
  [MarkCutout]::Process($p, 200, 36)
  Write-Output ("  cut " + $f)
}
Write-Output "done"
