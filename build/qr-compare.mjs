/**
 * qr-compare.mjs —— 把 src/web/qr.js 的矩阵与 qrcode(Python) 参考实现逐模块比对
 *
 * 运行：
 *   node build/qr-compare.mjs
 *
 * 断言：
 *   1) 每个用例的 size / 版本 / 每一位模块都与参考实现完全相同（含掩码自动选择）
 *   2) modules 是 size*size 的 Uint8Array，取值只有 0/1；get(x,y) 与 modules 一致
 *   3) UMD 的浏览器分支（window.QR）可用
 *   4) toString / toCanvas 的基本形状正确
 *   5) 超出容量时抛出明确错误
 */
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const QR_PATH = path.join(ROOT, 'src', 'web', 'qr.js');
const PY = path.join(HERE, 'qrvenv', 'Scripts', 'python.exe');

const require = createRequire(import.meta.url);
const QR = require(QR_PATH);

/* ------------------------------------------------------------------ */
/* 1. 测试用例                                                         */
/* ------------------------------------------------------------------ */

const STRINGS = [
  // 边界
  '',
  'A',
  'AB',
  ' ',
  '\n\t',
  // 纯 ASCII / 数字 / 大小写
  '0',
  '1234567890',
  '12345678901234567890123456789012345678901234567890',
  'H',
  'HELLO WORLD',
  'Hello, World!',
  'a1B2c3D4e5F6g7H8',
  'The quick brown fox jumps over the lazy dog.',
  "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~",
  // URL 形式（含 # & = - _）
  'https://example.com/',
  'https://example.com/path?a=1&b=2#frag',
  'https://example.com/a-b_c~d.e?x=1&y=2&z=3#section-2',
  'https://www.example.com/search?q=%E4%BA%8C%E7%BB%B4%E7%A0%81&lang=zh-CN',
  'ftp://user:pass@host:21/dir/file.txt',
  'mailto:someone@example.com?subject=Hi&body=There',
  // 中文 / 多语言
  '中',
  '中文',
  '你好，世界',
  '二维码生成模块测试',
  '这是一段较长的中文文本，用于测试多字节 UTF-8 编码在二维码字节模式下的填充与纠错处理是否正确。',
  '中英混排 mixed content 123 ABC',
  '日本語のテスト',
  '한국어 테스트',
  'Ω≈ç√∫˜µ≤≥÷',
  'ÄÖÜäöüß',
  // emoji
  '😀',
  '👍🏽',
  '🎉🎊✨',
  'emoji与中文混排😀🚀',
  'I ❤️ QR',
  // 国际化域名 / 路径
  'http://例子.测试/路径?查询=值#片段',
  // 长度阶梯（逼近各版本容量）
  'A'.repeat(100),
  'x'.repeat(200),
  '0123456789'.repeat(10),
  'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  '数据'.repeat(30),
  'https://example.com/very/long/path?token=' + 'a1b2c3d4'.repeat(8),
];

// EC 等级 M 下各版本的最大字节数（用于打版本选择边界）
const M_LIMITS = [14, 26, 42, 62, 84, 106, 122, 152, 180, 213];

const cases = [];
const push = (text, ec, tag) =>
  cases.push({
    id: `${tag}#${cases.length.toString().padStart(3, '0')}`,
    text,
    ec,
    tag,
  });

for (const s of STRINGS) push(s, 'M', 'strings');

// 每个版本容量的边界：正好装下 + 差 1 字节装不下（后者会自动进下一版本）
for (const n of M_LIMITS) {
  push('a'.repeat(n), 'M', 'vlimit-exact');
  push('a'.repeat(n + 1), 'M', 'vlimit-plus1');
}
push('a'.repeat(14), 'M', 'vlimit-exact'); // v1 满
push('汉'.repeat(70), 'M', 'vlimit-cjk'); // 210 字节，逼近 v10

