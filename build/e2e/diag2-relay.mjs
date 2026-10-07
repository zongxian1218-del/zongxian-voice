import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = 'D:\\文档\\ai001';
const APP = 'http://127.0.0.1:8799/dist/swiftdrop.html';
const PORT = 19443;
const PY = 'C:\\Users\\Administrator\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\python\\python.exe';

const relay = spawn(PY, ['-u', path.join(ROOT, 'src/relay/relay_server.py'), '--host', '127.0.0.1', '--port', String(PORT), '--log'],
  { stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' } });
const relayLog = [];
relay.stdout.on('data', d => relayLog.push(d.toString()));
relay.stderr.on('data', d => relayLog.push(d.toString()));

for (let i = 0; i < 50; i++) {
  try { const r = await fetch('http://127.0.0.1:' + PORT + '/'); if (r.ok) break; } catch (e) {}
  await new Promise(r => setTimeout(r, 200));
}
console.log('relay ready, GET / =', await (await fetch('http://127.0.0.1:' + PORT + '/')).text());

const browser = await chromium.launch({ channel: 'msedge', headless: true });
const ctx = await browser.newContext();
await ctx.route('**/*', async (route) => {
  const req = route.request();
  let u; try { u = new URL(req.url()); } catch (e) { return route.continue(); }
  if (u.hostname !== '127.0.0.1' || u.port !== '8799') return route.continue();
  const rel = decodeURIComponent(u.pathname).replace(/^\/+/, '');
  const file = path.join(ROOT, rel);
  if (!file.startsWith(ROOT) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404, body: 'nf' });
  return route.fulfill({ status: 200, headers: { 'content-type': 'text/html' }, body: fs.readFileSync(file) });
});
await ctx.addInitScript((s) => { localStorage.setItem('swiftdrop.settings', JSON.stringify(s)); },
  { relayUrl: 'ws://127.0.0.1:' + PORT, forceRelay: true });
const p = await ctx.newPage();
p.on('pageerror', e => console.log('PAGEERR', e.message));
p.on('console', m => { if (['error', 'warning'].includes(m.type())) console.log('CONSOLE-' + m.type(), m.text()); });
await p.goto(APP);
await p.waitForFunction(() => !!window.SDApp, null, { timeout: 30000 });
await p.click('#btnCreate');
await p.waitForTimeout(12000);
const info = await p.evaluate(() => ({
  relayState: window.SDApp.relay.state,
  relayRoom: window.SDApp.relay.room,
  relayUrl: window.SDApp.relay.url,
  linkIsRelay: !!(window.SDApp.link && window.SDApp.link.isRelay),
  connChip: document.getElementById('connChip').textContent,
  sessVisible: !document.getElementById('sessionPane').classList.contains('hidden'),
  log: document.getElementById('logBox').textContent
}));
console.log(JSON.stringify(info, null, 2));
console.log('---- relay log ----');
console.log(relayLog.join(''));
relay.kill('SIGKILL');
await browser.close();
