// ===========================================================================
// webrtc.js —— 一条 PeerConnection 怎么建起来（S3：从 call.js 的 mesh 区搬出）
//
// 里面三件事都只与"建连接"有关，和业务状态无关：
//   1) createPeerConnection：建 PC、挂 ontrack（音频/屏幕/屏幕音频分流）、ICE 失败上报；
//   2) describeIceFailure：把 ICE 失败翻译成用户能看懂的原因（"把失败原因说清楚"是用户要求）；
//   3) （后续）ICE 服务器配置族 normalizeIceUrls / makeIceServerList 等。
//
// 依赖通过 io 注入：post/log/esc/state/reportCallState/sendSignal/startStatsLoop
// 与四个"把远端流接到界面元素上"的函数（它们属于视图层，不该被搬到网络层）。
// ===========================================================================

/**
 * 把 ICE 失败翻译成用户能看懂的原因（"把失败原因说清楚"是用户明确要求）。
 * 只需 io（里面带 state）；签名与 createPeerConnection 一致，调用方不用记两套。
 */
export function redactIceUrl(url) {
  return String(url == null ? '' : url).replace(/\/\/[^@/]*@/, '//');
}

export function summarizeIceServers(servers) {
  let stun = 0, turn = 0, turns = 0;
  for (const s of servers) {
    const arr = Array.isArray(s.urls) ? s.urls : [s.urls];
    for (const u of arr) {
      if (/^turns:/i.test(u)) turns++;
      else if (/^turn:/i.test(u)) turn++;
      else if (/^stun:/i.test(u)) stun++;
    }
  }
  return { stun, turn, turns, total: servers.length };
}

export function describeIceFailure(pc, io) {
  // 【2026-10-07 防御】stopAll 之后 ICE 状态回调仍会触发，此时 io/io.state 可能
  // 已被模块宿主释放（实测 `Uncaught TypeError: Cannot read properties of undefined (reading 'state')`）。
  // 这时不报 ICE 细节，直接给一句"连接已停止"，不再抛异常刷屏。
  if (!io || !io.state) {
    return { reason: '连接已停止', detail: '', candidateTypes: [], relay: false };
  }
  const state = io.state;
  const st = pc.iceConnectionState;
  const types = [...((pc && pc.__candidateTypes) || [])];
  const relay = types.includes('relay');
  let reason;
  if (!state.iceConfigured) {
    reason = `ICE ${st}：P2P 打不通，而且当前**没有配置中继（TURN）**` +
             `—— 双方在对称 NAT/防火墙后面时必须靠 TURN 中继，请让应用层下发 TURN 配置`;
  } else if (!relay) {
    reason = `ICE ${st}：已配置 TURN，但**没有拿到 relay（中继）候选**` +
             `—— 检查 TURN 地址/端口/账号是否有效、TURN 服务器是否可达`;
  } else {
    reason = `ICE ${st}：已拿到 relay 候选仍然失败` +
             `—— TURN 服务器可能拒绝分配中继，或上行被防火墙拦截`;
  }
  if (st === 'disconnected') {
    reason += `（disconnected 可能自行恢复；持续约 10 秒会转成 failed）`;
  }
  const detail = `候选类型=[${types.join(',') || '无'}] relay=${relay}` +
                 ` turnConfigured=${state.iceConfigured}`;
  return { reason, detail, candidateTypes: types, relay };
}

