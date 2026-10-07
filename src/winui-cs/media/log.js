// ===========================================================================
// log.js —— 页面内置的可视日志 + 全局错误上报（S3 第 1 步从 call.html 搬出）
//
// 【为什么必须有错误上报】本会话踩过两次"JS 异常被静默吞掉"：
//   1) setInterval 回调里抛异常 → 统计莫名停止更新，日志毫无线索
//   2) SDP 改写函数抛异常 → 通话直接建不起来，界面只是"没反应"
// 浏览器默认只写 DevTools 控制台，而应用里没有 DevTools 可看。
// 所以未捕获异常与 Promise 拒绝都必须经 bus 报到 C#，让问题可见。
// ===========================================================================
import { post } from './bus.js';

// 【模块求值期的兜底上报】2026-10-06 实测：某个模块在**加载/求值阶段**抛异常时，
// 页面什么都不发，C# 只看到"未上报 engine-loaded"，没有任何原因可查
//（连 call.js 里那句心跳 post 都执行不到）。
// 这三条监听器放在最早的模块里，并且**自己的错误走最原始的一条通道**（直接调 post），
// 这样"哪个文件、哪一行、什么错"都能进应用日志。
window.addEventListener('error', (ev) => {
  try {
    post({
      type: 'js-error',
      message: String((ev && ev.message) || ev),
      file: String((ev && ev.filename) || ''),
      line: (ev && ev.lineno) || 0,
      col: (ev && ev.colno) || 0,
      stack: String((ev && ev.error && ev.error.stack) || '').split('\n').slice(0, 4).join(' | '),
    });
  } catch (e) { /* 上报自身失败也不能再抛 */ }
});
window.addEventListener('unhandledrejection', (ev) => {
  try {
    const r = ev && ev.reason;
    post({
      type: 'js-rejection',
      message: String((r && r.message) || r),
      stack: String((r && r.stack) || '').split('\n').slice(0, 4).join(' | '),
    });
  } catch (e) { }
});

// ===========================================================================
// 这是「不可见的通话引擎」。它跑在 WinUI 应用内嵌的 WebView2 里。
//
// 职责：
//   · 采集麦克风（带浏览器的回声消除 / 降噪 / 自动增益）
//   · 与对方建立 RTCPeerConnection，收发 Opus 音频
//   · 通过 WebSocket 与信令服务器交换 SDP / ICE
//   · 把连接质量（RTT / 丢包 / 抖动 / 码率）上报给 C#，用于界面显示
//
// 它**不做**的事：不画界面。所有可见控件都是原生 WinUI。
// 这里只通过 chrome.webview.postMessage 往 C# 报事件。
// ===========================================================================

// 【必须 export】2026-10-06 实测踩到：搬过来时只复制了 const 声明、忘了 export，
// 结果是 call.js 的 `import { log, esc, ok }` 直接 SyntaxError —— 整个页面**一行都不执行**，
// 而 C# 侧只看到"通话页未上报 engine-loaded"（错误监听器本身也在本文件里，还没来得及装）。
// 排查靠 tmp\esm-harness.mjs（Node 当假浏览器 import 一次即可拿到原始堆栈）。
export const out = document.getElementById('out');
export const log = s => { out.textContent += s + '\n'; out.scrollTop = out.scrollHeight; };
export const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
export const ok = b => b ? '<span class="ok">✓</span>' : '<span class="bad">✗</span>';


// ===========================================================================
// 全局错误捕获
//
// 【为什么必须有】
// 本会话踩过两次"JS 异常被静默吞掉"：
//   1) setInterval 回调里抛异常 → 统计莫名其妙停止更新，日志毫无线索
//   2) SDP 改写函数抛异常 → 通话直接建不起来，界面只是"没反应"
// 浏览器默认只在 DevTools 控制台显示，而我们没有 DevTools 可看。
// 所以这里把未捕获异常与 Promise 拒绝都上报到 C#，让问题可见。
// ===========================================================================
window.addEventListener('error', (e) => {
  try {
    post({
      type: 'js-error',
      message: e.message,
      source: e.filename ? String(e.filename).split('/').pop() : '',
      line: e.lineno,
      col: e.colno,
    });
  } catch (_) { }
});

window.addEventListener('unhandledrejection', (e) => {
  try {
    const r = e.reason;
    post({
      type: 'js-rejection',
      message: r && r.message ? r.message : String(r),
      stack: r && r.stack ? String(r.stack).split('\n').slice(0, 3).join(' | ') : '',
    });
  } catch (_) { }
});
