"""界面主题常量：配色 / 字体 / 尺寸（纯标准库，不 import tkinter）。

单独放一个模块是为了让 ``gui.py`` 只关心布局与逻辑：想换皮就改这里。
取值目标是 Windows 11 / WinUI 3 的浅色观感——

* 窗口底是**带一点冷调的浅灰**，卡片是纯白，靠 1px 描边 + 1px 底部投影分层；
* 文本分三级：``CLR_TEXT``（主）/ ``CLR_TEXT_DIM``（说明）/ ``CLR_TEXT_MUTE``
  （次要提示），弱化次要信息靠**颜色**而不是缩小字号；
* 状态色语义固定：忙=主色蓝，完成=绿，失败=红，等待=浅灰蓝。

所有尺寸都是 96 DPI 下的逻辑像素；字号是磅值，Tk 会按 DPI 自动缩放。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 调色板
# ---------------------------------------------------------------------------
CLR_WINDOW = "#EFF3F8"          # 窗口底（冷调浅灰，比纯中性灰更清爽）
CLR_SURFACE = "#FFFFFF"         # 卡片 / 列表底
CLR_SURFACE_ALT = "#F7F9FC"     # 次级表面（内嵌面板）
CLR_PANEL = "#F8FAFC"           # 曲线图 / 内嵌小面板底
CLR_BORDER = "#DFE5EE"          # 卡片 1px 描边
CLR_SHADOW = "#D2DAE6"          # 卡片底部 1px 投影（廉价但有效的层次感）
CLR_INPUT_BORDER = "#CBD4E0"    # 输入类控件描边（比卡片描边深一点）

CLR_ACCENT = "#0F6CBD"          # 主色（按钮 / 进度 / 选中）
CLR_ACCENT_HOVER = "#115EA3"
CLR_ACCENT_PRESS = "#0C3B5E"
CLR_ACCENT_SOFT = "#E8F1FB"     # 主色的浅底（徽标 / 选中行）

CLR_BTN = "#FFFFFF"             # 次按钮：白底 + 1px 描边
CLR_BTN_HOVER = "#F2F6FB"
CLR_BTN_PRESS = "#E6EDF6"
CLR_BTN_BORDER = "#C9D2DF"
CLR_BTN_DISABLED = "#F3F6FA"

CLR_TEXT = "#16202B"            # 一级：标题 / 关键数值
CLR_TEXT_DIM = "#566273"        # 二级：说明
CLR_TEXT_MUTE = "#8A94A3"       # 三级：次要 / 占位
CLR_TEXT_INVERT = "#FFFFFF"

CLR_SELECT = "#DCEBFA"          # 列表选中高亮
CLR_ROW_ALT = "#FAFCFE"         # 列表隔行底色（很淡）

CLR_TRACK = "#E3E9F1"           # 进度条轨道
CLR_IDLE = "#B9C4D2"            # 等待态填充
CLR_OK = "#0E7A0D"              # 完成
CLR_WARN = "#A96200"            # 提醒
CLR_ERROR = "#C42B1C"           # 失败

CLR_LOG_BG = "#F7F9FC"          # 日志底：纯白在大屏上偏刺眼，用柔和冷灰
CLR_LOG_FG = "#243040"
CLR_LOG_TS = "#94A0B0"          # 时间戳
CLR_LOG_ERR = "#B02418"         # 报错行

#: 进度条状态 → 填充色（忙碌 / 完成 / 失败 / 等待）
PROGRESS_STATE_COLORS = {
    "idle": CLR_IDLE,
    "busy": CLR_ACCENT,
    "done": CLR_OK,
    "error": CLR_ERROR,
    "warn": CLR_WARN,
}

# 诊断结论 → 颜色（结论码见 gui.DiagSampler._conclude）
CONCLUSION_COLORS = {
    "good": CLR_OK,
    "normal": CLR_TEXT_DIM,
    "disk": CLR_WARN,
    "uplink": CLR_WARN,
    "stream_spread": CLR_WARN,
    "lan_slow": CLR_ERROR,
    "none": CLR_TEXT_MUTE,
}

# ---------------------------------------------------------------------------
# 字体
# ---------------------------------------------------------------------------
FONT_CANDIDATES = (
    "Segoe UI Variable Display",
    "Segoe UI Variable Text",
    "Segoe UI",
    "Microsoft YaHei UI",
    "Microsoft YaHei",
)
MONO_CANDIDATES = ("Cascadia Mono", "Consolas", "Courier New")

FONT_TITLE = 17                 # 产品名
FONT_SUB = 10                   # 副标题
FONT_BODY = 10                  # 正文 / 控件
FONT_SMALL = 9                  # 说明 / 次要
FONT_TINY = 8                   # 诊断卡片那种超小字
FONT_LOG = 9                    # 日志等宽字

# ---------------------------------------------------------------------------
# 间距 / 尺寸
# ---------------------------------------------------------------------------
RADIUS = 3                      # 进度条圆角半径
PROGRESS_H = 6                  # 进度条高度（4px 太细，6px 才有存在感）
PAD_WINDOW = (16, 12, 16, 10)   # 窗口内容区 (左, 上, 右, 下)
CARD_GAP = 8                    # 卡片之间的竖直间距
PAD_CARD_X = 14                 # 卡片内部左右内边距
PAD_HEAD_TOP = 5                # 卡片顶 → 标题
PAD_HEAD_BOT = 5                # 标题 → 内容
PAD_ROW_TOP = 5                 # 内容首行上边距
PAD_ROW_BOT = 6                 # 内容末行下边距
PAD_ROW_Y = 3                   # 卡片内相邻行的间距
CTRL_PAD = (10, 3)              # 普通按钮内边距（决定了整行高度）
CTRL_PAD_ACCENT = (16, 3)       # 主按钮内边距（横向更宽 = 更突出）
CTRL_PAD_SUBTLE = (9, 2)        # 卡片内小按钮
INPUT_PAD = (8, 4)              # 输入框内边距（与按钮同高，行内不歪）
TREE_ROW_H = 30                 # 设备列表行高
LOG_HEIGHT = 5                  # 日志可见行数（1080p 下也能看见日志）


def mix(color: str, other: str, t: float) -> str:
    """把两个 ``#RRGGBB`` 按 ``t`` 混合（0 → color，1 → other）。"""
    t = max(0.0, min(1.0, float(t)))
    try:
        a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    except (ValueError, IndexError):
        return color
    out = [round(x + (y - x) * t) for x, y in zip(a, b)]
    return "#" + "".join(f"{v:02X}" for v in out)


def lighten(color: str, t: float) -> str:
    """向白色靠 ``t``（做高光 / 浅底用）。"""
    return mix(color, "#FFFFFF", t)
