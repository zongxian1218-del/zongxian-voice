// ===========================================================================
// stats.js —— 连接质量统计循环（S3：从 call.js 搬出）
//
// 这里只做一件事：每秒问一次 WebRTC 要 getStats，解析成界面要的数字，post 给 C#。
//
// 【三条"不许静默"的规矩，都是踩出来的，别删注释】
//  1) setInterval 回调里抛异常会被浏览器**静默吞掉** ⇒ 统计莫名停更、日志无线索。
//  2) collectStats 是 async，同步 try/catch 接不住它的 rejection ⇒ 统一 .catch()。
//  3) getStats 失败以前直接 return ⇒ 统计静默停更。现在只报第一次，恢复后自动复位。
//
// 依赖注入：io.post/log/esc/primaryPc/reportCallState + io.state（统计相关字段）。
// ===========================================================================

export function createStats(io) {
  function startStatsLoop() {
    stopStatsLoop();
    io.state.statsTimer = setInterval(() => {
      // setInterval 的回调抛异常会被浏览器**静默吞掉** —— 现象是统计莫名其妙
      // 停止更新，日志里没有任何线索。本会话就踩过：日志冻结在"连接状态:
      // connected"之后，而通话栏还残留着上一次的值，看起来像"正常但不变"。
      // 所以这里必须显式捕获并上报。
      //
      // 另外：collectStats() 是 async，同步 try/catch **接不住**它的 rejection，
      // 所以统一用 .catch() 接；再加重入保护，避免上一拍没回来就叠加下一拍。
      if (io.state.statsBusy) return;
      io.state.statsBusy = true;
      Promise.resolve()
        .then(() => collectStats())
        .catch(e => {
          const msg = String((e && e.message) || e);
          io.post({ type: 'scriptError', method: 'collectStats', stage: 'timer', message: msg });
          io.log(`<span class="bad">[统计异常]</span> ${io.esc(msg)}`);
        })
        .finally(() => { io.state.statsBusy = false; });
    }, 1000);
    io.log('<span class="k">统计循环已启动</span>（每秒一次，可被 stopStatsLoop/hangup/卸载取消）');
    try { collectStats(); } catch (e) {
      io.post({ type: 'scriptError', method: 'collectStats', stage: 'first', message: String(e) });
    }
  }

  /** 停掉统计轮询。离开房间（hangup）、关闭引擎（stopAll）、页面卸载都会调这里。 */
  function stopStatsLoop() {
    if (io.state.statsTimer) { clearInterval(io.state.statsTimer); io.state.statsTimer = null; }
    io.state.statsBusy = false;
  }

  let prevInbound = null;

  async function collectStats() {
    if (!io.primaryPc()) return;

    let stats;
    try { stats = await io.primaryPc().getStats(); }
    catch (e) {
      // 【以前这里直接 return —— 统计就此静默停更，日志里一行线索都没有】
      // 与 setInterval 吞异常是同一类病：只报第一次，恢复后自动复位，避免收尾时刷屏。
      if (!io.state.statsGetFailed) {
        io.state.statsGetFailed = true;
        const msg = String((e && e.message) || e);
        io.log(`<span class="bad">[统计失败] getStats: ${io.esc(msg)}</span>`);
        io.post({ type: 'scriptError', method: 'collectStats', stage: 'getStats', message: msg });
      }
      return;
    }
    io.state.statsGetFailed = false;

    // 采样计数放在最前面：这个数字持续增长就说明统计循环本身是活的，
    // 与后面的解析是否会抛异常无关。用它把"循环死了"和"解析出错"区分开。
    io.state._tick = (io.state._tick || 0) + 1;

    const result = {
      rttMs: null, jitterMs: null, packetsLost: 0, packetsReceived: 0,
      lossPct: 0, inboundKbps: 0, outboundKbps: 0,
      codec: '', candidateType: '', bytesReceived: 0, bytesSent: 0,
    };

    const byId = new Map();
    stats.forEach(r => byId.set(r.id, r));

    // 累计量：用来证明"音频确实在流动"，而不是只看瞬时指标
    let totalPackets = 0, totalBytes = 0, totalSent = 0;
    stats.forEach(r => {
      if (r.type === 'inbound-rtp' && r.kind === 'audio') {
        totalPackets += r.packetsReceived || 0;
        totalBytes += r.bytesReceived || 0;
      }
      if (r.type === 'outbound-rtp' && r.kind === 'audio') {
        totalSent += r.bytesSent || 0;
      }
    });
    result.totalPackets = totalPackets;
    result.totalBytes = totalBytes;
    result.totalSentBytes = totalSent;


    stats.forEach(r => {
      if (r.type === 'inbound-rtp' && r.kind === 'audio') {
        result.packetsLost = r.packetsLost || 0;
        result.packetsReceived = r.packetsReceived || 0;
        const total = result.packetsLost + result.packetsReceived;
        result.lossPct = total > 0 ? (result.packetsLost / total) * 100 : 0;
        result.jitterMs = r.jitter != null ? r.jitter * 1000 : null;
        result.bytesReceived = r.bytesReceived || 0;
        if (r.codecId && byId.has(r.codecId)) {
          result.codec = byId.get(r.codecId).mimeType || '';
        }
        // 码率用两次采样的字节差算，比直接读瞬时值可靠
        if (prevInbound && prevInbound.ts) {
          const dt = (r.timestamp - prevInbound.ts) / 1000;
          if (dt > 0) {
            result.inboundKbps = ((r.bytesReceived - prevInbound.bytes) * 8) / dt / 1000;
          }
        }
        prevInbound = { ts: r.timestamp, bytes: r.bytesReceived || 0 };
      }
      if (r.type === 'outbound-rtp' && r.kind === 'audio') {
        result.bytesSent = r.bytesSent || 0;
      }
      if (r.type === 'remote-inbound-rtp') {
        if (r.roundTripTime != null) result.rttMs = r.roundTripTime * 1000;
      }
      if (r.type === 'candidate-pair' && r.state === 'succeeded' && r.nominated) {
        if (r.currentRoundTripTime != null) result.rttMs = r.currentRoundTripTime * 1000;
        if (r.localCandidateId && byId.has(r.localCandidateId)) {
          result.candidateType = byId.get(r.localCandidateId).candidateType || '';
        }
      }
      // 出站视频：屏幕上行的真实码率与帧率
      if (r.type === 'outbound-rtp' && r.kind === 'video') {
        result.videoOutBytes = r.bytesSent || 0;
        result.videoOutFrames = r.framesEncoded || 0;
        result.videoOutWidth = r.frameWidth || 0;
        result.videoOutHeight = r.frameHeight || 0;
        if (!result.codec && r.codecId && byId.has(r.codecId)) {
          result.videoCodec = byId.get(r.codecId).mimeType || '';
        }
      }
      // 入站视频：对端画面下行的真实码率与帧率
      if (r.type === 'inbound-rtp' && r.kind === 'video') {
        result.videoInBytes = r.bytesReceived || 0;
        result.videoInFrames = r.framesDecoded || 0;
        result.videoInWidth = r.frameWidth || 0;
        result.videoInHeight = r.frameHeight || 0;
        result.videoPacketsLost = r.packetsLost || 0;
      }
    });

    // remote-inbound-rtp 里的 rtt 更准，但只有对端在发时才有；兜底用 candidate-pair
    io.state.lastStats = result;
    result.tick = io.state._tick;          // 让界面能看出统计是否真的在跑

    // 音频包计数：从 0 变为 >0 时说明对端真的开始说话了，
    // 这时才把"通话中"报上去。仅凭 receiver 存在会误判（见 reportCallState）。
    if (result.packetsReceived > 0 && !io.state.gotAudioPackets) {
      io.state.gotAudioPackets = true;
      io.reportCallState('收到首个音频包');
    }
    io.state.lastAudioPackets = result.packetsReceived;

    // 【实时路径少打日志】原来每一拍（1 秒）都写一行，日志被流量数字淹没。
    // 现在每 5 拍一行；io.state._tick 仍然每拍递增并随 stats 事件上报，
    // 所以"统计到底有没有在跑"看 tick 就够了，不依赖这行日志。
    if (io.state._tick % 5 === 1) {
      io.log(`<span class="k">[流量 #${io.state._tick}]</span> ` +
          `收 ${result.totalPackets} 包 / ${(result.totalBytes / 1024).toFixed(1)} KB，` +
          `发 ${(result.totalSentBytes / 1024).toFixed(1)} KB，` +
          `RTT ${result.rttMs == null ? '—' : result.rttMs.toFixed(0) + ' ms'}，` +
          `编解码 ${result.codec || '—'}，链路 ${result.candidateType || '—'}`);
    }

    io.post({ type: 'stats', stats: result });
  }

  /**
   * 释放：停掉统计轮询。
   * 【为什么模块自己管】定时器原来只在 hangup/stopAll 里停；谁忘了调就一直转，
   * 每秒问一次 getStats。现在由宿主统一在卸载/停机时释放（见 modules.js）。
   */
  function dispose() {
    try { stopStatsLoop(); } catch (e) { }
  }

  return { startStatsLoop, stopStatsLoop, collectStats, dispose };
}
