// ===========================================================================
// selfcheck.js —— 自检类实现（S3 第 4 步：从 call.js 的 zxEngine 里搬出重活）
//
// 这里放"只在自检时跑"的代码，好处有两个：
//   1) zxEngine 变成薄适配层（页面被 C# 调用的接口一眼能看全，不再夹着 150 行算法）；
//   2) 自检逻辑与业务状态解耦 —— 它们只通过 io 拿 post/log/esc/state/links，不反向依赖 call.js。
//
// 目前：signalChainTest（Opus 编解码往返 + 电平校验）、shareAudioDebug（链路与收发字节快照）。
// 留待搬入：recordSample（依赖 openMic 与 localAudioChain，见 call.js 的 S3 计划）。
// ===========================================================================

export async function signalChainTest(arg, io) {
  const freq = (arg && arg.freq) || 1000;
  const seconds = (arg && arg.seconds) || 1;
  const result = { type: 'signal-test', freq, ok: false, steps: [] };
  const note = (name, ok, detail) => {
    result.steps.push({ name, ok, detail });
    io.log(`${ok ? '✓' : '✗'} ${name}${detail ? '  — ' + detail : ''}`);
  };

  // 采样定时器与它依赖的 AudioContext 要在 try 之外声明，
  // 这样 finally 一定能看到它们并回收（无论成功/失败/提前 return）。
  let decodeTimer = null;
  let decodeCtx = null;

  try {
    // 1) 用 OfflineAudioContext 生成标准正弦：幅度 0.5 = -6 dBFS，好认
    const sr = 48000;
    const n = Math.floor(sr * seconds);
    const offline = new OfflineAudioContext(1, n, sr);
    const osc = offline.createOscillator();
    osc.type = 'sine';
    osc.frequency.value = freq;
    const gain = offline.createGain();
    gain.gain.value = 0.5;
    osc.connect(gain).connect(offline.destination);
    osc.start();
    const rendered = await offline.startRendering();
    const src = rendered.getChannelData(0);

    let peak = 0, sum = 0;
    for (let i = 0; i < src.length; i++) {
      const a = Math.abs(src[i]);
      if (a > peak) peak = a;
      sum += src[i] * src[i];
    }
    const srcRms = Math.sqrt(sum / src.length);
    const srcPeakDb = 20 * Math.log10(Math.max(peak, 1e-9));
    note('生成测试信号', true,
         `${freq} Hz，峰值 ${srcPeakDb.toFixed(1)} dBFS（预期 -6.0）`);

    // 2) 经 Opus 编解码往返（用真实的 RTCPeerConnection，
    //    这样测的是与实际通话完全相同的编码路径）
    const pc1 = new RTCPeerConnection();
    const pc2 = new RTCPeerConnection();
    pc1.onicecandidate = e => e.candidate && pc2.addIceCandidate(e.candidate);
    pc2.onicecandidate = e => e.candidate && pc1.addIceCandidate(e.candidate);

    const dest = new MediaStreamAudioDestinationNode(
      new AudioContext({ sampleRate: sr }));
    // 直接把生成的 PCM 播进 dest
    const ctx = dest.context;
    const buf = ctx.createBuffer(1, n, sr);
    buf.copyToChannel(src, 0);
    const bs = ctx.createBufferSource();
    bs.buffer = buf;
    bs.connect(dest);
    bs.start();

    const track = dest.stream.getAudioTracks()[0];
    pc1.addTrack(track, dest.stream);

    const decoded = [];
    pc2.ontrack = (ev) => {
      const s = ev.streams[0];
      const c = new AudioContext({ sampleRate: sr });
      decodeCtx = c;
      const srcNode = c.createMediaStreamSource(s);
      const an = c.createAnalyser();
      an.fftSize = 2048;
      srcNode.connect(an);
      const b = new Float32Array(an.fftSize);
      decodeTimer = setInterval(() => {
        // 回调抛异常会被静默吞掉 → 采样直接停、结果却报"样本不足"。必须显式处理。
        try {
          an.getFloatTimeDomainData(b);
          for (let i = 0; i < b.length; i++) decoded.push(b[i]);
        } catch (e) {
          const msg = String((e && e.message) || e);
          io.log(`<span class="bad">[信号链采样异常] ${io.esc(msg)}</span>`);
          io.post({ type: 'scriptError', method: 'signalChainTest', stage: 'decode',
                 message: msg });
          if (decodeTimer) { clearInterval(decodeTimer); decodeTimer = null; }
        }
      }, 20);
      setTimeout(() => {
        if (decodeTimer) { clearInterval(decodeTimer); decodeTimer = null; }
        try { c.close(); } catch (e) { }
        if (decodeCtx === c) decodeCtx = null;
      }, seconds * 1000);
    };

    const offer = await pc1.createOffer();
    await pc1.setLocalDescription(offer);
    await pc2.setRemoteDescription(offer);
    const answer = await pc2.createAnswer();
    await pc2.setLocalDescription(answer);
    await pc1.setRemoteDescription(answer);

    // 等连接建立 + 采够样本
    await new Promise(r => setTimeout(r, seconds * 1000 + 1500));

    pc1.close(); pc2.close();
    try { await ctx.close(); } catch (e) { }

    if (decoded.length < 1024) {
      note('解码得到样本', false, `只有 ${decoded.length} 个样本`);
      io.post(result);
      return;
    }

    // 3) 分析解码结果：电平与频响
    let dPeak = 0, dSum = 0;
    for (let i = 0; i < decoded.length; i++) {
      const a = Math.abs(decoded[i]);
      if (a > dPeak) dPeak = a;
      dSum += decoded[i] * decoded[i];
    }
    const dRms = Math.sqrt(dSum / decoded.length);
    const gainDb = 20 * Math.log10(Math.max(dRms, 1e-9) / Math.max(srcRms, 1e-9));
    const dPeakDb = 20 * Math.log10(Math.max(dPeak, 1e-9));

    note('解码得到样本', true, `${decoded.length} 个`);
    note('电平增益偏差', Math.abs(gainDb) < 4,
         `${gainDb >= 0 ? '+' : ''}${gainDb.toFixed(2)} dB（应接近 0）`);
    note('解码后无削波', dPeak < 0.99,
         `峰值 ${dPeakDb.toFixed(1)} dBFS`);

    result.ok = Math.abs(gainDb) < 4 && dPeak < 0.99;
    result.gainDb = gainDb;
    result.srcPeakDb = srcPeakDb;
    result.decodedPeakDb = dPeakDb;
    io.log(`<span class="${result.ok ? 'ok' : 'warn'}">信号链验证${result.ok ? '通过' : '有偏差'}</span>`);
  } catch (e) {
    note('信号链验证', false, `${e.name}: ${e.message}`);
    result.error = String(e.message);
  } finally {
    // 【统一回收】提前 return / 抛异常都不许留下跑着的采样定时器与 AudioContext
    if (decodeTimer) { clearInterval(decodeTimer); decodeTimer = null; }
    if (decodeCtx) { try { await decodeCtx.close(); } catch (e) { } decodeCtx = null; }
  }
  io.post(result);
}


