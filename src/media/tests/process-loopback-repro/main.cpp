// 最小复现：完全照微软官方 ApplicationLoopback 示例的写法调用"进程回环"。
//
// 目的：区分两件事
//   · 官方写法在这台机器上能跑  -> 崩溃是我们代码的问题（照官方改）
//   · 官方写法也崩              -> 这台机器的系统 API 有问题（换方案）
//
// 构建（见 tmp/loopback-min/build.cmd）：
//   cl /nologo /EHsc /std:c++17 main.cpp /link mmdevapi.lib ole32.lib runtimeobject.lib

#include <windows.h>
#include <audioclient.h>
#include <mmdeviceapi.h>
#include <audioclientactivationparams.h>
#include <wrl/implements.h>
#include <wrl/client.h>
#include <cstdio>

using Microsoft::WRL::ComPtr;
using Microsoft::WRL::RuntimeClass;
using Microsoft::WRL::RuntimeClassFlags;
using Microsoft::WRL::ClassicCom;
using Microsoft::WRL::FtmBase;

class Handler : public RuntimeClass<RuntimeClassFlags<ClassicCom>,
                                    IActivateAudioInterfaceCompletionHandler,
                                    FtmBase> {
public:
    HRESULT STDMETHODCALLTYPE ActivateCompleted(
        IActivateAudioInterfaceAsyncOperation* operation) override {
        HRESULT hrActivate = E_FAIL, hrGet = E_FAIL;
        ComPtr<IUnknown> activated;
        hrGet = operation->GetActivateResult(&hrActivate, activated.GetAddressOf());
        printf("[cb] ActivateCompleted hrGet=0x%08X hrActivate=0x%08X\n",
               (unsigned)hrGet, (unsigned)hrActivate);
        fflush(stdout);
        return S_OK;
    }
};

int main() {
    HRESULT hrCo = ::CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    printf("[0] CoInitializeEx(MTA) hr=0x%08X\n", (unsigned)hrCo);
    fflush(stdout);

    AUDIOCLIENT_ACTIVATION_PARAMS params = {};
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    params.ProcessLoopbackParams.TargetProcessId = ::GetCurrentProcessId();
    params.ProcessLoopbackParams.ProcessLoopbackMode =
        PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE;
    printf("[1] 参数就绪 (pid=%lu)\n", (unsigned long)::GetCurrentProcessId());
    fflush(stdout);

    PROPVARIANT pv = {};
    pv.vt = VT_BLOB;
    pv.blob.cbSize = sizeof(params);
    pv.blob.pBlobData = reinterpret_cast<BYTE*>(&params);

    auto handler = Microsoft::WRL::Make<Handler>();
    printf("[2] handler 就绪\n");
    fflush(stdout);

    ComPtr<IActivateAudioInterfaceAsyncOperation> op;
    printf("[3] 调 ActivateAudioInterfaceAsync ...\n");
    fflush(stdout);
    HRESULT hr = ::ActivateAudioInterfaceAsync(
        L"VAD\\Process_Loopback", __uuidof(IAudioClient), &pv,
        handler.Get(), op.GetAddressOf());
    printf("[4] 已返回 hr=0x%08X\n", (unsigned)hr);
    fflush(stdout);

    ::Sleep(2000);
    printf("[5] 正常结束\n");
    fflush(stdout);
    ::CoUninitialize();
    return 0;
}
