# libdatachannel 可行性验证（传输层替换评估）（2026-10-05 起）

> **为什么做**：用户提出"这些都有开源项目，感觉在重复造轮子"。对照后确认：自建 UDP 传输层
> （32B 包头 + 分片 + 单片 XOR 校验，**没有拥塞控制 / 加密 / NAT 打洞**）是其中最值得换的一处。
> 用户拍板：**先做 1~2 天可行性验证，用实测数据决定换不换**，不赌。
>
> 边界（重要）：`route-decision-2026-10-05.md` 里"**不做 WebRTC 作为视频主路径 / 不引 libwebrtc 整包**"
> 维持不变。这里评估的是 **libdatachannel（MPL-2.0，C++17）只替换传输子层**，不动
> 采集 → NV12 → 硬件 H.264 → 解码 → D3D11 上屏这条已验证链路。
>
> 许可结论（已核实）：**MPL-2.0 = 可以链接进闭源产品**；用户已表示"愿意开源（GPL/AGPL 可接受）"，
> 所以即使将来要换更重的方案（Sunshine GPL-3.0 / RustDesk AGPL-3.0）也不再有许可障碍 ——
> 但那是另一条路，**等本次验证结果出来再单独评估**。

---

## 一、这台机器能不能构建 libdatachannel？（第一个关卡）

### 已确认的事实（都是实测，不是推断）

| 项 | 结果 |
|---|---|
| cmake | ✅ 在 `C:\Program Files\CMake\bin\cmake.exe`（**不在 PATH**，要写绝对路径） |
| ninja / vcpkg | ❌ 不在 PATH（本次不走 vcpkg，直接 cmake + NMake） |
| MSVC | ✅ `D:\BuildTools\VC\Auxiliary\Build\vcvars64.bat`（既有工具链） |
| `git clone https://github.com/...` | ❌ **失败**：`github.com:443` 连不上，5 个子模块全部拉不下来 |
| `codeload.github.com` 的 tar.gz | ✅ **可用**（`Invoke-WebRequest` 实测下到 209 KB 的 plog 包） |
| `raw.githubusercontent.com`（PowerShell 直连） | ❌ TLS 握手失败（但 harness 的 web_fetch 能取到） |
| NuGet 源 | ✅ nuget.org 已注册（WinUIEx 走这条路） |

### 绕法：用仓库记录的**固定 commit** 从 codeload 拉依赖

子模块拉不动，就按 `git ls-tree HEAD deps/` 里记录的 gitlink 精确取源码 —— 避免用 HEAD 造成版本漂移：

| 依赖 | commit | 结果 |
|---|---|---|
| plog | `94899e0b` | 202 KB → 136 文件 |
| usrsctp | `fec583d5` | 756 KB → 269 文件 |
| libjuice | `b89c792e` | 106 KB → 81 文件 |
| json (nlohmann) | `55f93686` | 9.4 MB → 1163 文件 |
| libsrtp | `d33b8ffb` | 637 KB → 2387 文件 |
| MbedTLS v2.28.8（DTLS 后端，选它是因为比 OpenSSL 轻太多） | tag | 3.9 MB → 1452 文件 |

脚本：`build/fetch-datachannel-deps.ps1`、`build/fetch-mbedtls.ps1`（**纯 ASCII**）、
构建脚本 `build/build-datachannel.cmd`（`USE_MBEDTLS=1`，NMake Makefiles，x64 Release）。

### 又一个必须记住的编码坑

`.ps1` **也必须纯 ASCII**：Windows PowerShell 按 ANSI（本机 GBK）读脚本，
中文注释会吃掉后面的引号 → 解析报错（`意外的标记` / `字符串缺少终止符`）。
这和仓库里 `.cmd` 早就定下的"纯 ASCII"规矩是同一个原因。

---

## 二、验证计划（拿到能构建的结果之后）

1. **能不能跑起来**：用 libdatachannel 的 examples（`examples/` 里有 `echo`/`streamer`）做本机环回，
   确认 DataChannel 能建立。
