// udp-socket.h —— 极薄的 UDP 封装
//
// 只做三件事：绑定、收发、设置缓冲/超时。不做重传、不做拥塞控制 ——
// 那些由 media-packet.h 的分片重组与关键帧请求机制负责。
//
// 【为什么 header-only】
// 目前只有几个探针用它，独立 .cpp 会多一个编译单元和一处链接配置。
// 等接入正式应用时再拆出去。
// ============================================================================

#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#include <mstcpip.h>   // SIO_UDP_CONNRESET（见 Open() 里的说明）

// 有些 SDK 版本不在 mstcpip.h 里暴露这个宏，按官方定义补一个（值来自 _WSAIOW(IOC_VENDOR,12)）
#ifndef SIO_UDP_CONNRESET
#define SIO_UDP_CONNRESET _WSAIOW(IOC_VENDOR, 12)
#endif

#pragma comment(lib, "ws2_32.lib")

namespace zx {

// Winsock 需要成对初始/清理，用 RAII 保证不会漏
class WsaGuard {
public:
    WsaGuard()
    {
        WSADATA d{};
        ok_ = (WSAStartup(MAKEWORD(2, 2), &d) == 0);
    }
    ~WsaGuard() { if (ok_) WSACleanup(); }
    bool ok() const { return ok_; }
    WsaGuard(const WsaGuard&) = delete;
    WsaGuard& operator=(const WsaGuard&) = delete;
private:
    bool ok_ = false;
};

class UdpSocket {
public:
    UdpSocket() = default;
    ~UdpSocket() { Close(); }
    UdpSocket(const UdpSocket&) = delete;
    UdpSocket& operator=(const UdpSocket&) = delete;

    // 【缓冲必须在 bind 之前设置】Windows 上 bind 之后再设 SO_RCVBUF
    // 可能不生效，仍是默认的 64KB。一个 IDR 帧约 130KB（109 个分片）
    // 瞬间发出，64KB 缓冲必然溢出丢分片 —— 实测 2 个 IDR 全丢。
    bool Open(uint16_t bindPort, const char* bindAddr = "0.0.0.0", bool reuseAddr = false,
              int recvBufBytes = 0, int sendBufBytes = 0)
    {
        sock_ = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        if (sock_ == INVALID_SOCKET) return false;

        if (reuseAddr) {
            BOOL yes = TRUE;
            setsockopt(sock_, SOL_SOCKET, SO_REUSEADDR, (const char*)&yes, sizeof(yes));
        }
        if (recvBufBytes > 0) {
            int v = recvBufBytes;
            setsockopt(sock_, SOL_SOCKET, SO_RCVBUF, (const char*)&v, sizeof(v));
        }
        if (sendBufBytes > 0) {
            int v = sendBufBytes;
            setsockopt(sock_, SOL_SOCKET, SO_SNDBUF, (const char*)&v, sizeof(v));
        }

        sockaddr_in a{};
        a.sin_family = AF_INET;
        a.sin_port = htons(bindPort);
        if (inet_pton(AF_INET, bindAddr, &a.sin_addr) != 1) { Close(); return false; }
        if (bind(sock_, (sockaddr*)&a, sizeof(a)) == SOCKET_ERROR) { Close(); return false; }

        // 【Windows UDP 的经典陷阱】对端端口一旦消失，它回的 ICMP "端口不可达" 会让本 socket
        // 后续 recvfrom 报 WSAECONNRESET —— 在部分 Windows 配置上这个错误是**持续的**，
        // 表现为"收包突然永久停止"（远程控制场景里：查看端退出后，被控端再也收不到任何请求）。
        // 关闭 SIO_UDP_CONNRESET 即恢复成 POSIX 语义（错误只报一次、不影响后续收包）。
        // 【诚实标注】本机实测**没有**复现出"持续失败"（ICMP 只影响了一次 drain），
        // 但这是官方文档推荐的必备防护，代价为零，加上。
        {
            BOOL disable = FALSE;
            DWORD bytes = 0;
            if (WSAIoctl(sock_, SIO_UDP_CONNRESET, &disable, sizeof(disable),
                         nullptr, 0, &bytes, nullptr, nullptr) == SOCKET_ERROR) {
                // 老系统不支持也无所谓，不影响主流程
            }
        }
        return true;
    }

    // 读回真正生效的缓冲大小 —— Windows 会把请求值调整/截断，
    // 不读回来就无法判断到底给了多少
    int GetRecvBuffer() const
    {
        int v = 0, len = sizeof(v);
        if (getsockopt(sock_, SOL_SOCKET, SO_RCVBUF, (char*)&v, &len) == SOCKET_ERROR) return -1;
        return v;
    }
    int GetSendBuffer() const
    {
        int v = 0, len = sizeof(v);
        if (getsockopt(sock_, SOL_SOCKET, SO_SNDBUF, (char*)&v, &len) == SOCKET_ERROR) return -1;
        return v;
    }

    // 【注意】Winsock 的 SO_RCVTIMEO 单位是毫秒（和 POSIX 的 timeval 不同）
    bool SetRecvTimeout(int ms)
    {
        DWORD t = (DWORD)ms;
        return setsockopt(sock_, SOL_SOCKET, SO_RCVTIMEO, (const char*)&t, sizeof(t)) == 0;
    }

    // 视频帧突发量大，默认接收缓冲很容易溢出，必须显式调大
    bool SetRecvBuffer(int bytes)
    {
        return setsockopt(sock_, SOL_SOCKET, SO_RCVBUF, (const char*)&bytes, sizeof(bytes)) == 0;
    }
    bool SetSendBuffer(int bytes)
    {
        return setsockopt(sock_, SOL_SOCKET, SO_SNDBUF, (const char*)&bytes, sizeof(bytes)) == 0;
    }

    int SendTo(const char* host, uint16_t port, const void* data, size_t len)
    {
        sockaddr_in a{};
        a.sin_family = AF_INET;
        a.sin_port = htons(port);
        if (inet_pton(AF_INET, host, &a.sin_addr) != 1) return -1;
        return sendto(sock_, (const char*)data, (int)len, 0, (sockaddr*)&a, sizeof(a));
    }

    int SendToAddr(const sockaddr_in& to, const void* data, size_t len)
    {
        return sendto(sock_, (const char*)data, (int)len, 0, (const sockaddr*)&to, sizeof(to));
    }

    int RecvFrom(void* buf, size_t cap, sockaddr_in* from = nullptr)
    {
        int fl = (int)sizeof(sockaddr_in);
        return recvfrom(sock_, (char*)buf, (int)cap, 0,
                        from ? (sockaddr*)from : nullptr, from ? &fl : nullptr);
    }

    bool valid() const { return sock_ != INVALID_SOCKET; }

    void Close()
    {
        if (sock_ != INVALID_SOCKET) { closesocket(sock_); sock_ = INVALID_SOCKET; }
    }

    SOCKET handle() const { return sock_; }

private:
    SOCKET sock_ = INVALID_SOCKET;
};

} // namespace zx
