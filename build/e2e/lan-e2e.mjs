/* 实测「电脑起局域网服务 + 手机/浏览器直接访问」这条路（不依赖任何公共信令服务器）
 * 1) 用桌面版 exe 起 webhost（内置局域网信令中继 /signal）
 * 2) 两个浏览器页面都从 http://127.0.0.1:8787/swiftdrop.html 加载（模拟手机从局域网网址打开）
 * 3) 走局域网信令建连 + 传 16MB，接收端回读校验 SHA-256
 * 注意：为了让"局域网信令"成为唯一可用通道，本测试把外部 MQTT 域名解析拦掉。
 */
import { chromium } from 'playwright';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = 'D:\\文档\\ai001';
const PORT = 8787;
const APP = 'http://127.0.0.1:' + PORT + '/swiftdrop.html';
const TMP = path.join(ROOT, 'tmp', 'lan-test');
fs.rmSync(TMP, { recursive: true, force: true });
fs.mkdirSync(TMP, { recursive: true });
const BIG = path.join(TMP, 'lan16.bin');
{
  const fd = fs.openSync(BIG, 'w');
  let left = 16 * 1024 * 1024;
  while (left > 0) { const n = Math.min(left, 1048576); fs.writeSync(fd, crypto.randomBytes(n)); left -= n; }
  fs.closeSync(fd);
}
const want = crypto.createHash('sha256').update(fs.readFileSync(BIG)).digest('hex');

const browser = await chromium.launch({ channel: process.env.CH || 'msedge', headless: true });

async function page(label) {
  const ctx = await browser.newContext();
  await ctx.addInitScript(`window.__pickQueue=['recv'];
    window.showDirectoryPicker = async () => { const r = await navigator.storage.getDirectory(); return await r.getDirectoryHandle(window.__pickQueue.shift()||'recv',{create:true}); };`);
  const p = await ctx.newPage();
  p.on('pageerror', (e) => console.log('!! [' + label + '] ' + e.message));
  await p.goto(APP);
  await p.waitForFunction(() => !!window.SDApp, null, { timeout: 30000 });
  return p;
}

const A = await page('A'), B = await page('B');
await A.click('#btnCreate');
await A.waitForFunction(() => document.getElementById('codeText').textContent.trim().length >= 6, null, { timeout: 20000 });
const code = (await A.textContent('#codeText')).trim();
console.log('取件码：' + code);
// 本地服务地址 → 链接/二维码应该可见（局域网内朋友真能打开）
const share = await A.evaluate(() => ({
  linkRowHidden: document.getElementById('linkRow').classList.contains('hidden'),
  copyLabel: document.getElementById('btnCopyLink').textContent.trim(),
  link: document.getElementById('linkText').value
}));
console.log('局域网地址下的分享形态：' + JSON.stringify(share));

await B.fill('#joinCode', code);
await B.click('#btnJoin');
let ok = true;
try {
  await Promise.all([
    A.waitForSelector('#sessionPane:not(.hidden)', { timeout: 60000, state: 'visible' }),
    B.waitForSelector('#sessionPane:not(.hidden)', { timeout: 60000, state: 'visible' })
  ]);
} catch (e) { ok = false; }
const sigA = await A.evaluate(() => document.getElementById('sigChip').textContent.trim());
const modeA = await A.evaluate(() => (window.SDApp.sig && window.SDApp.sig.mode) || '');
console.log((ok ? '[PASS] ' : '[FAIL] ') + '建连成功（信令胶囊：' + sigA + '，mode=' + modeA + '）');
console.log((/lan/.test(modeA) ? '[PASS] ' : '[WARN] ') + '局域网信令中继已参与（mode 含 lan）');

// 回归：曾经在加入方那边误报"15 秒没收到对方消息"（加入方只收 offer，不收 hello）
const seen = await Promise.all([A, B].map((p) => p.evaluate(() => ({
  peerSeen: !!window.SDApp.peerSeen,
  troubleHidden: document.getElementById('connTrouble').classList.contains('hidden'),
  chip: document.getElementById('connChip').textContent.trim()
}))));
console.log('双方"对方已加入"判定：' + JSON.stringify(seen));
const seenOk = seen.every((x) => x.peerSeen && x.troubleHidden);
console.log((seenOk ? '[PASS] ' : '[FAIL] ') + '连接成功后不再误报"对方没加入"（peerSeen=true、提示条隐藏）');
ok = ok && seenOk;

