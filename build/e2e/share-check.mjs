/* 验证：本地打开（file://）时，链接与二维码必须被隐藏，只留取件码 */
import { chromium } from 'playwright';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const FILE = pathToFileURL(path.join(ROOT, 'dist', 'swiftdrop.html')).href;
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const ctx = await browser.newContext();
const page = await ctx.newPage();
await page.goto(FILE);
await page.waitForFunction(() => !!window.SDApp, null, { timeout: 20000 });
await page.click('#btnCreate');
await page.waitForFunction(() => document.getElementById('codeText').textContent.trim().length >= 6, null, { timeout: 15000 });
await page.waitForTimeout(500);
const r = await page.evaluate(() => ({
  code: document.getElementById('codeText').textContent.trim(),
  linkRowHidden: document.getElementById('linkRow').classList.contains('hidden'),
  qrHidden: document.getElementById('qrCanvas').classList.contains('hidden'),
  copyLabel: document.getElementById('btnCopyLink').textContent.trim(),
  hint: (document.getElementById('shareHint').textContent || '').slice(0, 60),
  chip: document.getElementById('connChip').textContent.trim()
}));
console.log(JSON.stringify(r, null, 2));
const pass = r.linkRowHidden && r.qrHidden && r.copyLabel.includes('取件码') && r.hint.includes('对朋友无效');
console.log((pass ? '[PASS] ' : '[FAIL] ') + '本地打开时隐藏无效链接/二维码，改为"复制取件码"+明确说明');
await browser.close();
process.exit(pass ? 0 : 1);
