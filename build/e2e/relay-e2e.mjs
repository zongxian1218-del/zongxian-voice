/* 棕仙的传输软件 —— 中继（relay）回退通道端到端测试
 * 本地起 relay_server.py → 两个浏览器上下文都填 forceRelay=true + relayUrl=ws://127.0.0.1:PORT
 * → 跳过 P2P 直接走中继 → 传一个 16MB 文件 → 接收端 SHA-256 校验。
 * 静态资源仍由 Playwright 从磁盘托管（复用 e2e.mjs 的方式）。
 */
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = 'D:\\文档\\ai001';
const APP = 'http://127.0.0.1:8799/dist/swiftdrop.html';
const PORT = 19443;
const RELAY_URL = 'ws://127.0.0.1:' + PORT;
const PY = process.env.PY || 'C:\\Users\\Administrator\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\python\\python.exe';
const RELAY_SCRIPT = path.join(ROOT, 'src', 'relay', 'relay_server.py');
const TMP = path.join(ROOT, 'tmp', 'relay-e2e');
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.bin': 'application/octet-stream', '.txt': 'text/plain; charset=utf-8' };

const results = [];
let failed = 0;
function ok(name, pass, detail) {
  results.push({ name, pass: !!pass, detail: detail || '' });
  console.log((pass ? '  [PASS] ' : '  [FAIL] ') + name + (detail ? '  — ' + detail : ''));
  if (!pass) failed++;
}
const rate = (bytes, ms) => (bytes / 1048576 / (ms / 1000)).toFixed(1);
const shaFile = (p) => crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');

/* ---------- 准备 16MB 随机文件 ---------- */
fs.rmSync(TMP, { recursive: true, force: true });
fs.mkdirSync(TMP, { recursive: true });
const bigFile = path.join(TMP, 'relay-16mb.bin');
{
  const fd = fs.openSync(bigFile, 'w');
  let left = 16 * 1024 * 1024;
  while (left > 0) { const n = Math.min(left, 1024 * 1024); fs.writeSync(fd, crypto.randomBytes(n)); left -= n; }
  fs.closeSync(fd);
}
const srcHash = shaFile(bigFile);
console.log('准备 16MB 测试文件，SHA-256 = ' + srcHash);

