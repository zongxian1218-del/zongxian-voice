#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""异地组网（虚拟局域网）识别。

Radmin VPN / Tailscale / ZeroTier / Hamachi 这类工具的本质，是给两台异地的机器
各发一个"虚拟局域网 IP"。**只要双方在同一个组网里，本软件的局域网多流 TCP 传输
就能直接跑**——不需要 NAT 打洞、也不需要中继服务器。

所以"异地组网传输"要做的只有三件事：
  1. 把这些网卡和地址认出来（26.x 看着像公网地址，其实是 Radmin 的组网段）；
  2. 把这个地址给出去（复制 / 二维码），让手机或另一台电脑用它连过来；
  3. 按延迟自动加并发流，别让 58ms 的跨网 RTT 把单流 TCP 卡死。

速度的硬上限仍然是**发送方的上行带宽**，组网只解决"连得上、连得稳、能跑满管道"。
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass

from .discovery import all_local_ipv4

#: 各家族网工具的地址特征 + 一句话说明。
#: 同一组网内的两台机器都会落在这些网段里，所以按地址段就能认出来。
PROFILES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("Tailscale", ("100.64.0.0/10",),
     "Tailscale（免费；Windows/安卓/iOS 全平台，WireGuard 直连，跨网速度通常最好）"),
    ("Radmin VPN", ("26.0.0.0/8",),
     "Radmin VPN（免费，Windows；对端也要加入同一个网络）"),
    ("Hamachi", ("25.0.0.0/8",),
     "LogMeIn Hamachi（免费版最多 5 台）"),
    ("ZeroTier", ("10.147.0.0/16", "10.242.0.0/16", "10.144.0.0/16"),
     "ZeroTier（免费；手机也有客户端）"),
    ("蒲公英/向日葵", ("10.168.0.0/16", "10.6.0.0/16"),
     "蒲公英异地组网 / 向日葵（商业方案）"),
)

#: 常见家庭/办公局域网段（用于区分"本地"和"异地"）
LAN_NETS = tuple(ipaddress.ip_network(n) for n in (
    "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12",
))


@dataclass(frozen=True)
class GroupNic:
    """一块"异地组网"虚拟网卡上的地址。"""

    tool: str
    ip: str
    hint: str

    @property
    def url_suffix(self) -> str:
        return self.ip


#: 推荐给用户的组网工具（软件里直接给下载链接，用户不用自己找）
TOOLS: tuple[tuple[str, str, str, str], ...] = (
    ("Tailscale", "https://tailscale.com/download",
     "全平台：Windows / 安卓 / iOS / macOS / Linux",
     "免费；WireGuard 直连，跨网速度通常最好。手机也要参与时选它。"),
    ("Radmin VPN", "https://www.radmin-vpn.com/cn/",
     "仅 Windows",
     "免费；国内速度快、配置极简。只有 Windows 电脑之间互传时选它。"),
    ("ZeroTier", "https://www.zerotier.com/download/",
     "全平台",
     "免费；需要创建网络并填 Network ID，比上面两个多一步。"),
)


def install_lines() -> list[str]:
    """给 CLI / GUI 用的"装哪个、去哪下"清单。"""
    out = ["想跨网络直连，装一个免费组网工具，让两台设备加入同一个网络即可："]
    for name, url, plat, why in TOOLS:
        out.append(f"  · {name}（{plat}）：{url}")
        out.append(f"      {why}")
    out.append("装好后运行 `棕仙的传输软件.exe group` 就能看到组网地址，把它给对方即可。")
    return out


def classify(ip: str) -> str:
    """返回 'loopback' / 'group' / 'lan' / 'public'。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "public"
    if addr.is_loopback:
        return "loopback"
    for _tool, nets, _hint in PROFILES:
        for net in nets:
            try:
                if addr in ipaddress.ip_network(net):
                    return "group"
            except ValueError:
                continue
    for net in LAN_NETS:
        if addr in net:
            return "lan"
    return "public"


def tool_of(ip: str) -> tuple[str, str]:
    """地址 → (工具名, 说明)；不是组网地址就返回 ('', '')。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "", ""
    for tool, nets, hint in PROFILES:
        for net in nets:
            try:
                if addr in ipaddress.ip_network(net):
                    return tool, hint
            except ValueError:
                continue
    return "", ""


def group_nics() -> list[GroupNic]:
    """本机所有"异地组网"地址（按 Tailscale → Radmin → … 的优先级）。"""
    out: list[GroupNic] = []
    seen: set[str] = set()
    order = {tool: i for i, (tool, _n, _h) in enumerate(PROFILES)}
    for ip in all_local_ipv4():
        if ip in seen:
            continue
        tool, hint = tool_of(ip)
        if not tool:
            continue
        seen.add(ip)
        out.append(GroupNic(tool=tool, ip=ip, hint=hint))
    out.sort(key=lambda g: order.get(g.tool, 99))
    return out


def primary_group() -> GroupNic | None:
    """优先用来分享的那一个组网地址。"""
    nics = group_nics()
    return nics[0] if nics else None


def lan_ips() -> list[str]:
    """普通局域网地址（不含组网网段）。"""
    return [ip for ip in all_local_ipv4() if classify(ip) == "lan"]


def share_url(ip: str, port: int, page: str = "swiftdrop.html") -> str:
    return f"http://{ip}:{port}/{page}"


def summary() -> str:
    """给 CLI / GUI 用的一小段人话说明。"""
    nics = group_nics()
    if not nics:
        return ("没检测到异地组网网卡。装一个（推荐 Tailscale，免费全平台）并让两台设备加入同一个网络，"
                "之后本软件就能像局域网一样跨网直传。")
    lines = []
    for n in nics:
        lines.append(f"{n.tool}：{n.ip}")
    lines.append("把上面的地址给对方（或让对方加入同一个组网），就能像局域网一样多流直传。")
    return "\n".join(lines)


def best_streams(host: str, port: int, *, timeout: float = 3.0) -> int:
    """按到目标的 TCP 建连耗时（RTT 代理）选择并发流数。

    跨网高延迟链路上，单条 TCP 的窗口填不满管道；多开几条流能明显提速。
    局域网（<5ms）4 条就够，跨网给到 6~8 条。
    """
    import time

    t0 = time.monotonic()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
    except OSError:
        return 4
    rtt_ms = (time.monotonic() - t0) * 1000.0
    if rtt_ms < 5:
        return 4
    if rtt_ms < 30:
        return 6
    if rtt_ms < 80:
        return 8
    return 8
