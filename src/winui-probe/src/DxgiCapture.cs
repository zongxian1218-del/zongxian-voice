// DxgiCapture.cs —— 用 DXGI Desktop Duplication 采集桌面
//
// ============================================================================
// 【为什么换掉 Windows.Graphics.Capture】
// WGC 的采集源创建在目标机器上无条件返回 E_ACCESSDENIED (0x80070005)，
// 已排除：DSH 沙箱、DispatcherQueue、虚拟显示器、远程桌面会话、窗口站、
// 权限、IsSupported。剩下最可能是"未打包应用的身份限制"，或我手写的
// COM 互操作封送仍有问题（手写 P/Invoke 已连错三次）。
//
// DXGI Desktop Duplication 的优势正是不需要任何 WinRT 互操作：
//   · 纯 DXGI COM 接口，不涉及 AppContainer/包身份
//   · OBS 等未打包程序多年使用
//   · 直接产出 GPU 纹理，交给硬件编码器零拷贝
//
// 代价：只能整屏采集，不能指定单个窗口。对本项目够用 ——
// 我们的屏幕共享需求是"看得见朋友的屏幕"，不需要挑窗口。
// ============================================================================

using System;
using System.Runtime.InteropServices;

namespace Probe;

internal sealed class DxgiCapture : IDisposable
{
    private IntPtr _device;         // ID3D11Device
    private IntPtr _context;        // ID3D11DeviceContext
    private IntPtr _output;         // IDXGIOutput
    private IntPtr _output1;        // IDXGIOutput1
    private IntPtr _duplication;    // IDXGIOutputDuplication

    public System.Collections.Generic.List<string> Steps { get; } = new();
    /// <summary>实时回调：卡住时也能看到进行到哪一步。</summary>
    public Action<string>? OnStep { get; set; }
    private void Step(string s)
    {
        var msg = DateTime.Now.ToString("HH:mm:ss.fff") + " " + s;
        Steps.Add(msg);
        OnStep?.Invoke(msg);
    }
    public int Width { get; private set; }
    public int Height { get; private set; }
    public string AdapterName { get; private set; } = "?";
    public string OutputName { get; private set; } = "?";

    public bool Initialize()
    {
        // ---- 创建 D3D11 设备 ----
        // 注意：Desktop Duplication 要求在**驱动该输出**的适配器上创建设备。
        // 先按"默认适配器"创建，失败再枚举所有适配器逐个试。
        Step("创建 D3D11 设备");
        if (!TryCreateDevice(IntPtr.Zero, out _device, out _context))
        {
            if (!TryCreateOnAnyAdapter())
                return false;
        }

        // ---- 枚举该设备对应适配器的输出 ----
        Step("D3D 设备已创建，找输出");
        if (!TryGetFirstOutput()) return false;

        // ---- 取 IDXGIOutput1 ----
        var iidOutput1 = new Guid("00cddea8-939b-4b83-a340-a685226666cc");
        Step("取 IDXGIOutput1");
        var hr = Marshal.QueryInterface(_output, ref iidOutput1, out _output1);
        if (hr < 0 || _output1 == IntPtr.Zero)
        {
            LastError = $"QueryInterface(IDXGIOutput1) 失败 0x{hr:X8}";
            return false;
        }

        // ---- 创建桌面复制接口 ----
        // IDXGIOutput1::DuplicateOutput 的 vtable 序号是 22
        // （IDXGIObject 4 个 + IDXGIOutput 18 个之后）
        Step("调用 DuplicateOutput");
        hr = DuplicateOutput(_output1, _device, out _duplication);
        if (hr < 0 || _duplication == IntPtr.Zero)
        {
            LastError = hr switch
            {
                unchecked((int)0x887A0004) => "设备不支持桌面复制（DXGI_ERROR_UNSUPPORTED）—— 可能选错了显卡",
                unchecked((int)0x887A0022) => "已有程序在复制这个输出（最多 4 个）",
                unchecked((int)0x80070005) => "拒绝访问（E_ACCESSDENIED）",
                _ => $"DuplicateOutput 失败 0x{hr:X8}"
            };
            return false;
        }

        return true;
    }

    public string LastError { get; private set; } = "";

