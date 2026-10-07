// ===========================================================================
// call.js —— 通话引擎主体（S3 第 1 步：从 call.html 的内联脚本搬出来，行为不变）
//
// 读取顺序：shell(call.html) → bus.js → log.js → 本文件。
// 本文件仍包含 store(state) 与全部业务；后续步骤会把 link 显式化、再按模块切开。
// ===========================================================================
import { post } from './bus.js';
import { log, esc, ok } from './log.js';
import { createLink, LinkRegistry } from './link.js';
import { createFileTransfer } from './file.js';
import { createSignaling } from './signaling.js';
import { createStats } from './stats.js';
import { createMesh } from './mesh.js';
import { createModuleHost } from './modules.js';
import {
  createPeerConnection as webrtcCreatePeerConnection,
  describeIceFailure as webrtcDescribeIceFailure,
  redactIceUrl, summarizeIceServers,
} from './webrtc.js';
import { encodeWav, arrayBufferToBase64 } from './util.js';
import { signalChainTest as selfcheckSignalChainTest, shareAudioDebug as selfcheckShareAudioDebug } from './selfcheck.js';
import { disposeVoice, startMicMeter as voiceStartMicMeter, stopMicMeter as voiceStopMicMeter,
         recordSample as voiceRecordSample } from './voice.js';
import {
  disposeScreen,
  startScreenShare as screenStartScreenShare, stopScreenShare as screenStopScreenShare,
  attachScreenToLink as screenAttachScreenToLink, isScreenSharing as screenIsScreenSharing,
  currentScreenStream as screenCurrentScreenStream,
} from './screen.js';

// ===========================================================================
// 通话状态
// ===========================================================================
const state = {
  selfName: '',
  room: 'default',
  signalUrl: '',
  ws: null,
  pc: null,
  localStream: null,
  remoteStream: null,
  peers: new Map(),        // id -> { name }
  remoteId: null,          // 当前通话对端
  inCall: false,
  statsTimer: null,
  muted: false,
  lastStats: null,
  // 麦克风约束。降噪档位通过调整这里的参数实现。
  noiseSuppression: true,
  echoCancellation: true,
  autoGainControl: true,
  denoiseLevel: 'normal',   // off | normal | strong
  // 设备选择（设置页）：空串 = 用系统默认设备。由 zxEngine.setDevice 写入。
  micDeviceId: '',
  spkDeviceId: '',
  // 对方是否明确发起过通话（收到 purpose=call）。用于"通话中"的兜底判定，
  // 避免把"对方只是共享电脑声音"误判成通话。
  peerCallRequested: false,
  // 是否真的收到过音频包。用来区分"有 receiver"与"有声音"。
  gotAudioPackets: false,
  lastAudioPackets: 0,
};

// ===========================================================================
// 屏幕共享 —— 已搬到 screen.js（S3 第 3 步）
//
// 搬迁时顺手做对的一件事：原来"给链路挂屏幕轨"在这份文件里有**两份**几乎一样的代码
// （发起方建链时、应答方应答后重协商时），各自维护 screenSender/screenAudioSender 单例。
// 多链路时表现为"第二个对端看不到画面 / 停止共享后仍在发"。
// 现在只有 screen.js 里的 attachScreenToLink() 一份实现，轨挂在 **link 对象**上。
// 这里保留同名适配函数（调用点很多），io 一次性绑好，行为不变。
// ===========================================================================
function screenIo() {
  return {
    state, links, post, log, esc, sendSignal, boostOpusInSdp,
    primaryId, primaryLink, ensureLink, linkOf,
  };
}
const startScreenShare = (opts) => screenStartScreenShare(opts, screenIo());
const stopScreenShare = (reason) => screenStopScreenShare(reason, screenIo());
const attachScreenToLink = (link) => screenAttachScreenToLink(link, screenIo());
const isScreenSharing = () => screenIsScreenSharing();
const currentScreenStream = () => screenCurrentScreenStream();


// ===========================================================================
// 文件传输（走 DataChannel）—— 已搬到 file.js（S3）
//
// 搬出的是分片/背压/落盘回执那一整套（约 180 行），以及 sendingFile/receivingFile 两个
// **页面全局状态**（现在收进 file.js 的工厂闭包）。
// 这里只留薄转发，调用点（zxEngine.sendFile、DataChannel 的 onmessage）行为不变。
// ===========================================================================
const fileTx = createFileTransfer({
  post, log, esc, primaryChat,
});
const sendFile = (payload) => fileTx.sendFile(payload);
const handleFileMessage = (msg) => fileTx.handleFileMessage(msg);
const handleFileChunk = (buffer) => fileTx.handleFileChunk(buffer);


// ===========================================================================
// 麦克风电平表 —— 已搬到 voice.js（S3 第 3 步）
//
// 为什么界面必须有它：用户说"对方听不到我 / 声音小"时，第一步要判断的是
// **本机到底有没有拾到音**，而不是先怀疑网络或编码。
// 搬迁方式：voice.js 通过 io 注入 post/log/esc/isMuted，模块不反向依赖 call.js。
// ===========================================================================
const startMicMeter = (stream) => voiceStartMicMeter(stream, {
  post, log, esc, isMuted: () => state.muted,
});
const stopMicMeter = voiceStopMicMeter;

// ===========================================================================
// 录音辅助
// ===========================================================================

/** Float32 PCM → 16-bit PCM WAV（44 字节标准头）。 */
// encodeWav 已搬到 util.js（S3：被 voice.js / selfcheck.js 共享）

/** ArrayBuffer → base64（分块处理，避免大数组时参数溢出）。 */
// arrayBufferToBase64 已搬到 util.js（S3：被 file.js / voice.js 共享）

// ===========================================================================
// 通话状态：用一个带日志的 setter，而不是散落赋值
//
// 【为什么】
// state.inCall 原本在三处被直接赋值（callPeer / onOffer 的两个分支）。
// 出现过"没点任何按钮双方就自动进通话并开麦"的问题，而三处入口
// 靠读代码根本判断不出是哪一条路径触发的。
// 统一走这个 setter 后，每次变更都会记录调用栈与原因。
// ===========================================================================
function setInCall(value, reason) {
  if (state.inCall === value) return;
  const stack = (new Error().stack || '').split('\n').slice(2, 5)
    .map(s => s.trim()).join(' ← ');
  state.inCall = value;
  // 【2026-10-07】加入语音时，把"之前因为没加入而没播放"的远端流接上；
  // 退出语音时断开（避免没加入语音还能听到对方）。
  if (value) {
    if (state.remoteStream) {
      try { attachRemoteAudio(state.remoteStream); } catch (e) { }
    }
  } else {
    try { detachRemoteAudio(); } catch (e) { }
  }
  if (typeof reportCallState === 'function') reportCallState('加入/退出语音');
  log(`<span class="k">通话状态 ${value} ← ${esc(reason)}</span>` +
      (value ? `  栈=${esc(stack)}` : ''));
}
//
// ===========================================================================
// 真实的通话状态上报
//
// 【为什么不能直接用 connectionState 当通话状态】
// 这是本会话踩到的一个有实际后果的 bug：
// connectionState 只表示"PeerConnection 连上了"，而这条连接**同时承载
// 聊天与文件传输** —— 只要两个人进了同一个房间，连接就会建立。
// 于是仅仅为了聊天，界面就显示成"通话中"、静音按钮也变成可用，
// 用户会以为麦克风被打开了（事实上没有，但界面在说谎）。
//
// 所以判断依据换成**有没有音频轨在传输**：
//   getSenders()   里有 audio 轨 → 本机在发声音
//   getReceivers() 里有 audio 轨 → 对端在发声音
// 两者皆空 = "连着但没通话"。
// ===========================================================================
function reportCallState(reason) {
  if (!primaryPc()) {
    post({ type: 'call-link', link: 'none', sending: false, receiving: false,
           active: false });
    return;
  }

  // 只看轨道是否存在还不够 —— 有 receiver ≠ 有声音。
  // 建立数据链路时用 offerToReceiveAudio 会让对端凭空多出一个 recvonly
  // 接收器，于是"收音频=True"但实际一包都没有（本会话实测踩到）。
  // 所以要再加两层判据：track.muted（未收到媒体时浏览器会置 true）
  // 与 inbound 音频包计数。
  const allAudioSenders = primaryPc().getSenders()
    .filter(s => s.track && s.track.kind === 'audio');
  const audioReceivers = primaryPc().getReceivers()
    .filter(r => r.track && r.track.kind === 'audio');

  // =========================================================================
  // 【2026-10-07 根因修复·"发起通话"按钮点不了（用户报"语音功能不能用"）】
  //
  // 这里以前不区分轨道来源：`sending = 任意音频发送轨 enabled && !muted`。
  // 而 A1 的「共享电脑声音」会在**链路建立时就常驻挂一条音频轨**（它是活跃合成轨，
  // enabled=true / muted=false）⇒ sending 恒为 true ⇒ active 恒为 true ⇒
  // 界面一进房间就显示"通话中" ⇒ **"发起通话"按钮被禁用，用户根本点不了**
  // ⇒ 麦克风从来没被打开过 ⇒ 听不到任何声音。
  //
  // 修法：把"共享声音轨"从通话判据里**排除** —— 通话中必须由**麦克风轨**说话。
  // =========================================================================
  const sharedTrack = state.sharedAudio ? state.sharedAudio.track : null;
  const micSenders = allAudioSenders.filter(s => s.track !== sharedTrack);
  const audioSenders = micSenders;

  const micSending = micSenders.some(s => s.track.enabled && !s.track.muted);
  const sharedSending = allAudioSenders.some(s => s.track === sharedTrack
                                                 && s.track.enabled && !s.track.muted);

  const sending = micSending;
  // muted=true 表示还没收到媒体；也要求确实收到过包
  const receiving = audioReceivers.some(r => !r.track.muted) && state.gotAudioPackets;

  const link = primaryPc().connectionState;

  log(`链路=${link} 发音频=${sending}(${micSenders.length}麦轨) ` +
      `共享轨在发=${sharedSending} ` +
      `收音频=${receiving}(${audioReceivers.length}轨 ` +
      `muted=${audioReceivers.map(r => r.track.muted).join(',') || '-'} ` +
      `包=${state.gotAudioPackets})（${esc(reason)}）`);

  post({
    type: 'call-link',
    link,
    sending,
    receiving,
    // 【关键】"通话中"必须由**明确的通话状态**或**麦克风在发**决定：
    //   · state.inCall —— 用户点了「发起通话」，或收到对方的 purpose=call 请求
    //   · sending      —— 麦克风轨真的在发（已排除共享声音轨）
    // 绝不能用"有音频轨在传"当判据：A1 的共享声音轨在链路建立时就常驻挂着，
    // 那样一进房间就显示"通话中"⇒"发起通话"按钮被禁用⇒用户点不了（本轮根因）。
    // 【兜底】对方明确发起过通话（信令里带 purpose=call，见 state.peerCallRequested）
    // 且本端确实收到了音频 ⇒ 也算通话中。
    // 为什么不直接用 receiving：A1 的共享电脑声音也会让 receiving=true，
    // 那样"对方只是共享声音"就会被误判成通话（那正是 v72 修的原始问题）。
    active: state.inCall || sending || (state.peerCallRequested && receiving),
    senderTracks: micSenders.length,
    receiverTracks: audioReceivers.length,
    sharedTrackSending: sharedSending,
    inCall: !!state.inCall,
  });
}

