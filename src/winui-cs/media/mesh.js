// ===========================================================================
// mesh.js —— 多链路（mesh）的注册与生命周期（S3 收口：从 call.js 整块搬出）
//
// 【它负责什么】
//   · 链路注册表（增/删/清/上报）—— 每条链路一个 Link 对象（见 link.js）；
//   · 建链（ensureLink / callPeer）、应答（onOffer / onAnswer / onRemoteIce）、挂断（hangup）；
//   · 聊天通道（setupChatChannel / bindChatChannel / sendChat）与共享声音轨挂载。
// 【它不负责什么】把远端流接到界面元素上（remoteAudioEl/remoteVideoEl/screenAudioEl）——
//   那是视图层，留在 call.js，通过 io 注入。
//
// 【S3 的一条硬结论】"同一事实存两份"是这一片的病根：chatChannel / state.pc / state.remoteId
// 都曾是"当前对端"的隐式单例。现在事实只在 Link 对象里，兼容视图由 call.js 的
// syncPrimaryAliases() 单向派生。
// ===========================================================================

// 【依赖必须整块带】createLink 来自 link.js。搬 mesh 时漏了这一行 import，
// 结果 registerLink 抛 `ReferenceError: createLink is not defined`，
// 表现为"双方都看到 2 人、链路 0 条"（被双实例闸门 + link-error 上报当场抓到）。
import { createLink } from './link.js';

