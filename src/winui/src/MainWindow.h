// MainWindow.h —— 主窗口
#pragma once

#include "pch.h"

namespace zx {

// 主窗口。W0 阶段只做一件事：把三栏骨架与深色主题搭出来，
// 不接任何功能（不连信令、不开麦克风）。
//
// 为什么先做骨架：UI 结构定型之后再往里塞功能，比先做功能再改布局便宜得多。
// 而且骨架能让界面评审变成"看真东西"，而不是看文档想象。
class MainWindow {
public:
    MainWindow();

    // 激活（显示）窗口
    void Activate();

    // 关闭事件，交给 App 决定是否退出应用
    template <typename Handler>
    void Closed(Handler&& h) {
        window_.Closed(std::forward<Handler>(h));
    }

    winrt::Microsoft::UI::Xaml::Window Window() const { return window_; }

private:
    // 搭三栏骨架：左（会话列表）/ 中（主区域）/ 右（成员列表）
    void BuildLayout();

    // 应用深色主题与配色（见 docs/ui-design-guideline-2026-10-04.md §5）
    void ApplyTheme();

    winrt::Microsoft::UI::Xaml::Window window_{nullptr};
};

}  // namespace zx
