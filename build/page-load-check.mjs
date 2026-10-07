// tmp/esm-harness.mjs —— 用 Node 当假浏览器加载页面的模块，直接拿到加载期异常。
//
// 为什么需要：页面改成 ES module 之后，一旦加载期抛异常，C# 只能看到
// "engine-loaded 超时"（错误监听器在 log.js 里，可能都还没装上）。
// 在 Node 里 import 一次就能拿到**原始堆栈**，比反复跑 GUI 快几十倍。
//
// 只桩掉页面真正需要的东西（DOM/桥/计时器/WebRTC 的最小面具）。
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const MEDIA = path.resolve(process.argv[2] || 'src/winui-cs/media');
const posts = [];

function makeEl(tag = 'div') {
  const el = {
    tagName: tag, style: {}, children: [], textContent: '', innerHTML: '',
    scrollTop: 0, scrollHeight: 0, value: '', checked: false, disabled: false,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute() {}, getAttribute: () => null, removeAttribute() {},
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) { return c; }, remove() {},
    addEventListener() {}, removeEventListener() {},
    querySelector: () => null, querySelectorAll: () => [],
    getContext: () => ({
      fillRect() {}, arc() {}, fill() {}, fillText() {}, beginPath() {},
      clearRect() {}, drawImage() {}, strokeText() {}, set fillStyle(v) {}, get fillStyle() { return ''; },
      set font(v) {}, get font() { return ''; },
    }),
    captureStream: () => ({ getTracks: () => [], getVideoTracks: () => [], getAudioTracks: () => [] }),
    getBoundingClientRect: () => ({ x: 0, y: 0, width: 100, height: 20, top: 0, left: 0 }),
    focus() {}, click() {}, play: () => Promise.resolve(), pause() {},
  };
  return el;
}

const documentStub = {
  getElementById: () => makeEl(),
  createElement: (t) => makeEl(t),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
  removeEventListener() {},
  body: makeEl('body'),
  documentElement: makeEl('html'),
  visibilityState: 'visible',
};

class FakeTrack {
  constructor(kind) { this.kind = kind; this.enabled = true; this.readyState = 'live'; }
  stop() { this.readyState = 'ended'; }
  addEventListener() {}
}
class FakeStream {
  constructor(tracks = []) { this._t = tracks; }
  getTracks() { return this._t; }
  getAudioTracks() { return this._t.filter(t => t.kind === 'audio'); }
  getVideoTracks() { return this._t.filter(t => t.kind === 'video'); }
  addTrack(t) { this._t.push(t); }
  removeTrack(t) { this._t = this._t.filter(x => x !== t); }
}
class FakePC {
  constructor(cfg) { this.cfg = cfg; this.localDescription = null; this.connectionState = 'new'; }
  addTrack() {} removeTrack() {} getSenders() { return []; } getReceivers() { return []; }
  createOffer() { return Promise.resolve({ type: 'offer', sdp: 'v=0' }); }
  createAnswer() { return Promise.resolve({ type: 'answer', sdp: 'v=0' }); }
  setLocalDescription() { return Promise.resolve(); }
  setRemoteDescription() { return Promise.resolve(); }
  setConfiguration() {}
  close() { this.connectionState = 'closed'; }
  getStats() { return Promise.resolve(new Map()); }
  addEventListener() {}
  addIceCandidate() { return Promise.resolve(); }
  createDataChannel() { return { readyState: 'open', send() {}, close() {}, addEventListener() {}, onmessage: null, onopen: null, onclose: null }; }
}

const windowStub = {
  chrome: { webview: { postMessage: (s) => posts.push(s), addEventListener() {} } },
  addEventListener() {}, removeEventListener() {},
  setTimeout: (fn, ms, ...a) => setTimeout(fn, ms, ...a),
  clearTimeout, setInterval: (fn, ms, ...a) => setInterval(fn, ms, ...a), clearInterval,
  requestAnimationFrame: (fn) => setTimeout(() => fn(Date.now()), 16),
  cancelAnimationFrame: clearTimeout,
  document: documentStub,
  location: { href: 'http://127.0.0.1:46111/call.html', search: '' },
  navigator: {
    mediaDevices: {
      enumerateDevices: async () => [],
      getUserMedia: async () => new FakeStream([new FakeTrack('audio')]),
      getDisplayMedia: async () => new FakeStream([new FakeTrack('video')]),
      addEventListener() {},
    },
    userAgent: 'node-harness',
  },
  RTCPeerConnection: FakePC,
  webkitRTCPeerConnection: FakePC,
  MediaStream: FakeStream,
  AudioContext: class {
    constructor() { this.state = 'running'; this.destination = {}; this.sampleRate = 48000; }
    createMediaStreamSource() { return { connect() {} }; }
    createGain() { return { connect() {}, gain: { value: 1 } }; }
    createAnalyser() { return { connect() {}, fftSize: 2048, getFloatTimeDomainData() {}, frequencyBinCount: 1024, getByteFrequencyData() {} }; }
    createScriptProcessor() { return { connect() {}, disconnect() {}, onaudioprocess: null }; }
    createMediaStreamDestination() { return { stream: new FakeStream() }; }
    close() { return Promise.resolve(); }
    resume() { return Promise.resolve(); }
  },
  AudioWorkletNode: class { constructor() { this.port = { postMessage() {}, onmessage: null, start() {} }; } connect() {} disconnect() {} },
  WebSocket: class {
    constructor(url) { this.url = url; this.readyState = 1; setTimeout(() => this.onopen && this.onopen({}), 0); }
    send() {} close() {}
  },
  performance: { now: () => Date.now() },
  isSecureContext: true,
  zxIceConfigReady: false,
};
windowStub.window = windowStub;

globalThis.window = windowStub;
globalThis.document = documentStub;
// Node 24 的 globalThis.navigator 只有 getter，直接赋值会抛 TypeError ⇒ 用 defineProperty
Object.defineProperty(globalThis, 'navigator', {
  value: windowStub.navigator, configurable: true, writable: true,
});
globalThis.location = windowStub.location;
globalThis.RTCPeerConnection = FakePC;
globalThis.MediaStream = FakeStream;
globalThis.AudioContext = windowStub.AudioContext;
globalThis.AudioWorkletNode = windowStub.AudioWorkletNode;
globalThis.WebSocket = windowStub.WebSocket;
globalThis.requestAnimationFrame = windowStub.requestAnimationFrame;
globalThis.cancelAnimationFrame = windowStub.cancelAnimationFrame;
globalThis.isSecureContext = true;

process.on('unhandledRejection', (e) => {
  console.log('[unhandledRejection]', e && e.stack ? e.stack.split('\n').slice(0, 4).join('\n') : e);
});

try {
  await import(pathToFileURL(path.join(MEDIA, 'call.js')).href);
  await new Promise(r => setTimeout(r, 300));
  console.log('模块加载成功');
  console.log('window.zxEngine 存在:', typeof windowStub.zxEngine);
  if (windowStub.zxEngine) {
    console.log('zxEngine 方法数:', Object.keys(windowStub.zxEngine).length);
  }
  const evs = posts.map(p => { try { return JSON.parse(p).type; } catch { return '?'; } });
  console.log('页面发出的事件:', evs.join(', ') || '(无)');
} catch (e) {
  console.log('模块加载失败:');
  console.log(e && e.stack ? e.stack.split('\n').slice(0, 12).join('\n') : String(e));
  process.exit(1);
}
