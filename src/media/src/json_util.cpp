#include "json_util.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

namespace zx {

namespace {

bool IsSpace(char c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r';
}

// 从 p 开始跳过一个 JSON 字符串（含转义），返回结束引号后的位置。
// 失败返回 npos。
size_t SkipString(const std::string& s, size_t p) {
    if (p >= s.size() || s[p] != '"') return std::string::npos;
    ++p;
    while (p < s.size()) {
        if (s[p] == '\\') {
            p += 2;
            continue;
        }
        if (s[p] == '"') return p + 1;
        ++p;
    }
    return std::string::npos;
}

// 从 p 开始跳过一个值（对象/数组/字符串/数字/字面量），返回其后的位置。
size_t SkipValue(const std::string& s, size_t p) {
    while (p < s.size() && IsSpace(s[p])) ++p;
    if (p >= s.size()) return std::string::npos;
    const char c = s[p];
    if (c == '"') return SkipString(s, p);
    if (c == '{' || c == '[') {
        const char open = c, close = (c == '{') ? '}' : ']';
        int depth = 0;
        while (p < s.size()) {
            const char d = s[p];
            if (d == '"') {
                p = SkipString(s, p);
                if (p == std::string::npos) return p;
                continue;
            }
            if (d == open) ++depth;
            else if (d == close) {
                --depth;
                if (depth == 0) return p + 1;
            }
            ++p;
        }
        return std::string::npos;
    }
    // 数字或 true/false/null
    while (p < s.size() && !IsSpace(s[p]) && s[p] != ',' && s[p] != '}' && s[p] != ']') ++p;
    return p;
}

std::string Unescape(const std::string& raw) {
    std::string out;
    out.reserve(raw.size());
    for (size_t i = 0; i < raw.size(); ++i) {
        if (raw[i] != '\\' || i + 1 >= raw.size()) {
            out += raw[i];
            continue;
        }
        const char n = raw[++i];
        switch (n) {
            case 'n': out += '\n'; break;
            case 'r': out += '\r'; break;
            case 't': out += '\t'; break;
            case 'b': out += '\b'; break;
            case 'f': out += '\f'; break;
            case '"': out += '"'; break;
            case '\\': out += '\\'; break;
            case '/': out += '/'; break;
            case 'u': {
                // 简化处理：\uXXXX 只有当 XXXX < 0x80 时能正确还原，
                // 中文通常走 UTF-8 直传（JsonWriter 就是这么写的），
                // 所以这里够用。真需要完整 \u 支持时再补代理对逻辑。
                if (i + 4 < raw.size()) {
                    char hex[5] = {raw[i + 1], raw[i + 2], raw[i + 3], raw[i + 4], 0};
                    const unsigned cp = static_cast<unsigned>(std::strtoul(hex, nullptr, 16));
                    if (cp < 0x80) {
                        out += static_cast<char>(cp);
                    }
                    i += 4;
                }
                break;
            }
            default: out += n; break;
        }
    }
    return out;
}

}  // namespace

size_t JsonReader::FindValuePos(const char* key) const {
    if (!key) return std::string::npos;
    // 只支持顶层/嵌套里第一次出现的该键。对配置对象（扁平结构）足够。
    const std::string needle = std::string("\"") + key + "\"";
    size_t p = 0;
    while (true) {
        p = text_.find(needle, p);
        if (p == std::string::npos) return p;
        // 确认它确实是「键」而不是某个字符串值的一部分：前面应是 { 或 ,
        size_t q = p;
        while (q > 0 && IsSpace(text_[q - 1])) --q;
        const bool looks_like_key =
            (q == 0) || text_[q - 1] == '{' || text_[q - 1] == ',';
        size_t after = p + needle.size();
        while (after < text_.size() && IsSpace(text_[after])) ++after;
        if (looks_like_key && after < text_.size() && text_[after] == ':') {
            ++after;
            while (after < text_.size() && IsSpace(text_[after])) ++after;
            return after;
        }
        p += needle.size();
    }
}

int64_t JsonReader::GetInt(const char* key, int64_t def) const {
    const size_t p = FindValuePos(key);
    if (p == std::string::npos || p >= text_.size()) return def;
    char* end = nullptr;
    const long long v = std::strtoll(text_.c_str() + p, &end, 10);
    if (end == text_.c_str() + p) return def;
    return static_cast<int64_t>(v);
}

std::string JsonReader::GetString(const char* key, const std::string& def) const {
    const size_t p = FindValuePos(key);
    if (p == std::string::npos || p >= text_.size() || text_[p] != '"') return def;
    const size_t end = SkipString(text_, p);
    if (end == std::string::npos) return def;
    // 去掉两端的引号，再反转义
    return Unescape(text_.substr(p + 1, end - p - 2));
}

bool JsonReader::GetBool(const char* key, bool def) const {
    const size_t p = FindValuePos(key);
    if (p == std::string::npos || p >= text_.size()) return def;
    if (text_.compare(p, 4, "true") == 0) return true;
    if (text_.compare(p, 5, "false") == 0) return false;
    // 也接受 0/1，配置文件手写时更顺手
    if (text_[p] == '1') return true;
    if (text_[p] == '0') return false;
    return def;
}

}  // namespace zx
