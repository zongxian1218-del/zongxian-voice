# 列出指定进程的所有顶层窗口：z 序（从上到下）、可见性、置顶、矩形、类名。
#
# 用途：诊断"窗口明明创建成功了，屏幕上却看不见"这类问题。
# WinUI 覆盖窗口踩过这个坑：窗口矩形完全正确，但 IsWindowVisible=False、WS_EX_TOPMOST 没设上，
# 于是"助手在正常解码、界面一片黑"。**别先怀疑渲染，先问系统窗口到底什么状态。**
#
# 用法（执行策略默认禁止运行 .ps1，所以必须先放开当前进程）：
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
#   .\build\inspect-windows.ps1
#   .\build\inspect-windows.ps1 -Names 'receiver-probe','VideoProbe'
#
# 输出示例：
#   z#9   pid=46268 hwnd=0x00381C30 vis=True  rect=(248,255)-(1528,975) 1280x720 topmost=True class='ZxPreviewWnd'
#   z#11  pid=53196 hwnd=0x001F0E30 vis=True  rect=(208,208)-(1568,1088) 1360x880 topmost=False class='WinUIDesktopWin32WindowClass'

[CmdletBinding()]
param(
    [string[]]$Names = @('VideoProbe', 'receiver-probe', 'sender-probe')
)

Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public class ZxWindowDump
{
    public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int i);
    [DllImport("user32.dll")] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);

    public struct RECT { public int Left, Top, Right, Bottom; }

    public static List<string> Dump(uint[] pids)
    {
        var outp = new List<string>();
        if (pids == null || pids.Length == 0) return outp;
        var wanted = new HashSet<uint>(pids);
        int z = 0;
        EnumWindows((h, l) =>
        {
            uint pid; GetWindowThreadProcessId(h, out pid);
            z++;
            if (!wanted.Contains(pid)) return true;
            RECT r; GetWindowRect(h, out r);
            var cls = new StringBuilder(256); GetClassName(h, cls, cls.Capacity);
            var txt = new StringBuilder(256); GetWindowTextW(h, txt, txt.Capacity);
            int ex = GetWindowLong(h, -20);
            outp.Add(string.Format(
                "z#{0,-4} pid={1,-7} hwnd=0x{2:X8} vis={3,-5} iconic={4,-5} rect=({5},{6})-({7},{8}) {9}x{10} topmost={11,-5} toolwin={12,-5} noactivate={13,-5} class='{14}' title='{15}'",
                z, pid, h.ToInt64(), IsWindowVisible(h), IsIconic(h),
                r.Left, r.Top, r.Right, r.Bottom, r.Right - r.Left, r.Bottom - r.Top,
                (ex & 0x8) != 0, (ex & 0x80) != 0, (ex & 0x08000000) != 0,
                cls.ToString(), txt.ToString()));
            return true;
        }, IntPtr.Zero);
        return outp;
    }
}
'@

$procs = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $Names -contains $_.ProcessName })
Write-Host ("matched processes: " + (($procs | ForEach-Object { "$($_.ProcessName)=$($_.Id)" }) -join ', '))
if ($procs.Count -eq 0) {
    Write-Host "no such process is running (nothing to dump)"
    return
}

$ids = [uint32[]]@($procs | ForEach-Object { [uint32]$_.Id })
$lines = [ZxWindowDump]::Dump($ids)
if ($lines.Count -eq 0) { Write-Host "processes exist but have no top-level windows" }
else { $lines | ForEach-Object { Write-Host $_ } }
