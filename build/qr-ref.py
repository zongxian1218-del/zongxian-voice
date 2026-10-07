#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""参考矩阵导出器：用 qrcode 库按 byte mode 生成同字符串同 EC 等级的矩阵。

用法:  python qr-ref.py <cases.json> <out.json>

cases.json : [{"id": "...", "text": "...", "ec": "M"}, ...]
out.json   : [{"id", "ok", "version", "size", "mask", "rows": ["0101..", ..]}, ...]

强制 byte mode（MODE_8BIT_BYTE），border=0，mask_pattern=None（由库自动选最优掩码）。
"""
import json
import sys

import qrcode
from qrcode.constants import (
    ERROR_CORRECT_L,
    ERROR_CORRECT_M,
    ERROR_CORRECT_Q,
    ERROR_CORRECT_H,
)
from qrcode.util import MODE_8BIT_BYTE, QRData

EC_MAP = {
    "L": ERROR_CORRECT_L,
    "M": ERROR_CORRECT_M,
    "Q": ERROR_CORRECT_Q,
    "H": ERROR_CORRECT_H,
}


def read_format_info(modules):
    """从最终矩阵里读回格式信息，反推 EC 等级与掩码号（独立校验用）。"""
    count = len(modules)
    bits = 0
    for i in range(15):
        if i < 6:
            bit = modules[i][8]
        elif i < 8:
            bit = modules[i + 1][8]
        else:
            bit = modules[count - 15 + i][8]
        if bit:
            bits |= 1 << i
    data = (bits ^ 0x5412) >> 10
    return {"ec_bits": (data >> 3) & 3, "mask": data & 7}


def build(text, ec):
    qr = qrcode.QRCode(
        version=None,
        error_correction=EC_MAP[ec],
        border=0,
        mask_pattern=None,
    )
    qr.add_data(
        QRData(text.encode("utf-8"), mode=MODE_8BIT_BYTE, check_data=False)
    )
    qr.make(fit=True)

    final = [list(row) for row in qr.modules]
    mask_from_lib = qr.best_mask_pattern()  # 确定性，可安全复算
    info = read_format_info(final)

    return {
        "version": qr.version,
        "size": len(final),
        "mask": info["mask"],
        "mask_from_lib": mask_from_lib,
        "ec_bits": info["ec_bits"],
        "rows": ["".join("1" if cell else "0" for cell in row) for row in final],
    }


def main():
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        cases = json.load(fh)

    results = []
    for case in cases:
        try:
            r = build(case["text"], case["ec"])
            r["id"] = case["id"]
            r["ok"] = True
        except Exception as exc:  # noqa: BLE001 - 任何失败都如实上报
            r = {
                "id": case["id"],
                "ok": False,
                "error": "%s: %s" % (type(exc).__name__, exc),
            }
        results.append(r)

    with open(sys.argv[2], "w", encoding="utf-8") as fh:
        json.dump(results, fh)

    ok = sum(1 for r in results if r["ok"])
    print("qr-ref: %d/%d cases exported" % (ok, len(results)))
    for r in results:
        if not r["ok"]:
            print("  ref failed:", r["id"], r["error"])


if __name__ == "__main__":
    main()