2. **同一套客观指标对比现有 UDP**（按项目"能不能测"的传统）：
   端到端帧数、解码延迟、抓图非黑率、**丢包链路下的吞吐**（现有 UDP 只有单片 XOR 校验；
   libdatachannel 有 NACK/FEC/拥塞控制）。
3. **把现有 `sender-probe` 的发送侧换成 DataChannel 发送**（保留 NV12→H264 全部逻辑），
   接收侧同理，跑同一份端到端验收。
4. **决策**：只有第 2/3 步的数据明显更好、且不破坏已验收的链路，才提"换"。

---

## 三、进度记录

### 2026-10-05：**第一个关卡通过 —— 能在这台机器上构建出来**

```
=== [0/3] enable MbedTLS DTLS-SRTP ===
=== [1/3] configure MbedTLS ===
=== [2/3] build MbedTLS ===
=== [3/3] configure+build libdatachannel ===
[100%] Built target datachannel-copy-paste-capi-answerer
BUILD OK
```

产物（都实测存在）：

| 文件 | 大小 |
|---|---|
| `D:\BuildTools\mbedtls\build\library\mbedtls.lib` | 1.20 MB |
| `D:\BuildTools\libdatachannel\build\datachannel.lib` | 701 KB |
| `D:\BuildTools\libdatachannel\build\datachannel.dll` | 2.41 MB |

示例程序也编出来了（做冒烟测试用）：`offerer.exe` / `answerer.exe` / `streamer.exe` /
`media-sender.exe` / `media-receiver.exe` / `client.exe`（另有 `-capi` 版本）。

### 这一路踩的 6 个坑（每个都是"实测报错 → 定位 → 修"，全部记下来免得重踩）

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | `git clone --recursive` 子模块全失败 | 本机到 `github.com:443` 不通 | 按仓库记录的 **gitlink 固定 commit** 从 `codeload.github.com` 下 tar.gz（`Invoke-WebRequest` 可用） |
| 2 | `.ps1` 报"意外的标记"、中文串吃引号 | Windows PowerShell 按 ANSI(GBK) 读脚本 | **.ps1 也纯 ASCII**（同 `.cmd` 的规矩） |
| 3 | `""C:\Program' is not recognized` | `set CM="C:\Program Files\..."` 再 `"%CM%"` 变成双引号嵌套 | 变量里**不要带引号**：`set CM=C:\Program Files\...` |
| 4 | `Compatibility with CMake < 3.5 has been removed` | 本机 CMake **4.4.3**，MbedTLS 声明 2.8.12 | `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` |
| 5 | `Could NOT find MbedTLS` / 版本 0.0.0 / 要求 ≥3 | ① 变量名**大小写敏感**（`MBEDTLS_LIBRARY` 无效，要 `MbedTLS_LIBRARY`）② 2.28 LTS 太老 | 用精确大小写变量名；换 **MbedTLS 3.6.2** |
| 6 | `C4819`（代码页 936）被当错误；`mbedtls_pk_copy_from_psa`/`mbedtls_ssl_get_dtls_srtp_negotiation_result` 找不到 | ① libsrtp 源码是 UTF-8，MSVC 默认 GBK ② 这两个符号需要 MbedTLS ≥3.6 且 **DTLS-SRTP 默认未开** | `-DCMAKE_C_FLAGS=/utf-8`；`framework` 子模块按 commit 拉齐；`config.py set MBEDTLS_SSL_DTLS_SRTP` |

> 这些坑一条都不"深"，但每条都能让构建原地失败半小时 —— 所以脚本已经固化：
> `build/fetch-datachannel-deps.ps1`、`build/fetch-mbedtls.ps1`、`build/build-datachannel.cmd`。

### 下一步（本轮之后）

1. **冒烟**：用 `offerer`/`answerer` 做一次本机 DataChannel 建连（SDP 用文件/管道交换，避免手动粘贴）。
2. **对比现有 UDP**：同样的客观指标（端到端帧数、解码延迟、非黑率、**丢包链路吞吐**）。
3. 通过再把 `sender-probe` 的发送侧换成 DataChannel，跑同一份端到端验收。

