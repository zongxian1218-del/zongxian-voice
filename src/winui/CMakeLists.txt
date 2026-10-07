# 棕仙语音 · WinUI 3 桌面端
#
# 为什么是手写 CMake 而不是用 Visual Studio 的 WinUI 项目模板：
#   1) 本机只装了 Build Tools，没有 IDE，拿不到模板；
#   2) 模板生成的东西不透明，出了编译问题很难定位；
#   3) 手写一遍正好验证「C++/WinRT + XAML 编译 + Windows App SDK」这条链
#      在本机到底能不能通 —— 这正是 W0 要回答的问题。
#
# 关键技术点（这三条是 WinUI 3 C++ 项目最容易卡住的地方）：
#   · .xaml 需要经过 XamlCompiler 生成 .g.hpp/.xbf，再用 cppwinrt.exe 生成
#     投影头，最后才轮到 cl.exe 编译 —— 普通 CMake 不知道这些步骤，
#     所以这里直接 include WindowsAppSDK 提供的 CMake 支持文件。
#   · C++/WinRT 需要 /await 与 /bigobj，且必须开 C++17 或更高。
#   · 部署时需要 Windows App SDK 运行时；开发期用 self-contained 避免装运行时。
cmake_minimum_required(VERSION 3.20)

project(zongxian_voice
    VERSION 0.1.0
    DESCRIPTION "棕仙语音 - Windows 桌面端（WinUI 3）"
    LANGUAGES CXX)

if(NOT WIN32)
    message(FATAL_ERROR "本目标仅支持 Windows。")
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE RelWithDebInfo CACHE STRING "" FORCE)
endif()

# ---------------------------------------------------------------------------
# 拉取 Windows App SDK（NuGet）
#
# WindowsAppSDK 提供了 WinUI 3 的 XAML 控件、窗口、Mica 材质、通知等一切。
# 它不是 MSVC 自带的，必须从 NuGet 取。这里用 CMake 的 NuGet 集成，
# 避免手工下载 nupkg 再解压。
# ---------------------------------------------------------------------------
set(WASDK_VERSION "1.7.250909003" CACHE STRING "Windows App SDK 版本")

include(FetchContent)

# 用 nuget.exe 还原到本地目录：比 FetchContent 的 URL 方式更贴近 .NET 生态，
# 而且 WindowsAppSDK 的包结构里有 tools/ 与 build/ 需要保持完整。
find_program(NUGET_EXE nuget.exe HINTS "${CMAKE_BINARY_DIR}" "$ENV{USERPROFILE}/.nuget")
if(NOT NUGET_EXE)
    message(STATUS "未找到 nuget.exe，正在下载到构建目录…")
    file(DOWNLOAD
        "https://dist.nuget.org/win-x86-commandline/latest/nuget.exe"
        "${CMAKE_BINARY_DIR}/nuget.exe"
        STATUS nuget_dl_status
        SHOW_PROGRESS)
    list(GET nuget_dl_status 0 nuget_dl_code)
    if(NOT nuget_dl_code EQUAL 0)
        message(FATAL_ERROR "nuget.exe 下载失败：${nuget_dl_status}")
    endif()
    set(NUGET_EXE "${CMAKE_BINARY_DIR}/nuget.exe")
endif()
message(STATUS "nuget.exe: ${NUGET_EXE}")

set(NUGET_PACKAGES_DIR "${CMAKE_BINARY_DIR}/packages")
file(MAKE_DIRECTORY "${NUGET_PACKAGES_DIR}")

# 只还原，不修改全局配置
execute_process(
    COMMAND "${NUGET_EXE}" install Microsoft.WindowsAppSDK
            -Version ${WASDK_VERSION}
            -OutputDirectory "${NUGET_PACKAGES_DIR}"
            -NonInteractive -Verbosity quiet
    RESULT_VARIABLE nuget_result
    OUTPUT_VARIABLE nuget_out
    ERROR_VARIABLE nuget_err)
if(NOT nuget_result EQUAL 0)
    message(FATAL_ERROR
        "Windows App SDK ${WASDK_VERSION} 还原失败（退出码 ${nuget_result}）\n"
        "stdout: ${nuget_out}\nstderr: ${nuget_err}\n"
        "排查建议：确认能访问 nuget.org；或改 -DWASDK_VERSION=<其他版本>。")
endif()

set(WASDK_DIR "${NUGET_PACKAGES_DIR}/Microsoft.WindowsAppSDK.${WASDK_VERSION}")
if(NOT EXISTS "${WASDK_DIR}")
    message(FATAL_ERROR "还原后目录不存在：${WASDK_DIR}")
endif()
message(STATUS "Windows App SDK: ${WASDK_DIR}")

# WindowsAppSDK 自带 C++/WinRT 与 XAML 的构建支持
set(WASDK_CPPWINRT_DIR "${WASDK_DIR}/tools")
list(APPEND CMAKE_MODULE_PATH "${WASDK_DIR}/build/native")

# ---------------------------------------------------------------------------
# 源文件
# ---------------------------------------------------------------------------
set(APP_SOURCES
    src/main.cpp
    src/App.cpp
    src/MainWindow.cpp
)

set(APP_XAML
    src/App.xaml
    src/MainWindow.xaml
)

set(APP_HEADERS
    src/App.h
    src/MainWindow.h
    src/pch.h
)

# ---------------------------------------------------------------------------
# 可执行目标
# ---------------------------------------------------------------------------
add_executable(zongxian_voice WIN32
    ${APP_SOURCES}
    ${APP_XAML}
    ${APP_HEADERS}
)

target_include_directories(zongxian_voice PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
    "${WASDK_DIR}/include"
)

# 生成 C++/WinRT 投影头与 XAML 相关文件需要的路径
target_include_directories(zongxian_voice PRIVATE
    "${CMAKE_CURRENT_BINARY_DIR}/Generated Files"
)

# C++/WinRT 必需的编译选项
target_compile_options(zongxian_voice PRIVATE
    /utf-8                 # 源码含中文
    /await                 # C++/WinRT 协程
    /bigobj                # 生成的投影代码目标文件很大
    /W4
    /permissive-
    /EHsc                  # C++/WinRT 需要标准异常
    /Zc:__cplusplus
    /Zc:twoPhase
    /MP
    /DUNICODE /D_UNICODE
)

target_compile_definitions(zongxian_voice PRIVATE
    NOMINMAX
    WIN32_LEAN_AND_MEAN
    _CRT_SECURE_NO_WARNINGS
    # 让 WindowsAppSDK 以自包含方式部署，开发期不必先装运行时
    WINDOWSAPPSDK_SELF_CONTAINED
)

# 链接 WindowsAppSDK 与 WinUI
target_link_libraries(zongxian_voice PRIVATE
    WindowsApp.lib
    Microsoft.WindowsAppRuntime.Bootstrap.lib
)

# 消息里把关键路径打出来，构建失败时便于核对
message(STATUS "源文件      : ${APP_SOURCES}")
message(STATUS "XAML        : ${APP_XAML}")
message(STATUS "构建类型    : ${CMAKE_BUILD_TYPE}")
message(STATUS "编译器      : ${CMAKE_CXX_COMPILER_ID} ${CMAKE_CXX_COMPILER_VERSION}")
message(STATUS "输出        : ${CMAKE_BINARY_DIR}/bin")
