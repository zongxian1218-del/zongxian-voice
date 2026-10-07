/* 验证桌面版内置窗口传给页面的局域网地址（?lan=）会被正确用作分享链接/二维码 */
import { chromium } from 'playwright';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const LAN = 'http://192.168.1.50:8787/swiftdrop.html';
const URL = 'file:///D:/文档/ai001/dist/swiftdrop.html?lan=' + encodeURIComponent(LAN);
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const page = await (await browser.newContext()).newPage();
await page.goto(URL);
await page.waitForFunction(() => !!window.SDApp, null, { timeout: 20000 });
await page.click('#btnCreate');
await page.waitForFunction(() => document.getElementById('codeText').textContent.trim().length >= 6, null, { timeout: 15000 });
await page.waitForTimeout(400);
const r = await page.evaluate(() => ({
  code: document.getElementById('codeText').textContent.trim(),
  linkRowHidden: document.getElementById('linkRow').classList.contains('hidden'),
  qrHidden: document.getElementById('qrCanvas').classList.contains('hidden'),
  copyLabel: document.getElementById('btnCopyLink').textContent.trim(),
  link: document.getElementById('linkText').value
}));
console.log(JSON.stringify(r, null, 2));
const pass = !r.linkRowHidden && !r.qrHidden && r.link.startsWith(LAN + '#c=') && r.copyLabel === '复制链接';
console.log((pass ? '[PASS] ' : '[FAIL] ') + '内置窗口里链接/二维码用的是局域网地址（朋友在同一 WiFi 下可直接打开）');
await browser.close();
process.exit(pass ? 0 : 1);
