/* 针对性验证：对方不打洞/不回应时，发起方会不会反复重发 offer？
 * 修复前的行为：加入方每 2.5~3 秒打招呼一次 → 发起方每次都重新协商 → 反复打断 ICE → 永远连不上。
 * 修复后的期望：15 秒内只发出 1 次 offer。
 */
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = 'D:\\文档\\ai001';
const APP = 'http://127.0.0.1:8799/dist/swiftdrop.html';
const MIME = { '.html': 'text/html; charset=utf-8', '.png': 'image/png' };

const browser = await chromium.launch({ channel: 'msedge', headless: true });
const ctx = await browser.newContext();
await ctx.route('**/*', async (route) => {
  const u = new URL(route.request().url());
  if (u.hostname !== '127.0.0.1') return route.continue();
  const f = path.join(ROOT, decodeURIComponent(u.pathname).replace(/^\/+/, ''));
  if (!fs.existsSync(f) || !fs.statSync(f).isFile()) return route.fulfill({ status: 404, body: 'x' });
  return route.fulfill({ status: 200, headers: { 'content-type': MIME[path.extname(f)] || 'application/octet-stream' }, body: fs.readFileSync(f) });
});
const page = await ctx.newPage();
await page.goto(APP);
await page.waitForFunction(() => !!window.SD && !!window.SD.PeerLink, null, { timeout: 20000 });

const out = await page.evaluate(async () => {
  const offers = [];
  const hellos = [];
  let hostLink = null, guestLink = null;
  const hostSig = {
    peerId: 'host', ready: true,
    publish(o) {
      if (o.t === 'offer') offers.push(Date.now());
      if (o.t !== 'hello' && guestLink) guestLink.handleSignal(o);
    }
  };
  const guestSig = {
    peerId: 'guest', ready: true,
    publish(o) {
      if (o.t === 'hello') hellos.push(Date.now());
      if (o.t !== 'hello' && hostLink) hostLink.handleSignal(o);
    }
  };
  hostLink = new SD.PeerLink({ signaling: hostSig, role: 'host', iceServers: [], handlers: {} }).start();
  guestLink = new SD.PeerLink({ signaling: guestSig, role: 'guest', iceServers: [], handlers: {} }).start();
  // 模拟"对方收到了 offer 但打洞一直不通、也不回 answer"
  guestLink.handleSignal = async () => {};
  await new Promise((r) => setTimeout(r, 15000));
  const n = offers.length;
  const h = hellos.length;
  hostLink.close(false);
  guestLink.close(false);
  return { offers: n, hellos: h, firstAt: offers.length ? offers[0] : 0, lastAt: offers.length ? offers[offers.length - 1] : 0 };
});

console.log('15 秒内：加入方打招呼 ' + out.hellos + ' 次；发起方发出 offer ' + out.offers + ' 次');
const pass = out.offers <= 1;
console.log((pass ? '[PASS] ' : '[FAIL] ') + '对方不回应时不再反复重发 offer（修复前会是 5~6 次）');
await browser.close();
process.exit(pass ? 0 : 1);
