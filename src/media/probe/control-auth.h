// control-auth.h —— 被控端的「远程控制授权」状态机 + 极简 UI
//
// 对齐设计文档 §9.5 的几条硬要求：
//   · **默认拒绝**：授权窗口的默认焦点在「拒绝」，回车即拒绝；30 秒无响应自动拒绝
//   · **键盘默认关**：窗口里有「允许键盘输入」勾选框，默认不勾
//   · **被控时常驻顶部红条**，「停止控制」一击生效（不再二次确认）
//   · **全局快捷键**也能停止控制（默认 Ctrl+Alt+End）
//   · 未授权期间收到的输入**一律丢弃**（不注入），并如实记录
//
// 用法（在发送端的帧循环里）：
//     auth.Poll();                                  // 每轮调一次：处理窗口消息与热键
//     if (auth.AnyInputRequested()) { auth.Ask(peer, ctrlSock); }   // 首次收到输入时弹窗
//     if (auth.AllowMouse()) { ...注入鼠标... }
//     if (auth.AllowKeyboard()) { ...注入键盘... }
//
// 注意：UI 必须在**有消息循环的线程**上创建 —— 发送端主循环里加一句 Poll() 即可。
#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <functional>
#include <string>

namespace zx {

class ControlAuth {
public:
    using Logger = std::function<void(const std::wstring&)>;

    void SetLogger(Logger l) { log_ = std::move(l); }

    // 由外部注入"把状态发给查看端"的实现（这个头不依赖传输层）
    std::function<void(uint8_t)> StateSender;

    // 授权窗口关闭前收到的输入要不要算作"有人请求控制"
    bool Idle() const { return state_ == kIdle; }
    bool Pending() const { return state_ == kPending; }
    bool AllowMouse() const { return state_ == kAllowed; }
    bool AllowKeyboard() const { return state_ == kAllowed && keyboardAllowed_; }
    uint8_t State() const { return (uint8_t)state_; }
    bool DialogVisible() const { return dlg_ != nullptr; }
    const sockaddr_in& Peer() const { return peer_; }

    // 弹出授权窗口（幂等：已经在等就不再弹）
    void Ask(const sockaddr_in& peer)
    {
        if (state_ == kPending) return;
        const bool reask = (state_ == kDenied || state_ == kStopped);
        peer_ = peer;
        state_ = kPending;
        keyboardAllowed_ = false;
        countdown_ = timeoutSec_;
        CreateDialogWindow(reask);
        SendState(kStatePending);
    }

    // 【Bug 修复】拒绝/超时/停止之后，必须有办法重新请求 —— 否则本进程内这个功能就废了。
    // 规则：换了个请求方（IP 或端口不同）立即可以再问；同一个人要等过冷静期（默认 30 秒），
    // 免得被同一个人反复弹窗骚扰。
    bool ShouldReask(const sockaddr_in& peer) const
    {
        if (state_ != kDenied && state_ != kStopped) return false;
        if (!hasDeniedPeer_) return true;
        if (peer.sin_addr.s_addr != lastDeniedPeer_.sin_addr.s_addr ||
            peer.sin_port != lastDeniedPeer_.sin_port) {
            return true;   // 换人了
        }
        const DWORD elapsedSec = (GetTickCount() - lastDeniedTick_) / 1000;
        return elapsedSec >= (DWORD)cooldownSec_;
    }

    void SetCooldownSeconds(int s) { cooldownSec_ = (s >= 0) ? s : 0; }

    // 让它每秒被调一次；也可以每轮都调（内部自己按 100ms 节流）
    void Poll()
    {
        MSG msg;
        while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        // 倒计时：到 0 自动拒绝（对齐"30 秒无响应即拒绝"）
        if (state_ == kPending && dlg_) {
            const DWORD now = GetTickCount();
            if (now - lastTick_ >= 1000) {
                lastTick_ = now;
                if (countdown_ > 0) {
                    --countdown_;
                    SetWindowTextW(GetDlgItem(dlg_, kIdCountdown), CountdownText().c_str());
                }
                if (countdown_ == 0) {
                    wchar_t b[96];
                    swprintf_s(b, L"  [授权] %d 秒无响应 → 自动拒绝", timeoutSec_);
                    Log(b);
                    Finish(kDenied);
                }
            }
        }
    }

