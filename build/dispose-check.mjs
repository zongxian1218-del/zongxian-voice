import { pathToFileURL } from 'node:url';
import path from 'node:path';
const MEDIA = path.resolve(process.argv[2] || 'src/winui-cs/media');

// 复用页面自检 harness 的面具（最小化）
const posts = [];
function el() {
  const e = { style: {}, children: [], textContent: '', innerHTML: '', scrollTop: 0, scrollHeight: 0,
    classList: { add(){}, remove(){}, toggle(){}, contains: () => false },
    setAttribute(){}, getAttribute: () => null, appendChild(c){ return c; }, remove(){},
    addEventListener(){}, removeEventListener(){}, querySelector: () => null, querySelectorAll: () => [],
    getContext: () => ({ fillRect(){}, arc(){}, fill(){}, fillText(){}, beginPath(){}, clearRect(){}, drawImage(){} }),
    captureStream: () => ({ getTracks: () => [], getVideoTracks: () => [], getAudioTracks: () => [] }),
    getBoundingClientRect: () => ({ x:0,y:0,width:100,height:20,top:0,left:0 }), focus(){}, click(){},
    play: () => Promise.resolve(), pause(){} };
  return e;
}
const doc = { getElementById: () => el(), createElement: () => el(), querySelector: () => null,
  querySelectorAll: () => [], addEventListener(){}, body: el(), documentElement: el(), visibilityState: 'visible' };
class S { constructor(t=[]){ this._t=t; } getTracks(){ return this._t; } getAudioTracks(){ return []; } getVideoTracks(){ return []; } }
class PC { constructor(){ this.connectionState='new'; } addTrack(){ return {}; } removeTrack(){} getSenders(){ return []; }
  getReceivers(){ return []; } getTransceivers(){ return []; } createOffer(){ return Promise.resolve({type:'offer',sdp:'v=0'}); }
  createAnswer(){ return Promise.resolve({type:'answer',sdp:'v=0'}); } setLocalDescription(){ return Promise.resolve(); }
  setRemoteDescription(){ return Promise.resolve(); } setConfiguration(){} close(){} getStats(){ return Promise.resolve(new Map()); }
  addEventListener(){} createDataChannel(){ return { readyState:'open', send(){}, close(){}, addEventListener(){} }; } }
const W = { chrome: { webview: { postMessage: s => posts.push(s), addEventListener(){} } },
  addEventListener(){}, removeEventListener(){}, setTimeout, clearTimeout, setInterval, clearInterval,
  requestAnimationFrame: fn => setTimeout(() => fn(Date.now()), 16), cancelAnimationFrame: clearTimeout,
  document: doc, location: { href:'http://127.0.0.1:1/call.html', search:'' },
  navigator: { mediaDevices: { enumerateDevices: async () => [], getUserMedia: async () => new S(), getDisplayMedia: async () => new S(), addEventListener(){} }, userAgent:'h' },
  RTCPeerConnection: PC, MediaStream: S, WebSocket: class { constructor(){ this.readyState=1; setTimeout(()=>this.onopen&&this.onopen({}),0);} send(){} close(){} },
  AudioContext: class { constructor(){ this.state='running'; this.destination={}; this.sampleRate=48000; this.audioWorklet={ addModule: async () => {} }; } createMediaStreamSource(){ return { connect(){} }; }
    createGain(){ return { connect(){}, gain:{value:1} }; } createAnalyser(){ return { connect(){}, fftSize:1024, getFloatTimeDomainData(){}, frequencyBinCount:512, getByteFrequencyData(){} }; }
    createScriptProcessor(){ return { connect(){}, disconnect(){} }; } createMediaStreamDestination(){ return { stream:new S() }; }
    close(){ return Promise.resolve(); } },
  AudioWorkletNode: class { constructor(){ this.port={ postMessage(){}, onmessage:null }; } connect(){} },
  performance: { now: () => Date.now() }, isSecureContext: true };
W.window = W;
globalThis.window = W; globalThis.document = doc;
Object.defineProperty(globalThis, 'navigator', { value: W.navigator, configurable: true });
globalThis.RTCPeerConnection = PC; globalThis.MediaStream = S; globalThis.AudioContext = W.AudioContext;
globalThis.AudioWorkletNode = W.AudioWorkletNode; globalThis.WebSocket = W.WebSocket;
globalThis.requestAnimationFrame = W.requestAnimationFrame; globalThis.cancelAnimationFrame = W.cancelAnimationFrame;
globalThis.isSecureContext = true;
process.on('unhandledRejection', () => {});

await import(pathToFileURL(path.join(MEDIA, 'call.js')).href);
await new Promise(r => setTimeout(r, 200));
const before = posts.map(p => { try { return JSON.parse(p).type; } catch { return '?'; } });
console.log('接线事件:', before.filter(t => t.startsWith('module')).join(', ') || '(无)');
try { window.zxEngine.stopAll(); } catch (e) { console.log('stopAll 抛错:', e.message); }
await new Promise(r => setTimeout(r, 200));
const after = posts.map(p => { try { return JSON.parse(p); } catch { return null; } }).filter(Boolean);
const disposed = after.filter(o => o.type === 'modules-disposed');
if (disposed.length === 0) { console.log('FAIL: stopAll 后没有 modules-disposed 事件'); process.exit(1); }
console.log('PASS: modules-disposed =', JSON.stringify(disposed[0]));
