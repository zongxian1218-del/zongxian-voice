/* 生成界面截图，用于人工/视觉核查 UI */
import { chromium } from 'playwright';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = 'D:\\文档\\ai001';
const APP = 'http://127.0.0.1:8799/dist/swiftdrop.html';
const TMP = path.join(ROOT, 'tmp', 'shots');
const OUT = path.join(ROOT, 'build', 'shots');
fs.rmSync(TMP, { recursive: true, force: true });
fs.mkdirSync(TMP, { recursive: true });
fs.mkdirSync(OUT, { recursive: true });

const BIG = path.join(TMP, '演示视频-4K.mp4');
{
  const fd = fs.openSync(BIG, 'w');
  let left = 256 * 1024 * 1024;
  while (left > 0) { const n = Math.min(left, 1048576); fs.writeSync(fd, crypto.randomBytes(n)); left -= n; }
  fs.closeSync(fd);
}

const browser = await chromium.launch({ channel: 'msedge', headless: true });
const MIME = { '.html': 'text/html; charset=utf-8', '.bin': 'application/octet-stream' };
async function page(first, name) {
  const ctx = await browser.newContext({ viewport: { width: 1180, height: 900 }, deviceScaleFactor: 1.5 });
  await ctx.route('**/*', async (route) => {
    const u = new URL(route.request().url());
    if (u.hostname !== '127.0.0.1') return route.continue();
    const f = path.join(ROOT, decodeURIComponent(u.pathname).replace(/^\/+/, ''));
    if (!fs.existsSync(f) || !fs.statSync(f).isFile()) return route.fulfill({ status: 404, body: 'x' });
    return route.fulfill({ status: 200, headers: { 'content-type': MIME[path.extname(f)] || 'application/octet-stream' }, body: fs.readFileSync(f) });
  });
  await ctx.addInitScript(`localStorage.setItem('swiftdrop.settings', JSON.stringify({deviceName:${JSON.stringify(name)}}));
    window.__pickQueue=${JSON.stringify([first])};
    window.showDirectoryPicker = async () => { const r = await navigator.storage.getDirectory(); return await r.getDirectoryHandle(window.__pickQueue.shift()||'recv',{create:true}); };`);
  const p = await ctx.newPage();
  await p.goto(APP);
  await p.waitForFunction(() => !!window.SDApp, null, { timeout: 20000 });
  return p;
}

const A = await page('recv', '小明的电脑');
await A.screenshot({ path: path.join(OUT, '01-home.png'), fullPage: true });

await A.click('#btnCreate');
await A.waitForFunction(() => document.getElementById('codeText').textContent.trim().length >= 6);
const code = (await A.textContent('#codeText')).trim();
await A.screenshot({ path: path.join(OUT, '02-code.png'), fullPage: true });

const B = await page('recv', '朋友的 MacBook');
await B.fill('#joinCode', code);
await B.click('#btnJoin');
await Promise.all([
  A.waitForSelector('#sessionPane:not(.hidden)', { timeout: 90000 }),
  B.waitForSelector('#sessionPane:not(.hidden)', { timeout: 90000 })
]);
await A.click('#btnPickSaveDir');
await B.click('#btnPickSaveDir');
await A.waitForFunction(() => !!window.SDApp.saveDir);
await B.waitForFunction(() => !!window.SDApp.saveDir);

await A.setInputFiles('#fileInput', [BIG]);
await new Promise((r) => setTimeout(r, 2600));
await A.screenshot({ path: path.join(OUT, '03-transfer.png'), fullPage: true });
await B.screenshot({ path: path.join(OUT, '04-receiving.png'), fullPage: true });
await B.waitForFunction(() => [...window.SDApp.tasks.values()].some((t) => t.state === 'done' || t.state === 'error'), null, { timeout: 120000 });
await A.screenshot({ path: path.join(OUT, '05-done.png'), fullPage: true });

// 深色主题
await A.evaluate(() => { localStorage.setItem('zongxian.theme', 'dark'); document.documentElement.setAttribute('data-theme', 'dark'); });
await B.evaluate(() => { localStorage.setItem('zongxian.theme', 'dark'); document.documentElement.setAttribute('data-theme', 'dark'); });
await new Promise((r) => setTimeout(r, 300));
await A.screenshot({ path: path.join(OUT, '05b-done-dark.png'), fullPage: true });
await A.evaluate(() => { document.documentElement.setAttribute('data-theme', 'light'); });
await B.evaluate(() => { document.documentElement.setAttribute('data-theme', 'light'); });

// 同步页
await A.click('.tab[data-tab="sync"]');
await A.evaluate(() => window.__pickQueue.push('syncA'));
await A.click('#btnPickSyncDir');
await A.waitForFunction(() => !!window.SDApp.syncDir);
await A.click('#btnSyncNow');
await new Promise((r) => setTimeout(r, 1500));
await A.screenshot({ path: path.join(OUT, '06-sync.png'), fullPage: true });

console.log('截图已保存到 ' + OUT);
fs.readdirSync(OUT).forEach((f) => console.log('  ' + f + '  ' + (fs.statSync(path.join(OUT, f)).size / 1024).toFixed(0) + ' KB'));
await browser.close();
