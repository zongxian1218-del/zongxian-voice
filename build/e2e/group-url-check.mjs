/* 验证：桌面版把「异地组网地址」传给页面后，页面给出的链接/二维码就是组网地址 */
import { chromium } from 'playwright';
const PORT = 8823;
const GROUP = `http://26.10.20.30:${PORT}/swiftdrop.html`;
const LANL = `http://192.168.1.50:${PORT}/swiftdrop.html`;
const URL = `http://127.0.0.1:${PORT}/swiftdrop.html?lan=${encodeURIComponent(GROUP)}&lanlocal=${encodeURIComponent(LANL)}`;
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const page = await (await browser.newContext()).newPage();
await page.goto(URL);
await page.waitForFunction(() => !!window.SDApp, null, { timeout: 20000 });
await page.click('#btnCreate');
await page.waitForFunction(() => document.getElementById('codeText').textContent.trim().length >= 6, null, { timeout: 15000 });
await page.waitForTimeout(400);
const r = await page.evaluate(() => ({
  code: document.getElementById('codeText').textContent.trim(),
  link: document.getElementById('linkText').value,
  qrVisible: !document.getElementById('qrCanvas').classList.contains('hidden'),
  hint: (document.getElementById('shareHint').textContent || '').trim(),
  copyLabel: document.getElementById('btnCopyLink').textContent.trim()
}));
console.log(JSON.stringify(r, null, 2));
const okLink = r.link.startsWith(GROUP + '#c=');
const okHint = r.hint.includes('异地组网') && r.hint.includes('192.168.1.50');
const okQr = r.qrVisible;
console.log((okLink ? '[PASS] ' : '[FAIL] ') + '分享链接用的是异地组网地址（26.x）而不是局域网地址');
console.log((okHint ? '[PASS] ' : '[FAIL] ') + '同时提示了"同一 WiFi 下也可以用局域网地址"');
console.log((okQr ? '[PASS] ' : '[FAIL] ') + '异地组网地址的二维码可见（异地朋友可扫）');
await browser.close();
process.exit(okLink && okHint && okQr ? 0 : 1);
