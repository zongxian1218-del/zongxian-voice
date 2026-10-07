// ===========================================================================
// util.js —— 与业务无关的纯工具（S3：被多个模块共享，所以不再靠"注入"）
//
// 为什么单独立一个文件：`arrayBufferToBase64` 被文件传输与录音自检同时需要，
// `encodeWav` 被录音自检与信号链自检同时需要。原来它们躺在 call.js 里，
// 别的模块要用只能靠 io 注入 —— 而"纯函数"根本没有状态，注入是多余耦合。
// ===========================================================================

export function encodeWav(samples, sampleRate) {
  const bytesPerSample = 2;
  const dataBytes = samples.length * bytesPerSample;
  const buffer = new ArrayBuffer(44 + dataBytes);
  const view = new DataView(buffer);

  const writeStr = (off, s) => {
    for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i));
  };

  writeStr(0, 'RIFF');
  view.setUint32(4, 36 + dataBytes, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  view.setUint32(16, 16, true);          // fmt 块长度
  view.setUint16(20, 1, true);           // 1 = PCM
  view.setUint16(22, 1, true);           // 单声道
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * bytesPerSample, true);  // 字节率
  view.setUint16(32, bytesPerSample, true);               // 块对齐
  view.setUint16(34, 16, true);          // 位深
  writeStr(36, 'data');
  view.setUint32(40, dataBytes, true);

  let off = 44;
  for (let i = 0; i < samples.length; i++) {
    // 饱和截断：宁可削平也不要回绕成刺耳爆音
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
    off += 2;
  }
  return buffer;
}

export function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}
