// MainWindow.cpp —— 主窗口的实现
#include "pch.h"

#include "MainWindow.h"

#include <microsoft.ui.xaml.window.h>   // IWindowNative（拿 HWND 用）

using namespace winrt;
using namespace Microsoft::UI::Xaml;
using namespace Microsoft::UI::Windowing;

namespace zx {

namespace {

// 窗口初始尺寸：按 UI 规范 §2 的三栏宽度反推 ——
// 240(左) + 480(中，最小) + 224(右) = 944，再加一点余量。
constexpr int kInitialWidth = 1180;
constexpr int kInitialHeight = 760;
constexpr int kMinWidth = 900;
constexpr int kMinHeight = 560;

// 取窗口的 HWND。WinUI 3 的 Window 不直接暴露 HWND，
// 要通过 IWindowNative 这个 COM 接口去要。
HWND GetHwnd(const Window& window) {
    HWND hwnd = nullptr;
    auto native = window.as<IWindowNative>();
    if (native) {
        native->get_WindowHandle(&hwnd);
    }
    return hwnd;
}

}  // namespace

MainWindow::MainWindow() {
    // 从 XAML 加载界面。XamlReader 会在编译期由 XamlCompiler 生成的
    // 初始化代码里可用；InitializeComponent 是生成的方法。
    InitializeComponent();

    ApplyTheme();
    BuildLayout();
}

void MainWindow::ApplyTheme() {
    // 跟随系统明暗主题，**不强制暗色**。
    //
    // 早先版本这里写的是 ElementTheme::Dark —— 那是"照抄 Discord 深色配色"
    // 的残留。既然要做符合 WinUI 的应用，就应该跟随系统：
    // 用户在"设置 → 个性化 → 颜色"里选了浅色，我们不该硬给他一个黑窗口。
    // 配色本身全部走语义画刷（见 App.xaml 与 ui-design-guideline §5），
    // 所以跟随主题时不需要改任何颜色代码。
    if (auto content = Content()) {
        content.as<FrameworkElement>().RequestedTheme(ElementTheme::Default);
    }

    // 标题栏也跟随系统。用 Default 而不是 Dark，避免出现
    // "内容区浅色、标题栏深色"的割裂。
    if (auto titleBar = window_.TitleBar()) {
        titleBar.PreferredTheme(winrt::Microsoft::UI::TitleBarTheme::UseDefaultAppMode);
    }

    // Mica 材质：WinUI 3 原生的窗口背景效果，本身就会随明暗主题变化。
    // 它在 Windows 10 上不可用，失败要静默降级（XAML 里的语义画刷已经
    // 保证了不用 Mica 也有正确的底色）。
    try {
        window_.SystemBackdrop(winrt::Microsoft::UI::Xaml::Media::MicaBackdrop());
    } catch (...) {
        // 不支持 Mica：什么都不做，退化为语义画刷的纯色背景。
    }

    window_.Title(L"棕仙语音");
}

void MainWindow::BuildLayout() {
    // 设置初始尺寸与最小尺寸。
    // WinUI 3 没有直接的 MinWidth/MinHeight，要通过 AppWindow 的
    // OverlappedPresenter 设置，或者处理 WM_GETMINMAXINFO。
    // 这里用 AppWindow 的方式，代码量最小。
    if (auto appWindow = window_.AppWindow()) {
        appWindow.Resize({kInitialWidth, kInitialHeight});

        if (auto presenter = appWindow.Presenter().try_as<OverlappedPresenter>()) {
            presenter.PreferredMinimumWidth(kMinWidth);
            presenter.PreferredMinimumHeight(kMinHeight);
        }
    }

    // 窗口居中（多显示器时在鼠标所在屏居中更自然，这里简化为主屏居中）
    if (HWND hwnd = GetHwnd(window_)) {
        RECT work{};
        if (::SystemParametersInfoW(SPI_GETWORKAREA, 0, &work, 0)) {
            const int w = work.right - work.left;
            const int h = work.bottom - work.top;
            const int x = work.left + (w - kInitialWidth) / 2;
            const int y = work.top + (h - kInitialHeight) / 2;
            ::SetWindowPos(hwnd, nullptr, x, y, 0, 0,
                           SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
        }
    }

    // W0 阶段：远端控制横幅保持隐藏。
    // 它的显示/隐藏逻辑属于 W5，这里先把控件找出来验证 XAML 里的名字能被解析到，
    // 名字写错会在 InitializeComponent 阶段就报错，早发现比晚发现好。
    if (auto banner = RootGrid().FindName(L"ControlBanner")) {
        banner.as<UIElement>().Visibility(Visibility::Collapsed);
    }
}

void MainWindow::Activate() {
    window_.Activate();
}

}  // namespace zx
