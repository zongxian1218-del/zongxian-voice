#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从界面截图里解码二维码，验证"取件码 → qr.js → 画布 → 可扫"这条链路。
用法：python build/check-qr-from-shot.py build/shots/02-code.png
"""
import sys
import os


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else 'build/shots/02-code.png'
    results = []
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode as zbar_decode
        img = Image.open(path)
        for r in zbar_decode(img):
            results.append(('pyzbar', r.data.decode('utf-8', 'replace')))
    except Exception as e:
        results.append(('pyzbar', 'ERR ' + str(e)))
    try:
        import cv2
        img2 = cv2.imread(path)
        det = cv2.QRCodeDetector()
        data, pts, _ = det.detectAndDecode(img2)
        results.append(('opencv', data if data else '(未检出)'))
    except Exception as e:
        results.append(('opencv', 'ERR ' + str(e)))
    try:
        import zxingcpp
        from PIL import Image
        rs = zxingcpp.read_barcodes(Image.open(path))
        results.append(('zxingcpp', rs[0].text if rs else '(未检出)'))
    except Exception as e:
        results.append(('zxingcpp', 'ERR ' + str(e)))

    print('图片：%s' % path)
    codes = []
    for name, text in results:
        print('  %-9s -> %s' % (name, text))
        if text and not text.startswith('ERR') and text != '(未检出)':
            codes.append(text)
    if not codes:
        print('结果：FAIL 没有解码出任何内容')
        return 1
    if len(set(codes)) == 1 and '#c=' in codes[0]:
        print('结果：PASS 二维码可被扫出，且内容就是带取件码的链接')
        return 0
    print('结果：WARN 解出了内容但不是预期的取件码链接')
    return 1


if __name__ == '__main__':
    sys.exit(main())
