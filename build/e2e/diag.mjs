/* 速度瓶颈定位：采样 sender 的 bufferedAmount / SCTP bytesSent 与 receiver 的落地速率 */
import { chromium } from 'playwright';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = 'D:\\文档\\ai001';
const APP = 'http://127.0.0.1:8799/dist/swiftdrop.html';
const TMP = path.join(ROOT, 'tmp', 'diag');
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

const CHUNK = parseInt(process.env.CHUNK || '65536', 10);
const HIGH = parseInt(process.env.HIGH || String(12 * 1048576), 10);

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
await A.waitForFunction(() => !!window.SDApp.saveDir);
await B.waitForFunction(() => !!window.SDApp.saveDir);
await A.evaluate((c) => SD.saveSettings({ chunkSize: c.c, bufferHigh: c.h, bufferLow: c.l }), { c: CHUNK, h: HIGH, l: 2 * 1048576 });
const mx = await A.evaluate(() => window.SDApp.link.maxMessage());
const qr = await A.evaluate(() => {
  try { if (!window.QR) return 'QR 未加载'; const c = document.getElementById('qrCanvas'); QR.toCanvas('http://127.0.0.1:8799/dist/swiftdrop.html#c=ABCDEFGHI', c); return 'ok size=' + c.width + 'x' + c.height + ' hidden=' + c.classList.contains('hidden'); }
  catch (e) { return 'QR 异常: ' + e.message; }
});
console.log(`SCTP maxMessageSize=${mx} 字节；二维码：${qr}`);
console.log(`参数：分片 ${CHUNK / 1024}KB / 高水位 ${HIGH / 1048576}MB / 文件 ${SZ / 1048576}MB\n`);
console.log('  时间   A发出(MB) A缓冲(MB)  A任务(MB)  B落地(MB)   瞬时(MB/s)');

const t0 = Date.now();
await A.setInputFiles('#fileInput', [BIG]);
let lastB = 0, lastT = 0, lastStats = 0;
for (let i = 0; i < 200; i++) {
  await new Promise((r) => setTimeout(r, 500));
  const s = await A.evaluate(async () => {
    const st = await window.SDApp.link.pc.getStats();
    let sent = 0;
    st.forEach((v) => { if (v.type === 'data-channel') sent = Math.max(sent, v.bytesSent || 0); });
    let st2 = 0; st.forEach((v) => { if (v.type === 'transport' && v.bytesSent) st2 = Math.max(st2, v.bytesSent); });
    const t = [...window.SDApp.tasks.values()].find((x) => x.dir === 'send' && x.state === 'run');
    return { buffered: window.SDApp.link.dat.bufferedAmount, got: t ? t.got : 0, dcSent: sent, trSent: st2 };
  });
  const b = await B.evaluate(() => {
    const t = [...window.SDApp.tasks.values()].find((x) => x.dir === 'recv' && x.state === 'run');
    return { got: t ? t.got : 0, done: !t };
  });
  const now = Date.now();
  const inst = lastT ? ((b.got - lastB) / 1048576) / ((now - lastT) / 1000) : 0;
  const trInst = lastT ? ((s.trSent - lastStats) / 1048576) / ((now - lastT) / 1000) : 0;
  console.log(`${String(((now - t0) / 1000).toFixed(1)).padStart(5)}s  ${(s.trSent / 1048576).toFixed(1).padStart(8)}  ${(s.buffered / 1048576).toFixed(1).padStart(8)}  ${(s.got / 1048576).toFixed(1).padStart(8)}  ${(b.got / 1048576).toFixed(1).padStart(8)}  ${inst.toFixed(1).padStart(8)}  (链路 ${trInst.toFixed(1)})`);
  lastB = b.got; lastT = now; lastStats = s.trSent;
  if (b.done && b.got > 0) break;
}
const el = (Date.now() - t0) / 1000;
const fin = await B.evaluate(() => { const t = [...window.SDApp.tasks.values()].find((x) => x.dir === 'recv'); return t ? { state: t.state, got: t.got } : null; });
console.log(`\n结束：用时 ${el.toFixed(1)}s，平均 ${(SZ / 1048576 / el).toFixed(1)} MB/s，结果 ${JSON.stringify(fin)}`);
await browser.close();
