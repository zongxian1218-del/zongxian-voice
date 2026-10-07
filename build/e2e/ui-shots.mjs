/* 界面打磨验收截图：浅色首屏 / 深色首屏 / 手机竖屏 / 已连接+传输中+诊断有数据
 * 用法： node D:\文档\ai001\tmp\ui\shots.mjs
 * 前置： 桌面版 webhost 已在 8787 端口运行（python -m swiftdrop webhost --port 8787）
 */
import { chromium } from 'playwright';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = 'D:\\文档\\ai001';
const PORT = Number(process.env.PORT || 8787);
const APP = 'http://127.0.0.1:' + PORT + '/swiftdrop.html';
const OUT = path.join(ROOT, 'tmp', 'ui');
fs.mkdirSync(OUT, { recursive: true });

const TMP = path.join(ROOT, 'tmp', 'ui-data');
fs.rmSync(TMP, { recursive: true, force: true });
fs.mkdirSync(TMP, { recursive: true });
const FILES = [];
for (let i = 0; i < 3; i++) {
  const p = path.join(TMP, ['产品介绍-v2.pdf', '家庭照片打包.zip', '安装包.dmg'][i]);
  const fd = fs.openSync(p, 'w');
  let left = [6, 14, 22][i] * 1024 * 1024;
  while (left > 0) { const n = Math.min(left, 1048576); fs.writeSync(fd, crypto.randomBytes(n)); left -= n; }
  fs.closeSync(fd);
  FILES.push(p);
}

const browser = await chromium.launch({ channel: process.env.CH || 'msedge', headless: true });
const errors = [];

async function newPage({ width = 1280, height = 900, mobile = false, theme = 'light', dir = 'recv', lan = false } = {}) {
  const ctx = await browser.newContext({
    viewport: { width, height },
    deviceScaleFactor: 2,
    isMobile: mobile,
    hasTouch: mobile,
    colorScheme: theme
  });
  await ctx.addInitScript(`(() => {
    localStorage.setItem('zongxian.theme', ${JSON.stringify(theme)});
    window.__pickQueue = [${JSON.stringify(dir)}];
    window.showDirectoryPicker = async function () {
      const n = window.__pickQueue.shift() || 'recv';
      const root = await navigator.storage.getDirectory();
      return await root.getDirectoryHandle(n, { create: true });
    };
  })();`);
  const p = await ctx.newPage();
  p.on('pageerror', (e) => errors.push('[' + width + 'x' + height + '] ' + e.message));
  p.on('console', (m) => { if (m.type() === 'error') errors.push('[console] ' + m.text()); });
  // lan=true 模拟"局域网里朋友真能打开的地址"：这时链接/二维码才有效
  const url = APP + (lan ? '?lan=http://192.168.1.50:' + PORT + '/swiftdrop.html' : '');
  await p.goto(url);
  await p.waitForFunction(() => !!window.SDApp, null, { timeout: 30000 });
  await p.waitForTimeout(500);
  return p;
}

const shot = async (p, name) => {
  await p.screenshot({ path: path.join(OUT, name) });
  console.log('  saved ' + name);
};

/* ---------- 1. 首屏三张 ---------- */
console.log('[1] 首屏截图');
const light = await newPage({ theme: 'light' });
await shot(light, 'web-light-1280.png');
// 顺手截一张"生成取件码后"的浅色态（顺带验证取件码区在深色/浅色都不溢出）
await light.click('#btnCreate');
await light.waitForFunction(() => document.getElementById('codeText').textContent.trim().length >= 6, null, { timeout: 20000 });
await light.waitForTimeout(700);
await shot(light, 'web-light-code-1280.png');

const dark = await newPage({ theme: 'dark' });
await shot(dark, 'web-dark-1280.png');
await dark.click('#btnCreate');
await dark.waitForFunction(() => document.getElementById('codeText').textContent.trim().length >= 6, null, { timeout: 20000 });
await dark.waitForTimeout(700);
await shot(dark, 'web-dark-code-1280.png');

const mob = await newPage({ width: 390, height: 844, mobile: true, theme: 'light' });
await shot(mob, 'web-mobile-390.png');
await mob.click('#btnCreate');
await mob.waitForFunction(() => document.getElementById('codeText').textContent.trim().length >= 6, null, { timeout: 20000 });
await mob.waitForTimeout(700);
await shot(mob, 'web-mobile-390-code.png');

const mq = await newPage({ width: 390, height: 844, mobile: true, theme: 'light', lan: true });
await mq.click('#btnCreate');
await mq.waitForFunction(() => document.getElementById('codeText').textContent.trim().length >= 6, null, { timeout: 20000 });
await mq.waitForTimeout(700);
const overflow = await mq.evaluate(() => {
  const bad = [];
  document.querySelectorAll('#app *').forEach((el) => {
    if (el.scrollWidth > el.clientWidth + 2 && getComputedStyle(el).overflowX !== 'auto') bad.push(el.id || el.className);
  });
  return { docScroll: document.documentElement.scrollWidth, win: window.innerWidth, bad: bad.slice(0, 8) };
});
console.log('  手机端溢出检查：' + JSON.stringify(overflow));
await shot(mq, 'web-mobile-390-code-qr.png');

