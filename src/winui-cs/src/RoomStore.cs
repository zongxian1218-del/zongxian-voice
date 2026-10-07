// ===========================================================================
// RoomStore.cs —— 房间列表（产品化：真正能用的"多房间"）
//
// 【为什么需要它】用户原话："要连自己的房间得连自己的 IP，很别扭"。
// 现状：只有一个"当前房间"（`--room` / 设置里的一个输入框），改完还要重启才生效；
// 界面里还有**两个**「联机 / 换房间」按钮 + 一个混装了"起名字/选房间/填地址"的浮层。
//
// 本类负责：
//   · 记住**多个**房间（名字 + 信令地址 + 房间名），持久化到 app-config.json；
//   · 增 / 删 / 改 / 排序，以及"当前选中哪一个"；
//   · 让 UI 只依赖这一个数据源（左栏列表 = 房间列表）。
//
// 【不负责什么】不做网络连接 —— 连接仍由页面的 zxEngine.join({signalUrl, room}) 完成。
// 本类只回答"有哪些房间、当前选的是哪个、上一台主机地址是什么"。
// ===========================================================================

using System.Text.Json;
using System.Text.Json.Serialization;

namespace ZongxianVoice;

/// <summary>一个房间条目：名字 + 信令地址（主机）+ 房间名。</summary>
public sealed class RoomEntry
{
    /// <summary>稳定 id（改名不影响选中；用 Guid 避免重名冲突）。</summary>
    public string Id { get; set; } = Guid.NewGuid().ToString("N")[..8];

    /// <summary>显示名（用户可改；空则用房间名兜底）。</summary>
    public string Label { get; set; } = "";

    /// <summary>信令地址：`ip:端口` 或 `ws://…/signal`。空 = 本机当房主。</summary>
    public string SignalAddress { get; set; } = "";

    /// <summary>房间名（双方必须一致）。</summary>
    public string Room { get; set; } = "default";

    /// <summary>是否本机当房主（地址为空时按房主处理）。</summary>
    [JsonIgnore]
    public bool IsHost => SignalAddress.Trim().Length == 0;

    /// <summary>列表里显示的文字：优先 Label，其次房间名，最后地址。</summary>
    [JsonIgnore]
    public string Display =>
        (Label.Trim().Length > 0 ? Label.Trim()
         : Room.Trim().Length > 0 ? Room.Trim() : "未命名房间");

    /// <summary>副标题：主机地址（房主显示"本机"）。</summary>
    [JsonIgnore]
    public string Subtitle => IsHost ? "本机（房主）" : SignalAddress;
}

/// <summary>房间列表 + 当前选中项（持久化在 app-config.json 里）。</summary>
public sealed class RoomStore
{
    /// <summary>所有房间（顺序 = 界面顺序）。</summary>
    public List<RoomEntry> Rooms { get; set; } = new();

    /// <summary>当前选中房间的 id（空 = 没选）。</summary>
    public string CurrentId { get; set; } = "";

    /// <summary>上次成功连接的主机地址（用于"重新连接"与状态显示）。</summary>
    public string LastConnectedAddress { get; set; } = "";

    [JsonIgnore]
    public RoomEntry? Current => Rooms.FirstOrDefault(r => r.Id == CurrentId);

    /// <summary>确保至少有一个房间（首次启动时创建"本机房主"这一项）。</summary>
    public RoomEntry EnsureDefault(string roomName)
    {
        if (Rooms.Count == 0)
        {
            var r = new RoomEntry { Label = "我的房间", Room = roomName, SignalAddress = "" };
            Rooms.Add(r);
            CurrentId = r.Id;
        }
        if (Current is null && Rooms.Count > 0) CurrentId = Rooms[0].Id;
        return Current ?? Rooms[0];
    }

    /// <summary>加一个房间并选为当前（同 id 已存在则只切换）。</summary>
    public RoomEntry Add(string label, string address, string room)
    {
        var same = Rooms.FirstOrDefault(r =>
            string.Equals(r.SignalAddress.Trim(), address.Trim(), StringComparison.OrdinalIgnoreCase)
            && string.Equals(r.Room.Trim(), room.Trim(), StringComparison.OrdinalIgnoreCase));
        if (same is not null)
        {
            CurrentId = same.Id;                       // 已在列表里 → 只切换，不重复添加
            return same;
        }
        var e = new RoomEntry { Label = label, SignalAddress = address, Room = room };
        Rooms.Add(e);
        CurrentId = e.Id;
        return e;
    }

    public bool Remove(string id)
    {
        var e = Rooms.FirstOrDefault(r => r.Id == id);
        if (e is null) return false;
        Rooms.Remove(e);
        if (CurrentId == id) CurrentId = Rooms.Count > 0 ? Rooms[0].Id : "";
        return true;
    }

    /// <summary>改名（空字符串 = 恢复用房间名兜底）。</summary>
    public bool Rename(string id, string label)
    {
        var e = Rooms.FirstOrDefault(r => r.Id == id);
        if (e is null) return false;
        e.Label = label.Trim();
        return true;
    }
}
