"""抓屏幕指定区域存 PNG，并给出客观亮度统计。

用法: python build/grab-region.py <x> <y> <w> <h> <out.png>

用途：验证"画面到底显示出来了没有"。
踩过的坑：只看日志'已解码 N 帧'会得出错误结论 —— 窗口明明没显示，日志一样在刷。
真正的判据是抓图后的亮度统计：XAML 视频区背景是 #FF0A0A0A，被抓到就说明覆盖窗口没盖上去
（实测 mean≈10.3 / 非黑≈0.6%），画面真出来时非黑率会到 80%~90%。

注意：中文路径不要用 `python -c`（PowerShell→Python 传参会按代码页损坏），
一律写成脚本文件再用 `python 脚本.py` 调用。见 docs/handoff-next-session.md 7.4。
"""
import sys
from PIL import ImageGrab, ImageStat

x, y, w, h, out = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
img.save(out)

gray = img.convert("L")
stat = ImageStat.Stat(gray)
hist = gray.histogram()
total = w * h
nonblack = sum(hist[16:]) / total          # 亮度 >= 16 的像素占比
dark = sum(hist[:16]) / total
print(f"out={out} size={img.size} mean={stat.mean[0]:.2f} stddev={stat.stddev[0]:.2f}")
print(f"nonblack(>=16)={nonblack*100:.1f}%  dark(<16)={dark*100:.1f}%")
