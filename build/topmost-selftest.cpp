// topmost-selftest.cpp —— 二分：无边框覆盖窗口为什么拿不到 WS_EX_TOPMOST
//
// 背景（实测）：同一个 receiver-probe.exe
//   --window --topmost            → exStyle=0x00000108  TOPMOST=True
//   --window --frameless --topmost→ exStyle=0x08000080  TOPMOST=False   ← 应用用的就是这个
// 助手日志也打印"设置失败（首次 SetWindowPos 后=未带，补设后=未带）"。
//
// 两种形态的差别只有窗口样式：无边框那份是 WS_POPUP + WS_EX_TOOLWINDOW|WS_EX_NOACTIVATE。
// 这个程序把变量拆开，一次量清：到底是谁挡住的、延迟置顶能不能救。
//
// 用法: topmost-selftest.exe     （结果同时写 stdout 与 topmost-selftest.txt）

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <cstdio>
#include <string>
#include <vector>

static FILE* g_out = nullptr;

static void Report(const wchar_t* fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vfwprintf(stdout, fmt, ap);
    if (g_out) vfwprintf(g_out, fmt, ap);
    va_end(ap);
    fflush(stdout);
    if (g_out) fflush(g_out);
}

static bool IsTopmost(HWND h)
{
    return (GetWindowLongPtrW(h, GWL_EXSTYLE) & WS_EX_TOPMOST) != 0;
}

static void PumpFor(int ms)
{
    const DWORD end = GetTickCount() + (DWORD)ms;
    MSG msg;
    while (GetTickCount() < end) {
        while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        Sleep(10);
    }
}

struct Variant {
    const wchar_t* name;
    DWORD exStyle;
    DWORD style;
    bool deferredTopmost;   // true = 先显示、泵一会儿消息，再置顶
    bool probeSequence;     // true = 完全照探针的写法：不带 WS_VISIBLE 创建 + SW_SHOWNOACTIVATE
                            //        + UpdateWindow + 一次 SetWindowPos(带 SWP_SHOWWINDOW)
    bool showFlag;          // probeSequence 时是否带 SWP_SHOWWINDOW
};

// 探针的窗口过程里只多做了一件事：WM_ERASEBKGND 返回 1（画面交给 D3D）。
// 这里照抄，避免"是不是自定义 WndProc 影响的"这种变量没固定住。
static LRESULT CALLBACK TestWndProc(HWND h, UINT m, WPARAM wp, LPARAM lp)
{
    if (m == WM_ERASEBKGND) return 1;
    return DefWindowProcW(h, m, wp, lp);
}

static HWND MakeWindow(const Variant& v, int index)
{
    wchar_t cls[64];
    swprintf_s(cls, L"ZxTopmostTest%d", index);
    WNDCLASSEXW wc{};
    wc.cbSize = sizeof(wc);
    wc.lpfnWndProc = v.probeSequence ? TestWndProc : DefWindowProcW;
    wc.hInstance = GetModuleHandleW(nullptr);
    wc.lpszClassName = cls;
    RegisterClassExW(&wc);

    const DWORD style = v.probeSequence ? v.style : (v.style | WS_VISIBLE);
    HWND h = CreateWindowExW(v.exStyle, cls, L"topmost test", style,
                             100 + index * 60, 100 + index * 60, 300, 200,
                             nullptr, nullptr, GetModuleHandleW(nullptr), nullptr);
    if (!h) return nullptr;
    if (v.probeSequence) {
        ShowWindow(h, SW_SHOWNOACTIVATE);
        UpdateWindow(h);
    } else {
        ShowWindow(h, v.deferredTopmost ? SW_SHOWNOACTIVATE : SW_SHOWNORMAL);
    }
    return h;
}

