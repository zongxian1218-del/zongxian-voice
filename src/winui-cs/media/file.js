// ===========================================================================
// file.js —— 文件传输（走 DataChannel）（S3：从 call.js 整块搬出）
//
// 【为什么用 DataChannel 而不是仓库里现成的 transfer.py】那份是 1318 行的多流 TCP 引擎，
// 能力更强（多流并行、断点续传、4MB 分块校验），但它要求用户机器上有 Python、要自己一套
// 握手/端口协商、还要另开一条链路。而 DataChannel 已经具备分片、保序、流控、端到端加密，
// 且**不需要任何额外进程或端口**。两人之间传文件，"简单可靠"比"多流并行"更重要。
// 代价：适合几十~几百 MB；>1GB 应回到 transfer.py（后续可选项）。
//
// 【两个关键实现细节】
//  · 分片 16 KB：太小则消息头占比高，太大则单帧占内存且 bufferedAmount 判断抖动。
//  · 必须看 bufferedAmount 做背压 —— 无脑 send 会把内存吃爆（DataChannel 发送缓冲无上限）。
//
// 【状态归属】发送/接收中的状态（sendingFile / receivingFile）原来是**页面全局变量**；
// 现在收进 createFileTransfer() 的闭包里 —— 一个事实一处，且不与其它模块共享。
// ===========================================================================

import { arrayBufferToBase64 } from './util.js';

