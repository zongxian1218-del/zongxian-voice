// d3d11-renderer.h —— 把一个 HWND 变成视频显示窗口
//
// ============================================================================
// 【为什么用 BGRA 而不是 NV12 + 着色器】
// NV12 上屏通常要写 HLSL 做色彩空间转换，还要处理 NV12 纹理的双平面
// （ArraySize 必须是 2，subresource 0 是 Y、1 是 UV），容易出错。
// 直接让 libavcodec 的 sws_scale 输出 BGRA，就只剩「上传一张纹理 + 拷贝到
// 后台缓冲 + Present」三步，没有任何着色器代码。
//
// 代价是 CPU→GPU 的带宽从 3.1 MB/帧 涨到 8.3 MB/帧（1080p30 就是 250 MB/s）。
// 相对 PCIe 和内存带宽这点量完全可以忽略，换来的是**不可能出错**的简单路径。
//
// 【为什么单独做成组件】
// 这是将来接到 WinUI 时唯一需要替换的部分：那个场景下 SwapChainPanel 会
// 提供交换链，本组件换成"往共享纹理里写"即可，解码/传输完全不用动。
// ============================================================================

#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <wrl/client.h>

#include <cstdint>

namespace zx {

// 【必须放在命名空间作用域】写在类内部会让 MSVC 把 ComPtr<...> 解析成
// 「缺参数列表的函数模板」(C7568)。这个坑在 h264-decoder.h 里已经踩过一次。
using Microsoft::WRL::ComPtr;

class D3D11WindowRenderer {
public:
    ~D3D11WindowRenderer() { Close(); }
    D3D11WindowRenderer() = default;
    D3D11WindowRenderer(const D3D11WindowRenderer&) = delete;
    D3D11WindowRenderer& operator=(const D3D11WindowRenderer&) = delete;

    bool Init(HWND hwnd, UINT width, UINT height, bool vsync = false)
    {
        hwnd_ = hwnd;
        w_ = width;
        h_ = height;
        vsync_ = vsync;

        UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
        D3D_FEATURE_LEVEL level = D3D_FEATURE_LEVEL_11_0;
        HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, flags,
                                       nullptr, 0, D3D11_SDK_VERSION,
                                       &device_, &level, &ctx_);
        if (FAILED(hr)) return false;

        ComPtr<IDXGIDevice> dxgiDev;
        if (FAILED(device_.As(&dxgiDev))) return false;
        ComPtr<IDXGIAdapter> adapter;
        if (FAILED(dxgiDev->GetAdapter(&adapter))) return false;
        ComPtr<IDXGIFactory2> factory;
        if (FAILED(adapter->GetParent(IID_PPV_ARGS(&factory)))) return false;

        // 【必须关掉 DXGI 自己的 Alt+Enter 全屏】否则按错键会打乱显示状态
        factory->MakeWindowAssociation(hwnd, DXGI_MWA_NO_ALT_ENTER);

        return CreateSwapChain(factory.Get(), width, height);
    }

    // 上传一帧 BGRA 并显示。pitch 是源数据的行字节数（可能大于 width*4）。
    //
    // 【为什么要带上源宽高】解码器输出的是**码流本身的分辨率**，不一定等于建窗时的预期
    // （例如助手按 1280x720 启动、实际推来 1920x1080）。后台缓冲必须和源纹理同尺寸，
    // 否则 CopyResource 只会拷到左上角一块 —— 表现为"画面被裁掉、位置也不对"，很容易误判成坐标问题。
    // 这里发现尺寸变了就重建后台缓冲；**窗口尺寸不受影响**，DXGI_SCALING_STRETCH 会把画面
    // 拉伸到窗口客户区（所以"后台缓冲尺寸"和"窗口尺寸"是两个独立的东西）。
    bool PresentBgra(const uint8_t* data, UINT pitch, UINT srcW = 0, UINT srcH = 0)
    {
        if (srcW && srcH && (srcW != w_ || srcH != h_)) {
            Resize(srcW, srcH);
        }
        if (!swap_ || !staging_) return false;

        ComPtr<ID3D11Texture2D> back;
        if (FAILED(swap_->GetBuffer(0, IID_PPV_ARGS(&back)))) return false;

        // UpdateSubresource 会处理 pitch 与纹理行距不一致的情况
        ctx_->UpdateSubresource(staging_.Get(), 0, nullptr, data, pitch, 0);
        ctx_->CopyResource(back.Get(), staging_.Get());

        // 不做垂直同步：等 vblank 会平白加上最多 16ms 延迟
        const HRESULT hr = swap_->Present(vsync_ ? 1 : 0, 0);
        ++frames_;
        return SUCCEEDED(hr);
    }