    private bool TryCreateDevice(IntPtr adapter, out IntPtr device, out IntPtr context)
    {
        // D3D11CreateDevice 的参数顺序：
        // (pAdapter, DriverType, Software, Flags, pFeatureLevels, FeatureLevels,
        //  SDKVersion, ppDevice, pFeatureLevel, ppImmediateContext)
        var hr = D3D11CreateDevice(
            adapter,
            adapter == IntPtr.Zero ? D3D_DRIVER_TYPE_HARDWARE : D3D_DRIVER_TYPE_UNKNOWN,
            IntPtr.Zero, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
            IntPtr.Zero, 0, 7,
            out device, out _, out context);
        return hr >= 0 && device != IntPtr.Zero;
    }

    private bool TryCreateOnAnyAdapter()
    {
        // 枚举所有适配器（含独显/核显），逐个尝试创建 + 复制
        var iidFactory = new Guid("7b7166ec-21c7-44ae-b21a-e3212a2a5b7d"); // IDXGIFactory
        var hr = CreateDXGIFactory1(ref iidFactory, out var factory);
        if (hr < 0) { LastError = $"CreateDXGIFactory1 失败 0x{hr:X8}"; return false; }

        try
        {
            // IDXGIFactory::EnumAdapters 的 vtable 序号是 7
            for (uint i = 0; ; i++)
            {
                hr = EnumAdapter(factory, i, out var adapter);
                if (hr < 0 || adapter == IntPtr.Zero) break;

                var ok = TryCreateDevice(adapter, out var dev, out var ctx);
                if (ok)
                {
                    // 记录适配器名

                    if (TryGetFirstOutputFor(dev))
                    {
                        _device = dev; _context = ctx;
                        Marshal.Release(adapter);
                        return true;
                    }
                    Marshal.Release(ctx);
                    Marshal.Release(dev);
                }
                Marshal.Release(adapter);
            }
        }
        finally { Marshal.Release(factory); }

        LastError = "所有适配器都不支持桌面复制";
        return false;
    }

    private bool TryGetFirstOutput()
    {
        // 通过 IDXGIDevice -> IDXGIAdapter -> EnumOutputs(0)
        var iidDxgiDevice = new Guid("54ec77fa-1377-44e6-8c32-88fd5f44c84c");
        var hr = Marshal.QueryInterface(_device, ref iidDxgiDevice, out var dxgiDevice);
        if (hr < 0) { LastError = "取 IDXGIDevice 失败"; return false; }
        try
        {
            hr = GetAdapter(dxgiDevice, out var adapter);
            if (hr < 0) { LastError = "取 IDXGIAdapter 失败"; return false; }
            try
            {

                hr = EnumOutputs(adapter, 0, out _output);
                if (hr < 0) { LastError = "该适配器没有输出（可能不是驱动显示器的显卡）"; return false; }

                var odesc = new DXGI_OUTPUT_DESC();
                if (GetOutputDesc(_output, ref odesc) >= 0)
                {
                    OutputName = odesc.DeviceName;
                    Width = odesc.DesktopCoordinates.Right - odesc.DesktopCoordinates.Left;
                    Height = odesc.DesktopCoordinates.Bottom - odesc.DesktopCoordinates.Top;
                }
                return true;
            }
            finally { Marshal.Release(adapter); }
        }
        finally { Marshal.Release(dxgiDevice); }
    }

    private bool TryGetFirstOutputFor(IntPtr device)
    {
        var iidDxgiDevice = new Guid("54ec77fa-1377-44e6-8c32-88fd5f44c84c");
        var hr = Marshal.QueryInterface(device, ref iidDxgiDevice, out var dxgiDevice);
        if (hr < 0) return false;
        try
        {
            hr = GetAdapter(dxgiDevice, out var adapter);
            if (hr < 0) return false;
            try
            {
                hr = EnumOutputs(adapter, 0, out _output);
                if (hr < 0) return false;
                var odesc = new DXGI_OUTPUT_DESC();
                if (GetOutputDesc(_output, ref odesc) >= 0)
                {
                    OutputName = odesc.DeviceName;
                    Width = odesc.DesktopCoordinates.Right - odesc.DesktopCoordinates.Left;
                    Height = odesc.DesktopCoordinates.Bottom - odesc.DesktopCoordinates.Top;
                }
                return true;
            }
            finally { Marshal.Release(adapter); }
        }
        finally { Marshal.Release(dxgiDevice); }
    }

