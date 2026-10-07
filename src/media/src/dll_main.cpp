// dll_main.cpp —— DLL 的入口点
//
// 什么都不做，只提供一个显式的 DllMain。理由：CMake 在 MSVC 上会为共享库
// 自动生成一个 DllMain，再自己写一个会冲突；显式提供可以让「这个 DLL 没有
// 初始化副作用」这件事在代码里看得见。
//
// 特别说明：**不要**在这里调用 zx_media_init()。DLL 加载时可能处在
// loader lock 下，那时初始化 COM 或创建线程都有死锁风险。
// 初始化必须由调用方在明确的位置显式发起。
#include <windows.h>

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved) {
    (void)instance;
    (void)reason;
    (void)reserved;
    return TRUE;
}
