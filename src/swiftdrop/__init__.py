"""棕仙的传输软件 —— 朋友间大文件传输 + 文件夹同步（Windows 桌面端，纯标准库）。

（源码包名仍是 ``swiftdrop``，协议常量与握手字段一律不变。）

模块划分::

    swiftdrop.protocol   帧协议 / 握手 / 校验 / 断点续传状态
    swiftdrop.discovery  UDP 广播设备发现
    swiftdrop.transfer   发送端（多流并发）+ 接收端
    swiftdrop.sync       目录清单 / 差异计算 / 同步与监控
    swiftdrop.signaling  局域网 WebSocket 信令中继
    swiftdrop.webhost    局域网静态文件服务
    swiftdrop.autostart  开机自启 + 同步文件夹登记 + autosync 守护进程
    swiftdrop.foldericon 同步文件夹的 desktop.ini 图标标记
    swiftdrop.gui        tkinter 图形界面（可选）
    swiftdrop.cli        命令行入口

入口: ``python -m swiftdrop``
"""

__all__ = [
    "protocol",
    "discovery",
    "transfer",
    "sync",
    "signaling",
    "webhost",
    "autostart",
    "foldericon",
    "cli",
    "gui",
]

#: 用户可见的产品名（协议字段、包名、注册表以外的标识都不要用它）
APP_NAME = "棕仙的传输软件"
VERSION = "1.0"
MAGIC = "SWIFTDROP1"
