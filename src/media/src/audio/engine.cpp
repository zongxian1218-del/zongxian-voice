// engine.cpp —— 引擎的全局初始化与关闭
//
// 负责的只有「进程级、全局一次」的事：
//   · COM 的初始化/反初始化（WASAPI、Media Foundation、WGC 都要 COM）
//   · 音频线程的调度优先级（MMCSS）—— 这一条直接决定会不会爆音
//
// 刻意不做的事：不在这里创建任何设备、源、会话。那些是可多次创建的，
// 放在各自的 _open/_create 里。全局状态越少越好。
#include "audio_engine.h"

#ifdef _WIN32
#  include <windows.h>
// objbase.h 提供 CoInitializeEx / CoUninitialize / RPC_E_CHANGED_MODE。
// 只写 windows.h 拿不到它们 —— 这一点很容易漏（windows.h 不包含 objbase.h）。
#  include <objbase.h>
#  include <avrt.h>
#endif

#include <atomic>
#include <mutex>

namespace zx {

namespace {

std::atomic<bool> g_engine_ready{false};
std::mutex g_engine_mutex;
bool g_com_initialized_here = false;

}  // namespace

bool EngineInitialize(std::string* reason) {
    std::lock_guard<std::mutex> lock(g_engine_mutex);
    if (g_engine_ready.load()) return true;

#ifdef _WIN32
    // MTA 而不是 STA：音频回调来自系统线程，MTA 下跨套间调用不会有
    // 消息泵依赖。如果宿主进程已经初始化过 COM（返回 RPC_E_CHANGED_MODE），
    // 那不是错误 —— 说明别人已经初始化好了，我们直接借用。
    const HRESULT hr = ::CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (SUCCEEDED(hr)) {
        g_com_initialized_here = true;
    } else if (hr == RPC_E_CHANGED_MODE) {
        g_com_initialized_here = false;   // 借用别人的，别去 CoUninitialize
    } else {
        if (reason) *reason = "无法初始化 COM（音频子系统依赖它）";
        return false;
    }

    // MMCSS：把音频相关线程注册成 "Pro Audio"，让调度器给它更高的优先级
    // 和更小的定时器粒度。不注册也能跑，但在系统忙的时候会听到断续。
    // 这里只是把进程标记好，真正的线程级注册在采集线程里做（见 media_api）。
    ::SetPriorityClass(::GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS);
#endif

    g_engine_ready.store(true);
    return true;
}

void EngineShutdown() {
    std::lock_guard<std::mutex> lock(g_engine_mutex);
    if (!g_engine_ready.load()) return;

#ifdef _WIN32
    if (g_com_initialized_here) {
        ::CoUninitialize();
        g_com_initialized_here = false;
    }
#endif

    g_engine_ready.store(false);
}

}  // namespace zx