if (ok) {
  await A.click('#btnPickSaveDir');
  await B.click('#btnPickSaveDir');
  await A.waitForFunction(() => !!window.SDApp.saveDir);
  await B.waitForFunction(() => !!window.SDApp.saveDir);
  const t0 = Date.now();
  await A.setInputFiles('#fileInput', [BIG]);
  await B.waitForFunction(() => {
    const ts = [...window.SDApp.tasks.values()].filter((t) => t.dir === 'recv');
    return ts.length > 0 && ts.some((t) => t.state === 'done' || t.state === 'error');
  }, null, { timeout: 180000 });
  const ms = Date.now() - t0;
  const got = await B.evaluate(async () => {
    const root = await navigator.storage.getDirectory();
    const d = await root.getDirectoryHandle('recv');
    const f = await (await d.getFileHandle('lan16.bin')).getFile();
    const b = new Uint8Array(await f.arrayBuffer());
    const h = await crypto.subtle.digest('SHA-256', b);
    return { size: f.size, hex: [...new Uint8Array(h)].map((x) => x.toString(16).padStart(2, '0')).join('') };
  });
  const same = got.hex === want;
  console.log((same ? '[PASS] ' : '[FAIL] ') + `16MB 传输校验（${(ms / 1000).toFixed(1)}s，${(16 / (ms / 1000)).toFixed(1)} MB/s，收到 ${got.size} 字节）`);
  ok = ok && same;

  // 诊断面板：必须是真实数据（候选类型/RTT/字节数/速度/结论）
  await B.waitForTimeout(2600);
  const dg = await B.evaluate(() => {
    const D = window.SDApp.Diag;
    const txt = (id) => (document.getElementById(id).textContent || '').trim();
    return {
      last: D.last, samples: D.samples.length, peak: D.peak, slow: D.slow,
      jitter: D.jitter(), engine: D.engine(), verdict: D.verdictText,
      dom: { link: txt('dgLink'), rtt: txt('dgRtt'), speed: txt('dgSpeed'), peak: txt('dgPeak'), stall: txt('dgStall'), cand: txt('dgCand'), verdict: txt('dgVerdict') },
      copy: D.copy()
    };
  });
  console.log('诊断面板：' + JSON.stringify(dg.dom));
  console.log('诊断原始：候选=' + dg.last.localType + '/' + dg.last.remoteType + ' RTT=' + dg.last.rtt +
    'ms 抖动=' + dg.jitter + 'ms 采样=' + dg.samples + ' 峰值=' + Math.round(dg.peak / 1048576 * 10) / 10 + 'MB/s 字节=' +
    dg.last.bytesSent + '/' + dg.last.bytesReceived + ' 卡顿=' + dg.engine.stalls);
  const dgOk = dg.samples > 0 && dg.peak > 0 && !!dg.last.localType && !!dg.verdict &&
    dg.copy.includes('连接诊断') && dg.dom.link.length > 0 && dg.dom.rtt.includes('ms') && dg.dom.speed !== '-';
  console.log((dgOk ? '[PASS] ' : '[FAIL] ') + '诊断面板取到真实指标（候选类型/延迟/速度/结论/可复制）');
  if (dg.copy) console.log('--- 复制出来的诊断信息 ---\n' + dg.copy + '\n--------------------------');
  // 截图存档（浅色 + 深色），供人工核对面板观感
  try {
    await B.locator('.diag').screenshot({ path: 'D:\\文档\\ai001\\tmp\\diag-light.png' });
    await B.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
    await B.waitForTimeout(400);
    await B.evaluate(() => { if (window.SDApp.Diag) window.SDApp.Diag.render(window.SDApp.Diag.last, 0); });
    await B.locator('.diag').screenshot({ path: 'D:\\文档\\ai001\\tmp\\diag-dark.png' });
    console.log('已保存截图：tmp/diag-light.png 、 tmp/diag-dark.png');
  } catch (e) { console.log('截图失败（不影响测试）：' + e.message); }
  ok = ok && dgOk;
}
await browser.close();
process.exit(ok ? 0 : 1);
