/*!
 * qr.js —— 独立、无依赖的二维码（QR Code）生成模块
 * UMD：<script src> 直接引入后暴露 window.QR，也支持 CommonJS / AMD。
 *
 * - 只使用 byte mode（0100），输入按 UTF-8（TextEncoder）取字节，中文 / emoji 均可
 * - 版本 1~10 自动选最小可用版本，超长抛出明确错误；纠错等级 L/M/Q/H（默认 M）
 * - 实现 ISO/IEC 18004 四条掩码罚分规则，自动选取最优掩码
 * - 无网络、无 CDN、无第三方依赖，也不使用任何动态求值
 *
 * API：QR.encode(text, opts)              -> { size, get(x,y) -> 0|1, modules }
 *      QR.toCanvas(text, canvasOrSize, opts) -> HTMLCanvasElement
 *      QR.toString(text, opts)            -> 文本二维码（每模块 2 字符宽）
 *      opts = { ec: 'L'|'M'|'Q'|'H' }，默认 'M'
 */
(function (root, factory) {
  'use strict';
  if (typeof module === 'object' && module && module.exports) {
    module.exports = factory();
  } else if (typeof define === 'function' && define.amd) {
    define([], factory);
  } else {
    root.QR = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /* ---------------- 1. 常量表（ISO/IEC 18004） ---------------- */

  var MAX_VERSION = 10;

  // 格式信息中的纠错等级指示位：L=01, M=00, Q=11, H=10
  var EC_FORMAT_BITS = { L: 1, M: 0, Q: 3, H: 2 };

  // 纠错码块结构：[每块纠错码字数, [每块数据码字数, 块数], ...]
  // 数据来源：ISO/IEC 18004 表 13~22（与 qrcode/base.py RS_BLOCK_TABLE 一致）
  var RS_TABLE = [
    /* v1  */ { L: [7, [19, 1]], M: [10, [16, 1]], Q: [13, [13, 1]], H: [17, [9, 1]] },
    /* v2  */ { L: [10, [34, 1]], M: [16, [28, 1]], Q: [22, [22, 1]], H: [28, [16, 1]] },
    /* v3  */ { L: [15, [55, 1]], M: [26, [44, 1]], Q: [18, [17, 2]], H: [22, [13, 2]] },
    /* v4  */ { L: [20, [80, 1]], M: [18, [32, 2]], Q: [26, [24, 2]], H: [16, [9, 4]] },
    /* v5  */ { L: [26, [108, 1]], M: [24, [43, 2]], Q: [18, [15, 2], [16, 2]], H: [22, [11, 2], [12, 2]] },
    /* v6  */ { L: [18, [68, 2]], M: [16, [27, 4]], Q: [24, [19, 4]], H: [28, [15, 4]] },
    /* v7  */ { L: [20, [78, 2]], M: [18, [31, 4]], Q: [18, [14, 2], [15, 4]], H: [26, [13, 4], [14, 1]] },
    /* v8  */ { L: [24, [97, 2]], M: [22, [38, 2], [39, 2]], Q: [22, [18, 4], [19, 2]], H: [26, [14, 4], [15, 2]] },
    /* v9  */ { L: [30, [116, 2]], M: [22, [36, 3], [37, 2]], Q: [20, [16, 4], [17, 4]], H: [24, [12, 4], [13, 4]] },
    /* v10 */ { L: [18, [68, 2], [69, 2]], M: [26, [43, 4], [44, 1]], Q: [24, [19, 6], [20, 2]], H: [28, [15, 6], [16, 2]] }
  ];

  // 校正图形中心坐标（版本 1~10）
  var ALIGN_COORDS = [
    [], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34],
    [6, 22, 38], [6, 24, 42], [6, 26, 46], [6, 28, 50]
  ];

  var PAD_BYTES = [0xEC, 0x11];
  var MODE_BYTE = 4; // 0100

  /* ------------- 2. GF(256) 有限域（本原多项式 0x11D） ------------- */

  var GF_EXP = new Uint8Array(512);
  var GF_LOG = new Uint8Array(256);

  (function initGf() {
    var x = 1;
    for (var i = 0; i < 255; i++) {
      GF_EXP[i] = x;
      GF_LOG[x] = i;
      x <<= 1;
      if (x & 0x100) x ^= 0x11D;
    }
    for (var j = 255; j < 512; j++) GF_EXP[j] = GF_EXP[j - 255];
  })();

  function gfMul(a, b) {
    if (a === 0 || b === 0) return 0;
    return GF_EXP[GF_LOG[a] + GF_LOG[b]];
  }

  function polyMul(a, b) {
    var res = new Array(a.length + b.length - 1);
    for (var i = 0; i < res.length; i++) res[i] = 0;
    for (var i = 0; i < a.length; i++) {
      if (a[i] === 0) continue;
      for (var j = 0; j < b.length; j++) res[i + j] ^= gfMul(a[i], b[j]);
    }
    return res;
  }

  // 生成多项式 g(x) = (x-a^0)(x-a^1)...(x-a^(ecLen-1))，系数按降幂排列
  var GEN_CACHE = {};
  function rsGeneratorPoly(ecLen) {
    if (GEN_CACHE[ecLen]) return GEN_CACHE[ecLen];
    var poly = [1];
    for (var i = 0; i < ecLen; i++) poly = polyMul(poly, [1, GF_EXP[i]]);
    GEN_CACHE[ecLen] = poly;
    return poly;
  }

  // Reed-Solomon 纠错码字（GF(256) 多项式长除的余式）
  function rsEncode(data, ecLen) {
    var gen = rsGeneratorPoly(ecLen);
    var rem = new Uint8Array(ecLen);
    for (var i = 0; i < data.length; i++) {
      var factor = data[i] ^ rem[0];
      rem.copyWithin(0, 1);
      rem[ecLen - 1] = 0;
      if (factor !== 0) {
        for (var j = 0; j < ecLen; j++) rem[j] ^= gfMul(gen[j + 1], factor);
      }
    }
    return rem;
  }

  /* ---------------- 3. 数据编码（byte mode） ---------------- */

  var textEncoder = typeof TextEncoder !== 'undefined' ? new TextEncoder() : null;

  // UTF-8 字节；模块本身不含 polyfill，缺少 TextEncoder 的环境直接报明确错误
  function utf8Bytes(str) {
    if (!textEncoder) throw new Error('QR: 当前环境缺少 TextEncoder，无法进行 UTF-8 编码');
    return textEncoder.encode(str);
  }

  function BitBuffer() {
    this.bits = [];
  }
  BitBuffer.prototype.put = function (value, length) {
    for (var i = length - 1; i >= 0; i--) this.bits.push((value >>> i) & 1);
  };

  // byte mode 的字符计数指示符位数：v1-9 为 8，v10-26 为 16
  function countBits(version) {
    return version < 10 ? 8 : 16;
  }

  function rsBlocks(version, ec) {
    var spec = RS_TABLE[version - 1][ec];
    var lens = [];
    for (var g = 1; g < spec.length; g++) {
      for (var i = 0; i < spec[g][1]; i++) lens.push(spec[g][0]);
    }
    return { ecLen: spec[0], dataLens: lens };
  }

  function capacityBits(version, ec) {
    var lens = rsBlocks(version, ec).dataLens, sum = 0;
    for (var i = 0; i < lens.length; i++) sum += lens[i];
    return sum * 8;
  }

  // 该版本/EC 下 byte mode 能装的最大字节数
  function maxBytes(version, ec) {
    return Math.floor((capacityBits(version, ec) - 4 - countBits(version)) / 8);
  }

  function chooseVersion(byteLen, ec) {
    for (var v = 1; v <= MAX_VERSION; v++) {
      if (4 + countBits(v) + 8 * byteLen <= capacityBits(v, ec)) return v;
    }
    return -1;
  }

  // 生成最终的码字序列（数据块 + 纠错块，按标准交织）
  function createCodewords(version, ec, bytes) {
    var info = rsBlocks(version, ec);
    var lens = info.dataLens;
    var totalData = 0;
    for (var i = 0; i < lens.length; i++) totalData += lens[i];
    var capBits = totalData * 8;

    var buf = new BitBuffer();
    buf.put(MODE_BYTE, 4);
    buf.put(bytes.length, countBits(version));
    for (var i = 0; i < bytes.length; i++) buf.put(bytes[i], 8);

    if (buf.bits.length > capBits) {
      throw new Error('QR: 内部错误，数据超出容量');
    }
    var term = Math.min(4, capBits - buf.bits.length);
    for (var i = 0; i < term; i++) buf.bits.push(0);
    while (buf.bits.length % 8 !== 0) buf.bits.push(0);
    for (var k = 0; buf.bits.length < capBits; k++) buf.put(PAD_BYTES[k % 2], 8);

    var dataCw = new Uint8Array(totalData);
    for (var i = 0; i < totalData; i++) {
      var byteVal = 0;
      for (var j = 0; j < 8; j++) byteVal = (byteVal << 1) | buf.bits[i * 8 + j];
      dataCw[i] = byteVal;
    }

    // 分块并计算纠错码字
    var blocks = [], offset = 0, maxK = 0;
    for (var b = 0; b < lens.length; b++) {
      var k = lens[b];
      var dc = dataCw.subarray(offset, offset + k);
      offset += k;
      if (k > maxK) maxK = k;
      blocks.push({ data: dc, ec: rsEncode(dc, info.ecLen) });
    }

    // 交织：先按列取数据码字，再按列取纠错码字
    var out = new Uint8Array(totalData + info.ecLen * blocks.length);
    var p = 0, i2, b2;
    for (i2 = 0; i2 < maxK; i2++) {
      for (b2 = 0; b2 < blocks.length; b2++) {
        if (i2 < blocks[b2].data.length) out[p++] = blocks[b2].data[i2];
      }
    }
    for (i2 = 0; i2 < info.ecLen; i2++) {
      for (b2 = 0; b2 < blocks.length; b2++) out[p++] = blocks[b2].ec[i2];
    }
    return out;
  }

  /* ------------- 4. BCH 校验码（格式信息 / 版本信息） ------------- */

  function bchDigit(data) {
    var digit = 0;
    while (data !== 0) {
      digit++;
      data >>>= 1;
    }
    return digit;
  }

  // 15 位格式信息：5 位数据 + 10 位 BCH，再异或 0x5412
  function bchTypeInfo(data) {
    var G15 = 0x537, d = data << 10;
    while (bchDigit(d) - bchDigit(G15) >= 0) d ^= G15 << (bchDigit(d) - bchDigit(G15));
    return ((data << 10) | d) ^ 0x5412;
  }

  // 18 位版本信息（版本 >= 7）：6 位数据 + 12 位 BCH
  function bchTypeNumber(data) {
    var G18 = 0x1f25, d = data << 12;
    while (bchDigit(d) - bchDigit(G18) >= 0) d ^= G18 << (bchDigit(d) - bchDigit(G18));
    return (data << 12) | d;
  }

  /* ---------------- 5. 掩码（8 种数据掩码） ---------------- */

  function maskBit(mask, i, j) {
    switch (mask) {
      case 0: return (i + j) % 2 === 0;
      case 1: return i % 2 === 0;
      case 2: return j % 3 === 0;
      case 3: return (i + j) % 3 === 0;
      case 4: return (Math.floor(i / 2) + Math.floor(j / 3)) % 2 === 0;
      case 5: return (i * j) % 2 + (i * j) % 3 === 0;
      case 6: return ((i * j) % 2 + (i * j) % 3) % 2 === 0;
      case 7: return ((i * j) % 3 + (i + j) % 2) % 2 === 0;
    }
    return false;
  }

  /* ---------------- 6. 矩阵构造 ---------------- */

  function setupProbe(m, size, row, col) {
    for (var r = -1; r <= 7; r++) {
      var rr = row + r;
      if (rr < 0 || rr >= size) continue;
      for (var c = -1; c <= 7; c++) {
        var cc = col + c;
        if (cc < 0 || cc >= size) continue;
        var on = (r >= 0 && r <= 6 && (c === 0 || c === 6)) ||
                 (c >= 0 && c <= 6 && (r === 0 || r === 6)) ||
                 (r >= 2 && r <= 4 && c >= 2 && c <= 4);
        m[rr * size + cc] = on ? 1 : 0;
      }
    }
  }

  function setupAdjust(m, size, version) {
    var pos = ALIGN_COORDS[version - 1];
    for (var i = 0; i < pos.length; i++) {
      for (var j = 0; j < pos.length; j++) {
        var row = pos[i], col = pos[j];
        if (m[row * size + col] !== -1) continue; // 与定位图形重叠时跳过
        for (var r = -2; r <= 2; r++) {
          for (var c = -2; c <= 2; c++) {
            var on = r === -2 || r === 2 || c === -2 || c === 2 || (r === 0 && c === 0);
            m[(row + r) * size + col + c] = on ? 1 : 0;
          }
        }
      }
    }
  }

  function setupTiming(m, size) {
    for (var r = 8; r < size - 8; r++) {
      if (m[r * size + 6] === -1) m[r * size + 6] = r % 2 === 0 ? 1 : 0;
    }
    for (var c = 8; c < size - 8; c++) {
      if (m[6 * size + c] === -1) m[6 * size + c] = c % 2 === 0 ? 1 : 0;
    }
  }

  // test=true 时格式信息位置全部写 0（用于掩码评估，与参考实现一致）
  function setupTypeInfo(m, size, ecBits, mask, test) {
    var bits = bchTypeInfo((ecBits << 3) | mask);
    var i, on;
    for (i = 0; i < 15; i++) {
      on = !test && ((bits >> i) & 1) === 1 ? 1 : 0;
      if (i < 6) m[i * size + 8] = on;
      else if (i < 8) m[(i + 1) * size + 8] = on;
      else m[(size - 15 + i) * size + 8] = on;
    }
    for (i = 0; i < 15; i++) {
      on = !test && ((bits >> i) & 1) === 1 ? 1 : 0;
      if (i < 8) m[8 * size + (size - i - 1)] = on;
      else if (i < 9) m[8 * size + 7] = on;
      else m[8 * size + (15 - i - 1)] = on;
    }
    m[(size - 8) * size + 8] = test ? 0 : 1; // 固定深色模块
  }

  function setupTypeNumber(m, size, version, test) {
    var bits = bchTypeNumber(version);
    var i, on, base = size - 11;
    for (i = 0; i < 18; i++) {
      on = !test && ((bits >> i) & 1) === 1 ? 1 : 0;
      m[Math.floor(i / 3) * size + (i % 3 + base)] = on;
    }
    for (i = 0; i < 18; i++) {
      on = !test && ((bits >> i) & 1) === 1 ? 1 : 0;
      m[(i % 3 + base) * size + Math.floor(i / 3)] = on;
    }
  }

  // 数据填充：从右下角起之字形向上/向下，跳过第 6 列，逐位应用掩码
  function mapData(m, size, codewords, mask) {
    var inc = -1, row = size - 1, bitIndex = 7, byteIndex = 0;
    var dataLen = codewords.length;
    for (var start = size - 1; start > 0; start -= 2) {
      var col = start <= 6 ? start - 1 : start;
      for (;;) {
        for (var k = 0; k < 2; k++) {
          var c = col - k;
          var idx = row * size + c;
          if (m[idx] === -1) {
            var dark = 0;
            if (byteIndex < dataLen) dark = (codewords[byteIndex] >> bitIndex) & 1;
            if (maskBit(mask, row, c)) dark = dark ? 0 : 1;
            m[idx] = dark;
            bitIndex--;
            if (bitIndex === -1) {
              byteIndex++;
              bitIndex = 7;
            }
          }
        }
        row += inc;
        if (row < 0 || row >= size) {
          row -= inc;
          inc = -inc;
          break;
        }
      }
    }
  }

  function buildMatrix(version, codewords, ecBits, mask, test) {
    var size = version * 4 + 17;
    var m = new Int8Array(size * size);
    m.fill(-1);
    setupProbe(m, size, 0, 0);
    setupProbe(m, size, size - 7, 0);
    setupProbe(m, size, 0, size - 7);
    setupAdjust(m, size, version);
    setupTiming(m, size);
    setupTypeInfo(m, size, ecBits, mask, test);
    if (version >= 7) setupTypeNumber(m, size, version, test);
    mapData(m, size, codewords, mask);
    return m;
  }

  /* ------- 7. 掩码罚分（ISO/IEC 18004 §8.8.2 四条规则） ------- */

  // 1:1:3:1:1 且某一侧有 4 模块宽的静区：10111010000 / 00001011101
  function matchFinderLike(m, base, stride) {
    if (m[base + stride] !== 0) return false;
    if (m[base + 4 * stride] !== 1) return false;
    if (m[base + 5 * stride] !== 0) return false;
    if (m[base + 6 * stride] !== 1) return false;
    if (m[base + 9 * stride] !== 0) return false;
    var b0 = m[base], b2 = m[base + 2 * stride], b3 = m[base + 3 * stride];
    var b7 = m[base + 7 * stride], b8 = m[base + 8 * stride], b10 = m[base + 10 * stride];
    if (b0 === 1 && b2 === 1 && b3 === 1 && b7 === 0 && b8 === 0 && b10 === 0) return true;
    if (b0 === 0 && b2 === 0 && b3 === 0 && b7 === 1 && b8 === 1 && b10 === 1) return true;
    return false;
  }

  function lostPoint(m, size) {
    var score = 0, i, j, runRow, runCol;

    // 规则 1：行/列中同色连续模块数 >= 5，罚分 = 3 + (n - 5) = n - 2
    for (i = 0; i < size; i++) {
      runRow = 1;
      runCol = 1;
      for (j = 1; j < size; j++) {
        if (m[i * size + j] === m[i * size + j - 1]) runRow++;
        else {
          if (runRow >= 5) score += runRow - 2;
          runRow = 1;
        }
        if (m[j * size + i] === m[(j - 1) * size + i]) runCol++;
        else {
          if (runCol >= 5) score += runCol - 2;
          runCol = 1;
        }
      }
      if (runRow >= 5) score += runRow - 2;
      if (runCol >= 5) score += runCol - 2;
    }

    // 规则 2：2x2 同色块，每块罚 3
    for (i = 0; i < size - 1; i++) {
      for (j = 0; j < size - 1; j++) {
        var v = m[i * size + j];
        if (v === m[i * size + j + 1] && v === m[(i + 1) * size + j] &&
            v === m[(i + 1) * size + j + 1]) score += 3;
      }
    }

    // 规则 3：出现类似定位图形的 1:1:3:1:1 图案，每次罚 40
    for (i = 0; i < size; i++) {
      for (j = 0; j + 10 < size; j++) {
        if (matchFinderLike(m, i * size + j, 1)) score += 40;
        if (matchFinderLike(m, j * size + i, size)) score += 40;
      }
    }

    // 规则 4：深色模块比例每偏离 50% 达 5%，罚 10
    var dark = 0;
    for (i = 0; i < m.length; i++) if (m[i] === 1) dark++;
    var percent = dark / (size * size);
    score += Math.floor(Math.abs(percent * 100 - 50) / 5) * 10;

    return score;
  }

  /* ---------------- 8. 公开 API ---------------- */

  function encode(text, opts) {
    opts = opts || {};
    var ec = opts.ec == null ? 'M' : String(opts.ec).toUpperCase();
    if (!Object.prototype.hasOwnProperty.call(EC_FORMAT_BITS, ec)) {
      throw new Error('QR.encode: 不支持的纠错等级 "' + opts.ec + '"，可选值为 L / M / Q / H');
    }
    if (typeof text !== 'string') {
      if (text == null) throw new Error('QR.encode: text 必须是字符串');
      text = String(text);
    }

    var bytes = utf8Bytes(text);
    var version = chooseVersion(bytes.length, ec);
    if (version < 0) {
      throw new Error('QR.encode: 内容过长，UTF-8 编码后 ' + bytes.length + ' 字节，' +
        '超出版本 ' + MAX_VERSION + '（纠错等级 ' + ec + '，上限 ' +
        maxBytes(MAX_VERSION, ec) + ' 字节）');
    }

    var codewords = createCodewords(version, ec, bytes);
    var ecBits = EC_FORMAT_BITS[ec];
    var size = version * 4 + 17;
    var bestMask = 0, bestScore = 0;
    for (var mask = 0; mask < 8; mask++) {
      var score = lostPoint(buildMatrix(version, codewords, ecBits, mask, true), size);
      if (mask === 0 || score < bestScore) {
        bestScore = score;
        bestMask = mask;
      }
    }

    var cells = buildMatrix(version, codewords, ecBits, bestMask, false);
    var modules = new Uint8Array(size * size);
    for (var i = 0; i < modules.length; i++) modules[i] = cells[i] === 1 ? 1 : 0;

    return {
      size: size,
      get: function (x, y) {
        if (x < 0 || y < 0 || x >= size || y >= size) return 0;
        return modules[y * size + x];
      },
      modules: modules
    };
  }

  function toCanvas(text, canvasOrSize, opts) {
    opts = opts || {};
    var canvas = canvasOrSize;
    if (typeof canvasOrSize === 'number') {
      if (typeof document === 'undefined' || !document.createElement) {
        throw new Error('QR.toCanvas: 只给了尺寸但没有 document，无法创建 canvas');
      }
      canvas = document.createElement('canvas');
      canvas.width = canvas.height = Math.max(1, Math.round(canvasOrSize));
    }
    if (!canvas || typeof canvas.getContext !== 'function') {
      throw new Error('QR.toCanvas: 第二个参数必须是 HTMLCanvasElement 或像素尺寸（number）');
    }
    var ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('QR.toCanvas: 无法获得 2d 绘图上下文');

    var qr = encode(text, opts);
    var w = canvas.width, h = canvas.height;
    var quiet = 4;
    var total = qr.size + quiet * 2;
    var scale = Math.max(1, Math.floor(Math.min(w, h) / total));
    var drawn = scale * total;
    var ox = Math.floor((w - drawn) / 2);
    var oy = Math.floor((h - drawn) / 2);

    ctx.fillStyle = opts.light || '#ffffff';
    ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = opts.dark || '#000000';
    for (var y = 0; y < qr.size; y++) {
      for (var x = 0; x < qr.size; x++) {
        if (qr.get(x, y)) {
          ctx.fillRect(ox + (x + quiet) * scale, oy + (y + quiet) * scale, scale, scale);
        }
      }
    }
    return canvas;
  }

  function toString(text, opts) {
    opts = opts || {};
    var qr = encode(text, opts);
    var quiet = opts.border == null ? 0 : opts.border | 0;
    var lines = [];
    for (var y = -quiet; y < qr.size + quiet; y++) {
      var line = '';
      for (var x = -quiet; x < qr.size + quiet; x++) {
        line += qr.get(x, y) ? '\u2588\u2588' : '  ';
      }
      lines.push(line);
    }
    return lines.join('\n');
  }

  return { encode: encode, toCanvas: toCanvas, toString: toString };
});
