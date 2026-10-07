import { encodeWav, arrayBufferToBase64 } from './util.js';

// ===========================================================================
// voice.js —— 语音相关的小模块集合（S3 第 3 步开始：按模块切开 call.js）
//
// 目前只有"麦克风电平表"：它是**用户侧第一诊断**（"对方听不到我"时要先判断本机到底
// 有没有拾到音，而不是先怀疑网络/编码），也是页面里最独立的一块。
// 后续 font 继续把 降噪档位 / Opus 质量 / 录音辅助 搬进来（见 docs 的 S3 计划）。
//
// 依赖注入：post/log/esc 由调用方传入（模块不直接 import call.js，避免循环依赖）。
// ===========================================================================

let micMeterCtx = null;
let micMeterTimer = null;

/**
 * 启动麦克风电平表。isMuted() 用于上报时带上"当前是否静音"。
 * @param {MediaStream} stream
 * @param {{post:Function, log:Function, esc:Function, isMuted:Function}} io
 */
export function startMicMeter(stream, io) {
  stopMicMeter();
  if (!stream) return;
  const { post, log, esc, isMuted } = io;
  try {
    micMeterCtx = new AudioContext();
    const src = micMeterCtx.createMediaStreamSource(stream);
    const an = micMeterCtx.createAnalyser();
    an.fftSize = 1024;
    src.connect(an);

    const buf = new Float32Array(an.fftSize);
    let lastPost = 0;

    micMeterTimer = setInterval(() => {
      // 【回调异常不许被静默吞掉】原来这里没有 try/catch：一旦抛异常，电平表就停在
      // 最后一个值上不动，看起来像"麦克风没声音"，其实只是循环死了。
      try {
        an.getFloatTimeDomainData(buf);
        let peak = 0, sum = 0;
        for (let i = 0; i < buf.length; i++) {
          const a = Math.abs(buf[i]);
          if (a > peak) peak = a;
          sum += buf[i] * buf[i];
        }
        const rms = Math.sqrt(sum / buf.length);
        const db = 20 * Math.log10(Math.max(rms, 1e-7));

        // 每 250ms 上报一次即可：更频繁只会让界面闪烁、徒增消息量
        const now = performance.now();
        if (now - lastPost >= 250) {
          lastPost = now;
          post({
            type: 'mic-level',
            dbfs: db,
            peakDbfs: 20 * Math.log10(Math.max(peak, 1e-7)),
            clipped: peak >= 0.999,
            muted: isMuted(),
          });
        }
      } catch (e) {
        const msg = String((e && e.message) || e);
        log(`<span class="bad">[电平表异常] ${esc(msg)}</span>（已停止电平表，避免显示过期数值）`);
        post({ type: 'scriptError', method: 'micMeter', message: msg });
        stopMicMeter();
      }
    }, 100);
  } catch (e) {
    log(`<span class="warn">电平表启动失败: ${esc(e.message)}</span>`);
  }
}

/** 停止并释放（幂等）。 */
export function stopMicMeter() {
  if (micMeterTimer) { clearInterval(micMeterTimer); micMeterTimer = null; }
  if (micMeterCtx) { try { micMeterCtx.close(); } catch (e) { } micMeterCtx = null; }
}

/** 是否正在跑（测试/诊断用）。 */
export function micMeterRunning() { return micMeterTimer !== null; }

/**
 * 录音自检：录 N 秒音频（"处理之后"的信号）并回传 WAV 给 C# 落盘。
 *
 * 依赖通过 io 注入：openMic（开麦）、audioChain()（当前软件降噪链，可能是 null）、
 * state（拿 denoiseLevel）。工具函数 encodeWav/arrayBufferToBase64 直接从 util.js import
 * —— 纯函数不该靠注入。
 */