    // 本人在界面上点「停止控制」或按全局热键
    void StopByUser(const wchar_t* how)
    {
        if (state_ != kAllowed) return;
        Log(std::wstring(L"  [授权] 收到停止控制（") + how + L"）→ 立即停止注入");
        Finish(kStopped);
    }

    void SetTimeoutSeconds(int s) { timeoutSec_ = (s > 0) ? s : 1; countdown_ = timeoutSec_; }
    void SetHotkeyText(const std::wstring& t) { hotkeyText_ = t; }

private:
    // 【命名注意】枚举不能叫 State —— 会和 State() 访问器撞名，类内解析成成员函数，
    // 表现是 Finish(State s) 编译不过。值直接对齐 media-packet.h 里的 ControlState，
    // 免得"内部状态"和"线上状态"两套编号互相错位（这个头故意不依赖传输层，所以不直接引用）。
    enum AuthState {
        kIdle    = 0xFF,   // 还没有人请求控制（不发给对端）
        kPending = 0,      // = kStatePending
        kAllowed = 1,      // = kStateAllowed
        kDenied  = 2,      // = kStateDenied
        kStopped = 3,      // = kStateStopped
    };
    static const int kIdAllow = 101, kIdDeny = 102, kIdKeyboard = 103;
    static const int kIdCountdown = 104, kIdStop = 201, kIdBannerText = 202;

    void Log(const std::wstring& s) { if (log_) log_(s); }

    std::wstring CountdownText() const
    {
        wchar_t b[64];
        swprintf_s(b, L"（%d 秒无响应将自动拒绝）", countdown_);
        return b;
    }

    static LRESULT CALLBACK DlgProc(HWND h, UINT m, WPARAM wp, LPARAM lp)
    {
        ControlAuth* self = reinterpret_cast<ControlAuth*>(GetWindowLongPtrW(h, GWLP_USERDATA));
        if (m == WM_NCCREATE) {
            auto* cs = reinterpret_cast<CREATESTRUCTW*>(lp);
            self = reinterpret_cast<ControlAuth*>(cs->lpCreateParams);
            SetWindowLongPtrW(h, GWLP_USERDATA, (LONG_PTR)self);
        }
        if (!self) return DefWindowProcW(h, m, wp, lp);
        if (self->dlg_ != h) self->dlg_ = h;

        switch (m) {
        case WM_COMMAND: {
            const int id = LOWORD(wp);
            if (id == kIdAllow) {
                self->keyboardAllowed_ =
                    (SendMessageW(GetDlgItem(h, kIdKeyboard), BM_GETCHECK, 0, 0) == BST_CHECKED);
                self->Log(std::wstring(L"  [授权] 本人点了「允许」") +
                          (self->keyboardAllowed_ ? L"（含键盘）" : L"（仅鼠标，键盘未勾选）"));
                self->Finish(kAllowed);
            } else if (id == kIdDeny) {
                self->Log(L"  [授权] 本人点了「拒绝」");
                self->Finish(kDenied);
            }
            return 0;
        }
        case WM_TIMER:
            return 0;
        case WM_CLOSE:
            self->Log(L"  [授权] 窗口被关闭 → 视为拒绝");
            self->Finish(kDenied);
            return 0;
        case WM_DESTROY:
            self->dlg_ = nullptr;
            return 0;
        default:
            return DefWindowProcW(h, m, wp, lp);
        }
    }