// ===========================================================================
// 降噪档位
//
// 【重要前提：浏览器只给布尔开关】
// getSupportedConstraints() 里 noiseSuppression 是**布尔**的，没有"轻/中/强"。
// 所以"三档"不能靠改约束实现 —— 那样做是假的，用户听不出区别。
//
// 这里的做法是把档位拆成两层真实处理：
//   1) 浏览器的 noiseSuppression / autoGainControl（Chromium 内建）
//   2) Web Audio 侧的附加链（仅 strong 档）
//
//   off     浏览器 NS 关、AGC 关、AEC 开（回声消除必须留，否则外放必炸）
//   normal  浏览器 NS 开、AGC 开、AEC 开。默认，通话推荐。
//   strong  normal + 软件侧：100 Hz 高通 + **温和**扩展器
//
// 【踩过的坑：双重自动增益】
// 早先 strong 档挂的是 DynamicsCompressor(threshold=-45, ratio=4)，
// 而浏览器的 autoGainControl 此时也是开的 —— 两套增益控制互相追，
// 听感就是"声音忽大忽小"。这是用户反馈里明确列出的症状之一。
// 现在改成：
//   · strong 档**关掉浏览器 AGC**，只留软件链，避免双头控制
//   · 用 ratio 1.8 的温和扩展（而不是 ratio 4 的压缩），只压底噪不压人声
// ===========================================================================
let localAudioChain = null;   // { ctx, source, highpass, expander, destination }

/** 按档位重建发送路径的音频处理链。返回实际生效的描述。 */
function buildAudioChain(stream, level) {
  // 拆掉旧链
  if (localAudioChain) {
    try { localAudioChain.source.disconnect(); } catch (e) { }
    try { localAudioChain.highpass?.disconnect(); } catch (e) { }
    try { localAudioChain.expander?.disconnect(); } catch (e) { }
    try { localAudioChain.ctx?.close(); } catch (e) { }
    localAudioChain = null;
  }
  if (!stream || level !== 'strong') return null;

  try {
    const ctx = new AudioContext();
    const source = ctx.createMediaStreamSource(stream);

    // 100 Hz 高通：人声基频最低约 85 Hz（男低音），
    // 100 Hz 基本不碰人声，但能削掉空调/桌面/电流的低频隆隆声。
    const highpass = ctx.createBiquadFilter();
    highpass.type = 'highpass';
    highpass.frequency.value = 100;
    highpass.Q.value = 0.7;

    // 温和扩展器：把 -50 dB 以下的残余底噪再压一点。
    //
    // 为什么用压缩器当扩展器：Web Audio 没有专门的 expander，
    // 而 DynamicsCompressor 的参数在低阈值 + 低压缩比时，
    // 对"远低于阈值"的部分等效于轻度下行扩展，且不会像
    // 高压缩比那样把人声压扁（那正是"发闷"的来源之一）。
    const expander = ctx.createDynamicsCompressor();
    expander.threshold.value = -50;   // 只在很安静的部分生效
    expander.knee.value = 20;         // 宽拐点 = 渐进过渡，不产生"抽气"感
    expander.ratio.value = 1.8;       // 温和：旧值是 4，太激进
    expander.attack.value = 0.01;     // 10ms：不抢人声起始
    expander.release.value = 0.35;

    const destination = ctx.createMediaStreamDestination();

    source.connect(highpass);
    highpass.connect(expander);
    expander.connect(destination);

    localAudioChain = { ctx, source, highpass, expander, destination };
    log('<span class="ok">已启用软件降噪链</span>（100 Hz 高通 + 温和扩展，已关浏览器 AGC 以免双重增益）');
    return destination.stream;
  } catch (e) {
    log(`<span class="warn">软件降噪链构建失败: ${esc(e.message)}</span>`);
    return null;
  }
}

/** 重建音频处理链（挂断后旧链里留着已 stop 的轨道，必须丢掉）。 */
function resetAudioChain() {
  try {
    if (localAudioChain) {
      try { localAudioChain.source?.disconnect(); } catch (e) { }
      try { localAudioChain.destination?.disconnect(); } catch (e) { }
      try { localAudioChain.ctx?.close(); } catch (e) { }
    }
  } catch (e) { }
  localAudioChain = null;
}

/**
 * 当前应该送给对端的音频轨道。
 * 【2026-10-07 修复·"挂断后再加入语音变成仅收听"】
 * 以前只要 `localAudioChain.destination` 存在就直接返回它的轨道，**不检查死活**。
 * 而挂断会 `stop()` 麦克风轨、却不会清掉这条链 ⇒ 再次「加入语音」时 addTrack 的是
 * 一条 `readyState==='ended'` 的死轨 ⇒ **媒体根本发不出去** ⇒ 界面显示"仅收听"
 *（用户实测："挂断一次再加入会变成仅收听"、"房主麦克风没声音"）。
 * 现在只认活轨。
 */
function outgoingTrack() {
  if (localAudioChain?.destination) {
    const t = localAudioChain.destination.stream.getAudioTracks()[0];
    if (t && t.readyState === 'live') return t;
  }
  const t2 = state.localStream?.getAudioTracks()[0];
  return (t2 && t2.readyState === 'live') ? t2 : null;
}

// ===========================================================================
// Opus 编码质量
//
// 【为什么必须显式设置】
// 不设置时，WebRTC 完全交给带宽估计决定码率，实测在回环/空闲链路上会压到
// **15 kbps** 左右 —— 这个码率下语音明显发闷、齿音与气息声丢失，听感就是
// "音质差"。而 WebRTC 的带宽估计偏保守：它优先保连接稳定，不会主动为了
// 音质多花带宽。
//
// 参数取值依据：
//   maxaveragebitrate=48000  Opus 在 48 kbps 单声道下已达到"透明"
//                            （听不出编码痕迹）。再高对语音收益很小。
//   maxBitrate=96000         给突发留的瞬时上限；拥塞时由 WebRTC 自行降档，
//                            所以不怕设高。
//   useinbandfec=1           轻微丢包用冗余恢复，而不是靠降码率规避
//   usedtx=1                 静音不发包，把带宽全留给说话时
//   stereo=0                 单声道：同样的带宽，单声道质量明显更好
// ===========================================================================
const OPUS_TARGET_BITRATE = 48000;      // 目标平均码率
const OPUS_MAX_BITRATE = 96000;         // 瞬时上限

async function tuneOpusQuality(pc) {
  try {
    const sender = pc.getSenders().find(s => s.track && s.track.kind === 'audio');
    if (!sender) {
      log('<span class="warn">找不到音频发送器，跳过 Opus 调优</span>');
      return;
    }

    const params = sender.getParameters();
    // 某些浏览器首次 getParameters() 返回的 encodings 是空数组，必须补一个
    if (!params.encodings || params.encodings.length === 0) {
      params.encodings = [{}];
    }
    params.encodings[0].maxBitrate = OPUS_MAX_BITRATE;
    // 网络允许时不要自我降码率：priority 影响拥塞时的取舍
    params.encodings[0].priority = 'high';
    params.encodings[0].networkPriority = 'high';

    // 通过 codec 的 fmtp 下发 Opus 参数。
    // 这一步在部分实现上会被忽略（需要 SDP 协商后才生效），所以下面
    // 还会在 setLocalDescription 之后用实际协商结果校正一次。
    await sender.setParameters(params);
    log(`<span class="ok">Opus 码率目标已设为 ${OPUS_TARGET_BITRATE / 1000} kbps</span>`);
  } catch (e) {
    log(`<span class="warn">Opus 调优失败（不影响通话）: ${esc(e.message)}</span>`);
  }
}

/**
 * 在 SDP 里直接改写 Opus 的 fmtp 行 —— 这是最可靠的方式。
 *
 * setParameters 改的是"发送端上限"，而 Opus 的真实工作点由 SDP 协商出来的
 * fmtp 决定。两者都做，才能确保 40 kbps 真的生效。
 */
/**
 * 在 SDP 里直接改写 Opus 的 fmtp 行 —— 这是最可靠的方式。
 *
 * setParameters 改的是"发送端上限"，而 Opus 的真实工作点由 SDP 协商出来的
 * fmtp 决定。两者都做，才能确保 40 kbps 真的生效。
 *
 * 【必须容错】这是"音质优化"，不是通话的必要条件。一旦这里抛异常，
 * 整个 callPeer/onOffer 流程会被中断，表现为"通话建立不起来"——
 * 本会话就这样踩过一次（只加了改写、没加保护，结果双方连不上）。
 * 所以任何失败都必须退回原始 SDP，让通话先成立。
 */
function boostOpusInSdp(sdp) {
  try {
    if (!sdp || typeof sdp !== 'string') return sdp;

    const m = sdp.match(/a=rtpmap:(\d+)\s+opus\/48000/i);
    if (!m) {
      log('<span class="warn">SDP 里没有 opus，跳过码率调优</span>');
      return sdp;
    }
    const pt = m[1];

    const desired = [
      'maxaveragebitrate=' + OPUS_TARGET_BITRATE,
      'useinbandfec=1',
      // DTX 开着：静音时不发包。
      // 曾经为了"不削掉人声起始"把它关掉，结果是静音期也在持续发包、
      // 码率数字被静音占满，而说话时反而分不到带宽。
      // WebRTC 的 VAD 对人声起始的处理足够好，DTX 该开。
      'usedtx=1',
      'stereo=0',
      'sprop-stereo=0',
      'minptime=10',
      'ptime=20',
    ];
    const desiredKeys = desired.map(x => x.split('=')[0]);
    const fmtpLine = 'a=fmtp:' + pt + ' ' + desired.join(';');

    const fmtpRe = new RegExp('^a=fmtp:' + pt + ' .*$', 'm');
    let out;
    if (fmtpRe.test(sdp)) {
      // 已有 fmtp：用函数式 replace，避免 $ 在替换串里被当特殊标记
      out = sdp.replace(fmtpRe, (old) => {
        const kept = old.replace(/^a=fmtp:\d+\s*/, '')
                        .split(';')
                        .map(s => s.trim())
                        .filter(s => s && !desiredKeys.includes(s.split('=')[0]));
        return 'a=fmtp:' + pt + ' ' + kept.concat(desired).join(';');
      });
    } else {
      // 没有 fmtp：在 rtpmap 行后补一行
      const rtpmapRe = new RegExp('^(a=rtpmap:' + pt + ' opus\\/48000.*)$', 'm');
      out = sdp.replace(rtpmapRe, (line) => line + '\r\n' + fmtpLine);
    }

    // 自检：改完必须仍含 opus rtpmap，且新码率确实在
    if (!/opus\/48000/i.test(out) || !out.includes('maxaveragebitrate=' + OPUS_TARGET_BITRATE)) {
      log('<span class="warn">SDP 改写结果异常，回退原值</span>');
      return sdp;
    }
    log(`<span class="ok">SDP 已抬高 Opus 码率至 ${OPUS_TARGET_BITRATE / 1000} kbps</span>`);
    return out;
  } catch (e) {
    log(`<span class="warn">SDP 改写失败，用原值继续: ${esc(e.message)}</span>`);
    return sdp;
  }
}


