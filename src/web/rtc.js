/* 棕仙的传输软件 —— WebRTC 连接层
 * host = 发起方（生成取件码的人）：负责创建数据通道并主动发 offer
 * guest = 加入方：只应答，避免双方同时 offer 冲突
 * 只有 host 创建数据通道，guest 通过 ondatachannel 获取，语义最清晰。
 */
(function (global) {
  'use strict';
  const SD = global.SD;

  class PeerLink {
    constructor(opts) {
      this.signaling = opts.signaling;
      this.role = opts.role;               // 'host' | 'guest'
      this.iceServers = opts.iceServers || [];
      this.h = opts.handlers || {};
      this.pc = null;
      this.ctl = null;                      // 控制通道（JSON）
      this.dat = null;                      // 数据通道（二进制分片）
      this.pendingCands = [];
      this.localCands = [];
      this.candTimer = 0;
      this.remoteInfo = null;
      this.connected = false;
      this.remoteDescSet = false;
      this.offering = false;
      this.hello = { name: (SD.loadSettings().deviceName || ''), caps: SD.caps(), v: 1 };
      this._closed = false;
      this._rtt = 0;
    }

    /* ---------- 生命周期 ---------- */
    start() {
      this._mkPc();
      this.signaling.publish({ t: 'hello', name: this.hello.name, caps: this.hello.caps });
      // 加入方打招呼，直到**收到对方的 offer/answer** 为止（不是等到连上为止）。
      // 关键：不能一直打到"连上"为止，否则发起方会被反复触发重新协商。
      if (this.role === 'guest') {
        let tries = 0;
        this.helloTimer = setInterval(() => {
          if (this.connected || this.gotRemoteDesc || this._closed) {
            clearInterval(this.helloTimer); this.helloTimer = 0; return;
          }
          tries++;
          if (tries > 8) {                       // 约 20 秒还没人应答，就停下别再刷
            clearInterval(this.helloTimer); this.helloTimer = 0;
            SD.log('对方还没有响应，先停止打招呼；如对方稍后打开页面，可以重新连接', 'warn');
            return;
          }
          this.signaling.publish({ t: 'hello', name: this.hello.name, caps: this.hello.caps });
        }, 2500);
      }
      // 兜底重试：只在**确实失败**或**握手压根没完成**时重启协商。
      // 绝不在 ICE 正常进行中重启（那会打断打洞，跨网络会永远连不上）。
      this.retryTimer = setInterval(() => {
        if (this._closed || this.connected || this.role !== 'host' || !this.pc || this.offering) return;
        const now = Date.now();
        const cs = this.pc.connectionState;
        const ics = this.pc.iceConnectionState;
        const failed = cs === 'failed' || ics === 'failed' || ics === 'disconnected';
        const noAnswer = !this.answerAt && (now - this.startedAt) > 20000;   // 喊了 20 秒没人应答
        const iceStuck = !!this.answerAt && !this.connected && (now - this.answerAt) > 40000;
        if (failed || noAnswer || iceStuck) {
          SD.log('直连没有建立（' + cs + '/' + ics + '），重启一次协商…', 'warn');
          this._mkOffer(true);
        } else if (this.answerAt && !this.connected && (now - this.answerAt) > 8000 && !this._slowLogged) {
          this._slowLogged = true;
          SD.log('正在打洞连接…跨网络通常需要 5~30 秒，请稍等（不要关页面）');
        }
      }, 4000);
      return this;
    }

    _mkPc() {
      if (this.pc) { try { this.pc.close(); } catch (e) {} }
      const cfg = { iceServers: this.iceServers, bundlePolicy: 'max-bundle', rtcpMuxPolicy: 'require' };
      const pc = (this.pc = new RTCPeerConnection(cfg));
      this.remoteDescSet = false;
      this.gotRemoteDesc = false;
      this.pendingCands = [];
      this.startedAt = Date.now();   // 计时基准（绝不能拿 0 当基准，否则第一次就误判超时）
      this.offerAt = 0;
      this.answerAt = 0;
      this._slowLogged = false;
      this._candErrLogged = false;

      if (this.role === 'host') {
        this.ctl = pc.createDataChannel('c', { ordered: true });
        this.dat = pc.createDataChannel('d', { ordered: true });
        this._bindChannel(this.ctl, 'ctl');
        this._bindChannel(this.dat, 'dat');
      } else {
        pc.ondatachannel = (ev) => {
          if (ev.channel.label === 'c') { this.ctl = ev.channel; this._bindChannel(this.ctl, 'ctl'); }
          else { this.dat = ev.channel; this._bindChannel(this.dat, 'dat'); }
          this._checkReady();
        };
      }

      pc.onicecandidate = (ev) => {
        if (!ev.candidate) return;
        this.localCands.push(ev.candidate.toJSON ? ev.candidate.toJSON() : { candidate: ev.candidate.candidate, sdpMid: ev.candidate.sdpMid, sdpMLineIndex: ev.candidate.sdpMLineIndex });
        if (!this.candTimer) {
          this.candTimer = setTimeout(() => {
            this.candTimer = 0;
            if (this.localCands.length) { this.signaling.publish({ t: 'cand', cands: this.localCands }); this.localCands = []; }
          }, 120);
        }
      };

      pc.onconnectionstatechange = () => {
        const st = pc.connectionState;
        SD.log('连接状态：' + st);
        if (this.h.onState) this.h.onState(st);
        if (st === 'connected') { this.connected = true; if (this.helloTimer) { clearInterval(this.helloTimer); this.helloTimer = 0; } }
        if (st === 'failed') {
          this.connected = false;
          if (this.role === 'host') { try { pc.restartIce(); } catch (e) {} this._mkOffer(true); }
        }
        if (st === 'disconnected') { setTimeout(() => { if (pc.connectionState === 'disconnected' && this.role === 'host') { try { pc.restartIce(); } catch (e) {} this._mkOffer(true); } }, 2500); }
      };

      pc.oniceconnectionstatechange = () => {
        if (this.h.onIceState) this.h.onIceState(pc.iceConnectionState);
      };
    }

    _bindChannel(ch, kind) {
      ch.binaryType = 'arraybuffer';
      const cfg = SD.loadSettings();
      const cap = Math.min(cfg.bufferHigh || 0, cfg.bufferCap || (14 * 1024 * 1024));
      if (kind === 'dat') { try { ch.bufferedAmountLowThreshold = Math.floor(cap * 0.75); } catch (e) {} }
      ch.onopen = () => { SD.log('通道就绪：' + kind); this._checkReady(); };
      ch.onclose = () => { SD.log('通道关闭：' + kind, 'warn'); this.connected = false; if (kind === 'ctl') this._readyFired = false; if (this.h.onState) this.h.onState('channel-closed'); };
      ch.onerror = (e) => SD.log('通道错误：' + kind + ' ' + (e.message || ''), 'warn');
      ch.onmessage = (ev) => {
        if (kind === 'ctl') {
          let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
          this._onControl(m);
        } else {
          if (this.h.onData) this.h.onData(new Uint8Array(ev.data));
        }
      };
    }

    _checkReady() {
      if (this.ctl && this.ctl.readyState === 'open' && this.dat && this.dat.readyState === 'open') {
        if (!this._readyFired) {
          this._readyFired = true;
          this.connected = true;
          if (this.helloTimer) { clearInterval(this.helloTimer); this.helloTimer = 0; }
          this._startPing();
          if (this.h.onReady) this.h.onReady();
        }
      }
    }

    _onControl(m) {
      if (m.t === 'ping') { this.sendControl({ t: 'pong', ts: m.ts }); return; }
      if (m.t === 'pong') { this._rtt = Math.max(0, Date.now() - m.ts); if (this.h.onRtt) this.h.onRtt(this._rtt); return; }
      if (m.t === 'hello' && m.name) { this.remoteInfo = m; if (this.h.onPeerInfo) this.h.onPeerInfo(m); }
      if (this.h.onControl) this.h.onControl(m);
    }

    _startPing() {
      this.pingTimer = setInterval(() => { if (this.ctl && this.ctl.readyState === 'open') this.sendControl({ t: 'ping', ts: Date.now() }); }, 2500);
    }

    /* ---------- 信令处理 ---------- */
    async handleSignal(m) {
      const pc = this.pc;
      if (!pc) return;
      if (m.t === 'hello') {
        this.remoteInfo = m;
        if (this.h.onPeerInfo) this.h.onPeerInfo(m);
        if (this.role === 'host' && !this.connected && !this.answerAt) {
          // 只在前一次 offer 已经过去 5 秒还没等到应答时才重发（怕 offer 或招呼丢了），
          // 一旦收到 answer 就绝不再自动重发，把时间交给 ICE 去打洞。
          const since = this.offerAt ? (Date.now() - this.offerAt) : Infinity;
          if (since > 5000) await this._mkOffer(false);
        }
        return;
      }
      if (m.t === 'offer') {
        // 理论上只有 host 发 offer；收到就正常应答
        try {
          await pc.setRemoteDescription({ type: 'offer', sdp: m.sdp });
          this.remoteDescSet = true;
          this.gotRemoteDesc = true;
          if (this.helloTimer) { clearInterval(this.helloTimer); this.helloTimer = 0; }
          await this._flushCands();
          const ans = await pc.createAnswer();
          await pc.setLocalDescription(ans);
          this.signaling.publish({ t: 'answer', sdp: pc.localDescription.sdp });
        } catch (e) { SD.log('应答失败：' + e.message, 'err'); }
        return;
      }
      if (m.t === 'answer') {
        try {
          if (pc.signalingState === 'have-local-offer') {
            await pc.setRemoteDescription({ type: 'answer', sdp: m.sdp });
            this.remoteDescSet = true;
            this.gotRemoteDesc = true;
            this.answerAt = Date.now();
            SD.log('对方已应答，开始打洞直连…');
            await this._flushCands();
          }
        } catch (e) { SD.log('设置应答失败：' + e.message, 'warn'); }
        return;
      }
      if (m.t === 'cand' && m.cands) {
        for (const c of m.cands) {
          if (!this.remoteDescSet) { this.pendingCands.push(c); continue; }
          try { await pc.addIceCandidate(c); }
          catch (e) {
            // 之前这里把错误吞掉了：候选加不上会导致"永远在打洞"，必须留痕
            if (!this._candErrLogged) {
              this._candErrLogged = true;
              SD.log('有一批对方的连接候选添加失败（' + (e && e.message) + '），可能影响直连', 'warn');
            }
          }
        }
        return;
      }
      if (m.t === 'bye') {
        SD.log('对方主动断开');
        this.connected = false;
        if (this.h.onState) this.h.onState('bye');
        return;
      }
    }

    async _flushCands() {
      const list = this.pendingCands; this.pendingCands = [];
      for (const c of list) {
        try { await this.pc.addIceCandidate(c); } catch (e) { /* 单条失败不影响其它 */ }
      }
    }

    async _mkOffer(iceRestart) {
      const pc = this.pc;
      if (!pc || this.offering) return;
      this.offering = true;
      try {
        const offer = await pc.createOffer({ iceRestart: !!iceRestart });
        await pc.setLocalDescription(offer);
        this.offerAt = Date.now();
        this.signaling.publish({ t: 'offer', sdp: pc.localDescription.sdp });
        SD.log(iceRestart ? '重新发起连接协商…' : '已发出连接请求…');
      } catch (e) {
        SD.log('创建 offer 失败：' + e.message, 'err');
      } finally {
        this.offering = false;
      }
    }

    /* ---------- 发送 ---------- */
    sendControl(obj) {
      if (this.ctl && this.ctl.readyState === 'open') { try { this.ctl.send(JSON.stringify(obj)); return true; } catch (e) { SD.log('控制消息发送失败：' + e.message, 'warn'); } }
      return false;
    }

    sendData(u8) {
      if (this.dat && this.dat.readyState === 'open') { try { this.dat.send(u8); return true; } catch (e) { return false; } }
      return false;
    }

    bufferedAmount() { return this.dat ? this.dat.bufferedAmount : 0; }

    canSendMore() {
      if (!this.dat || this.dat.readyState !== 'open') return false;
      const cfg = SD.loadSettings();
      const cap = Math.min(cfg.bufferHigh || 0, cfg.bufferCap || (14 * 1024 * 1024));
      return this.dat.bufferedAmount < cap;
    }

    maxMessage() {
      try { return (this.pc && this.pc.sctp && this.pc.sctp.maxMessageSize) || 262144; } catch (e) { return 262144; }
    }

    /* ---------- 关闭 ---------- */
    close(notify) {
      this._closed = true;
      clearInterval(this.helloTimer); clearInterval(this.retryTimer); clearInterval(this.pingTimer);
      if (notify) this.signaling.publish({ t: 'bye' });
      try { this.ctl && this.ctl.close(); } catch (e) {}
      try { this.dat && this.dat.close(); } catch (e) {}
      try { this.pc && this.pc.close(); } catch (e) {}
      this.connected = false;
      this._readyFired = false;
    }

    stats() {
      return { rtt: this._rtt, buffered: this.bufferedAmount(), state: this.pc ? this.pc.connectionState : 'none' };
    }

    /** 连接诊断：选中候选对的真实类型/RTT/可用带宽 + 数据通道真实字节数。 */
    async diag() {
      const out = {
        rtt: this._rtt || 0, localType: '', remoteType: '', relay: false, bw: 0,
        bytesSent: 0, bytesReceived: 0, state: this.pc ? this.pc.connectionState : 'none'
      };
      if (!this.pc || !this.pc.getStats) return out;
      let stats;
      try { stats = await this.pc.getStats(); } catch (e) { return out; }
      const types = {};
      const pairs = [];
      let selId = '';
      stats.forEach((r) => {
        if (!r || !r.type) return;
        if (r.type === 'local-candidate') types[r.id] = r.candidateType || '';
        else if (r.type === 'remote-candidate') types['R' + r.id] = r.candidateType || '';
        else if (r.type === 'candidate-pair' && (r.selected || r.nominated || r.state === 'succeeded')) pairs.push(r);
        else if (r.type === 'transport' && r.selectedCandidatePairId) selId = r.selectedCandidatePairId;
        else if (r.type === 'data-channel') {
          out.bytesSent += r.bytesSent || 0;
          out.bytesReceived += r.bytesReceived || 0;
        }
      });
      const sel = (selId && pairs.find((p) => p.id === selId)) || pairs.find((p) => p.selected) || pairs[pairs.length - 1];
      if (sel) {
        out.localType = types[sel.localCandidateId] || '';
        out.remoteType = types['R' + sel.remoteCandidateId] || '';
        const r = sel.currentRoundTripTime || sel.totalRoundTripTime || 0;
        if (r) out.rtt = Math.round(r * 1000);
        out.bw = Math.round(sel.availableOutgoingBitrate || sel.availableIncomingBitrate || 0);
      }
      out.relay = out.localType === 'relay' || out.remoteType === 'relay';
      return out;
    }
  }

  SD.PeerLink = PeerLink;
})(window);