    static LRESULT CALLBACK BannerProc(HWND h, UINT m, WPARAM wp, LPARAM lp)
    {
        ControlAuth* self = reinterpret_cast<ControlAuth*>(GetWindowLongPtrW(h, GWLP_USERDATA));
        if (m == WM_NCCREATE) {
            auto* cs = reinterpret_cast<CREATESTRUCTW*>(lp);
            self = reinterpret_cast<ControlAuth*>(cs->lpCreateParams);
            SetWindowLongPtrW(h, GWLP_USERDATA, (LONG_PTR)self);
        }
        if (!self) return DefWindowProcW(h, m, wp, lp);
        if (self->banner_ != h) self->banner_ = h;

        switch (m) {
        case WM_COMMAND:
            if (LOWORD(wp) == kIdStop) {
                self->StopByUser(L"点击「停止控制」");
                return 0;
            }
            return 0;
        case WM_HOTKEY:
            self->StopByUser(self->hotkeyText_.empty() ? L"全局快捷键" : self->hotkeyText_.c_str());
            return 0;
        case WM_CTLCOLORSTATIC: {
            // 红条：白字 + 纯红底（设计里"全应用唯一纯红"就用在被控提示上）
            HDC dc = (HDC)wp;
            SetTextColor(dc, RGB(255, 255, 255));
            SetBkColor(dc, RGB(200, 30, 30));
            return (LRESULT)GetStockObject(NULL_BRUSH);
        }
        case WM_ERASEBKGND: {
            RECT rc; GetClientRect(h, &rc);
            HBRUSH br = CreateSolidBrush(RGB(200, 30, 30));
            FillRect((HDC)wp, &rc, br);
            DeleteObject(br);
            return 1;
        }
        case WM_DESTROY:
            self->banner_ = nullptr;
            return 0;
        default:
            return DefWindowProcW(h, m, wp, lp);
        }
    }

    void RegisterClasses()
    {
        if (classesReady_) return;
        HINSTANCE inst = GetModuleHandleW(nullptr);
        WNDCLASSEXW wc{};
        wc.cbSize = sizeof(wc);
        wc.lpfnWndProc = DlgProc;
        wc.hInstance = inst;
        wc.lpszClassName = L"ZxControlAuthDlg";
        wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
        wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
        RegisterClassExW(&wc);

        wc.lpfnWndProc = BannerProc;
        wc.lpszClassName = L"ZxControlBanner";
        wc.hbrBackground = nullptr;   // 自己填红底
        RegisterClassExW(&wc);
        classesReady_ = true;
    }

    static HWND MakeChild(HWND parent, const wchar_t* cls, const wchar_t* text,
                          DWORD style, int x, int y, int w, int h, int id)
    {
        HWND c = CreateWindowExW(0, cls, text, WS_CHILD | WS_VISIBLE | style,
                                 x, y, w, h, parent, (HMENU)(INT_PTR)id,
                                 GetModuleHandleW(nullptr), nullptr);
        return c;
    }

    void CreateDialogWindow(bool reask)
    {
        RegisterClasses();
        const int w = 460, h = 240;
        const int x = (GetSystemMetrics(SM_CXSCREEN) - w) / 2;
        const int y = (GetSystemMetrics(SM_CYSCREEN) - h) / 2;
        dlg_ = CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW, L"ZxControlAuthDlg",
                               L"远程控制请求", WS_POPUP | WS_CAPTION | WS_SYSMENU,
                               x, y, w, h, nullptr, nullptr, GetModuleHandleW(nullptr), this);
        if (!dlg_) { Log(L"  [授权] 创建授权窗口失败 → 按拒绝处理"); Finish(kDenied); return; }

        MakeChild(dlg_, L"STATIC",
                  L"有人请求控制你的电脑。\n\n"
                  L"允许后对方可以移动鼠标并点击（键盘需另外勾选）。\n"
                  L"任何时候都可以点顶部红条的「停止控制」立即收回权限。",
                  SS_LEFT, 20, 16, w - 40, 78, 0);
        MakeChild(dlg_, L"BUTTON", L"允许键盘输入（默认关）", BS_AUTOCHECKBOX,
                  20, 104, 260, 24, kIdKeyboard);
        MakeChild(dlg_, L"STATIC",
                  (std::wstring(L"（") + std::to_wstring(timeoutSec_) +
                   L" 秒无响应将自动拒绝）").c_str(),
                  SS_LEFT, 20, 136, w - 40, 20, kIdCountdown);
        // 【默认焦点在「拒绝」】同时把「拒绝」设成默认按钮：回车 = 拒绝
        MakeChild(dlg_, L"BUTTON", L"拒绝", BS_DEFPUSHBUTTON, w - 220, 170, 90, 32, kIdDeny);
        MakeChild(dlg_, L"BUTTON", L"允许", BS_PUSHBUTTON, w - 120, 170, 90, 32, kIdAllow);

