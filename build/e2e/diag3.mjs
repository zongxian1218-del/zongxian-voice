/* 验证背压修正后：首次传输是否也能跑满 */
import { chromium } from 'playwright';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = 'D:\\文档\\ai001';
const APP = 'http://127.0.0.1:8799/dist/swiftdrop.html';
const TMP = path.join(ROOT, 'tmp', 'diag3');
fs.rmSync(TMP, { recursive: true, force: true });
fs.mkdirSync(TMP, { recursive: true });
const BIG = path.join(TMP, 'big.bin');
const SZ = 128 * 1024 * 1024;
const hash = (() => { const fd = fs.openSync(BIG, 'w'); let left = SZ; while (left > 0) { const n = Math.min(left, 1048576); fs.writeSync(fd, crypto.randomBytes(n)); left -= n; } fs.closeSync(fd); return crypto.createHash('sha256').update(fs.readFileSync(BIG)).digest('hex'); })();

const browser = await chromium.launch({ channel: 'msedge', headless: true });
const MIME = { '.html': 'text/html; charset=utf-8', '.bin': 'application/octet-stream' };
async function page(first) {
  const ctx = await browser.newContext();
  await ctx.route('**/*', async (route) => {
    const u = new URL(route.request().url());
    if (u.hostname !== '127.0.0.1') return route.continue();
    const f = path.join(ROOT, decodeURIComponent(u.pathname).replace(/^\/+/, ''));
    if (!fs.existsSync(f) || !fs.statSync(f).isFile()) return route.fulfill({ status: 404, body: 'x' });
    return route.fulfill({ status: 200, headers: { 'content-type': MIME[path.extname(f)] || 'application/octet-stream' }, body: fs.readFileSync(f) });
  });
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
const cfg = await A.evaluate(() => SD.loadSettings());
console.log(`参数：分片 ${cfg.chunkSize / 1024}KB / 高水位 ${cfg.bufferHigh / 1048576}MB / 文件 ${SZ / 1048576}MB\n`);

for (const round of [1, 2, 3]) {
  await A.evaluate((c) => SD.saveSettings(c), round === 2 ? { chunkSize: 131072 } : round === 3 ? { chunkSize: 65536 } : {});
  await B.evaluate(() => window.SDApp.tasks.clear());
  await A.evaluate(() => window.SDApp.tasks.clear());
  const t0 = Date.now();
  await A.setInputFiles('#fileInput', [BIG]);
  const sampler = setInterval(async () => {}, 1e9);
  clearInterval(sampler);
  const samples = [];
  const timer = setInterval(async () => {
    try { samples.push(await A.evaluate(() => window.SDApp.link.dat.bufferedAmount)); } catch (e) {}
  }, 250);
  await B.waitForFunction(() => {
    const ts = [...window.SDApp.tasks.values()].filter((t) => t.dir === 'recv');
    return ts.length > 0 && ts.some((t) => t.state === 'done' || t.state === 'error');
  }, null, { timeout: 300000 });
  clearInterval(timer);
  const ms = Date.now() - t0;
  const ch = await A.evaluate(() => SD.loadSettings().chunkSize);
  const st = await B.evaluate(() => { const t = [...window.SDApp.tasks.values()].find((x) => x.dir === 'recv' && x.path === 'big.bin'); return t ? t.state + '|' + t.msg : '?'; });
  const avg = samples.length ? (samples.reduce((a, b) => a + b, 0) / samples.length / 1048576).toFixed(1) : '-';
  const mx = samples.length ? (Math.max(...samples) / 1048576).toFixed(1) : '-';
  console.log(`第 ${round} 次（分片 ${ch / 1024}KB）：${(SZ / 1048576 / (ms / 1000)).toFixed(1)} MB/s  （${(ms / 1000).toFixed(1)}s，缓冲均值 ${avg}MB 峰值 ${mx}MB，状态 ${st}）`);
}

const got = await B.evaluate(async () => {
  const root = await navigator.storage.getDirectory();
  const d = await root.getDirectoryHandle('recv');
  const f = await (await d.getFileHandle('big.bin')).getFile();
  const b = new Uint8Array(await f.arrayBuffer());
  const h = await crypto.subtle.digest('SHA-256', b);
  return { size: f.size, hex: [...new Uint8Array(h)].map((x) => x.toString(16).padStart(2, '0')).join('') };
});
console.log(`\n最终校验：大小 ${got.size} / ${SZ}，SHA-256 ${got.hex === hash ? '一致 ✔' : '不一致 ✘'}`);
await browser.close();
