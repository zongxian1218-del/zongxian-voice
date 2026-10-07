import { chromium } from 'playwright';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const ctx = await browser.newContext();
await ctx.route('**/*', async (route) => {
  const req = route.request();
  let u; try { u = new URL(req.url()); } catch (e) { return route.continue(); }
  if (u.hostname !== '127.0.0.1') return route.continue();
  const fs = await import('node:fs');
  const path = await import('node:path');
  const ROOT = 'D:\\文档\\ai001';
  const rel = decodeURIComponent(u.pathname).replace(/^\/+/, '');
  const file = path.join(ROOT, rel);
  if (!file.startsWith(ROOT) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404, body: 'nf' });
  return route.fulfill({ status: 200, headers: { 'content-type': 'text/html' }, body: fs.readFileSync(file) });
});
await ctx.addInitScript((s) => {
  localStorage.setItem('swiftdrop.settings', JSON.stringify(s));
}, { relayUrl: 'ws://127.0.0.1:19443', forceRelay: true });
const p = await ctx.newPage();
p.on('pageerror', (e) => console.log('PAGEERR', e.message));
p.on('console', (m) => { if (m.type() === 'error' || m.type() === 'warning') console.log('CONSOLE', m.type(), m.text()); });
await p.goto('http://127.0.0.1:8799/dist/swiftdrop.html');
await p.waitForFunction(() => !!window.SDApp, null, { timeout: 30000 });
await p.click('#btnCreate');
await p.waitForTimeout(9000);
const info = await p.evaluate(async () => {
  const s = SD.loadSettings();
  return {
    relayUrl: s.relayUrl, forceRelay: s.forceRelay,
    relayState: window.SDApp.relay.state, relayRoom: window.SDApp.relay.room, relayUrl2: window.SDApp.relay.url,
    linkIsRelay: !!(window.SDApp.link && window.SDApp.link.isRelay),
    connChip: document.getElementById('connChip').textContent,
    log: document.getElementById('logBox').textContent.split('\n').slice(-14).join('\n')
  };
});
console.log(JSON.stringify(info, null, 2));
await browser.close();