        ShowWindow(dlg_, SW_SHOWNORMAL);
        SetForegroundWindow(dlg_);
        SetFocus(GetDlgItem(dlg_, kIdDeny));
        lastTick_ = GetTickCount();
        Log(reask ? L"  [授权] 已**再次**弹出授权窗口（上次是拒绝/被停止）"
                  : L"  [授权] 已弹出授权窗口（默认焦点=拒绝，超时后自动拒绝）");
    }

    void CreateBanner()
    {
        RegisterClasses();
        const int sw = GetSystemMetrics(SM_CXSCREEN);
        banner_ = CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                                  L"ZxControlBanner", L"远程控制中",
                                  WS_POPUP, 0, 0, sw, 44,
                                  nullptr, nullptr, GetModuleHandleW(nullptr), this);
        if (!banner_) { Log(L"  [授权] 创建红条失败（不影响授权本身）"); return; }
        MakeChild(banner_, L"STATIC", L"● 远程控制进行中 —— 对方可以操作你的鼠标", SS_LEFT,
                  12, 12, sw - 320, 22, kIdBannerText);
        MakeChild(banner_, L"BUTTON",
                  hotkeyText_.empty() ? L"停止控制" : (L"停止控制（" + hotkeyText_ + L"）").c_str(),
                  BS_PUSHBUTTON, sw - 300, 7, 280, 30, kIdStop);
        ShowWindow(banner_, SW_SHOWNOACTIVATE);
        // 全局快捷键：默认 Ctrl+Alt+End
        if (RegisterHotKey(banner_, 1, MOD_CONTROL | MOD_ALT, VK_END))
            Log(L"  [授权] 全局快捷键已注册：Ctrl+Alt+End = 停止控制");
        else
            Log(L"  [授权] [!] 全局快捷键注册失败（可能被别的程序占用）");
    }

    void Finish(AuthState s)
    {
        state_ = s;
        if (dlg_) { DestroyWindow(dlg_); dlg_ = nullptr; }
        if (s == kAllowed) {
            CreateBanner();
            SendState((uint8_t)s);
        } else {
            if (banner_) { UnregisterHotKey(banner_, 1); DestroyWindow(banner_); banner_ = nullptr; }
            // 记住"谁被拒过、什么时候"：决定下次要不要再弹窗（见 ShouldReask）
            if (s == kDenied || s == kStopped) {
                lastDeniedTick_ = GetTickCount();
                lastDeniedPeer_ = peer_;
                hasDeniedPeer_ = true;
                wchar_t b[160];
                swprintf_s(b, L"  [授权] 已记录：%d 秒内同一请求方不会再次弹窗（换了请求方则立即可以再请求）",
                           cooldownSec_);
                Log(b);
            }
            SendState((uint8_t)s);
        }
    }

    // 把状态告诉查看端：它据此停止继续发输入（否则会一直白送）
    void SendState(uint8_t st) { if (StateSender) StateSender(st); }

private:
    Logger log_;
    AuthState state_ = kIdle;
    HWND dlg_ = nullptr, banner_ = nullptr;
    bool classesReady_ = false;
    bool keyboardAllowed_ = false;
    int timeoutSec_ = 30;
    int countdown_ = 30;
    int cooldownSec_ = 30;        // 同一请求方被拒后，多久才允许再次弹窗
    DWORD lastDeniedTick_ = 0;
    sockaddr_in lastDeniedPeer_{};
    bool hasDeniedPeer_ = false;
    DWORD lastTick_ = 0;
    sockaddr_in peer_{};
    std::wstring hotkeyText_ = L"Ctrl+Alt+End";
};

} // namespace zx
