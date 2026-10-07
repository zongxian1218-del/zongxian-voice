# 造轮子对照：哪些该借开源、哪些已经淘汰过（2026-10-05）

> 起因：用户提出"这些东西都有开源项目可以参考，感觉在重复造轮子"。
> **这个意见成立**，而且接手时就应该先做这份对照。但项目历史上已经走过一轮
> "转 WebRTC"（10-04 决定 → 10-05 推翻，见 `route-decision-2026-10-05.md`），
> 所以关键不是"要不要用开源"，而是**分部位**看：哪些是真白造、哪些是当初实测淘汰掉的。
>
> 本文所有许可信息都来自 GitHub API / 官方站点核实（2026-10-05），不是凭记忆。

---

## 一、许可先摆出来（这决定了"能不能用"，不是"好不好用"）

| 项目 | 语言 | ★ | 许可 | 能不能链接进闭源产品 |
|---|---|---|---|---|
| [Sunshine](https://github.com/LizardByte/Sunshine)（Moonlight 串流主机） | C++ | 41.9k | **GPL-3.0** | ❌ 不能；只能**读思路** |
| [RustDesk](https://github.com/rustdesk/rustdesk)（远程桌面） | Rust | 125k | **AGPL-3.0** | ❌ 不能；只能**读思路** |
| [libdatachannel](https://libdatachannel.org/)（WebRTC 数据通道/媒体传输，C++17） | C++ | — | **MPL-2.0** | ✅ 可以（文件级 copyleft） |
| [LocalSend](https://github.com/localsend/localsend)（局域网互传，AirDrop 替代） | Dart | 93.4k | **Apache-2.0** | ✅ 可以 |
| [croc](https://github.com/schollz/croc)（无配置点对点传文件，PAKE） | Go | 40.5k | **MIT** | ✅ 可以 |
| [WinUIEx](https://github.com/dotMorten/WinUIEx)（WinUI 3 窗口扩展） | C# | 844 | **MIT** | ✅ 可以 |
| [微软 ApplicationLoopback 示例](https://learn.microsoft.com/samples/microsoft/windows-classic-samples/applicationloopbackaudio-sample/)（单应用音频回环） | C++ | — | MIT | ✅ 可以 |

> GPL / AGPL 的边界（"链接"vs"独立进程命令行调用"）属于法律判断，**需要你或法务拍板**，
> 我只能把技术事实摆清楚。棕仙若选择 GPL/AGPL 开源，Sunshine/RustDesk 就能直接用。

---

## 二、逐部位对照

| 我们自造的东西 | 位置 | 现成替代 | 判定 |
|---|---|---|---|
| **远程控制授权 UX**（弹窗、默认拒绝、超时、红条、一键停止、全局热键、状态回传） | `src/media/probe/control-auth.h` | RustDesk 的"连接确认 + 永久密码 + 白名单"、Sunshine 的配对 PIN 流程 | 🔴 **最典型的白造**。刚为此修了 4 个 bug（拒绝后无法再次请求、换会话不复位、静默丢弃、口径混乱），这些人家几年前就解决完了 |
| **自建 UDP 传输**（32B 包头 + 分片 + 单片 XOR 校验；无拥塞控制、无加密、无打洞） | `src/media/transport/media-packet.h`、`udp-socket.h` | libdatachannel（MPL-2.0，NACK/FEC/拥塞控制/DTLS/ICE 都现成）、SRT（MPL-2.0） | 🟡 **值得换**：不是不能用（LAN 上实测 145.5 MB/s、丢包恢复有实测），但"跨网/NAT/丢包链路"这块等于要自己写一遍工业级实现 |
| **WinUI 3 无边框顶层覆盖窗口**（frameless/topmost/定位/最小化跟随） | `src/winui-videoprobe/`、`receiver-probe.cpp` | WinUIEx（MIT，`WindowEx`/`SetAlwaysOnTop` 等） | 🟢 **直接借**，零风险；我们自己踩的坑（WS_POPUP 没 `ShowWindow` 导致不可见、topmost 被忽略）它早就封装好了 |
| **单应用音频采集**（WASAPI 进程回环 + 子进程隔离） | `src/audio/`（部分留用） | 微软 ApplicationLoopback 示例（MIT） | 🟢 **该借**：文档已定"必须子进程隔离"（进程回环会硬崩 `0xC0000374`），示例就是这个用法 |
| **采集→NV12→硬件 H.264→解码→上屏** | `sender-probe.cpp` / `receiver-probe.cpp` | Sunshine（GPL，不能链接） | 🟢 **保留自研**：这部分有实测数据支撑（帧率/延迟/非黑率），且换成 GPL 项目不可行 |
| **桌面版多流 TCP + 4MB SHA-256 分块续传**（A 线） | `src/swiftdrop/` | croc（MIT）、LocalSend（Apache-2.0） | 🟢 **保留**：已发布 v1.0、纯标准库零依赖、实测 154 MB/s、还能与组网工具配合；重写收益为负 |
| **网页版 WebRTC + 自研 MQTT-over-WS 信令**（A 线） | `src/web/`、`webhost.py` | PairDrop/Snapdrop 的思路、croc 的 PAKE 设计 | 🟡 **保留但可借鉴**：CONTRIBUTING 里已列出的短板（单条 SCTP 流吞吐塌、背压）正是 croc/LocalSend 处理过的问题 |
| **信令中继 `/signal`** | `src/swiftdrop/webhost.py` | — | 🟢 **保留**：已实测，且是"不装服务器"的产品约束的一部分 |

---

## 三、为什么"视频主路径不要转 WebRTC"这个结论我建议维持

`route-decision-2026-10-05.md` 里"已确认不做：WebRTC/getDisplayMedia 作为视频主路径、libwebrtc 整包"。
那一条是**实测淘汰**出来的（每换一条路都留了验证文档），理由是"判断依据始终是能不能测"。
现在再翻一次会是**第三次反复**，代价是推倒已有实测链路。

**但要注意区分两件事**：
- ❌ **视频主路径换 libwebrtc / getDisplayMedia** → 维持"不做"。
- ✅ **只把传输层换成 libdatachannel（不是 libwebrtc 整包，MPL-2.0、C++17、Windows 支持、依赖只有 OpenSSL/GnuTLS）**
  → 这不动采集/编码/解码/上屏，换来：ICE 打洞（跨网不用自己写）、NACK/FEC、拥塞控制、DTLS 加密、
  甚至能用浏览器当查看端。**这才是"少造轮子"收益最大的一处。**

---

## 四、建议的走法（三选一）

### 方案 A（推荐）：只换真正的轮子，不动已验证的链路
1. **WinUIEx** 替换手写窗口逻辑（半天，MIT）。
2. **RustDesk / Sunshine 只作设计参考**，重做授权 UX（当前版本能跑，但对齐它们的边界情况）。
3. **传输层做一次 libdatachannel 可行性验证**（1~2 天，按项目传统：用同样的客观指标对比
   现有 UDP：端到端帧数、解码延迟、非黑率、丢包恢复），**验证通过再决定换不换**。
4. 单应用音频以微软 ApplicationLoopback 示例为起点。

### 方案 B（激进）：B 线整体换到现成方案
被控端直接用 Sunshine（若接受 GPL）或 RustDesk（AGPL），棕仙只做外壳 + 语音 + 业务整合。
省掉绝大部分造轮子，但要接受：许可约束、上游技术栈（C++/Rust/Dart）、产品形态被上游框住。

### 方案 C（保持现状）
继续自研。可以，但要接受"每个已解决的问题都要自己再踩一遍"——本轮 4 个 bug 就是账单。

---

## 五、用户决策（2026-10-05）

| 问题 | 决定 |
|---|---|
| B 线传输层是否换 libdatachannel | **先做 1~2 天可行性验证，用实测数据再定** → 已开工，进度见 `libdatachannel-spike-2026-10-05.md` |
| WinUIEx 是否引入 | **直接引入**（MIT，低风险；待办） |
| 棕仙语音的许可形态 | **愿意开源（GPL/AGPL 可接受）** |

第三条的影响要说清楚：它把 Sunshine（GPL-3.0）/ RustDesk（AGPL-3.0）从"只能读思路"
变成"**可以直接复用**"，理论上最省造轮子的路变成"被控端直接用 Sunshine，棕仙只做外壳/语音/业务"。
但这会推翻现有的自研 C++ 管线（已有多份实测文档），所以**建议顺序**：
先看 libdatachannel 验证结果（它不动已验证链路），再单独评估"整体复用 Sunshine/RustDesk"这条路。
两条路的收益/代价不同，不要混在一次决策里。

---

## 六、需要你拍板的问题（已答，留档）

1. 传输层要不要走"libdatachannel 可行性验证"（方案 A 第 3 条）？
2. WinUIEx 是否直接引入（低风险、MIT）？
3. 被控端是否有"未来对外开放/卖"的计划？——如果有，GPL/AGPL 项目（Sunshine/RustDesk）
   就只能读思路；如果棕仙自己愿意 GPL/AGPL 开源，方案 B 立刻变得很划算。
