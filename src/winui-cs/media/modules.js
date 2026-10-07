// ===========================================================================
// modules.js —— 页面模块的统一宿主（S4 方案 A）
//
// 【为什么要这个文件】S3 把 call.js 拆成 13 个模块后，"接线"仍散在 call.js 末尾：
// 一串 `xxx = createXxx(io)`，而且"谁持有定时器/流、谁负责释放"全靠人记，
// 页面卸载只有 onPageGone 一条兜底（它调用 stopAll 顺手清）。
//
// 【本文件提供什么】
//   1) 统一形状：模块 = { name, create(deps), deps }，工厂返回值可带 dispose()；
//   2) 声明式接线：一张表说清"有哪些模块、按什么顺序起"，不再散落；
//   3) 统一生命周期：host.disposeAll() 按**注册相反顺序**逐个释放，逐个 try/catch，
//      任何一个模块释放失败都不影响其它模块，并统一上报（module-error）。
//
// 【这是页面侧的纯 JS 约定（方案 A，已与用户确认）】不引入跨语言抽象，
// 只解决"接线与生命周期"。
// ===========================================================================

/**
 * 创建模块宿主。
 * @param {{log:Function, post:Function}} io 宿主自身诊断用的日志/上报
 */
export function createModuleHost(io) {
  const log = io.log || (() => { });
  const post = io.post || (() => { });
  /** 已接线的项（按注册顺序；释放时反向） */
  const wired = [];

  return {
    /** 已接线模块名（顺序即初始化顺序；自检/诊断用） */
    names() { return wired.map((m) => m.name); },

    /**
     * 接线一个模块：调它的 create(deps) 并记住返回值。
     * @param {{name:string, create:Function, deps?:object}} mod
     * @returns 工厂返回值；失败返回 null 并上报（**不许静默**：模块起不来就是功能缺失）
     */
    wire(mod) {
      const { name, create, deps = {} } = mod;
      try {
        const api = create(deps) || {};
        wired.push({ name, api });
        log(`<span class="k">[模块] ${name} 已就绪</span>`);
        return api;
      } catch (e) {
        const msg = `${(e && e.name) || 'Error'}: ${(e && e.message) || e}`;
        log(`<span class="bad">[模块] ${name} 初始化失败: ${msg}</span>`);
        post({ type: 'module-error', module: name, stage: 'init', message: msg,
               stack: String((e && e.stack) || '').split('\n').slice(0, 3).join(' | ') });
        return null;
      }
    },

    /** 注册一个"非模块"的释放动作（页面自己持有的定时器/流/ws 等）。 */
    addDisposable(name, fn) {
      wired.push({ name, api: { dispose: fn } });
    },

    /**
     * 按**注册相反顺序**释放全部。
     * 为什么反向：后启动的模块可能依赖先启动的（如 stats 依赖 mesh 的 pc），
     * 先关依赖方、再关被依赖方，避免释放途中有回调打到已关闭的对象上。
     * @param {string} reason 释放原因（写日志用）
     */
    disposeAll(reason) {
      const failed = [];
      let released = 0;
      for (let i = wired.length - 1; i >= 0; i--) {
        const { name, api } = wired[i];
        try {
          if (api && typeof api.dispose === 'function') {
            const r = api.dispose();
            // 异步释放：不阻塞其它模块，但必须接住 rejection（否则又是一次静默失败）
            if (r && typeof r.then === 'function') {
              r.catch((e) => post({ type: 'module-error', module: name,
                                    stage: 'dispose-async', message: String((e && e.message) || e) }));
            }
            released++;
          }
        } catch (e) {
          failed.push(name);
          post({ type: 'module-error', module: name, stage: 'dispose',
                 message: String((e && e.message) || e) });
        }
      }
      wired.length = 0;
      log(`<span class="warn">[模块] 释放 ${released} 个${reason ? '（' + reason + '）' : ''}` +
          `${failed.length ? '，失败: ' + failed.join(',') : ''}</span>`);
      // 【为什么要 post】关窗时页面的 log() 可能已经写不进去（隐藏 DOM），
      // 而 C# 侧的日志是判断"到底释放了没有"的唯一可靠口径 ⇒ 结果必须走事件。
      post({ type: 'modules-disposed', released, failed, reason: reason || '' });
      return { released, failed };
    },
  };
}