/* ---------- 起中继服务器 ---------- */
console.log('\n启动中继服务器 ' + RELAY_SCRIPT + '（端口 ' + PORT + '）…');
const relayLog = [];
const relay = spawn(PY, ['-u', RELAY_SCRIPT, '--host', '127.0.0.1', '--port', String(PORT), '--log'],
  { stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' } });
relay.stdout.on('data', (d) => relayLog.push(d.toString()));
relay.stderr.on('data', (d) => relayLog.push(d.toString()));
relay.on('exit', (code) => console.log('  （中继进程退出，code=' + code + '）'));

async function waitForRelay() {
  for (let i = 0; i < 50; i++) {
    try {
      const r = await fetch('http://127.0.0.1:' + PORT + '/');
      if (r.ok) return (await r.text()).trim();
    } catch (e) {}
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error('中继服务器未就绪');
}
const homeText = await waitForRelay();
ok('中继服务器 GET / 返回就绪语', homeText.includes('中继已就绪'), homeText);

/* ---------- 浏览器 ---------- */
const browser = await chromium.launch({ channel: process.env.CH || 'msedge', headless: true });

async function serve(ctx) {
  await ctx.route('**/*', async (route) => {
    const req = route.request();
    let u;
    try { u = new URL(req.url()); } catch (e) { return route.continue(); }
    // 只拦截网页自身的静态服务器（8799）；中继（19443）与公网信令都放行
    if (u.hostname !== '127.0.0.1' || u.port !== '8799') return route.continue();
    const rel = decodeURIComponent(u.pathname).replace(/^\/+/, '');
    const file = path.join(ROOT, rel);
    if (!file.startsWith(ROOT) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
      return route.fulfill({ status: 404, body: 'not found' });
    }
    return route.fulfill({ status: 200, headers: { 'content-type': MIME[path.extname(file).toLowerCase()] || 'application/octet-stream' }, body: fs.readFileSync(file) });
  });
}

async function newPage(label) {
  const ctx = await browser.newContext({ acceptDownloads: true });
  await serve(ctx);
  await ctx.addInitScript((settings) => {
    localStorage.setItem('swiftdrop.settings', JSON.stringify(settings));
    window.__pickQueue = ['recv'];
    window.showDirectoryPicker = async function () {
      const n = window.__pickQueue.shift() || 'recv';
      const root = await navigator.storage.getDirectory();
      return await root.getDirectoryHandle(n, { create: true });
    };
    window.showSaveFilePicker = window.showDirectoryPicker;
  }, { relayUrl: RELAY_URL, forceRelay: true });
  const p = await ctx.newPage();
  p.on('pageerror', (e) => console.log('  !! [' + label + '] 页面异常: ' + e.message));
  await p.goto(APP);
  await p.waitForFunction(() => !!window.SDApp, null, { timeout: 30000 });
  return p;
}

async function opfsFile(page, dirName, relPath) {
  return await page.evaluate(async ([dirName, relPath]) => {
    const root = await navigator.storage.getDirectory();
    let h = await root.getDirectoryHandle(dirName);
    const parts = relPath.split('/');
    const name = parts.pop();
    for (const q of parts) h = await h.getDirectoryHandle(q);
    const f = await (await h.getFileHandle(name)).getFile();
    const buf = new Uint8Array(await f.arrayBuffer());
    const d = await crypto.subtle.digest('SHA-256', buf);
    return { size: f.size, hex: [...new Uint8Array(d)].map((x) => x.toString(16).padStart(2, '0')).join('') };
  }, [dirName, relPath]);
}

async function dumpLogs(A, B) {
  for (const [nm, p] of [['A', A], ['B', B]]) {
    if (!p) continue;
    try {
      const t = await p.textContent('#logBox');
      console.log('\n---- ' + nm + ' 端日志（末 14 行）----');
      console.log(t.split('\n').slice(-14).map((x) => x.slice(0, 160)).join('\n'));
    } catch (e) {}
  }
}

/* ================= 开始 ================= */
console.log('\n启动浏览器…');
let A, B;
try {
  A = await newPage('A发起方');
  B = await newPage('B接收方');

  console.log('\n[T1] 建立连接（强制走中继，跳过 P2P）');
  const t0 = Date.now();
  await A.click('#btnCreate');
  await A.waitForFunction(() => document.getElementById('codeText').textContent.trim().length >= 6, null, { timeout: 20000 });
  const code = (await A.textContent('#codeText')).trim();
  console.log('  取件码：' + code);
  await B.fill('#joinCode', code);
  await B.click('#btnJoin');
  let connOk = true;
  try {
    await Promise.all([
      A.waitForSelector('#sessionPane:not(.hidden)', { timeout: 60000, state: 'visible' }),
      B.waitForSelector('#sessionPane:not(.hidden)', { timeout: 60000, state: 'visible' })
    ]);
  } catch (e) { connOk = false; }
  ok('T1 双方经中继建立连接', connOk, connOk ? ('耗时 ' + ((Date.now() - t0) / 1000).toFixed(1) + ' 秒') : '超时未连接');
  if (!connOk) throw new Error('中继连接失败，后续用例无法继续');

  const aRelay = await A.evaluate(() => !!window.SDApp.link && window.SDApp.link.isRelay === true);
  const bRelay = await B.evaluate(() => !!window.SDApp.link && window.SDApp.link.isRelay === true);
  const aChip = await A.textContent('#connChip');
  ok('T1b 连接为中继而非 P2P', aRelay && bRelay, 'A.isRelay=' + aRelay + ' B.isRelay=' + bRelay + ' · chip=' + aChip);

  await A.click('#btnPickSaveDir');
  await B.click('#btnPickSaveDir');
  await A.waitForFunction(() => !!window.SDApp.saveDir, null, { timeout: 10000 });
  await B.waitForFunction(() => !!window.SDApp.saveDir, null, { timeout: 10000 });

  console.log('\n[T2] 16MB 文件走中继传输 + SHA-256 校验');
  const st = Date.now();
  await A.setInputFiles('#fileInput', [bigFile]);
  await B.waitForFunction(() => {
    const ts = [...window.SDApp.tasks.values()].filter((t) => t.dir === 'recv' && t.path === 'relay-16mb.bin');
    return ts.length > 0 && ts.every((t) => t.state === 'done' || t.state === 'error');
  }, null, { timeout: 180000 });
  const ms = Date.now() - st;
  const r = await opfsFile(B, 'recv', 'relay-16mb.bin');
  const task = await B.evaluate(() => {
    const t = [...window.SDApp.tasks.values()].find((x) => x.dir === 'recv' && x.path === 'relay-16mb.bin');
    return t ? { state: t.state, got: t.got, hash: t.hash } : null;
  });
  const hashOk = r.hex === srcHash;
  ok('T2 16MB 文件字节级一致（SHA-256 相同）', hashOk && task && task.state === 'done',
    '哈希一致=' + hashOk + ' · 收到=' + (r.size / 1048576).toFixed(1) + 'MB · 用时=' + (ms / 1000).toFixed(1) + 's · ' + rate(16 * 1024 * 1024, ms) + ' MB/s');

} catch (e) {
  ok('测试流程异常中断', false, e.message);
  console.log(e.stack);
}

if (failed) await dumpLogs(A, B);

/* 中继服务器日志（尾部） */
console.log('\n---- 中继服务器日志（末 12 行）----');
console.log(relayLog.join('').split('\n').filter(Boolean).slice(-12).join('\n'));

relay.kill('SIGKILL');
await browser.close();

console.log('\n================ 汇总 ================');
results.forEach((r) => console.log((r.pass ? 'PASS  ' : 'FAIL  ') + r.name + (r.detail ? '  — ' + r.detail : '')));
console.log(`${results.length - failed}/${results.length} 通过`);
process.exit(failed ? 1 : 0);
