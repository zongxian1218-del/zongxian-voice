/* 最简用法验证：双方都直接双击 swiftdrop.html（file:// 协议）互传
 * 接收端不选目录 → 走"内存接收 + 下载"路径（手机端也是这条路），从 Blob 回读校验。
 */
import { chromium } from 'playwright';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

const FILE = pathToFileURL(path.join(ROOT, 'dist', 'swiftdrop.html')).href;
const TMP = path.join(ROOT, 'tmp', 'file');
fs.rmSync(TMP, { recursive: true, force: true });
fs.mkdirSync(TMP, { recursive: true });
const F = path.join(TMP, 'file-protocol-test.bin');
{
  const fd = fs.openSync(F, 'w');
  let left = 8 * 1024 * 1024;
  while (left > 0) { const n = Math.min(left, 1048576); fs.writeSync(fd, crypto.randomBytes(n)); left -= n; }
  fs.closeSync(fd);
}
const want = crypto.createHash('sha256').update(fs.readFileSync(F)).digest('hex');

const browser = await chromium.launch({ channel: 'msedge', headless: true });
async function page(label) {
  const ctx = await browser.newContext({ acceptDownloads: true });
  const p = await ctx.newPage();
  p.on('pageerror', (e) => console.log('!! [' + label + '] ' + e.message));
  await p.goto(FILE);
  await p.waitForFunction(() => !!window.SDApp, null, { timeout: 20000 });
  return p;
}

const A = await page('A'), B = await page('B');
const caps = await B.evaluate(() => ({ secure: window.isSecureContext, subtle: typeof crypto.subtle, fsa: typeof window.showDirectoryPicker }));
const idbOk = await B.evaluate(async () => { try { return await SD.idbSet('misc', 'probe', { t: Date.now() }); } catch (e) { return 'err:' + e.message; } });
const opfsOk = await B.evaluate(async () => { try { await navigator.storage.getDirectory(); return true; } catch (e) { return 'err:' + e.name; } });
console.log(`file:// 能力：安全上下文=${caps.secure} crypto.subtle=${caps.subtle} 目录选择=${caps.fsa} IndexedDB写入=${idbOk} OPFS=${opfsOk}`);

await A.click('#btnCreate');
await A.waitForFunction(() => document.getElementById('codeText').textContent.trim().length >= 6, null, { timeout: 20000 });
const code = (await A.textContent('#codeText')).trim();
console.log('file:// 取件码：' + code);
await B.fill('#joinCode', code);
await B.click('#btnJoin');
let ok1 = true;
try {
  await Promise.all([
    A.waitForSelector('#sessionPane:not(.hidden)', { timeout: 60000, state: 'visible' }),
    B.waitForSelector('#sessionPane:not(.hidden)', { timeout: 60000, state: 'visible' })
  ]);
} catch (e) { ok1 = false; }
console.log((ok1 ? '[PASS] ' : '[FAIL] ') + 'file:// 双方直连建立' + (ok1 ? '' : '（超时）'));

if (ok1) {
  const t0 = Date.now();
  await A.setInputFiles('#fileInput', [F]);
  await B.waitForFunction(() => {
    const ts = [...window.SDApp.tasks.values()].filter((t) => t.dir === 'recv');
    return ts.length > 0 && ts.some((t) => t.state === 'done' || t.state === 'error');
  }, null, { timeout: 180000 });
  const ms = Date.now() - t0;
  const got = await B.evaluate(async () => {
    const t = [...window.SDApp.tasks.values()].find((x) => x.dir === 'recv');
    if (!t || !t.url) return { err: '没有收到下载链接', state: t && t.state, msg: t && t.msg };
    const buf = new Uint8Array(await (await fetch(t.url)).arrayBuffer());
    const h = await crypto.subtle.digest('SHA-256', buf);
    return { size: buf.length, hex: [...new Uint8Array(h)].map((x) => x.toString(16).padStart(2, '0')).join(''), state: t.state, msg: t.msg };
  });
  const good = got.hex === want;
  console.log((good ? '[PASS] ' : '[FAIL] ') + `file:// 传输 8MB 并校验（${(ms / 1000).toFixed(1)}s，${(8 / (ms / 1000)).toFixed(1)} MB/s，收到 ${got.size} 字节，哈希${good ? '一致' : '不一致'}，状态=${got.state}/${got.msg || got.err}）`);
}
await browser.close();
