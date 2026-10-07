# 同频 媒体引擎
#
# 刻意保持「没有第三方依赖」：所有需要的库都来自 Windows SDK 自带的那几个。
# 这样任何人 clone 下来配好 MSVC 就能直接构建，不需要包管理器、不需要
# 联网拉依赖。多出来的便利远大于自己写几十行 FFT/JSON 的成本。
cmake_minimum_required(VERSION 3.20)

project(zongxian_media
    VERSION 0.1.0
    DESCRIPTION "同频 媒体引擎（WASAPI 采集 / 降噪 / 屏幕共享 / 应用音频）"
    LANGUAGES CXX)

if(NOT WIN32)
    message(FATAL_ERROR "当前只实现了 Windows 后端（WASAPI）。其他平台请先补 audio/ 下的后端实现。")
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# 默认 Release：音频代码在 Debug 下会因为边界检查而抖动，测延迟必须用 Release
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

if(MSVC)
    # /utf-8 是必须的：源码里有大量中文注释、日志和错误信息，
    # 不加这个 MSVC 会按本地代码页解析源文件，中文会变成乱码甚至编译错误。
    add_compile_options(
        /utf-8
        /W4                    # 开高警告级别，音频 + COM 代码的坑大多能被警告抓到
        /permissive-           # 严格标准一致性
        /Zc:__cplusplus        # 让 __cplusplus 报告真实值而不是 199711L
        /MP                    # 多进程编译，构建快很多
    )
    add_compile_definitions(
        UNICODE _UNICODE       # 一律用宽字符 API，避免中文路径出问题
        NOMINMAX               # 防止 windows.h 的 min/max 宏污染 std::min/max
        WIN32_LEAN_AND_MEAN
        _CRT_SECURE_NO_WARNINGS
    )
endif()

# ---------------------------------------------------------------------------
# 引擎核心（静态库）
#
# 分成静态库 + 两个可执行目标（DLL 和 CLI 探针），好处是 CLI 探针能
# 独立跑，不需要先经过 Python —— 调音频的时候，「能直接用命令行测」
# 比「改 Python 再跑」快一个数量级。
# ---------------------------------------------------------------------------
add_library(zx_media_core STATIC
    src/audio/engine.cpp
    src/audio/ring_buffer.cpp
    src/audio/fft.cpp
    src/audio/audio_sink.cpp
    src/audio/noise_suppress.cpp
    src/audio/text_util.cpp
    src/audio/wasapi_backend.cpp
    src/json_util.cpp
    src/media_api.cpp
)

target_include_directories(zx_media_core
    PUBLIC  ${CMAKE_CURRENT_SOURCE_DIR}/include
    PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src
)

# Windows SDK 自带的库。avrt = MMCSS（音频线程优先级），
# ole32 = COM。
# mmdevapi 必须链：ActivateAudioInterfaceAsync（进程回环的入口）在它里面，
#   不在 ole32 里 —— 少了它只会得到一个"无法解析的外部符号"链接错误。
# uuid = 接口 IID 与 PKEY_* 的 GUID 定义（所以不需要 initguid.h）。
target_link_libraries(zx_media_core PUBLIC
    ole32
    oleaut32
    uuid
    avrt
    mmdevapi      # ActivateAudioInterfaceAsync / IMMDeviceEnumerator
    propsys       # IPropertyStore / PKEY_Device_FriendlyName
    mfplat        # 后续视频编码要用，先链上避免以后再改构建脚本
    mf
    mfuuid
    dxgi
    d3d11
)

# 静态库必须编成位置无关代码：要把它链进 DLL
set_target_properties(zx_media_core PROPERTIES POSITION_INDEPENDENT_CODE ON)

# ---------------------------------------------------------------------------
# 动态库：Python 通过 ctypes 加载它
# ---------------------------------------------------------------------------
add_library(zongxian_media SHARED
    src/dll_main.cpp
)
target_link_libraries(zongxian_media PRIVATE zx_media_core)

set_target_properties(zongxian_media PROPERTIES
    OUTPUT_NAME "zongxian_media"
    PREFIX ""                 # Windows 上不要 lib 前缀
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
)

# ---------------------------------------------------------------------------
# 命令行探针：PL0 的验收工具
#
# 用法见 tools/zxprobe.cpp 顶部的注释。它做三件事：
#   1) 列举音频设备，确认 WASAPI 通路是活的
#   2) 录一段音频到 WAV，验证 10ms 周期稳不稳
#   3) 同一段音频出「降噪前/降噪后」两份 WAV，供人耳 + 客观指标对比
# ---------------------------------------------------------------------------
add_executable(zxprobe tools/zxprobe.cpp)
target_link_libraries(zxprobe PRIVATE zx_media_core)

set_target_properties(zxprobe PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
)

# ---------------------------------------------------------------------------
# 降噪单元测试
#
# 它不碰设备、不碰线程、不碰 COM，只压 NoiseSuppressor 本身。
# 立体声 + 降噪曾经在集成测试里表现为访问冲突，用这个测试几分钟就定位到
# 是模块内部越界，而不是设备或线程问题。改动降噪前后都应该先跑它。
# ---------------------------------------------------------------------------
add_executable(nstest tools/nstest.cpp)
target_link_libraries(nstest PRIVATE zx_media_core)
# 单元测试要直接包含引擎内部头文件（audio/fft.h 等），
# 而 zx_media_core 把 src/ 目录设为 PRIVATE，不会传递过来。
target_include_directories(nstest PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)

set_target_properties(nstest PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
)

message(STATUS "构建类型     : ${CMAKE_BUILD_TYPE}")
message(STATUS "输出目录     : ${CMAKE_BINARY_DIR}/bin")
message(STATUS "编译器       : ${CMAKE_CXX_COMPILER_ID} ${CMAKE_CXX_COMPILER_VERSION}")
