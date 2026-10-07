# 截取窗口截图（不依赖任何第三方库，只用 .NET 自带的 GDI+）
param(
    [int]$ProcessId = 0,
    [string]$Out = "$env:TEMP\zx-window.png"
)

Add-Type -AssemblyName System.Drawing

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32Shot {
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr hwnd, int attr, out RECT rect, int size);
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
}
"@

$proc = if ($ProcessId -gt 0) { Get-Process -Id $ProcessId -ErrorAction Stop }
        else { Get-Process -Name 'ZongxianVoice' -ErrorAction Stop | Select-Object -First 1 }

$hwnd = $proc.MainWindowHandle
if ($hwnd -eq 0) { throw "找不到主窗口" }

[Win32Shot]::ShowWindow($hwnd, 9) | Out-Null   # SW_RESTORE
[Win32Shot]::SetForegroundWindow($hwnd) | Out-Null
Start-Sleep -Milliseconds 800

# 优先用 DwmGetWindowAttribute 拿"不含投影"的真实边界；
# 失败就退回 GetWindowRect。
$rect = New-Object Win32Shot+RECT
$ok = [Win32Shot]::DwmGetWindowAttribute($hwnd, 9, [ref]$rect, 16) -eq 0
if (-not $ok) { [Win32Shot]::GetWindowRect($hwnd, [ref]$rect) | Out-Null }

$w = $rect.Right - $rect.Left
$h = $rect.Bottom - $rect.Top
if ($w -le 0 -or $h -le 0) { throw "窗口尺寸异常: ${w}x${h}" }

$bmp = New-Object System.Drawing.Bitmap($w, $h)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($rect.Left, $rect.Top, 0, 0, (New-Object System.Drawing.Size($w, $h)))
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()

Write-Host "已截图: $Out  (${w}x${h})"