// 四个 EC 等级 × 代表性内容
const EC_CASES = [
  '',
  'A',
  'HELLO WORLD',
  'https://example.com/a?b=1&c=2#d',
  '你好，世界',
  '二维码生成模块测试😀',
  '0123456789'.repeat(4),
  'x'.repeat(100),
];
for (const ec of ['L', 'Q', 'H']) {
  for (const s of EC_CASES) push(s, ec, 'ec-' + ec);
}
// EC=L 的 v10 边界
push('a'.repeat(271), 'L', 'vlimit-exact');
push('a'.repeat(151), 'Q', 'vlimit-exact');
push('a'.repeat(119), 'H', 'vlimit-exact');

/* ------------------------------------------------------------------ */
/* 2. 调用参考实现                                                     */
/* ------------------------------------------------------------------ */

const casesFile = path.join(HERE, 'qr-cases.json');
const refFile = path.join(HERE, 'qr-ref-out.json');
writeFileSync(casesFile, JSON.stringify(cases.map(({ id, text, ec }) => ({ id, text, ec }))), 'utf8');

const run = spawnSync(PY, [path.join(HERE, 'qr-ref.py'), casesFile, refFile], {
  encoding: 'utf8',
});
if (run.stdout) process.stdout.write(run.stdout);
if (run.stderr) process.stderr.write(run.stderr);
if (run.status !== 0) {
  console.error('参考实现执行失败，退出码', run.status);
  process.exit(1);
}
const refById = new Map(JSON.parse(readFileSync(refFile, 'utf8')).map((r) => [r.id, r]));

/* ------------------------------------------------------------------ */
/* 3. 逐模块比对                                                       */
/* ------------------------------------------------------------------ */

function rowsOf(res) {
  const out = [];
  for (let y = 0; y < res.size; y++) {
    let s = '';
    for (let x = 0; x < res.size; x++) s += res.get(x, y) ? '1' : '0';
    out.push(s);
  }
  return out;
}

// 从最终矩阵反读格式信息，反推 EC 位与掩码号
function readFormat(res) {
  let bits = 0;
  for (let i = 0; i < 15; i++) {
    let bit;
    if (i < 6) bit = res.get(8, i);
    else if (i < 8) bit = res.get(8, i + 1);
    else bit = res.get(8, res.size - 15 + i);
    if (bit) bits |= 1 << i;
  }
  const data = (bits ^ 0x5412) >> 10;
  return { ecBits: (data >> 3) & 3, mask: data & 7 };
}

let mismatchCases = 0;
let mismatchCells = 0;
let compared = 0;
let expectedOverflow = 0;
const failures = [];
const mineForScan = [];

