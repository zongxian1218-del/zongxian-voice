/* 棕仙的传输软件 —— 传输引擎
 * 关键设计：
 *  - 控制消息走 c 通道，文件数据走 d 通道；文件相关的帧（开始/分片/结束）全在 d 通道上，
 *    靠 SCTP 的顺序保证"结束帧一定在所有分片之后"，避免跨通道乱序。
 *  - 4MB 为一"块"做 SHA-256，块摘要串成整文件指纹；断点续传只从块边界恢复，无需回读磁盘。
 *  - 背压：用 bufferedAmount 控制，避免一次性灌爆内存。
 */
(function (global) {
  'use strict';
  const SD = global.SD;
  const TD = new TextDecoder();
  const TE = new TextEncoder();

  const BLOCK = 4 * 1024 * 1024;   // 哈希块大小
  const WRITE_BUF = 1024 * 1024;   // 每次顺序写盘的字节数
  const BIG_PART = 32 * 1024;      // 大 JSON 消息分片大小
  const F = { CHUNK: 1, DONE: 2, BIG: 3, START: 4 };

  const wr32 = (u8, o, n) => { u8[o] = (n >>> 24) & 255; u8[o + 1] = (n >>> 16) & 255; u8[o + 2] = (n >>> 8) & 255; u8[o + 3] = n & 255; };
  const rd32 = (u8, o) => (u8[o] * 0x1000000) + (u8[o + 1] << 16) + (u8[o + 2] << 8) + u8[o + 3];

  class Speed {
    constructor() { this.t0 = performance.now(); this.b0 = 0; this.cur = 0; this._t = this.t0; this._b = 0; }
    push(bytes) {
      const t = performance.now();
      if (t - this._t > 500) {
        this.cur = (bytes - this._b) / ((t - this._t) / 1000);
        this._t = t; this._b = bytes;
      }
      return this.cur;
    }
  }

  class Transfer {
    constructor(opts) {
      this.link = opts.link;
      this.getTarget = opts.getTarget;          // async (mode) => 目标描述
      this.onEvent = opts.onEvent || function () {};
      this.onControl = opts.onControl || null;  // 未识别的控制消息交回上层（同步用）
      this.tidSeq = 0;
      this.jobs = new Map();                    // tid -> 发送任务
      this.queue = [];
      this.busy = false;
      this.recv = null;
      this.bigIn = new Map();
      this.bigId = 1;
      this.stopped = false;
      this.tasks = new Map();
    }

    /* ================= 通用 ================= */
    _task(o) {
      const t = Object.assign({
        id: o.id, dir: o.dir, path: o.path, size: o.size, got: o.got || 0,
        state: o.state || 'wait', msg: o.msg || '', url: '', started: 0, speed: 0, eta: 0, hash: ''
      }, o);
      this.tasks.set(t.id, t);
      return t;
    }
    _emit(t) { this.onEvent({ type: 'task', task: t }); }
    _emitLog(msg, level) { this.onEvent({ type: 'log', msg: msg, level: level || 'info' }); }

    sendControl(o) { return this.link.sendControl(o); }

    /* ================= 数据帧解析 ================= */
    onData(u8) {
      if (!u8 || !u8.length) return;
      const type = u8[0];
      if (type === F.CHUNK) {
        const tid = rd32(u8, 1), i = rd32(u8, 5);
        this._onChunk(tid, i, u8.subarray(9));
      } else if (type === F.START) {
        let m; try { m = JSON.parse(TD.decode(u8.subarray(1))); } catch (e) { return; }
        this._onStart(m);
      } else if (type === F.DONE) {
        let m; try { m = JSON.parse(TD.decode(u8.subarray(1))); } catch (e) { return; }
        this._onDone(m);
      } else if (type === F.BIG) {
        const msgId = rd32(u8, 1), part = rd32(u8, 5), total = rd32(u8, 9);
        this._onBig(msgId, part, total, u8.subarray(13));
      }
    }

    async sendBig(obj) {
      const bytes = TE.encode(JSON.stringify(obj));
      const total = Math.max(1, Math.ceil(bytes.length / BIG_PART));
      const msgId = this.bigId++ & 0x7fffffff;
      for (let p = 0; p < total; p++) {
        const slice = bytes.subarray(p * BIG_PART, Math.min(bytes.length, (p + 1) * BIG_PART));
        const f = new Uint8Array(13 + slice.length);
        f[0] = F.BIG; wr32(f, 1, msgId); wr32(f, 5, p); wr32(f, 9, total); f.set(slice, 13);
        await this._waitDrain();
        if (this.stopped) return false;
        if (!this.link.sendData(f)) { this._emitLog('数据通道不可用，发送中断', 'err'); return false; }
      }
      return true;
    }

    _onBig(msgId, part, total, bytes) {
      let e = this.bigIn.get(msgId);
      if (!e) { e = { total: total, parts: new Array(total), n: 0 }; this.bigIn.set(msgId, e); }
      if (e.parts[part] === undefined) { e.parts[part] = new Uint8Array(bytes); e.n++; }
      if (e.n === e.total) {
        this.bigIn.delete(msgId);
        let len = 0; e.parts.forEach((p) => (len += p.length));
        const all = new Uint8Array(len); let o = 0;
        e.parts.forEach((p) => { all.set(p, o); o += p.length; });
        let m; try { m = JSON.parse(TD.decode(all)); } catch (err) { return; }
        this.onControlMessage(m);
      }
    }

    /* ================= 控制消息 ================= */
    onControlMessage(m) {
      if (!m || !m.t) return;
      switch (m.t) {
        case 'offer-manifest': return this._onManifest(m);
        case 'accept': return this._onAccept(m);
        case 'reject': return this._onReject(m);
        case 'file-verify': return this._onVerify(m);
        case 'retry': return this._onRetry(m);
        case 'progress': return this._onProgress(m);
        case 'all-done': return this._onAllDone(m);
        case 'abort': return this._onAbortMsg(m);
        default: if (this.onControl) this.onControl(m);
      }
    }

    /* ================= 发送端 ================= */
    async send(entries, opts) {
      opts = opts || {};
      const tid = ++this.tidSeq;
      const files = entries.map((e, i) => ({ i: i, path: e.path, size: e.size, mtime: e.mtime || Date.now(), file: e.file, handle: e.handle }));
      const job = { tid: tid, files: files, mode: opts.mode || 'send', note: opts.note || '', verify: new Map(), accepted: null, resolveAccept: null, _r: null };
      job.done = new Promise((r) => (job._r = r));
      job.acceptP = new Promise((r) => (job.resolveAccept = r));
      this.jobs.set(tid, job);

      files.forEach((f) => {
        const t = this._task({ id: tid + ':' + f.i, dir: 'send', path: f.path, size: f.size, state: 'wait', msg: '等待对方接受' });
        this._emit(t);
      });
      this._emitLog('提出发送：' + files.length + ' 个文件，共 ' + SD.fmtBytes(files.reduce((a, b) => a + b.size, 0)));

      this.sendControl({ t: 'offer-manifest', tid: tid, mode: job.mode, note: job.note, files: files.map((f) => ({ i: f.i, path: f.path, size: f.size, mtime: f.mtime })) });

      const to = setTimeout(() => { if (!job.accepted) job.resolveAccept({ timeout: true }); }, 180000);
      const acc = await job.acceptP;
      clearTimeout(to);
      if (acc.timeout) { this._emitLog('等待对方接受超时', 'warn'); files.forEach((f) => { const t = this.tasks.get(tid + ':' + f.i); t.state = 'error'; t.msg = '对方未响应'; this._emit(t); }); return { ok: false }; }
      if (acc.reject) { this._emitLog('对方拒绝了传输'); return { ok: false }; }
      job.accepted = acc;

      this.queue.push(job);
      this._pump();
      await job.done;
      return { ok: true, tid: tid };
    }

    async _pump() {
      if (this.busy) return;
      const job = this.queue.shift();
      if (!job) return;
      this.busy = true;
      try { await this._runJob(job); }
      catch (e) { this._emitLog('发送中断：' + e.message, 'err'); }
      finally { this.busy = false; if (job._r) job._r(); setTimeout(() => this._pump(), 0); }
    }

    async _runJob(job) {
      const tid = job.tid;
      const want = job.accepted.want || [];
      const resume = job.accepted.resume || {};
      if (!want.length) { this.sendControl({ t: 'all-done', tid: tid }); return; }
      for (const i of want) {
        if (this.stopped) throw new Error('连接已断开');
        const f = job.files[i];
        if (!f) continue;
        let off = Math.max(0, Math.min(resume[i] || 0, f.size));
        let attempt = 0;
        for (;;) {
          const r = await this._sendOne(job, f, off);
          if (r.ok) break;
          attempt++;
          if (attempt > 1 || r.abort) { const t = this.tasks.get(tid + ':' + f.i); t.state = 'error'; t.msg = r.msg || '校验失败'; this._emit(t); break; }
          this._emitLog('文件校验不一致，重传：' + f.path, 'warn');
          off = 0;
        }
      }
      this.sendControl({ t: 'all-done', tid: tid });
    }

    async _sendOne(job, f, off) {
      const tid = job.tid, i = f.i;
      const t = this.tasks.get(tid + ':' + i);
      t.state = 'run'; t.got = off; t.started = performance.now(); t.msg = off > 0 ? '续传中' : '传输中';
      this._emit(t);
      const sp = new Speed();

      const file = f.file || (f.handle ? await f.handle.getFile() : null);
      if (!file) { return { ok: false, msg: '源文件不可读' }; }

      const fromBlock = Math.floor(off / BLOCK);
      const start = fromBlock * BLOCK;   // 对齐到块边界
      this.link.sendData((() => { const j = TE.encode(JSON.stringify({ tid: tid, i: i, path: f.path, size: f.size, mtime: f.mtime, from: start })); const u = new Uint8Array(1 + j.length); u[0] = F.START; u.set(j, 1); return u; })());

      const chunkSize = Math.max(16384, Math.min(SD.loadSettings().chunkSize, this.link.maxMessage() - 64));
      const digests = [];
      let sent = start;
      const verifyP = new Promise((r) => job.verify.set(i, r));

      // 关键：读取+哈希下一块 与 发送当前块 **重叠** 做。
      // 之前是"读一块→哈希→发完→再读下一块"，每 4MB 都会空一次管道，高速链路上等于周期性掉速。
      const readBlock = async (p) => {
        const end = Math.min(f.size, p + BLOCK);
        const buf = new Uint8Array(await file.slice(p, end).arrayBuffer());
        return { buf: buf, digest: await SD.sha256(buf) };
      };
      let pending = start < f.size ? readBlock(start) : null;

      for (let p = start; p < f.size; p += BLOCK) {
        const cur = await pending;
        const nextP = p + BLOCK;
        pending = nextP < f.size ? readBlock(nextP) : null;   // 先把下一块读起来
        const buf = cur.buf;
        digests.push(cur.digest);
        for (let q = 0; q < buf.length; q += chunkSize) {
          if (this.stopped) return { ok: false, abort: true, msg: '连接已断开' };
          for (;;) {
            if (this.link.canSendMore()) break;
            await this._waitDrain();
            if (this.stopped) return { ok: false, abort: true, msg: '连接已断开' };
          }
          const view = buf.subarray(q, Math.min(buf.length, q + chunkSize));
          const fr = new Uint8Array(9 + view.length);
          fr[0] = F.CHUNK; wr32(fr, 1, tid); wr32(fr, 5, i); fr.set(view, 9);
          let sentOk = false;
          for (let attempt = 0; attempt < 6 && !sentOk; attempt++) {
            sentOk = this.link.sendData(fr);
            if (!sentOk) {
              if (this.link.dat && this.link.dat.readyState !== 'open') break;
              await this._waitDrain(true);          // 缓冲满了，等它排空再试
              await SD.sleep(15 * attempt);
            }
          }
          if (!sentOk) return { ok: false, abort: true, msg: '数据通道不可用（对方可能已断开）' };
          sent += view.length;
        }
        const spd = sp.push(sent - start);
        t.got = sent; t.speed = spd;
        t.eta = spd > 0 ? (f.size - sent) / spd : 0;
        this._emit(t);
      }

      this.link.sendData((() => { const j = TE.encode(JSON.stringify({ tid: tid, i: i, fromBlock: fromBlock, digests: digests })); const u = new Uint8Array(1 + j.length); u[0] = F.DONE; u.set(j, 1); return u; })());

      const v = await Promise.race([verifyP, new Promise((r) => setTimeout(() => r({ timeout: true }), 90000))]);
      if (v && v.ok) {
        t.state = 'done'; t.got = f.size; t.msg = '已送达'; t.speed = 0; t.eta = 0; t.hash = v.hash || '';
        this._emit(t);
        return { ok: true };
      }
      if (v && v.timeout) { t.msg = '等待对方校验结果超时'; return { ok: false, msg: '校验超时' }; }
      return { ok: false, msg: '对方校验不通过' };
    }

    // 背压：缓冲快满时短暂等一下再喂，但绝不能让发送管道空转
    // （等太久会拖死 SCTP 拥塞窗口的增长，实测首次传输会掉到 2MB/s）
    async _waitDrain(force) {
      const dc = this.link.dat;
      const cfg = SD.loadSettings();
      const cap = Math.min(cfg.bufferHigh || 0, cfg.bufferCap || (14 * 1024 * 1024));
      const low = Math.max(512 * 1024, Math.min(cfg.bufferLow || (cap * 0.75), cap * 0.85));
      if (!dc || dc.readyState !== 'open') return;
      if (!force && dc.bufferedAmount < cap) return;
      const t0 = (typeof performance !== 'undefined' ? performance.now() : Date.now());
      await new Promise((res) => {
        let t = 0;
        const on = () => { if (dc.bufferedAmount <= low || dc.readyState !== 'open') done(); };
        const done = () => { clearTimeout(t); dc.removeEventListener('bufferedamountlow', on); res(); };
        dc.addEventListener('bufferedamountlow', on);
        t = setTimeout(done, force ? 40 : 15);
      });
      // 诊断用：发送管道被"憋住"的时长与次数（丢包/带宽不足时这个数会飙高）
      const dt = (typeof performance !== 'undefined' ? performance.now() : Date.now()) - t0;
      this.diagWaits = (this.diagWaits || 0) + 1;
      this.diagDrainMs = (this.diagDrainMs || 0) + dt;
      if (dt > 150) this.diagStalls = (this.diagStalls || 0) + 1;
    }

    /** 诊断：给面板用的引擎侧统计（等待占比 = 发送管道被憋住的时间占比）。 */
    diagStats() {
      return {
        waits: this.diagWaits || 0,
        drainMs: Math.round(this.diagDrainMs || 0),
        stalls: this.diagStalls || 0
      };
    }

    _onAccept(m) {
      const job = this.jobs.get(m.tid);
      if (!job || job.accepted) return;
      job.resolveAccept({ want: m.want || [], resume: m.resume || {} });
      const n = (m.want || []).length;
      if (!n) this._emitLog('对方没有选择任何文件');
      else this._emitLog('对方已接受 ' + n + ' 个文件，开始传输');
    }
    _onReject(m) { const job = this.jobs.get(m.tid); if (job && !job.accepted) job.resolveAccept({ reject: true }); }
    _onVerify(m) {
      const job = this.jobs.get(m.tid); if (!job) return;
      const r = job.verify.get(m.i);
      if (r) { job.verify.delete(m.i); r(m); }
    }
    _onProgress(m) {
      const t = this.tasks.get(m.tid + ':' + m.i);
      if (t) { t.recvGot = m.got; this._emit(t); }
    }
    _onAllDone(m) { this._emitLog('一批传输结束'); }

    async _onRetry(m) {
      const job = this.jobs.get(m.tid);
      if (!job || !job.accepted) return;
      const f = job.files[m.i];
      if (!f) return;
      this._emitLog('对方要求重传：' + f.path, 'warn');
      this.busy = true;
      try { await this._sendOne(job, f, 0); } catch (e) {}
      this.busy = false;
    }

    _onAbortMsg(m) {
      this._emitLog('对方中止了传输：' + (m.reason || ''), 'warn');
      const job = this.jobs.get(m.tid);
      if (job) { job.files.forEach((f) => { const t = this.tasks.get(job.tid + ':' + f.i); if (t && t.state !== 'done') { t.state = 'error'; t.msg = '对方中止'; this._emit(t); } }); job._r && job._r(); }
      if (this.recv && this.recv.tid === m.tid) { this._finishRecv(false, '对方中止'); }
    }

    abortAll() {
      const reason = '本机中止';
      this.sendControl({ t: 'abort', reason: reason });
      this.jobs.forEach((job) => { job.files.forEach((f) => { const t = this.tasks.get(job.tid + ':' + f.i); if (t && t.state !== 'done') { t.state = 'error'; t.msg = reason; this._emit(t); } }); job._r && job._r(); });
      this.jobs.clear(); this.queue = []; this.busy = false;
    }

    /* ================= 接收端 ================= */
    async _onManifest(m) {
      const files = m.files || [];
      this._emitLog('对方想发送 ' + files.length + ' 个文件，共 ' + SD.fmtBytes(files.reduce((a, b) => a + b.size, 0)));
      let target = null;
      this._recvMode = m.mode || 'send';
      try { target = await this.getTarget(this._recvMode); } catch (e) { this._emitLog('无法确定保存位置：' + e.message, 'err'); }
      if (!target) { this.sendControl({ t: 'reject', tid: m.tid }); this._emitLog('已拒绝（未选择保存目录）', 'warn'); return; }
      this._manifestTarget = target;

      const want = [], resume = {}, skipped = [];
      for (const f of files) {
        const t = this._task({ id: m.tid + ':' + f.i, dir: 'recv', path: f.path, size: f.size, state: 'wait', msg: '排队中' });
        this._emit(t);
        let off = 0;
        if (target.mode === 'fsa') {
          try {
            const st = await SD.idbGet('resume', 'resume:' + target.key + ':' + f.path);
            if (st && st.size === f.size && st.written > 0) off = st.written - (st.written % BLOCK);
          } catch (e) {}
        }
        if (off >= f.size && f.size > 0) {
          t.state = 'done'; t.got = f.size; t.msg = '上次已传完，跳过'; this._emit(t);
          skipped.push(f.i);
          continue;
        }
        if (off > 0) resume[f.i] = off;
        want.push(f.i);
      }
      this.sendControl({ t: 'accept', tid: m.tid, want: want, resume: resume, skip: skipped });
      if (Object.keys(resume).length) this._emitLog('发现 ' + Object.keys(resume).length + ' 个文件可续传，将从断点继续');
    }

    _onStart(m) {
      const t = this.tasks.get(m.tid + ':' + m.i);
      if (t) { t.state = 'run'; t.got = m.from || 0; t.started = performance.now(); t.msg = m.from > 0 ? '续传中' : '接收中'; this._emit(t); }
      const rec = this.recv = {
        tid: m.tid, i: m.i, path: m.path, size: m.size, mtime: m.mtime, from: m.from || 0,
        fromBlock: Math.floor((m.from || 0) / BLOCK), digests: [], hbufs: [], pend: new Uint8Array(WRITE_BUF), pendFill: 0,
        got: m.from || 0, chunks: [], writer: null, target: null, chain: Promise.resolve(), sp: new Speed(), lastEmit: 0, seeked: false
      };
      this._emitLog('开始接收：' + m.path + (m.from > 0 ? '（续传）' : ''));
    }

    _onChunk(tid, i, payload) {
      const rec = this.recv;
      if (!rec || rec.tid !== tid || rec.i !== i) { return; }
      const copy = new Uint8Array(payload);   // 脱离底层缓冲
      rec.chain = rec.chain.then(() => this._pushChunk(rec, copy)).catch((e) => this._emitLog('写入失败：' + e.message, 'err'));
    }

    async _prepareRecv(rec) {
      if (rec.target) return rec.target;
      const tgt = this._manifestTarget || await this.getTarget(this._recvMode || 'send');
      rec.target = tgt;
      if (tgt.mode === 'fsa') {
        const fh = await ensureFile(tgt.dirHandle, rec.path);
        rec.fh = fh;
        rec.writer = await fh.createWritable({ keepExistingData: true });
        const st = await SD.idbGet('resume', 'resume:' + tgt.key + ':' + rec.path);
        if (st && st.size === rec.size && st.written === rec.got) { rec.digests = (st.digests || []).slice(); }
      }
      return tgt;
    }

    // 分片先攒进 1MB 写缓冲，再一次性顺序写盘（顺序写比按偏移写快得多）
    async _pushChunk(rec, chunk) {
      await this._prepareRecv(rec);
      if (!rec.target) return;
      if (rec.target.mode === 'memory') {
        rec.chunks.push(chunk);
        rec.got += chunk.length;
        this._tick(rec);
        return;
      }
      let o = 0;
      while (o < chunk.length) {
        const n = Math.min(WRITE_BUF - rec.pendFill, chunk.length - o);
        rec.pend.set(chunk.subarray(o, o + n), rec.pendFill);
        rec.pendFill += n; o += n;
        if (rec.pendFill === WRITE_BUF) {
          const full = rec.pend;
          rec.pend = new Uint8Array(WRITE_BUF); rec.pendFill = 0;
          await this._writeOut(rec, full);
        }
      }
      this._tick(rec);
    }

    async _writeOut(rec, buf) {
      try {
        if (!rec.seeked) { rec.seeked = true; if (rec.got > 0 && rec.writer.seek) await rec.writer.seek(rec.got); }
        await rec.writer.write(buf);
      } catch (e) {
        this._emitLog('写盘失败：' + e.message, 'err');
        throw e;
      }
      rec.got += buf.length;
      rec.hbufs.push(buf);
      if (rec.hbufs.length >= BLOCK / WRITE_BUF) {
        const total = new Uint8Array(BLOCK);
        let o = 0;
        for (const b of rec.hbufs) { total.set(b, o); o += b.length; }
        rec.hbufs = [];
        rec.digests.push(await SD.sha256(total));
        await this._persist(rec);
      }
    }

    // 收尾：把不足 1MB 的残留写出去，并算出最后一个不满 4MB 的块摘要
    async _flushRecv(rec) {
      if (!rec.target || rec.target.mode !== 'fsa') return;
      if (rec.pendFill > 0) {
        const part = rec.pend.subarray(0, rec.pendFill);
        rec.pend = new Uint8Array(WRITE_BUF); rec.pendFill = 0;
        await this._writeOut(rec, new Uint8Array(part));
      }
      if (rec.hbufs.length) {
        let len = 0; rec.hbufs.forEach((b) => (len += b.length));
        const total = new Uint8Array(len);
        let o = 0;
        for (const b of rec.hbufs) { total.set(b, o); o += b.length; }
        rec.hbufs = [];
        rec.digests.push(await SD.sha256(total));
      }
    }

    async _persist(rec) {
      if (!rec.target || rec.target.mode !== 'fsa') return;
      await SD.idbSet('resume', 'resume:' + rec.target.key + ':' + rec.path, {
        size: rec.size, written: rec.digests.length * BLOCK, digests: rec.digests, mtime: rec.mtime, updated: Date.now()
      });
    }

    _tick(rec) {
      const t = this.tasks.get(rec.tid + ':' + rec.i);
      if (!t) return;
      const spd = rec.sp.push(rec.got - rec.from);
      const now = performance.now();
      if (now - rec.lastEmit > 150 || rec.got >= rec.size) {
        rec.lastEmit = now;
        t.got = rec.got; t.speed = spd; t.eta = spd > 0 ? (rec.size - rec.got) / spd : 0;
        this._emit(t);
        this.sendControl({ t: 'progress', tid: rec.tid, i: rec.i, got: rec.got });
      }
    }

    _onDone(m) {
      const rec = this.recv;
      if (!rec || rec.tid !== m.tid || rec.i !== m.i) return;
      rec.chain = rec.chain.then(() => this._finishRecv(true, '', m)).catch((e) => this._emitLog('收尾失败：' + e.message, 'err'));
    }

    async _finishRecv(chainOk, errMsg, m) {
      const rec = this.recv;
      if (!rec) return;
      this.recv = null;
      const t = this.tasks.get(rec.tid + ':' + rec.i);
      try {
        await this._prepareRecv(rec);
        if (rec.target && rec.target.mode === 'fsa') {
          await this._flushRecv(rec);
          try { await rec.writer.close(); } catch (e) {}
        }
        if (rec.target && rec.target.mode === 'memory') {
          if (rec.chunks.length) {
            rec.digests = rec.digests.concat(await this._hashMemory(rec.chunks));
          }
          const blob = new Blob(rec.chunks, { type: 'application/octet-stream' });
          rec.chunks = [];
          const url = URL.createObjectURL(blob);
          if (t) { t.url = url; t.msg = '接收完成，点击保存'; }
        }
        const ok = chainOk && m && this._compareDigests(rec, m);
        const full = rec.digests.length ? await SD.hashList(rec.digests) : '';
        if (ok) {
          if (rec.target && rec.target.mode === 'fsa') await SD.idbDel('resume', 'resume:' + rec.target.key + ':' + rec.path);
          this._manifestTarget = null;
          if (t) { t.state = 'done'; t.got = rec.size; t.speed = 0; t.eta = 0; t.hash = full; t.msg = rec.target && rec.target.mode === 'memory' ? '接收完成，点击保存' : '已完成并校验通过'; this._emit(t); }
          this.sendControl({ t: 'file-verify', tid: rec.tid, i: rec.i, ok: true, hash: full });
          this._emitLog('✔ ' + rec.path + ' 校验通过');
        } else {
          if (t) { t.state = 'error'; t.msg = errMsg || '校验不通过，已请求重传'; this._emit(t); }
          this._emitLog('✘ ' + rec.path + ' 校验不通过，已删除并请求重传', 'warn');
          if (rec.target && rec.target.mode === 'fsa') {
            const rel = String(rec.path).split('/').filter((x) => x && x !== '.' && x !== '..');
            try { await removeAt(rec.target.dirHandle, rel); } catch (e) {}
            await SD.idbDel('resume', 'resume:' + rec.target.key + ':' + rec.path);
          }
          this._manifestTarget = null;
          this.sendControl({ t: 'file-verify', tid: rec.tid, i: rec.i, ok: false, reason: 'digest-mismatch' });
        }
      } catch (e) {
        this._emitLog('收尾异常：' + e.message, 'err');
        if (t) { t.state = 'error'; t.msg = e.message; this._emit(t); }
      }
    }

    async _hashMemory(chunks) {
      // 内存模式下按块补算摘要（顺序拼接）
      const out = [];
      let buf = new Uint8Array(BLOCK), fill = 0;
      for (const c of chunks) {
        let o = 0;
        while (o < c.length) {
          const n = Math.min(BLOCK - fill, c.length - o);
          buf.set(c.subarray(o, o + n), fill); fill += n; o += n;
          if (fill === BLOCK) { out.push(await SD.sha256(buf)); buf = new Uint8Array(BLOCK); fill = 0; }
        }
      }
      if (fill > 0) out.push(await SD.sha256(buf.subarray(0, fill)));
      return out;
    }

    _compareDigests(rec, m) {
      const theirs = m.digests || [];
      const mine = rec.digests.slice(m.fromBlock || 0);
      if (mine.length !== theirs.length) { SD.log('块数不一致：本地 ' + mine.length + ' / 对方 ' + theirs.length, 'warn'); return false; }
      for (let i = 0; i < mine.length; i++) if (mine[i] !== theirs[i]) { SD.log('第 ' + (i + m.fromBlock) + ' 块摘要不一致', 'warn'); return false; }
      return true;
    }

    stop() { this.stopped = true; }
  }

  /* ---------- 目录工具 ---------- */
  async function ensureDir(rootHandle, parts) {
    let h = rootHandle;
    for (const p of parts) h = await h.getDirectoryHandle(p, { create: true });
    return h;
  }
  async function ensureFile(rootHandle, relPath) {
    const parts = String(relPath).split('/').filter((x) => x && x !== '.' && x !== '..');
    const name = parts.pop() || 'unnamed';
    const dir = await ensureDir(rootHandle, parts.length ? parts : []);
    return dir.getFileHandle(name, { create: true });
  }
  async function removeAt(rootHandle, relParts) {
    const parts = relParts.slice();
    const name = parts.pop();
    let h = rootHandle;
    for (const p of parts) h = await h.getDirectoryHandle(p);
    await h.removeEntry(name, { recursive: true });
  }

  SD.Transfer = Transfer;
  SD.T = { F: F, BLOCK: BLOCK, ensureDir: ensureDir, ensureFile: ensureFile };
})(window);
