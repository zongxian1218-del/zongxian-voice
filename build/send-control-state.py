"""给查看端（receiver-probe）发一个「被控端授权状态」包，或一个可指定 sessionId 的媒体包。

用途（确定性地验证查看端的授权状态机，不依赖真被控端的时序）：
  · state=2/3：模拟被控端"已拒绝/已停止" → 查看端应进入"不再发送输入"
  · media      ：发一个 sessionId 不同的媒体包 → 查看端应检测到"新会话"并复位授权状态

用法:
    python build/send-control-state.py state <端口> <状态 0..3>
    python build/send-control-state.py media <端口> <sessionId 十六进制或十进制>

查看端的两个端口：41001 = rx（媒体），41002 = reqSock（控制状态）。
"""
import socket
import struct
import sys

KHEADER = 32


def send_state(port: int, st: int) -> None:
    p = bytearray(KHEADER + 1)
    struct.pack_into("<BBBBB", p, 0, 0x5A, 1, 4, 0, 0)   # magic, ver, type=4(ControlState)
    struct.pack_into("<H", p, 5, 0x1234)                  # sessionId
    struct.pack_into("<H", p, 28, 1)                      # payloadSize = 1
    p[KHEADER] = st
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(bytes(p), ("127.0.0.1", port))
    print(f"sent control-state={st} -> 127.0.0.1:{port}")


def send_media(port: int, session_id: int) -> None:
    """一个自成一帧的媒体包（fragCount=1）。载荷是垃圾，解码器会报错但无害 ——
    这里只为触发「会话身份变化」这条路径。"""
    payload = b"\x00\x00\x00\x01\x09\xf0" + b"\x00" * 10   # 假的 Annex-B 头 + 垃圾
    p = bytearray(KHEADER + len(payload))
    struct.pack_into("<BBBBB", p, 0, 0x5A, 1, 0, 0, 0)   # type=0(Media)
    struct.pack_into("<H", p, 5, session_id & 0xFFFF)
    struct.pack_into("<I", p, 8, 1)                      # frameId=1
    struct.pack_into("<HH", p, 12, 0, 1)                 # fragIndex=0 fragCount=1
    struct.pack_into("<I", p, 24, len(payload))          # totalFrameBytes
    struct.pack_into("<H", p, 28, len(payload))          # payloadSize
    p[KHEADER:] = payload
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(bytes(p), ("127.0.0.1", port))
    print(f"sent media sessionId=0x{session_id & 0xFFFF:04X}（{len(payload)} 字节载荷）"
          f" -> 127.0.0.1:{port}")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(2)
    what, port_s, arg = sys.argv[1].lower(), sys.argv[2], sys.argv[3]
    if what == "state":
        send_state(int(port_s), int(arg, 0))
    elif what == "media":
        send_media(int(port_s), int(arg, 0))
    else:
        print(f"未知类型: {what}")
        sys.exit(2)
