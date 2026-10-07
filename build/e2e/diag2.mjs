/* 对照实验：接收端分别用「空消费」「只写盘不哈希」「完整逻辑」，看速度差在哪 */
import { chromium } from 'playwright';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const ROOT = 'D:\\文档\\ai001';
const APP = 'http://127.0.0.1:8799/dist/swiftdrop.html';
const TMP = path.join(ROOT, 'tmp', 'diag2');
fs.rmSync(TMP, { recursive: true, force: true });
fs.mkdirSync(TMP, { recursive: true });
const BIG = path.join(TMP, 'big.bin');
const SZ = 64 * 1024 * 1024;
{
  const fd = fs.openSync(BIG, 'w');
  let left = SZ;
  while (left > 0) { const n = Math.min(left, 1048576); fs.writeSync(fd, crypto.randomBytes(n)); left -= n; }
  fs.closeSync(fd);
}
console.log('CPU 逻辑核：' + os.cpus().length + ' / ' + os.cpus()[0].model);

const browser = await chromium.launch({ channel: 'msedge', headless: true });
const MIME = { '.html': 'text/html; charset=utf-8', '.bin': 'application/octet-stream' };
async function serve(ctx) {
  await ctx.route('**/*', async (route) => {
    const u = new URL(route.request().url());
    if (u.hostname !== '127.0.0.1') return route.continue();
    const f = path.join(ROOT, decodeURIComponent(u.pathname).replace(/^\/+/, ''));
    if (!fs.existsSync(f) || !fs.statSync(f).isFile()) return route.fulfill({ status: 404, body: 'x' });
    return route.fulfill({ status: 200, headers: { 'content-type': MIME[path.extname(f)] || 'application/octet-stream' }, body: fs.readFileSync(f) });
  });
}
async function page(first) {
  const ctx = await browser.newContext();
  await serve(ctx);
  await ctx.addInitScript(`window.__pickQueue=${JSON.stringify([first])};
    window.showDirectoryPicker = async () => { const r = await navigator.storage.getDirectory(); return await r.getDirectoryHandle(window.__pickQueue.shift()||'recv',{create:true}); };`);
  const p = await ctx.newPage();
  p.on('pageerror', (e) => console.log('!! pageerror:', e.message));
  await p.goto(APP);
  await p.waitForFunction(() => !!window.SDApp, null, { timeout: 30000 });
  return p;
}
const A = await page('recv'), B = await page('recv');
await A.click('#btnCreate');
await A.waitForFunction(() => document.getElementById('codeText').textContent.trim().length >= 6);
const code = (await A.textContent('#codeText')).trim();
await B.fill('#joinCode', code);
await B.click('#btnJoin');
await Promise.all([A.waitForSelector('#sessionPane:not(.hidden)', { timeout: 90000 }), B.waitForSelector('#sessionPane:not(.hidden)', { timeout: 90000 })]);
await A.click('#btnPickSaveDir'); await B.click('#btnPickSaveDir');
await A.waitForFunction(() => !!window.SDApp.saveDir); await B.waitForFunction(() => !!window.SDApp.saveDir);
await A.evaluate(() => SD.saveSettings({ chunkSize: 65536, bufferHigh: 8 * 1048576, bufferLow: 2 * 1048576 }));

async function trial(label, setup) {
  await B.evaluate(setup);
  await B.evaluate(() => { window.SDApp.tasks.clear(); window.__prof = { chunks: 0, bytes: 0, gaps: [], writes: [], hashes: [], persists: [], t0: 0 }; });
  await A.evaluate(() => { window.SDApp.tasks.clear(); window.__drain = 0; });
  const t0 = Date.now();
  await A.setInputFiles('#fileInput', [BIG]);
  try {
    await B.waitForFunction(() => {
      const ts = [...window.SDApp.tasks.values()].filter((t) => t.dir === 'recv');
      return ts.length > 0 && ts.some((t) => t.state === 'done' || t.state === 'error');
    }, null, { timeout: 200000 });
  } catch (e) { console.log(`  ${label}: 超时`); }
  const ms = Date.now() - t0;
  const prof = await B.evaluate(() => {
    const p = window.__prof;
    const st = (a) => a.length ? `n=${a.length} 平均${(a.reduce((x, y) => x + y, 0) / a.length).toFixed(1)}ms 最大${Math.max(...a).toFixed(1)}ms` : '无';
    const gaps = p.gaps.slice(1);
    return { chunks: p.chunks, bytes: p.bytes, write: st(p.writes), hash: st(p.hashes), persist: st(p.persists), gapAvg: gaps.length ? (gaps.reduce((x, y) => x + y, 0) / gaps.length).toFixed(2) : '-', gapMax: gaps.length ? Math.max(...gaps).toFixed(1) : '-' };
  });
  const task = await B.evaluate(() => { const t = [...window.SDApp.tasks.values()].find((x) => x.dir === 'recv' && x.path === 'big.bin'); return t ? t.state : '?'; });
  console.log(`  ${label}\n     用时 ${(ms / 1000).toFixed(1)}s → ${(SZ / 1048576 / (ms / 1000)).toFixed(1)} MB/s ；状态=${task}`);
  console.log(`     收帧=${prof.chunks} 收字节=${(prof.bytes / 1048576).toFixed(1)}MB 帧间隔 平均${prof.gapAvg}ms 最大${prof.gapMax}ms`);
  console.log(`     写盘 ${prof.write} ; 哈希 ${prof.hash} ; IDB ${prof.persist}`);
}

// 探针：统一记录各阶段耗时
const profiler = () => {
  const P = window.__prof = { chunks: 0, bytes: 0, gaps: [], writes: [], hashes: [], persists: [] };
  const T = SD.Transfer.prototype;
  const oc = T._onChunk, ow = T._writeOut, op = T._persist;
  const osha = SD.sha256;
  T._onChunk = function (tid, i, payload) {
    const now = performance.now();
    if (P.chunks) P.gaps.push(now - P.last);
    P.last = now; P.chunks++; P.bytes += payload.length;
    const t = performance.now();
    const r = oc.call(this, tid, i, payload);
    P.recvHandler = performance.now() - t;
    return r;
  };
  T._writeOut = async function (rec, buf) { const t = performance.now(); const r = await ow.call(this, rec, buf); P.writes.push(performance.now() - t); return r; };
  T._persist = async function (rec) { const t = performance.now(); const r = await op.call(this, rec); P.persists.push(performance.now() - t); return r; };
  SD.sha256 = async function (b) { const t = performance.now(); const r = await osha(b); P.hashes.push(performance.now() - t); return r; };
  window.__origPush = T._pushChunk;
  window.__origSha = osha;
};

console.log('\n对照实验（64MB，64KB 分片，8MB 缓冲）');
await B.evaluate(profiler);

await trial('① 只计数（完全不写盘、不哈希）', () => {
  SD.Transfer.prototype._pushChunk = async function (rec, chunk) { rec.got += chunk.length; this._tick(rec); };
});
await trial('② 只写盘（不哈希、不写 IDB）', () => {
  SD.Transfer.prototype._pushChunk = window.__origPush;
  SD.sha256 = async () => 'deadbeef';
  SD.idbSet = async () => true;
});
await trial('③ 完整逻辑', () => {
  SD.sha256 = window.__origSha;
  SD.idbSet = Object.getPrototypeOf ? window.__origIdbSet || SD.idbSet : SD.idbSet;
});
console.log('\n※ ③ 的 IDB 屏蔽未恢复，仅供对比参考');
await browser.close();
