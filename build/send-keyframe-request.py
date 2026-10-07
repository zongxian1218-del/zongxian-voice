"""往被控端控制端口发一个关键帧请求包（type=1）。
用途：验证"接收端 → 发送端"这条控制通道本身通不通（和输入包分开测）。
"""
import socket
import struct
import sys

port = int(sys.argv[1])
frame_id = int(sys.argv[2]) if len(sys.argv) > 2 else 0

pkt = bytearray(32)
struct.pack_into("<BBBBB", pkt, 0, 0x5A, 1, 1, 0, 0)   # magic, ver, type=1(keyframe request), flags, streamId
struct.pack_into("<H", pkt, 5, 0x1234)                 # sessionId
struct.pack_into("<I", pkt, 8, frame_id)               # frameId
struct.pack_into("<H", pkt, 28, 0)                     # payloadSize
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.sendto(bytes(pkt), ("127.0.0.1", port))
print(f"sent keyframe-request frameId={frame_id} -> 127.0.0.1:{port}")
