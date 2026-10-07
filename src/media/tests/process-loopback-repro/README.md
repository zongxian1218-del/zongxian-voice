# 进程回环最小复现（根因铁证）

2026-10-06 用它推翻了"系统 API 崩溃"的旧结论。

## 怎么用

    cd src\media\tests\process-loopback-repro
    cmd /c build.cmd
    .\loopback-min.exe

## 预期输出（能跑通 = API 正常）

    [0] CoInitializeEx(MTA) hr=0x00000000
    [1] 参数就绪 (pid=...)
    [2] handler 就绪
    [3] 调 ActivateAudioInterfaceAsync ...
    [4] 已返回 hr=0x00000000
    [cb] ActivateCompleted hrGet=0x00000000 hrActivate=0x00000000
    [5] 正常结束

## 它证明了什么

* 官方写法（**不调** PropVariantClear）→ 激活成功。
* 我们引擎原来的写法（对指向**栈上** params 的 VT_BLOB 调 PropVariantClear
  → CoTaskMemFree(栈指针)）→ 堆损坏 0xC0000374，且崩在激活调用路径内。
* 所以"进程回环一调就崩"不是系统 API 的问题，是那行多余的清理。
  修掉后 dist\zxprobe.exe loopback-probe 返回 supported=1 / 退出码 0。
