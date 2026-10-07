// EncoderProbe.cs —— 检查 H.264 硬件编码器是否可用
//
// 【为什么需要单独验证】
// 屏幕共享链路里最贵的一环是编码。如果目标机器没有硬件编码器，
// 1080p60 靠 CPU 软编基本不可能实时，整条方案就要改设计。
// 所以宁可在写传输层之前先把这一步确认掉。

using System;
using System.Runtime.InteropServices;

namespace Probe;

internal static class EncoderProbe
{
    public static bool TryFindHardwareH264(out string? name)
    {
        name = null;

        var input = new MFT_REGISTER_TYPE_INFO
        {
            guidMajorType = MFMediaType_Video,
            guidSubtype = MFVideoFormat_NV12
        };
        var output = new MFT_REGISTER_TYPE_INFO
        {
            guidMajorType = MFMediaType_Video,
            guidSubtype = MFVideoFormat_H264
        };

        // 只要硬件编码器；MFT_ENUM_FLAG_HARDWARE = 0x4
        var hr = MFTEnumEx(
            MFT_CATEGORY_VIDEO_ENCODER,
            0x4 | 0x40,           // HARDWARE | SORTANDFILTER
            ref input, ref output,
            out var activates, out var count);

        if (hr < 0 || count == 0 || activates == IntPtr.Zero) return false;

        try
        {
            // 第一个就是排序后的最佳候选；取它的友好名
            var first = Marshal.ReadIntPtr(activates);
            if (first != IntPtr.Zero)
                name = GetFriendlyName(first);
            return true;
        }
        finally
        {
            for (uint i = 0; i < count; i++)
            {
                var p = Marshal.ReadIntPtr(activates, (int)(i * IntPtr.Size));
                if (p != IntPtr.Zero) Marshal.Release(p);
            }
            CoTaskMemFree(activates);
        }
    }

    /// <summary>从 IMFActivate 读 MFT_FRIENDLY_NAME_Attribute。</summary>
    private static string? GetFriendlyName(IntPtr activate)
    {
        try
        {
            var attrs = (IMFAttributes)Marshal.GetObjectForIUnknown(activate);
            // MFT_FRIENDLY_NAME_Attribute = {314FFBAE-5B41-4C95-9C19-4E7D586Face};
            var key = new Guid("314FFBAE-5B41-4C95-9C19-4E7D586FACEF");
            var hr = attrs.GetAllocatedString(ref key, out var value, out _);
            if (hr < 0 || value == IntPtr.Zero) return null;
            try { return Marshal.PtrToStringUni(value); }
            finally { CoTaskMemFree(value); }
        }
        catch
        {
            return null;
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MFT_REGISTER_TYPE_INFO
    {
        public Guid guidMajorType;
        public Guid guidSubtype;
    }

    private static readonly Guid MFMediaType_Video = new("73646976-0000-0010-8000-00AA00389B71");
    private static readonly Guid MFVideoFormat_NV12 = new("3231564E-0000-0010-8000-00AA00389B71");
    private static readonly Guid MFVideoFormat_H264 = new("34363248-0000-0010-8000-00AA00389B71");

    private const uint MFT_CATEGORY_VIDEO_ENCODER = 0x00000006;

    [ComImport]
    [Guid("2CD2D921-C447-44A7-A13C-4ADABFC247E3")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMFAttributes
    {
        [PreserveSig] int GetItem(ref Guid key, IntPtr value);
        [PreserveSig] int GetItemType(ref Guid key, out uint type);
        [PreserveSig] int CompareItem(ref Guid key, IntPtr value, out int result);
        [PreserveSig] int Compare(IMFAttributes theirs, uint matchType, out int result);
        [PreserveSig] int GetUINT32(ref Guid key, out uint value);
        [PreserveSig] int GetUINT64(ref Guid key, out ulong value);
        [PreserveSig] int GetDouble(ref Guid key, out double value);
        [PreserveSig] int GetGUID(ref Guid key, out Guid value);
        [PreserveSig] int GetStringLength(ref Guid key, out uint length);
        [PreserveSig] int GetString(ref Guid key, [MarshalAs(UnmanagedType.LPWStr)] out string value, uint size, out uint length);
        [PreserveSig] int GetAllocatedString(ref Guid key, out IntPtr value, out uint length);
        [PreserveSig] int GetBlobSize(ref Guid key, out uint size);
        [PreserveSig] int GetBlob(ref Guid key, IntPtr buf, uint bufSize, out uint blobSize);
        [PreserveSig] int GetAllocatedBlob(ref Guid key, out IntPtr buf, out uint size);
        [PreserveSig] int GetUnknown(ref Guid key, ref Guid riid, out IntPtr value);
    }

    [DllImport("mfplat.dll", ExactSpelling = true)]
    private static extern int MFTEnumEx(
        uint guidCategory, uint flags,
        ref MFT_REGISTER_TYPE_INFO pInputType,
        ref MFT_REGISTER_TYPE_INFO pOutputType,
        out IntPtr pppMFTActivate, out uint pnumMFTActivate);

    [DllImport("ole32.dll")]
    private static extern void CoTaskMemFree(IntPtr ptr);
}
