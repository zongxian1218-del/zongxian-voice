// CaptureHelper.cs —— D3D11 设备 + Windows.Graphics.Capture 互操作
//
// 【为什么单独抽出来】
// WGC 的采集源必须从"具体显示器"创建，这一步要经过
// IGraphicsCaptureItemInterop 这个 COM 互操作接口。
// 控制台版在这里拿到 E_ACCESSDENIED (0x80070005)，
// 本文件是 WinUI 版的对应实现，用于验证"调用线程需要 DispatcherQueue"的假设。
//
// 【为什么不用手写 P/Invoke 创建 D3D 设备】
// 手写那次编译就出错（CS7036/CS0039）。CsWinRT 已经提供
// Windows.Graphics.DirectX.Direct3D11.Interop 扩展方法，
// 官方推荐用它把 ID3D11Device 转成 WinRT 的 IDirect3DDevice，
// 既少写代码也不容易错。

using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using Windows.Graphics.Capture;
using Windows.Graphics.DirectX.Direct3D11;
using WinRT;

namespace Probe;

/// <summary>GraphicsCaptureItem 的 COM 互操作接口。</summary>
[ComImport]
[Guid("3628E81B-3CAC-4C60-B7F4-23CE0E0C3356")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IGraphicsCaptureItemInterop
{
    IntPtr CreateForWindow([In] IntPtr window, [In] ref Guid iid);
    IntPtr CreateForMonitor([In] IntPtr monitor, [In] ref Guid iid);
}

internal static class CaptureHelper
{
    private static IDirect3DDevice? _device;

    /// <summary>供采集帧池使用的 WinRT D3D 设备（懒创建）。</summary>
    public static IDirect3DDevice? WinRTDevice => _device ??= CreateDevice();

    private static IDirect3DDevice? CreateDevice()
    {
        try
        {
            // WindowsAppSDK / CsWinRT 会在这里自动初始化 D3D11 并给出硬件设备
            var device = Direct3D11Helper.CreateDevice();
            return device;
        }
        catch
        {
            return null;
        }
    }

    /// <summary>为主显示器创建采集源。这是控制台版失败的那一步。</summary>
    public static GraphicsCaptureItem? CreateItemForPrimaryMonitor()
        => CreateItemForMonitor(MonitorFromPoint(new POINT { X = 0, Y = 0 }, MONITOR_DEFAULTTOPRIMARY));

    /// <summary>为指定 HMONITOR 创建采集源。</summary>
    public static GraphicsCaptureItem? CreateItemForMonitor(IntPtr monitor)
    {
        if (monitor == IntPtr.Zero) throw new InvalidOperationException("监视器句柄为空");

        var interop = GraphicsCaptureItem.As<IGraphicsCaptureItemInterop>();
        var iid = typeof(GraphicsCaptureItem).GUID;

        var ptr = interop.CreateForMonitor(monitor, ref iid);
        if (ptr == IntPtr.Zero)
            throw new InvalidOperationException("CreateForMonitor 返回空指针");

        // 交给 CsWinRT 托管，由它负责引用计数
        return MarshalInterface<GraphicsCaptureItem>.FromAbi(ptr);
    }

    /// <summary>枚举全部显示器句柄，便于逐个尝试采集。</summary>
    public static List<(IntPtr Handle, string Name)> EnumMonitors()
    {
        var result = new List<(IntPtr, string)>();
        EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, (hMon, _, _, _) =>
        {
            var info = new MONITORINFOEX { cbSize = Marshal.SizeOf<MONITORINFOEX>() };
            var ok = GetMonitorInfo(hMon, ref info);
            var name = ok ? info.szDevice : "?";
            var primary = ok && (info.dwFlags & 1) != 0;   // MONITORINFOF_PRIMARY
            result.Add((hMon, $"{name}{(primary ? "（主）" : "")}"));
            return true;
        }, IntPtr.Zero);
        return result;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct MONITORINFOEX
    {
        public int cbSize;
        public RECT rcMonitor;
        public RECT rcWork;
        public uint dwFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
        public string szDevice;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT { public int Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT { public int X; public int Y; }

    private const uint MONITOR_DEFAULTTOPRIMARY = 1;

    private delegate bool MonitorEnumProc(IntPtr hMonitor, IntPtr hdc, IntPtr rect, IntPtr data);

    [DllImport("user32.dll")]
    private static extern IntPtr MonitorFromPoint(POINT pt, uint dwFlags);

    [DllImport("user32.dll")]
    private static extern bool EnumDisplayMonitors(IntPtr hdc, IntPtr clip, MonitorEnumProc proc, IntPtr data);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool GetMonitorInfo(IntPtr hMonitor, ref MONITORINFOEX info);
}
