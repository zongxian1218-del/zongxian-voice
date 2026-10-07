/* 棕仙的传输软件 —— 中继链路（RelayLink）
 * 当 P2P 打洞失败时，双方改走一台公网 WebSocket 中继，由中继原样转发字节。
 * 接口与 SD.PeerLink 对齐：sendControl / sendData / bufferedAmount / canSendMore /
 * maxMessage / close，以及构造函数 handlers 的 onReady / onState / onControl / onData，
 * 因此 SD.Transfer / SD.Sync 无需任何改动即可原样跑在这条链路上。
 *
 * 安全：中继转发的是应用层字节，这里**先做一次 AES-GCM 加密封装**，绝不送明文：
 *   - 密钥由 roomId（取件码经 SHA-256 派生的 20 位 hex）再 SHA-256 派生，双方一致；
 *   - 每个帧随机 12 字节 IV，IV 随帧头发送，密钥 + IV 保证每次会话/每帧独立；
 *   - 控制帧（文本 JSON）与数据帧（二进制）**都**加密，封装成统一二进制帧：
 *       [1 字节 type][12 字节 IV][AES-GCM 密文+16 字节 tag]，type=1 控制 / 2 数据；
 *   - type 字节同时作为 AAD，防止帧类型被篡改。
 */
(function (global) {
  'use strict';
  const SD = global.SD;
  const TE = new TextEncoder();
  const TD = new TextDecoder();

  const TYPE_CTL = 1;
  const TYPE_DAT = 2;
  const HEARTBEAT_MS = 25000;     // 应用层心跳（走加密控制帧，不用原生 WS ping）
  const HELLO_RETRY_MS = 3000;    // 未收到对方 hello 前，每 3 秒补发一次
  const HARD_QUEUE_CAP = 32 * 1024 * 1024;   // 数据队列硬上限，防止无界堆积

  class RelayLink {
    constructor(opts) {
      this.url = (opts.url || '').replace(/\/+$/, '');
      this.roomId = opts.roomId || '';
      this.name = opts.name || '';
      this.caps = opts.caps || {};
      this.h = opts.handlers || {};
      this.ws = null;
      this.connected = false;
      this.remoteInfo = null;
      this._closed = false;
      this._byeSeen = false;
      this._rtt = 0;
      this._key = null;
      this._keyP = null;
      this._ctlQueue = [];
      this._datQueue = [];
      this._datBytes = 0;
      this._ctlPump = false;
      this._datPump = false;
      this._rxChain = Promise.resolve();
      this._pingTimer = 0;
      this._helloTimer = 0;
      this.isRelay = true;

      // 数据通道门面：SD.Transfer 的 _waitDrain / _sendOne 会访问
      // link.dat.readyState / link.dat.bufferedAmount / addEventListener('bufferedamountlow')。
      const self = this;
      const lowListeners = new Set();
      this.dat = {
        readyState: 'connecting',
        bufferedAmount: 0,
        addEventListener(type, fn) { if (type === 'bufferedamountlow') lowListeners.add(fn); },
        removeEventListener(type, fn) { if (type === 'bufferedamountlow') lowListeners.delete(fn); }
      };
      this._lowListeners = lowListeners;
    }

    /* ---------- 生命周期 ---------- */
    start() {
      if (typeof crypto === 'undefined' || !crypto || !crypto.subtle) {
        SD.log('中继需要 Web Crypto（请用 https 或 http://localhost 打开），已拒绝明文中继', 'err');
        if (this.h.onState) this.h.onState('error');
        return this;
      }
      let ws;
      try {
        ws = new WebSocket(this.url + '/relay?room=' + encodeURIComponent(this.roomId));
      } catch (e) {
        SD.log('中继连接创建失败：' + (e.message || e), 'err');
        if (this.h.onState) this.h.onState('error');
        return this;
      }
      this.ws = ws;
      ws.binaryType = 'arraybuffer';

      ws.onopen = () => {
        this.connected = true;
        this.dat.readyState = 'open';
        this._startHeartbeat();
        this._startHello();
        if (this.h.onState) this.h.onState('open');
        if (this.h.onReady) this.h.onReady();
      };

      ws.onmessage = (ev) => {
        // 收端串行化解密：WS 事件按序到达，但解密是异步的，必须排队保证顺序
        this._rxChain = this._rxChain
          .then(() => this._handleMessage(ev))
          .catch((e) => SD.log('中继消息处理失败：' + (e && e.message || e), 'warn'));
      };

      ws.onerror = () => { /* onclose 会随后触发 */ };
      ws.onclose = () => {
        this.connected = false;
        this.dat.readyState = 'closed';
        this._stopTimers();
        if (!this._byeSeen && !this._closed && this.h.onState) this.h.onState('closed');
      };
      return this;
    }

    _stopTimers() {
      if (this._pingTimer) { clearInterval(this._pingTimer); this._pingTimer = 0; }
      if (this._helloTimer) { clearInterval(this._helloTimer); this._helloTimer = 0; }
    }

    _startHeartbeat() {
      this._pingTimer = setInterval(() => { this.sendControl({ t: 'ping', ts: Date.now() }); }, HEARTBEAT_MS);
    }

    _startHello() {
      this.sendControl({ t: 'hello', name: this.name, caps: this.caps, v: 1 });
      this._helloTimer = setInterval(() => {
        if (this.remoteInfo || this._closed) { clearInterval(this._helloTimer); this._helloTimer = 0; return; }
        if (this.ws && this.ws.readyState === 1) this.sendControl({ t: 'hello', name: this.name, caps: this.caps, v: 1 });
      }, HELLO_RETRY_MS);
    }

    /* ---------- 加解密 ---------- */
    async _deriveKey() {
      if (this._key) return this._key;
      if (this._keyP) return this._keyP;
      this._keyP = (async () => {
        const material = TE.encode('swiftdrop/relay-aes-gcm|' + this.roomId);
        const raw = await crypto.subtle.digest('SHA-256', material);
        this._key = await crypto.subtle.importKey('raw', raw, { name: 'AES-GCM' }, false, ['encrypt', 'decrypt']);
        return this._key;
      })();
      return this._keyP;
    }

    async _encryptFrame(type, u8) {
      const key = await this._deriveKey();
      const iv = crypto.getRandomValues(new Uint8Array(12));
      const aad = new Uint8Array([type]);
      const ct = new Uint8Array(await crypto.subtle.encrypt(
        { name: 'AES-GCM', iv: iv, additionalData: aad, tagLength: 128 }, key, u8));
      const out = new Uint8Array(1 + 12 + ct.length);
      out[0] = type;
      out.set(iv, 1);
      out.set(ct, 13);
      return out;
    }

    async _decryptFrame(buf) {
      const key = await this._deriveKey();
      const type = buf[0];
      const iv = buf.subarray(1, 13);
      const ct = buf.subarray(13);
      const pt = new Uint8Array(await crypto.subtle.decrypt(
        { name: 'AES-GCM', iv: iv, additionalData: new Uint8Array([type]), tagLength: 128 }, key, ct));
      return { type: type, pt: pt };
    }

    /* ---------- 发送（接口与 PeerLink 一致，返回布尔） ---------- */
    sendControl(obj) {
      if (!this.ws || this.ws.readyState !== 1) return false;
      this._ctlQueue.push(obj);
      this._pumpCtl();
      return true;
    }

    sendData(u8) {
      if (!this.ws || this.ws.readyState !== 1) return false;
      if (this._datBytes + u8.length > HARD_QUEUE_CAP) return false;   // 硬上限，强制背压
      this._datQueue.push(u8);
      this._datBytes += u8.length;
      this._sentBytes = (this._sentBytes || 0) + u8.length;            // 诊断用
      this._updateBuffer();
      this._pumpData();
      return true;
    }

    /** 诊断：中继链路的真实状态（速度靠这两个字节计数算出来）。 */
    async diag() {
      return {
        rtt: this._rtt || 0, localType: 'relay', remoteType: 'relay', relay: true,
        bw: 0, bytesSent: this._sentBytes || 0, bytesReceived: this._recvBytes || 0,
        state: this.connected ? 'connected' : 'disconnected'
      };
    }

    bufferedAmount() { return this.dat ? this.dat.bufferedAmount : 0; }

    canSendMore() {
      if (!this.dat || this.dat.readyState !== 'open') return false;
      // 与 PeerLink 用同一套背压阈值（bufferHigh/bufferCap），否则 Transfer._waitDrain
      // 会因阈值不一致而空转。
      const cfg = SD.loadSettings();
      const cap = Math.min(cfg.bufferHigh || 0, cfg.bufferCap || (14 * 1024 * 1024));
      return this.dat.bufferedAmount < cap;
    }

    maxMessage() { return 1024 * 1024; }   // 1MB，供分片上限使用

    close(notify) {
      this._closed = true;
      this._stopTimers();
      this.connected = false;
      this.dat.readyState = 'closed';
      // 对端断开由中继负责通知（一端断开→另一端收到 {"t":"bye"}），这里只管关本地 WS。
      try { if (this.ws) this.ws.close(); } catch (e) {}
    }

    stats() {
      return { rtt: this._rtt, buffered: this.bufferedAmount(), state: this.connected ? 'connected' : 'disconnected', relay: true };
    }

    /* ---------- 队列泵（保持帧顺序 + 背压） ---------- */
    async _pumpCtl() {
      if (this._ctlPump) return;
      this._ctlPump = true;
      try {
        while (this._ctlQueue.length && !this._closed) {
          const obj = this._ctlQueue.shift();
          const frame = await this._encryptFrame(TYPE_CTL, TE.encode(JSON.stringify(obj)));
          if (this.ws && this.ws.readyState === 1) { try { this.ws.send(frame); } catch (e) {} }
        }
      } catch (e) {
        SD.log('中继控制发送失败：' + (e && e.message || e), 'warn');
      } finally {
        this._ctlPump = false;
      }
    }

    async _pumpData() {
      if (this._datPump) return;
      this._datPump = true;
      try {
        while (this._datQueue.length && !this._closed) {
          const u8 = this._datQueue.shift();
          const frame = await this._encryptFrame(TYPE_DAT, u8);
          if (this.ws && this.ws.readyState === 1) { try { this.ws.send(frame); } catch (e) {} }
          this._datBytes -= u8.length;
          this._updateBuffer();
        }
      } catch (e) {
        SD.log('中继数据发送失败：' + (e && e.message || e), 'warn');
      } finally {
        this._datPump = false;
        this._updateBuffer();
      }
    }

    _updateBuffer() {
      const wsBuf = (this.ws && this.ws.readyState === 1) ? this.ws.bufferedAmount : 0;
      const next = this._datBytes + wsBuf;
      const prev = this.dat.bufferedAmount;
      this.dat.bufferedAmount = next;
      if (next < prev || next === 0) this._fireLow();
    }

    _fireLow() {
      if (!this._lowListeners.size) return;
      for (const fn of Array.from(this._lowListeners)) { try { fn(); } catch (e) {} }
    }

    /* ---------- 接收 ---------- */
    async _handleMessage(ev) {
      if (typeof ev.data === 'string') {
        // 中继发来的明文通知（仅用于「对端断开」），不含业务数据
        let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
        if (m && m.t === 'bye') {
          this._byeSeen = true;
          if (this.h.onState) this.h.onState('bye');
          try { this.ws.close(); } catch (e) {}
        }
        return;
      }
      const buf = new Uint8Array(ev.data);
      if (buf.length < 13) return;              // 至少 1(type)+12(IV)，密文含 tag 更短则非法
      const { type, pt } = await this._decryptFrame(buf);
      if (type === TYPE_CTL) {
        let m; try { m = JSON.parse(TD.decode(pt)); } catch (e) { return; }
        this._onControlMsg(m);
      } else if (type === TYPE_DAT) {
        this._recvBytes = (this._recvBytes || 0) + pt.length;          // 诊断用
        if (this.h.onData) this.h.onData(pt);
      }
    }

    _onControlMsg(m) {
      if (!m || !m.t) return;
      if (m.t === 'ping') { this.sendControl({ t: 'pong', ts: m.ts }); return; }
      if (m.t === 'pong') { this._rtt = Math.max(0, Date.now() - (m.ts || 0)); if (this.h.onRtt) this.h.onRtt(this._rtt); return; }
      if (m.t === 'hello' && m.name) {
        this.remoteInfo = m;
        if (this.h.onPeerInfo) this.h.onPeerInfo(m);
        if (this._helloTimer) { clearInterval(this._helloTimer); this._helloTimer = 0; }
        return;
      }
      if (m.t === 'bye') {
        this._byeSeen = true;
        if (this.h.onState) this.h.onState('bye');
        try { this.ws.close(); } catch (e) {}
        return;
      }
      if (this.h.onControl) this.h.onControl(m);
    }
  }

  SD.RelayLink = RelayLink;
})(window);