/** 打开麦克风。返回 stream 或 null。 */
async function openMic() {
  // strong 档会挂软件增益链，此时必须关掉浏览器的 AGC，
  // 否则两套增益控制互相追，听感就是"声音忽大忽小"。
  const useSoftwareChain = state.denoiseLevel === 'strong';

  const constraints = {
    audio: {
      echoCancellation: state.echoCancellation,
      noiseSuppression: state.noiseSuppression,
      autoGainControl: useSoftwareChain ? false : state.autoGainControl,
      channelCount: 1,
      // 设置页选了具体麦克风就用它；没选则交给系统默认
      ...(state.micDeviceId ? { deviceId: { exact: state.micDeviceId } } : {}),
    },
    video: false,
  };

  const stream = await navigator.mediaDevices.getUserMedia(constraints);
  const track = stream.getAudioTracks()[0];
  const st = track.getSettings ? track.getSettings() : {};

  // 按当前档位架软件侧处理链（strong 档才会真的建链）
  buildAudioChain(stream, state.denoiseLevel);

  const applied = {
    echoCancellation: st.echoCancellation,
    noiseSuppression: st.noiseSuppression,
    autoGainControl: st.autoGainControl,
    sampleRate: st.sampleRate,
    channelCount: st.channelCount,
    label: track.label,
    denoiseLevel: state.denoiseLevel,
    softwareChain: !!localAudioChain,
  };
  // 浏览器接受约束 ≠ 真的生效，必须读回来确认
  post({ type: 'mic-opened', applied });
  log(`${ok(st.noiseSuppression === true)} 麦克风: ${esc(track.label)}  ` +
      `AEC=${st.echoCancellation} NS=${st.noiseSuppression} ` +
      `AGC=${st.autoGainControl} 档位=${state.denoiseLevel}`);

  // 起电平表，让界面能看到本机是否真的在拾音
  startMicMeter(stream);

  return stream;
}

// ===========================================================================
// 信令（WebSocket）—— 已搬到 signaling.js（S3）
//
// 搬走的是连/发/收 三件事（含两条坑的注释：sendSignal 未连接时必须报错、
// JSON.parse 失败不能变成"未捕获异常"）。这里只接线：
//   · 信令要调 mesh 的入口（ensureLink/onOffer/onAnswer/onRemoteIce/hangup…）
//   · mesh 要通过 sendSignal 发消息 ⇒ 互相依赖，用注入解掉（与页面初始化时无关顺序）
// call.js 初始化时调用 initSignaling() 建立这个闭包。
// ===========================================================================
/** 信令模块的注入面（只写一次，避免各处再拼）。 */
function signalIo() {
  return {
    state, post, log, esc, links,
    primaryId, primaryPc, primaryLink, primaryChat,
    adoptPrimaryAliases, linkOf, unregisterLink,
    ensureLink, onOffer, onAnswer, onRemoteIce, hangup, detachRemoteVideo,
  detachRemoteAudio,
  resetAudioChain,
  };
}

let stats = null;
let mesh = null;
let signaling = null;
let moduleHost = null;

/** mesh.js 的注入面（网络与媒体逻辑需要的一切，一次写清）。 */
function meshIo() {
  return {
    post, log, esc, state, links,
    primaryId, primaryPc, primaryLink, primaryChat, linkOf, adoptPrimaryAliases, syncPrimaryAliases,
    createPeerConnection, boostOpusInSdp, outgoingTrack, openMic,
    setInCall, reportCallState, sendSignal, stopStatsLoop, tuneOpusQuality,
    handleFileMessage, handleFileChunk, stopMicMeter,
    attachRemoteAudio, attachRemoteVideo, attachScreenAudio, detachRemoteVideo,
    attachScreenToLink, isScreenSharing,
    reportLinks,                 // "共享声音"那段已定义（块外），mesh 通过注入用
    ensureSharedAudioTrack,      // 同上：留在 call.js，mesh 建链时要给链路挂共享声音轨
    attachSharedAudioTrack,      // 同上
  };
}

/**
 * 接线全部页面模块（S4 方案 A：声明式，顺序即依赖顺序）。
 *
 * 【为什么用宿主】S3 拆出 13 个模块后，接线还是散在文件末尾的一串调用，
 * 而且"谁持有定时器/流、谁负责释放"全靠人记。现在：
 *   · 顺序在一张表里说清；
 *   · 释放由 host.disposeAll() 按相反顺序统一做（模块自己的 dispose() 是真源）；
 *   · 单个模块起不来只影响它自己，并上报 module-error（不许静默）。
 *
 * 【顺序为什么是这个】webrtc → mesh（建链要用 PC）→ signaling（信令要调 mesh 的入口）
 * → stats（统计依赖主链路）→ voice/screen/file/selfcheck（彼此独立，放最后）。
 */
function wireModules() {
  moduleHost = createModuleHost({ log, post });
  mesh = moduleHost.wire({ name: 'mesh', create: createMesh, deps: meshIo() });
  signaling = moduleHost.wire({ name: 'signaling', create: createSignaling, deps: signalIo() });
  stats = moduleHost.wire({ name: 'stats', create: createStats,
                            deps: { post, log, esc, state, primaryPc, reportCallState } });
  moduleHost.wire({ name: 'voice', create: () => ({ dispose: disposeVoice }) });
  moduleHost.wire({ name: 'screen', create: () => ({ dispose: disposeScreen }) });

  // 页面自己持有的资源（不属于任何模块，但卸载时同样要收）
  moduleHost.addDisposable('page-resources', () => {
    try { if (state.localStream) state.localStream.getTracks().forEach((t) => t.stop()); } catch (e) { }
    try { if (state.ws) state.ws.close(); } catch (e) { }
    state.localStream = null;
    state.ws = null;
  });

  // 【自检口径】模块清单进应用日志：起没起齐一眼能看到（不依赖界面）
  post({ type: 'modules-ready', modules: moduleHost.names() });
}

// ---- 薄访问器（信令闭包初始化后才有值）----
// 【为什么用 function 声明】`signalIo()` 会引用 `links` 等 `const`；若在文件顶部就调用
// initSignaling()，模块求值阶段会踩**暂时性死亡**（TDZ）⇒ 抛 ReferenceError
// ⇒ `post({type:'engine-loaded'})` 永远发不出去（C# 只看到"未上报 engine-loaded"）。
// 所以：访问器用可提升的 function 声明，真正的接线放在**模块末尾**（见 initSignaling() 调用处）。
function connectSignal() { return signaling.connectSignal(); }
function sendSignal(obj) { return signaling.sendSignal(obj); }
function handleSignal(msg) { return signaling.handleSignal(msg); }

// ===========================================================================
// WebRTC
// ===========================================================================

// ===========================================================================
// ICE 配置（STUN / TURN）—— 页面**不写死任何 TURN 地址或账号**
//
// 【接口契约：与 C# 侧约定】应用层在页面就绪后下发一次，用户在设置里改了再下发：
//   zxEngine.setIceConfig({ iceServers: [{ urls, username, credential }] })   ← 首选
//   zxEngine.setTurnConfig({ urls, username, credential })                    ← 兼容别名
//   · urls 接受字符串或字符串数组（'turn:host:3478?transport=udp'）
//   · 空 / 不调用 ⇒ 保持 STUN-only（等于现状）
// 收到后合并进 iceServers，并对**已存在**的每条 PeerConnection 调
// setConfiguration() 立即生效（否则要等下一次建链才用得上）。
//
// 【为什么必须有 TURN】只有 STUN 时，双方都在对称 NAT / 企业防火墙后面就拿不到
// 可用候选，ICE 必然 failed —— 这是"跨网段必失败"的根因。
// ===========================================================================

// 默认（无 TURN 下发时）：只用 STUN 拿反射候选
const DEFAULT_ICE_SERVERS = [
  { urls: 'stun:stun.l.google.com:19302' },
  { urls: 'stun:stun1.l.google.com:19302' },
];

// 运行期生效的 iceServers。只改这一处，别在别处写死地址。
state.iceServers = DEFAULT_ICE_SERVERS.slice();
state.iceConfigured = false;      // 是否真的拿到了 TURN（用于失败原因判断）

/** 由 state.iceServers 生成传给 RTCPeerConnection 的配置。 */
function buildRtcConfig() {
  return { iceServers: state.iceServers };
}

/** urls → 字符串数组（接受字符串 / 数组 / 逗号分隔）。 */
function normalizeIceUrls(urls) {
  if (Array.isArray(urls)) {
    return urls.map(u => String(u == null ? '' : u).trim()).filter(Boolean);
  }
  return String(urls == null ? '' : urls).split(',')
    .map(u => u.trim()).filter(Boolean);
}

/** {urls,username,credential} → 合法 iceServer；不合法返回 null。 */
function normalizeIceServer(entry) {
  if (!entry || typeof entry !== 'object') return null;
  const urls = normalizeIceUrls(entry.urls || entry.url);
  if (!urls.length) return null;
  const out = { urls: urls.length === 1 ? urls[0] : urls };
  if (entry.username) out.username = String(entry.username);
  if (entry.credential) out.credential = String(entry.credential);
  if (entry.credentialType) out.credentialType = String(entry.credentialType);
  return out;
}

/** 合并：应用层下发的在前，默认 STUN 兜底；按 urls 去重。 */
function makeIceServerList(list) {
  const seen = new Set();
  const out = [];
  for (const raw of (Array.isArray(list) ? list : [])) {
    const s = normalizeIceServer(raw);
    if (!s) continue;
    const key = Array.isArray(s.urls) ? s.urls.join('|') : s.urls;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(s);
  }
  for (const s of DEFAULT_ICE_SERVERS) {
    if (!seen.has(s.urls)) { seen.add(s.urls); out.push(s); }
  }
  return out;
}

/** 账密不进日志：把 URL 里的 user:pass@ 抹掉。 */

/** 只统计数量/类型，绝不回显账号密码。 */

/** 应用到所有 PeerConnection（含已存在的），并记住供后续新建使用。 */
function applyIceServers(servers) {
  state.iceServers = servers;
  const pcs = new Set();
  if (primaryPc()) pcs.add(primaryPc());
  for (const l of links.values()) if (l && l.pc) pcs.add(l.pc);
  let live = 0, failed = 0;
  for (const pc of pcs) {
    try { pc.setConfiguration(buildRtcConfig()); live++; }
    catch (e) {
      failed++;
      log(`<span class="warn">setConfiguration 失败: ${esc(e.message)}</span>`);
    }
  }
  return { live, failed, pcs: pcs.size };
}

/**
 * 下发 ICE 配置（对外接口的实现，setIceConfig / setTurnConfig 共用）。
 * 空配置 = 只用 STUN。返回结果同时上报 `ice-config` 事件。
 */
