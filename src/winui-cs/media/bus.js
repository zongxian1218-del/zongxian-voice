// ===========================================================================
// bus.js —— 页面 → C# 的**唯一出口**（S3 第 1 步从 call.html 搬出）
//
// 【为什么单独一个文件】以前 post() 定义在 2800 行脚本中间，任何地方都能绕开它
// 直接摸 window.chrome.webview —— 于是"消息到底从哪发出去的"没法回答。
// 现在全页面只有这里碰桥：
//   · post(obj)              页面 → C#（事件）
//   · zxEngine 的命令仍由 C# 调进来，出口只有这一处
// 独立浏览器（没有桥）里静默丢弃，方便在浏览器里单独调试页面。
// ===========================================================================

export function post(obj) {
  try {
    if (window.chrome && window.chrome.webview) {
      window.chrome.webview.postMessage(JSON.stringify(obj));
    }
  } catch (e) { /* 独立浏览器里没有桥 */ }
}

