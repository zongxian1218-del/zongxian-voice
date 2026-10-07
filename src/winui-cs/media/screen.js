// ===========================================================================
// screen.js —— 屏幕共享（S3 第 3 步：从 call.js 整块搬出，并把"挂轨"收敛成一处）
//
// 【它负责什么】采集（真实 getDisplayMedia / 合成画面）、屏幕轨与屏幕音频轨的发布、
// 停止与清理、把"选择被取消/资源被占用"这类失败原因上报清楚。
// 【它不负责什么】不画界面（画面由原生 WinUI 叠层显示）、不碰聊天/文件/语音。
//
// 【搬迁时做对的一件事：把"给链路挂屏幕轨"收成一个函数】
// 原来 call.js 里有两份几乎一样的代码（发起方建链时、应答方应答后重协商时），
// 各自维护 screenSender/screenAudioSender 两个**单例**。多链路时：
//   · 只有主链路的 sender 被记住 ⇒ 停止共享时释放不干净；
//   · 新链路要各自再写一遍，容易漏（漏了就"第二个对端看不到画面"）。
// 现在只有 attachScreenToLink() 一份实现：轨写进 **link 对象**（link.screenSender /
// link.screenAudioSender），停止时按链路逐个 removeTrack。
//
// 依赖注入：state 与 post/log/esc/sendSignal/boostOpusInSdp 由调用方传入，模块不反向依赖 call.js。
// ===========================================================================

let screenStream = null;

/** 本端是否正在共享（有流就算）。 */
export function isScreenSharing() { return !!screenStream; }
export function currentScreenStream() { return screenStream; }

