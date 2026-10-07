// ===========================================================================
// link.js —— 一条对端链路的**全部上下文**（S3 第 2 步：把隐式单例变成显式对象）
//
// 【为什么要这个文件】审计病根 1 的原话是"隐式单例 vs mesh 多链路"：
// 页面按"只有一个对端"写 —— 单 state.pc、单 remoteAudioEl、单 chatChannel、单 screenSender；
// 可产品要 mesh（3 人）。后果（都实测过）：
//   · ontrack 只有两路分支，后到的音频覆盖先到的；
//   · 同一事实存两份（links Map 与 state.pc/state.remoteId 别名），3 人时第二个对端的状态永不上报；
//   · 共享电脑声音的常驻音轨与通话语音共用同一个播放元素，互相覆盖。
//
// 【本文件给出的修复】一条链路的**所有**上下文只存在一个地方：
//   Link = { id, pc, chat, screenSender, screenAudioSender, createdAt }
// 谁要"按链路做事"（音频上报、统计、屏幕轨、共享声音、聊天）就必须接收 link 参数，
// 页面里不再有"当前对端"这种全局概念；需要"主链路"时由 primary() 明确选一条。
//
// 依赖方向：link.js 不依赖 call.js（只通过网络回调与注入的日志/上报函数）。
// ===========================================================================

/**
 * 创建一条链路对象。
 * @param {string} id       对端 id
 * @param {RTCPeerConnection} pc
 * @param {{log?:Function, post?:Function}} io  日志与页面上报（由调用方注入，避免循环依赖）
 */
export function createLink(id, pc, io = {}) {
  const log = io.log || (() => {});
  const post = io.post || (() => {});
  return {
    id: String(id),
    pc,
    chat: null,                 // RTCDataChannel
    screenSender: null,         // 屏幕视频轨 sender
    screenAudioSender: null,    // 屏幕音频轨 sender
    createdAt: Date.now(),
    lastStats: null,            // 该链路的统计快照（每条链路各存一份）
    gotAudio: false,            // 是否真的收到过音频包（区分"有 receiver"与"有声音"）

    /** 只保留与自己链路相关的字段，避免一个事实存两份。 */
    snapshot() {
      return { id: this.id, createdAt: this.createdAt, hasChat: !!this.chat, pcState: this.pc ? this.pc.connectionState : 'none' };
    },

    /** 显式释放：轨、通道、连接都要收干净（挂断/对端离开都走这里）。 */
    close(reason = '') {
      for (const s of [this.screenSender, this.screenAudioSender]) {
        try { if (s) this.pc.removeTrack(s); } catch (e) { }
      }
      this.screenSender = null;
      this.screenAudioSender = null;
      try { if (this.chat) this.chat.close(); } catch (e) { }
      this.chat = null;
      try { if (this.pc) this.pc.close(); } catch (e) { }
      if (reason) log(`[链路] ${this.id} 已释放（${reason}）`);
      post({ type: 'link-closed', link: this.id, reason });
    },
  };
}

/** 链路注册表：peerId -> Link。所有"按对端"的东西都从这里查。 */
export class LinkRegistry {
  constructor() { this.map = new Map(); this.primaryId = null; }

  get size() { return this.map.size; }
  has(id) { return this.map.has(String(id)); }
  get(id) { return id == null ? null : (this.map.get(String(id)) || null); }
  values() { return this.map.values(); }
  ids() { return [...this.map.keys()]; }
  /**
   * 既有代码里有 `for (const [id, l] of links)`（本来是 Map 的写法）⇒ 注册表必须可迭代，
   * 否则那处会抛 "links is not iterable"（2026-10-06 实测：shareAudioDebug 因此整段失败）。
   */
  entries() { return this.map.entries(); }
  keys() { return this.map.keys(); }
  [Symbol.iterator]() { return this.map.entries(); }

  /** 登记一条链路；若还没有主链路，它就是主链路。 */
  add(link) {
    this.map.set(link.id, link);
    if (!this.primaryId) this.primaryId = link.id;
    return link;
  }

  /**
   * 主链路 = **最早建立**的那条（对话/统计/挂断默认针对它）。
   * 【为什么不取最新】原来到处用 state.pc 表示"当前对端"，而它被 5 处覆盖写，
   * 于是"谁是主链路"取决于最后一次赋值的时机 —— 3 人时表现为状态乱跳。现在规则固定。
   */
  primary() {
    if (this.primaryId) {
      const l = this.get(this.primaryId);
      if (l) return l;
    }
    let best = null;
    for (const l of this.map.values()) if (!best || l.createdAt < best.createdAt) best = l;
    this.primaryId = best ? best.id : null;
    return best;
  }

  /** 显式指定主链路（对端离开后由页面的 adopt 流程调用）。 */
  setPrimary(id) {
    this.primaryId = id == null ? null : String(id);
    return this.primary();
  }

  /** 删除并释放；若删的是主链路，主链路会按"最早建立"重新选出。 */
  remove(id, reason = '') {
    const l = this.get(id);
    if (!l) return null;
    this.map.delete(l.id);
    if (this.primaryId === l.id) this.primaryId = null;
    l.close(reason);
    this.primary();
    return l;
  }

  /** 全部释放（挂断/整页停机）。 */
  clear(reason = '') {
    for (const l of this.map.values()) l.close(reason);
    this.map.clear();
    this.primaryId = null;
  }

  /** 遍历所有 PeerConnection（统计、屏幕轨扇出都用它）。 */
  allPcs() {
    const pcs = new Set();
    for (const l of this.map.values()) if (l && l.pc) pcs.add(l.pc);
    return pcs;
  }
}