function setIceConfigImpl(cfg) {
  const raw = (cfg && Array.isArray(cfg.iceServers) && cfg.iceServers.length)
    ? cfg.iceServers
    : (() => { const one = normalizeIceServer(cfg || {}); return one ? [one] : []; })();
  const servers = raw.length ? makeIceServerList(raw) : DEFAULT_ICE_SERVERS.slice();
  const applied = applyIceServers(servers);
  const sum = summarizeIceServers(servers);
  // 【Lead 钉的 ack 语义】count = 调用方下发的 iceServers 条数（空数组 ⇒ 0），
  // 不是合并后的总数（合并后总会带上默认 STUN，用它会让"空配置"看起来像下发成功）。
  const supplied = raw.filter(r => normalizeIceServer(r)).length;
  state.iceConfigured = (sum.turn + sum.turns) > 0;

  const detail = `收到 ${supplied} 条 / 生效 ${sum.total} 条` +
                 `（STUN ${sum.stun} / TURN ${sum.turn + sum.turns}）` +
                 (state.iceConfigured ? '' : '｜未下发 TURN，仅 STUN；跨网段可能打不通');
  log(`<span class="k">ICE 配置已更新</span> ${esc(detail)}；` +
      `已应用到 ${applied.live}/${applied.pcs} 条链路`);
  post({
    type: 'ice-config',
    count: supplied,             // Lead 钉的字段名：空数组 ⇒ count:0；只报个数
    ok: true,                    // ack：C# 用它确认"页面就绪且配置已生效"
    effective: sum.total,        // 合并后实际生效条数（含默认 STUN），仅诊断用
    stun: sum.stun, turn: sum.turn, turns: sum.turns,
    turnConfigured: state.iceConfigured,
    applied: applied.live, live: applied.pcs, failed: applied.failed,
  });
  return { ok: applied.failed === 0, count: supplied, effective: sum.total,
           turnConfigured: state.iceConfigured, live: applied.live };
}

/**
 * 【Lead 钉死的入口，不许改名】C# 侧调用：
 *   window.zxIceConfig({ iceServers: [ { urls, username, credential } ] })
 * 空 / 字段缺失 ⇒ 只保留 STUN，不抛异常。
 * 另有 window.zxIceConfigReady = true 供 C# 轮询确认页面已就绪（防"页面没加载完就下发"）。
 * zxEngine.setIceConfig / zxEngine.setTurnConfig 是同一实现的别名。
 */
window.zxIceConfig = function (cfg) {
  try {
    return setIceConfigImpl(cfg || {});
  } catch (e) {
    // 配置下发失败不许静默：报错并退回 STUN-only，别把页面整体带崩
    log(`<span class="bad">ICE 配置下发失败: ${esc(e && e.message ? e.message : e)}</span>`);
    post({ type: 'ice-config', count: 0, ok: false,
           error: String(e && e.message ? e.message : e) });
    return { ok: false, error: String(e && e.message ? e.message : e) };
  }
};
window.zxIceConfigReady = true;

/** 失败原因要说清：拿到了哪几种候选、有没有中继、有没有配 TURN。 */
// describeIceFailure 已搬到 webrtc.js（S3）
function describeIceFailure(pc) { return webrtcDescribeIceFailure(pc, webrtcIo()); }

// createPeerConnection 已搬到 webrtc.js（S3）
function createPeerConnection(remoteId) { return webrtcCreatePeerConnection(remoteId, webrtcIo()); }

/** webrtc.js 需要的注入面（一次写清，避免各处再拼）。 */
function webrtcIo() {
  return {
    post, log, esc, state, primaryPc, reportCallState, sendSignal, startStatsLoop,
    attachRemoteAudio, attachRemoteVideo, attachScreenAudio, detachRemoteVideo,
  };
}

// ===========================================================================
// 文字聊天：走 WebRTC DataChannel
//
// 【为什么用 DataChannel 而不是经过信令服务器】
// 信令服务器只是"牵线"用的，我们可以自己控制它。
// 而聊天内容应该**端到端加密、不经服务器**：
//   · DataChannel 走 DTLS-SRTP，端到端加密，服务器看不到内容
//   · 不增加服务器负担，也不留聊天记录在别人机器上
//   · 断开会话就自然失效，不需要额外的清理
//
// 代价：DataChannel 只在对端之间建立后才可用（需要先连上）。
// 对"已经连上的人之间聊天"这个场景完全够。
//
// 与语音通话**互相独立**：可以只聊天不打电话，也可以边聊边打。
// ===========================================================================


// ===========================================================================
// 【S1：多链路注册表】见 docs/multi-peer-mesh-design-2026-10-05.md
//
// 为什么先只做"登记"而不改读写：state.pc / primaryId() / chatChannel 在页面里
// 分别被引用 63 / 22 / 23 次，直接换结构风险太大。所以 S1 先把"每个对端一条链路"
// 的数据结构立起来，**读写仍然走原来的单例变量，行为完全不变**；
// 后续 S2（信令按 from 路由）、S4（共享扇出到每条链路）再逐步切过来。
// ===========================================================================
const links = new LinkRegistry();   // peerId -> Link（见 link.js；S3 第 2 步起它是唯一事实来源）

function reportLinks() {
  const ids = [...links.keys()];
  log(`<span class="k">[链路注册表] ${ids.length} 条：${ids.join(',') || '无'}</span>`);
  // 上报给应用：这是 S1 的"可观测证据"，也让后面 S2–S5 的验证有据可查
  post({ type: 'links', count: ids.length, ids });
}

/**
 * 把正在共享的电脑声音加到链路：**只 addTrack，不在这里协商**。
 *
 * 【为什么不能在这里 createOffer】registerLink 是在处理**对方 offer 的过程中**被调用的，
 * 此刻 PC 处于 have-remote-offer 状态，createOffer 会直接报
 * "Called in wrong state: have-remote-offer"（实测踩到）。
 * 正确做法：先把轨加上，紧接着那条流程里的 createAnswer 就会把它协商进去。
 *
 * 【死代码已删】原来这里写着"对已稳定的链路由 renegotiateSharedAudio 单独发 offer"。
 * 那个函数（createOffer + setLocalDescription，purpose='share-audio'）全仓库零调用，
 * 2026-10-06 审计列为死代码，已删除 —— 现在每条链路**建链时就带轨**，任何情况下都不需要
 * 为共享声音重新协商（见 ensureSharedAudioTrack 的说明）。
 */
function attachSharedAudioTrack(id, pc) {
  const s = state.sharedAudio;
  if (!s || !pc || s.sent.some((x) => x.pc === pc)) return false;
  try {
    s.sent.push({ pc, sender: pc.addTrack(s.track, s.stream) });
    log(`<span class="ok">共享声音已加到链路 ${esc(String(id))}</span>`);
    post({ type: 'share-audio-attached', link: String(id), sent: s.sent.length, links: links.size });
    return true;
  } catch (e) {
    log(`<span class="warn">共享声音加链路失败: ${esc(e.message)}</span>`);
    post({ type: 'share-audio-attach-error', link: String(id), message: String(e.message) });
    return false;
  }
}

/**
 * 确保「共享电脑声音」这条音轨存在（**不管有没有在共享**）。
 *
 * 【为什么一开始就建】之前是"开始共享时再加轨、然后重新协商"，实测反复失败：
 *   answer 不能新增 m-line、发起方不走 onOffer、negotiationneeded / connectionstatechange
 *   也都没触发。换成本做法后**根本不需要重新协商**：链路建立时就把这条（静音的）音轨
 *   一起协商进去，开始共享只是往里灌 PCM，停止共享只是停止灌 —— 轨道本身一直在。
 * 代价：始终多一条音频 m-line（静音时 Opus 基本不占带宽）。这是标准取舍。
 */
let sharedAudioTrackPromise = null;
async function ensureSharedAudioTrack() {
  if (state.sharedAudio) return state.sharedAudio.track;
  if (sharedAudioTrackPromise) return sharedAudioTrackPromise;
  sharedAudioTrackPromise = (async () => {
    try {
      const ctx = new AudioContext({ sampleRate: 48000 });
      await ctx.audioWorklet.addModule('share-audio-worklet.js');
      const node = new AudioWorkletNode(ctx, 'share-audio-player');
      const dest = ctx.createMediaStreamDestination();
      // 【A1 修复】worklet 与 destination 的声道数必须一致：worklet 默认按输入通道数
      // 输出（实测 1 声道），而 MediaStreamAudioDestinationNode 默认建 2 声道音轨 ——
      // 不一致时实测现象是"音轨 live、RMS 非零，但 RTP audioOutBytes 恒为 0"。
      // 这里两边都显式定为 2 声道（Opus 立体声是标准路径）。
      try {
        node.channelCount = 2;
        node.channelCountMode = 'explicit';
        node.channelInterpretation = 'speakers';
        dest.channelCount = 2;
        dest.channelCountMode = 'explicit';
        dest.channelInterpretation = 'speakers';
      } catch (e) { /* 老内核不支持这些属性：保持原样，不影响其它逻辑 */ }
      // 【A1 探针】两段 RMS 对比：worklet 有没有出声音 vs 音轨里有没有声音。
      // 只报 maxOutPeak（worklet 内部）不足以判定 —— 实测那儿有 0.55 但 RTP 是 0 字节。
      const anNode = ctx.createAnalyser();
      anNode.fftSize = 1024;
      node.connect(anNode);
      const anDest = ctx.createAnalyser();
      anDest.fftSize = 1024;
      ctx.createMediaStreamSource(dest.stream).connect(anDest);
      const rmsOf = (an) => {
        try {
          const b = new Float32Array(an.fftSize);
          an.getFloatTimeDomainData(b);
          let s = 0;
          for (let i = 0; i < b.length; i++) s += b[i] * b[i];
          return Math.sqrt(s / b.length);
        } catch (e) { return -1; }
      };
      state.sharedAudio = { ctx, node, dest, anNode, anDest, rmsOf,
                            nodeRms: 0, destRms: 0,
                            trackState: () => (dest.stream.getAudioTracks()[0] || {}).readyState || 'none' };
      node.connect(dest);
      // 每 500ms 采一次两段 RMS（诊断；采样很便宜）
      const diagTimer = setInterval(() => {
        const s = state.sharedAudio;
        if (!s || !s.rmsOf) return;
        s.nodeRms = s.rmsOf(s.anNode);
        s.destRms = s.rmsOf(s.anDest);
      }, 500);
      state.sharedAudio.diagTimer = diagTimer;
      const track = dest.stream.getAudioTracks()[0];
      // 复用上面探针建好的对象（补上 track/stream/sent 等字段）
      state.sharedAudio.track = track;
      state.sharedAudio.stream = dest.stream;
      state.sharedAudio.sent = [];
      state.sharedAudio.pushed = 0;
      state.sharedAudio.worklet = null;
      state.sharedAudio.pushedSamples = 0;
      try {
        node.port.onmessage = (ev) => {
          if (ev && ev.data && state.sharedAudio) state.sharedAudio.worklet = ev.data;
        };
        node.port.postMessage({ type: 'stats' });     // 主动要一次，确认通道双向可用
      } catch (e) { /* 回执拿不到不影响播放，只是诊断少一格 */ }
      log('<span class="k">共享声音音轨已就绪（静音待用）</span>');
      return track;
    } catch (e) {
      sharedAudioTrackPromise = null;
      post({ type: 'share-audio-error', message: String((e && e.message) || e) });
      return null;
    }
  })();
  return sharedAudioTrackPromise;
}
// ===========================================================================
// 【S3：链路显式化】事实来源只有 io/links（LinkRegistry）；下面这些是**兼容视图**。
// 只有 syncPrimaryAliases() 能写 state.pc/state.remoteId；读一律走访问器。
// mesh.js 通过 io 注入使用它们，所以它们必须留在 call.js。
// ===========================================================================