export function createPeerConnection(remoteId, io) {
  // 【依赖必须跟着一起搬】原来是 buildRtcConfig()（还留在 call.js）—— 结果是建链抛
  // `ReferenceError: buildRtcConfig is not defined`，被 try/catch 吞掉（只写隐藏 DOM），
  // 现象是"双方都看到 2 人、链路 0 条、应用日志里没有任何错误"（2026-10-06 实测）。
  // 现在这里只读注入的 state.iceServers，不再依赖 call.js 的任何函数。
  const pc = new RTCPeerConnection({ iceServers: io.state.iceServers });

  // 候选类型追踪：失败原因要能说清"拿到了哪种候选"，否则只剩"失败了"三个字。
  // host=本机直连 / srflx=STUN 反射 / prflx=对端反射 / relay=TURN 中继
  pc.__candidateTypes = new Set();

  pc.onicecandidate = ev => {
    if (ev.candidate) {
      try { pc.__candidateTypes.add(ev.candidate.type || 'unknown'); } catch (e) { }
      io.sendSignal({ type: 'ice', to: remoteId, candidate: ev.candidate });
    }
  };

  // 【TURN/STUN 服务器不可达】以前完全没人处理这个事件：配了 TURN 也永远不知道
  // 它到底通没通。这里上报（URL 抹掉账密），让"失败原因"能落到具体服务器。
  pc.onicecandidateerror = ev => {
    const url = redactIceUrl(ev && ev.url);
    const text = (ev && ev.errorText) || '';
    io.log(`<span class="warn">ICE 服务器报错: ${io.esc(url)} code=${(ev && ev.errorCode) || ''} ${io.esc(text)}</span>`);
    io.post({
      type: 'ice-candidate-error', url,
      code: (ev && ev.errorCode) || 0, text,
      address: (ev && ev.address) || '', port: (ev && ev.port) || 0,
      turnConfigured: io.state.iceConfigured,
    });
  };

  pc.ontrack = ev => {
    io.log(`<span class="ok">收到远端音轨</span> ${io.esc(ev.track.kind)}`);

    if (ev.track.kind === 'video') {
      // 对方在共享屏幕。视频画面交给 C# 用 WebView2 之外的控件显示吗？
      // 不行 —— 视频帧在 Chromium 内部，跨进程传帧代价极高。
      // 所以共享画面**仍然显示在 WebView2 里**，由 WebView2 自己渲染，
      // 只是这次它不再是 0 尺寸，而是在界面上占一块区域。
      //
      // 【多人同时共享】
      // 目前只有一个画面区，策略是"最后开始共享的人优先"：新流替换旧流。
      // 这样不理想，但**不静默** —— 会明确告知并上报给界面，
      // 用户至少知道画面为什么换人了。多方同屏需要多画面布局，是更大的改动。
      // 【A2 修复】这里以前直接引用 call.js 的 `remoteVideoEl`（跨模块裸引用）——
      // 模块化之后那个变量不在本模块作用域里 ⇒ 每次对方开始共享都抛
      // `ReferenceError: remoteVideoEl is not defined`（实测日志：webrtc.js:103），
      // ontrack 后续代码全部中断 ⇒ **画面永远接不上**。这正是用户说的
      // "屏幕共享有时候显示不出来"的直接原因之一。
      // 改判据：用注入的 state 判断"已经在看某个远端画面"，不摸别的模块的变量。
      if (io.state.remoteVideoStream
          && io.state.remoteVideoStream.getVideoTracks
          && io.state.remoteVideoStream.getVideoTracks().length > 0) {
        io.log('<span class="warn">又有一个人开始共享，画面切换为最新的一位</span>');
        io.post({ type: 'remote-screen-switched', peer: remoteId });
      }
      io.state.remoteVideoStream = ev.streams[0] || new MediaStream([ev.track]);
      io.attachRemoteVideo(io.state.remoteVideoStream);
      // 【P4】屏幕流可能带系统音频（Chromium 只在"共享整个屏幕/标签页"时提供，
      // 共享单个窗口时没有）。以前这里只挂了视频，把音频轨丢掉了 ——
      // 即便对端传了声音，本端也不会响。用独立 audio 元素播，避免顶掉通话语音。
      if (io.state.remoteVideoStream.getAudioTracks().length > 0) {
        io.attachScreenAudio(io.state.remoteVideoStream);
        io.post({ type: 'screen-audio-received' });   // 让应用日志能证明"对方的声音真的到了"
        io.log('<span class="ok">屏幕声音已接上</span>');
      }

      // 双重保险：对端停止共享时，可能触发 track.onended，也可能从
      // MediaStream 里移除轨道。两种都要处理 —— 只挂一个的话，
      // 另一种路径下画面会停格在最后一帧（本会话踩过）。
      ev.track.onended = () => {
        io.log('<span class="warn">对方的屏幕轨已结束</span>');
        io.detachRemoteVideo();
        io.post({ type: 'remote-screen-stopped', peer: remoteId });
      };
      const ms = io.state.remoteVideoStream;
      if (ms && ms.onremovetrack !== undefined) {
        ms.addEventListener('removetrack', () => {
          if (ms.getVideoTracks().length === 0) {
            io.log('<span class="warn">对方的视频轨已从流中移除</span>');
            io.detachRemoteVideo();
            io.post({ type: 'remote-screen-stopped', peer: remoteId });
          }
        });
      }
      io.post({ type: 'remote-screen-started', peer: remoteId });
      return;
    }

    io.state.remoteStream = ev.streams[0] || new MediaStream([ev.track]);
    // 【2026-10-07 已回退一次尝试】曾在这里加过"只有本端已加入语音才播放"，
    // 目的是解决"不加入也能听到对方说话"。但那**误伤了 A1 共享电脑声音** ——
    // 共享轨和对端麦克风走的是**同一条 ontrack 音频通路**，无法区分，
    // 结果共享声音收到字节却一个都没播（守卫实测 发送=130698/能量=0.00）。
    // 所以恢复"总是播放"。若要真正做"不加入就不听"，得先让 A1 的共享声音走独立音轨。
    io.attachRemoteAudio(io.state.remoteStream);
    io.post({ type: 'remote-track', kind: ev.track.kind });
  };

  pc.onconnectionstatechange = () => {
    const s = pc.connectionState;
    io.log(`连接状态: <span class="k">${s}</span>`);
    // 注意：这里上报的是"链路"状态，不是"通话"状态。
    // 界面要区分两者 —— 连接起来只为聊天时不该显示成通话中。
    io.post({ type: 'connection-state', state: s });
    if (s === 'connected') {
      io.startStatsLoop();
      io.reportCallState('链路已连接');
    }
    if (s === 'failed') reportIceFailure(pc, pc.iceConnectionState || 'failed', 'connection-state');
  };

  pc.oniceconnectionstatechange = () => {
    const s = pc.iceConnectionState;
    io.post({ type: 'ice-state', state: s });
    if (s === 'connected' || s === 'completed') {
      pc.__lastIceFailure = null;          // 恢复正常后再出问题要能重新上报
    } else if (s === 'failed' || s === 'disconnected') {
      // 【用户要求"把失败原因说清楚"】必须 post 一条带原因的事件，
      // 不许只 io.log() 到隐藏 DOM（应用日志里看不到，界面也看不到）。
      reportIceFailure(pc, s, 'ice-state');
    }
  };

  /**
   * 上报 ICE 失败（failed / disconnected），带可读原因。
   * 同一种状态只报一次，避免状态抖动时刷屏；恢复后会自动复位。
   */
  function reportIceFailure(pc2, iceState, source) {
    if (pc2.__lastIceFailure === iceState) return;
    pc2.__lastIceFailure = iceState;
    const why = describeIceFailure(pc2);
    io.log(`<span class="bad">ICE ${io.esc(iceState)}：${io.esc(why.reason)}</span>`);
    io.post({
      type: 'call-error',
      source: source || 'ice',
      iceState,
      reason: why.reason,          // C# 的 case "call-error" 直接显示这一段
      detail: why.detail,
      candidateTypes: why.candidateTypes,
      relayCandidate: why.relay,
      turnConfigured: io.state.iceConfigured,
    });
  }

  return pc;
}
