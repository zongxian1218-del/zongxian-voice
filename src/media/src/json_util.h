// json_util.h —— 极小的 JSON 读写工具
//
// 为什么不用第三方（nlohmann/json 之类）：
//   · 本项目对外承诺「零第三方依赖」。为了几个几十字节的配置对象引入一个
//     几百 KB 的头文件库，会让构建、审计、打包三件事都变复杂。
//   · 需要写的 JSON 结构是我们自己定的，形状固定且简单。
//   · 需要的解析能力更弱：只读几个已知字段，不需要通用解析器。
//
// 设计要点（写 JSON 的部分）：
//   逗号与嵌套的处理**不用栈**，而是给每一层容器一个自己的 sink 对象。
//   早期版本用一个共享的 vector<bool> 记录"当前容器是否已有元素"，
//   结果数组元素里的对象会把父对象的状态覆盖掉，输出成
//   [{,"id":1"name":"x"}...] 这种非法 JSON。
//   容器各自持有状态之后，这类问题在结构上就不可能发生。
//
// 明确的能力边界（不要拿它当通用 JSON 库用）：
//   · 解析器只支持对象、数组、字符串、数字、true/false/null。
//   · 不做错误恢复：格式错就当字段不存在。所有字段都有默认值，
//     配置文件写坏了应该退化成默认行为，而不是让引擎启动失败。
#pragma once

#include <cstdint>
#include <cstdio>
#include <memory>
#include <string>
#include <vector>

namespace zx {

// ============================ 写 ============================

// 一层容器的写入状态。每层各有一个实例，所以父层的状态不会被子层影响。
class JsonSink {
public:
    virtual ~JsonSink() = default;
    // 写分隔符（第一个元素不写），然后写内容
    virtual void Append(const std::string& text) = 0;
};

class JsonWriter;

namespace detail {

class ObjectSink : public JsonSink {
public:
    explicit ObjectSink(std::string* out) : out_(out) {}
    void Append(const std::string& text) override {
        if (started_) *out_ += ',';
        started_ = true;
        *out_ += text;
    }
    // 对象的 key 也走 Append，这样逗号逻辑只有一处
private:
    std::string* out_;
    bool started_ = false;
};

class ArraySink : public JsonSink {
public:
    explicit ArraySink(std::string* out) : out_(out) {}
    void Append(const std::string& text) override {
        if (started_) *out_ += ',';
        started_ = true;
        *out_ += text;
    }

private:
    std::string* out_;
    bool started_ = false;
};

}  // namespace detail

class JsonWriter {
public:
    JsonWriter() { out_.reserve(512); }

    JsonWriter& BeginObject() {
        OpenContainer('{');
        sinks_.push_back(std::make_shared<detail::ObjectSink>(&out_));
        return *this;
    }
    JsonWriter& EndObject() {
        CloseContainer('}');
        return *this;
    }
    JsonWriter& BeginArray() {
        OpenContainer('[');
        sinks_.push_back(std::make_shared<detail::ArraySink>(&out_));
        return *this;
    }
    JsonWriter& EndArray() {
        CloseContainer(']');
        return *this;
    }

    JsonWriter& Key(const char* k) {
        // key 本身也是一个"元素"，用来驱动逗号
        if (!sinks_.empty()) sinks_.back()->Append(Escape(k ? k : ""));
        // key 后面的值直接追加，不参与逗号逻辑
        out_ += ':';
        in_key_ = true;
        return *this;
    }

    JsonWriter& Value(const char* v) {
        Emit(Escape(v ? v : ""));
        return *this;
    }
    JsonWriter& Value(const std::string& v) { return Value(v.c_str()); }
    JsonWriter& Value(bool v) { Emit(v ? "true" : "false"); return *this; }
    // 整数需要为每个常见宽度各写一个显式重载。
    // 只留 int64_t/uint64_t 会让 Value(1) 这类调用变成"两个都可行"的
    // 二义性（C2668），编译器既不报错也不猜 —— 它直接拒绝编译。
    // 这里全部转发到同一个底层实现，没有任何逻辑重复。
    JsonWriter& Value(int v) { return ValueInt(static_cast<int64_t>(v)); }
    JsonWriter& Value(unsigned int v) { return ValueInt(static_cast<int64_t>(v)); }
    JsonWriter& Value(long v) { return ValueInt(static_cast<int64_t>(v)); }
    JsonWriter& Value(unsigned long v) { return ValueInt(static_cast<int64_t>(v)); }
    JsonWriter& Value(long long v) { return ValueInt(static_cast<int64_t>(v)); }
    JsonWriter& Value(unsigned long long v) { return ValueUint(static_cast<uint64_t>(v)); }
    JsonWriter& Value(double v) {
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%.6g", v);
        Emit(buf);
        return *this;
    }
    // 直接塞一段已经是合法 JSON 的内容
    JsonWriter& Raw(const std::string& json) { Emit(json); return *this; }

    const std::string& str() const { return out_; }

private:
    JsonWriter& ValueInt(int64_t v) { Emit(std::to_string(v)); return *this; }
    JsonWriter& ValueUint(uint64_t v) { Emit(std::to_string(v)); return *this; }

    // 写容器开始符。容器本身也是父容器里的一个元素，要走父容器的逗号逻辑。
    void OpenContainer(char c) {
        Push();
        out_ += c;
    }
    void CloseContainer(char c) {
        if (!sinks_.empty()) sinks_.pop_back();
        out_ += c;
    }

    // 写一个值。若刚刚写了 key（in_key_），则直接追加；
    // 否则作为容器元素走 sink 的逗号逻辑。
    void Emit(const std::string& text) {
        if (in_key_) {
            in_key_ = false;
            out_ += text;
            return;
        }
        Push();
        out_ += text;
    }

    void Push() {
        if (!sinks_.empty()) sinks_.back()->Append("");
        // 注意：Append("") 只负责写逗号，sink 内部会记住"已经有元素了"。
        // 这样分隔符与内容分离，避免了"先写内容再想逗号"的顺序问题。
    }

    static std::string Escape(const char* s) {
        std::string r;
        r += '"';
        for (const char* p = s; *p; ++p) {
            const unsigned char c = static_cast<unsigned char>(*p);
            switch (c) {
                case '"':  r += "\\\""; break;
                case '\\': r += "\\\\"; break;
                case '\n': r += "\\n"; break;
                case '\r': r += "\\r"; break;
                case '\t': r += "\\t"; break;
                case '\b': r += "\\b"; break;
                case '\f': r += "\\f"; break;
                default:
                    if (c < 0x20) {
                        char buf[8];
                        std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                        r += buf;
                    } else {
                        // 中文等 UTF-8 字节直接透传（等价于 ensure_ascii=False），
                        // 抓包与日志里人能直接读懂
                        r += static_cast<char>(c);
                    }
            }
        }
        r += '"';
        return r;
    }

    std::string out_;
    std::vector<std::shared_ptr<JsonSink>> sinks_;
    bool in_key_ = false;
};

// ============================ 读 ============================

// 解析 JSON 字符串里某个键的值。只做够用的解析，失败返回默认值。
class JsonReader {
public:
    explicit JsonReader(const std::string& text) : text_(text) {}

    int64_t GetInt(const char* key, int64_t def) const;
    std::string GetString(const char* key, const std::string& def) const;
    bool GetBool(const char* key, bool def) const;

private:
    size_t FindValuePos(const char* key) const;
    const std::string& text_;
};

}  // namespace zx