/** 当前主链路（显式 API，S3 起新代码一律用它）。 */
function primaryLink() {
  return links.primary();
}

/** 按对端取链路。 */
function linkOf(id) {
  return links.get(id);
}

/**
 * 兼容视图的**唯一写入口**：主链路换了/没了就调它。
 * 只有 state.pc / state.remoteId 两个字段还需要"同步"；chatChannel **不是变量**，
 * 由 primaryChat() 直接从主链路对象上取 —— 从根上消灭"同一事实存两份"。
 */
function syncPrimaryAliases() {
  const l = primaryLink();
  state.pc = l ? l.pc : null;
  state.remoteId = l ? l.id : null;
  return l;
}
const adoptPrimaryAliases = syncPrimaryAliases;   // 旧名字保留（调用点多，语义相同）

// ---- 兼容访问器：读的地方统一走这三个（将来换成"传 link 参数"只改这里）----
// 【必须用 function 声明而不是 const 箭头】文件顶部/末端的初始化会用到它们，
// const 箭头有**暂时性死区**（TDZ）⇒ 模块加载期直接抛错、整页不启动（实测踩到两次）。
function primaryPc() { return state.pc; }
function primaryId() { return state.remoteId; }
function primaryChat() {
  // 【④ 根因修复】优先返回**任何一条已经 open 的聊天通道**，而不是死认主链路那一条。
  // 实测：房间里出现"幽灵对端"或双方各自建链时，主链路可能指向一条 chat 为 null 的链路
  // （日志：`[聊天诊断] bind state=open` 但随后 `[聊天] 通道尚未打开，消息已排队`）⇒
  // 文字永远发不出去，用户报"文字不可用"。open 的通道才是可用通道。
  if (typeof links !== 'undefined' && links && typeof links.values === 'function') {
    for (const l of links.values()) {
      if (l && l.chat && l.chat.readyState === 'open') return l.chat;
    }
  }
  const l = primaryLink();
  return l ? (l.chat || null) : null;
}

// ===========================================================================
// 视图层接线（留在 call.js）—— 把远端流接到隐藏的 audio/video 元素上。
// mesh.js 需要它们时通过 io 注入（见 meshIo），所以必须留在这里，不能一起搬走。
// ===========================================================================
/** 把屏幕流的音频接上播放（独立元素，避免顶掉通话语音）。 */
let screenAudioEl = null;
function attachScreenAudio(stream) {
  if (!screenAudioEl) {
    screenAudioEl = document.createElement('audio');
    screenAudioEl.autoplay = true;
    document.body.appendChild(screenAudioEl);
  }
  screenAudioEl.srcObject = stream;
  screenAudioEl.play().catch(e => {
    // 【静默失败必修】播放被拦 = 用户完全听不到对方的屏幕声音，但以前只写 DOM 日志，
    // 应用日志与界面都没有任何痕迹。
    const msg = `屏幕声音播放被拦: ${(e && e.message) || e}`;
    log(`<span class="bad">${esc(msg)}</span>`);
    post({ type: 'call-error', source: 'screen-audio', reason: msg });
  });
}

/** 把远端音频流接上播放。用隐藏的 audio 元素最简单可靠。 */
let remoteAudioEl = null;
/**
 * 断开远端语音（挂断时调用）。
 * 【为什么要有这个函数】mesh.js 以前直接写 `remoteAudioEl.srcObject = null`，
 * 而 remoteAudioEl 是本模块的变量 ⇒ mesh 里 ReferenceError ⇒ **hangup 中途中断**，
 * 后面的 setInCall(false) 不执行 ⇒ 两端状态停在"通话中"（实测）。
 * 跨模块一律走注入，不碰别人的变量。
 */
function detachRemoteAudio() {
  try {
    if (remoteAudioEl) remoteAudioEl.srcObject = null;
  } catch (e) { }
}

function attachRemoteAudio(stream) {
  if (!remoteAudioEl) {
    remoteAudioEl = document.createElement('audio');
    remoteAudioEl.autoplay = true;
    document.body.appendChild(remoteAudioEl);
  }
  remoteAudioEl.srcObject = stream;
  remoteAudioEl.play().catch(e => {
    // 同上：远端语音播放被拦是"对方听得到我、我听不到对方"的根因之一，必须上报。
    const msg = `远端音频播放被拦: ${(e && e.message) || e}`;
    log(`<span class="bad">${esc(msg)}</span>`);
    post({ type: 'call-error', source: 'remote-audio', reason: msg });
  });
  applySpeaker();
}

/**
 * 测试扬声器：播放 1 秒 440Hz 提示音到**当前选定的扬声器**。
 * 【为什么要有】用户报"语音不能用"，但实测链路双向 7.4 万字节音频在传、麦克风轨已挂、
 * 播放也没被拦 —— 那就必须能区分"链路问题"与"扬声器/音量问题"。
 * 能听到 ⇒ 设备没问题；听不到 ⇒ 就是扬声器选择或音量。
 */
async function testSpeaker() {
  try {
    const ctx = new AudioContext();
    // 尽量输出到"设置里选的扬声器"
    if (state.spkDeviceId && typeof ctx.setSinkId === 'function') {
      try { await ctx.setSinkId(state.spkDeviceId); } catch (e) {
        log(`<span class="warn">测试音无法切到所选扬声器（${esc(e.message)}），改用系统默认</span>`);
      }
    }
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.value = 440;
    gain.gain.value = 0.18;
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 1.0);
    post({ type: 'speaker-test-started',
           sinkId: state.spkDeviceId || '(系统默认)',
           canSetSink: typeof ctx.setSinkId === 'function' });
    setTimeout(() => { try { ctx.close(); } catch (e) { } }, 1500);
    log('<span class="ok">已播放测试音（1 秒 440Hz）</span>');
  } catch (e) {
    post({ type: 'call-error', source: 'speaker-test', reason: `测试音失败: ${e.message}` });
  }
}

/** 把"设置页选的扬声器"应用上去（Chromium 支持 setSinkId；不支持就静默沿用默认设备）。 */
function applySpeaker() {
  if (!remoteAudioEl || !state.spkDeviceId) return;
  if (typeof remoteAudioEl.setSinkId !== 'function') {
    log('<span class="warn">本内核不支持选择扬声器，仍用系统默认设备</span>');
    // 【只报一次】否则每次挂音频都会重复上报，把日志刷满。
    if (!state._sinkUnsupportedReported) {
      state._sinkUnsupportedReported = true;
      post({ type: 'engine-error',
             message: '本内核不支持选择扬声器（setSinkId 不可用），设置里的扬声器选择不会生效，仍用系统默认设备' });
    }
    return;
  }
  remoteAudioEl.setSinkId(state.spkDeviceId)
    .then(() => log('已切到所选扬声器'))
    .catch(e => {
      // 切扬声器失败以前只写 DOM 日志 = 界面上"选了没反应"。必须上报。
      const msg = `切换扬声器失败: ${(e && e.message) || e}`;
      log(`<span class="bad">${esc(msg)}</span>`);
      post({ type: 'call-error', source: 'speaker', reason: msg });
    });
}

/** 把远端屏幕画面接上显示。 */
let remoteVideoEl = null;
let remoteFrameCount = 0;
let screenFrameLabel = null;

function attachRemoteVideo(stream) {
  if (!remoteVideoEl) {
    remoteVideoEl = document.createElement('video');
    remoteVideoEl.autoplay = true;
    remoteVideoEl.playsInline = true;
    remoteVideoEl.muted = true;   // 共享音频由 audio 元素负责，别放两遍
    remoteVideoEl.id = 'screenView';
    // 铺满视口：界面会把 WebView2 调大成一块显示区域
    remoteVideoEl.style.cssText =
      'position:fixed;inset:0;width:100%;height:100%;object-fit:contain;background:#000';
    document.body.appendChild(remoteVideoEl);
    document.body.style.margin = '0';
    document.body.style.padding = '0';

    // 左上角诊断标签：帧数 + 实际分辨率
    screenFrameLabel = document.createElement('div');
    screenFrameLabel.style.cssText =
      'position:fixed;left:10px;top:10px;color:#8CF;font:12px Consolas,monospace;' +
      'background:#000A;padding:3px 8px;border-radius:4px;z-index:10';
    screenFrameLabel.textContent = '等待画面…';
    document.body.appendChild(screenFrameLabel);
  }
  remoteVideoEl.srcObject = stream;
  remoteVideoEl.play().catch(e => log(`<span class="warn">画面播放被拦: ${e.message}</span>`));
  log('<span class="ok">远端屏幕画面已接上</span>');

  // 帧计数：这是"画面真的在传"的直接证据。
  // 光看到 track 到达不够 —— 编码失败或协商不对时 track 也会到，但一帧都没有。
  remoteFrameCount = 0;
  let lastReport = 0;
  const countFrame = (now) => {
    remoteFrameCount++;
    if (now - lastReport > 1000) {
      lastReport = now;
      // 在画面左上角显示帧数与分辨率。
      // 目的很实际：观看方"看不到东西"时，一眼就能分辨是
      //   · 帧数不动 → 没收到帧（传输/协商问题）
      //   · 帧数在涨但画面黑 → 收到了但没画出来（渲染/尺寸问题）
      // 没有这个数字，两种情况从外面看是一样的。
      if (screenFrameLabel) {
        screenFrameLabel.textContent =
          `${remoteFrameCount} 帧 · ${remoteVideoEl.videoWidth}×${remoteVideoEl.videoHeight}`;
      }
      post({ type: 'screen-frames', frames: remoteFrameCount,
             videoWidth: remoteVideoEl.videoWidth,
             videoHeight: remoteVideoEl.videoHeight });
    }
    remoteVideoEl.requestVideoFrameCallback(countFrame);
  };
  if (remoteVideoEl.requestVideoFrameCallback) {
    remoteVideoEl.requestVideoFrameCallback(countFrame);
  }
}

/** 隐藏画面并清掉元素（对端停止共享时）。 */
function detachRemoteVideo() {
  if (remoteVideoEl) {
    try { remoteVideoEl.pause(); } catch (e) { }
    remoteVideoEl.srcObject = null;
    // 必须真的从 DOM 移除：只清 srcObject 的话元素会停格显示最后一帧，
    // 用户看到的就是"断了共享但还有画面"。
    remoteVideoEl.remove();
    remoteVideoEl = null;
  }
  if (screenFrameLabel) {
    screenFrameLabel.remove();
    screenFrameLabel = null;
  }
  remoteFrameCount = 0;
  log('<span class="ok">已清除远端画面元素</span>（画面应回到纯黑）');
}

