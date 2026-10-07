// share-audio-worklet.js —— 「共享电脑声音」的播放端
//
// 为什么需要它：采集在**应用进程**（C# 调引擎），声音以不定时的块推给页面；
// 而 WebAudio 需要**严格准时**地取样本。中间必须有一个缓冲，否则会爆音/断音。
//
// 数据流：C# 读引擎 PCM -> base64 -> ExecuteScriptAsync -> 本 worklet 的队列 -> 音轨
// 单位约定：一律 **float32 / 48000 / 2 声道交叉**（与引擎侧一致，改动要两边一起改）。

class ShareAudioPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];          // 待播的 Float32Array（交叉，2 声道）
    this.queued = 0;          // 待播样本数
    this.target = 48000 * 2 * 0.20;   // 目标缓冲 200ms：够吸收抖动，又不至于太延迟
    this.starved = 0;         // 饿死次数（诊断用：说明推数据不够快）
    this.dropped = 0;         // 丢弃样本数（说明推得太快，缓冲顶了）
    // 【A1 诊断】收到/处理计数：区分"没收到数据"与"收到了但 process 没消费"
    this.gotMsgs = 0;
    this.gotSamples = 0;
    this.processedFrames = 0;
    // 【A1 诊断】渲染量子与消费量：代码假设每帧 128 帧、2 声道。若实际不是，
    // 队列就会"只进不出"（本会话正是在这里卡住的）。把这些事实记下来，别再假设。
    this.lastN = 0;
    this.lastChannels = 0;
    this.consumed = 0;
    this.maxOutPeak = 0;
    this.renderQuantum = (typeof sampleRate === 'number') ? sampleRate : -1;
    this.port.onmessage = (ev) => {
      const d = ev.data;
      if (!d) return;
      if (d.type === 'pcm' && d.samples && d.samples.length) {
        this.gotMsgs += 1;
        this.gotSamples += d.samples.length;
        // 顶了就丢**最旧**的：宁可跳一下，也不要让延迟无限增长
        while (this.queued + d.samples.length > this.target * 3 && this.queue.length) {
          this.queued -= this.queue.shift().length;
          this.dropped += 1;
        }
        this.queue.push(d.samples);
        this.queued += d.samples.length;
      } else if (d.type === 'stats') {
        // 【A1 诊断】除了饿死/丢弃，还要报"收到多少块、处理了多少帧、最近一次队列深度"：
        // 只报饿死次数无法区分"没收到数据"与"收到了但 process() 没在消费"。
        this.port.postMessage({
          starved: this.starved, dropped: this.dropped,
          queuedMs: Math.round(this.queued / 96),
          gotMsgs: this.gotMsgs, gotSamples: this.gotSamples, processedFrames: this.processedFrames,
          lastQueued: Math.round(this.queued),
          sampleRate: this.renderQuantum, lastN: this.lastN, lastChannels: this.lastChannels,
          consumed: this.consumed, maxOutPeak: this.maxOutPeak,
        });
      }
    };
  }

  process(inputs, outputs) {
    this.processedFrames += 1;
    const ch = outputs[0];
    const n = ch[0].length;          // 通常 128 帧
    // 【A1 修复】按**实际输出声道数**算需要多少交叉样本，别再假设立体声。
    // 实测这个 AudioContext 的输出是 1 声道（lastChannels=1）：
    // 硬编码 *2 会每次多取一倍样本、并把奇数位样本整批丢掉（音频被"抽帧"破坏）。
    const outCh = ch.length;
    const need = n * outCh;
    // 【A1 诊断】把"每帧真实长度/声道数/消费量/写出峰值"记下来：
    // 之前假设 n 恒为 128，若实际不是（例如渲染量子变化），队列就会只进不出。
    this.lastN = n;
    this.lastChannels = ch.length;
    this.consumed = (this.consumed || 0) + need;
    if (this.queued < need) {
      // 没数据：输出静音，并记一次饿死（诊断"到底有没有在供上"）
      this.starved += 1;
      for (let c = 0; c < ch.length; ++c) ch[c].fill(0);
      return true;
    }
    // 从队列里取 need 个交叉样本
    const out = new Float32Array(need);
    let filled = 0;
    while (filled < need && this.queue.length) {
      const head = this.queue[0];
      const take = Math.min(head.length, need - filled);
      out.set(head.subarray(0, take), filled);
      if (take === head.length) this.queue.shift();
      else this.queue[0] = head.subarray(take);
      filled += take;
    }
    this.queued -= need;
    // 交叉 -> 分平面（WebAudio 要的是每声道一条数组）。
    // 单声道：直接取交叉的第 0 声道；多声道：按 outCh 步长取对应平面。
    for (let c = 0; c < outCh; ++c) {
      const dst = ch[c];
      for (let i = 0; i < n; ++i) dst[i] = out[i * outCh + c];
    }
    // 记写出峰值（0 说明一直在写静音 —— 那对端就永远收不到）
    let peak = 0;
    for (let i = 0; i < need; ++i) { const a = Math.abs(out[i]); if (a > peak) peak = a; }
    if (peak > this.maxOutPeak) this.maxOutPeak = peak;
    return true;
  }
}

registerProcessor('share-audio-player', ShareAudioPlayer);