export function createFileTransfer(io) {
  //
  // 【为什么不用仓库里现成的 transfer.py】
  // 那份实现是 1318 行的多流 TCP 引擎，能力更强（多流并行、断点续传、
  // 4MB SHA-256 分块校验）。但它需要：
  //   · 起一个 Python 进程（而 WinUI 应用不该依赖用户装 Python）
  //   · 自己的一套握手/端口协商
  //   · 与信令服务器另开一条链路
  // 而 DataChannel 已经具备传输所需的一切：分片、保序、流控、端到端加密，
  // 且**不需要任何额外进程或端口**。对"两个人之间传文件"这个场景，
  // 简单可靠比多流并行更重要。
  //
  // 代价：DataChannel 的量级适合几十~几百 MB。超大文件（>1GB）应该回到
  // transfer.py 那条路 —— 这是后续可选项，不是当前必需。
  //
  // 【关键实现细节：分片大小与背压】
  // · 分片 16 KB：太小则消息头开销占比高，太大则一帧占用过多内存且
  //   容易在 bufferedAmount 判断上产生抖动。
  // · 必须看 bufferedAmount 做背压。无脑 send 会把内存吃爆 ——
  //   DataChannel 的发送缓冲没有上限保护。
  // ===========================================================================

  const CHUNK_SIZE = 16 * 1024;              // 16 KB
  const HIGH_WATER = 4 * 1024 * 1024;        // 缓冲超过 4MB 就等

  // 发送/接收中状态（工厂内闭包，不再依赖页面的全局变量）
  let sendingFile = null;
  let receivingFile = null;

  /** 读取本地文件并发送。C# 把文件读成 base64 传进来（WebView2 不能直接读盘）。 */
  async function sendFile(payload) {
  if (!io.primaryChat() || io.primaryChat().readyState !== 'open') {
    io.post({ type: 'file-error', message: '聊天通道未就绪' });
    return;
  }
  if (sendingFile) {
    io.post({ type: 'file-error', message: '已有文件在发送中' });
    return;
  }

  try {
    // base64 → 二进制
    const bin = atob(payload.dataBase64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);

    const id = 'f' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
    const total = Math.ceil(bytes.length / CHUNK_SIZE);

    sendingFile = { id, name: payload.name, size: bytes.length, total, sent: 0 };
    io.log(`<span class="k">开始发送文件</span> ${io.esc(payload.name)} ` +
        `(${(bytes.length / 1024).toFixed(1)} KB，${total} 片)`);

    // 1) 元信息
    io.primaryChat().send(JSON.stringify({
      type: 'file-meta',
      id,
      name: payload.name,
      size: bytes.length,
      mime: payload.mime || 'application/octet-stream',
      chunks: total,
    }));

    // 2) 分片发送，带背压
    for (let i = 0; i < total; i++) {
      // 背压：等缓冲降下来再发下一片
      let waited = 0;
      while (io.primaryChat().bufferedAmount > HIGH_WATER) {
        await new Promise(r => setTimeout(r, 20));
        waited += 20;
        if (waited > 30000) throw new Error('发送缓冲长时间不降，可能对端卡住');
      }

      const slice = bytes.subarray(i * CHUNK_SIZE, (i + 1) * CHUNK_SIZE);
      // 分片头用 JSON 文本、负载用二进制：两者不能混在一条消息里，
      // 所以这里把序号和负载一起打包成二进制（前 4 字节存序号）。
      const frame = new Uint8Array(4 + slice.length);
      new DataView(frame.buffer).setUint32(0, i, true);
      frame.set(slice, 4);
      io.primaryChat().send(frame.buffer);

      sendingFile.sent = i + 1;
      if ((i + 1) % 20 === 0 || i + 1 === total) {
        io.post({
          type: 'file-progress', direction: 'send', id,
          name: payload.name,
          sent: i + 1, total,
          percent: Math.round(((i + 1) / total) * 100),
        });
      }
    }

    // 3) 结束标记
    io.primaryChat().send(JSON.stringify({ type: 'file-end', id }));
    io.log(`<span class="ok">文件发送完成</span> ${io.esc(payload.name)}`);
    io.post({ type: 'file-done', direction: 'send', id, name: payload.name });
    sendingFile = null;
  } catch (e) {
    sendingFile = null;
    io.log(`<span class="bad">文件发送失败: ${io.esc(e.message)}</span>`);
    io.post({ type: 'file-error', message: `发送失败: ${e.message}` });
  }
  }

  /** 处理收到的文件元信息 / 分片 / 结束标记。 */
  function handleFileMessage(msg) {
  if (msg.type === 'file-meta') {
    if (receivingFile) {
      // 上一份还没收完就又来一份：拒绝，避免内存错乱
      io.log('<span class="warn">上一份文件未接收完，忽略新的传输</span>');
      io.primaryChat().send(JSON.stringify({ type: 'file-error', id: msg.id,
        message: '对端上一份文件仍在接收' }));
      return;
    }
    receivingFile = {
      id: msg.id, name: msg.name, size: msg.size,
      chunks: msg.chunks, parts: new Array(msg.chunks), got: 0,
    };
    io.log(`<span class="k">开始接收文件</span> ${io.esc(msg.name)} ` +
        `(${(msg.size / 1024).toFixed(1)} KB)`);
    io.post({ type: 'file-start', direction: 'receive', id: msg.id,
           name: msg.name, size: msg.size });
    return;
  }

  if (msg.type === 'file-end') {
    if (!receivingFile || receivingFile.id !== msg.id) return;
    const f = receivingFile;
    receivingFile = null;

    const missing = f.parts.findIndex(p => !p);
    if (missing >= 0) {
      io.log(`<span class="bad">文件不完整：缺少第 ${missing} 片</span>`);
      io.post({ type: 'file-error', id: f.id, message: '接收到的文件不完整' });
      return;
    }

    // 拼回完整文件
    let len = 0;
    for (const p of f.parts) len += p.length;
    const out = new Uint8Array(len);
    let off = 0;
    for (const p of f.parts) { out.set(p, off); off += p.length; }

    io.log(`<span class="ok">文件接收完成</span> ${io.esc(f.name)} ` +
        `(${(len / 1024).toFixed(1)} KB)，交给 C# 落盘`);
    io.post({
      type: 'file-received',
      id: f.id, name: f.name, size: len,
      dataBase64: arrayBufferToBase64(out.buffer),
    });
    return;
  }

  if (msg.type === 'file-error') {
    io.log(`<span class="bad">对端报告传输错误: ${io.esc(msg.message)}</span>`);
    io.post({ type: 'file-error', message: msg.message });
  }
  }

  /** 处理二进制分片（前 4 字节是序号）。 */
  function handleFileChunk(buffer) {
  if (!receivingFile) return;
  const view = new DataView(buffer);
  const idx = view.getUint32(0, true);
  const payload = new Uint8Array(buffer, 4);
  if (idx >= receivingFile.chunks) return;

  receivingFile.parts[idx] = payload;
  receivingFile.got++;

  if (receivingFile.got % 20 === 0 || receivingFile.got === receivingFile.chunks) {
    io.post({
      type: 'file-progress', direction: 'receive',
      id: receivingFile.id, name: receivingFile.name,
      sent: receivingFile.got, total: receivingFile.chunks,
      percent: Math.round((receivingFile.got / receivingFile.chunks) * 100),
    });
  }
  }

  return { sendFile, handleFileMessage, handleFileChunk, isSending: () => !!sendingFile, isReceiving: () => !!receivingFile };
}