for (const c of cases) {
  const ref = refById.get(c.id);
  let res;
  try {
    res = QR.encode(c.text, { ec: c.ec });
  } catch (err) {
    // 本模块的规格上限是版本 10；参考实现可到版本 40。
    // 若参考实现需要版本 > 10（或同样失败），则这里抛错是预期行为，单独计数。
    const refNeeds = ref && ref.ok ? ref.version : null;
    if (refNeeds === null || refNeeds > 10) {
      expectedOverflow++;
      if (!/超出版本 10/.test(err.message)) {
        failures.push({ c, why: '越界错误信息不够明确: ' + err.message });
        mismatchCases++;
      }
      continue;
    }
    failures.push({ c, why: 'QR.encode 抛错: ' + err.message });
    mismatchCases++;
    continue;
  }
  if (!ref || !ref.ok) {
    failures.push({ c, why: '参考实现无结果: ' + (ref && ref.error) });
    mismatchCases++;
    continue;
  }

  const problems = [];
  // 结构检查
  if (!(res.modules instanceof Uint8Array)) problems.push('modules 不是 Uint8Array');
  if (res.modules.length !== res.size * res.size) {
    problems.push(`modules 长度 ${res.modules.length} != size^2 ${res.size * res.size}`);
  }
  for (let i = 0; i < res.modules.length; i++) {
    if (res.modules[i] !== 0 && res.modules[i] !== 1) {
      problems.push(`modules[${i}] = ${res.modules[i]} 不是 0/1`);
      break;
    }
  }
  const expectSize = ref.version * 4 + 17;
  if (res.size !== ref.size) problems.push(`size ${res.size} != 参考 ${ref.size}`);
  if ((res.size - 17) / 4 !== ref.version) {
    problems.push(`版本 ${(res.size - 17) / 4} != 参考 ${ref.version}`);
  }

  // get(x,y) 与 modules 一致性（含越界返回 0）
  const rows = new Array(res.size);
  for (let y = 0; y < res.size; y++) {
    let s = '';
    for (let x = 0; x < res.size; x++) s += res.get(x, y) ? '1' : '0';
    rows[y] = s;
  }
  for (let y = 0; y < res.size; y++) {
    for (let x = 0; x < res.size; x++) {
      if (Number(rows[y][x]) !== res.modules[y * res.size + x]) {
        problems.push(`get(${x},${y}) 与 modules 不一致`);
        y = res.size;
        break;
      }
    }
  }
  if (res.get(-1, 0) !== 0 || res.get(0, -1) !== 0 || res.get(res.size, 0) !== 0 ||
      res.get(0, res.size) !== 0) {
    problems.push('越界 get() 未返回 0');
  }

  // 逐位比对
  let diff = 0;
  if (res.size === ref.size) {
    for (let y = 0; y < res.size; y++) {
      for (let x = 0; x < res.size; x++) {
        if (Number(rows[y][x]) !== Number(ref.rows[y][x])) diff++;
      }
    }
  } else {
    diff = -1;
  }

  const fmt = readFormat(res);
  if (fmt.mask !== ref.mask) problems.push(`掩码 ${fmt.mask} != 参考 ${ref.mask}`);
  if (fmt.ecBits !== ref.ec_bits) problems.push(`格式 EC 位 ${fmt.ecBits} != 参考 ${ref.ec_bits}`);
  if (ref.mask_from_lib !== ref.mask) {
    problems.push(`参考库自身 mask 复算不一致 ${ref.mask_from_lib} vs ${ref.mask}`);
  }

  compared++;
  mineForScan.push({
    id: c.id,
    text: c.text,
    ec: c.ec,
    version: ref.version,
    size: res.size,
    rows,
  });

  if (diff !== 0 || problems.length) {
    mismatchCases++;
    mismatchCells += Math.max(diff, 0);
    failures.push({
      c,
      why: problems.length ? problems.join('; ') : '逐位不相同',
      diff,
      size: res.size,
      refSize: ref.size,
      mask: fmt.mask,
      refMask: ref.mask,
    });
  }
  void expectSize;
}

/* ------------------------------------------------------------------ */
/* 4. 容量越界：必须抛出明确错误                                       */
/* ------------------------------------------------------------------ */

const overflowOk = [];
for (const [ec, max] of [['L', 271], ['M', 213], ['Q', 151], ['H', 119]]) {
  let threw = null;
  try {
    QR.encode('a'.repeat(max + 1), { ec });
  } catch (err) {
    threw = err;
  }
  overflowOk.push({
    ec,
    limit: max,
    fits: QR.encode('a'.repeat(max), { ec }).size,
    threw: threw ? threw.message : null,
  });
}

/* ------------------------------------------------------------------ */
/* 5. UMD 浏览器分支 + toString / toCanvas                             */
/* ------------------------------------------------------------------ */

const sandbox = {};
sandbox.self = sandbox;
sandbox.window = sandbox;
// 浏览器环境必备的全局对象，注入后才能模拟 <script src> 场景
sandbox.TextEncoder = TextEncoder;
vm.runInNewContext(readFileSync(QR_PATH, 'utf8'), sandbox, { filename: 'qr.js' });
const globalOk =
  !!(sandbox.QR && typeof sandbox.QR.encode === 'function' &&
     typeof sandbox.QR.toCanvas === 'function' &&
     typeof sandbox.QR.toString === 'function');
let globalMatch = false;
if (globalOk) {
  const a = sandbox.QR.encode('UMD-browser-branch', { ec: 'M' });
  const b = QR.encode('UMD-browser-branch', { ec: 'M' });
  globalMatch = a.size === b.size && rowsOf(a).join('') === rowsOf(b).join('');
}

const apiKeys = Object.keys(QR).sort();
const resKeys = Object.keys(QR.encode('api-shape')).sort();

