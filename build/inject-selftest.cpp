// inject-selftest.cpp —— 最小化验证：原生 exe 调 SendInput 到底有没有效果
//
// 背景：在 sender-probe 里 SendInput 返回 1、结构布局也正确，但光标纹丝不动；
// 而在 PowerShell 里同样参数的调用却生效。这个程序把变量压到最小（不碰 D3D/MF/Winsock），
// 用来区分"是环境限制"还是"我的代码/进程状态有问题"。
//
// 用法: inject-selftest.exe [out.txt]
//       把结果同时写 stdout 和文件（文件路径必须给工作区内的，否则写不进去）

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <stdio.h>

static void Report(FILE* f, const wchar_t* line)
{
    fwprintf(stdout, L"%s\n", line);
    if (f) fwprintf(f, L"%s\n", line);
}

int wmain(int argc, wchar_t** argv)
{
    FILE* f = nullptr;
    if (argc > 1) _wfopen_s(&f, argv[1], L"w, ccs=UTF-8");

    wchar_t buf[512];
    POINT before{}, after{};
    GetCursorPos(&before);
    swprintf_s(buf, L"起点光标 = (%ld,%ld)", before.x, before.y);
    Report(f, buf);

    // ① 相对移动：不依赖坐标映射
    INPUT rel{};
    rel.type = INPUT_MOUSE;
    rel.mi.dwFlags = MOUSEEVENTF_MOVE;
    rel.mi.dx = 40;
    rel.mi.dy = 40;
    SetLastError(0);
    const UINT r1 = SendInput(1, &rel, sizeof(INPUT));
    const DWORD e1 = GetLastError();
    Sleep(200);
    GetCursorPos(&after);
    swprintf_s(buf, L"① 相对(+40,+40): SendInput=%u err=%u 光标=(%ld,%ld)", r1, e1, after.x, after.y);
    Report(f, buf);

    // ② 绝对移动（虚拟桌面 0..65535）
    INPUT abs{};
    abs.type = INPUT_MOUSE;
    abs.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK;
    abs.mi.dx = 32768;
    abs.mi.dy = 32768;
    SetLastError(0);
    const UINT r2 = SendInput(1, &abs, sizeof(INPUT));
    const DWORD e2 = GetLastError();
    Sleep(200);
    GetCursorPos(&after);
    swprintf_s(buf, L"② 绝对(中心): SendInput=%u err=%u 光标=(%ld,%ld)", r2, e2, after.x, after.y);
    Report(f, buf);

    if (f) fclose(f);
    return 0;
}
