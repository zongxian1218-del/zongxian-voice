// dc-smoketest.cpp —— libdatachannel 的 DataChannel 冒烟测试（单进程自建连，无需手动贴 SDP）
//
// 【为什么不用自带的 offerer/answerer】那两个例子要人工把 SDP 拷进 stdin，
// 无法重复跑、也无法进自动化。这里把两个 PeerConnection 放在同一进程里，
// 通过回调直接互换 SDP/候选，于是可以反复跑、还能继续长成"承载 H.264 分片"的验证。
//
// 测什么：建连耗时、小消息投递、**大载荷**（会走 SCTP 分块与重组 —— 正是我们关心的一段）。
//
// 用法: dc-smoketest.exe [小消息条数=200] [大载荷字节=262144]

#include <rtc/rtc.hpp>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <thread>

using namespace std::chrono_literals;

static double SinceMs(std::chrono::steady_clock::time_point t0)
{
    return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
}

int main(int argc, char** argv)
{
    const int msgCount = (argc > 1) ? atoi(argv[1]) : 200;
    const size_t bigBytes = (argc > 2) ? (size_t)_atoi64(argv[2]) : (size_t)(256 * 1024);

    rtc::InitLogger(rtc::LogLevel::Info);

    std::atomic<bool> chOpen{ false };
    std::atomic<int> got{ 0 };
    std::atomic<size_t> gotBytes{ 0 };
    std::atomic<bool> bigOk{ false };
    std::atomic<bool> failed{ false };
    std::atomic<int> candCount{ 0 };

    try {
        // 环回场景：host 候选就够，不需要 STUN（自带 examples 里 STUN 也是注释掉的）
        rtc::Configuration cfg;
        // 【为什么要绑 127.0.0.1】本机有多张网卡（IPv6 / 公网 26.x / 局域网 192.168.x），
        // 同进程两个 PeerConnection 做 ICE 时，候选对选择不稳定：实测 3 次里只有 1 次成功，
        // 失败时日志显示"一侧 connected、另一侧一直 checking"。做环回基准测试要先把变量固定住，
        // 否则测出来的是 ICE 抖动而不是传输性能。
        cfg.bindAddress = "127.0.0.1";
        auto pc1 = std::make_shared<rtc::PeerConnection>(cfg);
        auto pc2 = std::make_shared<rtc::PeerConnection>(cfg);

        // 【trickle ICE 的竞态】候选可能在"远端描述"之前就到达，那时 addRemoteCandidate
        // 会被丢掉，表现为 ICE 一直 Checking 然后 Disconnected。所以先缓冲、等描述就位再灌进去。
        std::vector<rtc::Candidate> candToPc2, candToPc1;
        std::atomic<bool> pc1RemoteSet{ false }, pc2RemoteSet{ false };

        pc1->onLocalDescription([&](rtc::Description d) {
            pc2->setRemoteDescription(d);
            pc2RemoteSet = true;
            for (auto& c : candToPc2) pc2->addRemoteCandidate(c);
            candToPc2.clear();
        });
        pc2->onLocalDescription([&](rtc::Description d) {
            pc1->setRemoteDescription(d);
            pc1RemoteSet = true;
            for (auto& c : candToPc1) pc1->addRemoteCandidate(c);
            candToPc1.clear();
        });
        pc1->onLocalCandidate([&](rtc::Candidate c) {
            printf("  pc1 候选: %s\n", c.candidate().c_str());
            ++candCount;
            if (pc2RemoteSet) pc2->addRemoteCandidate(c); else candToPc2.push_back(c);
        });
        pc2->onLocalCandidate([&](rtc::Candidate c) {
            printf("  pc2 候选: %s\n", c.candidate().c_str());
            ++candCount;
            if (pc1RemoteSet) pc1->addRemoteCandidate(c); else candToPc1.push_back(c);
        });

        pc1->onStateChange([](rtc::PeerConnection::State s) { printf("  pc1 state=%d\n", (int)s); });
        pc2->onStateChange([](rtc::PeerConnection::State s) { printf("  pc2 state=%d\n", (int)s); });

        std::shared_ptr<rtc::DataChannel> dc2;
        pc2->onDataChannel([&](std::shared_ptr<rtc::DataChannel> dc) {
            dc2 = dc;
            dc->onMessage([&](rtc::message_variant data) {
                if (std::holds_alternative<std::string>(data)) {
                    const std::string& s = std::get<std::string>(data);
                    ++got;
                    gotBytes += s.size();
                    if (s.size() == bigBytes) bigOk = true;
                } else {
                    ++got;
                    gotBytes += std::get<rtc::binary>(data).size();
                }
            });
        });

        const auto t0 = std::chrono::steady_clock::now();
        auto dc1 = pc1->createDataChannel("smoke");
        dc1->onOpen([&] {
            chOpen = true;
            printf("  通道打开，耗时 %.1f ms\n", SinceMs(t0));
        });
        dc1->onError([&](const std::string& e) { printf("  dc1 错误: %s\n", e.c_str()); failed = true; });

        while (!chOpen && !failed && SinceMs(t0) < 15000.0) std::this_thread::sleep_for(20ms);
        if (!chOpen) { printf("结果 FAIL：15 秒内通道没打开\n"); return 1; }
        const double openMs = SinceMs(t0);

        // ---- 小消息投递 ----
        const auto t1 = std::chrono::steady_clock::now();
        for (int i = 0; i < msgCount; ++i) dc1->send("msg " + std::to_string(i));
        while (got < msgCount && SinceMs(t1) < 15000.0) std::this_thread::sleep_for(5ms);
        const double smallMs = SinceMs(t1);
        const int smallGot = got.load();
        printf("  小消息: %d/%d 条，%.1f ms（%.0f 条/秒）\n", smallGot, msgCount, smallMs,
               smallMs > 0 ? smallGot * 1000.0 / smallMs : 0.0);

        // ---- 大载荷（SCTP 分块 + 重组）----
        std::string big(bigBytes, 'x');
        got = 0; gotBytes = 0;
        const auto t2 = std::chrono::steady_clock::now();
        dc1->send(big);
        while (!bigOk && SinceMs(t2) < 30000.0) std::this_thread::sleep_for(5ms);
        const double bigMs = SinceMs(t2);
        const bool bigArrived = bigOk.load();
        printf("  大载荷: %zu 字节 -> %s，%.1f ms（%.1f MB/s）\n", bigBytes,
               bigArrived ? "完整到达" : "未到达/超时", bigMs,
               bigMs > 0 && bigArrived ? (bigBytes / 1048576.0) / (bigMs / 1000.0) : 0.0);

        const bool pass = (smallGot == msgCount) && bigArrived;

        // ---- 可选：流式吞吐（带背压）----
        // 【为什么要背压】DataChannel 的发送缓冲没有上限保护，无脑 send 会把内存吃光；
        // 项目文档里已经点过这个坑（w2-chat-file-screen "必须做背压"）。这里用
        // bufferedAmount() 做高水位控制，并统计"send 被拒"的次数。
        const double streamMB = (argc > 3) ? atof(argv[3]) : 0.0;
        const size_t chunk = (argc > 4) ? (size_t)_atoi64(argv[4]) : (size_t)(16 * 1024);
        double streamSec = 0.0, streamThroughput = 0.0;
        size_t streamSent = 0, streamGot = 0;
        int sendRejected = 0;
        if (streamMB > 0.0) {
            const size_t total = (size_t)(streamMB * 1048576.0);
            std::string buf(chunk, 'y');
            gotBytes = 0;
            const auto t3 = std::chrono::steady_clock::now();
            while (streamSent < total) {
                while (dc1->bufferedAmount() > (size_t)(8 * 1024 * 1024))
                    std::this_thread::sleep_for(1ms);          // 高水位：等对端消化
                if (dc1->send(buf)) streamSent += chunk;
                else { ++sendRejected; std::this_thread::sleep_for(1ms); }
            }
            while (gotBytes < total && SinceMs(t3) < 120000.0) std::this_thread::sleep_for(1ms);
            streamSec = SinceMs(t3) / 1000.0;
            streamGot = gotBytes.load();
            streamThroughput = (streamSec > 0) ? (streamGot / 1048576.0) / streamSec : 0.0;
            printf("  流式: 发 %zu 字节 / 收 %zu 字节，%.2f 秒 -> %.1f MB/s"
                   "（块 %zu B，send 被拒 %d 次）\n",
                   streamSent, streamGot, streamSec, streamThroughput, chunk, sendRejected);
        }

        dc1->close();
        pc1->close();
        pc2->close();
        std::this_thread::sleep_for(300ms);

        const bool passAll = pass && (streamMB <= 0.0 || streamGot >= (size_t)(streamMB * 1048576.0));
        printf("\n结果: %s（建连 %.1f ms，小消息 %.0f 条/秒，大载荷 %.1f MB/s%s）\n",
               passAll ? "PASS" : "FAIL", openMs,
               smallMs > 0 ? smallGot * 1000.0 / smallMs : 0.0,
               bigMs > 0 && bigArrived ? (bigBytes / 1048576.0) / (bigMs / 1000.0) : 0.0,
               streamMB > 0.0 ? (", 流式 " + std::to_string((int)streamThroughput) + " MB/s").c_str() : "");
        return passAll ? 0 : 1;
    } catch (const std::exception& e) {
        printf("异常: %s\n结果: FAIL\n", e.what());
        return 1;
    }
}
