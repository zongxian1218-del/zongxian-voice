/*
FileStore.cs —— 收文件的落盘策略（S2 拆桥，2026-10-06）。

【它负责什么】决定"文件存到哪、重名怎么办、路径安不安全"，并真写盘。
【它不负责什么】不画卡片、不聊天、不碰控件 —— 名字与位置返回给调用方去渲染。

【为什么这些规则要独立成一处】
  · 目录候选顺序（下载目录 → exe\received → 临时目录）是**用户体验**规则：
    用户找不到收到的文件是最糟的体验，所以还要"实际写一个测试文件"来验证可写。
  · 同名不覆盖：绝不冲掉用户已有文件。
  · 路径穿越防护：只取对端文件名的最后一段，`../` 之类写不出去。
这三条都是"一旦被别处随手改写就会出安全问题/丢文件"的规则，集中在这里并配测试。
*/
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace ZongxianVoice;

internal sealed class FileStore
{
    private readonly Action<string> _log;
    public FileStore(Action<string>? log = null) => _log = log ?? (_ => { });

    /// <summary>
    /// 接收入口目录，按优先级挑第一个**真正可写**的：
    ///   1) 用户的「下载」目录（最符合预期）
    ///   2) exe 同目录的 received\（便携、几乎总能写）
    ///   3) 临时目录（兜底，至少不丢文件）
    /// </summary>
    public static string ResolveReceiveDirectory()
    {
        var candidates = new List<string>
        {
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                         "Downloads", "棕仙语音"),
            Path.Combine(AppContext.BaseDirectory, "received"),
            Path.Combine(Path.GetTempPath(), "棕仙语音"),
        };
        foreach (var dir in candidates)
        {
            try
            {
                Directory.CreateDirectory(dir);
                // 光建目录不够：有些目录能建子目录但写不了文件
                var probe = Path.Combine(dir, ".write-test");
                File.WriteAllText(probe, "ok");
                File.Delete(probe);
                return dir;
            }
            catch
            {
                // 换下一个候选
            }
        }
        return candidates[^1];
    }

    /// <summary>
    /// 从对端给的 rawName 求一个**安全且不覆盖**的目标路径。
    /// 只取文件名（挡 `../` 路径穿越），重名自动加 ` (n)`。
    /// </summary>
    public static string ResolveTargetPath(string directory, string rawName)
    {
        var name = Path.GetFileName(rawName ?? "");
        if (string.IsNullOrWhiteSpace(name)) name = "received.bin";
        var target = Path.Combine(directory, name);
        var stem = Path.GetFileNameWithoutExtension(name);
        var ext = Path.GetExtension(name);
        int n = 1;
        while (File.Exists(target))
            target = Path.Combine(directory, $"{stem} ({n++}){ext}");
        return target;
    }

    /// <summary>把 base64 内容落盘；返回 (完整路径, 字节数, 文件名)。失败抛异常，由调用方决定怎么提示。</summary>
    public (string path, int bytes, string name) SaveBase64(string rawName, string base64)
    {
        var bytes = Convert.FromBase64String(base64);
        var dir = ResolveReceiveDirectory();
        var target = ResolveTargetPath(dir, rawName);
        File.WriteAllBytes(target, bytes);
        var saved = Path.GetFileName(target);
        _log($"文件已保存: {target}（{bytes.Length} 字节）");
        return (target, bytes.Length, saved);
    }
}
