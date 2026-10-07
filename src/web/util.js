/* 棕仙的传输软件 —— 公共工具与基础设施 */
(function (global) {
  'use strict';
  const SD = (global.SD = global.SD || {});

  /* ---------- 取件码 ---------- */
  // Crockford Base32：无 I/L/O/U，避免手抄歧义
  const ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';

  function randomCode(len) {
    len = len || 9;
    const buf = new Uint8Array(len);
    crypto.getRandomValues(buf);
    let s = '';
    for (let i = 0; i < len; i++) s += ALPHABET[buf[i] & 31];
    return s;
  }

  function normalizeCode(raw) {
    if (!raw) return '';
    let s = String(raw).toUpperCase().replace(/[^0-9A-Z]/g, '');
    s = s.replace(/[IL]/g, '1').replace(/O/g, '0');
    let out = '';
    for (const ch of s) if (ALPHABET.indexOf(ch) >= 0) out += ch;
    return out;
  }

  async function roomIdFromCode(code) {
    const data = new TextEncoder().encode('swiftdrop/v1|' + normalizeCode(code));
    const h = await crypto.subtle.digest('SHA-256', data);
    return hex(new Uint8Array(h)).slice(0, 20);
  }

  /* ---------- 字节/哈希 ---------- */
  function hex(u8) {
    let s = '';
    for (let i = 0; i < u8.length; i++) s += u8[i].toString(16).padStart(2, '0');
    return s;
  }

  async function sha256(buf) {
    const h = await crypto.subtle.digest('SHA-256', buf);
    return hex(new Uint8Array(h));
  }

  async function hashList(hexList) {
    // 把一串块摘要再摘要一次，得到整文件指纹（Merkle 式）
    const s = hexList.join('');
    return sha256(new TextEncoder().encode(s));
  }

  function fmtBytes(n) {
    if (n === null || n === undefined || isNaN(n)) return '-';
    if (n < 1024) return n + ' B';
    const u = ['KB', 'MB', 'GB', 'TB'];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
    return n.toFixed(n >= 100 ? 0 : 1) + ' ' + u[i];
  }

  function fmtSpeed(bps) {
    if (!bps || bps <= 0) return '-';
    return fmtBytes(bps) + '/s';
  }

  function fmtEta(sec) {
    if (!isFinite(sec) || sec <= 0) return '-';
    if (sec < 60) return Math.ceil(sec) + ' 秒';
    if (sec < 3600) return Math.floor(sec / 60) + ' 分 ' + Math.round(sec % 60) + ' 秒';
    return Math.floor(sec / 3600) + ' 小时 ' + Math.round((sec % 3600) / 60) + ' 分';
  }

  function fmtTime(ts) {
    try {
      const d = new Date(ts);
      const p = (x) => String(x).padStart(2, '0');
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    } catch (e) { return '-'; }
  }

  /* ---------- 通用 ---------- */
  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
  function now() { return performance.now(); }

  function debounce(fn, ms) {
    let t = 0;
    return function () {
      const a = arguments;
      clearTimeout(t);
      t = setTimeout(() => fn.apply(null, a), ms);
    };
  }

  function el(id) { return document.getElementById(id); }

  function h(tag, attrs, children) {
    const n = document.createElement(tag);
    if (attrs) for (const k in attrs) {
      if (k === 'class') n.className = attrs[k];
      else if (k === 'text') n.textContent = attrs[k];
      else if (k.startsWith('on') && typeof attrs[k] === 'function') n.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] !== null && attrs[k] !== undefined) n.setAttribute(k, attrs[k]);
    }
    (children || []).forEach((c) => n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c));
    return n;
  }

  function safeName(s) {
    return String(s || '').replace(/[\\/:*?"<>|\u0000-\u001f]/g, '_').slice(0, 180) || 'file';
  }

  /* ---------- 日志 ---------- */
  const logBuf = [];
  const logListeners = [];
  function log(msg, level) {
    const rec = { t: Date.now(), level: level || 'info', msg: String(msg) };
    logBuf.push(rec);
    if (logBuf.length > 500) logBuf.shift();
    logListeners.forEach((f) => { try { f(rec); } catch (e) {} });
    const tag = level === 'err' ? '[错误] ' : level === 'warn' ? '[警告] ' : '';
    (level === 'err' ? console.error : level === 'warn' ? console.warn : console.log)(tag + rec.msg);
  }
  function onLog(fn) { logListeners.push(fn); return () => { const i = logListeners.indexOf(fn); if (i >= 0) logListeners.splice(i, 1); }; }

  /* ---------- IndexedDB ---------- */
  const DB_NAME = 'swiftdrop';
  const DB_VER = 1;
  let dbP = null;
  function db() {
    if (dbP) return dbP;
    dbP = new Promise((resolve, reject) => {
      let req;
      try { req = indexedDB.open(DB_NAME, DB_VER); } catch (e) { return reject(e); }
      req.onupgradeneeded = () => {
        const d = req.result;
        ['resume', 'syncstate', 'misc'].forEach((s) => { if (!d.objectStoreNames.contains(s)) d.createObjectStore(s); });
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    }).catch((e) => { log('本地数据库不可用（断点续传/同步状态将只在本次会话有效）：' + e, 'warn'); return null; });
    return dbP;
  }

  async function idbGet(store, key) {
    const d = await db(); if (!d) return null;
    return new Promise((res) => {
      const r = d.transaction(store, 'readonly').objectStore(store).get(key);
      r.onsuccess = () => res(r.result === undefined ? null : r.result);
      r.onerror = () => res(null);
    });
  }
  async function idbSet(store, key, val) {
    const d = await db(); if (!d) return false;
    return new Promise((res) => {
      const tx = d.transaction(store, 'readwrite');
      tx.objectStore(store).put(val, key);
      tx.oncomplete = () => res(true);
      tx.onerror = () => res(false);
    });
  }
  async function idbDel(store, key) {
    const d = await db(); if (!d) return false;
    return new Promise((res) => {
      const tx = d.transaction(store, 'readwrite');
      tx.objectStore(store).delete(key);
      tx.oncomplete = () => res(true);
      tx.onerror = () => res(false);
    });
  }
  async function idbKeys(store) {
    const d = await db(); if (!d) return [];
    return new Promise((res) => {
      const r = d.transaction(store, 'readonly').objectStore(store).getAllKeys();
      r.onsuccess = () => res(r.result || []);
      r.onerror = () => res([]);
    });
  }

  /* ---------- 设置（localStorage） ---------- */
  const DEFAULTS = {
    deviceName: '',
    iceServers: [
      { urls: 'stun:stun.miwifi.com:3478' },
      { urls: 'stun:stun.chat.bilibili.com:3478' },
      { urls: 'stun:stun.qq.com:3478' },
      { urls: 'stun:stun.l.google.com:19302' },
      { urls: 'stun:stun1.l.google.com:19302' },
      { urls: 'stun:stun.cloudflare.com:3478' },
      { urls: 'stun:stun.ekiga.net:3478' },
      { urls: 'stun:stun.nextcloud.com:443' }
    ],
    turnUrl: '',
    turnUser: '',
    turnPass: '',
    relayUrl: '',          // 中继服务器（wss://… 或 ws://…），空 = 打洞失败时不回退
    forceRelay: false,     // 强制走中继：跳过 P2P，直接连中继（测试 / 极端网络）
    chunkSize: 131072,
    bufferHigh: 12 * 1024 * 1024,
    bufferLow: 9 * 1024 * 1024,
    // 浏览器数据通道的发送缓冲上限（Chrome 约 16MB，越过会直接抛错），留足余量
    bufferCap: 14 * 1024 * 1024,
    preferRelay: false,
    brokers: null
  };

  let _cfgCache = null;
  function loadSettings() {
    if (_cfgCache) return _cfgCache;
    let s = {};
    try { s = JSON.parse(localStorage.getItem('swiftdrop.settings') || '{}') || {}; } catch (e) {}
    const out = Object.assign({}, DEFAULTS, s);
    if (!out.deviceName) out.deviceName = guessDeviceName();
    out.iceServers = buildIceServers(out);
    out.bufferLow = Math.min(out.bufferLow, out.bufferHigh);
    _cfgCache = out;
    return out;
  }
  function saveSettings(patch) {
    let s = {};
    try { s = JSON.parse(localStorage.getItem('swiftdrop.settings') || '{}') || {}; } catch (e) {}
    Object.assign(s, patch);
    try { localStorage.setItem('swiftdrop.settings', JSON.stringify(s)); } catch (e) {}
    _cfgCache = null;
  }
  function buildIceServers(s) {
    const list = (s.iceServers || DEFAULTS.iceServers).filter((x) => x && x.urls && (typeof x.urls !== 'string' || !x.urls.startsWith('turn')));
    if (s.turnUrl) {
      const urls = String(s.turnUrl).split(/[,\s]+/).filter(Boolean).map((u) => (/^turns?:/.test(u) ? u : 'turn:' + u));
      if (urls.length) list.push({ urls: urls, username: s.turnUser || '', credential: s.turnPass || '' });
    }
    return list;
  }

  function guessDeviceName() {
    const ua = navigator.userAgent;
    let os = '设备';
    if (/Windows/i.test(ua)) os = 'Windows';
    else if (/Mac OS X|Macintosh/i.test(ua)) os = 'Mac';
    else if (/Android/i.test(ua)) os = '安卓';
    else if (/iPhone|iPad|iPod/i.test(ua)) os = 'iPhone';
    else if (/Linux/i.test(ua)) os = 'Linux';
    let br = '';
    if (/Edg\//.test(ua)) br = 'Edge';
    else if (/Chrome\//.test(ua)) br = 'Chrome';
    else if (/Firefox\//.test(ua)) br = 'Firefox';
    else if (/Safari\//.test(ua)) br = 'Safari';
    return (os + (br ? '·' + br : '')).slice(0, 24);
  }

  /* ---------- 环境能力 ---------- */
  function caps() {
    return {
      fsa: typeof global.showDirectoryPicker === 'function',
      fsFile: typeof global.showSaveFilePicker === 'function',
      mobile: /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent),
      qr: !!((SD.QR || global.QR) && typeof (SD.QR || global.QR).toCanvas === 'function')
    };
  }

  Object.assign(SD, {
    ALPHABET, randomCode, normalizeCode, roomIdFromCode,
    hex, sha256, hashList,
    fmtBytes, fmtSpeed, fmtEta, fmtTime,
    sleep, now, debounce, el, h, safeName,
    log, onLog, idbGet, idbSet, idbDel, idbKeys,
    loadSettings, saveSettings, buildIceServers, guessDeviceName, caps, DEFAULTS
  });
})(window);
