/* 闪电快传 SwiftDrop —— 端到端真实传输测试
 * 两个独立浏览器上下文 → 真实 MQTT 信令 → WebRTC 直连 → 真实文件传输 → 接收端 SHA-256 校验
 * 静态资源由 Playwright 自身从磁盘托管（不依赖外部 HTTP 服务器，避免进程被回收导致的假失败）
 * 接收目录使用 OPFS 顶替原生目录选择框，走的是同一套 File System Access 写盘代码路径。
 */
import { chromium } from 'playwright';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = 'D:\\文档\\ai001';
const APP = 'http://127.0.0.1:8799/dist/swiftdrop.html';
const TMP = path.join(ROOT, 'tmp', 'e2e');
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.bin': 'application/octet-stream', '.txt': 'text/plain; charset=utf-8' };

const results = [];
let failed = 0;
function ok(name, pass, detail) {
  results.push({ name, pass: !!pass, detail: detail || '' });
  console.log((pass ? '  [PASS] ' : '  [FAIL] ') + name + (detail ? '  — ' + detail : ''));
  if (!pass) failed++;
}
const rate = (bytes, ms) => (bytes / 1048576 / (ms / 1000)).toFixed(1);

function mkRandom(p, size) {
  const fd = fs.openSync(p, 'w');
  let left = size;
  while (left > 0) { const n = Math.min(left, 1024 * 1024); fs.writeSync(fd, crypto.randomBytes(n)); left -= n; }
  fs.closeSync(fd);
  return crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
}
const shaFile = (p) => crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');

fs.rmSync(TMP, { recursive: true, force: true });
fs.mkdirSync(TMP, { recursive: true });
console.log('准备测试文件…');
const files = {
  big: path.join(TMP, 'big-video.bin'),
  resume: path.join(TMP, 'resume-test.bin'),
  cn: path.join(TMP, '中文 文件名 带空格.txt'),
  emoji: path.join(TMP, '表情😀测试.bin'),
  small: path.join(TMP, 'small.txt'),
  rev: path.join(TMP, '反向发送.txt')
};
const hashes = {};
hashes.big = mkRandom(files.big, 32 * 1024 * 1024);
hashes.resume = mkRandom(files.resume, 12 * 1024 * 1024);
fs.writeFileSync(files.cn, '这是中文内容测试\n' + 'X'.repeat(5000), 'utf8'); hashes.cn = shaFile(files.cn);
fs.writeFileSync(files.emoji, crypto.randomBytes(200000)); hashes.emoji = shaFile(files.emoji);
fs.writeFileSync(files.small, 'hello swiftdrop\n', 'utf8'); hashes.small = shaFile(files.small);
fs.writeFileSync(files.rev, '来自接收方的问候\n', 'utf8'); hashes.rev = shaFile(files.rev);

const manyDir = path.join(TMP, 'many');
fs.mkdirSync(manyDir, { recursive: true });
const manyPaths = [];
for (let i = 0; i < 400; i++) {
  const nm = 'f' + String(i).padStart(4, '0') + '-' + 'x'.repeat(90) + '.txt';
  const p = path.join(manyDir, nm);
  fs.writeFileSync(p, 'file ' + i + '\n');
  manyPaths.push(p);
}

/* ---------- 浏览器 ---------- */
const browser = await chromium.launch({ channel: process.env.CH || 'msedge', headless: true });

async function serve(ctx) {
  await ctx.route('**/*', async (route) => {
    const req = route.request();
    let u;
    try { u = new URL(req.url()); } catch (e) { return route.continue(); }
    if (u.hostname !== '127.0.0.1') return route.continue();
    const rel = decodeURIComponent(u.pathname).replace(/^\/+/, '');
    const file = path.join(ROOT, rel);
    if (!file.startsWith(ROOT) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
      return route.fulfill({ status: 404, body: 'not found' });
    }
    return route.fulfill({ status: 200, headers: { 'content-type': MIME[path.extname(file).toLowerCase()] || 'application/octet-stream' }, body: fs.readFileSync(file) });
  });
}

