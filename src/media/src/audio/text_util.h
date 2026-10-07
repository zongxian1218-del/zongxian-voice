// audio/text_util.h —— UTF-8 ↔ UTF-16 转换
//
// 中文用户名、中文目录名在这台机器上就是现实（工作目录本身就是 D:\文档\ai001）。
// 用 ANSI 版 API（fopen / GetModuleFileNameA）会直接失败或乱码，
// 所以全引擎统一按 UTF-8 存字符串，只在边界处转宽字符。
#pragma once

#include <string>

namespace zx {

std::wstring Utf8ToWide(const std::string& s);
std::string  WideToUtf8(const wchar_t* s, int len = -1);

}  // namespace zx
