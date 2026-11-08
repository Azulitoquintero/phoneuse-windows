# Captura la ventana del espejo del iPhone (uxplay-windows) a un PNG.
param([string]$Out = "$PSScriptRoot\pantalla.png")

Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class W {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
"@
[W]::SetProcessDPIAware() | Out-Null

$p = Get-Process uxplay-windows -ErrorAction SilentlyContinue |
     Where-Object { $_.MainWindowTitle -eq 'AirPlay Video Stream' } | Select-Object -First 1
if (-not $p) { Write-Error "No encuentro la ventana 'AirPlay Video Stream'. ¿Está duplicando el iPhone?"; exit 1 }
$h = $p.MainWindowHandle

$r = New-Object W+RECT
[W]::GetClientRect($h, [ref]$r) | Out-Null
$w = $r.R - $r.L; $hh = $r.B - $r.T
$bmp = New-Object System.Drawing.Bitmap $w, $hh
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
# 1 = solo el área cliente, 2 = contenido renderizado por GPU
[W]::PrintWindow($h, $hdc, 3) | Out-Null
$g.ReleaseHdc($hdc); $g.Dispose()
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
"$Out ${w}x${hh}"