// ===========================================================================
// mesh（链路注册 + 建链/应答/挂断 + 聊天/共享声音轨）—— 已搬到 mesh.js（S3 收口）
// 这里只留薄访问器；mesh 闭包在模块末尾接线时建立（见 initSignaling）。
// 注意：reportLinks 不在此列 —— 它在更上面已有定义（共享声音那段也要用），保持原样。
// ===========================================================================
function registerLink(id, pc) { return mesh.registerLink(id, pc); }
function setLinkChat(id, chat) { return mesh.setLinkChat(id, chat); }
function unregisterLink(id) { return mesh.unregisterLink(id); }
function unregisterAllLinks() { return mesh.unregisterAllLinks(); }
function setupChatChannel(pc, isInitiator) { return mesh.setupChatChannel(pc, isInitiator); }
function bindChatChannel(dc) { return mesh.bindChatChannel(dc); }
function ensureLink(remoteId) { return mesh.ensureLink(remoteId); }
function callPeer(remoteId) { return mesh.callPeer(remoteId); }
function onOffer(msg) { return mesh.onOffer(msg); }
function onAnswer(msg) { return mesh.onAnswer(msg); }
function onRemoteIce(msg) { return mesh.onRemoteIce(msg); }
function hangup(reason) { return mesh.hangup(reason); }
function sendChat(text) { return mesh.sendChat(text); }

// ===========================================================================
// 连接质量统计
//
// 这是"量化验收"的关键：从 getStats() 取真实值，而不是靠感觉说"不卡"。
// ===========================================================================
// ===========================================================================
// 连接质量统计 —— 已搬到 stats.js（S3）
// 这里只留薄访问器（stats 闭包在模块末尾接线时建立）。
// ===========================================================================
function startStatsLoop() { stats.startStatsLoop(); }
function stopStatsLoop() { stats.stopStatsLoop(); }
function collectStats() { return stats.collectStats(); }

