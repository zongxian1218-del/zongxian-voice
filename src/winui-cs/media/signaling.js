// ===========================================================================
// signaling.js —— 信令（WebSocket）往返（S3：从 call.js 整块搬出）
//
// 【它负责什么】连信令服务器、发消息（连不上必须报错，不许静默丢）、
// 以及把收到的消息分派给具体处理者（建链 / offer / answer / ICE / 对端进出 / 停止共享通知）。
// 【它不负责什么】不建 PeerConnection、不管媒体、不画界面 —— 那些由注入的 handler 做。
//
// 【为什么用工厂】信令与 mesh 是双向依赖：信令要调 ensureLink/onOffer/hangup，
// 而这些又要通过 sendSignal 发消息。放进闭包后：内部互调保持原名，
// 依赖一次注入（见 call.js 的 signalIo()），不需要"先定义谁"的小心翼翼。
//
// 【两条踩过的坑，注释留在原地别删】
//  · sendSignal 以前在未连接时**直接丢消息**（应用日志连一行都没有）⇒ 表现为"点发起通话没反应"。
//    现在必须上报 call-error。
//  · JSON.parse 失败以前会变成"未捕获异常"，那条信令直接消失 ⇒ 现在报 signal-parse。
// ===========================================================================

export function createSignaling(io) {
  const { state, post, log, esc } = io;

  /**
   * 信令被动断开后的自动重连（指数退避）。
   * 【为什么必须有】服务端可能因网络抖动/休眠/防火墙短暂断开；
   * 以前断开就永远停在"信令断开"，用户只能重启应用（实测现场就是这样）。
   */
  let reconnectTimer = null;
  function scheduleReconnect() {
    const n = (state.reconnectAttempt = (state.reconnectAttempt || 0) + 1);
    if (n > 10) {
      log('<span class="bad">信令重连已放弃</span>（连续 10 次失败）—— ' +
          '请检查对方/本机的信令服务，或点界面上的「重新连接」');
      post({ type: 'signal-reconnect-gaveup', attempts: n });
      return;
    }
    const delay = Math.min(15000, 1000 * Math.pow(2, Math.min(n - 1, 4)));
    log(`<span class="k">信令将在 ${Math.round(delay / 1000)} 秒后自动重连</span>（第 ${n} 次）`);
    post({ type: 'signal-reconnecting', attempt: n, delayMs: delay });
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(async () => {
      reconnectTimer = null;
      if (state.ws === null) return;
      try {
        await connectSignal();
        log('<span class="ok">信令已自动重连成功</span>');
      } catch (e) {
        // connectSignal 失败会再次触发 onclose/onerror → 由那里继续排下一次重试
      }
    }, delay);
  }

  function connectSignal() {
    // 【2026-10-07 根因修复·"信令断开"误报】
    // 这里以前直接 `state.ws = new WebSocket(...)` 覆盖旧连接，**不管旧连接**。
    // 而建房/改名/切房间会再调一次（start/join）⇒ 旧 ws 的 onclose 在**新连接建立之后**
    // 才触发，于是 post({type:'signal-closed'}) 把状态判成"信令断开"
    //（用户实测：房主窗口左下角红点"信令断开"，但房间其实连着）。
    //
    // 修法：**先把旧连接的所有回调摘掉再关**，它就再也不会干扰新连接的状态。
    const old = state.ws;
    if (old) {
      try {
        old.__silenced = true;        // 主动切换：这条连接不该触发自动重连
        old.onopen = null;
        old.onerror = null;
        old.onclose = null;      // ← 关键：否则它的 onclose 会误报"断开"
        old.onmessage = null;
        if (old.readyState <= 1) old.close();
      } catch (e) { }
      log('<span class="k">重连信令</span>（已静默旧连接，避免误报断开）');
    }
    return new Promise((resolve, reject) => {
      const url = `${state.signalUrl}?room=${encodeURIComponent(state.room)}` +
                  `&name=${encodeURIComponent(state.selfName)}`;
      log(`<span class="k">连接信令</span> ${esc(url)}`);

      const ws = new WebSocket(url);
      state.ws = ws;
      let settled = false;

      ws.onopen = () => {
        log('<span class="ok">信令已连接</span>');
        state.reconnectAttempt = 0;        // 连上就重置退避计数
        post({ type: 'signal-open' });
        if (!settled) { settled = true; resolve(); }
      };
      ws.onerror = (e) => {
        log(`<span class="bad">信令错误</span>`);
        if (!settled) { settled = true; reject(new Error('websocket error')); }
      };
      ws.onclose = () => {
        log('<span class="warn">信令已断开</span>');
        post({ type: 'signal-closed' });
        if (!settled) { settled = true; reject(new Error('websocket closed')); }
        // 【2026-10-07 产品级修复·自动重连】
        // 以前断开就永远停在"信令断开"：用户既不知原因，也没有恢复手段（只能重启应用）。
        // 实测现场：服务端完全正常（真 WebSocket 能连上并收到 welcome），
        // 但界面一直红点"信令断开" —— 就是缺这个自动重连。
        // 只有"当前连接"且**不是被主动静默的**才自动重连。
        // 注意不要用全局标记（曾用 state.signalClosing，新连接失败时它清不掉 ⇒ 只重试一次）。
        if (state.ws === ws && !ws.__silenced) {
          scheduleReconnect();
        }
      };
      ws.onmessage = ev => {
        // JSON.parse 抛异常会被当成"未捕获异常"，而且这条信令直接消失。
        // 协议不对必须报出来，不能静默。
        try {
          handleSignal(JSON.parse(ev.data));
        } catch (e) {
          const msg = `信令消息解析失败: ${(e && e.message) || e}`;
          log(`<span class="bad">${esc(msg)}</span>`);
          post({ type: 'call-error', source: 'signal-parse', reason: msg });
        }
      };
    });
  }

  /**
   * 发一条信令。
   * 【以前完全静默】信令没连上时直接丢掉：表现为"点了发起通话没反应"，
   * 应用日志里连一行都没有。现在必须上报（C# 的 case "call-error" 会显示原因）。
   */
  function sendSignal(obj) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(obj));
      return true;
    }
    const rs = state.ws ? state.ws.readyState : -1;
    const reason = `信令未连接（readyState=${rs}），${(obj && obj.type) || '?'} 消息未能发出` +
                   `（对端收不到这一步的 SDP/ICE）`;
    log(`<span class="bad">${esc(reason)}</span>`);
    post({ type: 'call-error', source: 'signal-not-open', signalType: (obj && obj.type) || '',
           readyState: rs, reason });
    return false;
  }

  async function handleSignal(msg) {
    // 【信令字段诊断】"不同对端的 offer/answer 该不该忽略"这个判据一直靠猜：
    // msg.from 到底有没有、是不是发送者 id，从来没看清过。
    // 信令消息数量很少（一次通话几条），所以直接全打，不怕刷屏。
    if (msg && (msg.type === 'offer' || msg.type === 'answer' || msg.type === 'ice')) {
      // 注意：必须用 post() 才能进应用日志 —— 页面里的 log() 只写页面自己的日志区。
      const dbg = `[信令] ${msg.type} from=${String(msg.from)} to=${String(msg.to)} ` +
                  `键=${Object.keys(msg).join(',')}` +
                  `${msg.purpose ? ' purpose=' + msg.purpose : ''}` +
                  ` 我方remoteId=${String(io.primaryId())}`;
      log(`<span class="k">${esc(dbg)}</span>`);
      post({ type: 'signal-debug', text: dbg });
    }
    switch (msg.type) {
      case 'welcome': {
        state.peers.clear();
        // 必须把自己的 id 存下来：停止共享时要靠它标识发送方。
        state.selfId = msg.selfId;
        for (const p of msg.peers || []) state.peers.set(p.id, p);
        log(`<span class="k">我 = ${esc(msg.selfId)}</span>，房间内已有 ${state.peers.size} 人`);
        post({ type: 'roster', selfId: msg.selfId, peers: [...state.peers.values()] });
        // 【A3 诊断】每次发 roster 都带上"人数/名单/触发原因"：
        // C# 的成员数只来自 roster.peers 的长度，时序错了界面就会少人。
        post({ type: 'roster-update', why: 'welcome', count: state.peers.size,
               ids: [...state.peers.keys()], selfId: String(msg.selfId) });


        // 房间里已经有人 → 对**每一位**已在房间的对端建立链路（用于聊天/媒体）。
        // 【为什么不是只连"第一位"】mesh 要求每个成员之间有链路：只连第一位的话，
        // 第三人加入后可能连到第二位而不是共享方，于是永远看不到共享 —— 实测踩过。
        // 不自动开麦打电话：自动打电话会突然占用对方麦克风，很打扰；链路本身是无感的。
        // 【诊断要走 post】log() 只写隐藏 DOM，应用日志看不到。
        post({ type: 'link-debug', step: 'welcome', selfId: msg.selfId || '',
               peerCount: state.peers.size, peers: [...state.peers.values()].map((x) => x.id) });
        if (state.peers.size > 0) {
          for (const p of [...state.peers.values()]) {
            log(`房间内已有对端，建立链路：${esc(p.name || p.id)}`);
            try { await io.ensureLink(p.id); } catch (e) {
              // 【必须 post，不能只 log】页面 log() 只写隐藏 DOM，应用日志看不到 ——
              // 2026-10-06 双实例自检就是因为这条只 log，才出现"链路 0 条、却查不到任何原因"。
              const why = `${(e && e.name) || 'Error'}: ${(e && e.message) || e}`;
              log(`<span class="bad">建立链路失败: ${esc(why)}</span>`);
              post({ type: 'link-error', peer: String(p.id), message: why,
                     stack: String((e && e.stack) || '').split('\n').slice(0, 3).join(' | ') });
            }
          }
        }
        break;
      }

      case 'peer-joined': {
        state.peers.set(msg.id, { id: msg.id, name: msg.name });
        log(`<span class="k">${esc(msg.name || msg.id)} 加入房间</span>`);
        post({ type: 'roster', selfId: null, peers: [...state.peers.values()] });
        post({ type: 'roster-update', why: 'peer-joined', count: state.peers.size,
               ids: [...state.peers.keys()], joined: String(msg.id) });
        // 新加入者会收到 welcome 并主动建链，这边等 offer 即可
        break;
      }

      case 'peer-left': {
        state.peers.delete(msg.id);
        log(`<span class="warn">${esc(msg.id)} 离开房间</span>`);
        post({ type: 'roster', selfId: null, peers: [...state.peers.values()] });

        // 【mesh：只拆**这位对端**的链路，不要整体挂断】
        // 原来这里会把**所有**链路一起关掉：3 人房间里走掉一个人，剩下的人也跟着断了、
        // 共享画面也没了。正确的是只拆他这一条，然后把主链路别名切到还活着的那位。
        const gone = io.linkOf(msg.id);
        if (gone) {
          try { gone.pc.close(); } catch (e) { }
          io.unregisterLink(msg.id);
        }
        // 他走了 → 界面那一格画面要收起（应用侧按共享者集合处理）
        post({ type: 'remote-screen-stopped', peer: String(msg.id) });

        if (io.primaryId() === msg.id) {
          io.adoptPrimaryAliases();               // 把兼容视图切到还活着的链路
          if (io.primaryPc()) {
            log(`<span class="ok">主链路已切换到 ${esc(String(io.primaryId()))}（还剩 ${io.links.size} 条）</span>`);
            post({ type: 'link-switched', current: String(io.primaryId()), count: io.links.size });
          } else {
            // 一条都不剩了，才收尾
            await io.hangup('对端离开');
          }
        }
        break;
      }

      case 'offer':
        await io.onOffer(msg);
        break;

      case 'answer':
        await io.onAnswer(msg);
        break;

      case 'ice':
        await io.onRemoteIce(msg);
        break;

      case 'hangup': {
        // 【2026-10-07】对端挂了断 —— 本端必须跟着退出通话状态，
        // 否则界面会一直停在"通话中"（用户实测的两端状态不一致）。
        log(`<span class="warn">收到 ${esc(String(msg.from))} 的挂断通知</span>`);
        // 不需要再 post 一个 peer-hangup：io.hangup() 内部会 post
        // `call-state: idle, reason='对方已挂断'`，C# 已有 case 处理（少一个事件面）。
        await io.hangup('对方已挂断');
        break;
      }

      case 'screen-stop-notify': {
        // 对端明确告知停止共享。这是**唯一可靠**的清理信号 ——
        // WebRTC 的 track.onended / removetrack 在对端停止时都实测不可靠。
        log(`<span class="warn">收到 ${esc(String(msg.from))} 停止共享的通知</span>`);
        io.detachRemoteVideo();
        // 【S6：带上是谁停的】应用侧现在维护"共享者集合"，必须知道是哪一位
        post({ type: 'remote-screen-stopped', peer: String(msg.from) });
        break;
      }

      case 'stats':
        // 对端回报的质量（可选，用于双向显示）
        post({ type: 'remote-stats', from: msg.from, stats: msg.stats });
        break;

      default:
        log(`<span class="warn">未知信令</span> ${esc(JSON.stringify(msg))}`);
    }
  }

  /**
   * 释放：关掉信令 WebSocket。
   * 【为什么必须有】ws 不显式关，页面卸载后服务器的对端表里还会留着我们（幽灵成员），
   * 别人看到的成员数就多一个。由 modules.js 宿主在卸载时统一调用。
   */
  function dispose() {
    try { if (state.ws) state.ws.close(); } catch (e) { }
    state.ws = null;
  }

  return { connectSignal, sendSignal, handleSignal, dispose };
}