// toString
const ts = QR.toString('文本二维码 toString');
const tsLines = ts.split('\n');
const tsSize = QR.encode('文本二维码 toString').size;
const tsShape =
  tsLines.length === tsSize &&
  tsLines.every((l) => l.length === tsSize * 2 && /^[\u2588 ]+$/.test(l));
const tsBordered = QR.toString('A', { border: 2 }).split('\n');
const tsBorderShape =
  tsBordered.length === QR.encode('A').size + 4 &&
  tsBordered[0].trim() === '' &&
  tsBordered[0].length === (QR.encode('A').size + 4) * 2;

// toCanvas：用最小 canvas 桩验证绘制行为
function stubCanvas(w, h) {
  const calls = [];
  const ctx = {
    fillStyle: '',
    fillRect(x, y, ww, hh) {
      calls.push({ style: this.fillStyle, x, y, w: ww, h: hh });
    },
  };
  return { width: w, height: h, getContext: () => ctx, __calls: calls };
}
const canvasChecks = [];
{
  const cv = stubCanvas(300, 300);
  const ret = QR.toCanvas('canvas 测试', cv, { ec: 'M' });
  const qr = QR.encode('canvas 测试', { ec: 'M' });
  const dark = qr.modules.reduce((a, b) => a + b, 0);
  const total = qr.size + 8;
  const scale = Math.floor(300 / total);
  const drawn = scale * total;
  const ox = Math.floor((300 - drawn) / 2);
  const bg = cv.__calls[0];
  canvasChecks.push({ name: '返回同一 canvas', ok: ret === cv });
  canvasChecks.push({
    name: '白底铺满',
    ok: bg.style.toLowerCase() === '#ffffff' && bg.x === 0 && bg.y === 0 && bg.w === 300 && bg.h === 300,
  });
  canvasChecks.push({
    name: '黑块数量 == 深色模块数',
    ok: cv.__calls.length - 1 === dark,
  });
  canvasChecks.push({
    name: '静区 4 模块 + 整数缩放',
    ok:
      scale >= 1 &&
      cv.__calls.slice(1).every(
        (c) =>
          c.style.toLowerCase() === '#000000' &&
          c.w === scale && c.h === scale &&
          c.x >= ox && c.x < ox + drawn && c.y >= ox && c.y < ox + drawn
      ),
  });
  canvasChecks.push({
    name: '黑块坐标落在静区之内',
    ok: cv.__calls.slice(1).every(
      (c) => c.x >= ox + 4 * scale && c.y >= ox + 4 * scale
    ),
  });
}
{
  // 传数字尺寸：需要 document
  globalThis.document = { createElement: () => stubCanvas(256, 256) };
  const cv = QR.toCanvas('A', 256, {});
  const qr = QR.encode('A');
  canvasChecks.push({
    name: '传 number 时自动创建 canvas 并绘制',
    ok: cv.width === 256 && cv.height === 256 && cv.__calls.length === qr.modules.reduce((a, b) => a + b, 0) + 1,
  });
  delete globalThis.document;
}

// 导出 toCanvas 的真实绘制指令，供 Python 渲染成 PNG 后解码，
// 从而端到端验证「静区 4 模块 + 整数缩放 + 白底黑块」的几何正确性
const CANVAS_SAMPLES = [
  { text: '你好，世界', opts: { ec: 'M' }, canvas: [512, 512] },
  { text: '二维码生成模块测试😀', opts: { ec: 'M' }, canvas: [512, 512] },
  {
    text: 'https://www.example.com/search?q=%E4%BA%8C%E7%BB%B4%E7%A0%81&lang=zh-CN',
    opts: { ec: 'M' },
    canvas: [600, 600],
  },
  {
    text: '这是一段较长的中文文本，用于测试多字节 UTF-8 编码在二维码字节模式下的填充与纠错处理是否正确。',
    opts: { ec: 'M' },
    canvas: [720, 720],
  },
  { text: 'H 等级：静区与整数缩放', opts: { ec: 'H' }, canvas: [480, 480] },
  { text: '中英混排 mixed content 123 ABC', opts: { ec: 'Q' }, canvas: [400, 240] },
  { text: 'inverted polarity', opts: { ec: 'M', dark: '#ffffff', light: '#000000' }, canvas: [480, 480] },
  { text: 'A', opts: { ec: 'L' }, canvas: [160, 160] },
];
const canvasOut = CANVAS_SAMPLES.map(({ text, opts, canvas: [w, h] }) => {
  const cv = stubCanvas(w, h);
  QR.toCanvas(text, cv, opts);
  return { text, ec: opts.ec, w, h, rects: cv.__calls };
});
writeFileSync(path.join(HERE, 'qr-canvas.json'), JSON.stringify(canvasOut), 'utf8');