// ===========================================================================
// 对 C# 暴露的接口
// ===========================================================================
window.zxEngine = {
  /**
   * 测试扬声器（诊断）：播 1 秒 440Hz 到当前选定的扬声器。
   * 能听到 ⇒ 设备/音量没问题；听不到 ⇒ 问题就在扬声器选择或音量。
   */
  testSpeaker,


  /**
   * 下发 ICE（STUN/TURN）配置。三个入口是**同一个实现**，C# 任选其一：
   *   · window.zxIceConfig(cfg)            ← Lead 钉死的名字（含 zxIceConfigReady 就绪标志）
   *   · zxEngine.setIceConfig(cfg)         ← CallAsync("setIceConfig", cfg)
   *   · zxEngine.setTurnConfig({urls,username,credential})  ← CallAsync("setTurnConfig", ...)
   * 页面不写死任何 TURN 地址/账号；空配置 = 只用 STUN。
   */
  setIceConfig(cfg) { return window.zxIceConfig(cfg || {}); },
  setTurnConfig(cfg) { return window.zxIceConfig(cfg || {}); },

  /** 当前 ICE 配置概览（只报数量/类型，不回显账号密码）。 */
  iceConfigState() {
    const sum = summarizeIceServers(state.iceServers);
    const info = { ...sum, turnConfigured: state.iceConfigured,
                   ready: window.zxIceConfigReady === true };
    post({ type: 'ice-config-state', info });
    return info;
  },

  /** 启动：配好参数并连信令。C# 在窗口就绪后调用。 */
  async start(cfg) {
    try {
      state.selfName = cfg.selfName || '我';
      state.room = cfg.room || 'default';
      state.signalUrl = cfg.signalUrl;
      if (cfg.noiseSuppression !== undefined) state.noiseSuppression = cfg.noiseSuppression;
      if (cfg.echoCancellation !== undefined) state.echoCancellation = cfg.echoCancellation;
      if (cfg.autoGainControl !== undefined) state.autoGainControl = cfg.autoGainControl;

      // 【2026-10-07 说明·为什么这里**不**动旧连接】
      // 曾在此加 `state.ws.close()` 以消除"同一实例两个身份"的问题，但实测**有害**：
      // 旧 ws 的 onclose 会在新连接建立**之后**才回调，把刚建好的状态判成"信令断开"、
      // 链路被清空（守卫实测 `[链路] 注册表 0 条` ⇒ 文字发不出去）。
      // 正确做法：**重连统一走 join()**（它有完整的"先收旧、再连新"的顺序），
      // 见 C# 的 StartHostRoomAsync —— 建房/改名/切房间都改用 join。
      log(`启动 名称=${esc(state.selfName)} 房间=${esc(state.room)}`);
      // 先只连信令，不开麦克风 —— 麦克风等到真要通话时再开，
      // 避免用户一进界面就被占用设备（Windows 会亮麦克风指示）
      await connectSignal();
      post({ type: 'ready' });
    } catch (e) {
      log(`<span class="bad">启动失败: ${esc(e.message)}</span>`);
      post({ type: 'engine-error', message: String(e.message) });
    }
  },

  /**
   * 换一个信令地址/房间并重连（界面上「连接」按钮调用）。
   *
   * 为什么要有这个：在这之前**只能靠命令行 `--signal`** 才能加入别人的房间，
   * 界面上没有地方填地址 —— 用户没法用鼠标测双端/多人。
   *
   * 注意要先彻底收掉旧连接（信令 + 所有对端链路 + 成员表），
   * 否则会出现"连着旧房间"的幽灵状态：新房间里看到旧对端、或两边各连一半。
   */
  async join(cfg) {
    const url = String((cfg && cfg.signalUrl) || '').trim();
    const room = String((cfg && cfg.room) || state.room || 'default').trim() || 'default';
    if (!url) {
      post({ type: 'engine-error', message: '请先填写对方地址' });
      return;
    }
    // 【2026-10-07 根因修复·名字/成员栏错乱】
    // 这里以前**完全不碰 state.selfName**，而它的初值是空字符串 ⇒ 加入方拼信令 URL 时
    // `name=` 是空的 ⇒ 服务端 DisplayName 为空 ⇒ 对端只能用 **id 前 6 位**兜底显示
    //（用户截图里成员名显示成 `147148` 就是这个）。
    // 同时这也让"改名字"对加入方永远不生效。
    if (cfg && typeof cfg.selfName === 'string' && cfg.selfName.trim().length > 0) {
      state.selfName = cfg.selfName.trim();
    }
    if (!state.selfName) state.selfName = '我';   // 兜底：绝不发空名字出去
    try {
      log(`切换信令地址 → ${esc(url)}（房间 ${esc(room)} / 名字 ${esc(state.selfName)}）`);
      try { unregisterAllLinks(); } catch (e) { /* 旧链路清不干净也不能挡住重连 */ }
      try { state.peers.clear(); } catch (e) { }
      // 【不要在这里裸 close()】旧 ws 的 onclose 会 post signal-closed 造成"信令断开"误报。
      // connectSignal() 现在会先静默旧连接（摘掉所有回调）再连新的。
      state.signalUrl = url;
      state.room = room;
      await connectSignal();
      post({ type: 'joined', signalUrl: url, room: room });
    } catch (e) {
      log(`<span class="bad">连接失败: ${esc(e.message)}</span>`);
      post({ type: 'engine-error', message: String(e.message) });
    }
  },

  /**
   * 列出可用的麦克风/扬声器（设置页用）。
   * 注意：设备 label 只有在拿到过麦克风权限之后才有内容，所以第一次可能显示"未命名设备"。
   */
  async listDevices() {
    try {
      const all = await navigator.mediaDevices.enumerateDevices();
      // 【麦克风/扬声器 与「应用来源」必须分开】
      // 这里只出**本机音频设备**（浏览器能看到的 audioinput/audiooutput），并显式带 role，
      // 界面不必靠猜 kind 字符串去分组。
      // 「共享电脑声音」的应用来源是引擎（WASAPI 进程回环）枚举出来的，由 C# 单独出；
      // 页面**不把它混进 devices** —— 混在一起就会出现"选了个应用却被当成麦克风"。
      const roleOf = k => (k === 'audioinput' ? 'mic' : (k === 'audiooutput' ? 'speaker' : 'other'));
      const devices = all
        .filter(d => d.kind === 'audioinput' || d.kind === 'audiooutput')
        .map(d => ({
          role: roleOf(d.kind),
          kind: d.kind,
          id: d.deviceId,
          label: d.label || '(未命名设备)',
        }));
      const counts = { mic: 0, speaker: 0 };
      for (const d of devices) if (counts[d.role] !== undefined) counts[d.role]++;
      post({ type: 'devices', devices, counts, source: 'enumerateDevices',
             note: '只含本机麦克风/扬声器；应用来源由引擎枚举，不在本事件里' });
    } catch (e) {
      post({ type: 'engine-error', message: '列设备失败: ' + String((e && e.message) || e) });
    }
  },

  /** 选择麦克风/扬声器（设置页调用）。麦克风下次开麦生效；扬声器立刻生效。 */
  async setDevice(cfg) {
    const kind = cfg && cfg.kind;
    const id = (cfg && cfg.id) || '';
    if (kind === 'audioinput') {
      state.micDeviceId = id;
      log(`麦克风已选：${esc(cfg.label || id || '系统默认')}（下次开麦生效）`);
      post({ type: 'device-applied', role: 'mic', id, label: cfg.label || '' });
    } else if (kind === 'audiooutput') {
      state.spkDeviceId = id;
      log(`扬声器已选：${esc(cfg.label || id || '系统默认')}`);
      applySpeaker();
      post({ type: 'device-applied', role: 'speaker', id, label: cfg.label || '' });
    } else {
      // 【不许静默忽略】类型写错时以前什么都不做：界面上"选了没反应"，日志也没有。
      post({ type: 'engine-error',
             message: `未知的设备类型: ${String(kind)}（只接受 audioinput=麦克风 / audiooutput=扬声器；应用来源不在这里设置）` });
    }
  },

  /** 主动呼叫房间里第一个人（界面上的「发起通话」按钮）。 */
  async call() {
    const first = [...state.peers.values()][0];
    if (!first) {
      post({ type: 'engine-error', message: '房间里还没有其他人' });
      return;
    }
    await callPeer(first.id);
  },

  /**
   * 发送聊天消息。
   *
   * 走 DataChannel（端到端加密、不经服务器）。若链路还没建好，
   * 会自动建立 —— 用户不需要先打电话才能发消息。
   */
  async sendChat(arg) {
    const text = (arg && arg.text) || '';
    if (!text.trim()) return;

    // 链路没建好时先建（首次发消息会有一点延迟，之后就是即时）
    if (!primaryPc()) {
      const first = [...state.peers.values()][0];
      if (!first) {
        post({ type: 'chat-error', message: '房间里还没有其他人' });
        return;
      }
      try { await ensureLink(first.id); } catch (e) {
        post({ type: 'chat-error', message: '建立链路失败: ' + e.message });
        return;
      }
      // 等通道 open（最多 5 秒）
      const t0 = Date.now();
      while ((!primaryChat() || primaryChat().readyState !== 'open')
             && Date.now() - t0 < 5000) {
        await new Promise(r => setTimeout(r, 100));
      }
    }

    if (sendChat(text)) {
      // 只在本机显示；对端收到后会从 DataChannel 过来，不会重复
      post({ type: 'chat-message', text, at: Date.now(), from: 'self' });
    }
  },

  /** 查询聊天通道状态（供界面显示"可发送"与否）。 */
  chatState() {
    post({
      type: 'chat-state',
      ready: !!(primaryChat() && primaryChat().readyState === 'open'),
      hasLink: !!primaryPc(),
      peers: [...state.peers.values()].map(p => p.name || p.id),
    });
  },

  /** 挂断。 */
  async hangup() { await hangup('本机挂断'); },

  /** 静音开关。 */
  setMuted(arg) {
    state.muted = !!arg.muted;
    // 【2026-10-07 修复·"静音恢复后成员说不了话"】
    // 必须改**实际送出去的**那条轨道。开了降噪链时，addTrack 的是降噪链的输出轨
    //（localAudioChain.destination.stream），而以前这里只改 state.localStream 的轨 ——
    // 两者不是同一条 ⇒ 静音/取消静音对真实发送的轨道没作用（用户实测恢复不过来）。
    const sent = outgoingTrack();
    if (sent) {
      try { sent.enabled = !state.muted; } catch (e) { }
    }
    // localStream 的轨也一起改（没开降噪链时它就是发送轨；降噪链场景下改它能让源静音）
    if (state.localStream) {
      state.localStream.getAudioTracks().forEach(t => { t.enabled = !state.muted; });
    }
    // 【必须上报】静音直接改变 sending（track.enabled）⇒ 不重新上报的话，
    // 界面与"发音频"判定都停在旧值（实测：点了静音，[通话判定] 时间戳纹丝不动）。
    log(`<span class="k">静音=${state.muted}</span>（实际发送轨 ` +
        `${sent ? sent.readyState + '/enabled=' + sent.enabled : 'none'}）`);
    post({ type: 'muted', muted: state.muted });
    try { reportCallState(state.muted ? '已静音' : '已取消静音'); } catch (e) { }
  },

  /**
   * 调整降噪档位。
   *
   * 浏览器只给布尔的 noiseSuppression，所以三档是这样实现的：
   *   off     浏览器 NS 关、AGC 关
   *   normal  浏览器 NS 开、AGC 开、AEC 开（默认）
   *   strong  normal + Web Audio 侧 100 Hz 高通 + 动态范围压缩
   *
   * 切换时需要重建采集链（改约束对已有轨道不生效），所以：
   *   · 未通话时：直接重建，立即生效
   *   · 通话中：重建会中断音频，所以先告知 C# 询问用户，
   *     由界面决定是"立即重连"还是"下次通话生效"
   */
  async setDenoise(arg) {
    const level = arg.level;
    if (!['off', 'normal', 'strong'].includes(level)) return;

    state.denoiseLevel = level;
    state.noiseSuppression = level !== 'off';
    state.autoGainControl = level !== 'off';
    state.echoCancellation = true;

    if (state.inCall) {
      // 通话中切档位要重建轨道，界面应提示用户
      log(`<span class="warn">通话中切换降噪档位 → ${level}（需重连采集链）</span>`);
      post({ type: 'denoise-needs-reconnect', level });
      return;
    }

    post({ type: 'denoise-applied', level, applied: false });
    log(`降噪档位已设为 ${level}（下次开麦时生效）`);
  },

  /**
   * 立即取一次连接统计（界面定时调用，也用于诊断）。
   *
   * 为什么要单独暴露这个方法：定时器回调里的异常会被浏览器静默吞掉，
   * 排查"统计为什么不更新"时完全看不到线索。由 C# 主动触发一次，
   * 可以判定是"定时器死了"还是"取统计本身有问题"。
   */
  async statsNow() {
    if (!primaryPc()) {
      post({ type: 'stats-debug', hasPc: false, inCall: state.inCall,
             wsState: state.ws ? state.ws.readyState : -1 });
      return;
    }
    try {
      await collectStats();
      post({
        type: 'stats-debug',
        hasPc: true,
        inCall: state.inCall,
        tick: state._tick || 0,
        timerAlive: state.statsTimer !== null,
        connState: primaryPc().connectionState,
        iceState: primaryPc() ? primaryPc().iceConnectionState : 'none',
      });
    } catch (e) {
      post({ type: 'stats-debug', error: String(e && e.message ? e.message : e) });
    }
  },

  /**
   * 麦克风录音自检：录一段并存成 WAV 交给 C# 落盘。
   *
   * 【为什么需要它】
   * 单机双实例测出来的"音质差"里混着声学回环伪影（扬声器的声音被麦克风
   * 收回去），无法用来判断真实音质。而录下**本地麦克风经过处理链之后**
   * 的音频，就能在同一台机器上客观对比不同降噪档位的差别：
   * 听文件、看频谱、比噪声底，都不依赖对端。
   *
   * 参数 seconds 由 C# 指定；处理链按当前档位生效，所以切换档位后各录一次
   * 就能直接比较。
   */
  async recordSample(arg) {
    // 实现已搬到 voice.js（S3：语音相关聚在一处）
    await voiceRecordSample(arg || {}, {
      post, log, esc, state, openMic, audioChain: () => localAudioChain,
    });
  },

  /**
   * 信号链验证：用**合成正弦波**跑一遍"编码 → 解码"，检查增益与频响。
   *
   * 【这个测试能证明什么、不能证明什么】
   * 能：编码/解码链路是通的、增益没有明显偏差、频率没有被异常衰减或放大。
   *     这些是"明显 bug"的检查，可脚本化、无需人耳、无需真实麦克风环境。
   * 不能：判断实际听感。真实音质取决于麦克风、房间、扬声器、网络抖动，
   *     以及主观偏好 —— 那些必须由人在真实通话里判断。
   *
   * 之所以要做：把"能自动验证的部分"和"必须人工判断的部分"分开。
   * 前者进 CI，后者才需要每次找人试听。
   */
  async signalChainTest(arg) {
    // 实现已搬到 selfcheck.js（S3 第 4 步）：页面被 C# 调用的接口保持薄。
    await selfcheckSignalChainTest(arg || {}, { post, log, esc });
  },

  /** 屏幕共享：开始。C# 在用户点「共享屏幕」时调用。 */
  async startScreenShare(arg) {
    await startScreenShare(arg || {});
  },

  /**
   * zxEngine 方法名自检：确认 C# 里写的方法名都真的存在于页面上。
   *
   * 为什么需要：C# 调 CallAsync 时，如果方法名拼错，页面里的
   * try/catch 会把它报成 js-error，但如果连 postMessage 都没生效，
   * 就完全没有痕迹。有一个显式的清单可以避免"以为调了其实没调"。
   */
  listMethods() {
    const names = Object.keys(window.zxEngine || {});
    post({ type: 'engine-methods', methods: names });
    return names;
  },

  /**
   * 发送文件。C# 把文件内容读成 base64 传进来。
   *
   * 为什么由 C# 读文件：WebView2 页面没有文件系统访问权限（这是浏览器的
   * 安全边界，不该绕过）。C# 读盘 → base64 → 页面转二进制 → DataChannel。
   * base64 有 33% 膨胀，对几百 MB 的文件要注意；超大文件应该改走
   * transfer.py 的多流 TCP 路径。
   */
  async sendFile(arg) {
    await sendFile(arg || {});
  },

  /** 屏幕共享：停止。 */
  /**
   * 开始「共享电脑声音」。
   *
   * 【独立功能】与屏幕共享、麦克风语音互不依赖：自己一条音轨、自己一个开关。
   * 采集在应用进程（引擎 WASAPI），这里只负责把推过来的 PCM 变成真正的音轨。
   */
  async startSharedAudio() {
    const track = await ensureSharedAudioTrack();
    if (!track) {
      // ensureSharedAudioTrack 内部已发过 share-audio-error；这里再补一条"开始失败"，
      // 保证界面一定看得到原因，而不是只靠返回值（C# 可能只看事件）。
      post({ type: 'share-audio-error', phase: 'start', message: '共享声音开启失败：音轨创建失败' });
      log('<span class="bad">共享电脑声音开启失败：音轨创建失败</span>');
      return { ok: false, error: 'track-create-failed' };
    }
    // 【A1 修复】对**已存在**的链路：挂轨 + **重协商**。
    // 只 addTrack 不会生成 m-line —— 实测那会让该链路 getStats() 里一条 outbound-rtp 都没有，
    // RTP 恒 0 字节，对端完全听不到（这正是用户报的"音频共享完全失效"）。
    let renegotiated = 0;
    for (const [lid, l] of links) {
      try { renegotiated += await mesh.attachAndRenegotiateSharedAudio(lid, l); } catch (e) { /* 已记录 */ }
    }
    if (renegotiated > 0) log(`<span class="k">共享声音：${renegotiated} 条已存在链路完成重协商</span>`);
    // 【挂载不全必须上报】轨在、链路在，但某条链路没挂上 = 那一位对端完全听不到。
    // 以前这里只报 started，看上去"成功"。
    const attached = state.sharedAudio.sent.length;
    if (links.size > 0 && attached < links.size) {
      const msg = `共享声音已开启，但只有 ${attached}/${links.size} 条链路挂上了音轨` +
                  `（其余链路对端听不到共享声音）`;
      log(`<span class="warn">${esc(msg)}</span>`);
      post({ type: 'share-audio-error', phase: 'start', message: msg,
             links: links.size, attached });
    }
    post({ type: 'share-audio-started', links: links.size, sent: attached });
    log(`<span class="ok">共享电脑声音已开始</span>（独立音轨，链路 ${links.size} 条，已挂 ${attached} 条）`);
    return { ok: true, links: attached };
  },

  /** 推一块 PCM：base64 编码的 float32 / 48000 / 2 声道交叉。应用侧按块调用。 */
  pushSharedAudio(b64) {
    const s = state.sharedAudio;
    if (!s || !b64) return false;
    try {
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; ++i) bytes[i] = bin.charCodeAt(i);
      const samples = new Float32Array(bytes.buffer, 0, bytes.length >> 2);
      s.node.port.postMessage({ type: 'pcm', samples }, [samples.buffer]);
      s.pushed = (s.pushed || 0) + 1;
      s.pushedSamples = (s.pushedSamples || 0) + samples.length;
      // 每 20 块要一次 worklet 回执（不频繁，避免给音频线程添负担）
      if (s.pushed % 20 === 0) { try { s.node.port.postMessage({ type: 'stats' }); } catch (e) { } }
      return true;
    } catch (e) {
      // 【A1：不许静默】原来这里只 return false，用户看到的是"共享了但对方听不到"，
      // 而应用日志里**一行原因都没有**。
      const msg = `pushSharedAudio 失败: ${(e && e.message) || e}`;
      log(`<span class="bad">${esc(msg)}</span>`);
      post({ type: 'share-audio-push-error', message: msg, pushed: s.pushed || 0 });
      return false;
    }
  },

  /** 结束共享电脑声音。 */
  /**
   * 共享电脑声音的自检出口：把"链路几条、音轨加到几条、发送端有没有这条轨"
   * 一次性报出来。发送端到底有没有在发，只有这里能看清（接收端只看得到结果）。
   */
  async shareAudioDebug() {
    // 实现已搬到 selfcheck.js（S3 第 4 步）。
    await selfcheckShareAudioDebug({ post, log, esc, state, links, primaryId });
  },

  stopSharedAudio() {
    const s = state.sharedAudio;
    // 【必须同时复位 promise 缓存】否则下次 ensureSharedAudioTrack() 会命中旧 promise，
    // 返回**已关闭 AudioContext 里的死音轨**：接口返回 ok、attach 也照发，但对方永远听不到
    // —— "停一次共享 → 之后永久静音" 的静默故障（2026-10-06 审计发现）。
    sharedAudioTrackPromise = null;
    if (!s) { post({ type: 'share-audio-stopped', ok: true, removed: 0, failed: 0 }); return { ok: true }; }

    // 【清理失败不许静默】removeTrack/close 的异常以前被空 catch 吃掉。
    let removed = 0, failed = 0, closeErr = '';
    for (const it of s.sent) {
      try { it.pc.removeTrack(it.sender); removed++; }
      catch (e) { failed++; }
    }
    try { s.node.disconnect(); } catch (e) { closeErr = String((e && e.message) || e); }
    try { s.ctx.close(); } catch (e) { closeErr = String((e && e.message) || e); }
    state.sharedAudio = null;
    if (failed || closeErr) {
      const detail = `停止共享声音时清理不完全：removeTrack 失败 ${failed} 条` +
                     (closeErr ? `；关闭 AudioContext 出错 ${closeErr}` : '');
      log(`<span class="warn">${esc(detail)}</span>`);
      post({ type: 'share-audio-error', phase: 'stop', message: detail });
    } else {
      log('<span class="warn">共享电脑声音已停止</span>（promise 缓存已复位，可以再次开启）');
    }
    post({ type: 'share-audio-stopped', ok: failed === 0 && !closeErr,
           removed, failed, error: closeErr });

    // 【复核复位】下次 ensureSharedAudioTrack() 必须重新建轨；若状态没清干净就自己修好并报出来，
    // 这正是"停一次共享永久静音"的根因，不能只靠注释保证。
    const resetOk = sharedAudioTrackPromise === null && state.sharedAudio === null;
    if (!resetOk) {
      sharedAudioTrackPromise = null;
      state.sharedAudio = null;
      post({ type: 'share-audio-error', phase: 'stop',
             message: '内部状态未复位（已强制复位；否则下次开启共享后对方会永久听不到）' });
    }
    return { ok: failed === 0 && !closeErr, removed, failed, resetOk };
  },

  stopScreenShare() {
    stopScreenShare('本机停止');
  },

  /**
   * 显示区域尺寸变化时通知页面。
   *
   * WebView2 平时是 0×0 的隐藏媒体宿主；共享屏幕时 C# 会把它放大成
   * 一块真正的显示区域。页面里的 <video> 用的是 position:fixed + 100%，
   * 会自动跟随，这里只需要记下尺寸用于诊断与自适应。
   */
  setViewport(arg) {
    state.viewport = { w: arg.w, h: arg.h };
    log(`显示区域: ${arg.w}×${arg.h}` + (arg.visible ? '（可见）' : '（隐藏）'));
    if (remoteVideoEl) {
      // 让画面按比例铺满，不拉伸变形
      remoteVideoEl.style.objectFit = 'contain';
    }
  },

  /**
   * 屏幕捕获能力探针。
   *
   * 与 startScreenShare 的区别：这里**不**把流接进 PeerConnection，
   * 只回答一个问题 —— 宿主任不任我调 getDisplayMedia。
   *
   * 为什么需要单独探针：WebView2 默认会拒绝屏幕捕获（走
   * CoreWebView2.ScreenCaptureStarting 事件）。默认拒绝的表现是
   * 整个调用卡住、既不成功也不报错，看起来像"点了没反应"。
   * 用一个只测不用的探针就能把"宿主权限"与"传输链路"两件事分开。
   */
  async probeScreenCapture(arg) {
    const timeoutMs = (arg && arg.timeoutMs) || 12000;
    const result = { type: 'screen-probe' };

    try {
      result.hasApi = !!(navigator.mediaDevices &&
                         navigator.mediaDevices.getDisplayMedia);
      if (!result.hasApi) {
        result.ok = false;
        result.error = 'getDisplayMedia 不存在';
        post(result);
        return;
      }

      // 超时保护：宿主默认拒绝时调用会一直挂着，不给超时就会永远等
      const stream = await Promise.race([
        navigator.mediaDevices.getDisplayMedia({ video: true, audio: false }),
        new Promise((_, rej) =>
          setTimeout(() => rej(new Error(`超时 ${timeoutMs}ms —— 宿主未放行也未报错`)),
                     timeoutMs)),
      ]);

      const t = stream.getVideoTracks()[0];
      const st = t.getSettings ? t.getSettings() : {};
      result.ok = true;
      result.width = st.width || null;
      result.height = st.height || null;
      result.frameRate = st.frameRate || null;
      result.displaySurface = st.displaySurface || '';
      result.label = t.label || '';

      // 立刻释放：探针不该占着用户的屏幕
      stream.getTracks().forEach(x => x.stop());
      log(`<span class="ok">屏幕捕获探针成功</span> ` +
          `${result.width}x${result.height} ${result.displaySurface}`);
    } catch (e) {
      result.ok = false;
      result.error = `${e.name}: ${e.message}`;
      log(`<span class="bad">屏幕捕获探针失败: ${esc(result.error)}</span>`);
    }
    post(result);
  },

  /** 停止一切（窗口关闭 / 页面卸载时调用）。 */
  stopAll() {
    // 【S4 方案 A】先让模块宿主统一释放（每个模块自己的 dispose 是真源：stats 停统计、
    // voice 停电平表、screen 停采集、mesh 关链路、signaling 关 ws、page-resources 收流）。
    // 为什么放最前：模块释放是"资源回收"，下面是"业务停机"；先回收再收尾，
    // 避免释放途中的回调打到已经停了一半的对象上。下面几行保留为兼容兜底（宿主未接线时）。
    if (moduleHost) moduleHost.disposeAll('stopAll');
    stopStatsLoop();                             // 兼容兜底：轮询真的停（清 timer + busy 标志）
    stopMicMeter();                              // 兼容兜底：电平表轮询真的停
    stopScreenShare('停机');      // S3：停采集 + 摘掉所有链路的屏幕轨 + 上报
    detachRemoteVideo();
    // 【共享声音也必须收】否则 AudioContext 与 worklet 还活着：麦克风/扬声器指示灯
    // 不灭、下次开启共享还会命中旧状态。这里走同一套停止逻辑（含 promise 复位复核）。
    if (state.sharedAudio || sharedAudioTrackPromise) {
      try { window.zxEngine.stopSharedAudio(); } catch (e) { }
    }
    try { if (state.ws) state.ws.close(); } catch (e) { }
    if (state.localStream) state.localStream.getTracks().forEach(t => t.stop());
    links.clear('停机');                          // 幽灵链路也一起收（S3 第 2 步）
    unregisterAllLinks();
    adoptPrimaryAliases();
    state.localStream = null;
    state.ws = null;
  },

  /**
   * 麦克风自检：打开麦克风，读回**实际生效**的降噪参数，然后立刻关掉。
   *
   * 为什么必须读回实际值：请求 noiseSuppression:true **不等于**它生效。
   * 浏览器可能忽略该约束、报告 false，或底层设备不支持。
   * 所以这里报的是请求值与生效值的对照，而不是"我请求了什么"。
   */
  async selfCheck() {
    log('<span class="k">开始麦克风自检…</span>');
    let stream = null;
    try {
      stream = await openMic();
      const track = stream.getAudioTracks()[0];
      const st = track.getSettings ? track.getSettings() : {};

      // 顺便测 1.5 秒电平，区分"拿到轨道"与"轨道真的有声音"
      let maxDb = -Infinity;
      try {
        const ctx = new AudioContext();
        const src = ctx.createMediaStreamSource(stream);
        const an = ctx.createAnalyser();
        an.fftSize = 1024;
        src.connect(an);
        const buf = new Float32Array(an.fftSize);
        const t0 = performance.now();
        await new Promise(resolve => {
          const tick = () => {
            an.getFloatTimeDomainData(buf);
            let sum = 0;
            for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
            const db = 20 * Math.log10(Math.max(Math.sqrt(sum / buf.length), 1e-7));
            if (db > maxDb) maxDb = db;
            if (performance.now() - t0 < 1500) requestAnimationFrame(tick);
            else resolve();
          };
          tick();
        });
        await ctx.close();
      } catch (e) {
        log(`<span class="warn">电平检测失败: ${esc(e.message)}</span>`);
      }

      post({
        type: 'selfcheck',
        ok: st.noiseSuppression === true && st.echoCancellation === true,
        applied: {
          label: track.label,
          echoCancellation: st.echoCancellation,
          noiseSuppression: st.noiseSuppression,
          autoGainControl: st.autoGainControl,
          sampleRate: st.sampleRate,
          channelCount: st.channelCount,
        },
        maxDbF: isFinite(maxDb) ? maxDb : null,
      });
      log(`${ok(st.noiseSuppression === true)} 自检完成 电平=${maxDb.toFixed(1)} dBFS`);
    } catch (e) {
      post({ type: 'selfcheck', ok: false, error: `${e.name}: ${e.message}` });
      log(`<span class="bad">自检失败: ${esc(e.name)} — ${esc(e.message)}</span>`);
    } finally {
      // 自检不留占用：立刻释放麦克风
      if (stream) stream.getTracks().forEach(t => t.stop());
    }
  },
};