export async function shareAudioDebug(io) {
  const s = io.state.sharedAudio;
  const info = {
    active: !!s,
    links: io.links.size,
    attached: s ? s.sent.length : 0,
    pushed: s ? (s.pushed || 0) : 0,
    trackState: s && s.track ? s.track.readyState : 'none',
    trackKind: s && s.track ? s.track.kind : 'none',
    perLink: [],
    // 【A1 定位的关键三格】只报 pushed 不足以判断"音轨有没有数据"：
    //   ctxState=suspended ⇒ AudioContext 没跑；worklet=null ⇒ 回执通道不通/没在出样本；
    //   worklet.starved 持续增长 ⇒ 供数据不够快（或压根没进队列）。
    ctxState: s && s.ctx ? s.ctx.state : 'none',
    hasNode: !!(s && s.node),
    pushedSamples: s ? (s.pushedSamples || 0) : 0,
    worklet: s ? (s.worklet || null) : null,
    // 【A1 探针】两段 RMS：nodeRms=worklet 输出的声音，destRms=音轨里的声音。
    // 若 nodeRms>0 而 destRms=0 ⇒ 断在 worklet → MediaStreamDestination 之间。
    nodeRms: s ? Number((s.nodeRms || 0).toFixed(5)) : 0,
    destRms: s ? Number((s.destRms || 0).toFixed(5)) : 0,
    audioTrackState: s && s.trackState ? s.trackState() : 'none',
    registry: io.links.ids(),              // S3：链路注册表快照（双/三实例验证的口径）
    primary: io.primaryId(),
  };
  if (s) {
    for (const [id, l] of io.links) {
      const senders = l.pc.getSenders();
      const has = senders.some((x) => x.track === s.track);
      // 【唯一可靠判据】字节数：发送端 bytesSent > 0 才算"真的在发"；
      // 接收端 bytesReceived > 0 才算"真的收到了"。只看 track 数量会误判。
      let outBytes = 0;
      let inBytes = 0;
      let inEnergy = 0;      // 收到的音频**能量**：字节数会被静音轨骗过，能量不会
      let outEnergy = 0;
      let inLevel = 0;
      const outboundDump = [];   // 【A1】原始 outbound 条目：确认我们读的不是错的条目
      let senderInfo = [];
      try {
        const stats = await l.pc.getStats();
        stats.forEach((r) => {
          if (r.type === 'outbound-rtp') {
            outboundDump.push([r.kind || '', r.bytesSent || 0, r.packetsSent || 0]);
          }
          if (r.type === 'outbound-rtp' && r.kind === 'audio') {
            outBytes += (r.bytesSent || 0);
            outEnergy += (r.totalAudioEnergy || 0);
          }
          if (r.type === 'inbound-rtp' && r.kind === 'audio') {
            inBytes += (r.bytesReceived || 0);
            inEnergy += (r.totalAudioEnergy || 0);
            if (typeof r.audioLevel === 'number') inLevel = Math.max(inLevel, r.audioLevel);
          }
        });
        // 发送端的"这个 sender 到底在发哪条轨"：track.id/enabled/muted 是判断依据
        // [kind, enabled, muted, isShared]：判"这个 sender 到底是不是共享轨、有没有被静音"
        senderInfo = senders.map((x) => [x.track ? x.track.kind : 'none',
                                         x.track ? !!x.track.enabled : false,
                                         x.track ? !!x.track.muted : false,
                                         x.track === s.track]);
      } catch (e) { /* 忽略：拿不到 stats 不影响其它信息 */ }
      info.perLink.push({ id: String(id).slice(0, 6), senders: senders.length,
                          hasShared: has, conn: l.pc.connectionState,
                          audioOutBytes: outBytes, audioInBytes: inBytes,
                          audioInEnergy: Number(inEnergy.toFixed(6)),
                          audioOutEnergy: Number(outEnergy.toFixed(6)),
                          audioInLevel: Number(inLevel.toFixed(4)),
                          outboundDump, senderInfo });
    }
  }
  io.post({ type: 'share-audio-debug', info });
  io.log(`<span class="k">[共享声音自检] 活动=${info.active} 链路=${info.links} ` +
      `已挂=${info.attached} 已推=${info.pushed} 音轨=${info.trackState} ` +
      `${info.perLink.map((x) => `${x.id} send=${x.audioOutBytes} recv=${x.audioInBytes}${x.hasShared ? ' +共享' : ''}`).join(' | ')}</span>`);
  return info;
}

