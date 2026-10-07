/* 棕仙的传输软件 —— 文件夹同步引擎（基于三方状态比较，rsync 思路）
 * 只有"驱动方"计算并执行同步计划，另一方只负责响应（提供文件、执行删除），
 * 这样不会出现双方同时推送、互相覆盖的问题。
 * 状态库：记录上次同步时两侧各自的大小/时间，用来判断"谁改过了"。
 */
(function (global) {
  'use strict';
  const SD = global.SD;

  const IGNORE = [
    /^\.swiftdrop/, /\.part$/, /^~\$/, /^\.DS_Store$/, /^Thumbs\.db$/, /^desktop\.ini$/,
    /^\.git$/, /^node_modules$/, /^__pycache__$/, /^\.idea$/, /^\.vscode$/, /^\$RECYCLE\.BIN$/
  ];
  const MAX_ENTRIES = 20000;
  const MAX_DEPTH = 12;

  function ignored(name) { return IGNORE.some((r) => r.test(name)); }

  async function scanDir(dirHandle, prefix, out, depth) {
    if (out.length >= MAX_ENTRIES || depth > MAX_DEPTH) return;
    const entries = [];
    for await (const [name, h] of dirHandle.entries()) {
      if (ignored(name)) continue;
      entries.push([name, h]);
    }
    for (const [name, h] of entries) {
      if (out.length >= MAX_ENTRIES) return;
      const p = prefix ? prefix + '/' + name : name;
      if (h.kind === 'directory') { await scanDir(h, p, out, depth + 1); }
      else {
        try { const f = await h.getFile(); out.push({ path: p, size: f.size, mtime: f.lastModified }); } catch (e) {}
      }
    }
  }

  async function resolveFile(root, relPath) {
    const parts = String(relPath).split('/').filter((x) => x && x !== '.' && x !== '..');
    const name = parts.pop();
    let h = root;
    for (const p of parts) h = await h.getDirectoryHandle(p);
    return h.getFileHandle(name);
  }

  async function resolveDir(root, relPath) {
    const parts = String(relPath).split('/').filter((x) => x && x !== '.' && x !== '..');
    let h = root;
    for (const p of parts) h = await h.getDirectoryHandle(p);
    return h;
  }

  async function removeAt(root, relPath) {
    const parts = String(relPath).split('/').filter((x) => x && x !== '.' && x !== '..');
    const name = parts.pop();
    const dir = parts.length ? await resolveDir(root, parts.join('/')) : root;
    await dir.removeEntry(name, { recursive: true });
  }

  async function ensureDir(root, parts) {
    let h = root;
    for (const p of parts) h = await h.getDirectoryHandle(p, { create: true });
    return h;
  }

  function sigOf(files) {
    // 用 路径数 + 总大小 + 最大时间 做轻量指纹
    let size = 0, mx = 0;
    for (const f of files) { size += f.size; if (f.mtime > mx) mx = f.mtime; }
    return files.length + ':' + size + ':' + Math.round(mx / 1000);
  }

  class Sync {
    constructor(opts) {
      this.link = opts.link;
      this.transfer = opts.transfer;
      this.getName = opts.getName || (() => '本机');
      this.onEvent = opts.onEvent || function () {};
      this.onLog = opts.onLog || function () {};
      this.root = null;          // 本地同步目录句柄
      this.key = '';             // 状态库键
      this.state = {};           // relpath -> {ls,lm,rs,rm}
      this.local = null;         // 本地清单
      this.remote = null;        // 对方清单
      this.remoteSig = '';
      this.pendingStart = null;
      this.pendingNeed = null;
      this.stateCache = new Map();
      this.lastSig = '';
      this.rounds = 0;
    }

    _log(m, l) { SD.log('[同步] ' + m, l); this.onLog(m, l || 'info'); }
    _emit(o) { o.type = 'sync'; this.onEvent(o); }

    /* ---------- 设定本地同步目录（双方都要设，被动方也要） ---------- */
    async setDir(handle, peerName) {
      this.root = handle || null;
      const pn = peerName || (this.link && this.link.remoteInfo && this.link.remoteInfo.name) || 'peer';
      this.key = handle ? ('sync:' + handle.name + '|' + pn) : '';
      if (this.root) await this.loadState();
      return this.root;
    }

    /* ---------- 状态库 ---------- */
    async loadState() {
      const rec = await SD.idbGet('syncstate', this.key);
      this.state = (rec && rec.state) || {};
      this.stateDirty = false;
    }
    async saveState() {
      await SD.idbSet('syncstate', this.key, { state: this.state, updated: Date.now(), dir: this.root ? this.root.name : '' });
    }
    async resetState() { this.state = {}; await this.saveState(); }

    /* ---------- 控制消息入口 ---------- */
    async onControl(m, fromName) {
      switch (m.t) {
        case 'sync-start': {
          if (!this.root) {
            this._log('对方发起了同步，但本机没有选择同步目录', 'warn');
            this.control({ t: 'sync-error', sid: m.sid, msg: '对方尚未选择同步目录' });
            return;
          }
          this.pendingStart = m;
          const files = await this._scan();
          await this.transfer.sendBig({ t: 'sync-manifest', sid: m.sid, files: files, sig: sigOf(files), name: this.getName() });
          this._log('对方发起了文件夹同步（' + files.length + ' 个文件），已发送本机清单');
          this._emit({ ev: 'remote-start', mode: m.mode });
          break;
        }
        case 'sync-manifest': {
          this.remote = m.files || [];
          this.remoteSig = m.sig || sigOf(this.remote);
          if (this.pendingStart) { const st = this.pendingStart; this.pendingStart = null; this._driveFrom(st); }
          if (this._waitRemote) { const r = this._waitRemote; this._waitRemote = null; r(this.remote); }
          break;
        }
        case 'sync-error': {
          this._log('对方报告错误：' + (m.msg || ''), 'err');
          this._emit({ ev: 'error', msg: m.msg || '对方出错' });
          this.syncError = m.msg || '对方出错';
          if (this._waitRemote) { const r = this._waitRemote; this._waitRemote = null; r(null); }
          break;
        }
        case 'sync-scan': {
          const files = await this._scan();
          await this.transfer.sendBig({ t: 'sync-manifest', sid: m.sid, files: files, sig: sigOf(files), rescan: true, name: this.getName() });
          break;
        }
        case 'sync-need': {
          const paths = m.paths || [];
          if (!paths.length) break;
          this._log('对方请求 ' + paths.length + ' 个文件');
          const entries = [];
          for (const p of paths) {
            try { const fh = await resolveFile(this.root, p); const f = await fh.getFile(); entries.push({ path: p, size: f.size, mtime: f.lastModified, file: f }); } catch (e) {}
          }
          if (entries.length) await this.transfer.send(entries, { mode: 'sync' });
          break;
        }
        case 'sync-del': {
          const paths = m.paths || [];
          let n = 0;
          for (const p of paths) { try { await removeAt(this.root, p); n++; } catch (e) {} }
          this._log('按对方要求删除了 ' + n + ' 个文件', n ? 'warn' : 'info');
          break;
        }
        case 'sync-done': { this._log('对方同步完成：' + JSON.stringify(m.sum || {})); break; }
      }
    }

    async _scan() {
      const out = [];
      if (this.root) await scanDir(this.root, '', out, 0);
      out.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
      this.local = out;
      return out;
    }

    /* ---------- 发起一次同步（驱动方） ---------- */
    async start(opts) {
      opts = opts || {};
      const peerName = (this.link.remoteInfo && this.link.remoteInfo.name) || 'peer';
      await this.setDir(opts.dirHandle || this.root, peerName);
      if (!this.root) throw new Error('请先选择本地同步目录');
      const sid = SD.randomCode(8);
      this.sid = sid;
      const files = await this._scan();
      const sig = sigOf(files);
      this._log('开始同步：本机 ' + files.length + ' 个文件，共 ' + SD.fmtBytes(files.reduce((a, b) => a + b.size, 0)));
      this._emit({ ev: 'start', local: files.length });
      this.syncError = '';
      const remoteP = new Promise((r) => (this._waitRemote = r));
      this.control({ t: 'sync-start', sid: sid, mode: opts.mode || 'two-way', deleteExtra: !!opts.deleteExtra, name: this.getName(), sig: sig });
      this.transfer.sendBig({ t: 'sync-manifest', sid: sid, files: files, sig: sig, name: this.getName() });
      const remote = await Promise.race([remoteP, new Promise((r) => setTimeout(() => r(null), 60000))]);
      if (this.syncError) { this._emit({ ev: 'error', msg: this.syncError }); return; }
      if (!remote) { this._log('等待对方文件清单超时', 'err'); this._emit({ ev: 'error', msg: '等待对方清单超时' }); return; }
      await this._drive({ sid: sid, mode: opts.mode || 'two-way', deleteExtra: !!opts.deleteExtra });
    }

    control(o) { this.link.sendControl(o); }

    async _waitRemoteManifest(sid) {
      const p = new Promise((r) => (this._waitRemote = r));
      this.control({ t: 'sync-scan', sid: sid });
      const r = await Promise.race([p, new Promise((rr) => setTimeout(() => rr(null), 60000))]);
      return r;
    }

    async _driveFrom(st) {
      // 收到 sync-start 后：本机是被动方，等对方的 sync-need/sync-del 即可
      this._emit({ ev: 'passive' });
    }

    /* ---------- 核心：比较 + 执行 ---------- */
    async _drive(opts) {
      const sid = opts.sid;
      const L = this.local, R = this.remote || [];
      const lm = new Map(L.map((f) => [f.path, f]));
      const rm = new Map(R.map((f) => [f.path, f]));
      const all = new Set([...lm.keys(), ...rm.keys()]);
      const state = this.state;
      const myName = this.getName(), peerName = (this.link.remoteInfo && this.link.remoteInfo.name) || 'peer';
      const iWinTie = myName <= peerName;

      const toSend = [], toRecv = [], toDelRemote = [], toDelLocal = [], keep = [], conflicts = [];

      for (const p of all) {
        const l = lm.get(p), r = rm.get(p), s = state[p];
        if (!l && !r) continue;
        if (!s) {
          if (l && r) {
            if (l.size === r.size) { keep.push(p); }
            else {
              // 从未同步过且大小不同：新者胜，时间接近则按名字定胜负（保证双方算出同一结果）
              const iNewer = Math.abs(l.mtime - r.mtime) < 2000 ? iWinTie : l.mtime > r.mtime;
              if (iNewer) toSend.push(p); else toRecv.push(p);
              conflicts.push(p + '（未同步过，大小不同）');
            }
          } else if (l) toSend.push(p);
          else toRecv.push(p);
          continue;
        }
        const lc = l ? (l.size !== s.ls || Math.abs(l.mtime - s.lm) > 1500) : true;
        const rc = r ? (r.size !== s.rs || Math.abs(r.mtime - s.rm) > 1500) : true;
        if (l && r) {
          if (!lc && !rc) { keep.push(p); continue; }
          if (lc && !rc) { toSend.push(p); continue; }
          if (!lc && rc) { toRecv.push(p); continue; }
          const iNewer = Math.abs(l.mtime - r.mtime) < 2000 ? iWinTie : l.mtime > r.mtime;
          if (iNewer) toSend.push(p); else toRecv.push(p);
          conflicts.push(p);
          continue;
        }
        if (l && !r) { // 对方删了或本地新加
          if (!lc && opts.deleteExtra) { toDelLocal.push(p); }
          else if (!lc) { conflicts.push(p + '（对方已删除，本机保留）'); }
          else { toSend.push(p); conflicts.push(p + '（本机新增/改动，对方已删除）'); }
          continue;
        }
        if (!l && r) {
          if (!rc && opts.deleteExtra) { toDelRemote.push(p); }
          else if (!rc) { conflicts.push(p + '（本机已删除，对方保留）'); }
          else { toRecv.push(p); conflicts.push(p + '（对方新增/改动，本机已删除）'); }
        }
      }

      this._log(`计划：发送 ${toSend.length}，接收 ${toRecv.length}，删除 ${toDelRemote.length + toDelLocal.length}，已一致 ${keep.length}`);

      // 单向模式：只推或只拉，且不动删除
      const oneWay = opts.mode === 'up' || opts.mode === 'down';
      if (opts.mode === 'up') { toRecv.length = 0; }
      if (opts.mode === 'down') { toSend.length = 0; }
      if (oneWay) { toDelRemote.length = 0; toDelLocal.length = 0; }

      this._emit({ ev: 'plan', send: toSend.length, recv: toRecv.length, del: toDelRemote.length + toDelLocal.length, conflicts: conflicts.length, conflictList: conflicts.slice(0, 50) });

      // 1) 让对端删除
      if (toDelRemote.length) {
        this.control({ t: 'sync-del', sid: sid, paths: toDelRemote.slice(0, 5000) });
        this._log('要求对方删除 ' + toDelRemote.length + ' 个文件', 'warn');
      }
      // 2) 本地删除
      let deletedLocal = 0;
      for (const p of toDelLocal) { try { await removeAt(this.root, p); deletedLocal++; } catch (e) {} }
      // 3) 请求对方发送
      if (toRecv.length) {
        this.control({ t: 'sync-need', sid: sid, paths: toRecv.slice(0, 5000) });
      }
      // 4) 本机发送
      if (toSend.length) {
        const entries = [];
        for (const p of toSend) {
          try { const fh = await resolveFile(this.root, p); const f = await fh.getFile(); entries.push({ path: p, size: f.size, mtime: f.lastModified, file: f }); }
          catch (e) { this._log('读取失败，跳过：' + p, 'warn'); }
        }
        if (entries.length) await this.transfer.send(entries, { mode: 'sync' });
      }

      // 5) 收尾：重新取双方清单，写入状态
      const L2 = await this._scan();
      const R2 = (await this._waitRemoteManifest(sid)) || this.remote;
      const l2 = new Map(L2.map((f) => [f.path, f]));
      const r2 = new Map((R2 || []).map((f) => [f.path, f]));
      let recorded = 0;
      for (const p of new Set([...l2.keys(), ...r2.keys()])) {
        const a = l2.get(p), b = r2.get(p);
        if (a && b) { this.state[p] = { ls: a.size, lm: a.mtime, rs: b.size, rm: b.mtime }; recorded++; }
      }
      for (const p of [...toSend, ...toRecv]) { if (!this.state[p]) { const a = l2.get(p), b = r2.get(p); if (a && b) { this.state[p] = { ls: a.size, lm: a.mtime, rs: b.size, rm: b.mtime }; recorded++; } } }
      await this.saveState();
      this.rounds++;
      const sum = { send: toSend.length, recv: toRecv.length, del: toDelRemote.length + toDelLocal.length, conflicts: conflicts.length };
      this._log('本轮同步完成：' + JSON.stringify(sum));
      this._emit({ ev: 'done', sum: sum, conflictList: conflicts.slice(0, 50) });
      this.control({ t: 'sync-done', sid: sid, sum: sum });
      this.lastSig = sigOf(L2);
      return sum;
    }

    /* ---------- 监控模式 ---------- */
    async watchTick() {
      if (!this.root || this.syncing) return false;
      const files = await this._scan();
      const sig = sigOf(files);
      if (sig === this.lastSig) return false;   // 本机没变化就不打扰对方
      this.syncing = true;
      try { await this.start({ dirHandle: this.root, mode: this.mode || 'two-way', deleteExtra: this.deleteExtra }); }
      catch (e) { this._log('自动同步出错：' + e.message, 'err'); }
      finally { this.syncing = false; }
      return true;
    }
  }

  SD.Sync = Sync;
  SD.syncUtil = { scanDir, resolveFile, resolveDir, removeAt, ensureDir, sigOf, ignored, IGNORE };
})(window);