/* ---------- 2. 局域网地址下：链接 + 二维码有效（顺带验证取件码+二维码排布） ---------- */
console.log('[1b] 局域网地址（链接/二维码有效）');
const lanLight = await newPage({ theme: 'light', lan: true });
await lanLight.click('#btnCreate');
await lanLight.waitForFunction(() => document.getElementById('codeText').textContent.trim().length >= 6, null, { timeout: 20000 });
await lanLight.waitForTimeout(800);
await shot(lanLight, 'web-lan-code-light-1280.png');
const lanQr = await lanLight.evaluate(() => {
  const c = document.getElementById('qrCanvas');
  return { hidden: c.classList.contains('hidden'), w: c.width, h: c.height, cssW: Math.round(c.getBoundingClientRect().width) };
});
console.log('  二维码：' + JSON.stringify(lanQr));
const lanDark = await newPage({ theme: 'dark', lan: true });
await lanDark.click('#btnCreate');
await lanDark.waitForFunction(() => document.getElementById('codeText').textContent.trim().length >= 6, null, { timeout: 20000 });
await lanDark.waitForTimeout(800);
await shot(lanDark, 'web-lan-code-dark-1280.png');

/* ---------- 3. 已连接 + 传输中 + 诊断有数据 ---------- */
console.log('[2] 建连并传输');
const A = await newPage({ theme: 'light', dir: 'recvA', lan: true });
const B = await newPage({ theme: 'dark', dir: 'recvB' });
await A.click('#btnCreate');
await A.waitForFunction(() => document.getElementById('codeText').textContent.trim().length >= 6, null, { timeout: 20000 });
const code = (await A.textContent('#codeText')).trim();
await B.fill('#joinCode', code);
await B.click('#btnJoin');
let ok = true;
try {
  await Promise.all([
    A.waitForSelector('#sessionPane:not(.hidden)', { timeout: 60000, state: 'visible' }),
    B.waitForSelector('#sessionPane:not(.hidden)', { timeout: 60000, state: 'visible' })
  ]);
} catch (e) { ok = false; console.log('  连接失败：' + e.message); }

if (ok) {
  for (const p of [A, B]) { await p.click('#btnPickSaveDir'); await p.waitForFunction(() => !!window.SDApp.saveDir); }
  // 发三批文件：接收端能看到"进行中 + 完成"两种行状态
  await A.setInputFiles('#fileInput', FILES);
  // 等进度跑起来（有 run 状态 + 有字节增量）
  await B.waitForFunction(() => [...window.SDApp.tasks.values()].some((t) => t.dir === 'recv' && t.state === 'run' && t.got > 0), null, { timeout: 30000 }).catch(() => {});
  await A.waitForFunction(() => [...window.SDApp.tasks.values()].some((t) => t.dir === 'send' && t.state === 'run' && t.got > 0), null, { timeout: 30000 }).catch(() => {});
  await B.waitForTimeout(3200);   // 让诊断面板采到 2~3 个样本（每秒 1 次）
  const dg = await B.evaluate(() => ({
    samples: window.SDApp.Diag.samples.length,
    rtt: (document.getElementById('dgRtt').textContent || '').trim(),
    speed: (document.getElementById('dgSpeed').textContent || '').trim(),
    verdict: (document.getElementById('dgVerdict').textContent || '').trim().slice(0, 40)
  }));
  console.log('  诊断面板：' + JSON.stringify(dg));
  await shot(A, 'web-connected-light-1280.png');
  await shot(B, 'web-connected-dark-1280.png');
  await B.locator('.diag').screenshot({ path: path.join(OUT, 'web-diag-dark.png') });
  console.log('  saved web-diag-dark.png');
  await A.locator('.diag').screenshot({ path: path.join(OUT, 'web-diag-light.png') });
  console.log('  saved web-diag-light.png');
  // 手机竖屏下的会话界面
  const mobConn = await newPage({ width: 390, height: 844, mobile: true, theme: 'light', dir: 'recvM' });
  await mobConn.fill('#joinCode', code);
  await mobConn.click('#btnJoin');
  await mobConn.waitForSelector('#sessionPane:not(.hidden)', { timeout: 60000, state: 'visible' }).catch(() => {});
  await mobConn.waitForTimeout(1500);
  await shot(mobConn, 'web-mobile-390-session.png');
  // 设置对话框（浅色 + 深色）
  await A.evaluate(() => document.getElementById('settingsDialog').showModal());
  await A.waitForTimeout(400);
  await shot(A, 'web-settings-light.png');
  await B.evaluate(() => document.getElementById('settingsDialog').showModal());
  await B.waitForTimeout(400);
  await shot(B, 'web-settings-dark.png');
} else {
  await shot(A, 'web-connected-light-1280.png');
  await shot(B, 'web-connected-dark-1280.png');
}

console.log(errors.length ? '\n页面错误：\n' + errors.join('\n') : '\n无页面错误');
console.log('输出目录：' + OUT);
await browser.close();
process.exit(errors.length ? 1 : 0);