---

## 四、第二个关卡：DataChannel 真能跑通，并拿到吞吐数字

自带的 `offerer`/`answerer` 要人工贴 SDP，没法重复跑，所以另写了
`src/media/probe/dc-smoketest.cpp`（+ `build-dc-smoketest.cmd`）：**两个 PeerConnection 放同一进程**，
SDP/候选通过回调互给，可反复自动跑；还能直接长成"承载 H.264 分片"的验证。

### 结果

```
ICE checking → connected → DTLS handshake finished → SCTP connected
小消息: 200/200 条，15.1 ms（13245 条/秒）
大载荷: 262144 字节 -> 完整到达，15.6 ms
流式: 发 134217728 字节 / 收 138903552 字节，5.02 秒 -> 26.4 MB/s（块 16384 B，send 被拒 327 次）
结果: PASS（建连 25.6 ms，小消息 13245 条/秒, 流式 26 MB/s）
```

（跑了 2 次，26.4 / 26.7 MB/s，很稳。）

### 三个必须记下来的观察

**① 吞吐：DataChannel ≈ 26 MB/s，自研 UDP ≈ 145 MB/s（LAN 实测）**

| 方案 | 环回/局域网吞吐 | 来源 |
|---|---|---|
| libdatachannel DataChannel（16KB 块 + 背压） | **26.4 MB/s** | 本次实测 |
| A 线网页版 WebRTC DataChannel（真实公共信令 + 真机） | **27.5 MB/s** | `dist/zongxian-transfer-guide.md` 已有记录 |
| A 线桌面版多流 TCP | **154 MB/s** | 同上 |
| B 线自研 UDP（32B 包头 + 分片 + XOR 校验） | **145.5 MB/s** | `README.md` |

两个独立来源（本次 libdatachannel 26.4 / A 线 WebRTC 27.5）**互相印证** —— 说明 26 MB/s 不是
测试写错了，而是**用户态 SCTP 数据通道的量级**。也就是说：**换 libdatachannel 会在 LAN 吞吐上
付 ~5 倍的代价**，换来的是 ICE 打洞 / DTLS 加密 / 拥塞控制 / NACK-FEC —— 这笔账值不值，
要看你更缺哪一头（局域网内用 → 自研 UDP 明显更优；跨网直连 → DataChannel 直接给你方案）。

**② 背压确实必须做**：128 MB 里 `send` 被缓冲上限拒了 **327 次**。
项目文档 `w2-chat-file-screen-2026-10-04.md` 早就点过这个坑（"DataChannel 的发送缓冲没有上限保护，
无脑 send 会把内存吃光"），实测证实。本次用 `bufferedAmount() > 8MB` 做高水位控制。

**③ 同进程双 PeerConnection 的 ICE 不稳定（3 次只过 1 次）**：本机有多张网卡
（IPv6 / 公网 26.103.67.21 / 局域网 192.168.1.104），失败时日志显示"一侧 connected、另一侧一直
checking"。**把候选限制到 `bindAddress = 127.0.0.1` 后 3/3 通过**。
判断：这是**测试形态**（同进程、同机、多网卡自连）的产物，不一定代表真实两台机器；
但基准测试必须固定变量，否则测到的是 ICE 抖动而不是传输性能。

### 还没做

- 接收字节比发送多约 3.5%（138.9 MB vs 134.2 MB）——**测试账目问题，待查**（可能是
  账目重置时机，或上一阶段的大载荷仍在途）。在把它当基准之前要修干净。
- 把 H.264 码流真的灌进 DataChannel 跑端到端（帧数/延迟/非黑率）。
- **丢包链路**下的对比（现有 UDP 只有单片 XOR；这是 DataChannel 理论上的强项）——
  本机怎么造可控丢包要先想清楚（clumsy / 自建丢包代理）。
