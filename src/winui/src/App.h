// App.h —— 应用对象
#pragma once

#include "pch.h"

namespace zx {

// WinUI 3 的 Application 派生类。
//
// 它负责的只有三件事：
//   · 建立窗口
//   · 加载全局资源（主题、画刷）
//   · 维护窗口生命周期（最后一个窗口关闭就退出）
//
// 业务逻辑一律不要放这里 —— Application 对象在整个进程里只有一个，
// 往里堆东西会让后面拆分会话/设置/共享等模块时很痛苦。
class App : public winrt::Microsoft::UI::Xaml::ApplicationT<App> {
public:
    App() = default;

    // 由 main() 调用，内部建立消息循环，窗口关闭后返回退出码。
    int Run();

    // XAML 框架在应用启动时回调，这里做第一件事：建主窗口。
    void OnLaunched(const winrt::Microsoft::UI::Xaml::LaunchActivatedEventArgs& args);

private:
    // 主窗口。用 shared_ptr 而不是裸指针：WinUI 的窗口在关闭时的
    // 销毁时机不完全由我们控制，裸指针会带来"窗口已关但事件还在回调"的悬垂。
    std::shared_ptr<class MainWindow> main_window_;
};

}  // namespace zx
