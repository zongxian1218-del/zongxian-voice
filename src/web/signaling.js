/* 棕仙的传输软件 —— 信令层
 * 两条独立通道，互为备份：
 *   1) 公网 MQTT over WebSocket（无需注册、无需公网 IP）：broker.emqx.io 等公共服务器
 *   2) 局域网 WebSocket 中继（同源 /signal，完全离线可用，由桌面版提供）
 * 双方都会同时订阅两条通道，因此只要有一条通就能建连。
 * 信令里只交换 SDP/ICE（不含文件内容）；文件数据由 WebRTC 的 DTLS 端到端加密。
 */
(function (global) {
  'use strict';
  const SD = global.SD;
  const TE = new TextEncoder();
  const TD = new TextDecoder();

  SD.MQTT_BROKERS = [
    // 优先国内可达性好的节点；443 端口的那个最不容易被运营商/公司网拦掉
    'wss://broker-cn.emqx.io:8084/mqtt',
    'wss://broker.emqx.io:8084/mqtt',
    'wss://mqtt.eclipseprojects.io:443/mqtt',
    'wss://broker.hivemq.com:8884/mqtt',
    'wss://test.mosquitto.org:8081/mqtt',
    'wss://broker.emqx.io:8083/mqtt',
    'ws://mqtt.eclipseprojects.io:80/mqtt'
  ];

  const encStr = (s) => {
    const b = TE.encode(s);
    const out = new Uint8Array(2 + b.length);
    out[0] = b.length >> 8; out[1] = b.length & 255;
    out.set(b, 2);
    return out;
  };
  const encLen = (n) => {
    const out = [];
    do { let d = n % 128; n = Math.floor(n / 128); if (n > 0) d |= 128; out.push(d); } while (n > 0);
    return new Uint8Array(out);
  };
  function cat(parts) {
    let len = 0; parts.forEach((p) => (len += p.length));
    const out = new Uint8Array(len);
    let o = 0; parts.forEach((p) => { out.set(p, o); o += p.length; });
    return out;
  }
  const u16 = (n) => new Uint8Array([(n >> 8) & 255, n & 255]);
  const u32 = (n) => new Uint8Array([(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255]);

  /* ================= MQTT 3.1.1 最小客户端 ================= */
  class MqttClient {
    constructor(url, opts) {
      this.url = url;
      this.opts = opts || {};
      this.topics = new Set();
      this.rx = new Uint8Array(0);
      this.ws = null;
      this.connected = false;
      this.closedByUser = false;
      this.pid = 1;
      this.retry = 0;
    }

    connect() {
      return new Promise((resolve, reject) => {
        let ws;
        try { ws = new WebSocket(this.url, 'mqtt'); } catch (e) { return reject(e); }
        this.ws = ws;
        ws.binaryType = 'arraybuffer';
        let opened = false;
        const hint = (code) => {
          // 关闭码是判断"为什么连不上"的关键线索
          if (code === 1006) return '网络层被拦或域名解析失败（关闭码 1006）';
          if (code === 1002 || code === 1003) return '协议被服务器拒绝（关闭码 ' + code + '）';
          if (code === 1008 || code === 4403) return '服务器拒绝了这个来源/策略（关闭码 ' + code + '）';
          if (code) return '连接被关闭（关闭码 ' + code + '）';
          return '连接被关闭';
        };
        const to = setTimeout(() => {
          try { ws.close(); } catch (e) {}
          reject(new Error('连接超时 9 秒（可能被防火墙/运营商拦截）'));
        }, 9000);
        ws.onopen = () => {
          opened = true;
          const clientId = 'sd_' + SD.randomCode(14);
          const flags = 0x02; // clean session
          const vh = cat([encStr('MQTT'), new Uint8Array([4, flags]), u16(60)]);
          const payload = encStr(clientId);
          const body = cat([vh, payload]);
          ws.send(cat([new Uint8Array([0x10]), encLen(body.length), body]));
          this.keep = setInterval(() => this._send(new Uint8Array([0xc0, 0x00])), 25000);
        };
        ws.onmessage = (ev) => {
          const u8 = new Uint8Array(ev.data);
          this.rx = cat([this.rx, u8]);
          try { this._parse(); } catch (e) { SD.log('MQTT 解析异常：' + e.message, 'warn'); }
        };
        ws.onerror = () => {
          // onerror 通常不给细节，真正的线索在随后的 onclose 关闭码里
          if (!opened) { clearTimeout(to); reject(new Error('网络不可达（WebSocket 握手失败）')); }
        };
        ws.onclose = (ev) => {
          clearTimeout(to); clearInterval(this.keep);
          const was = this.connected;
          this.connected = false;
          if (this.opts.onStatus) this.opts.onStatus(was ? 'lost' : 'closed');
          if (was) reject(new Error(hint(ev && ev.code)));
          else if (!opened) reject(new Error(hint(ev && ev.code)));
          if (this.opts.onClose) this.opts.onClose();
        };
        this._resolve = () => { clearTimeout(to); this.connected = true; this.retry = 0; resolve(); };
        this._reject = (e) => { clearTimeout(to); reject(e); };
      });
    }

    _send(u8) { if (this.ws && this.ws.readyState === 1) { try { this.ws.send(u8); } catch (e) {} } }

    _parse() {
      let off = 0;
      for (;;) {
        if (this.rx.length - off < 2) break;
        const first = this.rx[off];
        let mul = 1, val = 0, i = off + 1, complete = false, bad = false;
        for (;;) {
          if (i >= this.rx.length) break;
          const b = this.rx[i++];
          val += (b & 127) * mul; mul *= 128;
          if (!(b & 128)) { complete = true; break; }
          if (i - off > 5) { bad = true; break; }
        }
        if (bad) { this.rx = new Uint8Array(0); throw new Error('报文长度非法'); }
        if (!complete) break;
        if (this.rx.length < i + val) break;
        const pkt = this.rx.subarray(i, i + val);
        this._handle(first, pkt);
        off = i + val;
      }
      if (off > 0) this.rx = this.rx.subarray(off).slice();
    }

    _handle(first, pkt) {
      const type = first >> 4;
      if (type === 2) { // CONNACK
        if (pkt[1] === 0) { this._resolve && this._resolve(); this.topics.forEach((t) => this.subscribe(t)); }
        else { this._reject && this._reject(new Error('MQTT 拒绝连接，错误码 ' + pkt[1])); }
      } else if (type === 3) { // PUBLISH
        const tlen = (pkt[0] << 8) | pkt[1];
        const topic = TD.decode(pkt.subarray(2, 2 + tlen));
        const qos = (first >> 1) & 3;
        let o = 2 + tlen;
        if (qos > 0) { const pid = (pkt[o] << 8) | pkt[o + 1]; o += 2; this._send(cat([new Uint8Array([0x40]), encLen(2), u16(pid)])); }
        const data = TD.decode(pkt.subarray(o));
        if (this.opts.onMessage) this.opts.onMessage(topic, data);
      }
    }

    subscribe(topic) {
      this.topics.add(topic);
      if (!this.connected) return;
      const pid = this.pid++ & 0xffff;
      const body = cat([u16(pid), encStr(topic), new Uint8Array([0])]);
      this._send(cat([new Uint8Array([0x82]), encLen(body.length), body]));
    }

    publish(topic, text) {
      if (!this.connected) return false;
      const body = cat([encStr(topic), TE.encode(text)]);
      this._send(cat([new Uint8Array([0x30]), encLen(body.length), body]));
      return true;
    }

    close() { this.closedByUser = true; clearInterval(this.keep); this._send(new Uint8Array([0xe0, 0x00])); try { this.ws && this.ws.close(); } catch (e) {} }
  }

  /* ================= 局域网 WebSocket 中继客户端 ================= */
  class LocalWsClient {
    constructor(url, opts) {
      this.url = url;
      this.opts = opts || {};
      this.topics = new Set();
      this.connected = false;
      this.closedByUser = false;
    }
    connect() {
      return new Promise((resolve, reject) => {
        let ws;
        try { ws = new WebSocket(this.url); } catch (e) { return reject(e); }
        this.ws = ws;
        ws.binaryType = 'arraybuffer';
        const to = setTimeout(() => { try { ws.close(); } catch (e) {} reject(new Error('本地信令超时')); }, 6000);
        ws.onopen = () => { clearTimeout(to); this.connected = true; this.topics.forEach((t) => this.subscribe(t)); resolve(); };
        ws.onmessage = (ev) => {
          let d;
          try { d = JSON.parse(typeof ev.data === 'string' ? ev.data : TD.decode(ev.data)); } catch (e) { return; }
          if (d.t === 'msg' && this.opts.onMessage) this.opts.onMessage(d.topic, JSON.stringify(d.d));
        };
        ws.onerror = () => { clearTimeout(to); reject(new Error('本地信令不可用')); };
        ws.onclose = () => {
          const was = this.connected; this.connected = false;
          if (this.opts.onStatus) this.opts.onStatus(was ? 'lost' : 'closed');
          if (!was) reject(new Error('本地信令未连上'));
        };
      });
    }
    subscribe(topic) { this.topics.add(topic); this._raw({ t: 'sub', topic: topic }); }
    publish(topic, text) {
      if (!this.connected) return false;
      let d; try { d = JSON.parse(text); } catch (e) { return false; }
      this._raw({ t: 'pub', topic: topic, d: d });
      return true;
    }
    _raw(o) { if (this.ws && this.ws.readyState === 1) { try { this.ws.send(JSON.stringify(o)); } catch (e) {} } }
    close() { this.closedByUser = true; try { this.ws && this.ws.close(); } catch (e) {} }
  }

  /* ================= 信令管理器 ================= */
  class Signaling {
    constructor(code, handlers) {
      this.code = SD.normalizeCode(code);
      this.h = handlers || {};
      this.peerId = SD.randomCode(8);
      this.transports = [];
      this.seen = new Set();
      this.topic = '';
      this.ready = false;
      this.mode = '';       // 'net' | 'lan' | 'net+lan'
      this.brokerIndex = 0;
      this.stopped = false;
    }

    async start() {
      this.topic = 'sd1/' + (await SD.roomIdFromCode(this.code));
      const onMsg = (topic, text) => this._incoming(text);
      const jobs = [];

      // 公网 MQTT
      jobs.push(this._startMqtt(onMsg));
      // 局域网中继（同源 /signal）
      if (location.protocol === 'http:' || location.protocol === 'https:') {
        const wsUrl = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/signal';
        const c = new LocalWsClient(wsUrl, { onMessage: onMsg, onStatus: (s) => this._status('lan', s) });
        c.subscribe(this.topic);
        this.transports.push({ name: 'lan', client: c });
        jobs.push(c.connect().then(() => {
          SD.log('局域网信令已就绪（离线可用）');
          this._refreshMode();
        }).catch(() => { /* 局域网没有中继，正常 */ }));
      }

      await Promise.all(jobs);
      this.ready = this.transports.some((t) => t.client.connected);
      return this.ready;
    }

    async _startMqtt(onMsg) {
      let brokers = (SD.loadSettings().brokers || SD.MQTT_BROKERS).slice();
      // https 页面禁止发起 ws://（混合内容），别白试、也别在控制台刷错误
      if (location.protocol === 'https:') brokers = brokers.filter((u) => /^wss:/i.test(u));
      if (!brokers.length) return;
      let first = true;
      const tryNext = async () => {
        if (this.stopped) return;
        const url = brokers[this.brokerIndex % brokers.length];
        this.brokerIndex++;
        const c = new MqttClient(url, {
          onMessage: onMsg,
          onStatus: (s) => this._status('net', s),
          onClose: () => { if (!this.stopped) setTimeout(tryNext, this.transports.some((t) => t.name === 'net' && t.client.connected) ? 2000 : 800); }
        });
        c.subscribe(this.topic);
        const entry = { name: 'net', client: c };
        if (!this.transports.some((t) => t.name === 'net')) this.transports.push(entry);
        else this.transports[this.transports.findIndex((t) => t.name === 'net')] = entry;
        try {
          await c.connect();
          if (first) SD.log('公网信令已就绪：' + url.replace(/^(wss?):\/\//, ''));
          else SD.log('信令已切换到：' + url.replace(/^(wss?):\/\//, ''));
          first = false;
          this._refreshMode();
        } catch (e) {
          this._failCount = (this._failCount || 0) + 1;
          // 把"为什么失败"打出来：手机上看一眼就知道是不是被运营商/系统联网权限拦了
          SD.log('信令服务器 ' + url.replace(/^(wss?):\/\//, '') + ' 连不上：' + (e && e.message || e), 'warn');
          if (this._failCount === brokers.length) {
            SD.log('所有公共信令服务器都连不上。请检查：① 手机的「应用联网权限」是否允许本应用联网；'
              + '② 是否开了代理/VPN；③ 换 WiFi 或手机热点再试。', 'err');
          }
          if (!this.stopped) setTimeout(tryNext, 300);
        }
      };
      await tryNext();
    }

    _status(name, s) {
      if (s === 'lost' || s === 'closed') SD.log((name === 'lan' ? '局域网' : '公网') + '信令断开', 'warn');
      this._refreshMode();
    }

    _refreshMode() {
      const net = this.transports.some((t) => t.name === 'net' && t.client.connected);
      const lan = this.transports.some((t) => t.name === 'lan' && t.client.connected);
      this.mode = net && lan ? 'net+lan' : net ? 'net' : lan ? 'lan' : '';
      this.ready = !!(net || lan);
      if (this.h.onStatus) this.h.onStatus(this.mode);
    }

    publish(obj) {
      obj.from = this.peerId;
      if (!obj.mid) obj.mid = SD.randomCode(6) + Date.now().toString(36).slice(-4);
      const text = JSON.stringify(obj);
      let n = 0;
      this.transports.forEach((t) => { if (t.client.publish(this.topic, text)) n++; });
      return n > 0;
    }

    _incoming(text) {
      let d;
      try { d = JSON.parse(text); } catch (e) { return; }
      if (!d || d.from === this.peerId) return;
      if (d.mid) { if (this.seen.has(d.mid)) return; this.seen.add(d.mid); if (this.seen.size > 800) { const it = this.seen.values(); for (let i = 0; i < 300; i++) this.seen.delete(it.next().value); } }
      if (this.h.onMessage) this.h.onMessage(d);
    }

    stop() {
      this.stopped = true;
      this.transports.forEach((t) => { try { t.client.close(); } catch (e) {} });
    }
  }

  SD.Signaling = Signaling;
  SD.MqttClient = MqttClient;
  SD.LocalWsClient = LocalWsClient;
  SD._signalingInternals = { encLen, encStr, cat, u16, u32, TD, TE };
})(window);
