#include "text_util.h"

#ifdef _WIN32
#  include <windows.h>
#endif

namespace zx {

std::wstring Utf8ToWide(const std::string& s) {
#ifdef _WIN32
    if (s.empty()) return std::wstring();
    const int need = ::MultiByteToWideChar(CP_UTF8, 0, s.c_str(),
                                          static_cast<int>(s.size()), nullptr, 0);
    if (need <= 0) return std::wstring();
    std::wstring w(static_cast<size_t>(need), L'\0');
    ::MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()),
                          &w[0], need);
    return w;
#else
    return std::wstring(s.begin(), s.end());
#endif
}

std::string WideToUtf8(const wchar_t* s, int len) {
#ifdef _WIN32
    if (!s || (len == 0)) return std::string();
    const int need = ::WideCharToMultiByte(CP_UTF8, 0, s, len, nullptr, 0,
                                           nullptr, nullptr);
    if (need <= 0) return std::string();
    std::string out(static_cast<size_t>(need), '\0');
    ::WideCharToMultiByte(CP_UTF8, 0, s, len, &out[0], need, nullptr, nullptr);
    return out;
#else
    (void)len;
    return std::string();
#endif
}

}  // namespace zx