export function createMesh(io) {

  const { log, post } = io;      // registerLink 里要拼 createLink 的 io

  function registerLink(id, pc) {
    if (!id || !pc) return;
    io.links.add(createLink(id, pc, { log, post }));
    io.reportLinks();
  }

  /**
   * 【A1 修复】建链**之前**先把常驻共享音轨准备好并挂上，再让调用方发 offer。
   *
   * 【为什么必须这样做（实测证据）】原来 registerLink 里写的是
   *   `io.ensureSharedAudioTrack().then((t) => { if (t) io.attachSharedAudioTrack(id, pc); })`
   * —— 这是**异步竞态**：调用方紧接着就 `createOffer()`，那条音轨还没建好，
   * 于是**第一份 offer 里根本没有它**；等 then 回调跑起来时 offer 已经发出去了，
   * 而 addTrack 本身**不会触发重协商**（旧注释还写过"这里重新协商实测反复失败，已移除"）。
   * 结果（用户报的 A1）：采集在跑、PCM 在推、worklet 里 RMS 有 0.33，
   * 但该链路 `getStats()` 里**一条 outbound-rtp 都没有** ⇒ RTP 恒 0 字节 ⇒ 对端完全听不到。
   *
   * 现在的顺序：等音轨就绪 → addTrack → 调用方才 createOffer ⇒ 首次协商就带轨，
   * 多一条静音 audio m-line（这是原本就接受的代价，见 ensureSharedAudioTrack 注释）。
   *
   * @returns {Promise<boolean>} 是否挂上了（挂不上不影响建链，只是没有共享声音）
   */
  async function prepareSharedAudioForNewLink(id, pc) {
    try {
      const track = await io.ensureSharedAudioTrack();
      if (!track) return false;
      return io.attachSharedAudioTrack(id, pc);
    } catch (e) {
      io.post({ type: 'share-audio-error', phase: 'prepare', link: String(id),
                message: String((e && e.message) || e) });
      return false;
    }
  }

  /**
   * 【A1 修复】把共享声音轨挂到**已存在**的链路上，并**重协商**让对端真的收到。
   *
   * 场景：用户先建链、后点「共享电脑声音」开关（这是最常见的顺序）。
   * 此时链路已经稳定，只 addTrack 不会生成 m-line，必须补一次 offer。
   * 已经有轨的链路直接返回 0，不做多余协商。
   *
   * @returns {Promise<number>} 真正完成重协商的链路数
   */
  async function attachAndRenegotiateSharedAudio(id, link) {
    const s = io.state.sharedAudio;
    if (!s || !link || !link.pc) return 0;
    if (s.sent.some((x) => x.pc === link.pc)) return 0;      // 建链时就带轨了
    if (!io.attachSharedAudioTrack(id, link.pc)) return 0;
    try {
      const offer = await link.pc.createOffer();
      offer.sdp = io.boostOpusInSdp(offer.sdp);
      await link.pc.setLocalDescription(offer);
      io.sendSignal({ type: 'offer', to: link.id, sdp: link.pc.localDescription,
                      purpose: 'share-audio' });
      io.log(`<span class="ok">共享声音轨已挂到链路 ${io.esc(String(id))} 并发出重协商 offer</span>`);
      io.post({ type: 'share-audio-renegotiated', link: String(id) });
      return 1;
    } catch (e) {
      const msg = `共享声音重协商失败（链路 ${id}）: ${(e && e.message) || e}`;
      io.log(`<span class="bad">${io.esc(msg)}</span>`);
      io.post({ type: 'share-audio-error', phase: 'renegotiate', link: String(id), message: msg });
      return 0;
    }
  }

  function setLinkChat(id, chat) {
    const l = id ? io.links.get(id) : null;
    if (l) l.chat = chat;
  }

  function unregisterLink(id) {
    if (id && io.links.remove(id, '页面注销')) io.reportLinks();
  }

  function unregisterAllLinks() {
    if (io.links.size) { io.links.clear(); io.reportLinks(); }
  }

  // ===========================================================================
  // 【S3 第 2 步：链路显式化】见 docs/multi-peer-mesh-design-2026-10-05.md + link.js
  //
  // 事实来源只有一个：`links`（LinkRegistry，每条 Link 拥有自己的 pc / chat / 屏幕 sender）。
  // `io.state.pc` / `primaryId()` / `chatChannel` 退化成**兼容视图**：
  //   · 只有 io.adoptPrimaryAliases() 能写它们（**唯一写入口**）；
  //   · 读它们的地方一律用下面的访问器（primaryPc / primaryId / primaryChat），
  //     这样将来要"传 link 参数"时只需改访问器，不用满页面找变量。
  // 主链路 = 最早建立的那条（规则固定，不再取决于最后一次赋值的时机）。
  // ===========================================================================

  /** 当前主链路（显式 API，S3 起新代码一律用它）。 */
  function primaryLink() {
    return io.links.primary();
  }

  /** 按对端取链路。 */
  function linkOf(id) {
    return io.links.get(id);
  }

  /**
   * 兼容视图的**唯一写入口**：主链路换了/没了就调它。
   * 只有 io.state.pc / io.state.remoteId 两个字段还需要"同步"；chatChannel **不是变量**，
   * 由 primaryChat() 直接从主链路对象上取 —— 从根上消灭"同一事实存两份"。
   * （注意：别把这里的赋值目标也"访问器化"——那是赋值，不是读。）
   */
  function syncPrimaryAliases() {
    const l = primaryLink();
    io.state.pc = l ? l.pc : null;
    io.state.remoteId = l ? l.id : null;
    return l;
  }
  const adoptPrimaryAliases = syncPrimaryAliases;   // 旧名字保留（调用点多，语义相同）

  // ---- 兼容访问器：读的地方统一走这三个（将来换成"传 link 参数"只改这里）----
  // 【必须用 function 声明而不是 const 箭头】2026-10-06 实测：文件顶部的 fileTx 构造时就会
  // 用到 primaryChat，而 const 箭头函数有**暂时性死区**（TDZ）⇒ 模块加载期直接
  // "Cannot access 'primaryChat' before initialization"，整页不启动
  //（被 build\page-load-check.mjs 当场抓到）。函数声明会提升，可以安全用在模块顶部的初始化里。
  function primaryPc() { return io.state.pc; }
  function primaryId() { return io.state.remoteId; }
  function primaryChat() {
    const l = primaryLink();
    return l ? (l.chat || null) : null;
  }


  /**
   * ⛔【已弃用 · 禁止调用】选一条"最可用"的链路当主链路。
   *
   * 【为什么禁止】它会在聊天通道 open 时**切换媒体主链路** —— 而主链路是
   * 语音 / 屏幕共享 / 文件传输走的路。实测后果（用户报）：
   * "原本好的分享屏幕也坏了、文件传输用不了了、语音不可用"。
   * 房间里有多条链路（含幽灵对端）时，它会把主链路切到错误的一条 ⇒ 三者同时失效。
   *
   * 聊天**不需要**它：`primaryChat()` 已经改成"在所有链路里找 open 的通道"。
   * 所以正确做法是：**聊天通道打开不得改变媒体主链路**。
   *
   * 保留函数体仅为可追溯历史；任何新代码都不要调用它。
   */
  function selectBestPrimary() {
    const ids = [...io.links.keys()];
    if (ids.length === 0) return;
    const withOpenChat = ids.find((id) => {
      const l = io.links.get(id);
      return l && l.chat && l.chat.readyState === 'open';
    });
    const cur = io.state.pc && primaryId ? primaryId() : null;
    const next = withOpenChat || (cur && ids.includes(cur) ? cur : ids[0]);
    if (next && next !== cur) {
      io.links.setPrimary(next);
      syncPrimaryAliases();
      io.log(`<span class="k">主链路切换为 ${io.esc(next)}</span>` +
             `（聊天通道 ${withOpenChat ? '已就绪' : '未知'}）`);
      io.post({ type: 'link-debug', step: 'primary-switched', peer: next,
                reason: withOpenChat ? 'chat-open' : 'fallback' });
    }
  }

  /**
   * 当前是否存在**已 open** 的聊天通道。
   * 【为什么不能用 primaryChat() 判断】它的兜底会返回未 open 的通道对象，
   * 于是 `!primaryChat()` 恒为 false —— 补建逻辑永远不会执行（本轮踩到）。
   */
  function hasOpenChat() {
    const ch = primaryChat();
    return !!(ch && ch.readyState === 'open');
  }

  /** 创建聊天通道。negotiated=false，由发起方 onDataChannel 触发对端。 */
  function setupChatChannel(pc, isInitiator, linkId) {
    if (isInitiator) {
      const dc = pc.createDataChannel('chat', {
        ordered: true,      // 聊天必须保序：乱序的消息读起来是灾难
      });
      bindChatChannel(dc, linkId);      // 【④ 修复】直接绑到所属链路
    }
    // 应答方不主动创建，等 ondatachannel 事件
    pc.ondatachannel = (ev) => {
      if (ev.channel.label === 'chat') bindChatChannel(ev.channel, linkId);
      // 后续若加"文件传输进度"通道，也在这里按 label 分派
    };
  }

  function bindChatChannel(dc, linkId) {
    // 【④ 修复】**创建时就把通道绑到链路**，不要等"open 之后再取一次"。
    // 原来的写法 `setLinkChat(id, primaryChat())` 在 dc 还是 connecting 时执行，
    // primaryChat() 只认 open ⇒ 存进去的是 null，通道就此变成"孤儿"（能 open 却没人找得到）。
    if (linkId) setLinkChat(linkId, dc);

    // 【④ 诊断】通道全生命周期：readyState / error / close 都要能看到。
    // 实测现象：链路 connected、音频在传，但聊天通道**永远不 open**
    //（join 侧日志：「通道尚未打开，消息已排队」→ 15 秒后超时）。
    // 没有这些事件就无从判断是"没走到 open"还是"走到又被关了"。
    io.post({ type: 'chat-debug', step: 'bind', readyState: dc.readyState,
              label: dc.label, negotiated: !!dc.negotiated });
    dc.onerror = (ev) => {
      io.post({ type: 'chat-debug', step: 'error', readyState: dc.readyState,
                message: String((ev && ev.error && ev.error.message) || '') });
    };

    dc.onopen = () => {
      io.log('<span class="ok">聊天通道已建立</span>（端到端加密，不经服务器）');
      io.post({ type: 'chat-open' });
      // 【④ 诊断】把"注册表里每条链路的 chat 状态"报上来：
      // 之前只能看到"通道开了但发不出去"，看不出 primaryChat() 指向了哪一条。
      try {
        const rows = [];
        for (const [id, l] of io.links) {
          rows.push(`${id}:${l && l.chat ? l.chat.readyState : 'null'}`);
        }
        io.post({ type: 'chat-debug', step: 'open-scan', readyState: dc.readyState,
                  label: dc.label, message: rows.join(' ') || '(注册表为空)' });
      } catch (e) { }
      flushPendingChat();          // 通道一开，把排队中的消息立刻发出去
      // 【2026-10-07 回归修复】这里以前会调 selectBestPrimary() **切换主链路**。
      // 那是错的：主链路是**媒体**（语音/屏幕共享/文件传输）走的路，
      // 而聊天只需要 primaryChat() 能在各链路里找到 open 通道（已经改成这样了）。
      // 实测后果：房间里有多条链路时主链路被切走 ⇒ 屏幕共享、文件传输、语音**同时失效**
      //（用户报"原本好的分享屏幕也坏了 / 文件传输用不了了 / 语音不可用"）。
      // 结论：**聊天通道打开不得改变媒体主链路**。
    };

    dc.onclose = () => {
      io.log('<span class="warn">聊天通道已关闭</span>');
      io.post({ type: 'chat-closed' });
    };

    dc.onmessage = (ev) => {
      // 二进制 = 文件分片；文本 = 控制消息。两者必须分开处理，
      // 因为分片是高频大流量，不该走 JSON 解析。
      if (typeof ev.data !== 'string') {
        io.handleFileChunk(ev.data);
        return;
      }
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'chat') {
          // peerName 来自发送方自报的名字（功能层补的数据）；UI 层据此显示真实说话人
          io.post({ type: 'chat-message', text: msg.text, at: msg.at, from: 'peer',
                 peerName: msg.fromName || '' });
        } else if (msg.type && msg.type.startsWith('file-')) {
          io.handleFileMessage(msg);
        }
      } catch (e) {
        io.log(`<span class="warn">消息解析失败: ${io.esc(e.message)}</span>`);
      }
    };

    dc.onerror = (e) => {
      io.log(`<span class="bad">聊天通道错误</span>`);
      io.post({ type: 'chat-error', message: String(e && e.message ? e.message : e) });
    };
  }

  /**
   * 确保与对端之间存在一条可用的链路（PeerConnection + 聊天通道）。
   *
   * 与"打电话"分开：聊天不需要麦克风，也不应该因为没在通话就用不了。
   * 所以这里按需建立 PeerConnection，但不碰本地麦克风轨。
   *
   * 【S2：按对端各建一条】原来是 `if (io.state.pc) return true` —— 房间里第二个人永远建不了链路。
   * 现在按对端查：已经有这个对端的链路就返回；没有就新建（这就是 mesh 的发起侧）。
   * 聊天与音频仍只挂在**主链路**上（设计稿 D2/S4）。
   */
  async function ensureLink(remoteId) {
    if (linkOf(remoteId)) return true;             // 已经有这个对端的链路

    const isPrimary = io.links.size === 0;
    io.log(`<span class="k">正在与 ${io.esc(remoteId)} 建立链路…${isPrimary ? '' : '（第 ' + (io.links.size + 1) + ' 条）'}</span>`);
    const pc = io.createPeerConnection(remoteId);
    registerLink(remoteId, pc);

    if (isPrimary) {
      io.links.setPrimary(remoteId);
      syncPrimaryAliases();
      setupChatChannel(pc, /* isInitiator */ true, remoteId);   // ④ 通道直接绑到本链路
    } else {
      io.log(`<span class="warn">与 ${io.esc(remoteId)} 建立第 ${io.links.size} 条链路（媒体链路；` +
          `聊天与音频仍只走第一位对端）</span>`);
      io.post({ type: 'link-added', peer: String(remoteId), count: io.links.size });
      // 【2026-10-07 根因修复】同应答方：只要当前**还没有任何可用聊天通道**，
      // 这条链路也要建 —— 否则 links.size>1 时永远没有 chat-open，
      // C# 的 `_chatReady` 保持 false ⇒ 输入框禁用（用户"打不了字"）。
      if (!hasOpenChat()) {
        setupChatChannel(pc, /* isInitiator */ true, remoteId);
        io.log('<span class="k">非主链路补建聊天通道</span>（此前没有任何可用通道）');
      }
    }

    // 本端正在共享 → 新链路也挂上屏幕轨（S3：唯一实现，见 screen.js 的 attachScreenToLink）
    if (io.isScreenSharing()) {
      const lk = linkOf(remoteId);
      if (lk) io.attachScreenToLink(lk);
    }

    // 【A1 修复】先把常驻共享音轨挂好（等它真的就绪），再发 offer —— 否则音轨进不了首次协商
  await prepareSharedAudioForNewLink(remoteId, pc);
  const offer = await pc.createOffer({ offerToReceiveAudio: true });
  // 【A1 诊断】上报 SDP 结构：共享音轨有没有协商成发送方向（m-line 数 / 方向 / ssrc 数）
  try {
    const lines = String(offer.sdp || '').split(/\r?\n/);
    io.post({
      type: 'sdp-summary', tag: 'offer-link',
      mlines: lines.filter((x) => x.startsWith('m=')),
      dirs: lines.filter((x) => /^a=(sendrecv|sendonly|recvonly|inactive)/.test(x)),
      ssrcCount: lines.filter((x) => x.startsWith('a=ssrc:')).length,
      senders: pc.getSenders().filter((s) => s.track && s.track.kind === 'audio').length,
    });
  } catch (e) { }
    offer.sdp = io.boostOpusInSdp(offer.sdp);
    await pc.setLocalDescription(offer);
    io.sendSignal({ type: 'offer', to: remoteId, sdp: pc.localDescription,
                 purpose: 'link' });
    return true;
  }

  /** 发送一条聊天消息。 */
  function sendChat(text) {
    const ch = primaryChat();
    if (!ch || ch.readyState !== 'open') {
      // 【实测根因】DataChannel 比"链路 connected"晚约 1 秒才 open；
      // 原来的实现此时**直接报错丢弃**，用户看到的就是"聊天通道未就绪" —— 以为功能坏了。
      // 现在改成**排队**：通道一开立刻发（最多等 15 秒），并把等待状态如实报给界面。
      queueChat(text);
      return true;
    }
    sendChatNow(text);
    return true;
  }

  /** 真正把消息发出去（通道已 open）。 */
  function sendChatNow(text) {
    const ch = primaryChat();
    // 带上发送者名字：对端才能显示"谁说的"。
    // 以前对端永远显示"对方"，三个人说话完全分不清（用户实测反馈 P1-b）。
    const payload = { type: 'chat', text, at: Date.now(), fromName: io.state.selfName || '' };
    ch.send(JSON.stringify(payload));
    io.post({ type: 'chat-sent', text });          // 让界面确认"真的发出去了"
  }

  /** 待发队列：通道还没 open 时的消息先存着。 */
  const pendingChat = [];
  let chatFlushTimer = null;

  /** 把排队中的消息全部发出（通道刚 open 时调用）。 */
  function flushPendingChat() {
    const ch = primaryChat();
    if (!ch || ch.readyState !== 'open') return;
    while (pendingChat.length > 0) sendChatNow(pendingChat.shift());
    if (chatFlushTimer) {
      clearInterval(chatFlushTimer);
      chatFlushTimer = null;
    }
  }

  function queueChat(text) {
    pendingChat.push(text);
    io.post({ type: 'chat-queued', text, pending: pendingChat.length });
    if (chatFlushTimer) return;
    const started = Date.now();
    chatFlushTimer = setInterval(() => {
      const ch = primaryChat();
      if (ch && ch.readyState === 'open') {
        flushPendingChat();
        return;
      }
      if (Date.now() - started > 15000) {
        // 真的等不到就说清原因（不是静默丢弃）
        clearInterval(chatFlushTimer);
        chatFlushTimer = null;
        const n = pendingChat.length;
        pendingChat.length = 0;
        io.post({ type: 'chat-error', message: `聊天通道 15 秒内没打开，${n} 条消息未发出（对方可能已离开）` });
      }
    }, 200);
  }

  /** 发起通话。复用已有的链路，不重复建 PeerConnection。 */
  async function callPeer(remoteId) {
    // 带上调用栈 —— 曾经出现过"没点任何按钮双方就自动进通话并开麦"，
    // 而 inCall 有三处入口，没有调用栈根本查不出是哪一条路径触发的。
    io.log(`<span class="k">callPeer 被调用</span> remote=${io.esc(remoteId)} ` +
        `栈=${io.esc((new Error().stack || '').split('\n').slice(1, 4).join(' ← '))}`);

    if (io.state.inCall) { io.log('已在通话中，忽略重复呼叫'); return; }

    // 先确保链路存在（可能因为聊天已经建好了）
    await ensureLink(remoteId);

    if (!io.state.localStream) io.state.localStream = await io.openMic();

    // 把麦克风轨加进去。若之前已加过（比如先聊天后通话），先清掉避免重复。
    const existing = primaryPc().getSenders().filter(s => s.track && s.track.kind === 'audio');
    for (const s of existing) { try { primaryPc().removeTrack(s); } catch (e) { } }

    let track = io.outgoingTrack();
    if (!track || track.readyState === 'ended') {
      // 【2026-10-07】挂断后旧降噪链里留着已 stop 的轨道 ⇒ 必须重建，
      // 否则 addTrack 一条死轨、媒体发不出去（界面显示"仅收听"）。
      io.log('<span class="warn">音频轨已失效，重建麦克风处理链</span>');
      try { io.resetAudioChain?.(); } catch (e) { }
      try { io.state.localStream?.getTracks().forEach(t => t.stop()); } catch (e) { }
      io.state.localStream = null;
      io.state.localStream = await io.openMic();
      track = io.outgoingTrack();
    }
    if (track) primaryPc().addTrack(track, io.state.localStream);

    io.state.peerCallRequested = true;
      io.setInCall(true, 'callPeer 发起呼叫');

    // 重新协商：加了音轨必须再走一次 offer/answer
    const offer = await primaryPc().createOffer({
      offerToReceiveAudio: true,
      offerToReceiveVideo: false,
    });
    offer.sdp = io.boostOpusInSdp(offer.sdp);
    await primaryPc().setLocalDescription(offer);
    io.sendSignal({ type: 'offer', to: remoteId, sdp: primaryPc().localDescription,
                 purpose: 'call' });
    io.log(`<span class="k">已发出 offer → ${io.esc(remoteId)}</span>`);
    await io.tuneOpusQuality(primaryPc());
    io.reportCallState('已发起呼叫');
    io.post({ type: 'call-state', state: 'calling' });
  }

  /** 收到 offer（对方发起链路或通话）。 */
  async function onOffer(msg) {
    const purpose = msg.purpose || 'call';
    const from = msg.from;
    io.log(`收到 offer（用途=${io.esc(purpose)}，来自=${io.esc(String(from))}，已有链路=${io.links.size} 条）`);

    // =========================================================================
    // 【S2：按 from 路由】见 docs/multi-peer-mesh-design-2026-10-05.md
    //   同一位对端 → 在**它自己那条链路**上重协商
    //   新对端     → **新建一条链路**（这是"房间里第二个人也能拿到媒体"的关键）
    // S2 之前是"已有 PC 就当重协商、不同对端就忽略"：第二个对端永远拿不到媒体；
    // 而且不同对端的 offer 会被塞进第一条链路的 PC（实测能把那条正常链路弄坏）。
    // =========================================================================
    const existing = linkOf(from);

    if (existing) {
      try {
        await existing.pc.setRemoteDescription(new RTCSessionDescription(msg.sdp));
        const answer = await existing.pc.createAnswer();
        answer.sdp = io.boostOpusInSdp(answer.sdp);
        await existing.pc.setLocalDescription(answer);
        io.sendSignal({ type: 'answer', to: from, sdp: existing.pc.localDescription });
        // 【共享电脑声音】answer 发完，PC 已是 stable —— 此刻显式发 offer 一定合法。
        // 不靠 negotiationneeded 事件（实测它没触发），这条路是确定性的。
        // 【共享电脑声音】不需要在这里做任何事：链路建立时就已经带上这条轨了
        // （见 ensureSharedAudioTrack / registerLink）。以前在这里重新协商，实测反复失败，已移除。
        io.log('<span class="k">已回复重协商 answer</span>');

        // 音频仍 1:1（设计稿 D2）：只有主链路的通话请求才接
        if (purpose === 'call' && !io.state.inCall && existing.id === primaryId()) {
          if (!io.state.localStream) io.state.localStream = await io.openMic();
          const track = io.outgoingTrack();
          if (track) existing.pc.addTrack(track, io.state.localStream);
          io.state.peerCallRequested = true;
      io.setInCall(true, '收到 purpose=call 的重协商');
          io.post({ type: 'incoming-call' });
        }
      } catch (e) {
        io.log(`<span class="bad">重协商失败: ${io.esc(e.message)}</span>`);
      }
      return;
    }

    // ---- 新对端：新建一条链路（S2 起，房间里第二个人也能拿到媒体）----
    const pc = io.createPeerConnection(from);
    registerLink(from, pc);
    // 【A1 修复】应答方也要在 createAnswer **之前**把常驻共享音轨挂上 ——
    // answer 不能新增 m-line，晚挂就永远协商不进去（这正是"对端完全听不到共享声音"的成因）。
    await prepareSharedAudioForNewLink(from, pc);
    const isPrimary = io.links.size === 1;      // 刚登记完只有 1 条 → 这就是主链路

    if (isPrimary) {
      io.links.setPrimary(from);
      syncPrimaryAliases();
      setupChatChannel(pc, /* isInitiator */ false, from);      // ④ 通道直接绑到本链路
    } else {
      io.log(`<span class="warn">与 ${io.esc(from)} 建立第 ${io.links.size} 条链路（媒体链路；` +
          `聊天与音频仍只走第一位对端，见设计稿 D2/S4）</span>`);
      io.post({ type: 'link-added', peer: String(from), count: io.links.size });
      // 【2026-10-07 根因修复·输入框禁用（"打不了字"）】
      // 以前这里**不建聊天通道** —— 于是当 links.size > 1（房间里有多条链路或幽灵对端）时，
      // 房主永远不会收到 chat-open ⇒ C# 的 `_chatReady` 一直 false
      // ⇒ `MessageInput.IsEnabled=false` ⇒ **用户根本没法打字**（间歇性，取决于当时几条链路）。
      // 修法：只要当前**还没有任何可用的聊天通道**，这条链路也要补建。
      if (!hasOpenChat()) {
        setupChatChannel(pc, /* isInitiator */ false, from);
        io.log('<span class="k">非主链路补建聊天通道</span>（此前没有任何可用通道）');
      }
    }

    // 本端正在共享 → 这条新链路也要拿到屏幕轨。
    // 【顺序很关键，实测踩过】见函数末尾：必须**先应答**，再用重协商 offer 推屏幕轨。
    // （WebRTC 的 answer 不能加 offer 里没有的 m-line；对方的 link offer 只含音频。）

    if (purpose === 'call' && isPrimary) {
      if (!io.state.localStream) io.state.localStream = await io.openMic();
      const track = io.outgoingTrack();
      if (track) pc.addTrack(track, io.state.localStream);
      io.state.peerCallRequested = true;
      io.setInCall(true, '收到 purpose=call 的新连接');
    }

    await pc.setRemoteDescription(new RTCSessionDescription(msg.sdp));
    const answer = await pc.createAnswer();
    answer.sdp = io.boostOpusInSdp(answer.sdp);
    await pc.setLocalDescription(answer);
    io.sendSignal({ type: 'answer', to: from, sdp: pc.localDescription });
    // 同上：answer 之后是 stable，把正在共享的电脑声音通过一次新 offer 送出去
    // 【共享电脑声音】同上：链路建立时已带轨，这里无需重新协商。
    io.log(`<span class="k">已回复 answer → ${io.esc(from)}</span>`);
    if (purpose === 'call' && isPrimary) {
      await io.tuneOpusQuality(pc);
      io.post({ type: 'call-state', state: 'connecting' });
    }
    // 无论用途是什么，协商完成后都重报一次真实状态。
    // 漏掉这一步的后果：link 用途（只为聊天）处理完后没人纠正状态，
    // 界面上会残留一个假的"通话中"。
    io.reportCallState(`已完成 ${purpose} 协商`);

    // 【S3：本端在共享时，新链路也要拿到屏幕轨】
    // 必须放在应答**之后**：WebRTC 的 answer 不能加 offer 里没有的 m-line
    //（对方的 link offer 只含音频），放在前面的话那条轨根本协商不上、对端永远看不到画面。
    // 正确做法：先应答，再用一次重协商 offer 把屏幕轨推过去。
    if (io.isScreenSharing()) {
      try {
        const _lk = linkOf(from);
        if (_lk) await io.attachScreenToLink(_lk);
        const so = await pc.createOffer();
        so.sdp = io.boostOpusInSdp(so.sdp);
        await pc.setLocalDescription(so);
        io.sendSignal({ type: 'offer', to: from, sdp: pc.localDescription, purpose: 'screen' });
        io.log('<span class="ok">新链路已挂上屏幕轨，并发出重协商 offer（本端正在共享）</span>');
      } catch (e) {
        io.log(`<span class="bad">新链路挂屏幕轨失败: ${io.esc(e.message)}</span>`);
      }
    }
  }

  async function onAnswer(msg) {
    // 【S2：按 from 路由】answer 属于哪条链路就交给哪条链路；
    // 没有对应链路才退回主链路（兼容老路径）。
    const target = linkOf(msg.from) || (primaryPc() ? { pc: primaryPc(), id: primaryId() } : null);
    if (!target) { io.log('<span class="warn">收到 answer 但没有对应链路，忽略</span>'); return; }

    // 【状态不对就不要调】实测过的未处理拒绝：
    //   setRemoteDescription: Failed to set remote answer sdp: Called in wrong state: stable
    // 即"根本没发过 offer（或已经完成协商）却收到 answer"。这种消息必须丢掉，
    // 而不是让它变成页面里的未处理拒绝（那会污染日志、还可能让后续逻辑半途而废）。
    const ss = target.pc.signalingState;
    if (ss !== 'have-local-offer') {
      io.log(`<span class="warn">收到 ${io.esc(String(msg.from))} 的 answer，但该链路信令状态=${io.esc(ss)}` +
          `（不是 have-local-offer），忽略</span>`);
      return;
    }

    try {
      await target.pc.setRemoteDescription(new RTCSessionDescription(msg.sdp));
      io.log(`<span class="k">已收到 answer（链路 ${io.esc(String(target.id))}）</span>`);
      io.post({ type: 'call-state', state: 'connecting' });
    } catch (e) {
      io.log(`<span class="bad">设置 answer 失败（已吞掉，不再变成未处理拒绝）: ${io.esc(e.message)}</span>`);
    }
  }

  async function onRemoteIce(msg) {
    // 【S2：按 from 路由】ICE 也要落到对应链路，否则第二条链路的候选全被丢掉。
    const target = linkOf(msg.from) || (primaryPc() ? { pc: primaryPc() } : null);
    if (!target || !msg.candidate) return;
    try {
      await target.pc.addIceCandidate(new RTCIceCandidate(msg.candidate));
    } catch (e) {
      io.log(`<span class="warn">添加 ICE 候选失败: ${io.esc(e.message)}</span>`);
    }
  }

  /** 挂断并释放所有资源。 */
  async function hangup(reason) {
    // =====================================================================
    // 【2026-10-07 修复·"挂了断但对端还显示通话中"】
    // 以前这里只做**本端**清理，从不通知对端 ⇒ 对端界面上一直停在"通话中"
    //（用户实测："点了挂断没声音了但是状态一个还是通话中"）。
    // 挂断必须告知对端 —— 这是双向的事。
    // 【注意顺序】通知要在清链路**之前**发（链路清掉后信令仍在，但 peer id 得先拿到）。
    // =====================================================================
    try {
      const ids = [];
      const seen = new Set();
      for (const [id, l] of io.links.entries()) {
        const s = String(id);
        if (!seen.has(s)) { seen.add(s); ids.push(s); }
      }
      if (ids.length === 0 && typeof io.primaryPeerId === 'function') {
        const pid = io.primaryPeerId();
        if (pid) ids.push(String(pid));
      }
      for (const id of ids) {
        io.sendSignal({ type: 'hangup', to: id, reason: reason || '' });
      }
      if (ids.length > 0) {
        io.log(`<span class="k">已通知 ${ids.length} 位对端挂断</span>`);
      }
    } catch (e) {
      io.log(`<span class="warn">通知对端挂断失败（继续本端清理）: ${io.esc(e.message)}</span>`);
    }

    io.stopStatsLoop();
    io.stopMicMeter();
    // 挂断后音频包计数归零，否则下次通话会被上一次的状态污染
    io.state.gotAudioPackets = false;
    io.state.lastAudioPackets = 0;

    // 【S2/S1：挂断要关掉**所有**链路】只关 io.state.pc 的话，多出来的链路会变成幽灵
    // （对端还在、PC 还在跑，但我们这边已经"挂断"了）。
    // 【S3 第 2 步】不再自己关 pc + 置空别名：Link.close() 会把轨/通道/连接都收干净，
    // 注册表清空后主链路自然为空，兼容视图再同步一次。
    io.links.clear('挂断');
    io.adoptPrimaryAliases();
    if (io.state.localStream) {
      io.state.localStream.getTracks().forEach(t => t.stop());
      io.state.localStream = null;
    }
    // 【不要直接碰 remoteAudioEl】它是 call.js 的模块级变量，在这里引用会抛
    // ReferenceError 并**中断整个 hangup**（实测：状态卡在"通话中"）。
    // 走注入的清理函数（同类 bug 第三次，见 docs）。
    try { io.detachRemoteAudio?.(); } catch (e) { }
    io.state.remoteStream = null;
    unregisterAllLinks();                          // S1：挂断时清空注册表
    io.setInCall(false, 'hangup');
    syncPrimaryAliases();

    io.log(`<span class="warn">通话结束${reason ? '：' + io.esc(reason) : ''}</span>`);
    io.reportCallState('已挂断');
    io.post({ type: 'call-state', state: 'idle', reason: reason || '' });
  }

  /**
   * 释放：关掉**所有**链路（PC + 数据通道 + 轨）。
   * 【为什么必须有】链路持有 RTCPeerConnection 与 DataChannel；页面卸载/引擎停机时
   * 不显式关，对端会一直以为我们还连着（幽灵链路），本机也会残留连接与采集。
   * 由 modules.js 宿主在卸载时按相反顺序统一调用。
   */
  function dispose() {
    try {
      io.links.clear('模块释放');
      io.syncPrimaryAliases();
    } catch (e) {
      io.post({ type: 'module-error', module: 'mesh', stage: 'dispose', message: String((e && e.message) || e) });
    }
  }

  return {
    registerLink, setLinkChat, unregisterLink, unregisterAllLinks,
    setupChatChannel, bindChatChannel, ensureLink, callPeer,
    onOffer, onAnswer, onRemoteIce, hangup,
    sendChat, dispose, prepareSharedAudioForNewLink, attachAndRenegotiateSharedAudio,
  };
}
