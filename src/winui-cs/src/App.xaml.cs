// App.xaml.cs —— 应用对象
//
// 职责刻意收窄到三件事：
//   · 建立主窗口
//   · 处理进程级异常（不让用户看到"闪一下就没了"）
//   · 维护窗口生命周期
//
// 业务逻辑一律不要放这里。Application 实例在整个进程里只有一个，
// 往里堆东西会让后面拆分模块时很痛苦。

using Microsoft.UI.Xaml;

namespace ZongxianVoice;

public partial class App : Application
{
    /// <summary>主窗口。用字段持有引用，避免被 GC 回收导致窗口消失。</summary>
    private Window? _mainWindow;

    public App()
    {
        InitializeComponent();

        // 未处理异常：默认行为是直接崩掉、什么都不显示，
        // 用户只会看到窗口闪一下。这里至少留下一条可诊断的信息。
        UnhandledException += OnUnhandledException;
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        // 启动阶段的异常必须被记下来。
        //
        // 为什么这么重要：WinUI 3 里 XAML 加载失败不会打印任何东西 ——
        // 进程直接以 0xC000027B（STATUS_STOWED_EXCEPTION）退出，事件日志
        // 里也可能什么都没有，只能在命令行看到一个没有任何上下文的退出码。
        // 把异常写进文件是唯一可靠的诊断手段。
        try
        {
            _mainWindow = new MainWindow();
            _mainWindow.Activate();
        }
        catch (Exception ex)
        {
            LogFatal("OnLaunched", ex);
            throw;
        }
    }

    /// <summary>
    /// 把致命错误写到 exe 同目录下的 startup-error.log。
    /// 用 exe 所在目录而不是临时目录：用户/开发者拿到这个文件夹就能直接看到。
    /// </summary>
    internal static void LogFatal(string stage, Exception ex)
    {
        try
        {
            var dir = AppContext.BaseDirectory;
            var path = System.IO.Path.Combine(dir, "startup-error.log");
            var text = $"""
                [{DateTime.Now:yyyy-MM-dd HH:mm:ss}] 启动失败
                阶段: {stage}
                类型: {ex.GetType().FullName}
                消息: {ex.Message}
                HRESULT: 0x{ex.HResult:X8}

                堆栈:
                {ex}

                """;
            System.IO.File.AppendAllText(path, text);
            System.Diagnostics.Debug.WriteLine(text);
        }
        catch
        {
            // 记日志本身失败就算了，不要因为这个再抛一次
        }
    }

    private void OnUnhandledException(object sender, Microsoft.UI.Xaml.UnhandledExceptionEventArgs e)
    {
        // 先记录下来，便于排查；Handle 置 true 让应用继续活着，
        // 而不是因为某个非致命异常整体退出。
        System.Diagnostics.Debug.WriteLine($"[未处理异常] {e.Message}\n{e.Exception}");
        e.Handled = true;
    }
}