/** 合成视频源：给"屏幕共享"做可脚本化验证用（真实采集要用户在系统弹窗里选）。 */
function createSyntheticVideoStream(fps) {
  const canvas = document.createElement('canvas');
  canvas.width = 1280;
  canvas.height = 720;
  const ctx = canvas.getContext('2d');

  let frame = 0;
  const draw = () => {
    frame++;
    // 画会动的内容：静态画面会让编码器输出 0 帧，测不出真实码率
    ctx.fillStyle = '#101418';
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    const t = frame / 30;
    ctx.fillStyle = '#4CC26A';
    ctx.fillRect(60 + Math.sin(t) * 200, 120, 160, 160);

    ctx.fillStyle = '#7CC0FF';
    ctx.beginPath();
    ctx.arc(960, 360 + Math.cos(t * 1.3) * 120, 90, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = '#FFFFFF';
    ctx.font = '28px Consolas, monospace';
    ctx.fillText(`合成测试画面  帧 ${frame}`, 60, 640);
    ctx.fillText(new Date().toLocaleTimeString(), 60, 680);

    requestAnimationFrame(draw);
  };
  draw();

  const stream = canvas.captureStream(fps);
  stream.__synthetic = true;
  return stream;
}

/**
 * 开始共享。
 * @param {object} opts { audio, maxFps, timeoutMs, synthetic }
 * @param {object} io   { state, post, log, esc, sendSignal, boostOpusInSdp, primaryId, ensureLink }
 */
export async function startScreenShare(opts, io) {
  const { state, post, log, esc, sendSignal, boostOpusInSdp } = io;
  const withAudio = !opts || opts.audio !== false;
  const maxFps = (opts && opts.maxFps) || 15;
  const timeoutMs = (opts && opts.timeoutMs) || 20000;

  // 已经在共享就先停掉，避免两路流叠在一起（界面上会看起来"花屏"）
  if (screenStream) stopScreenShare('重新开始共享', io);

  // 合成画面模式：把"可自动验证的部分"和"选窗口"这个人工步骤分开
  const synthetic = !!(opts && opts.synthetic);
  if (synthetic) {
    log('<span class="warn">[测试后门] 合成画面模式（不弹系统选择窗口）</span>');
    post({ type: 'test-backdoor', name: 'synthetic-screen' });
    screenStream = createSyntheticVideoStream(maxFps);
  } else {
    // 真实采集：系统弹窗由用户选源。这里只能设约束 + 等结果 + 说清失败原因。
    const constraints = {
      video: { frameRate: { ideal: maxFps, max: maxFps } },
      audio: withAudio ? { echoCancellation: false, noiseSuppression: false, autoGainControl: false } : false,
    };
    let timer = null;
    try {
      screenStream = await Promise.race([
        navigator.mediaDevices.getDisplayMedia(constraints),
        new Promise((_, rej) => {
          timer = setTimeout(() => rej(new Error(`getDisplayMedia 超时（${timeoutMs} ms）`)), timeoutMs);
        }),
      ]);
    } catch (e) {
      const name = (e && e.name) || '';
      const msg = String((e && e.message) || e);
      // 把三类失败分开说清：用户取消 / 资源被占用 / 其它（用户要"说清失败原因"）
      const cancelled = name === 'NotAllowedError' || /cancel|denied|dismiss/i.test(msg);
      const notReadable = name === 'NotReadableError' || /could not start|in use|占用/i.test(msg);
      log(`<span class="bad">屏幕共享失败（${esc(name || 'Error')}）：${esc(msg)}</span>`);
      post({
        type: 'screen-error',
        cancelled,
        notReadable,
        name,
        message: msg,
        hint: cancelled ? '可能是选择窗口被挡/超时，或你点了取消'
                        : notReadable ? '采集源被占用，已清理，可重试'
                        : '看日志里的 name/message',
      });
      return { ok: false, error: msg, cancelled, notReadable };
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  const vt = screenStream.getVideoTracks()[0];
  const at = screenStream.getAudioTracks()[0];
  if (!vt) {
    post({ type: 'screen-error', message: '采集到的流里没有视频轨' });
    return { ok: false, error: 'no video track' };
  }

  // 【A2】轨"自己结束"必须被当成"共享已停止"处理。
  // 触发场景：用户在系统弹窗/浏览器条上点了"停止共享"，或采集源消失（窗口被关）。
  // 以前没有这个监听 ⇒ 界面停在"共享中"、对端永远收不到新帧，
  // 用户看到的现象正是"有时候显示不出来"（而且再点一次还可能因为旧流没清而失败）。
  vt.addEventListener('ended', () => {
    post({ type: 'screen-track-ended', kind: 'video' });
    stopScreenShare('采集轨结束（用户/系统停止）', io);
  });
  if (at) {
    at.addEventListener('ended', () => {
      post({ type: 'screen-track-ended', kind: 'audio' });
    });
  }

  // 对**每条已经存在的链路**都挂上（mesh 扇出）；没有链路就先留着，
  // 等新链路建立时由 attachScreenToLink() 补上（不会漏，只此一份实现）。
  for (const l of io.links ? io.links.values() : []) {
    await attachScreenToLink(l, io);
  }
  // 还没有链路：也要给主链路发一次重协商，否则先开共享再拉人时对端看不到画面
  if ((io.links ? io.links.size : 0) === 0) {
    log('<span class="warn">当前没有链路，屏幕轨先留着；新链路建立时会自动挂上</span>');
  } else if (io.primaryId && io.primaryId()) {
    try {
      const l = io.primaryLink ? io.primaryLink() : null;
      if (l && l.pc) {
        const so = await l.pc.createOffer();
        so.sdp = boostOpusInSdp(so.sdp);
        await l.pc.setLocalDescription(so);
        sendSignal({ type: 'offer', to: l.id, sdp: l.pc.localDescription, purpose: 'screen' });
        log('<span class="ok">已发出屏幕轨重协商 offer</span>');
      }
    } catch (e) {
      log(`<span class="warn">重协商 offer 失败: ${esc(e.message)}</span>`);
    }
  }

  const hasAudio = !!at;
  // 【2026-10-06 修复登记的"死分支"】C# 里一直有 `case "screen-source"`（诊断"采集到几条轨道"，
  // 是 P4"共享音频完全失效"的关键证据），但页面**从来没发过**这个 type —— 契约守卫报"死分支"。
  // 与其删掉那条诊断，不如把数据发出来：audio=0 说明平台没给音频（Chromium 只在共享
  // "整个屏幕/标签页"时给系统音频，共享单个窗口没有）。
  post({
    type: 'screen-source',
    audio: screenStream.getAudioTracks().length,
    video: screenStream.getVideoTracks().length,
    synthetic,
  });
  post({
    type: 'screen-started',
    width: vt.getSettings ? (vt.getSettings().width || 0) : 0,
    height: vt.getSettings ? (vt.getSettings().height || 0) : 0,
    frameRate: vt.getSettings ? (vt.getSettings().frameRate || maxFps) : maxFps,
    hasAudio,
    synthetic,
  });
  log(`<span class="ok">屏幕共享已开始</span>（${hasAudio ? '含系统音频' : '无音频'}）`);
  return { ok: true, hasAudio };
}

/**
 * 给一条链路挂上当前屏幕轨（**唯一实现**）。
 *
 * 【顺序很重要】应答方路径必须**先应答、再挂轨重协商**：WebRTC 的 answer 不能新增 m-line，
 * 放在应答前那条轨根本协商不上、对端永远看不到画面（实测踩到过）。
 * 这里只负责"挂到 link 上"，要不要重协商由调用方按自己的状态决定（见 call.js 的 answer 路径）。
 */
export async function attachScreenToLink(link, io) {
  if (!screenStream || !link || !link.pc) return false;
  const { log, esc } = io;
  try {
    const rvt = screenStream.getVideoTracks()[0];
    const rat = screenStream.getAudioTracks()[0];
    link.screenSender = rvt ? link.pc.addTrack(rvt, screenStream) : null;
    link.screenAudioSender = rat ? link.pc.addTrack(rat, screenStream) : null;
    log(`<span class="ok">屏幕轨已挂到链路 ${esc(link.id)}</span>`);
    return true;
  } catch (e) {
    log(`<span class="bad">挂屏幕轨失败（${esc(link.id)}）：${esc(e.message)}</span>`);
    io.post({ type: 'screen-error', message: `挂屏幕轨失败: ${e.message}`, link: link.id });
    return false;
  }
}

/** 把屏幕轨从某条链路摘掉（停止共享时按链路逐个调用，避免"以为停了其实还在发"）。 */
export function detachScreenFromLink(link, io) {
  if (!link) return;
  for (const key of ['screenSender', 'screenAudioSender']) {
    const s = link[key];
    if (!s) continue;
    try { link.pc.removeTrack(s); } catch (e) { }
    link[key] = null;
  }
}

/**
 * 停止共享：停采集 + 摘掉所有链路上的屏幕轨 + 上报。
 * @param {string} reason 停止原因（会写进日志与事件，便于诊断"为什么停了"）
 */
export function stopScreenShare(reason, io) {
  const { post, log, esc, sendSignal, state } = io;
  if (screenStream) {
    try { screenStream.getTracks().forEach((t) => t.stop()); } catch (e) { }
    screenStream = null;
  }
  for (const l of io.links ? io.links.values() : []) detachScreenFromLink(l, io);
  post({ type: 'screen-stopped', reason: reason || '' });
  log(`<span class="warn">屏幕共享已停止${reason ? '：' + esc(reason) : ''}</span>`);

  // 【搬漏过一次，别删】明确通知对端清理画面 —— 不依赖 WebRTC 的 track 事件，
  // 实测那条不可靠（对端会一直停在最后一帧）。2026-10-06 抽取 screen.js 时漏了这行，
  // 被 build\run-checks.py 的契约守卫当场抓到（"登记了 screen-stop-notify 但页面从不发"）。
  try {
    if (sendSignal) sendSignal({ type: 'screen-stop-notify', from: state && state.selfId });
  } catch (e) { /* 信令没连上时不致命 */ }
}

/**
 * 释放：停掉屏幕采集（**不**通知对端 —— 那是 stopScreenShare 的职责）。
 * 页面卸载/引擎停机时只需把本机资源收干净，避免"关了引擎采屏还在跑"。
 */
export function disposeScreen() {
  if (screenStream) {
    try { screenStream.getTracks().forEach((t) => t.stop()); } catch (e) { }
    screenStream = null;
  }
}