    /// <summary>抓一帧。返回 true 表示拿到新帧（纹理句柄由调用方使用，不要释放）。</summary>
    public bool TryGetFrame(out IntPtr texture, int timeoutMs)
    {
        texture = IntPtr.Zero;
        if (_duplication == IntPtr.Zero) return false;

        var hr = AcquireNextFrame(_duplication, (uint)timeoutMs,
                                  out var frameInfo, out var resource);
        if (hr < 0)
        {
            // DXGI_ERROR_WAIT_TIMEOUT = 0x887A0027 表示这段时间桌面没有变化，属正常
            if (hr == unchecked((int)0x887A0027)) return false;
            return false;
        }

        try
        {
            var iidTexture = new Guid("6f15aaf2-d208-4e89-9ab4-489535d34f9c"); // ID3D11Texture2D
            var qi = Marshal.QueryInterface(resource, ref iidTexture, out texture);
            if (qi < 0) texture = IntPtr.Zero;
            return texture != IntPtr.Zero;
        }
        finally
        {
            Marshal.Release(resource);
        }
    }

    /// <summary>用完一帧后必须调用，否则复制接口会停止出帧。</summary>
    public void ReleaseFrame()
    {
        if (_duplication != IntPtr.Zero) ReleaseFrameAPI(_duplication);
    }

    public void Dispose()
    {
        if (_duplication != IntPtr.Zero) { Marshal.Release(_duplication); _duplication = IntPtr.Zero; }
        if (_output1 != IntPtr.Zero) { Marshal.Release(_output1); _output1 = IntPtr.Zero; }
        if (_output != IntPtr.Zero) { Marshal.Release(_output); _output = IntPtr.Zero; }
        if (_context != IntPtr.Zero) { Marshal.Release(_context); _context = IntPtr.Zero; }
        if (_device != IntPtr.Zero) { Marshal.Release(_device); _device = IntPtr.Zero; }
    }

    // ---- vtable 调用（按序号）----
    // IDXGIFactory::EnumAdapters 序号 7（IDXGIObject 4 个 + EnumAdapters 到 7）
    private static int EnumAdapter(IntPtr factory, uint index, out IntPtr adapter)
        => VtblCall(factory, 7, index, out adapter);

    // IDXGIAdapter::EnumOutputs 序号 7（IDXGIObject 4 + GetDesc/CheckInterfaceSupport/... 到 6）
    private static int EnumOutputs(IntPtr adapter, uint index, out IntPtr output)
        => VtblCall(adapter, 7, index, out output);

        // IDXGIDevice::GetAdapter 序号 7
    private static int GetAdapter(IntPtr dxgiDevice, out IntPtr adapter)
        => VtblCall(dxgiDevice, 7, out adapter);

    // IDXGIOutput::GetDesc 序号 7
    private static int GetOutputDesc(IntPtr output, ref DXGI_OUTPUT_DESC desc)
        => VtblCallRef(output, 7, ref desc);

    // IDXGIOutput1::DuplicateOutput 序号 22
    private static int DuplicateOutput(IntPtr output1, IntPtr device, out IntPtr duplication)
        => VtblCall(output1, 22, device, out duplication);

    // IDXGIOutputDuplication::AcquireNextFrame 序号 8
    private static int AcquireNextFrame(IntPtr dup, uint timeout,
                                        out DXGI_OUTDUPL_FRAME_INFO info, out IntPtr resource)
        => AcquireNextFrameImpl(dup, timeout, out info, out resource);

    // IDXGIOutputDuplication::ReleaseFrame 序号 14
    private static int ReleaseFrameAPI(IntPtr dup) => VtblCall(dup, 14);

    private static int VtblCall(IntPtr obj, int slot, out IntPtr a)
    {
        var vtbl = Marshal.ReadIntPtr(obj);
        var fn = Marshal.ReadIntPtr(vtbl, slot * IntPtr.Size);
        var del = Marshal.GetDelegateForFunctionPointer<Fn_OutPtr>(fn);
        return del(obj, out a);
    }

    private static int VtblCall(IntPtr obj, int slot)
    {
        var vtbl = Marshal.ReadIntPtr(obj);
        var fn = Marshal.ReadIntPtr(vtbl, slot * IntPtr.Size);
        var del = Marshal.GetDelegateForFunctionPointer<Fn_Void>(fn);
        return del(obj);
    }

    private static int VtblCall(IntPtr obj, int slot, uint a, out IntPtr b)
    {
        var vtbl = Marshal.ReadIntPtr(obj);
        var fn = Marshal.ReadIntPtr(vtbl, slot * IntPtr.Size);
        var del = Marshal.GetDelegateForFunctionPointer<Fn_Uint_OutPtr>(fn);
        return del(obj, a, out b);
    }

    private static int VtblCall(IntPtr obj, int slot, IntPtr a, out IntPtr b)
    {
        var vtbl = Marshal.ReadIntPtr(obj);
        var fn = Marshal.ReadIntPtr(vtbl, slot * IntPtr.Size);
        var del = Marshal.GetDelegateForFunctionPointer<Fn_Ptr_OutPtr>(fn);
        return del(obj, a, out b);
    }