// ===========================================================================
// 【页面卸载：所有轮询/定时器必须真的停】
// 离开房间走 hangup()，页面卸载走这里。两条路都要收干净 —— 否则 setInterval
// 会在 WebView2 复用页面时继续跑（统计/电平表"看起来还在动"），
// AudioContext 也不会释放。
// ===========================================================================
function onPageGone(why) {
  try {
    log(`<span class="warn">页面${why}：停止全部轮询与媒体</span>`);
    // 【S4 方案 A】先让每个模块自己释放（定时器/采集/流），再走 stopAll 的业务收尾。
    // 为什么模块优先：模块知道自己持有什么；stopAll 是"业务上停机"，两者互补。
    if (moduleHost) moduleHost.disposeAll(why);
    window.zxEngine.stopAll();
  } catch (e) { /* 卸载路径不许再抛异常 */ }
}
window.addEventListener('pagehide', () => onPageGone('pagehide'));
window.addEventListener('beforeunload', () => onPageGone('beforeunload'));

// 【接线：模块宿主】放在模块末尾 —— 这时所有 function 声明（含 mesh 的 ensureLink/onOffer/
// hangup 与视图层的 attachRemoteVideo）都已就绪，注入不会有"访问到未初始化"的风险。
// 【为什么不放在文件顶部】signalIo() 会引用 links 等 const；在顶部调用会踩 TDZ
// ⇒ 抛 ReferenceError ⇒ 后面的 post(engine-loaded) 永远发不出去。
// 【为什么包 try/catch 并 post】模块求值期抛异常时，页面什么都不发（window.onerror 也抓不到
// 模块图解析失败），C# 只看到"未上报 engine-loaded"。这条让原因可见。
try {
  wireModules();
} catch (e) {
  post({ type: 'link-error', peer: '(init)', message: `initSignaling 失败: ${(e && e.message) || e}`,
         stack: String((e && e.stack) || '').split('\n').slice(0, 3).join(' | ') });
}

post({ type: 'engine-loaded' });
log('<span class="k">通话引擎已加载</span>，等待 C# 下发启动参数…');
log(`ICE 就绪标志 zxIceConfigReady=${String(window.zxIceConfigReady)}（C# 可据此下发 TURN）`);
