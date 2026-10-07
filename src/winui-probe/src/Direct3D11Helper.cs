// Direct3D11Helper.cs —— 把 D3D11 设备交给 WinRT（给 WGC 帧池使用）
//
// 【踩过的两个坑，记下来避免重复】
// 1) 手写 CreateDirect3D11DeviceFromDXGIDevice 的 P/Invoke 两次编译都错
//    （CS7036 少参数、CS0039 类型转换失败）。
// 2) object.As<IInspectable>() 返回的是 WinRT 内部引用类型
//    ObjectReference<T>，不能当 IInspectable 直接用（CS0266）。
//    正确做法是用 IInspectable 的**静态** FromAbi 从裸指针构造。
//
// 所以这里全程走"裸指针 + FromAbi"，不依赖 As<> 的返回类型细节。

using System;
using System.Runtime.InteropServices;
using Windows.Graphics.DirectX.Direct3D11;
using WinRT;

namespace Probe;

internal static class Direct3D11Helper
{
    private const uint D3D_DRIVER_TYPE_HARDWARE = 1;
    private const uint D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20;

    private static readonly Guid IID_IDXGIDevice = new("54ec77fa-1377-44e6-8c32-88fd5f44c84c");

    public static IDirect3DDevice CreateDevice()
    {
        var hr = D3D11CreateDevice(
            IntPtr.Zero, D3D_DRIVER_TYPE_HARDWARE, IntPtr.Zero,
            D3D11_CREATE_DEVICE_BGRA_SUPPORT,
            IntPtr.Zero, 0, 7,
            out var d3dDevice, out _, out var context);
        if (hr < 0 || d3dDevice == IntPtr.Zero)
            throw new InvalidOperationException($"D3D11CreateDevice 失败 0x{hr:X8}");

        IntPtr dxgiPtr = IntPtr.Zero;
        try
        {
            // 静态只读字段不能用作 ref（CS0199），用局部变量
            var iidDxgi = IID_IDXGIDevice;
            var qi = Marshal.QueryInterface(d3dDevice, ref iidDxgi, out dxgiPtr);
            if (qi < 0 || dxgiPtr == IntPtr.Zero)
                throw new InvalidOperationException($"QueryInterface(IDXGIDevice) 失败 0x{qi:X8}");

            // 用静态 FromAbi 构造 IInspectable（不要用 object.As<IInspectable>）
            var dxgiInspectable = IInspectable.FromAbi(dxgiPtr);
            var deviceInspectable = CreateDirect3D11DeviceFromDXGIDevice(dxgiInspectable);

            // 再转成 IDirect3DDevice
            var abi = MarshalInterface<IDirect3DDevice>.FromAbi(
                Marshal.GetIUnknownForObject(deviceInspectable));
            return abi;
        }
        finally
        {
            if (dxgiPtr != IntPtr.Zero) Marshal.Release(dxgiPtr);
            if (context != IntPtr.Zero) Marshal.Release(context);
            Marshal.Release(d3dDevice);
        }
    }

    [DllImport("d3d11.dll", SetLastError = true)]
    private static extern int D3D11CreateDevice(
        IntPtr pAdapter, uint driverType, IntPtr software, uint flags,
        IntPtr pFeatureLevels, uint featureLevels, uint sdkVersion,
        out IntPtr ppDevice, out uint pFeatureLevel, out IntPtr ppImmediateContext);

    // Windows 自带的互操作：IInspectable(IDXGIDevice) -> IInspectable(IDirect3DDevice)
    [DllImport("d3d11.dll", ExactSpelling = true)]
    private static extern IInspectable CreateDirect3D11DeviceFromDXGIDevice(
        [In] IInspectable dxgiDevice);
}
