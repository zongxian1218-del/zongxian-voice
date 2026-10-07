"""往被控端（sender-probe）的控制端口发一个远程控制输入包 —— 用于客观验证远程控制。

用法:
    python build/send-input-packet.py <端口> <kind> <nx> <ny> [frameId] [wheel] [vk] [flags]

  kind  : 0=鼠标移动 1=鼠标按键 2=滚轮 3=键盘
  nx,ny : 归一化坐标 0..32767（16384 ≈ 屏幕正中）
  frameId: 可选。被控端只在 |已发帧 - 事件帧| <= 3 时才注入，
           所以给一个很大的值（如 4294967295）等于"最新"，用来验证注入路径；
           给 0 则一定是"陈旧"，用来验证防陈旧点击的拦截。
  vk    : kind=3 时的虚拟键码（如 0x7C=F13、0x41='A'）
  flags : kind=1 时是键位（1=左 2=右 4=中）+ 8=按下；kind=3 时 8=按下、0=抬起

为什么要有这个工具：注入会真的动鼠标/按键，靠"看画面"验证不可靠。
直接构造事件包、再用 GetCursorPos（鼠标）或日志（键盘）验证，才是客观判据。
在受限制的环境里（被拉起的原生 exe 的 SendInput 是空操作），这是唯一能验证
"协议+守卫+注入调用"三层都正确的手段。
"""
import socket
import struct
import sys

KHEADER = 32
K_MAGIC = 0x5A
K_VERSION = 1
K_TYPE_INPUT = 3

def num(i, default=0):
    """解析第 i 个参数；int(x, 0) 让 0x7C 这类十六进制写法也能用。"""
    return int(sys.argv[i], 0) if len(sys.argv) > i else default


port = num(1)
kind = num(2)
nx = num(3)
ny = num(4)
frame_id = num(5, 0xFFFFFFFF)
wheel = num(6)
vk = num(7)
flags = num(8)

pkt = bytearray(KHEADER + 12)

# ---- MediaPacket 头（#pragma pack(1)，32 字节）----
struct.pack_into("<BBBBB", pkt, 0, K_MAGIC, K_VERSION, K_TYPE_INPUT, 0, 0)  # magic/ver/type/flags/streamId
struct.pack_into("<H", pkt, 5, 0x1234)          # sessionId
pkt[7] = 0                                      # reserved0
struct.pack_into("<I", pkt, 8, frame_id)        # frameId ← 陈旧判定的依据
struct.pack_into("<HH", pkt, 12, 0, 0)          # fragIndex / fragCount（输入包不分片）
struct.pack_into("<Q", pkt, 16, 0)              # timestampUs
struct.pack_into("<I", pkt, 24, 0)              # totalFrameBytes
struct.pack_into("<H", pkt, 28, 12)             # payloadSize = sizeof(InputEvent)
struct.pack_into("<H", pkt, 30, 0)              # reserved1

# ---- InputEvent（12 字节）----
struct.pack_into("<BBhhih", pkt, KHEADER, kind, flags, nx, ny, wheel, vk)

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.sendto(bytes(pkt), ("127.0.0.1", port))
print(f"sent: kind={kind} flags={flags} nx={nx} ny={ny} wheel={wheel} vk=0x{vk:02X} "
      f"frameId={frame_id} -> 127.0.0.1:{port}")
