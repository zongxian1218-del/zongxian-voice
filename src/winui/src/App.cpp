// App.cpp —— 应用对象的实现
#include "pch.h"

#include "App.h"
#include "MainWindow.h"

using namespace winrt;
using namespace Microsoft::UI::Xaml;

namespace zx {

namespace {

// Application::Start 的回调是 void 返回，拿不到应用的退出码。
// 用一个匿名命名空间里的静态量把它带出来 —— 比给 App 加静态成员干净。
int g_exit_code = 0;

}  // namespace

int App::Run() {
    // Application::Start 会建立消息循环并阻塞到应用退出。
    // 参数是一个「创建 Application 派生类实例」的委托。
    Application::Start([](const ApplicationInitializationCallbackParams&) {
        // make 出来的实例由 XAML 框架持有引用，不需要我们保存。
        // 注意：不要在这里 catch —— 抛出去会被框架吞掉并变成静默退出，
        // 让它在 main 的 try 里冒出来，才能打出可读的错误。
        make<App>();
    });
    return g_exit_code;
}

void App::OnLaunched(const LaunchActivatedEventArgs& /*args*/) {
    // 创建主窗口并激活。
    //
    // 这里用 make_self 取得 shared_ptr：MainWindow 里需要把自身弱引用
    // 交给一些回调（例如共享状态变化），用 shared/weak 成对管理，
    // 避免窗口关闭后回调还在跑。
    auto window = std::make_shared<MainWindow>();
    main_window_ = window;
    window->Activate();

    // 窗口关闭 = 应用退出。WinUI 3 不会自动这么做（一个进程可以有多个窗口），
    // 我们目前是单窗口应用，所以显式退出。
    window->Closed([this](auto&&, auto&&) {
        main_window_.reset();
        // 退出码固定 0；真有错误会在 main 的异常路径里体现。
        g_exit_code = 0;
        Application::Current().Exit();
    });
}

}  // namespace zx
