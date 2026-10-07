/* 棕仙的传输软件 —— 界面与流程控制 */
(function () {
  'use strict';
  const SD = window.SD;
  const $ = SD.el;

  const App = {
    settings: null, sig: null, link: null, tf: null, sync: null,
    role: '', code: '', connected: false, ready: false,
    saveDir: null, syncDir: null, watchTimer: 0, totalTimer: 0,
    rows: new Map(), tasks: new Map(), lastSaveDir2: false,
    relay: { state: 'none', link: null, room: '', url: '', timer: 0 }
  };
  window.SDApp = App;
  App.Diag = null;   // 在下面赋值（便于测试脚本读取诊断数据）

  /* ---------------- 小工具 ---------------- */
  /* ---------------- 主题（跟随系统 / 浅色 / 深色） ---------------- */
  function initTheme() {
    let t = 'auto';
    try { t = localStorage.getItem('zongxian.theme') || 'auto'; } catch (e) {}
    setTheme(t, true);
    const b = $('btnTheme');
    if (b) b.addEventListener('click', () => {
      const cur = document.documentElement.getAttribute('data-theme') || 'auto';
      setTheme(cur === 'auto' ? 'light' : cur === 'light' ? 'dark' : 'auto');
    });
  }
  function setTheme(t, silent) {
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem('zongxian.theme', t); } catch (e) {}
    const b = $('btnTheme');
    if (b) b.title = '主题：' + ({ auto: '跟随系统', light: '浅色', dark: '深色' }[t] || t);
    if (!silent) SD.log('主题已切换为：' + ({ auto: '跟随系统', light: '浅色', dark: '深色' }[t] || t));
  }

  function toast(msg, kind, ms) {
    const box = $('toasts');
    // 同一条消息不重复弹（同步失败这类错误会连续触发）
    for (const el of Array.from(box.children)) if (el.dataset.msg === msg) return;
    const n = SD.h('div', { class: 'toast ' + (kind || ''), text: msg });
    n.dataset.msg = msg;
    box.appendChild(n);
    while (box.children.length > 4) box.removeChild(box.firstChild);
    setTimeout(() => { n.style.opacity = '0'; n.style.transition = 'opacity .3s'; setTimeout(() => n.remove(), 320); }, ms || 4200);
  }

  function logLine(rec) {
    const box = $('logBox');
    if (!box) return;
    const t = new Date(rec.t);
    const p = (x) => String(x).padStart(2, '0');
    const line = SD.h('div', { class: rec.level === 'err' ? 'err' : rec.level === 'warn' ? 'warn' : '' });
    line.appendChild(SD.h('span', { class: 't', text: `${p(t.getHours())}:${p(t.getMinutes())}:${p(t.getSeconds())}` }));
    line.appendChild(document.createTextNode(rec.msg));
    box.appendChild(line);
    while (box.children.length > 400) box.removeChild(box.firstChild);
    box.scrollTop = box.scrollHeight;
  }

  function syncLogLine(msg, level) {
    const box = $('syncLog');
    if (!box) return;
    const n = SD.h('div', { class: level === 'err' ? 'err' : level === 'warn' ? 'warn' : '', text: msg });
    box.appendChild(n);
    while (box.children.length > 300) box.removeChild(box.firstChild);
    box.scrollTop = box.scrollHeight;
  }

  function chip(id, text, cls) {
    const c = $(id);
    if (!c) return;
    c.textContent = text;
    c.className = 'chip' + (cls ? ' ' + cls : '');
  }

  function baseName(p) { const a = String(p).split('/'); return a[a.length - 1] || p; }

  async function copyText(s) {
    try { await navigator.clipboard.writeText(s); return true; } catch (e) {}
    try {
      const ta = document.createElement('textarea');
      ta.value = s; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
      return true;
    } catch (e) { return false; }
  }

  /* ---------------- 初始化 ---------------- */
  function init() {
    if (window.QR && !SD.QR) SD.QR = window.QR;    // 二维码模块挂在 window.QR 上，桥接进来
    App.settings = SD.loadSettings();
    // 安卓壳（WebView）注入的原生桥：没有（或壳里本地服务没起来）就用浏览器自身的下载
    try {
      if (window.ZongxianNative && typeof window.ZongxianNative.saveUrl === 'function') {
        const u = String(window.ZongxianNative.saveUrl() || '');
        if (u) App.native = { saveUrl: u, toast: (m) => { try { window.ZongxianNative.toast(m); } catch (e) {} } };
      }
    } catch (e) {}
    initTheme();
    SD.onLog(logLine);
    const c = SD.caps();
    $('capText').textContent = '能力检测：文件夹写入(流式/无限大小)=' + (c.fsa ? '支持' : '不支持') +
      ' · 二维码=' + (c.qr ? '支持' : '不支持') + ' · 设备=' + SD.guessDeviceName() +
      ' · 安全上下文=' + (window.isSecureContext ? '是' : '否（加密与文件夹功能不可用，请用 http://localhost 或 https 打开）') +
      (App.native ? ' · 运行在安卓客户端内' : '');
    SD.log('棕仙的传输软件已就绪，设备名：' + App.settings.deviceName);
    if (!window.isSecureContext) {
      SD.log('当前不是安全上下文：crypto.subtle/哈希校验不可用。请用 https 或 http://localhost 打开本页。', 'err');
      toast('请用 http://localhost 或 https 打开，否则无法加密与校验', 'err', 9000);
    }
    if (!c.fsa) {
      $('dropHint').textContent = (c.mobile || App.native)
        ? '手机端：点「选择文件」可一次多选；接收的文件会保存到系统"下载"目录（单个文件建议不超过 1GB）。'
        : '当前浏览器不支持"选择保存目录"（建议用 Chrome/Edge）：接收的文件会先放在内存再下载，超大文件请换浏览器。';
      // 安卓/iOS 的 WebView 与手机浏览器都不支持"选择文件夹"，留着只会点了没反应
      if ((c.mobile || App.native) && $('btnPickFolder')) {
        $('btnPickFolder').classList.add('hidden');
      }
    } else {
      $('dropHint').textContent = '选择保存目录后，接收的大文件会直接流式写入磁盘，多大都行。';
    }

    wire();
    restoreDirs();
    autoJoinFromUrl();
    App.totalTimer = setInterval(updateTotals, 500);
    window.addEventListener('beforeunload', (e) => {
      const busy = Array.from(App.tasks.values()).some((t) => t.state === 'run' || t.state === 'wait');
      if (busy) { e.preventDefault(); e.returnValue = '还有文件正在传输，确定离开？'; }
    });
  }

  function wire() {
    $('btnCreate').addEventListener('click', () => createSession());
    $('btnJoin').addEventListener('click', () => {
      const code = SD.normalizeCode($('joinCode').value);
      if (code.length < 6) { toast('取件码看起来不对，应该是 9 位字母数字', 'err'); return; }
      joinSession(code);
    });
    $('joinCode').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('btnJoin').click(); });
    $('btnCopyLink').addEventListener('click', async () => {
      // 本地打开时链接对朋友无效（指向你自己的硬盘/手机内部），改为复制取件码+说明
      const codeOnly = shareMode() !== 'link';
      const text = codeOnly
        ? ('取件码：' + (App.code || '') + '\n让朋友打开你发给他的 swiftdrop.html，在「我要接收」里输入这个取件码，点「连接」。')
        : $('linkText').value;
      const ok = await copyText(text);
      toast(ok ? (codeOnly ? '取件码已复制，发给朋友让他手动输入' : '链接已复制，发给朋友即可') : '复制失败，请手动选中复制', ok ? 'ok' : 'err');
    });
    $('btnClearLog').addEventListener('click', () => { $('logBox').innerHTML = ''; });
    $('btnDiagCopy').addEventListener('click', async () => {
      const text = Diag.copy();
      const ok = await copyText(text);
      toast(ok ? '诊断信息已复制，可直接发给对方或发给我排查' : '复制失败，请手动选中复制', ok ? 'ok' : 'err');
      SD.log('诊断：' + text.replace(/\n/g, ' | '));
    });
    $('btnLeave').addEventListener('click', () => leave());
    $('btnSettings').addEventListener('click', openSettings);
    $('btnHelp').addEventListener('click', showHelp);

    document.querySelectorAll('.tab').forEach((b) => b.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      $('tab-files').classList.toggle('hidden', b.dataset.tab !== 'files');
      $('tab-sync').classList.toggle('hidden', b.dataset.tab !== 'sync');
    }));

    // 拖拽
    const dz = $('dropZone');
    ['dragenter', 'dragover'].forEach((k) => dz.addEventListener(k, (e) => { e.preventDefault(); dz.classList.add('over'); }));
    ['dragleave', 'drop'].forEach((k) => dz.addEventListener(k, (e) => { e.preventDefault(); dz.classList.remove('over'); }));
    dz.addEventListener('drop', (e) => {
      const roots = [];
      if (e.dataTransfer && e.dataTransfer.items) {
        for (const it of Array.from(e.dataTransfer.items)) {
          if (it.kind !== 'file') continue;
          try { const en = it.webkitGetAsEntry && it.webkitGetAsEntry(); if (en) roots.push(en); } catch (err) {}
        }
      }
      const plain = e.dataTransfer ? Array.from(e.dataTransfer.files || []) : [];
      handleDrop(roots, plain);
    });
    window.addEventListener('dragover', (e) => e.preventDefault());
    window.addEventListener('drop', (e) => e.preventDefault());

    $('btnPickFiles').addEventListener('click', () => $('fileInput').click());
    $('btnPickFolder').addEventListener('click', () => $('dirInput').click());
    $('fileInput').addEventListener('change', (e) => { sendFileList(Array.from(e.target.files || [])); e.target.value = ''; });
    $('dirInput').addEventListener('change', (e) => { sendFileList(Array.from(e.target.files || [])); e.target.value = ''; });

    $('btnPickSaveDir').addEventListener('click', pickSaveDir);
    $('btnPickSaveDir2').addEventListener('click', pickSaveDir);
    $('btnPickSyncDir').addEventListener('click', pickSyncDir);
    $('btnSyncNow').addEventListener('click', doSync);
    $('syncWatch').addEventListener('change', updateWatch);

    $('btnSaveSettings').addEventListener('click', saveSettings);
    $('btnClearResume').addEventListener('click', async () => {
      const keys = await SD.idbKeys('resume');
      for (const k of keys) await SD.idbDel('resume', k);
      toast('断点续传缓存已清空（' + keys.length + ' 条）', 'ok');
    });
    $('btnClearSync').addEventListener('click', async () => {
      const keys = await SD.idbKeys('syncstate');
      for (const k of keys) await SD.idbDel('syncstate', k);
      if (App.sync) await App.sync.resetState();
      toast('同步状态已清空（' + keys.length + ' 条）', 'ok');
    });
  }

  function showHelp() {
    toast('用法：1) 你点"生成取件码"→ 把码或链接发给朋友；2) 朋友打开链接或用同款页面输入码 → 自动直连；3) 拖文件/文件夹进窗口即发送；4) 想同步文件夹就切到"文件夹同步"页，双方各选一个本地文件夹。' +
      ' 注意：本页必须用 http://localhost 或 https 打开（file:// 也可以，但手机请用链接访问）。', 'ok', 20000);
  }

  /* ---------------- 目录选择与恢复 ---------------- */
  async function pickDir(mode) {
    if (!window.showDirectoryPicker) throw new Error('当前浏览器不支持文件夹选择（请用 Chrome/Edge 桌面版）');
    const h = await window.showDirectoryPicker({ mode: mode, id: 'swiftdrop-' + mode, startIn: 'downloads' });
    if (h.requestPermission) {
      let p = await h.queryPermission({ mode: mode });
      if (p !== 'granted') p = await h.requestPermission({ mode: mode });
      if (p !== 'granted') throw new Error('没有获得该文件夹的读写授权');
    }
    return h;
  }

  async function pickSaveDir() {
    try {
      const h = await pickDir('readwrite');
      App.saveDir = { handle: h, name: h.name };
      $('saveDirName').textContent = h.name;
      $('btnPickSaveDir').classList.add('hidden');
      $('btnPickSaveDir2').classList.remove('hidden');
      SD.idbSet('misc', 'saveDir', h);
      SD.log('接收目录已设为：' + h.name);
      toast('接收的文件会保存到「' + h.name + '」', 'ok');
    } catch (e) { if (e.name !== 'AbortError') toast('选择目录失败：' + e.message, 'err'); }
  }

  async function pickSyncDir() {
    try {
      const h = await pickDir('readwrite');
      App.syncDir = { handle: h, name: h.name };
      $('syncDirName').textContent = h.name;
      SD.idbSet('misc', 'syncDir', h);
      if (App.sync) await App.sync.setDir(h);
      SD.log('同步目录已设为：' + h.name);
      toast('同步文件夹已设为「' + h.name + '」', 'ok');
    } catch (e) { if (e.name !== 'AbortError') toast('选择目录失败：' + e.message, 'err'); }
  }

  async function restoreDirs() {
    for (const [k, setter] of [['saveDir', (h) => { App.saveDir = { handle: h, name: h.name }; $('saveDirName').textContent = h.name; $('btnPickSaveDir').classList.add('hidden'); $('btnPickSaveDir2').classList.remove('hidden'); }],
                               ['syncDir', (h) => { App.syncDir = { handle: h, name: h.name }; $('syncDirName').textContent = h.name; }]]) {
      try {
        const h = await SD.idbGet('misc', k);
        if (!h) continue;
        const p = h.queryPermission ? await h.queryPermission({ mode: 'readwrite' }) : 'granted';
        if (p === 'granted') { setter(h); SD.log('已恢复上次的' + (k === 'saveDir' ? '接收' : '同步') + '目录：' + h.name); }      } catch (e) {}
    }
  }

  /* ---------------- 收集文件 ---------------- */
  async function walkEntry(entry, prefix, out) {
    if (out.length >= 20000) return;
    if (entry.isFile) {
      const file = await new Promise((res, rej) => entry.file(res, rej));
      out.push({ path: prefix ? prefix + '/' + entry.name : entry.name, size: file.size, mtime: file.lastModified, file: file });
    } else if (entry.isDirectory) {
      const p = prefix ? prefix + '/' + entry.name : entry.name;
      const reader = entry.createReader();
      for (;;) {
        const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        if (!batch.length) break;
        for (const e of batch) await walkEntry(e, p, out);
        if (out.length >= 20000) break;
      }
    }
  }

  async function handleDrop(roots, plain) {
    if (!App.ready) { toast('还没有连接对方，请先建立连接', 'err'); return; }
    let out = [];
    if (roots && roots.length) {
      for (const r of roots) await walkEntry(r, '', out);
    } else {
      out = plain.map((f) => ({ path: f.webkitRelativePath || f.name, size: f.size, mtime: f.lastModified, file: f }));
    }
    if (!out.length) { toast('没有识别到文件', 'err'); return; }
    dispatch(out);
  }

  function sendFileList(files) {
    if (!App.ready) { toast('还没有连接对方，请先建立连接', 'err'); return; }
    const out = files.map((f) => ({ path: f.webkitRelativePath || f.name, size: f.size, mtime: f.lastModified, file: f }));
    if (!out.length) return;
    dispatch(out);
  }

  async function dispatch(entries) {
    const total = entries.reduce((a, b) => a + b.size, 0);
    SD.log('准备发送 ' + entries.length + ' 个文件，共 ' + SD.fmtBytes(total));
    if (!App.tf) { toast('传输引擎未就绪', 'err'); return; }
    try { await App.tf.send(entries, { mode: 'send' }); }
    catch (e) { SD.log('发送失败：' + e.message, 'err'); toast('发送失败：' + e.message, 'err'); }
  }

  /* ---------------- 连接流程 ---------------- */
  /** 链接/二维码只有"别人能访问到这个地址"时才有意义。 */
  function shareMode() {
    const host = location.hostname;
    // 桌面版内置窗口会把局域网地址通过 ?lan= 传进来：那是朋友真能打开的地址
    if (lanBase()) return 'link';
    if (location.protocol === 'file:') return 'code';           // 本地文件：链接指向自己的硬盘
    if (host === '127.0.0.1' || host === 'localhost' || host === '[::1]') return 'code'; // 手机 App 内/本机服务
    // App 的兜底加载用的是这个假域名，链接/二维码同样对别人无效
    if (/^appassets\./i.test(host)) return 'code';
    // 局域网地址是"别人真的能访问"的（同一 WiFi 内），链接/二维码有效
    return 'link';
  }
  /** 桌面版（或别处）注入的分享基地址，形如 http://192.168.1.5:8787/swiftdrop.html
   *  有 `lan` 参数就用它（桌面版会优先填**异地组网**地址，Radmin/Tailscale 那种）。 */
  function lanBase() {
    try {
      const q = new URLSearchParams(location.search).get('lan');
      return q && /^https?:\/\//i.test(q) ? q : '';
    } catch (e) { return ''; }
  }
  /** 桌面版同时给的局域网地址（仅作附带提示：同一个 WiFi 下用它更快）。 */
  function lanLocal() {
    try {
      const q = new URLSearchParams(location.search).get('lanlocal');
      return q && /^https?:\/\//i.test(q) ? q : '';
    } catch (e) { return ''; }
  }
  /** 分享地址是不是"异地组网"地址（Radmin 26/8、Tailscale 100.64/10 等）。 */
  function isGroupHost(url) {
    try {
      const h = new URL(url).hostname;
      if (/^26\./.test(h) || /^25\./.test(h)) return true;
      const m = h.match(/^(\d+)\.(\d+)\./);
      if (!m) return false;
      const a = +m[1], b = +m[2];
      if (a === 100 && b >= 64 && b <= 127) return true;   // Tailscale
      if (a === 10 && (b === 147 || b === 242 || b === 144 || b === 168)) return true;
      return false;
    } catch (e) { return false; }
  }
  function makeLink(code) {
    const base = lanBase();
    if (base) return base + '#c=' + code;
    return location.origin + location.pathname + '#c=' + code;
  }

  /** 按分享方式渲染"取件码 / 链接 / 二维码"三件套。 */
  function renderShare(code) {
    const mode = shareMode();
    $('codeText').textContent = code;
    const linkRow = $('linkRow') || $('linkText').parentNode;
    const hint = $('shareHint');
    const copy = $('btnCopyLink');
    if (mode === 'link') {
      linkRow.classList.remove('hidden');
      $('linkText').value = makeLink(code);
      if (copy) copy.textContent = '复制链接';
      drawQR($('linkText').value);
      if (hint) {
        const local = lanLocal();
        const kind = isGroupHost($('linkText').value) ? '异地组网' : '局域网';
        hint.textContent = '这是' + kind + '地址，发给朋友；他打开就能直接连上你。'
          + (local ? '（同一个 WiFi 下也可以用：' + local + '，通常更快）' : '');
      }
    } else {
      // 关键修复：这两个场景下链接/二维码对朋友是**废的**（指向你自己的硬盘或手机内部），
      // 以前照样显示、还写着"复制链接"，害得对方根本没进房间。
      linkRow.classList.add('hidden');
      if (copy) copy.textContent = '复制取件码';
      const cv = $('qrCanvas');
      if (cv) cv.classList.add('hidden');
      if (hint) {
        hint.textContent = '⚠ 当前是本地打开（' + (location.protocol === 'file:' ? 'file:// 本地文件' : '本机内部地址')
          + '），链接和二维码对朋友无效（指向你自己的设备）。正确做法：把 swiftdrop.html 这个文件发给朋友，'
          + '让他在自己设备上打开，然后手动输入这 9 位取件码。';
      }
    }
  }

  async function createSession() {
    if (App.sig) return;
    App.role = 'host';
    App.code = SD.randomCode(9);
    $('codeBox').classList.remove('hidden');
    $('btnCreate').disabled = true;
    $('btnCreate').textContent = '取件码已生成';
    history.replaceState(null, '', '#c=' + App.code);
    renderShare(App.code);
    chip('connChip', '连接：等待对方加入…', '');
    SD.log('取件码：' + App.code + '（把它发给朋友；分享方式：' + (shareMode() === 'link' ? '链接/二维码可用' : '只能发取件码，因为当前是本机地址') + '）');
    await startSignaling(App.code);
  }

  async function joinSession(code) {
    if (App.sig) return;
    App.role = 'guest';
    App.code = code;
    $('btnJoin').disabled = true;
    $('btnJoin').textContent = '连接中…';
    history.replaceState(null, '', '#c=' + code);
    chip('connChip', '连接：正在联系对方…', 'warn');
    SD.log('正在连接取件码：' + code);
    await startSignaling(code);
  }

  function drawQR(text) {
    const cv = $('qrCanvas');
    const Q = SD.QR || window.QR;
    if (!Q || !Q.toCanvas) { cv.classList.add('hidden'); SD.log('二维码模块未加载，已改用取件码/链接分享', 'warn'); return; }
    try { Q.toCanvas(text, cv, { ec: 'M' }); cv.classList.remove('hidden'); }
    catch (e) { cv.classList.add('hidden'); SD.log('二维码生成失败：' + e.message, 'warn'); }
  }

  function showTrouble(html) {
    const el = $('connTrouble');
    if (!el) return;
    el.innerHTML = html;
    el.classList.remove('hidden');
  }
  function clearTrouble() {
    const el = $('connTrouble');
    if (el) el.classList.add('hidden');
  }

  /** 确认"对方确实进来了"。
   *  注意：加入方永远收不到 hello（只有 offer），所以不能只在 hello 里判断，
   *  否则会在 15 秒后误报"对方没加入"（实测踩过）。 */
  function markPeerSeen(name) {
    if (App.peerSeen) return;
    App.peerSeen = true;
    clearTimeout(App._peerT);
    clearTrouble();
    chip('connChip', '连接：对方已加入，正在打洞…', 'warn');
    const nm = name || (App.link && App.link.remoteInfo && App.link.remoteInfo.name) || '对方';
    SD.log('对方已加入：' + nm + '（开始协商直连）');
    if (!App.seenToasted) {
      App.seenToasted = true;
      toast('对方已加入，正在打洞直连…', 'ok');
    }
  }

  /** 打洞确实打不通时给出可操作的三条路（不要只报"失败"）。 */
  function showNoPath() {
    if (App.connected) return;
    showTrouble('<b>双方打洞失败（P2P 直连没打通）</b><br>'
      + '说明至少一方在「运营商大内网 / 严格 NAT」后面。按成功率排序试：<br>'
      + '① <b>装一个免费组网工具</b>（最推荐，100% 有效，不需要公网 IP）：'
      + '<a href="https://tailscale.com/download" target="_blank" rel="noopener">Tailscale</a>（全平台，手机也能装）、'
      + '<a href="https://www.radmin-vpn.com/cn/" target="_blank" rel="noopener">Radmin VPN</a>（仅 Windows，国内快）、'
      + '<a href="https://www.zerotier.com/download/" target="_blank" rel="noopener">ZeroTier</a>。'
      + '两边加入同一个网络后，就像在同一个局域网里——本软件会优先用这个"异地组网地址"；<br>'
      + '② <b>手机开热点</b>给电脑连上，两台设备就在同一局域网，秒连（最省事）；<br>'
      + '③ 在 ⚙ 设置里填一台有公网 IP 的中继服务器（5 元/月 VPS 即可），程序会自动回退到中继转发。');
  }

  async function startSignaling(code) {
    try { App.relay.room = await SD.roomIdFromCode(code); } catch (e) { App.relay.room = ''; }
    App.sig = new SD.Signaling(code, {
      onStatus: (mode) => {
        if (mode === 'lan') chip('sigChip', '信令：局域网', 'ok');
        else if (mode === 'net') chip('sigChip', '信令：公网', 'ok');
        else if (mode === 'net+lan') chip('sigChip', '信令：公网+局域网', 'ok');
        else chip('sigChip', '信令：连接中…', 'warn');
        SD.log('信令通道：' + (mode || '无'));
      },
      onMessage: (m) => {
        if (m.t === 'relay-fallback' || m.t === 'relay-fallback-ok') { handleRelaySignal(m); return; }
        if (App.link) App.link.handleSignal(m);
      }
    });
    chip('sigChip', '信令：连接中…', 'warn');
    App.sig.start().then((ok) => {
      if (!ok) SD.log('暂时没有连上任何信令服务器，正在重试…（不影响局域网互传）', 'warn');
      if (SD.loadSettings().forceRelay) beginRelaySession();
    });
    clearTimeout(App._sigT);
    App._sigT = setTimeout(() => {
      const ready = App.sig && App.sig.ready;
      if (!App.connected && !ready) {
        showTrouble('<b>连不上信令服务器</b><br>你的网络可能屏蔽了这些公共服务器（常见于公司/校园网，或开了代理）。'
          + '试试：<b>换用手机热点</b>、关闭代理/VPN、或检查防火墙。<br>'
          + '（同一个 WiFi 时不受影响，可用桌面版的 webhost 走局域网。）');
      }
    }, 12000);

    // 强制中继：跳过 P2P，不创建 PeerLink
    if (SD.loadSettings().forceRelay) {
      SD.log('已开启「强制走中继」：跳过 P2P，直接连中继');
      return;
    }

    const pl = new SD.PeerLink({
      signaling: App.sig,
      role: App.role,
      iceServers: App.settings.iceServers,
      handlers: {
        onState: (st) => {
          if (App.link !== pl) return;   // 已切到中继后，忽略 PeerLink 迟到的状态
          const map = { new: ['正在建立连接…', 'warn'], connecting: ['正在打洞连接…', 'warn'], connected: ['已直连', 'ok'], disconnected: ['连接中断', 'warn'], failed: ['连接失败，重试中', 'err'], closed: ['已断开', 'err'], 'channel-closed': ['通道关闭', 'err'], bye: ['对方已断开', 'warn'] };
          const v = map[st] || [st, ''];
          chip('connChip', '连接：' + v[0], v[1]);
          // 走到 connecting 就说明 SDP 已经换完（对方确实进来了）——
          // 加入方永远收不到 hello，只能靠这个判断，否则会误报"对方没加入"。
          if (st === 'connecting' || st === 'connected') markPeerSeen();
          if (st === 'connected') { SD.log('P2P 直连已建立 ✔'); clearTrouble(); App.iceFails = 0; }
          if (st === 'bye' || st === 'closed') { App.ready = false; toast('与对方的连接已断开', 'warn'); }
          if (st === 'failed') { App.iceFails = (App.iceFails || 0) + 1; tryRelayFallback(); }
          clearTimeout(App._connT);
          if (st === 'connecting' || st === 'failed' || st === 'new') {
            App._connT = setTimeout(() => {
              if (!App.connected && App.sig && App.sig.ready) {
                tryRelayFallback();
                if (!App.connected) showNoPath();
              }
            }, 30000);
          }
          // 连续两次打洞失败就别让人干等了，直接给可操作的方案
          if (st === 'failed' && App.iceFails >= 2) showNoPath();
        },
        onReady: onReady,
        onPeerInfo: (m) => {
          if (App.link !== pl) return;
          $('peerName').textContent = (m.name || '对方') + (m.caps && m.caps.mobile ? '（手机）' : '');
          markPeerSeen(m.name || '对方');
        },
        onRtt: (ms) => { if (App.link !== pl) return; const c = $('rttChip'); c.classList.remove('hidden'); c.textContent = '延迟 ' + ms + ' ms'; },
        onControl: (m) => {
          if (App.tf) App.tf.onControlMessage(m);
          if (App.sync) App.sync.onControl(m, App.link && App.link.remoteInfo ? App.link.remoteInfo.name : '');
        },
        onData: (u8) => { if (App.tf) App.tf.onData(u8); }
      }
    });
    App.link = pl;
    pl.start();

    // 关键诊断：15 秒内没收到对方任何消息，就别让人干等——直接列出最可能的原因
    App.peerSeen = false;
    App.seenToasted = false;
    App.iceFails = 0;
    clearTimeout(App._peerT);
    App._peerT = setTimeout(() => {
      if (!App.peerSeen && !App.connected) {
        const mySig = App.sig ? App.sig.mode : '';
        showTrouble('<b>还没有收到对方任何信号</b>（连 offer 都没到）<br>逐条确认：<br>'
          + '① <strong>两边都打开了吗</strong>：把 swiftdrop.html 文件发给朋友，让他在自己设备上打开（手机建议直接装 App）；<br>'
          + '② <strong>只有一边点"生成取件码"</strong>，另一边必须输入取件码点「连接」——两边都点生成的话，谁都不会主动联系对方；<br>'
          + '③ 取件码<strong>完全一致</strong>（9 位，注意 O/0、I/1）；<br>'
          + '④ 对方的「信令」必须是<strong>公网</strong>：若对方一直显示"连接中…"，说明<strong>对方网络连不上公共信令服务器</strong>'
          + '（手机流量常见），让他换 WiFi、开/关代理再试，或用下面的 Tailscale 方案绕开公共信令。<br>'
          + '（你这边信令：' + (mySig || '未连接') + '）');
        SD.log('15 秒内没有收到对方任何信号：对方可能没加入/取件码不一致/对方信令没连上', 'warn');
      }
    }, 15000);
  }

  /* ---------------- 中继回退 ---------------- */
  async function tryRelayFallback() {
    if (App.connected) return;
    const s = SD.loadSettings();
    if (!s.relayUrl || !App.sig || !App.sig.ready) return;
    if (App.relay.state !== 'none') return;
    App.sig.publish({ t: 'relay-fallback', url: s.relayUrl, room: App.relay.room });
    SD.log('P2P 打洞失败，发起中继回退…');
    startRelay(s.relayUrl, App.relay.room);
  }

  async function beginRelaySession() {
    if (App.connected) return;
    const s = SD.loadSettings();
    if (s.relayUrl) {
      // 双方都配置了地址时，各自发起+直连（room 由取件码派生，保证配对）
      App.sig.publish({ t: 'relay-fallback', url: s.relayUrl, room: App.relay.room });
      startRelay(s.relayUrl, App.relay.room);
    } else if (App.role === 'host') {
      showTrouble('<b>强制走中继</b>需要在 ⚙ 设置里填写「中继服务器」地址（wss://…），否则无法连接。');
      SD.log('强制走中继模式但未配置中继服务器', 'err');
    }
    // 加入方没配地址时，等对方经信令发来的 relay-fallback 再连
  }

  function handleRelaySignal(m) {
    if (App.connected) return;
    const s = SD.loadSettings();
    if (m.t === 'relay-fallback') {
      if (App.relay.state === 'active' || App.relay.state === 'connecting') return;
      const url = (m.url || s.relayUrl || '').replace(/\/+$/, '');
      const room = m.room || App.relay.room;
      if (!url) return;
      App.sig.publish({ t: 'relay-fallback-ok', url: url, room: room });
      SD.log('对方发起中继回退，切换中继…');
      startRelay(url, room);
    } else if (m.t === 'relay-fallback-ok') {
      SD.log('对方已确认中继回退');
      if (App.relay.state === 'none') {
        const url = (m.url || s.relayUrl || '').replace(/\/+$/, '');
        if (url) startRelay(url, m.room || App.relay.room);
      }
    }
  }

  function startRelay(url, room) {
    if (!url || !room) return;
    if (App.relay.state === 'connecting' || App.relay.state === 'active') return;
    App.relay.state = 'connecting';
    App.relay.url = url;
    App.relay.room = room;
    clearTimeout(App.relay.timer);
    SD.log('正在连接中继：' + url + '（房间 ' + room + '）');

    const rl = new SD.RelayLink({
      url: url,
      roomId: room,
      name: SD.loadSettings().deviceName,
      caps: SD.caps(),
      handlers: {
        onReady: () => {
          if (App.relay.state === 'active') return;
          App.relay.state = 'active';
          clearTimeout(App.relay.timer);
          App.relay.link = rl;
          const old = App.link;
          App.link = rl;
          if (App.tf) App.tf.link = rl;
          if (App.sync) { App.sync.link = rl; App.sync.transfer = App.tf; }
          if (old && old !== rl && old.close) { try { old.close(false); } catch (e) {} }
          onReady();
        },
        onState: (st) => {
          if (App.link !== rl) return;
          if (st === 'bye' || st === 'closed' || st === 'error') {
            App.ready = false; App.connected = false;
            App.relay.state = 'none';
            clearTimeout(App.relay.timer);
            chip('connChip', st === 'bye' ? '连接：对方已断开' : st === 'error' ? '连接：中继连接失败' : '连接：中继断开', 'err');
            toast('中继连接已断开', 'warn');
          }
        },
        onPeerInfo: (m) => {
          if (App.link !== rl) return;
          $('peerName').textContent = (m.name || '对方') + (m.caps && m.caps.mobile ? '（手机）' : '');
        },
        onRtt: (ms) => { if (App.link !== rl) return; const c = $('rttChip'); c.classList.remove('hidden'); c.textContent = '延迟 ' + ms + ' ms'; },
        onControl: (m) => {
          if (App.tf) App.tf.onControlMessage(m);
          if (App.sync) App.sync.onControl(m, rl.remoteInfo ? rl.remoteInfo.name : '');
        },
        onData: (u8) => { if (App.tf) App.tf.onData(u8); }
      }
    });
    rl.start();

    // 容错：中继连不上 / 超时，回退到原提示，不崩
    App.relay.timer = setTimeout(() => {
      if (App.relay.state !== 'active') {
        SD.log('中继连接超时，回退到原提示', 'warn');
        App.relay.state = 'none';
        try { rl.close(false); } catch (e) {}
        if (!App.connected && !App.link) {
          showTrouble('<b>双方没能打洞直连，中继也没连上</b><br>请检查 ⚙ 设置里的中继服务器地址是否正确、服务器是否在线、防火墙是否放行。');
        }
      }
    }, 20000);
  }

  function onReady() {
    const first = !App.ready;
    const isRelay = App.link && App.link.isRelay;
    App.ready = true;
    App.connected = true;
    chip('connChip', isRelay ? '连接：中继已连接' : '连接：已直连', 'ok');
    $('connectPane').classList.add('hidden');
    $('sessionPane').classList.remove('hidden');
    if (!App.tf) {
      App.tf = new SD.Transfer({
        link: App.link,
        getTarget: getTarget,
        onEvent: onTransferEvent,
        onControl: (m) => { if (App.sync) App.sync.onControl(m, ''); }
      });
    } else { App.tf.link = App.link; }
    if (!App.sync) {
      App.sync = new SD.Sync({
        link: App.link,
        transfer: App.tf,
        getName: () => SD.loadSettings().deviceName,
        onEvent: onSyncEvent,
        onLog: syncLogLine
      });
    } else { App.sync.link = App.link; App.sync.transfer = App.tf; }
    const nm = (App.link.remoteInfo && App.link.remoteInfo.name) || '对方';
    $('peerName').textContent = nm;
    $('connKind').textContent = isRelay ? '（中继转发 · P2P 不可用）' : (App.role === 'host' ? '（我是发起方）' : '（我是加入方）');
    if (App.sync && App.syncDir) App.sync.setDir(App.syncDir.handle, nm).catch(() => {});
    if (first) {
      toast(isRelay ? '已通过中继连接（P2P 直连不可用），可以互传文件了' : '已和「' + nm + '」建立直连，可以互传文件了', 'ok');
      updateWatch();
    }
    Diag.start();
  }

  /* ---------------- 连接诊断（判断"慢在哪"） ---------------- */
  const Diag = {
    timer: null, samples: [], peak: 0, sum: 0, n: 0, lastBytes: 0, lastAt: 0,
    rttHist: [], slow: 0, last: {},
    reset() {
      this.samples = []; this.peak = 0; this.sum = 0; this.n = 0;
      this.lastBytes = 0; this.lastAt = 0; this.rttHist = []; this.slow = 0; this.last = {};
    },
    start() {
      this.reset();
      if (this.timer) return;
      this.tick();
      this.timer = setInterval(() => this.tick(), 1000);
    },
    stop() {
      if (this.timer) { clearInterval(this.timer); this.timer = null; }
    },
    rate(v) {
      if (!v || v < 1) return '0 B/s';
      const u = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
      let i = 0;
      while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
      return (i === 0 ? Math.round(v) : v.toFixed(v < 10 ? 1 : 0)) + ' ' + u[i];
    },
    async tick() {
      const link = App.link;
      if (!link) return;
      let d = {};
      try { d = (link.diag ? await link.diag() : {}) || {}; } catch (e) { d = {}; }
      const now = Date.now();
      const bytes = (d.bytesSent || 0) + (d.bytesReceived || 0);
      let sp = 0;
      if (this.lastAt && now > this.lastAt && bytes > this.lastBytes) {
        sp = (bytes - this.lastBytes) * 1000 / (now - this.lastAt);
      }
      this.lastAt = now; this.lastBytes = bytes;
      if (sp > 0) {
        this.samples.push(sp);
        if (this.samples.length > 60) this.samples.shift();
        this.peak = Math.max(this.peak, sp); this.sum += sp; this.n++;
        const avg = this.sum / this.n;
        if (sp < avg * 0.2) this.slow++;
      }
      if (d.rtt) { this.rttHist.push(d.rtt); if (this.rttHist.length > 20) this.rttHist.shift(); }
      this.last = d;
      this.render(d, sp);
    },
    jitter() {
      const h = this.rttHist;
      if (h.length < 3) return 0;
      let mn = Infinity, mx = 0;
      h.forEach((v) => { if (v < mn) mn = v; if (v > mx) mx = v; });
      return Math.max(0, mx - mn);
    },
    engine() {
      try { return (App.tf && App.tf.diagStats) ? App.tf.diagStats() : { waits: 0, drainMs: 0, stalls: 0 }; }
      catch (e) { return { waits: 0, drainMs: 0, stalls: 0 }; }
    },
    render(d, sp) {
      const en = this.engine();
      const jit = this.jitter();
      const avg = this.n ? this.sum / this.n : 0;
      const typeName = (t) => t === 'relay' ? '中继' : t === 'srflx' ? '公网映射' : t === 'prflx' ? '对端映射' : t === 'host' ? '本机地址' : (t || '?');
      const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
      set('dgLink', d.relay ? '中继转发' : ('直连 · ' + (App.role === 'host' ? '我发起' : '我加入')));
      set('dgRtt', d.rtt ? d.rtt + ' ms' : '未知');
      set('dgJit', this.rttHist.length >= 3 ? jit + ' ms' : '采样中…');
      set('dgBw', d.bw ? this.rate(d.bw / 8) : '未知');
      set('dgSpeed', sp > 0 ? this.rate(sp) : '空闲');
      set('dgPeak', this.n ? (this.rate(this.peak) + ' / ' + this.rate(avg)) : '-');
      set('dgStall', (en.stalls || 0) + ' 次' + (en.drainMs ? '（累计憋住 ' + (en.drainMs / 1000).toFixed(1) + 's）' : ''));
      set('dgCand', typeName(d.localType) + ' → ' + typeName(d.remoteType));
      this.chart();
      const v = this.verdict(d, sp, jit, en);
      const el = $('dgVerdict');
      if (el) { el.textContent = v.text; el.className = 'dgverdict ' + (v.cls || ''); }
      this.verdictText = v.text;
    },
    verdict(d, sp, jit, en) {
      const MB = 1024 * 1024;
      // 只用"真在传输时"的采样来判断：心跳/空闲会把瞬时速度拉到接近 0，
      // 拿它下结论会误判成"带宽不足"（这个坑实测踩到过）。
      const busy = this.samples.filter((v) => v > 64 * 1024);
      const busyAvg = busy.length ? busy.reduce((a, b) => a + b, 0) / busy.length : 0;
      const idle = !sp || sp < 64 * 1024;
      const ref = idle ? busyAvg : sp;
      if (this.n < 3 || ref <= 0) return { cls: '', text: '正在采样…（有传输时判断更准）' };
      const pre = idle ? '当前空闲（上次传输平均 ' + this.rate(busyAvg) + '）：' : '';
      const drainRatio = en.drainMs && this.n ? Math.min(1, (en.drainMs / 1000) / Math.max(1, this.n)) : 0;
      if (d.relay) {
        return { cls: 'warn', text: pre + '当前走中继转发：速度上限由中继服务器带宽决定；双方网络允许时应优先直连。' };
      }
      if (ref >= 8 * MB) return { cls: 'ok', text: pre + '链路状态良好（' + this.rate(ref) + '）。' };
      if (ref >= 1 * MB) {
        return { cls: 'ok', text: pre + '速度属于常见水平（' + this.rate(ref) + '）：这个量级多半是 WiFi 频段或跨网链路的正常上限。' };
      }
      if (drainRatio > 0.5) {
        return { cls: 'warn', text: pre + '发送管道长期被憋住（占 ' + Math.round(drainRatio * 100) + '% 时间）：像是链路丢包或带宽不足。建议改用桌面版 TCP 多流（配 Tailscale），抗丢包强得多。' };
      }
      if (d.rtt > 150 || jit > 80) {
        return { cls: 'warn', text: pre + '速度低（' + this.rate(ref) + '）且延迟/抖动大（RTT ' + d.rtt + 'ms，抖动 ' + jit + 'ms）：像是跨运营商绕行或链路拥塞，建议换时段再试。' };
      }
      return { cls: 'warn', text: pre + '速度低（' + this.rate(ref) + '）但延迟很小（' + d.rtt + 'ms）：更像发送方上行带宽不足（家宽上传常只有几百 Kbps）——可以让对方发给你试试。' };
    },
    chart() {
      const cv = $('dgChart');
      if (!cv || !cv.getContext) return;
      const w = Math.max(120, Math.floor(cv.clientWidth || 400));
      if (cv.width !== w) cv.width = w;
      const h = cv.height || 52;
      const ctx = cv.getContext('2d');
      ctx.clearRect(0, 0, w, h);
      if (!this.samples.length) return;
      const max = Math.max(this.peak, 1);
      const step = w / 60;
      const bw = Math.max(2, Math.floor(step) - 1);
      let col = '#0067c0';
      try { col = (getComputedStyle(document.documentElement).getPropertyValue('--accent') || col).trim() || col; } catch (e) {}
      ctx.fillStyle = col;
      this.samples.forEach((v, i) => {
        const bh = Math.max(1, Math.round((v / max) * (h - 6)));
        ctx.globalAlpha = 0.35 + 0.65 * (v / max);
        ctx.fillRect(Math.round(i * step), h - bh, bw, bh);
      });
      ctx.globalAlpha = 1;
    },
    copy() {
      const d = this.last || {};
      const en = this.engine();
      const avg = this.n ? this.sum / this.n : 0;
      const lines = [
        '棕仙的传输软件 连接诊断',
        '时间：' + new Date().toLocaleString(),
        '角色：' + (App.role === 'host' ? '发起方（生成取件码）' : '加入方（输入取件码）'),
        '链路：' + (d.relay ? '中继转发' : 'P2P 直连') + '，候选 ' + (d.localType || '?') + ' → ' + (d.remoteType || '?'),
        'RTT：' + (d.rtt || 0) + ' ms，抖动：' + this.jitter() + ' ms',
        '可用带宽估计：' + (d.bw ? this.rate(d.bw / 8) : '未知'),
        '实时速度：' + this.rate(this.samples.length ? this.samples[this.samples.length - 1] : 0),
        '峰值：' + this.rate(this.peak) + '，平均：' + this.rate(avg) + '，采样 ' + this.n + ' 次',
        '发送管道憋住：' + (en.stalls || 0) + ' 次，累计 ' + (en.drainMs || 0) + ' ms（等待调用 ' + (en.waits || 0) + ' 次）',
        '慢速采样次数（含传输间隙，仅供参考）：' + this.slow,
        '累计收发：' + (d.bytesSent || 0) + ' B 发出 / ' + (d.bytesReceived || 0) + ' B 收到',
        '结论：' + (this.verdictText || '（尚未判断）')
      ];
      return lines.join('\n');
    }
  };
  App.Diag = Diag;

  function leave() {
    if (App.link) { App.link.close(true); }
    if (App.relay.link) { try { App.relay.link.close(true); } catch (e) {} }
    if (App.sig) { App.sig.stop(); }
    clearTimeout(App.relay.timer);
    App.relay = { state: 'none', link: null, room: '', url: '', timer: 0 };
    App.ready = false; App.connected = false;
    Diag.stop();
    App.tf = null; App.sync = null; App.link = null; App.sig = null;
    if (App.watchTimer) { clearInterval(App.watchTimer); App.watchTimer = 0; }
    $('sessionPane').classList.add('hidden');
    $('connectPane').classList.remove('hidden');
    $('btnCreate').disabled = false;
    $('btnCreate').textContent = '生成取件码';
    $('btnJoin').disabled = false;
    $('btnJoin').textContent = '连接';
    $('codeBox').classList.add('hidden');
    $('joinCode').value = '';
    $('syncWatch').checked = false;
    chip('connChip', '未连接', '');
    chip('sigChip', '信令：已断开', '');
    $('rttChip').classList.add('hidden');
    SD.log('已断开本次会话');
  }

  /* ---------------- 接收位置 ---------------- */
  async function getTarget(mode) {
    if (mode === 'sync') {
      if (!App.syncDir) throw new Error('尚未选择同步文件夹');
      return { mode: 'fsa', dirHandle: App.syncDir.handle, key: 'sync:' + App.syncDir.handle.name, name: App.syncDir.name };
    }
    if (App.saveDir) {
      const p = App.saveDir.handle.queryPermission ? await App.saveDir.handle.queryPermission({ mode: 'readwrite' }) : 'granted';
      if (p !== 'granted') {
        const q = await App.saveDir.handle.requestPermission({ mode: 'readwrite' });
        if (q === 'granted') return { mode: 'fsa', dirHandle: App.saveDir.handle, key: 'save:' + App.saveDir.handle.name, name: App.saveDir.name };
        throw new Error('接收目录授权已失效，请重新选择');
      }
      return { mode: 'fsa', dirHandle: App.saveDir.handle, key: 'save:' + App.saveDir.handle.name, name: App.saveDir.name };
    }
    return { mode: 'memory' };
  }

  /* ---------------- 传输事件与渲染 ---------------- */
  function onTransferEvent(e) {
    if (e.type === 'log') { SD.log(e.msg, e.level === 'err' ? 'err' : e.level); return; }
    if (e.type === 'task') { renderTask(e.task); }
  }

  function renderTask(t) {
    let row = App.rows.get(t.id);
    if (!row) {
      const name = SD.h('span', { class: 'fname' });
      const meta = SD.h('span', { class: 'fmeta' });
      const bar = SD.h('i');
      const st = SD.h('span', { class: 'st' });
      const sp = SD.h('span', { class: 'sp' });
      const save = SD.h('button', { class: 'save hidden', text: '保存文件' });
      const badge = SD.h('span', { class: 'badge ' + (t.dir === 'send' ? 'send' : 'recv'), text: t.dir === 'send' ? '发送' : '接收' });
      const node = SD.h('div', { class: 'fitem' }, [
        SD.h('div', { class: 'fhead' }, [badge, name, meta]),
        SD.h('div', { class: 'bar' }, [bar]),
        SD.h('div', { class: 'fsub' }, [st, sp]),
        save
      ]);
      save.addEventListener('click', () => {
        const a = document.createElement('a');
        a.href = t.url; a.download = baseName(t.path);
        document.body.appendChild(a); a.click(); a.remove();
      });
      $('fileList').insertBefore(node, $('fileList').firstChild);
      row = { node: node, name: name, meta: meta, bar: bar, st: st, sp: sp, save: save, task: t };
      App.rows.set(t.id, row);
      App.tasks.set(t.id, t);
    }
    const tt = row.task = t;
    App.tasks.set(t.id, t);
    row.name.textContent = t.path;
    row.name.title = t.path + (t.hash ? '\nSHA-256: ' + t.hash : '');
    row.meta.textContent = SD.fmtBytes(t.size);
    const pct = t.size ? Math.min(100, (t.got / t.size) * 100) : (t.state === 'done' ? 100 : 0);
    row.bar.style.width = pct.toFixed(1) + '%';
    row.node.className = 'fitem' + (t.state === 'done' ? ' done' : t.state === 'error' ? ' error' : '');
    const stateText = { wait: '排队中', run: '传输中', done: '完成', error: '出错', skip: '已跳过' }[t.state] || t.state;
    row.st.textContent = stateText + (t.msg && t.msg !== stateText ? ' · ' + t.msg : '');
    let extra = '';
    if (t.state === 'run') {
      extra = SD.fmtSpeed(t.speed) + (t.eta ? ' · 剩余 ' + SD.fmtEta(t.eta) : '') + ' · ' + SD.fmtBytes(t.got) + '/' + SD.fmtBytes(t.size);
    } else if (t.state === 'done') {
      extra = SD.fmtBytes(t.got || t.size) + (t.hash ? ' · 校验通过 ' + t.hash.slice(0, 12) + '…' : '');
    } else if (t.state === 'error') {
      extra = SD.fmtBytes(t.got) + '/' + SD.fmtBytes(t.size);
    }
    row.sp.textContent = extra;
    if (t.url) {
      row.save.classList.remove('hidden');
      if (!t._autoSaved) {
        t._autoSaved = true;
        if (App.native) {
          // 安卓客户端：流式 POST 到本机 /__save，直接落到系统"下载"目录
          (async () => {
            try {
              t.msg = '正在保存到下载目录…'; renderTask(t);
              const blob = await (await fetch(t.url)).blob();
              const r = await fetch(App.native.saveUrl + '?name=' + encodeURIComponent(baseName(t.path)), { method: 'POST', body: blob });
              const j = await r.json().catch(() => null);
              const okSave = j && j.ok;
              t.msg = okSave ? '已保存到手机下载目录' : '保存失败';
              if (App.native.toast) App.native.toast(okSave ? '已保存：' + baseName(t.path) : '保存失败');
              toast(okSave ? '已保存到手机下载目录：' + baseName(t.path) : '保存到手机失败', okSave ? 'ok' : 'err', 7000);
              SD.log(okSave ? '已保存到下载目录：' + (j.path || t.path) : '保存到下载目录失败', okSave ? 'info' : 'err');
            } catch (e) {
              t.msg = '保存失败：' + e.message;
              toast('保存到手机失败：' + e.message, 'err', 8000);
            }
            renderTask(t);
          })();
        } else {
          try { row.save.click(); } catch (e) {}
        }
      }
    }
  }

  function updateTotals() {
    let spd = 0, active = 0, done = 0;
    App.tasks.forEach((t) => {
      if (t.state === 'run') { spd += t.speed || 0; active++; }
      else if (t.state === 'wait') active++;
      if (t.state === 'done') done++;
    });
    $('totalSpeed').textContent = spd > 0 ? SD.fmtSpeed(spd) : (active ? '计算中…' : '-');
    $('queueInfo').textContent = active ? '进行中 ' + active + ' 个任务' : (done ? '已完成 ' + done + ' 个文件' : '');
  }

  /* ---------------- 同步 ---------------- */
  function onSyncEvent(e) {
    if (e.ev === 'plan') {
      const box = $('syncPlan');
      box.classList.remove('hidden');
      let html = '<b>同步计划</b><div>发送 ' + e.send + ' 个 / 接收 ' + e.recv + ' 个 / 删除 ' + e.del + ' 个';
      if (e.conflicts) html += ' / <span style="color:#ffd08a">需注意 ' + e.conflicts + ' 个</span>';
      html += '</div>';
      if (e.conflictList && e.conflictList.length) {
        html += '<ul>' + e.conflictList.slice(0, 8).map((x) => '<li>' + x.replace(/[<>&]/g, '') + '</li>').join('') + '</ul>';
      }
      box.innerHTML = html;
    } else if (e.ev === 'done') {
      toast('同步完成：发送 ' + e.sum.send + ' / 接收 ' + e.sum.recv + ' / 删除 ' + e.sum.del + (e.sum.conflicts ? ' / 注意 ' + e.sum.conflicts + ' 项' : ''), 'ok', 6000);
    } else if (e.ev === 'error') {
      toast('同步失败：' + e.msg, 'err', 7000);
    }
  }

  async function doSync() {
    if (!App.ready) { toast('还没有连接对方', 'err'); return; }
    if (!App.syncDir) { toast('请先选择本地同步文件夹', 'err'); return; }
    if (!App.sync) { toast('同步引擎未就绪', 'err'); return; }
    const mode = $('syncMode').value;
    const del = $('syncDelete').checked;
    App.sync.mode = mode; App.sync.deleteExtra = del;
    $('btnSyncNow').disabled = true;
    try { await App.sync.start({ dirHandle: App.syncDir.handle, mode: mode, deleteExtra: del }); }
    catch (e) { toast('同步出错：' + e.message, 'err'); SD.log('同步出错：' + e.message, 'err'); }
    finally { $('btnSyncNow').disabled = false; }
  }

  function updateWatch() {
    if (App.watchTimer) { clearInterval(App.watchTimer); App.watchTimer = 0; }
    if (!$('syncWatch').checked) return;
    if (App.role !== 'host') { SD.log('自动监控只在发起方（生成取件码的一方）生效，避免双方同时推送', 'warn'); toast('自动监控在对方（加入方）不会生效，请让发起方开启', 'warn', 6000); }
    App.watchTimer = setInterval(async () => {
      if (!App.ready || !App.syncDir || !App.sync) return;
      if (App.role !== 'host') return;
      App.sync.mode = $('syncMode').value;
      App.sync.deleteExtra = $('syncDelete').checked;
      try { await App.sync.watchTick(); } catch (e) {}
    }, 5000);
    SD.log('自动监控已开启（每 5 秒检查一次）');
  }

  /* ---------------- 设置 ---------------- */
  function openSettings() {
    const s = SD.loadSettings();
    $('setDeviceName').value = s.deviceName || '';
    $('setTurnUrl').value = s.turnUrl || '';
    $('setTurnUser').value = s.turnUser || '';
    $('setTurnPass').value = s.turnPass || '';
    $('setRelayUrl').value = s.relayUrl || '';
    $('setForceRelay').checked = !!s.forceRelay;
    $('setChunk').value = String(s.chunkSize);
    $('setBuffer').value = String(s.bufferHigh);
    $('setBrokers').value = (s.brokers || SD.MQTT_BROKERS).join('\n');
    $('settingsDialog').showModal();
  }

  function saveSettings() {
    const brokers = $('setBrokers').value.split('\n').map((x) => x.trim()).filter(Boolean);
    SD.saveSettings({
      deviceName: $('setDeviceName').value.trim() || SD.guessDeviceName(),
      turnUrl: $('setTurnUrl').value.trim(),
      turnUser: $('setTurnUser').value.trim(),
      turnPass: $('setTurnPass').value,
      relayUrl: $('setRelayUrl').value.trim(),
      forceRelay: !!$('setForceRelay').checked,
      chunkSize: parseInt($('setChunk').value, 10) || 65536,
      bufferHigh: parseInt($('setBuffer').value, 10) || 12582912,
      brokers: brokers.length ? brokers : null
    });
    App.settings = SD.loadSettings();
    SD.log('设置已保存（TURN / 分片大小等将在下次连接时生效）');
    toast('设置已保存', 'ok');
  }

  /* ---------------- URL 自动加入 ---------------- */
  function autoJoinFromUrl() {
    let code = '';
    const mh = /[#&]c=([0-9A-Za-z]+)/.exec(location.hash || '');
    const mq = /[?&]c=([0-9A-Za-z]+)/.exec(location.search || '');
    if (mh) code = mh[1]; else if (mq) code = mq[1];
    code = SD.normalizeCode(code);
    if (code.length >= 6) {
      $('joinCode').value = code;
      SD.log('检测到链接里的取件码，正在自动连接…');
      setTimeout(() => joinSession(code), 300);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