    void Resize(UINT width, UINT height)
    {
        if (!swap_ || width == 0 || height == 0) return;
        if (width == w_ && height == h_) return;
        ctx_->ClearState();
        staging_.Reset();
        if (FAILED(swap_->ResizeBuffers(0, width, height, DXGI_FORMAT_UNKNOWN, 0))) return;
        w_ = width;
        h_ = height;
        ComPtr<IDXGIDevice> dxgiDev;
        device_.As(&dxgiDev);
        ComPtr<IDXGIAdapter> adapter;
        dxgiDev->GetAdapter(&adapter);
        ComPtr<IDXGIFactory2> factory;
        adapter->GetParent(IID_PPV_ARGS(&factory));
        CreateStaging(width, height);
    }

    UINT framesPresented() const { return frames_; }
    UINT width() const { return w_; }
    UINT height() const { return h_; }
    bool valid() const { return swap_ && staging_; }

    void Close()
    {
        staging_.Reset();
        swap_.Reset();
        ctx_.Reset();
        device_.Reset();
        hwnd_ = nullptr;
    }

private:
    bool CreateSwapChain(IDXGIFactory2* factory, UINT width, UINT height)
    {
        DXGI_SWAP_CHAIN_DESC1 sd{};
        sd.Width = width;
        sd.Height = height;
        sd.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        sd.SampleDesc.Count = 1;
        sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
        sd.BufferCount = 2;
        // 翻转模型比 bitblt 模型更适合视频显示；注意翻转模型的后台缓冲不能直接 Map
        sd.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
        sd.AlphaMode = DXGI_ALPHA_MODE_IGNORE;

        HRESULT hr = factory->CreateSwapChainForHwnd(device_.Get(), hwnd_, &sd,
                                                     nullptr, nullptr, &swap_);
        if (FAILED(hr)) {
            // 少数老驱动不支持 FLIP，退回 bitblt 模型
            sd.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;
            sd.BufferCount = 1;
            hr = factory->CreateSwapChainForHwnd(device_.Get(), hwnd_, &sd,
                                                 nullptr, nullptr, &swap_);
            if (FAILED(hr)) return false;
        }
        return CreateStaging(width, height);
    }

    bool CreateStaging(UINT width, UINT height)
    {
        D3D11_TEXTURE2D_DESC td{};
        td.Width = width;
        td.Height = height;
        td.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        td.ArraySize = 1;
        td.MipLevels = 1;
        td.SampleDesc.Count = 1;
        td.Usage = D3D11_USAGE_DEFAULT;     // UpdateSubresource 的目标
        td.BindFlags = 0;
        return SUCCEEDED(device_->CreateTexture2D(&td, nullptr, &staging_));
    }

    HWND hwnd_ = nullptr;
    ComPtr<ID3D11Device> device_;
    ComPtr<ID3D11DeviceContext> ctx_;
    ComPtr<IDXGISwapChain1> swap_;
    ComPtr<ID3D11Texture2D> staging_;
    UINT w_ = 0, h_ = 0;
    UINT frames_ = 0;
    bool vsync_ = false;
};

} // namespace zx
