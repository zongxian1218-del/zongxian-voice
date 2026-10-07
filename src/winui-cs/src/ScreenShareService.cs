/*
ScreenShareService.cs —— 屏幕共享的**状态机**（S2 拆桥，2026-10-06）。

【它负责什么】把"我方在共享 / 正在请求中 / 在观看对方"这三个状态收在一处，
并提供合法的状态迁移 —— 以前这三个 bool 散落在 MainWindow 各处被随手赋值，
出错时（例如失败路径忘了复位 `_screenBusy`）界面上就是"按钮灰着点不动"。

【它不负责什么】不点按钮、不调页面、不碰控件：真正的请求/停止由 UI 层调用，
本类只回答"现在是什么状态、能不能再发起、失败后该怎么复位"。
拆桥前在 MainWindow 里是 `_sharingScreen`/`_screenBusy`/`_viewingRemoteScreen` 三个散落的字段。

【状态含义（务必分清）】
  · Sharing       —— 我方正在采集并发布视频轨
  · Busy          —— 已经发出请求、还没收到结果（此时不允许再次发起）
  · ViewingRemote —— 正在看**对方**的画面（本机没在采集）
*/
namespace ZongxianVoice;

internal sealed class ScreenShareService
{
    public bool Sharing { get; private set; }
    public bool Busy { get; private set; }
    public bool ViewingRemote { get; private set; }

    /// <summary>我方共享状态改变时触发（true=开始共享，false=停止/失败）。</summary>
    public event System.Action<bool>? SharingChanged;
    /// <summary>Busy 改变时触发（UI 用它决定按钮可用性与提示）。</summary>
    public event System.Action<bool>? BusyChanged;

    /// <summary>能否再发起一次共享（正在共享或正在请求时都不能）。</summary>
    public bool CanRequest => !Sharing && !Busy;

    /// <summary>发出共享请求：进入 Busy，并让出"观看对方"的画面。</summary>
    public void RequestStarted()
    {
        ViewingRemote = false;
        SetBusy(true);
    }

    /// <summary>共享成功启动（页面回报 screen-started）。</summary>
    public void Started()
    {
        SetSharing(true);
        SetBusy(false);
    }

    /// <summary>
    /// 共享结束/失败/取消 —— **无论哪种原因都必须调用它**，
    /// 否则界面会停在"请求中"：按钮灰着、用户以为功能坏了。
    /// </summary>
    public void Stopped()
    {
        SetSharing(false);
        SetBusy(false);
    }

    /// <summary>开始观看对方画面（我方没在采集）。</summary>
    public void BeginViewingRemote()
    {
        ViewingRemote = true;
        SetSharing(false);
        SetBusy(false);
    }

    /// <summary>停止观看对方。</summary>
    public void EndViewingRemote() => ViewingRemote = false;

    private void SetSharing(bool v)
    {
        if (Sharing == v) return;
        Sharing = v;
        SharingChanged?.Invoke(v);
    }

    private void SetBusy(bool v)
    {
        if (Busy == v) return;
        Busy = v;
        BusyChanged?.Invoke(v);
    }
}