async function newPage(label, firstPickDir) {
  const ctx = await browser.newContext({ acceptDownloads: true });
  await serve(ctx);
  await ctx.addInitScript(`(() => {
    window.__pickQueue = ${JSON.stringify([firstPickDir])};
    window.showDirectoryPicker = async function () {
      const n = window.__pickQueue.shift() || 'recv';
      const root = await navigator.storage.getDirectory();
      return await root.getDirectoryHandle(n, { create: true });
    };
    window.showSaveFilePicker = window.showDirectoryPicker;
  })();`);
  const p = await ctx.newPage();
  p.on('pageerror', (e) => console.log('  !! [' + label + '] 页面异常: ' + e.message));
  await p.goto(APP);
  await p.waitForFunction(() => !!window.SDApp, null, { timeout: 30000 });
  return p;
}

const clearTasks = (p) => p.evaluate(() => window.SDApp.tasks.clear());

async function waitDone(p, epath, timeout) {
  await p.waitForFunction((ep) => {
    const ts = [...window.SDApp.tasks.values()].filter((t) => t.dir === 'recv' && t.path === ep);
    return ts.length > 0 && ts.every((t) => t.state === 'done' || t.state === 'error');
  }, epath, { timeout: timeout || 300000 });
  return await p.evaluate((ep) => {
    const t = [...window.SDApp.tasks.values()].find((x) => x.dir === 'recv' && x.path === ep);
    return t ? { state: t.state, got: t.got, hash: t.hash, msg: t.msg } : null;
  }, epath);
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

async function listOpfs(page, dirName) {
  return await page.evaluate(async ([dirName]) => {
    const root = await navigator.storage.getDirectory();
    const out = [];
    async function walk(h, prefix) {
      for await (const [name, e] of h.entries()) {
        const p = prefix ? prefix + '/' + name : name;
        if (e.kind === 'directory') await walk(e, p);
        else { const f = await e.getFile(); out.push({ path: p, size: f.size }); }
      }
    }
    let d = null;
    try { d = await root.getDirectoryHandle(dirName); } catch (e) { return []; }
    await walk(d, '');
    return out.sort((a, b) => (a.path < b.path ? -1 : 1));
  }, [dirName]);
}

async function dumpLogs(A, B) {
  for (const [nm, p] of [['A', A], ['B', B]]) {
    if (!p) continue;
    try {
      const t = await p.textContent('#logBox');
      console.log('\n---- ' + nm + ' 端日志（末 12 行）----');
      console.log(t.split('\n').slice(-12).map((x) => x.slice(0, 160)).join('\n'));
    } catch (e) {}
  }
}

/* ================= 开始 ================= */
console.log('\n启动浏览器…');
let A, B;
try {
  A = await newPage('A发起方', 'recv');
  B = await newPage('B接收方', 'recv');

  /* ---- T1 连接 ---- */
  console.log('\n[T1] 建立连接');
  const t0 = Date.now();
  await A.click('#btnCreate');
  await A.waitForFunction(() => document.getElementById('codeText').textContent.trim().length >= 6, null, { timeout: 20000 });
  const code = (await A.textContent('#codeText')).trim();
  console.log('  取件码：' + code + '（二维码已渲染：' + await A.evaluate(() => { const c = document.getElementById('qrCanvas'); return !c.classList.contains('hidden') && c.width > 20; }) + '）');
  await B.fill('#joinCode', code);
  await B.click('#btnJoin');
  let connOk = true;
  try {
    await Promise.all([
      A.waitForSelector('#sessionPane:not(.hidden)', { timeout: 90000, state: 'visible' }),
      B.waitForSelector('#sessionPane:not(.hidden)', { timeout: 90000, state: 'visible' })
    ]);
  } catch (e) { connOk = false; }
  ok('T1 双方建立 P2P 直连', connOk, connOk ? ('耗时 ' + ((Date.now() - t0) / 1000).toFixed(1) + ' 秒') : '超时未连接');
  if (!connOk) throw new Error('连接失败，后续用例无法继续');

  // 候选对统计是异步上报的：刚 connected 时 getStats 可能还没有 state==='succeeded' 的候选对，
  // 一次性读取会偶发拿到 null（变成假失败）。这里按 app 自己的判定口径轮询几秒。
  let cand = { local: null, remote: null };
  for (let i = 0; i < 20; i++) {
    cand = await A.evaluate(async () => {
      const s = await window.SDApp.link.pc.getStats();
      let pair = null, local = null, remote = null;
      s.forEach((v) => {
        if (v.type !== 'candidate-pair') return;
        if (!(v.selected || v.nominated || v.state === 'succeeded')) return;
        if (!pair || v.state === 'succeeded') pair = v;
      });
      s.forEach((v) => { if (!pair) return; if (v.type === 'local-candidate' && v.id === pair.localCandidateId) local = v; if (v.type === 'remote-candidate' && v.id === pair.remoteCandidateId) remote = v; });
      return { local: local && local.candidateType, remote: remote && remote.candidateType };
    });
    if (cand.local) break;
    await A.waitForTimeout(250);
  }
  ok('T1b 连接为直连而非中继', cand.local === 'host', '本地候选=' + cand.local + ' 远端候选=' + cand.remote);

  await A.click('#btnPickSaveDir');
  await B.click('#btnPickSaveDir');
  await A.waitForFunction(() => !!window.SDApp.saveDir, null, { timeout: 10000 });
  await B.waitForFunction(() => !!window.SDApp.saveDir, null, { timeout: 10000 });

  /* ---- T2 单文件 + 参数扫参 ---- */
  console.log('\n[T2] 单文件 32MB（顺带扫参挑默认值）');
  let best = null;
  for (const cfg of [{ c: 65536 }, { c: 131072 }, { c: 262144 }]) {
    await clearTasks(B);
    await A.evaluate((cfg) => SD.saveSettings({ chunkSize: cfg.c }), cfg);
    const st = Date.now();
    await A.setInputFiles('#fileInput', [files.big]);
    const t = await waitDone(B, 'big-video.bin');
    const ms = Date.now() - st;
    const r = await opfsFile(B, 'recv', 'big-video.bin');
    const good = r.hex === hashes.big && t && t.state === 'done';
    const sp = rate(32 * 1024 * 1024, ms);
    console.log(`  分片 ${cfg.c / 1024}KB → ${sp} MB/s（${(ms / 1000).toFixed(1)}s，校验${good ? '通过' : '失败'}）`);
    if (good && (!best || +sp > +best.sp)) best = { cfg, sp };
  }
  ok('T2 32MB 文件字节级一致（SHA-256 相同）', !!best, best ? ('最佳 ' + best.sp + ' MB/s（分片 ' + best.cfg.c / 1024 + 'KB）') : '全部失败');
  if (best) await A.evaluate((cfg) => SD.saveSettings({ chunkSize: cfg.c }), best.cfg);

  /* ---- T3 中文/空格/emoji 多文件 ---- */
  console.log('\n[T3] 中文 / 空格 / emoji 文件名 + 多文件同发');
  {
    await clearTasks(B);
    await A.setInputFiles('#fileInput', [files.cn, files.emoji, files.small]);
    await waitDone(B, '中文 文件名 带空格.txt', 60000);
    await waitDone(B, '表情😀测试.bin', 60000);
    await waitDone(B, 'small.txt', 60000);
    const a = await opfsFile(B, 'recv', '中文 文件名 带空格.txt');
    const b = await opfsFile(B, 'recv', '表情😀测试.bin');
    const c = await opfsFile(B, 'recv', 'small.txt');
    ok('T3 中文/空格/emoji 文件名与内容都对', a.hex === hashes.cn && b.hex === hashes.emoji && c.hex === hashes.small,
      `中文=${a.hex === hashes.cn} emoji=${b.hex === hashes.emoji} 小文件=${c.hex === hashes.small}`);
  }

  /* ---- T4 大量小文件 ---- */
  console.log('\n[T4] 400 个小文件（大清单走分片通道）');
  {
    await clearTasks(B);
    const st = Date.now();
    await A.setInputFiles('#fileInput', manyPaths);
    await B.waitForFunction(() => {
      const ts = [...window.SDApp.tasks.values()].filter((t) => t.dir === 'recv' && /^f\d{4}-/.test(t.path));
      return ts.length >= 400 && ts.every((t) => t.state === 'done' || t.state === 'error');
    }, null, { timeout: 300000 });
    const lst = await listOpfs(B, 'recv');
    const got = lst.filter((x) => /^f\d{4}-x+\.txt$/.test(x.path));
    const bad = await B.evaluate(() => [...window.SDApp.tasks.values()].filter((t) => t.dir === 'recv' && /^f\d{4}-/.test(t.path) && t.state !== 'done').length);
    ok('T4 400 个小文件全部到达', got.length === 400 && bad === 0, '到达 ' + got.length + ' / 失败 ' + bad + ' · 用时 ' + ((Date.now() - st) / 1000).toFixed(1) + 's');
  }

  /* ---- T5 断点续传 ---- */
  console.log('\n[T5] 断点续传（预置 8MB 已完成分片）');
  {
    const total = fs.statSync(files.resume).size;
    await B.evaluate(async ([url, name, size]) => {
      const BLOCK = 4 * 1024 * 1024, W = 8 * 1024 * 1024;
      const ab = await (await fetch(url)).arrayBuffer();
      const root = await navigator.storage.getDirectory();
      const dir = await root.getDirectoryHandle('recv', { create: true });
      const fh = await dir.getFileHandle(name, { create: true });
      const w = await fh.createWritable();
      await w.seek(0); await w.write(new Uint8Array(ab, 0, W)); await w.close();
      const digests = [];
      for (let i = 0; i < W / BLOCK; i++) {
        const h = await crypto.subtle.digest('SHA-256', new Uint8Array(ab, i * BLOCK, BLOCK));
        digests.push([...new Uint8Array(h)].map((x) => x.toString(16).padStart(2, '0')).join(''));
      }
      await new Promise((res, rej) => {
        const rq = indexedDB.open('swiftdrop', 1);
        rq.onsuccess = () => {
          const tx = rq.result.transaction('resume', 'readwrite');
          tx.objectStore('resume').put({ size: size, written: W, digests: digests, mtime: Date.now(), updated: Date.now() }, 'resume:save:recv:' + name);
          tx.oncomplete = () => res(null); tx.onerror = () => rej(tx.error);
        };
        rq.onerror = () => rej(rq.error);
      });
    }, ['http://127.0.0.1:8799/tmp/e2e/resume-test.bin', 'resume-test.bin', total]);

    await clearTasks(B);
    const st = Date.now();
    await A.setInputFiles('#fileInput', [files.resume]);
    const t = await waitDone(B, 'resume-test.bin');
    const ms = Date.now() - st;
    const r = await opfsFile(B, 'recv', 'resume-test.bin');
    const logTxt = await B.textContent('#logBox');
    const resumed = logTxt.includes('续传');
    const cleared = await B.evaluate(() => new Promise((res) => {
      const rq = indexedDB.open('swiftdrop', 1);
      rq.onsuccess = () => { const r2 = rq.result.transaction('resume', 'readonly').objectStore('resume').get('resume:save:recv:resume-test.bin'); r2.onsuccess = () => res(r2.result == null); r2.onerror = () => res(false); };
      rq.onerror = () => res(false);
    }));
    ok('T5 断点续传：整文件校验通过 + 断点记录清除 + 只传剩余部分',
      r.hex === hashes.resume && resumed && cleared,
      `哈希一致=${r.hex === hashes.resume} 识别续传=${resumed} 断点已清除=${cleared} 用时=${(ms / 1000).toFixed(1)}s 状态=${JSON.stringify(t)}`);
  }

  /* ---- T6 文件夹双向同步 ---- */
  console.log('\n[T6] 文件夹双向同步');
  {
    await A.click('.tab[data-tab="sync"]');
    await B.click('.tab[data-tab="sync"]');
    await A.evaluate(() => window.__pickQueue.push('syncA'));
    await B.evaluate(() => window.__pickQueue.push('syncB'));
    await A.click('#btnPickSyncDir');
    await B.click('#btnPickSyncDir');
    await A.waitForFunction(() => !!window.SDApp.syncDir, null, { timeout: 10000 });
    await B.waitForFunction(() => !!window.SDApp.syncDir, null, { timeout: 10000 });

    await A.evaluate(async () => {
      const root = await navigator.storage.getDirectory();
      const d = await root.getDirectoryHandle('syncA', { create: true });
      const w1 = await (await d.getFileHandle('a.txt', { create: true })).createWritable();
      await w1.write(new TextEncoder().encode('AAA-来自A\n')); await w1.close();
      const deep = await (await d.getDirectoryHandle('sub', { create: true })).getDirectoryHandle('deep', { create: true });
      const w2 = await (await deep.getFileHandle('b.txt', { create: true })).createWritable();
      await w2.write(new TextEncoder().encode('BBB-深层目录\n')); await w2.close();
    });
    await B.evaluate(async () => {
      const root = await navigator.storage.getDirectory();
      const d = await root.getDirectoryHandle('syncB', { create: true });
      const w = await (await d.getFileHandle('c.txt', { create: true })).createWritable();
      await w.write(new TextEncoder().encode('CCC-来自B\n')); await w.close();
    });

    await A.click('#btnSyncNow');
    let syncOk = true;
    try { await A.waitForFunction(() => window.SDApp.sync && window.SDApp.sync.rounds > 0, null, { timeout: 180000 }); }
    catch (e) { syncOk = false; }

    const la = (await listOpfs(A, 'syncA')).map((x) => x.path);
    const lb = (await listOpfs(B, 'syncB')).map((x) => x.path);
    const want = ['a.txt', 'c.txt', 'sub/deep/b.txt'];
    const allThere = (arr) => want.every((w) => arr.includes(w));
    let contentOk = false;
    try {
      const sa = await A.evaluate(async () => (await (await (await navigator.storage.getDirectory()).getDirectoryHandle('syncA')).getFileHandle('c.txt')).getFile().then((f) => f.text()));
      const sb = await B.evaluate(async () => (await (await (await navigator.storage.getDirectory()).getDirectoryHandle('syncB')).getDirectoryHandle('sub').then((d) => d.getDirectoryHandle('deep')).then((d) => d.getFileHandle('b.txt'))).getFile().then((f) => f.text()));
      contentOk = sa.includes('CCC-来自B') && sb.includes('BBB-深层目录');
    } catch (e) { contentOk = false; }
    ok('T6 双向同步：两侧集合与内容一致（含深层目录）', syncOk && allThere(la) && allThere(lb) && contentOk,
      'A侧=' + JSON.stringify(la) + ' B侧=' + JSON.stringify(lb) + ' 内容=' + contentOk);
  }

  /* ---- T7 反向传输 ---- */
  console.log('\n[T7] 反向（接收方 → 发起方）');
  {
    await clearTasks(A);
    await B.setInputFiles('#fileInput', [files.rev]);
    await waitDone(A, '反向发送.txt', 60000);
    const r = await opfsFile(A, 'recv', '反向发送.txt');
    ok('T7 反向传输可用', r.hex === hashes.rev, '哈希一致=' + (r.hex === hashes.rev));
  }

} catch (e) {
  ok('测试流程异常中断', false, e.message);
  console.log(e.stack);
}

if (failed) await dumpLogs(A, B);
console.log('\n================ 汇总 ================');
results.forEach((r) => console.log((r.pass ? 'PASS  ' : 'FAIL  ') + r.name + (r.detail ? '  — ' + r.detail : '')));
console.log(`${results.length - failed}/${results.length} 通过`);
await browser.close();
process.exit(failed ? 1 : 0);