/* ------------------------------------------------------------------ */
/* 6. 报告                                                             */
/* ------------------------------------------------------------------ */

console.log('\n================ 逐模块比对结果 ================');
console.log(`用例数（含越界）: ${cases.length}`);
console.log(`已逐位比对      : ${compared}`);
console.log(`预期越界抛错    : ${expectedOverflow}（参考实现需版本 > 10，本模块按规格抛错）`);
console.log(`不一致用例数    : ${mismatchCases}`);
console.log(`不一致模块数    : ${mismatchCells}`);
console.log(
  `逐位完全相等    : ${mismatchCases === 0 && compared + expectedOverflow === cases.length ? '是' : '否'}`
);

if (failures.length) {
  console.log('\n---- 不一致明细（最多 8 条）----');
  for (const f of failures.slice(0, 8)) {
    console.log(JSON.stringify(f, null, 2));
  }
}

console.log('\n---- 容量越界（应抛错）----');
for (const o of overflowOk) {
  console.log(
    `EC=${o.ec} 上限=${o.limit} 字节  ->  ${o.limit} 字节可用(size=${o.fits})，` +
      `${o.limit + 1} 字节 ${o.threw ? '抛错: ' + o.threw : '未抛错!!'}`
  );
}

console.log('\n---- UMD / API / 渲染 ----');
console.log(`window.QR 分支可用      : ${globalOk}`);
console.log(`浏览器分支矩阵与 CJS 相同: ${globalMatch}`);
console.log(`QR 导出键               : ${JSON.stringify(apiKeys)}`);
console.log(`encode() 返回键         : ${JSON.stringify(resKeys)}`);
console.log(`toString 形状正确       : ${tsShape} (${tsSize} 行 x ${tsSize * 2} 列)`);
console.log(`toString border:2 正确  : ${tsBorderShape}`);
for (const c of canvasChecks) console.log(`${c.name.padEnd(24)}: ${c.ok}`);

console.log('\n---- 覆盖范围 ----');
const verHist = {};
const ecHist = {};
for (const m of mineForScan) {
  verHist[m.version] = (verHist[m.version] || 0) + 1;
  ecHist[m.ec] = (ecHist[m.ec] || 0) + 1;
}
console.log('版本分布 : ' + Object.keys(verHist).sort((a, b) => a - b).map((v) => `v${v}:${verHist[v]}`).join('  '));
console.log('EC 分布  : ' + ['L', 'M', 'Q', 'H'].map((e) => `${e}:${ecHist[e] || 0}`).join('  '));

console.log('\n---------------- toString 样例（节选）----------------');
for (const l of tsLines.slice(0, 5)) console.log(l.replace(/█/g, '##').replace(/ /g, '..'));

// 供可扫性检查使用
writeFileSync(path.join(HERE, 'qr-mine.json'), JSON.stringify(mineForScan), 'utf8');
console.log(`\n已写出 build/qr-mine.json（${mineForScan.length} 个矩阵）供解码验证`);
console.log(`已写出 build/qr-canvas.json（${canvasOut.length} 组 toCanvas 绘制指令）供解码验证`);

const allOk =
  compared + expectedOverflow === cases.length &&
  mismatchCases === 0 &&
  globalOk && globalMatch && tsShape && tsBorderShape &&
  canvasChecks.every((c) => c.ok) &&
  overflowOk.every((o) => o.threw) &&
  failures.length === 0;
console.log(
  `\n最终: 用例数 ${cases.length} / 逐位不一致 ${mismatchCases} ` +
    `(实际比对 ${compared}，预期越界 ${expectedOverflow})  ->  ${allOk ? 'PASS' : 'FAIL'}`
);
process.exit(allOk ? 0 : 2);