export async function recordSample(arg, io) {
    const seconds = (arg && arg.seconds) || 3;
    io.log(`<span class="k">开始录音自检 ${seconds} 秒…</span>请正常说话`);
    let stream = null;
    let ctx = null;
    try {
      stream = await io.openMic();
      const track = stream.getAudioTracks()[0];
      const st = track.getSettings ? track.getSettings() : {};

      // 录"处理之后"的信号：如果 strong 档挂了软件链，就录链的输出，
      // 否则录原始麦克风。这样文件内容与对端实际听到的一致。
      const sourceStream = localAudioChain?.destination
        ? localAudioChain.destination.stream
        : stream;

      ctx = new AudioContext();
      const src = ctx.createMediaStreamSource(sourceStream);

      // 用 ScriptProcessor 抓原始 PCM。
      // 它是 deprecated 但兼容性最好且不需要额外加载 AudioWorklet 模块文件
      // （AudioWorklet 需要一个独立的 .js 文件，多一个部署环节）。
      // 对"录两三秒做对比"这个用途，ScriptProcessor 完全够。
      const bufferSize = 4096;
      const processor = ctx.createScriptProcessor(bufferSize, 1, 1);
      const chunks = [];
      let samples = 0;
      const need = Math.floor(ctx.sampleRate * seconds);

      const done = new Promise(resolve => {
        processor.onaudioprocess = (e) => {
          const ch = e.inputBuffer.getChannelData(0);
          chunks.push(new Float32Array(ch));
          samples += ch.length;
          if (samples >= need) {
            processor.onaudioprocess = null;
            resolve();
          }
        };
        src.connect(processor);
        processor.connect(ctx.destination);
      });

      await done;

      // 拼接
      const total = chunks.reduce((n, c) => n + c.length, 0);
      const pcm = new Float32Array(total);
      let off = 0;
      for (const c of chunks) { pcm.set(c, off); off += c.length; }

      // 转 16-bit PCM WAV
      const wav = encodeWav(pcm, ctx.sampleRate);

      // 统计：这几个数字直接反映"音质"的两个可量化方面
      let peak = 0, sum = 0;
      for (let i = 0; i < pcm.length; i++) {
        const a = Math.abs(pcm[i]);
        if (a > peak) peak = a;
        sum += pcm[i] * pcm[i];
      }
      const rms = Math.sqrt(sum / pcm.length);
      const stats = {
        seconds: (total / ctx.sampleRate).toFixed(1),
        sampleRate: ctx.sampleRate,
        peakDbfs: (20 * Math.log10(Math.max(peak, 1e-7))).toFixed(1),
        rmsDbfs: (20 * Math.log10(Math.max(rms, 1e-7))).toFixed(1),
        clipped: peak >= 0.999,
        denoiseLevel: io.state.denoiseLevel,
        browserAec: st.echoCancellation,
        browserNs: st.noiseSuppression,
        browserAgc: st.autoGainControl,
        softwareChain: !!localAudioChain,
      };

      // base64 交给 C# 落盘（WebView2 里不能直接写文件系统）
      const b64 = arrayBufferToBase64(wav);
      io.post({ type: 'recording', format: 'wav', ok: true, stats, dataBase64: b64 });
      io.log(`<span class="ok">录音完成</span> ${stats.seconds}s  峰值 ${stats.peakDbfs} dBFS  ` +
          `RMS ${stats.rmsDbfs} dBFS  档位=${stats.denoiseLevel}` +
          (stats.clipped ? '  <span class="bad">（削波！）</span>' : ''));
    } catch (e) {
      io.post({ type: 'recording', ok: false, error: `${e.name}: ${e.message}` });
      io.log(`<span class="bad">录音失败: ${io.esc(e.name)} — ${io.esc(e.message)}</span>`);
    } finally {
      try { ctx && await ctx.close(); } catch (e) { }
      if (stream) stream.getTracks().forEach(t => t.stop());
    }
  }

/**
 * 释放：停麦克风电平表（定时器 + AudioContext）。
 * 电平表是 100ms 一次的定时器，不显式停会一直跑并占着 AudioContext。
 * 由 modules.js 宿主在页面卸载/引擎停机时统一调用。
 */
export function disposeVoice() {
  stopMicMeter();
}
