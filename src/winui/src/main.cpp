// main.cpp —— 进程入口
//
// WinUI 3 桌面应用的入口有两件事必须在 main 里做（不能延迟）：
//   1) 用 Bootstrap 初始化 Windows App SDK 运行时。
//      少了它，任何 WinUI 调用都会以 "class not registered" 失败。
//   2) 初始化 COM 套间为 STA。WinUI 的 UI 线程必须是单线程套间。
//
// 注意这里**不用** WinMain：WinUI 3 的标准做法是自带 main，
// 由 XamlControlsResources 等自己处理消息循环（见 App.cpp 的 Application::Start）。
#include "pch.h"

#include "App.h"

#include <MddBootstrap.h>          // Windows App SDK 引导
#include <wil/result_macros.h>     // 失败即抛出，便于定位

#include <cstdio>

namespace {

// 启动失败时给出可操作的提示，而不是让用户看到一个闪退。
// 这类"窗口没出现就退出"的问题最难排查，所以宁可多打印几行。
int ReportFatal(const wchar_t* what, HRESULT hr) {
    wchar_t buf[512];
    ::swprintf_s(buf,
                 L"棕仙语音启动失败\n\n"
                 L"阶段：%s\n"
                 L"HRESULT：0x%08lX\n\n"
                 L"常见原因：\n"
                 L"  · Windows App SDK 运行时未安装（开发期应为自包含部署）\n"
                 L"  · 系统版本过低（需要 Windows 10 1809 及以上）\n",
                 what, static_cast<unsigned long>(hr));
    ::MessageBoxW(nullptr, buf, L"棕仙语音", MB_OK | MB_ICONERROR);
    std::fwprintf(stderr, L"[fatal] %s hr=0x%08lX\n", what,
                  static_cast<unsigned long>(hr));
    return static_cast<int>(hr);
}

}  // namespace

int APIENTRY wWinMain(HINSTANCE, HINSTANCE, LPWSTR, int) {
    // ---- 1) 引导 Windows App SDK ----
    // 版本号用 0 表示"任意可用的 1.x"，开发期最省事。
    // 发布时应改成具体的 major.minor，避免运行时被意外升级到不兼容版本。
    const HRESULT boot = ::MddBootstrapInitialize(0x00010007 /*1.7*/, nullptr, 0);
    if (FAILED(boot)) {
        return ReportFatal(L"初始化 Windows App SDK 运行时", boot);
    }

    // ---- 2) 初始化 COM（UI 线程必须是 STA）----
    const HRESULT com = ::CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    if (FAILED(com)) {
        ::MddBootstrapShutdown();
        return ReportFatal(L"初始化 COM 套间（STA）", com);
    }

    int exit_code = 0;
    try {
        // 注意：这里**不要**再调 winrt::init_apartment。
        // 上面已经用 CoInitializeEx 把本线程设成了 STA，而 init_apartment
        // 在已初始化的线程上会抛 RPC_E_CHANGED_MODE（或行为不确定）。
        // C++/WinRT 在 STA 线程上直接可用，不需要额外初始化。
        zx::App app;
        exit_code = app.Run();
    } catch (const winrt::hresult_error& e) {
        exit_code = ReportFatal(L"运行应用主循环", e.code());
    } catch (...) {
        exit_code = ReportFatal(L"运行应用主循环（未知异常）", E_FAIL);
    }

    ::CoUninitialize();
    ::MddBootstrapShutdown();
    return exit_code;
}