    private static int VtblCallRef<T>(IntPtr obj, int slot, ref T a) where T : struct
    {
        var vtbl = Marshal.ReadIntPtr(obj);
        var fn = Marshal.ReadIntPtr(vtbl, slot * IntPtr.Size);
        var del = Marshal.GetDelegateForFunctionPointer<Fn_Ref>(fn);
        var handle = GCHandle.Alloc(a, GCHandleType.Pinned);
        try { return del(obj, handle.AddrOfPinnedObject()); }
        finally { handle.Free(); }
    }

    private static int AcquireNextFrameImpl(IntPtr obj, uint timeout,
                                            out DXGI_OUTDUPL_FRAME_INFO info, out IntPtr resource)
    {
        var vtbl = Marshal.ReadIntPtr(obj);
        var fn = Marshal.ReadIntPtr(vtbl, 8 * IntPtr.Size);
        var del = Marshal.GetDelegateForFunctionPointer<Fn_Acquire>(fn);

        var size = Marshal.SizeOf<DXGI_OUTDUPL_FRAME_INFO>();
        var buf = Marshal.AllocHGlobal(size);
        try
        {
            Marshal.StructureToPtr(new DXGI_OUTDUPL_FRAME_INFO(), buf, false);
            var hr = del(obj, timeout, buf, out resource);
            info = Marshal.PtrToStructure<DXGI_OUTDUPL_FRAME_INFO>(buf);
            return hr;
        }
        finally { Marshal.FreeHGlobal(buf); }
    }

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int Fn_OutPtr(IntPtr self, out IntPtr a);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int Fn_Void(IntPtr self);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int Fn_Uint_OutPtr(IntPtr self, uint a, out IntPtr b);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int Fn_Ptr_OutPtr(IntPtr self, IntPtr a, out IntPtr b);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int Fn_Ref(IntPtr self, IntPtr a);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int Fn_Acquire(IntPtr self, uint timeout, IntPtr info, out IntPtr resource);

    [StructLayout(LayoutKind.Sequential)]
    private struct DXGI_ADAPTER_DESC
    {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
        public string Description;
        public uint VendorId, DeviceId, SubSysId, Revision;
        public UIntPtr DedicatedVideoMemory, DedicatedSystemMemory, SharedSystemMemory;
        public long AdapterLuid;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct DXGI_RATIONAL { public uint Numerator, Denominator; }

    [StructLayout(LayoutKind.Sequential)]
    private struct DXGI_MODE_DESC
    {
        public uint Width, Height;
        public DXGI_RATIONAL RefreshRate;
        public uint Format, ScanlineOrdering, Scaling;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT { public int Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential)]
    private struct DXGI_OUTPUT_DESC
    {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
        public string DeviceName;
        public RECT DesktopCoordinates;
        public int AttachedToDesktop;
        public uint Rotation;
        public IntPtr Monitor;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct LUID { public uint LowPart; public int HighPart; }

    [StructLayout(LayoutKind.Sequential)]
    private struct DXGI_OUTDUPL_DESC
    {
        public DXGI_MODE_DESC ModeDesc;
        public uint Rotation;
        public int DesktopImageInSystemMemory;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct DXGI_OUTDUPL_FRAME_INFO
    {
        public long LastPresentTime;
        public long LastMouseUpdateTime;
        public uint AccumulatedFrames;
        public int RectsCoalesced;
        public int ProtectedContentMaskedOut;
        public IntPtr PointerPosition;   // 占位，大小与 DXGI_OUTDUPL_POINTER_POSITION 一致
        public uint TotalMetadataBufferSize;
        public uint PointerShapeBufferSize;
    }

    private const uint D3D_DRIVER_TYPE_HARDWARE = 1;
    private const uint D3D_DRIVER_TYPE_UNKNOWN = 0;
    private const uint D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20;

    [DllImport("d3d11.dll", SetLastError = true)]
    private static extern int D3D11CreateDevice(
        IntPtr pAdapter, uint driverType, IntPtr software, uint flags,
        IntPtr pFeatureLevels, uint featureLevels, uint sdkVersion,
        out IntPtr ppDevice, out uint pFeatureLevel, out IntPtr ppImmediateContext);

    [DllImport("dxgi.dll", ExactSpelling = true)]
    private static extern int CreateDXGIFactory1(ref Guid riid, out IntPtr factory);
}
