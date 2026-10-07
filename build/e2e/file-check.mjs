/* 验证 file:// 直接双击打开时，各关键能力是否可用 */
import { chromium } from 'playwright';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const FILE = pathToFileURL(path.join(ROOT, 'dist', 'swiftdrop.html')).href;
const browser = await chromium.launch({ channel: 'msedge', headless: true });

for (const url of [FILE, 'http://127.0.0.1:8799/dist/swiftdrop.html']) {
  const ctx = await browser.newContext();
  const p = await ctx.newPage();
  const errs = [];
  p.on('pageerror', (e) => errs.push(e.message));
  await p.goto(url);
  await p.waitForTimeout(1500);
  const r = await p.evaluate(() => ({
    secure: window.isSecureContext,
    subtle: typeof crypto.subtle,
    idb: typeof indexedDB,
    fsa: typeof window.showDirectoryPicker,
    rtc: typeof RTCPeerConnection,
    qr: typeof (window.QR && window.QR.toCanvas),
    app: typeof window.SDApp,
    ver: window.SD_VERSION || '?'
  }));
  console.log((url.startsWith('file') ? '[file://] ' : '[http://] ') + JSON.stringify(r) + (errs.length ? ' 页面异常:' + errs.join(';') : ''));
  await ctx.close();
}

// file:// 下能否真正连上信令
const ctx = await browser.newContext();
const p = await ctx.newPage();
p.on('pageerror', (e) => console.log('  file:// 页面异常: ' + e.message));
await p.goto(FILE);
await p.waitForFunction(() => !!window.SDApp, null, { timeout: 15000 });
await p.click('#btnCreate');
await p.waitForTimeout(6000);
const st = await p.evaluate(() => ({ code: document.getElementById('codeText').textContent.trim(), sig: document.getElementById('sigChip').textContent, log: document.getElementById('logBox').textContent.slice(-200) }));
console.log('file:// 信令测试：' + JSON.stringify(st));
await browser.close();