int wmain()
{
    _wfopen_s(&g_out, L"topmost-selftest.txt", L"w, ccs=UTF-8");

    const std::vector<Variant> variants = {
        { L"A 现状：WS_POPUP + TOOLWINDOW|NOACTIVATE（覆盖窗口用的就是这个）",
          WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, WS_POPUP, false, false, true },
        { L"B 去掉 NOACTIVATE：WS_POPUP + TOOLWINDOW",
          WS_EX_TOOLWINDOW, WS_POPUP, false, false, true },
        { L"C 去掉 TOOLWINDOW：WS_POPUP + NOACTIVATE",
          WS_EX_NOACTIVATE, WS_POPUP, false, false, true },
        { L"D 都不带：WS_POPUP",
          0, WS_POPUP, false, false, true },
        { L"E 现状 + 延迟置顶（先显示、泵消息，之后才 SetWindowPos）",
          WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, WS_POPUP, true, false, true },
        { L"F 非 frameless 对照：WS_OVERLAPPED 系 + 无扩展样式",
          0, WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU, false, false, true },
        // 下面三组完全照探针的写法，逐项排除序列里的差异
        { L"G 探针序列：无 WS_VISIBLE 创建 + SW_SHOWNOACTIVATE + UpdateWindow + SWP_SHOWWINDOW",
          WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, WS_POPUP, false, true, true },
        { L"H 同 G，但不带 SWP_SHOWWINDOW",
          WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, WS_POPUP, false, true, false },
        { L"I 同 G，但先泵 400ms 消息再置顶",
          WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, WS_POPUP, true, true, true },
    };

    std::vector<HWND> hwnds;
    for (size_t i = 0; i < variants.size(); ++i) hwnds.push_back(MakeWindow(variants[i], (int)i));

    Report(L"创建后（还没置顶）：\n");
    for (size_t i = 0; i < variants.size(); ++i) {
        Report(L"  %-58s hwnd=%p TOPMOST=%s\n", variants[i].name, (void*)hwnds[i],
               hwnds[i] && IsTopmost(hwnds[i]) ? L"True" : L"False");
    }

    // 非延迟的那几组立刻置顶；延迟组先泵一会儿消息
    Report(L"\n置顶尝试：\n");
    for (size_t i = 0; i < variants.size(); ++i) {
        HWND h = hwnds[i];
        if (!h) { Report(L"  %-58s 窗口创建失败\n", variants[i].name); continue; }

        if (variants[i].deferredTopmost) PumpFor(400);   // 模拟"显示之后再来一次"

        const UINT swpFlags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE |
                              (variants[i].showFlag ? SWP_SHOWWINDOW : 0u);
        SetWindowPos(h, HWND_TOPMOST, 0, 0, 0, 0, swpFlags);
        const bool afterSwp = IsTopmost(h);

        // 再试一次显式设位（探针里也试过这条路）
        LONG_PTR ex = GetWindowLongPtrW(h, GWL_EXSTYLE);
        SetWindowLongPtrW(h, GWL_EXSTYLE, ex | WS_EX_TOPMOST);
        SetWindowPos(h, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
        const bool afterForce = IsTopmost(h);

        Report(L"  %-58s SetWindowPos后=%s  显式设位后=%s  exStyle=0x%08llX\n",
               variants[i].name, afterSwp ? L"True" : L"False",
               afterForce ? L"True" : L"False",
               (unsigned long long)GetWindowLongPtrW(h, GWL_EXSTYLE));
    }

    // 延迟 1.5 秒再看一遍：确认不是"先设上又被系统摘掉"
    PumpFor(1500);
    Report(L"\n1.5 秒后（检查是否被摘掉）：\n");
    for (size_t i = 0; i < variants.size(); ++i) {
        if (!hwnds[i]) continue;
        Report(L"  %-58s TOPMOST=%s\n", variants[i].name, IsTopmost(hwnds[i]) ? L"True" : L"False");
    }

    for (HWND h : hwnds) if (h) DestroyWindow(h);
    if (g_out) fclose(g_out);
    return 0;
}
