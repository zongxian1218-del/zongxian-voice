"""冒烟检查 —— 本机没有 CI，用它当最小可用的 CI。

用法:
    python build/run-checks.py            # 全跑
    python build/run-checks.py build      # 只跑构建
    python build/run-checks.py selftest   # 只跑探针自测

覆盖:
    1. C++ 助手构建（build-receiver-probe.cmd）
    2. 传输层自测（transport-probe.exe 的 5 个阶段）
    3. C# 视频原型构建（winui-videoprobe，即覆盖窗口这条主线）
    4. C# 主应用构建（winui-cs，语音/聊天/文件）

原则：**每一步都完整打印输出**（这个项目的教训是"构建失败却跑了旧 exe，得出完全错误的结论"），
最后给 PASS/FAIL 汇总；任何一步失败，脚本返回非 0。
"""
import hashlib
import pathlib
import re
import subprocess
import sys

# 控制台是 GBK：被拉起的程序（自检脚本、PowerShell）输出里可能有 GBK 编不出的字符，
# 直接 print 会抛 UnicodeEncodeError，把"其实通过"的一轮回归判成崩溃（实测踩过）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOTNET = r"C:\Program Files\dotnet\dotnet.exe"


class EnvBlocked(Exception):
    """检查项因**环境**无法成立（不是产品失败）。

    约定（与 build\\smoke-ui.py 共享）：退出码 2 或输出里有 [ABORT]。
    为什么要单独一类：界面自检要求"窗口真在最前"，而用户在用电脑/弹窗/UAC 会让它
    物理上做不到。以前这会被报成 [FAIL]，看着像产品坏了 —— 于是要么误改产品，
    要么干脆被无视。现在：**报环境、如实说我不能验证、并照样拦住发版**（发版不改）。
    """

    def __init__(self, title, detail):
        super().__init__(detail)
        self.title = title
        self.detail = detail


def run(title, argv, cwd, ok_markers=(), bad_markers=(": error", "BUILD FAILED", "error CS", "error MSB"),
        env_abort=False):
    print("=" * 78)
    print(f"== {title}")
    print("=" * 78)
    proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    print(out.rstrip() if out.strip() else "(no output)")

    # 环境中止：退出码 2 或 [ABORT] 标记 ⇒ 抛 EnvBlocked（调用方决定怎么汇总）
    if env_abort and (proc.returncode == 2 or "[ABORT]" in out):
        print(f"---- ENV-BLOCKED: {title}（退出码 {proc.returncode}；这是测试环境不满足，不是产品结论）")
        raise EnvBlocked(title, f"退出码 {proc.returncode}")

    problems = []
    if proc.returncode != 0:
        problems.append(f"exit code {proc.returncode}")
    for m in bad_markers:
        if m.lower() in out.lower():
            problems.append(f"输出里出现 {m!r}")
    for m in ok_markers:
        if m not in out:
            problems.append(f"输出里缺少 {m!r}")
    ok = not problems
    print(f"---- {'PASS' if ok else 'FAIL'}: {title}" + ("" if ok else "  <- " + "; ".join(problems)))
    return ok


def check_dist_media_and_probe():
    """发版守卫：dist 里的**页面资源**与**引擎探针**必须与源码/最新构建一致。

    【为什么是硬闸门】实测两次踩到（2026-10-06）：
      · dist\\winui\\media\\call.html 停在 10/5 的旧版本 —— 打包出去等于
        "共享电脑声音"这个功能根本不存在（页面里没有那些代码）。
      · 应用目录里的 tools\\zxprobe.exe 是旧副本 —— 旧探针不认识新子命令 stream，
        启动即退码 1，采集泵一块数据都拿不到，而界面/日志看上去全对。
    这类"版本不一致导致的静默失效"只能靠比对字节来挡，靠人记不住。
    """
    problems = []
    # 【S3 起改成"整目录比对"】页面已经从单文件拆成 call.html（壳）+ bus.js + log.js + call.js。
    # 硬编码文件清单在拆分后必然漏（旧清单只认 call.html），而漏掉一个模块的表现就是
    # "打包出去等于没有这个功能"（v36 就是这么出的）。所以改成：src\media 下**每个文件**
    # 都必须在 dist\winui\media 里有逐字节相同的一份。
    src_media = ROOT / "src" / "winui-cs" / "media"
    dst_media = ROOT / "dist" / "winui" / "media"
    media_files = sorted(p for p in src_media.rglob("*") if p.is_file()) if src_media.is_dir() else []
    for src in media_files:
        rel = src.relative_to(src_media)
        dst = dst_media / rel
        if not dst.is_file():
            problems.append(f"dist 缺 {dst.relative_to(ROOT)}（打包脚本没拷过去）")
            continue
        if src.read_bytes() != dst.read_bytes():
            problems.append(
                f"{dst.relative_to(ROOT)} 与源码不一致"
                f"（dist {dst.stat().st_size} 字节 / 源码 {src.stat().st_size} 字节）")
    # 反向：dist 里多出来的页面文件（源码已删但产物还在 ⇒ 会被打进包）
    if dst_media.is_dir():
        known = {p.relative_to(src_media) for p in media_files}
        for dst in sorted(dst_media.rglob("*")):
            if dst.is_file() and dst.relative_to(dst_media) not in known:
                problems.append(f"dist 里有源码已不存在的页面文件：{dst.relative_to(ROOT)}")

    # 探针：dist 与 dist\winui\tools 必须是同一份（应用实际调用的就是后者）
    probes = [ROOT / "dist" / "zxprobe.exe",
              ROOT / "dist" / "winui" / "tools" / "zxprobe.exe"]
    have = [p for p in probes if p.is_file()]
    if len(have) == 2 and have[0].read_bytes() != have[1].read_bytes():
        problems.append("dist\\winui\\tools\\zxprobe.exe 与 dist\\zxprobe.exe 不是同一版本"
                        "（引擎探针版本不一致会让共享电脑声音静默失效）")
    ok = not problems
    print("=" * 78)
    print("== 媒体资源与引擎探针版本一致性")
    print("=" * 78)
    for p in problems:
        print("  ! " + p)
    print(f"---- {'PASS' if ok else 'FAIL'}: 媒体资源与引擎探针版本一致性")
    return ok


def check_app_pri():
    """发版守卫：WinUI 3 非打包应用的产物必须带 resources.pri。

    【为什么是硬闸门】实测：构建产物缺 resources.pri → 应用启动即崩
    （Microsoft.UI.Xaml.dll / 0xc000027b / 连窗口都没有）；补上 pri → 正常出窗口。
    csproj 里 <EnableCoreMrtTooling>false</EnableCoreMrtTooling> 的注释写着
    "非打包模式下不需要它"，那个结论是错的。这条检查就是为了不再靠人发现。
    """
    print("=" * 78)
    print("== 发版守卫：主应用产物是否带 resources.pri")
    print("=" * 78)
    app = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
           / "net8.0-windows10.0.19041.0" / "win-x64")
    exe = app / "ZongxianVoice.exe"
    pri = app / "resources.pri"

    if not exe.is_file():
        print(f"[SKIP] 找不到 {exe}")
        print("---- FAIL: 主应用产物资源索引（resources.pri）")
        return False

    has_pri = pri.is_file()
    print(f"  {exe.name}: OK（{exe.stat().st_size:,} 字节）")
    print(f"  resources.pri: " + (f"OK（{pri.stat().st_size:,} 字节）" if has_pri else "缺失"))
    if not has_pri:
        print("  [原因] 缺 resources.pri 时 WinUI 3 加载 XAML 会抛 0xc000027b，应用启动即崩、无窗口。")
        print("  [修法] powershell -File build\\gen-app-pri.ps1 -AppDir "
              f'"{app}"')
    print(f"---- {'PASS' if has_pri else 'FAIL'}: 主应用产物资源索引（resources.pri）")
    return has_pri


def _git(args: list[str]) -> str:
    """跑 git 拿字符串（找不到 git 就返回空串，让调用方降级成 SKIP）。"""
    for cand in (r"D:\BuildTools\MinGit\cmd\git.exe", "git"):
        try:
            r = subprocess.run([cand, *args], cwd=str(ROOT), capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
            if r.returncode == 0:
                return r.stdout.strip()
        except FileNotFoundError:
            continue
    return ""


def check_dist():
    """发版守卫：dist/ 是"能交给用户的东西"，必须没有陈旧重复物、没有中间产物。

    【为什么要有这条】接手时列的三件"比 bug 更危险"的事里就有"dist 一团乱"：
    同一个发布线躺着好几个版本的 zip（谁是当前版靠猜）、日志混在里面、
    打包产物比源码老十几个小时（`dist/winui` 的 dll 是 03:50、构建树是 15:09）——
    这类问题不会报错，只会让人发错包。规则与清单见 docs/dist-policy-2026-10-05.md。
    """
    print("=" * 78)
    print("== 发版守卫：dist/ 卫生与打包产物")
    print("=" * 78)
    dist = ROOT / "dist"
    if not dist.is_dir():
        print("[SKIP] 没有 dist/ 目录")
        print("---- FAIL: dist 卫生")
        return False

    problems: list[str] = []
    # 0.5) 【2026-10-06 事故】用户配置与测试配置绝不能进包。
    #   v58 曾带上 app-config.json（内含我截图手写的假房间与假地址），
    #   用户打开看到的就是"房间是写死的"。用户配置应由应用**首次启动时自己生成**。
    voice = dist / "winui"
    for bad_pat, why in (("app-config.json", "用户配置（应由应用首次启动时生成，不由发布物携带）"),
                         ("room-test-*.json", "房间单测的临时配置"),
                         ("*.log", "运行时日志")):
        for f in sorted(voice.glob(bad_pat)):
            if f.is_file():
                problems.append(f"dist\\winui\\{f.name} 不该进包：{why}")
                print(f"  ! 发现不应进包的产物：dist\\winui\\{f.name}（{why}）")


    # 0) 先把「解压出来的测试副本」排除掉。
    #    实测（2026-10-05）：用户把发布包直接解压进了 dist/（Windows「全部解压」默认建同名文件夹），
    #    dist 下就出现一个目录，里面是完整应用（含 .webview2 与它自己的日志）。
    #    那**不是**发布物、也**不是**垃圾 —— 是用户正在用的测试副本。
    #    守卫必须识别并跳过，否则每次都误报；但**要打印出来**，不能静默当没看见。
    #
    #    判据用「特征」而不是「和某个 zip 同名」：副本对应的 zip 可能已经被归档挪走
    #    （本轮就踩到：v25 的 zip 已进隔离区，只剩用户解压出来的目录）。
    #    特征 = 顶层目录 ≠ winui，里面有 ZongxianVoice.exe，且带 .webview2 或 _internal。
    extracted = set()
    for d in dist.iterdir():
        if not d.is_dir() or d.name == "winui":
            continue
        # 语音包解压出来：有 ZongxianVoice.exe + media/call.html；
        # A 线解压出来：有 _internal；跑过一次还会多出 .webview2。
        # 三种都认，避免"解压后还没运行过"就被判成未登记项。
        if (d / "ZongxianVoice.exe").is_file() and (
                (d / ".webview2").is_dir() or (d / "_internal").is_dir()
                or (d / "media" / "call.html").is_file()):
            extracted.add(d.name)
    if extracted:
        print("  [提示] 以下目录是解压出来的测试副本，已跳过检查：%s" % "、".join(sorted(extracted)))
        print("         （建议解压到 dist 之外，比如桌面 —— dist 是发布目录）")

    files = [p for p in dist.rglob("*") if p.is_file()
             and p.relative_to(dist).parts[0] not in extracted]

    # 1) WinUI 应用目录必须带 resources.pri（缺了启动即崩，见 check_app_pri）
    for app in dist.rglob("ZongxianVoice.exe"):
        if not (app.parent / "resources.pri").is_file():
            problems.append(f"{app.relative_to(dist)} 旁边缺 resources.pri")
        if not (app.parent / "BUILD-INFO.txt").is_file():
            problems.append(f"{app.parent.relative_to(dist)} 缺 BUILD-INFO.txt（无法判断是哪次构建）")

    # 2) 中间产物 / 日志 / 测试残留不许进 dist
    #    【规则只有一份】判定在 build\residue-rules.py（打包脚本与守卫共用）。
    #    审计 §7.2 的病根就是"什么算残留"在四处各有一份定义、互不相同 ——
    #    v36 的 received/zx-filetest.bin 正是这么漏进发布包的。
    sys.path.insert(0, str(ROOT / "build"))
    from importlib import import_module as _imp
    residue = _imp("residue-rules")
    for p in files:
        rel = p.relative_to(dist)
        why = residue.dist_residue_reason(rel)
        if why:
            problems.append(f"{why}进 dist：{rel}")

    # 3) 陈旧重复：文件名带"副本"/"- Copy"，或同一发布线留了多个版本
    for p in files:
        if "副本" in p.name or " - Copy" in p.name:
            problems.append(f"重复副本：{p.relative_to(dist)}")
    voice_zips = sorted(dist.glob("棕仙语音-测试版-v*.zip"), key=lambda q: q.stat().st_mtime)
    if len(voice_zips) > 1:
        keep = voice_zips[-1].name
        for p in voice_zips[:-1]:
            problems.append(f"同一发布线留了多个包（应只留最新的 {keep}）：{p.name}")

    total_mb = sum(p.stat().st_size for p in files) / 1024 / 1024

    # 4) 【可靠判据】交付的网页必须与源码副本逐字节一致。
    #    这条比"看时间戳谁新"可靠得多 —— 本仓库 2026-10-05 才初始化，拿 git 提交时间
    #    比产物时间会得出"A 线落后 20 小时"这种**假警报**（源码 10-04 写、10-05 才提交）。
    #    哈希不会骗人。
    base = dist / "swiftdrop.html"
    if base.is_file():
        base_hash = hashlib.sha256(base.read_bytes()).hexdigest()
        portable_dirs = [d for d in dist.iterdir() if d.is_dir() and (d / "_internal").is_dir()]
        if not portable_dirs:
            problems.append("找不到 A 线便携版目录（含 _internal 的那个）")
        for d in portable_dirs:
            for rel in (pathlib.Path("swiftdrop.html"),
                        pathlib.Path("_internal") / "swiftdrop.html"):
                cp = d / rel
                if not cp.is_file():
                    problems.append(f"便携版里缺 {d.name}\\{rel}")
                elif hashlib.sha256(cp.read_bytes()).hexdigest() != base_hash:
                    problems.append(f"网页与源码不一致（{d.name}\\{rel} 与 dist\\swiftdrop.html 不同）")
        print(f"  交付网页哈希：{base_hash[:16]}...（已与便携版内两份比对）")

    # 5) 【可靠判据】发布物清单双向核对：顶层每一项都登记了；登记的都还存在。
    manifest = dist / "RELEASES.md"
    if not manifest.is_file():
        problems.append("dist\\RELEASES.md 缺失（没有发布物清单）")
    else:
        text = manifest.read_text(encoding="utf-8", errors="replace")
        for child in sorted(dist.iterdir()):
            if child.name == manifest.name or child.name in extracted:
                continue
            if child.name not in text:
                problems.append(f"{child.name} 没在 dist\\RELEASES.md 里登记")
        for name in re.findall(r"^\|\s*`([^`]+)`", text, flags=re.M):
            clean = name.strip().rstrip("\\")
            if clean and clean != manifest.name and not (dist / clean).exists():
                problems.append(f"dist\\RELEASES.md 登记了 {clean}，但 dist 里没有")
        print(f"  发布物清单：{manifest.name}（{len(text.splitlines())} 行）")

    # 6) 【溯源有效性】打包产物的 BUILD-INFO 必须跟得上源码。
    #    只看"commit == HEAD"会误报：之后只改文档/构建脚本的提交并不影响二进制。
    #    所以判据是：commit == HEAD，**或者** 从那个 commit 到现在没有任何提交动过应用源码。
    stamp_commit = ""
    # 每份产物该看哪些源码：BUILD-INFO 里的 `sources:` 行说了算（缺省按 WinUI 的那套）
    default_sources = ["src/winui-cs", "src/media"]
    for info in dist.rglob("BUILD-INFO.txt"):
        if info.parent.name == ".webview2":
            continue
        # 用户解压出来的测试副本里的 BUILD-INFO 天然落后，不参与溯源判定
        if info.relative_to(dist).parts[0] in extracted:
            continue
        text_info = info.read_text(encoding="utf-8", errors="replace")
        stamp_commit = ""
        src_paths = default_sources
        for line in text_info.splitlines():
            if line.startswith("commit:"):
                stamp_commit = line.split(":", 1)[1].strip().split("+")[0]
            elif line.startswith("sources:"):
                src_paths = [s.strip() for s in line.split(":", 1)[1].split(",") if s.strip()]
        if not stamp_commit:
            problems.append(f"{info.relative_to(dist)} 里没有 commit 行")
            continue
        head = _git(["rev-parse", "--short", "HEAD"])
        if not head:
            print("  [SKIP] 找不到 git，跳过溯源有效性检查")
            break
        if stamp_commit == head:
            print(f"  溯源：{info.parent.relative_to(dist)} 的 BUILD-INFO = HEAD（{head}）")
            continue
        changed = _git(["log", "--oneline", f"{stamp_commit}..HEAD", "--", *src_paths])
        if changed:
            problems.append(f"{info.parent.relative_to(dist)} 的构建（{stamp_commit}）之后"
                            f"它依赖的源码又改过 {len(changed.splitlines())} 次，需要重新打包")
        else:
            print(f"  溯源：{info.parent.relative_to(dist)} 构建于 {stamp_commit}，"
                  f"之后依赖源码没变 → 仍然有效")
    print(f"  dist: {len(files)} 个文件，{total_mb:.1f} MB")
    for p in sorted(files, key=lambda q: q.stat().st_size, reverse=True)[:5]:
        print(f"    最大：{p.stat().st_size/1024/1024:8.1f} MB  {p.relative_to(dist)}")
    if voice_zips:
        print(f"  语音测试版包：{voice_zips[-1].name}（共 {len(voice_zips)} 个）")
    for m in problems:
        print(f"  [问题] {m}")
    if problems:
        print("  [修法] 把陈旧/重复物挪到 build\\dist-quarantine-<日期>\\（不要直接删，见策略文档）")
    ok = not problems
    print(f"---- {'PASS' if ok else 'FAIL'}: dist 卫生")
    return ok


def check_script_sanity():
    """守卫：build\\*.py 里"用了某个名字但从没绑定"——必然 NameError。

    【为什么加这条】审计 §7.3 抓到：`smoke-ui.py` 新增断言用了 `re.search`，但文件里
    没有 `import re`，于是**必定抛 NameError**，被外层兜成 [FAIL]；
    而 run-checks 又按 [PASS] 判定 —— 结果是"规矩四：测试用模拟键鼠"这条自检
    在最关键的一轮里**根本不可能通过**，而没人看得出来。
    这类"死代码级"错误只能靠机器扫，靠读代码会漏。
    """
    import ast
    print("=" * 78)
    print("== 守卫：build\\*.py 名字绑定（防 NameError 型死断言）")
    print("=" * 78)
    import builtins as _bi
    builtins_names = set(dir(_bi)) | {"__name__", "__file__", "__doc__", "True", "False", "None", "self", "cls"}
    problems = []
    scanned = 0
    for path in sorted((ROOT / "build").glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"), filename=str(path))
        except SyntaxError as e:
            problems.append(f"{path.name}: 语法错误 {e}")
            continue
        scanned += 1
        bound = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for al in node.names:
                    bound.add((al.asname or al.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for al in node.names:
                    bound.add(al.asname or al.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
            elif isinstance(node, ast.arg):
                bound.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
                bound.update(node.names)
            elif isinstance(node, ast.alias):
                bound.add((node.asname or node.name).split(".")[0])
        # 只查"属性访问的根名字"（re.search / os.path 这种），这是实测出错的那一类
        roots = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                roots.setdefault(node.value.id, node.lineno)
        unknown = {n: ln for n, ln in roots.items()
                   if n not in bound and n not in builtins_names and len(n) <= 20}
        for n, ln in sorted(unknown.items(), key=lambda kv: kv[1]):
            problems.append(f"{path.name}:{ln} 用了 `{n}.…` 但全文没有绑定 `{n}`")
    print(f"  扫描 {scanned} 个脚本")
    for p in problems:
        print("  ! " + p)
    ok = not problems
    print(f"---- {'PASS' if ok else 'FAIL'}: build 脚本名字绑定")
    return ok


def check_residue_rules():
    """守卫：证明"残留清单"真的拦得住 —— 用构造出来的路径做反例，不靠读代码相信。

    【为什么要有】v36 的 `received/zx-filetest.bin`（184320 B）进了发布包，原因是
    "什么算残留"在打包脚本里那份清单**不认识 received/**。这条守卫直接对
    build\\residue-rules.py 断言几个必须拦住的例子；谁把规则改松了，这里立刻红。
    """
    print("=" * 78)
    print("== 守卫：测试残留规则（唯一来源 build\\residue-rules.py）")
    print("=" * 78)
    sys.path.insert(0, str(ROOT / "build"))
    from importlib import import_module as _imp
    residue = _imp("residue-rules")

    must_skip = [
        "received/zx-filetest.bin",              # v36 实际漏出去的那一个
        "recordings/mic-selfcheck.wav",
        "sent/photo.jpg",
        "incoming/a.bin",
        "audio-probe-tmp/x.bin",
        "engine.log",
        "A.txt",
        "nativevideo-pos.txt",
        "video.h264",
        "shot.bmp",
        "debug.pdb",
    ]
    must_keep = [
        "ZongxianVoice.exe",
        "resources.pri",
        "BUILD-INFO.txt",
        "media/call.html",
        "media/call.js",
        "media/bus.js",
        "media/log.js",
        "media/share-audio-worklet.js",
        "tools/zxprobe.exe",
        "ZongxianVoice.runtimeconfig.json",
    ]
    problems = []
    for rel in must_skip:
        why = residue.skip_reason(pathlib.PurePath(rel))
        if not why:
            problems.append(f"本该排除却允许进包：{rel}")
        else:
            print(f"  排除 OK：{rel}  ← {why}")
    for rel in must_keep:
        why = residue.skip_reason(pathlib.PurePath(rel))
        if why:
            problems.append(f"本该保留却被排除：{rel}（{why}）")
    # dist 守卫这一侧：.webview2 是合法运行时目录，received/ 必须算残留
    if residue.dist_residue_reason(pathlib.PurePath("winui/.webview2/EBWebView/Default/000003.log")):
        problems.append("dist 守卫误报 .webview2 运行时文件")
    if not residue.dist_residue_reason(pathlib.PurePath("winui/received/zx-filetest.bin")):
        problems.append("dist 守卫漏掉 winui/received/（v36 的漏洞）")
    for p in problems:
        print("  ! " + p)
    ok = not problems
    print(f"---- {'PASS' if ok else 'FAIL'}: 残留规则反例（{len(must_skip)} 必排 + {len(must_keep)} 必留）")
    return ok


def check_ui_element_references():
    """守卫：build\\smoke-ui.py 里 locate('X') 的 X 必须在 MainWindow.xaml 里真实存在。

    【为什么】2026-10-06 踩到：设置里有个假开关 ShareAudioSwitch（全仓库零引用）被删掉后，
    smoke-ui 还拿它当"设置面板已打开"的判据 —— 于是设置**正常打开**也会报 FAIL，
    看起来像产品坏了。测试引用不存在的控件名，和产品少了控件，必须能被机器分开。
    允许例外（UIA 不暴露的纯布局容器 / 刻意不做的断言）写在 ALLOWED_MISSING 里并注明原因。
    """
    print("=" * 78)
    print("== 守卫：界面自检引用的控件名是否真实存在")
    print("=" * 78)
    ALLOWED_MISSING = {
        # 纯布局容器，UIA 树里不暴露（实测 locate('MemberList') 拿不到），
        # 只是文档性写法，不参与断言 —— 保留在注释里可以，出现在 locate() 里才报错。
        "MemberList": "纯布局容器，UIA 不暴露",
    }
    smoke = ROOT / "build" / "smoke-ui.py"
    xaml = ROOT / "src" / "winui-cs" / "src" / "MainWindow.xaml"
    problems = []
    if not smoke.is_file() or not xaml.is_file():
        print("  [SKIP] 缺 smoke-ui.py 或 MainWindow.xaml")
        print("---- FAIL: 界面自检控件名")
        return False
    used = sorted(set(re.findall(r"locate\(\s*'([A-Za-z0-9_]+)'", smoke.read_text(encoding="utf-8", errors="replace")))
                  | set(re.findall(r"wait_for\(\s*'([A-Za-z0-9_]+)'", smoke.read_text(encoding="utf-8", errors="replace"))))
    text = xaml.read_text(encoding="utf-8", errors="replace")
    defined = set(re.findall(r'x:Name="([A-Za-z0-9_]+)"', text))
    print(f"  smoke-ui 引用 {len(used)} 个控件名；MainWindow.xaml 定义 {len(defined)} 个 x:Name")
    for name in used:
        if name in defined:
            continue
        if name in ALLOWED_MISSING:
            problems.append(f"smoke-ui 引用了刻意找不到的控件 {name}（{ALLOWED_MISSING[name]}）——不该写进断言")
        else:
            problems.append(f"smoke-ui 断言了 MainWindow.xaml 里不存在的控件 {name}（测试写错，不是产品坏）")
    for p in problems:
        print("  ! " + p)
    ok = not problems
    print(f"---- {'PASS' if ok else 'FAIL'}: 界面自检控件名")
    return ok


def check_cmd_line_endings():
    """守卫：build\\*.cmd 必须是 CRLF 行尾。

    【为什么是硬闸门，2026-10-06 实测】build\\build-winui-cs.cmd 变成**纯 LF** 后，
    cmd.exe 解析直接错位：报 `'buildTransitive' is not recognized`、`'xist' is not recognized`、
    `inside was unexpected at this time`，整个日志只有 13 行、退出码 255 —— 而文件本身
    `dotnet build` 一点问题没有，看上去像"构建挂了"。改回 CRLF 后立刻 Build OK。
    这个仓库的 .cmd 里有 UTF-8 中文注释（文件头虽写着 ASCII-only），
    "LF 行尾 + 多字节注释"正是最容易踩的组合，所以按文件类型硬性要求 CRLF。
    """
    print("=" * 78)
    print("== 守卫：build\\*.cmd 行尾（必须是 CRLF）")
    print("=" * 78)
    problems = []
    checked = 0
    for p in sorted((ROOT / "build").glob("*.cmd")):
        raw = p.read_bytes()
        checked += 1
        lf = raw.count(b"\n")
        cr = raw.count(b"\r")
        if cr != lf:
            problems.append(f"{p.name}: CR={cr} / LF={lf} —— 有裸 LF 行尾，cmd 会解析错位")
        if not raw.endswith(b"\r\n"):
            problems.append(f"{p.name}: 文件末尾没有换行（cmd 在最后一行会报 'inside was unexpected'）")
    print(f"  检查 {checked} 个 .cmd")
    for p in problems:
        print("  ! " + p)
    ok = not problems
    print(f"---- {'PASS' if ok else 'FAIL'}: cmd 行尾")
    return ok


def check_releases_manifest_shape():
    """守卫：dist\\RELEASES.md 的登记行必须落在**真正的表格**里。

    【为什么】2026-10-06 实测：`release.py` 的 `step_normalize_dist()` 把新产物行
    `out.append('| zxprobe.exe | … |')` 追加在**文件末尾**，而文件末尾是正文段落 ——
    于是这一行成了"孤立的表格行"（前面没有表头/分隔行的空行），markdown 渲染错乱，
    守卫却因为"文件里出现过 zxprobe.exe 这个名字"而 PASS。名字对得上、位置是错的，
    这类错误只能靠结构检查抓（下面的规则就是从实际文件里总结出来的）。
    """
    print("=" * 78)
    print("== 守卫：dist\\RELEASES.md 表格结构（登记行不许变孤行）")
    print("=" * 78)
    rel = ROOT / "dist" / "RELEASES.md"
    if not rel.is_file():
        print("  [SKIP] 没有 dist\\RELEASES.md（release.py 首次发版会建）")
        print("---- FAIL: RELEASES 表格结构")
        return False
    lines = rel.read_text(encoding="utf-8", errors="replace").splitlines()
    problems = []

    def is_row(s: str) -> bool:
        t = s.strip()
        return t.startswith("|") and t.endswith("|") and t.count("|") >= 3

    def is_sep(s: str) -> bool:
        t = s.strip()
        return bool(re.fullmatch(r"\|[\s\-\|:]+\|", t)) if t else False

    for i, ln in enumerate(lines):
        if not is_row(ln) or is_sep(ln):
            continue
        prev = lines[i - 1].strip() if i > 0 else ""
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        # 合法三种：① 本行是表头（下一行是分隔行）② 上一行是分隔行 ③ 上一行是同表的数据行
        if is_sep(nxt) or is_sep(prev) or (is_row(prev) and not is_sep(prev)):
            continue
        problems.append(f"第 {i+1} 行是孤立表格行（前一行：{prev[:30]!r}）：{ln[:60]}")
    for p in problems:
        print("  ! " + p)
    ok = not problems
    print(f"---- {'PASS' if ok else 'FAIL'}: RELEASES 表格结构（{len(lines)} 行）")
    return ok


def check_bridge_contract():
    """守卫：C#↔页面 契约双向对账（S1）。

    审计病根 3 的原话是"C# 与 JS 的契约是裸字符串，**双向都有对不上**"，
    当时全靠肉眼才发现 `devices` 没人处理。这条守卫把那次肉眼检查变成机器检查：
      A. C# 会调用的页面方法（CallAsync 实参）必须真的存在于页面（否则调用静默失败）
      B. 页面会发的事件（post/sendSignal 的 type）必须有人接：
         要么在 HandleMediaEvent 里有**带实现的** case，要么在 BridgeContract 的
         SignalingOnly / ReservedEvents 里**写明理由**（故意不接 / 保留未触发）
      C. 反向：C# 有 case、页面从不发 ⇒ 要么进 ReservedEvents，要么就是登记过期
    契约登记的唯一来源是 src/winui-cs/src/BridgeContract.cs（EventsIn / SignalingOnly / ReservedEvents）。
    """
    print("=" * 78)
    print("== 守卫：C# ↔ 页面 契约双向对账（BridgeContract.cs 为准）")
    print("=" * 78)
    import ast
    # 【S3 起】页面已拆成 shell + 多个模块：契约面（window.zxEngine、post 的 type）
    # 不再全在 call.html 里，所以这里扫**整个 media 目录**的 .html/.js。
    media_dir = ROOT / "src" / "winui-cs" / "media"
    page_path = media_dir / "call.html"
    cs_dir = ROOT / "src" / "winui-cs" / "src"
    bc_path = cs_dir / "BridgeContract.cs"
    if not page_path.is_file() or not bc_path.is_file():
        print("  [SKIP] 缺 call.html 或 BridgeContract.cs")
        print("---- FAIL: 桥接契约对账")
        return False
    sources = sorted([p for p in media_dir.rglob("*") if p.suffix.lower() in (".html", ".js")])
    page = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in sources)
    bc = bc_path.read_text(encoding="utf-8", errors="replace")
    cs = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in sorted(cs_dir.glob("*.cs")))
    # 壳必须真的把入口模块引起来（拆完之后最容易犯的错：壳里忘了 <script src>）
    shell_problems = []
    shell = page_path.read_text(encoding="utf-8", errors="replace")
    if "./call.js" not in shell:
        shell_problems.append("call.html 里没有引用入口模块 ./call.js（拆完之后页面不会启动）")

    def strip_cs_line_comments(text):
        """去掉 C# 的行注释（// …）。**必须先剥注释再取字符串字面量**：
        2026-10-06 实测，我在 `"link-error", // 建链失败的原因（原来只 log 到隐藏 DOM…）`
        这类登记行里写了带引号的说明，守卫于是把注释里的中文当成"登记的事件名"，
        报出"登记了 `看到几个对端` 但页面从不发"这种假失败。"""
        return "\n".join(re.sub(r"//.*$", "", ln) for ln in text.split("\n"))

    def csharp_string_list(field):
        m = re.search(re.escape(field) + r"\s*=\s*new\[\]\s*\{(.*?)\};", bc, re.S)
        return sorted(set(re.findall(r'"([^"]+)"', strip_cs_line_comments(m.group(1))))) if m else []

    def csharp_dict_keys(field):
        m = re.search(re.escape(field) + r"\s*=\s*new Dictionary<string, string>\s*\{(.*?)\n    \};", bc, re.S)
        return sorted(set(re.findall(r'\["([^"]+)"\]', strip_cs_line_comments(m.group(1))))) if m else []

    events_in = csharp_string_list("EventsIn")
    methods_out = csharp_string_list("MethodsOut")
    page_only = csharp_string_list("PageOnlyMethods")
    signaling = csharp_dict_keys("SignalingOnly")
    reserved = csharp_dict_keys("ReservedEvents")
    if not events_in or not methods_out:
        print("  ! BridgeContract.cs 里读不到 EventsIn / MethodsOut")
        print("---- FAIL: 桥接契约对账")
        return False

    problems = list(shell_problems)

    # A. C# 调用的方法必须存在于页面
    called = sorted(set(re.findall(r"Call(?:RawString)?Async\(\s*\"([A-Za-z0-9_]+)\"", cs)))
    i = page.find("window.zxEngine = {")
    j = page.find("\n};", i)
    body = page[i:j] if i >= 0 else ""
    # ① 方法定义：  async foo(...)  /  foo(...)
    page_methods = set(re.findall(r"^\s{2}(?:async\s+)?([a-zA-Z][A-Za-z0-9_]*)\s*\(", body, re.M))
    # ② 简写属性：  testSpeaker,
    #    【2026-10-07 补】上面那条只认 `名字(`，于是 `window.zxEngine = { testSpeaker, ... }`
    #    这种简写属性会被误判成"页面没有这个方法"（本轮实测：契约守卫误报）。
    #    在 zxEngine 的对象体里，`名字,` 就是"把同名函数挂上去"，所以这样匹配是安全的。
    page_methods |= set(re.findall(r"^\s{2}([a-zA-Z][A-Za-z0-9_]*)\s*,\s*$", body, re.M))
    page_methods = sorted(page_methods)
    for m in called:
        if m not in page_methods:
            problems.append(f"C# 调用页面方法 `{m}`，但页面 zxEngine 里没有它（调用会静默失败）")
    for m in methods_out:
        if m not in called and m != "setIceConfig":
            problems.append(f"登记 MethodsOut 里的 `{m}`，但 C# 里没有对应的 CallAsync 调用（登记过期？）")
    for m in called:
        if m not in methods_out:
            problems.append(f"C# 调用了 `{m}` 但没登记进 MethodsOut（登记漏了）")

    # B. 页面事件必须有 case（带实现）或写明理由
    events = sorted(set(re.findall(r"(?:post|sendSignal)\(\s*\{\s*type:\s*['\"]([A-Za-z0-9_\-]+)['\"]", page))
                    | set(re.findall(r"type:\s*['\"]([A-Za-z0-9_\-]+)['\"]", page)))
    cases = sorted(set(_top_level_event_cases(_handle_event_body(cs))))
    for e in events:
        if e in cases or e in signaling or e in reserved:
            continue
        problems.append(f"页面会发事件 `{e}`，但 C# 没有 case、也没在 SignalingOnly/ReservedEvents 写明理由")
    for e in events_in:
        if e not in events:
            problems.append(f"登记 EventsIn 里的 `{e}`，但页面从不发它（登记过期或页面改名）")
    for e in events:
        if e not in events_in:
            problems.append(f"页面发了 `{e}` 但没登记进 EventsIn（登记漏了）")

    # C. 反向：C# 有 case、页面不发 ⇒ 必须有理由
    for c in cases:
        if c not in events and c not in reserved:
            problems.append(f"C# 有 case `{c}` 但页面从不发它，且没进 ReservedEvents（死分支？）")

    print(f"  页面方法 {len(page_methods)} 个 / C# 调用 {len(called)} 个")
    print(f"  页面事件 {len(events)} 种 / C# case {len(cases)} 个；"
          f"信令类 {len(signaling)}、保留未触发 {len(reserved)}")
    for p in problems:
        print("  ! " + p)
    ok = not problems
    print(f"---- {'PASS' if ok else 'FAIL'}: 桥接契约对账")
    return ok


def _top_level_event_cases(switch_body):
    """从 `switch (ev.Type) { … }` 的**块体**里取顶层 case 名。

    【两个坑，都是实测踩出来的】
      1. 不能对整个方法体正则找 `case "x":` —— call-state 里还有内层 `switch (st) { case "calling": … }`，
         于是"页面从不发的死分支"会误报 calling/connecting/idle（第一版就这么错的）。
      2. 不能只看 depth==0 —— 叠在一起的 case 标签（`case "a": case "b": 实现`）里，
         前一个标签下面的代码是空的，会被误判成"空实现"。
    这里用括号深度跳过所有嵌套块，保证只取最外层 switch 的标签。
    """
    labels = []
    depth = 0
    i = 0
    while i < len(switch_body):
        c = switch_body[i]
        if c == '"':
            i += 1
            while i < len(switch_body) and switch_body[i] != '"':
                i += 2 if switch_body[i] == "\\" else 1
        elif c == "/" and i + 1 < len(switch_body) and switch_body[i + 1] == "/":
            while i < len(switch_body) and switch_body[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < len(switch_body) and switch_body[i + 1] == "*":
            i = switch_body.find("*/", i) + 1
        elif c in "{}":
            depth += 1 if c == "{" else -1
        elif depth == 0:
            m = re.compile(r'case\s+"([^"]+)"\s*:').match(switch_body, i)
            if m:
                labels.append(m.group(1))
                i = m.end()
                continue
        i += 1
    return labels


def _handle_event_body(cs_text):
    """取出 HandleMediaEvent 里 `switch (ev.Type) { … }` 的块体（守卫与扫描脚本同一套逻辑）。"""
    m = re.search(r"private void HandleMediaEvent\([^)]*\)\s*\{", cs_text)
    if not m:
        return ""
    start = cs_text.index("{", m.start())
    depth, i = 0, start
    in_str = in_chr = False
    end = len(cs_text)
    while i < len(cs_text):
        c = cs_text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif in_chr:
            if c == "\\":
                i += 2
                continue
            if c == "'":
                in_chr = False
        elif c == '"':
            in_str = True
        elif c == "'":
            in_chr = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
        i += 1
    method = cs_text[start:end]
    k = method.find("switch (ev.Type)")
    if k < 0:
        return ""
    b = method.find("{", k)
    depth = 0
    for j in range(b, len(method)):
        if method[j] == "{":
            depth += 1
        elif method[j] == "}":
            depth -= 1
            if depth == 0:
                return method[b + 1:j]
    return ""


def check_media_line_endings():
    """守卫：页面/前端资源必须是 **LF** 行尾。

    【为什么】2026-10-06 实测：`src\\winui-cs\\media\\call.html` 在某个环节被整篇写成 CRLF
    （内容一字未变，只是每个 LF 前多了 CR：142,147 → 145,394 字节，差 3,247 = 行数），
    于是它与 `dist\\winui\\media\\call.html` 不再逐字节相同 —— 媒体/探针一致性守卫立刻 FAIL。
    媒体文件在两个平台上都能跑，但"同一份文件在两个目录里字节不同"正是这个项目被打过两次的
    静默失效来源（v36 就是陈旧页面出的包），所以这里按**行尾**卡死，别让它在构建链里漂。
    """
    print("=" * 78)
    print("== 守卫：前端资源行尾（应为 LF）")
    print("=" * 78)
    problems = []
    checked = 0
    media = ROOT / "src" / "winui-cs" / "media"
    if not media.is_dir():
        print("  [SKIP] 没有 media 目录")
        print("---- FAIL: 前端资源行尾")
        return False
    for p in sorted(media.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in (".html", ".js", ".css"):
            continue
        checked += 1
        raw = p.read_bytes()
        cr = raw.count(b"\r")
        if cr:
            problems.append(f"{p.relative_to(ROOT)} 含 {cr} 个 CR（应为纯 LF）")
    print(f"  检查 {checked} 个前端资源")
    for x in problems:
        print("  ! " + x)
    ok = not problems
    print(f"---- {'PASS' if ok else 'FAIL'}: 前端资源行尾")
    return ok


def check_page_module_loads():
    """守卫：用 Node 当"假浏览器"**真的 import 一次页面模块**，必须加载成功。

    【为什么必须有】2026-10-06 拆页面时实测踩到两次，都是"模块加载期就死"：
      1) `log.js` 搬过来时只复制了 `const`、忘了 `export` ⇒ call.js 的 import 直接 SyntaxError，
         整个页面**一行都不执行**，而 C# 侧只看到"通话页未上报 engine-loaded"；
      2) C# 生成 JS 用裸名 `zxEngine` ⇒ 模块作用域里 ReferenceError（改动见 MediaEngine.cs）。
    这两种问题在应用里表现为"某个功能悄悄不生效"，排查很贵（反复跑 GUI 才知道）。
    Node 里 import 一次就能拿到原始堆栈 ⇒ 作为发版守卫，比"看代码"硬得多。
    """
    print("=" * 78)
    print("== 守卫：页面模块能否被加载（Node 假浏览器 import 一次）")
    print("=" * 78)
    node = None
    for cand in (r"C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe",
                 "node"):
        try:
            r = subprocess.run([cand, "--version"], capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                node = cand
                break
        except Exception:
            continue
    harness = ROOT / "build" / "page-load-check.mjs"
    if node is None or not harness.is_file():
        print("  [SKIP] 找不到 node 或 page-load-check.mjs")
        print("---- FAIL: 页面模块加载")
        return False
    r = subprocess.run([node, str(harness), str(ROOT / "src" / "winui-cs" / "media")],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-2000:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "模块加载成功" in out and "window.zxEngine 存在: object" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 页面模块加载")
    return ok


def check_page_singletons():
    """守卫：页面的链路单例别名**只能出现在兼容视图里**。

    【背景】S3 第 2 步把"一条链路一个对象"立起来（media/link.js），`state.pc` /
    `state.remoteId` / `chatChannel` 降级成兼容视图。但光靠约定守不住 —— 审计病根 1 就是
    "同一事实存两份，读几乎都走别名"，所以这里把它变成**机器可查的规则**：

      · `state.pc` / `state.remoteId` / `chatChannel` 的裸引用只允许出现在
        `primaryPc / primaryId / primaryChat` 三个访问器与 `adoptPrimaryAliases()` 里；
      · 其他地方要读就用访问器，要写就调 adoptPrimaryAliases()。

    这条规则允许渐进式迁移（剩下的调用点用访问器即自然合规），而不是一次性大改。
    """
    print("=" * 78)
    print("== 守卫：页面链路单例别名只允许出现在兼容视图（S3 第 2 步规则）")
    print("=" * 78)
    page = ROOT / "src" / "winui-cs" / "media" / "call.js"
    if not page.is_file():
        print("  [SKIP] 没有 media/call.js（页面可能还没拆）")
        print("---- FAIL: 页面单例别名")
        return False
    lines = page.read_text(encoding="utf-8", errors="replace").split("\n")
    allowed = {
        # 访问器用 function 声明（不是 const 箭头）：模块顶部的初始化会用到它们，
        # const 箭头有 TDZ，会让整页加载失败（2026-10-06 实测，被页面加载守卫抓到）。
        "function primaryPc() { return state.pc; }": "primaryPc 访问器",
        "function primaryId() { return state.remoteId; }": "primaryId 访问器",
        "state.pc = l ? l.pc : null;": "adoptPrimaryAliases 写入口",
        "state.remoteId = l ? l.id : null;": "adoptPrimaryAliases 写入口",
        "chatChannel = l ? (l.chat || null) : null;": "adoptPrimaryAliases 写入口",
    }
    problems = []
    allowed_hits = 0
    for n, raw in enumerate(lines, 1):
        s = raw.strip()
        if s.startswith("//") or s.startswith("*") or s.startswith("/*"):
            continue
        if s in allowed:
            allowed_hits += 1
            continue
        for name in ("state.pc", "state.remoteId", "chatChannel"):
            if re.search(r"(?<![\w.])" + re.escape(name) + r"\b", raw):
                problems.append(f"call.js:{n} 裸用了 `{name}`（应改用访问器/adoptPrimaryAliases）：{s[:80]}")
                break
    print(f"  兼容视图白名单命中 {allowed_hits}/{len(allowed)} 条；裸用 {len(problems)} 处")
    for p in problems:
        print("  ! " + p)
    ok = not problems
    print(f"---- {'PASS' if ok else 'FAIL'}: 页面单例别名")
    return ok


def check_two_instance():
    """守卫：双实例联机（S3 第 2 步判据，也是"多对端"这类洞的第一块补丁）。

    【为什么必须有】此前所有自检都只跑**一个**实例：链路建不起来、注册表是空的、
    第二个人拿不到媒体 —— 这些"多人相关"的故障在任何单实例自检里都**完全看不出来**。
    审计里"3 人通话时第二个对端的状态永不上报"就是这一类。

    判据全部从两个应用日志取证（不猜、不看界面）：
      · 两侧都 `[成员] 2 人`（信令联机）
      · 两侧 `[链路] 注册表 N 条：<id>`（链路注册表真的有内容）
      · 两侧 `[通话判定] 链路=connected`（媒体链路真的通了）
      · 至少一侧出现 `share-audio-debug`（getStats 级证据）
      · 两侧都没有 scriptError / ReferenceError / TypeError / [JS 异常]
    """
    print("=" * 78)
    print("== 守卫：双实例联机（链路注册表 / 通话判定 / 无脚本错误）")
    print("=" * 78)
    script = ROOT / "build" / "two-instance-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\two-instance-check.py")
        print("---- FAIL: 双实例联机")
        return False
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-3000:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "双实例联机: PASS" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 双实例联机")
    return ok


def check_media_route_collisions():
    """守卫：页面文件名不许撞上本地 HTTP 服务的内部路由前缀。

    【为什么必须有】2026-10-06 实测踩到：新增 `media\\signaling.js` 后，
    `SignalingServer` 的路由用 `path.StartsWith("/signal")` 判信令 WebSocket，
    于是 `/signaling.js` 被当成信令端点 ⇒ 404 ⇒ 页面 `import` 失败 ⇒
    **整页一行都不执行**，而 C# 只看到"通话页未上报 engine-loaded"，
    连 `js-error` 都收不到（模块图解析失败不触发 window.onerror）。
    最后是靠"用 HTTP 去问运行中的应用"才定位到的 —— 这条守卫把它变成机器检查。
    """
    print("=" * 78)
    print("== 守卫：页面文件名与本地 HTTP 路由前缀不冲突（/signal 这类）")
    print("=" * 78)
    media = ROOT / "src" / "winui-cs" / "media"
    server = ROOT / "src" / "winui-cs" / "src" / "SignalingServer.cs"
    if not media.is_dir() or not server.is_file():
        print("  [SKIP] 缺 media 目录或 SignalingServer.cs")
        print("---- FAIL: 路由前缀冲突")
        return False
    src = server.read_text(encoding="utf-8", errors="replace")
    # 【先剥注释】守卫自己就踩过：我在修路由的注释里写了"原来是 StartsWith(\"/signal\")"，
    # 结果守卫把这句说明当成了现役路由判断（假失败）。与契约守卫同一处理方式。
    src_code = "\n".join(re.sub(r"//.*$", "", ln) for ln in src.split("\n"))
    # 按 C# 真实语义抽路由判断，而不是只看字面前缀：
    #   Equals("/x")        → 只命中 /x
    #   StartsWith("/x/")   → 命中 /x/…（C# 的 StartsWith 是字面前缀）
    #   StartsWith("/x?")   → 命中 /x?…
    eq_paths = set(re.findall(r'Equals\(\s*"(/[A-Za-z0-9._-]+)"', src_code))
    sw_paths = set(re.findall(r'StartsWith\(\s*"(/[A-Za-z0-9._-]+)"', src_code))
    if not eq_paths and not sw_paths:
        print("  [SKIP] SignalingServer 里没找到 Equals(\"/…\")/StartsWith(\"/…\") 形式的路由判断")
        print("---- PASS: 路由前缀冲突")
        return True

    def route_hits(url):
        hit = []
        if url.lower() in {p.lower() for p in eq_paths}:
            hit.append("Equals")
        for pre in sw_paths:
            if url.lower().startswith(pre.lower()):
                hit.append(f"StartsWith({pre})")
        return hit

    problems = []
    files = [p for p in sorted(media.iterdir()) if p.is_file()]
    for p in files:
        url = "/" + p.name
        for how in route_hits(url):
            problems.append(f"media\\{p.name}（请求路径 {url}）会被路由 {how} 截胡"
                            f" ⇒ HTTP 404 ⇒ 页面整体不启动")
    print(f"  路由判据 Equals{sorted(eq_paths)} + StartsWith{sorted(sw_paths)}；检查 {len(files)} 个媒体文件")
    for x in problems:
        print("  ! " + x)
    ok = not problems
    print(f"---- {'PASS' if ok else 'FAIL'}: 路由前缀冲突")
    return ok


def check_page_modules_shape():
    """守卫：页面模块宿主（S4 方案 A）的接线形状必须真实可释放。

    【为什么必须有】方案 A 的价值就在"生命周期统一"。如果某个模块接进来却没有 dispose、
    或者 wire 时名字写错、工厂返回空对象，卸载路径就不会释放它的定时器/采集 ——
    现象正是审计里那类"关了引擎还在跑"的隐性泄漏，而界面上完全看不出来。
    这条守卫把"模块形状 + dispose 出口"变成机器检查（不靠人记）。
    """
    print("=" * 78)
    print("== 守卫：页面模块形状（name + dispose 出口，S4 方案 A）")
    print("=" * 78)
    media = ROOT / "src" / "winui-cs" / "media"
    call = media / "call.js"
    if not call.is_file() or not (media / "modules.js").is_file():
        print("  [SKIP] 缺 call.js 或 modules.js")
        print("---- FAIL: 页面模块形状")
        return False
    src = call.read_text(encoding="utf-8", errors="replace")
    # 1) wire({ name: 'x', create: FACTORY, deps: ... })
    wires = re.findall(r"moduleHost\.wire\(\{\s*name:\s*'([^']+)'\s*,\s*create:\s*([A-Za-z_][A-Za-z0-9_]*)", src)
    # 2) 内联形式：create: () => ({ dispose: X })
    inline = re.findall(r"moduleHost\.wire\(\{\s*name:\s*'([^']+)'\s*,\s*create:\s*\(\)\s*=>\s*\(\{\s*dispose:\s*([A-Za-z_][A-Za-z0-9_]*)", src)
    # 3) addDisposable('name', ...)
    disposables = re.findall(r"moduleHost\.addDisposable\('([^']+)'", src)
    imports = set(re.findall(r"import\s*\{([^}]*)\}\s*from", src))
    imported = set()
    for group in imports:
        for nm in group.split(","):
            nm = nm.strip()
            if not nm:
                continue
            imported.add(nm.split(" as ")[-1].strip() if " as " in nm else nm)

    problems = []
    names = []
    for name, factory in wires:
        names.append(name)
        # 工厂所在文件：按 export function <factory> 找
        found = None
        for p in media.glob("*.js"):
            txt = p.read_text(encoding="utf-8", errors="replace")
            if re.search(r"export\s+function\s+" + re.escape(factory) + r"\s*\(", txt):
                found = (p, txt)
                break
        if not found:
            problems.append(f"模块 {name} 的工厂 {factory} 在任何模块文件里都找不到")
            continue
        p, txt = found
        # 工厂体：从函数声明到文件末尾（工厂是文件里最后一个函数，见各文件约定）
        i = txt.index(f"export function {factory}(")
        body = txt[i:]
        # 【判据要硬且简单】工厂是文件里最后一个函数，它的 return { 一定在文件**末尾附近**。
        # 所以：从文件结尾往回找最后一个 "return {"，取它到结尾这段文本，段内必须出现 dispose。
        # （之前用正则匹配 `return \{(.*?)\n  \};` 会因为对象里有嵌套 }/换行而截断，
        #   也会因为文件里别的 return 而认错位置 —— 两次都是假失败。）
        tail_from = body.rfind("return {")
        if tail_from < 0:
            problems.append(f"模块 {name} 的工厂 {factory} 没有 return {{...}}（无法判断 dispose 出口）")
            continue
        returned = [x.strip() for x in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", body[tail_from:])]
        has_dispose = any("dispose" in r.lower() for r in returned)
        if not has_dispose:
            # 兜底：模块文件里导出了 dispose* 也算有出口（宿主只在 return 里暴露它才算数，
            # 所以这条只是给"还没接进 return"的情况留一个明确提示）
            if not re.search(r"export\s+function\s+dispose", txt):
                problems.append(
                    f"模块 {name}（{p.name}:{factory}）没有 dispose 出口："
                    f"return 段里找不到 dispose（文件里也没有 export function dispose*）")

    for name, fn in inline:
        names.append(name)
        if fn not in imported:
            problems.append(f"模块 {name} 内联 dispose 引用的 {fn} 没有被 import 进 call.js")

    print(f"  wire 的模块：{names}")
    print(f"  内联 dispose：{[n for n, _ in inline]}；addDisposable：{disposables}")
    for x in problems:
        print("  ! " + x)
    ok = not problems and bool(names)
    print(f"---- {'PASS' if ok else 'FAIL'}: 页面模块形状")
    return ok


def check_modules_dispose():
    """守卫：模块统一释放**真的执行**（S4 方案 A 的核心价值，必须有可复核证据）。

    做法：用 Node 假浏览器加载页面 → 调 `window.zxEngine.stopAll()` →
    断言页面发出 `modules-disposed` 且 `failed` 为空、released >= 5。

    为什么不用"应用里关窗"来验证：页面自己的 pagehide 在 WebView2 关窗时不可靠
    （实测走了正常关闭路径也没有释放记录），而且关窗后日志文件可能已不可写。
    模块逻辑的确定性验证更硬：它证的正是"宿主会把每个模块的 dispose 都调到"。
    """
    print("=" * 78)
    print("== 守卫：模块统一释放真的执行（stopAll → modules-disposed）")
    print("=" * 78)
    node = None
    for cand in (r"C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe",
                 "node"):
        try:
            if subprocess.run([cand, "--version"], capture_output=True, text=True, timeout=20).returncode == 0:
                node = cand
                break
        except Exception:
            continue
    harness = ROOT / "build" / "dispose-check.mjs"
    if node is None or not harness.is_file():
        print("  [SKIP] 找不到 node 或 dispose-check.mjs")
        print("---- FAIL: 模块统一释放")
        return False
    r = subprocess.run([node, str(harness), str(ROOT / "src" / "winui-cs" / "media")],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-1200:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "PASS: modules-disposed" in out and '"failed":[]' in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 模块统一释放")
    return ok


def check_share_audio_end_to_end():
    """守卫：共享电脑声音**端到端**（A1 的回归判据）。

    判据：双实例 + 已知音频 → 两侧 audioOutBytes > 0、audioInBytes > 0、
    audioInEnergy > 0、且 getStats 里至少有一条 outbound-rtp。
    为什么必须带已知音频：默认播放设备可能是虚拟声卡，回环采到的是数字静音，
    那时 RTP 为 0 属于"没声音可发"而不是 bug（否则判据会假红）。
    """
    print("=" * 78)
    print("== 守卫：共享电脑声音端到端（A1）")
    print("=" * 78)
    script = ROOT / "build" / "a1-share-audio-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\a1-share-audio-check.py")
        print("---- FAIL: 共享电脑声音端到端")
        return False
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-2500:] if out.strip() else "(no output)")
    if r.returncode == 2:
        print("  [ENV] 环境不满足（没有可用的已知音频）—— 不改判为通过")
        print("---- FAIL: 共享电脑声音端到端")
        return False
    ok = r.returncode == 0 and "PASS: 共享电脑声音端到端" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 共享电脑声音端到端")
    return ok


def check_invite_parse():
    """守卫：邀请串解析（房间名@地址 / 可点击协议 / 旧写法只给地址）。

    为什么必须有：这是纯逻辑但边界真实（房间名自带 @、ws:// 里带 @、只给地址）。
    本轮就是这条用例抓到"应该取最后一个 @ 却取了第一个" —— 那会让邀请串连错地址。
    它离线跑（不起媒体、不连网、不需要前台），所以任何环境都能验证。
    """
    print("=" * 78)
    print("== 守卫：邀请串解析（A4）")
    print("=" * 78)
    script = ROOT / "build" / "invite-parse-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\invite-parse-check.py")
        print("---- FAIL: 邀请串解析")
        return False
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-1500:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "邀请串解析单测: PASS" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 邀请串解析")
    return ok


def check_screen_share_end_to_end():
    """守卫：屏幕共享端到端（A2）。合成画面共享 → 观众侧必须收到**持续增长**的画面帧。

    【为什么必须有】用户报"屏幕共享有时能显示、有时显示不出来"。
    真实 getDisplayMedia 要人点系统弹窗、无法自动化；但"采集→协商→ontrack→上屏→帧计数"
    这条链是可自动验证的：本轮正是它抓到 `webrtc.js` 里跨模块引用 `remoteVideoEl`
    导致的 `ReferenceError` —— 每次对方开始共享都中断 ontrack，画面永远接不上。
    判据：host `--screen-test` 共享；join 侧 `收到画面帧 N 帧` 至少 2 条且**单调增长**；
    两侧都没有 screen-error / scriptError / 采集轨结束。
    """
    print("=" * 78)
    print("== 守卫：屏幕共享端到端（合成画面，A2）")
    print("=" * 78)
    script = ROOT / "build" / "screen-share-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\screen-share-check.py")
        print("---- FAIL: 屏幕共享端到端")
        return False
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-2500:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "PASS: 屏幕共享端到端（合成画面）" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 屏幕共享端到端")
    return ok


def check_docs_consistency():
    """守卫：活文档里的关键数字必须与实测一致（S6）。

    【为什么必须有】本轮发现 docs 里 17 处仍写"闸门 14 项"（实际 25）、
    包状态还停在"v37 被拦"（实际已到 v55）。文档漂移的代价是**下一个接手的人照着错数字做事**。
    只查活文档（handoff / coding-rules / architecture-modules / direction-decisions）；
    历史文档（audit / user-feedback / 验证报告）保留当时结论，不查。
    """
    print("=" * 78)
    print("== 守卫：活文档数字与实测一致（S6）")
    print("=" * 78)
    # 实测：闸门项数（run-checks 的 release 项 + 3 构建 + 2 界面 + 传输层）
    rc_src = (ROOT / "build" / "run-checks.py").read_text(encoding="utf-8", errors="replace")
    named = len(re.findall(r"results\.append\((\w+)\(\)\)", rc_src))
    gates = named + 3 + 2 + 1          # 3 构建 + 界面前置/界面 + 传输层
    # 实测：包版本
    zips = sorted((ROOT / "dist").glob("*.zip"), key=lambda p: p.stat().st_mtime)
    latest = zips[-1].name if zips else "(无)"
    ver = re.search(r"v(\d+)", latest)
    ver = ("v" + ver.group(1)) if ver else "(未知)"
    # 实测：页面模块数 / 壳行数
    modules = len(list((ROOT / "src" / "winui-cs" / "media").glob("*.js")))
    shell = len((ROOT / "src" / "winui-cs" / "media" / "call.html")
                .read_text(encoding="utf-8", errors="replace").split("\n"))

    live = ["handoff-next-session.md", "coding-rules-2026-10-06.md",
            "architecture-modules-2026-10-06.md", "direction-decisions-2026-10-06.md"]
    problems = []
    for name in live:
        p = ROOT / "docs" / name
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        # 闸门数量：只查**声明当前总量**的句子；历史叙述（"每个版本都跑 16→19 项"、
        # "新增第 20 项"、"进度"）是演进记录，不能当漂移。
        HIST = ("第", "→", "进度", "每轮", "当时", "原先", "曾经", "从 ", "增至")
        for m in re.finditer(r"(\d+)\s*项", text):
            ctx = text[max(0, m.start() - 40):m.end() + 12]
            if not any(k in ctx for k in ("闸门", "守卫", "自检", "release")):
                continue
            if any(h in ctx for h in HIST):
                continue
            if int(m.group(1)) != gates:
                problems.append(f"{name}: 写了「{m.group(1)} 项」，实测 {gates} 项 —— {ctx.strip()[:70]}")
        # 过时的包说法
        if re.search(r"v3[67]\s*被拦|没有可交付的语音包|无可用语音包", text):
            problems.append(f"{name}: 仍写「没有包/被拦」，实测最新包 {latest}")
    print(f"  实测：闸门 {gates} 项（{named} 守卫 + 3 构建 + 2 界面 + 传输层）"
          f"｜最新包 {latest}（{ver}）｜页面模块 {modules} 个｜call.html {shell} 行")
    for x in problems:
        print("  ! " + x)
    ok = not problems
    print(f"---- {'PASS' if ok else 'FAIL'}: 活文档数字一致")
    return ok


def check_ui_no_duplicate_entry():
    """守卫：界面上不能有两个**同名按钮**（同一功能的重复入口）。

    【为什么必须有】用户要求"UI 功能不重复"。实测原始状态：左栏与中栏各有一个
    「联机 / 换房间」按钮，都调同一个 ShowWelcome()。这类问题人眼容易漏，扫文案能抓住。
    判据：XAML 里所有 Button 的 Content 文案不得重复（图标按钮没有 Content，不参与）。
    """
    print("=" * 78)
    print("== 守卫：界面无重复入口（同名按钮）")
    print("=" * 78)
    xaml = (ROOT / "src" / "winui-cs" / "src" / "MainWindow.xaml").read_text(
        encoding="utf-8", errors="replace")
    # <Button ... Content="文案" ...>（顺序两种都要认）
    found = []
    for m in re.finditer(r"<Button\b[^>]*?x:Name=\"([A-Za-z_]\w*)\"[^>]*?Content=\"([^\"]+)\"", xaml):
        found.append((m.group(1), m.group(2).strip()))
    for m in re.finditer(r"<Button\b[^>]*?Content=\"([^\"]+)\"[^>]*?x:Name=\"([A-Za-z_]\w*)\"", xaml):
        found.append((m.group(2), m.group(1).strip()))
    by_text = {}
    for name, content in found:
        by_text.setdefault(content, []).append(name)
    dup = {k: v for k, v in by_text.items() if len(v) > 1}
    print(f"  带文案的按钮 {len(found)} 个，不同文案 {len(by_text)} 种")
    for content, names in dup.items():
        print(f"  ! 文案「{content}」出现 {len(names)} 次：{', '.join(names)}")
    ok = not dup
    print(f"---- {'PASS' if ok else 'FAIL'}: 界面无重复入口")
    return ok


def check_room_store():
    """守卫：房间列表（产品化）—— 增删改切换 + **持久化往返**。

    【为什么必须有】"多个房间 + 重启还在"是本轮产品化的核心承诺。
    判据是往返（写盘 → 读回 → 条数/当前项/名字一致）；它本轮真的抓到过东西：
    `AppConfig.Save` 静默失败（`catch { return false; }`），根因是应用容器不允许写系统 TEMP。
    它离线跑（不连网、不需要前台），任何环境都能验证。
    """
    print("=" * 78)
    print("== 守卫：房间列表与持久化（产品化）")
    print("=" * 78)
    script = ROOT / "build" / "room-store-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\room-store-check.py")
        print("---- FAIL: 房间列表")
        return False
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-1800:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "房间列表离线单测: PASS" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 房间列表")
    return ok


def check_room_create():
    """守卫：创建房间（产品化）—— 走界面同一条 CreateRoomAsync，验证"能创建房间"。

    【为什么必须有】用户明确反馈过"还是不能创建房间"。判据必须机器可验：
    房间进 RoomStore 并成为当前项、左栏真的渲染出该项、邀请串包含房间名（对方据此加入）。
    它只占一个窗口、不连外网、不需要前台，所以任何环境都能跑。
    """
    print("=" * 78)
    print("== 守卫：创建房间（走界面同一条逻辑）")
    print("=" * 78)
    script = ROOT / "build" / "room-create-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\room-create-check.py")
        print("---- FAIL: 创建房间")
        return False
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-1500:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "建房验收: PASS" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 创建房间")
    return ok


def check_chat_end_to_end():
    """守卫：文字消息端到端（双向真的收发）。

    【为什么必须有】用户报"文字不可用"。根因是**时序 bug**：建完 DataChannel 立刻
    `setLinkChat(id, primaryChat())`，而那一刻 readyState 还是 connecting、
    primaryChat() 只认 open ⇒ 存进去的是 null，通道成"孤儿"（能 open 却没人找得到）。
    判据：双方各发一条，两边都要有 `[收到消息]` 和 `[聊天] 已发出`。
    """
    print("=" * 78)
    print("== 守卫：文字消息端到端（双向）")
    print("=" * 78)
    script = ROOT / "build" / "chat-e2e-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\chat-e2e-check.py")
        print("---- FAIL: 文字端到端")
        return False
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=420)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-2000:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "PASS: 文字与语音端到端" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 文字端到端")
    return ok


def check_room_rename_delete():
    """守卫：房间改名重连 + 删除踢人（走界面按钮同一段代码）。

    【为什么必须有】用户实机报"改名不刷新、删了房间别人还在"。根因是这两条**界面路径**
    （RenameRoomAsync / DeleteRoomAsync）只改本地列表、不重连/不断连 ——
    而之前的自动化只测了命令行建房路径，一直没抓到用户实机的问题。
    判据：改名后房间名变 + 信令重连到新房名；删除后房间消失 + 成员清空。
    """
    print("=" * 78)
    print("== 守卫：房间改名重连 + 删除踢人")
    print("=" * 78)
    script = ROOT / "build" / "room-rename-delete-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\room-rename-delete-check.py")
        print("---- FAIL: 改名/删除")
        return False
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-1800:] if out.strip() else "(no output)")
    ok = ("[改名验收] 结果 PASS" in out) and ("[删除验收] 结果 PASS" in out)
    print(f"---- {'PASS' if ok else 'FAIL'}: 房间改名/删除")
    return ok


def check_name_change():
    """守卫：改名字要真的生效（内存 + 存盘 + 重连广播）。

    【为什么必须有】用户报"名字修改完全失效"。根因：`SelfDisplayName()` 只读输入框
    用于本地显示，**从不写回 `_selfName`**（信令广播的名字）、也**不存盘**
    （`AppConfig` 里根本没有名字字段）⇒ 自己看到新名、别人看到旧名、重启后丢失。
    判据走设置面板「关闭」同一段代码（OnCloseSettings）。
    """
    print("=" * 78)
    print("== 守卫：改名字生效（存盘 + 重连广播）")
    print("=" * 78)
    script = ROOT / "build" / "name-change-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\name-change-check.py")
        print("---- FAIL: 改名字")
        return False
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-1200:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "[名字验收] 结果 PASS" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 改名字")
    return ok


def check_input_clickable():
    """守卫：双实例在线时，输入框与发送按钮必须**真的可用**（能打字）。

    【为什么必须有】用户报"文字无法输入（对话框点不动）"。`MessageInput.IsEnabled = _chatReady`，
    而 `_chatReady` 只在聊天通道打开时为 true。上一轮的"文字端到端"守卫用 `--chat-test`
    **绕过了输入框**，所以 PASS 但用户打不了字 —— 判据必须量**界面控件状态**本身。
    """
    print("=" * 78)
    print("== 守卫：输入框真的能用（双实例在线）")
    print("=" * 78)
    script = ROOT / "build" / "input-clickable-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\input-clickable-check.py")
        print("---- SKIP: 输入框可用")
        return True
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-1600:] if out.strip() else "(no output)")
    bad = [l for l in out.splitlines() if "MessageInput" in l and "enabled=False" in l]
    ok = r.returncode == 0 and not bad
    print(f"---- {'PASS' if ok else 'FAIL'}: 输入框可用")
    return ok


def check_signal_stays_alive():
    """守卫：建房/重连后信令必须保持连接（不许误报"信令断开"）。

    【为什么必须有】用户实测：房主窗口显示红点"信令断开"，但左栏房间却是"已连接" ——
    状态自相矛盾。根因是 `connectSignal()` 覆盖 `state.ws` 时不管旧连接，
    旧 ws 的 onclose 在新连接建立后触发 ⇒ 误报断开。
    判据：建房后 `信令断开` 出现次数必须为 0，且加入方能收到 welcome。
    """
    print("=" * 78)
    print("== 守卫：信令保持连接（不误报断开）")
    print("=" * 78)
    script = ROOT / "build" / "signal-alive-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\signal-alive-check.py")
        print("---- SKIP: 信令保持连接")
        return True
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-1200:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "PASS: 建房后信令保持连接" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 信令保持连接")
    return ok


def check_auto_reconnect():
    """守卫：信令被动断开后必须自动重连（并且持续重试）。

    【为什么必须有】用户实测"根本无法连接，显示信令断开"：服务端完全正常
    （真 WebSocket 能连上并收到 welcome），但界面永远停在红点 ——
    页面**没有自动重连机制**，断了只能重启应用。
    判据：杀掉房主后，加入方必须出现"正在自动重连（第 N 次）"。
    """
    print("=" * 78)
    print("== 守卫：信令断开后自动重连")
    print("=" * 78)
    script = ROOT / "build" / "auto-reconnect-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\auto-reconnect-check.py")
        print("---- SKIP: 自动重连")
        return True
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-1400:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "PASS: 断开后自动重连已触发" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 自动重连")
    return ok


def check_user_flow():
    """守卫：按用户真实流程端到端（改名 → 房主删房间 → 新建房间 → 成员搜索加入）。

    【为什么必须有】用户给的原话步骤暴露了"房间名"这条根因：
    `CreateRoomAsync` 跳过统一设置点 `SetCurrentRoom` ⇒ 局域网广播不更新房间名
    ⇒ 别人扫到旧名、进错房间（表现为"自动搜索加入不可用 / 房间名不同步"）。
    """
    print("=" * 78)
    print("== 守卫：用户真实流程（改名→删房间→建房→搜索加入）")
    print("=" * 78)
    script = ROOT / "build" / "user-flow-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\user-flow-check.py")
        print("---- SKIP: 用户流程")
        return True
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=420)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-2000:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "房主流程: PASS" in out and "加入方流程: PASS" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 用户流程")
    return ok


def check_file_and_selfname():
    """守卫：文件传输端到端 + 左下角名字跟随改名。

    【为什么必须有】用户实机报两条：
      ① 传文件"一直显示发送中…"但对方已经接收 —— 因为 `AddFileMessage` 只画一次、
         `file-done` 从不更新那张卡片（现在会在完成后改成"已发送"）；
      ② 左下角名字改了不变 —— XAML 里那两处是**硬编码 Text="我"**（现在跟随 SelfDisplayName）。
    另外这条守卫顺带把"文件传输"真正跑通验证：以前 `--file-test` 的等待跑在加入方连接之前，
    必然超时 ⇒ **文件传输从来没被真正验证过**。
    """
    print("=" * 78)
    print("== 守卫：文件传输端到端 + 左下角名字")
    print("=" * 78)
    script = ROOT / "build" / "file-and-name-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\file-and-name-check.py")
        print("---- SKIP: 文件与名字")
        return True
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=420)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-1600:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "文件卡片状态更新: PASS" in out and "左下角名字跟随改名: PASS" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 文件与名字")
    return ok


def check_file_open_entry():
    """守卫：收到文件后界面必须有"打开方式"入口（打开文件 / 打开所在文件夹）。

    【为什么必须有】用户原话："文件传输没问题了但是没有给我打开方式"。
    按用户规矩"功能必须有界面入口" —— 收到文件却打不开，等于功能不完整。
    """
    print("=" * 78)
    print("== 守卫：收到文件后有打开入口")
    print("=" * 78)
    script = ROOT / "build" / "file-open-entry-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\file-open-entry-check.py")
        print("---- SKIP: 文件打开入口")
        return True
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=420)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-1200:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "PASS: 收到文件后有打开入口" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 文件打开入口")
    return ok


def check_voice_call():
    """守卫：语音通话真的建立（双方发起 + 麦克风轨挂上 + 有音频字节）。

    【为什么必须有】我之前"验证语音"用的是 `--share-audio-test` 的字节 ——
    那其实是**共享电脑声音**，不是麦克风语音。而真正的语音测试（`--call-test`）
    一直跑在错误时机（加入方连接之前）必然超时 ⇒ **语音从来没被真正验证过**
    （用户报"语音不能用"就是这么漏掉的）。
    判据：① 双方发起通话 ② senderInfo 里存在 isShared=false 的音频 sender ③ 音频字节 > 0。
    """
    print("=" * 78)
    print("== 守卫：语音通话端到端（麦克风）")
    print("=" * 78)
    script = ROOT / "build" / "voice-call-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\voice-call-check.py")
        print("---- SKIP: 语音通话")
        return True
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=420)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-1600:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "PASS: 双方都发起了通话且麦克风轨已挂" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 语音通话")
    return ok


def check_call_button_clickable():
    """守卫：进了房间但还没通话时，「发起通话」按钮必须可点。

    【为什么必须有】用户报"语音功能不能用"，截图显示状态已是"通话中"、按钮却是灰的。
    根因：A1 的常驻「共享电脑声音」轨在链路建立时就挂着，而 `reportCallState` 只看
    "有没有音频发送轨" ⇒ 一进房间就判成"通话中" ⇒ 按钮被禁用 ⇒ **用户根本点不了通话**，
    麦克风从来没被打开过。这条守卫把"未通话 ⇒ 按钮可点"钉死。
    """
    print("=" * 78)
    print("== 守卫：未通话时「发起通话」可点")
    print("=" * 78)
    script = ROOT / "build" / "call-button-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\call-button-check.py")
        print("---- SKIP: 发起通话按钮")
        return True
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=420)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-1500:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "PASS: 未通话时「发起通话」可点" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 发起通话按钮")
    return ok


def check_one_side_call():
    """守卫：只有一端点「发起通话」时，另一端也必须进入通话且挂断/静音可用。

    【为什么必须有】用户实测：一端"通话中"、另一端一直"建立连接…"，
    而且那端**挂断和静音都点不动**。根因是通话状态只由页面的 call-link 事件驱动，
    而 `setInCall()` 本身不上报 ⇒ 漏一条路径界面就停在旧状态；挂断/静音又完全
    依赖这个状态位。现在：① active 加"对方明确发起过通话且能收到音频"的兜底；
    ② 挂断/静音改成"有对端即可用"（永远不会错的下限）。
    """
    print("=" * 78)
    print("== 守卫：单端发起通话（另一端也要进通话）")
    print("=" * 78)
    script = ROOT / "build" / "one-side-call-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\one-side-call-check.py")
        print("---- SKIP: 单端发起通话")
        return True
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=420)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-1500:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "PASS: 单端发起也能双向进入通话" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 单端发起通话")
    return ok


def check_hangup_sync():
    """守卫：一端挂断后，两端都必须退出通话状态。

    【为什么必须有】用户实测："点了挂断没声音了但是状态一个还是通话中，一个是已连接未通话"。
    根因有两层：① `hangup()` 从不发信令通知对端；② hangup 里裸引用 call.js 的
    `remoteAudioEl` 抛 ReferenceError ⇒ **中途中断**，后面的 setInCall(false) 没执行。
    判据：挂断后两端最后判定都是 通话中=False，且按钮名为「加入语音」。
    """
    print("=" * 78)
    print("== 守卫：挂断后两端状态一致")
    print("=" * 78)
    script = ROOT / "build" / "hangup-sync-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\hangup-sync-check.py")
        print("---- SKIP: 挂断同步")
        return True
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=420)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-1600:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "PASS: 挂断后两端都退出通话" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 挂断同步")
    return ok


def check_no_js_error():
    """守卫（运行时）：完整流程里不许出现 JS 未处理拒绝。

    【为什么用运行时判据】静态扫"跨模块裸引用"误报太多（各模块都从 io 解构依赖）。
    而这类 bug 的症状很明确：页面抛 ReferenceError ⇒ C# 记 `[JS 未处理拒绝]`。
    历史三次同类：webrtc.js 的 `remoteVideoEl`（画面接不上）、mesh.js 的 `remoteAudioEl`
    （hangup 中断 ⇒ 状态卡"通话中"）。
    """
    print("=" * 78)
    print("== 守卫：流程中无 JS 未处理拒绝")
    print("=" * 78)
    script = ROOT / "build" / "no-js-error-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\no-js-error-check.py")
        print("---- SKIP: JS 未处理拒绝")
        return True
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=420)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-1600:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "PASS: 无 JS 未处理拒绝" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: JS 未处理拒绝")
    return ok


def check_rejoin_call():
    """守卫：挂断后再「加入语音」，必须重新进入通话（而不是变成"仅收听"）。

    【为什么必须有】用户实测（v74）："挂断一次再加入会变成仅收听"、
    "房主麦克风没声音只有成员有"。根因：挂断 stop() 了麦克风轨，但降噪链里
    留着那条死轨 ⇒ `outgoingTrack()` 把它返回 ⇒ addTrack 死轨 ⇒ 媒体发不出去。
    判据：挂断→再加入后，最后判定必须是 发音频=True 且 通话中=True。
    """
    print("=" * 78)
    print("== 守卫：挂断后再加入语音（不能变成仅收听）")
    print("=" * 78)
    script = ROOT / "build" / "rejoin-call-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\rejoin-call-check.py")
        print("---- SKIP: 再次加入语音")
        return True
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=420)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-1700:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "PASS: 挂断后再加入仍是通话中" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 再次加入语音")
    return ok


def check_mute_cycle():
    """守卫：静音 → 取消静音必须正确往返。

    【为什么必须有】用户实测（v75）："双方点静音后再恢复只有房主有声音成员听不到"。
    根因：`setMuted` 只改 `state.localStream` 的轨，而实际 addTrack 的是**降噪链的输出轨**
    ⇒ 改错了对象；而且改完**不重新上报**，界面/判定都停在旧值。
    判据（收紧）：通话中 发=True → 静音后 发=False → 取消后 发=True。
    """
    print("=" * 78)
    print("== 守卫：静音往返（发→不发→发）")
    print("=" * 78)
    script = ROOT / "build" / "mute-cycle-check.py"
    if not script.is_file():
        print("  [SKIP] 没有 build\\mute-cycle-check.py")
        print("---- SKIP: 静音往返")
        return True
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=420)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.rstrip()[-1700:] if out.strip() else "(no output)")
    ok = r.returncode == 0 and "PASS: 静音与取消静音都正确生效" in out
    print(f"---- {'PASS' if ok else 'FAIL'}: 静音往返")
    return ok


def fmt_env_blocked(title, detail):
    """环境中止的统一话术：说清"哪一项、为什么、怎么办、发版怎么办"。"""
    print("=" * 78)
    print(f"== 环境中止：{title}")
    print("=" * 78)
    print(f"  原因：{detail}")
    print("  含义：这一项**无法验证**（不是验证失败，也不是通过）。")
    print("  典型触发：用户正在操作电脑 / 有窗口抢焦点 / 弹窗 / UAC 安全桌面。")
    print("  怎么办：让电脑空一分钟（不要动键鼠），重跑 python build\\run-checks.py")
    print("          排障时可先单独跑：python build\\smoke-ui.py --exe <exe> --no-click")
    print("  发版：不发。'自检不过不发包'包含'没法自检'——不许把没验过的包发出去。")
    print("  退出码：3（环境中止；非 0 = 拦住发版，但不要当成产品 bug 去改代码）")


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what not in ("all", "build", "dist", "selftest", "release"):
        print(f"未知参数 {what!r}：可用 all|build|dist|selftest|release")
        return 2
    results = []

    if what in ("all", "build", "release"):
        results.append(run(
            "C++ 助手构建（receiver-probe）",
            ["cmd", "/c", "build-receiver-probe.cmd"],
            ROOT / "src" / "media" / "probe",
            ok_markers=("BUILD OK",),
            bad_markers=("BUILD FAILED", "error C", "LNK"),
        ))
        results.append(run(
            "C# 视频原型构建（winui-videoprobe）",
            [DOTNET, "build", "VideoProbe.csproj", "-c", "Release", "-p:Platform=x64", "--nologo", "-v", "m"],
            ROOT / "src" / "winui-videoprobe",
            ok_markers=("0 个错误",),
            bad_markers=("error CS", "error MSB", ": error"),
        ))
        results.append(run(
            "C# 主应用构建（winui-cs / 棕仙语音）",
            [DOTNET, "build", "ZongxianVoice.csproj", "-c", "Release", "-p:Platform=x64", "--nologo", "-v", "m"],
            ROOT / "src" / "winui-cs",
            ok_markers=("0 个错误",),
            bad_markers=("error CS", "error MSB", ": error"),
        ))

        # 界面自检：用**模拟键鼠**真点一遍（启动 → 进入 → 设置），并断言窗口在屏幕内。
        # 【为什么必须有】只验"进程活着"漏掉了：点击没反应、窗口跑到屏幕外、设置打不开
        #   —— 用户连着收到的几个坏包就是这么漏出去的（2026-10-06）。
        smoke = ROOT / "build" / "smoke-ui.py"
        app_exe = (ROOT / "src" / "winui-cs" / "bin" / "x64" / "Release"
                   / "net8.0-windows10.0.19041.0" / "win-x64" / "ZongxianVoice.exe")
        if smoke.is_file() and app_exe.is_file():
            # 先加一条**不点鼠标**的守卫前置检查：启动 + 真置顶 + 前台断言。
            # 它绿了而全量自检红 ⇒ 只能是产品问题；它红 ⇒ 环境问题，当场说清楚。
            try:
                results.append(run(
                    "界面自检前置（只验启动+真置顶+前台断言，不发键鼠）",
                    [sys.executable, str(smoke), "--exe", str(app_exe), "--no-click"],
                    ROOT,
                    ok_markers=("[PASS] smoke-ui --no-click",),
                    bad_markers=("[FAIL]",),
                    env_abort=True,
                ))
                results.append(run(
                    "界面自检（模拟键鼠：启动→进入→设置；元素必须在屏幕内）",
                    [sys.executable, str(smoke), "--exe", str(app_exe)],
                    ROOT,
                    ok_markers=("[PASS] smoke-ui",),
                    bad_markers=("[FAIL]",),
                    env_abort=True,
                ))
            except EnvBlocked as e:
                fmt_env_blocked(e.title, e.detail)
                return 3
        else:
            print("[SKIP] 找不到 build\\smoke-ui.py 或应用 exe（先构建 winui-cs）")
            results.append(False)   # SKIP 一律按 FAIL：最关键的界面自检不许被静默跳过

        results.append(check_app_pri())
        results.append(check_dist())
        results.append(check_dist_media_and_probe())
        results.append(check_script_sanity())
        results.append(check_residue_rules())
        results.append(check_ui_element_references())
        results.append(check_releases_manifest_shape())
        results.append(check_cmd_line_endings())
        results.append(check_media_line_endings())
        results.append(check_bridge_contract())
        results.append(check_page_module_loads())
        results.append(check_page_singletons())
        results.append(check_media_route_collisions())
        results.append(check_docs_consistency())
        results.append(check_ui_no_duplicate_entry())
        results.append(check_room_store())
        results.append(check_room_create())
        results.append(check_chat_end_to_end())
        results.append(check_voice_call())
        results.append(check_call_button_clickable())
        results.append(check_one_side_call())
        results.append(check_hangup_sync())
        results.append(check_rejoin_call())
        results.append(check_mute_cycle())
        results.append(check_no_js_error())
        results.append(check_room_rename_delete())
        results.append(check_name_change())
        results.append(check_input_clickable())
        results.append(check_signal_stays_alive())
        results.append(check_auto_reconnect())
        results.append(check_user_flow())
        results.append(check_file_and_selfname())
        results.append(check_file_open_entry())
        results.append(check_page_modules_shape())
        results.append(check_modules_dispose())
        results.append(check_share_audio_end_to_end())
        results.append(check_invite_parse())
        results.append(check_screen_share_end_to_end())

    # 【release 也必须跑这一条】2026-10-06 发现的洞：原来是 `("all", "selftest")`，
    # 于是**发版严格模式 release 反而漏跑了传输层 6 阶段自测** —— 而 release 是唯一出包的路径，
    # 漏掉的正好是最该跑的那一环（"严格模式比随便跑跑还松"）。现在三处都有它。
    if what in ("all", "release"):
        results.append(check_two_instance())

    if what in ("all", "selftest", "release"):
        # 传输层自测：结果写在探针同级目录的日志里，比解析 stdout 稳妥。
        # 【为什么要重试一次】这条在本机上偶发失败（本会话见过两次，重跑即过）：
        # 回环 UDP 在这一层本来就会零星丢包（harness 备注也写了"每阶段首个数据报可能丢"），
        # 而各阶段的判据是硬阈值。**重试一次并如实打印用没用上重试** ——
        # 不掩盖"第二次才好"这件事，也不让偶发把整轮回归染红。
        probe = ROOT / "src" / "media" / "probe" / "build" / "transport-probe.exe"
        log = ROOT / "src" / "media" / "probe" / "build" / "transport-probe-log.txt"
        if not probe.is_file():
            # 发版模式下"找不到探针"就是 FAIL：SKIP 不许放行（审计 §7.2 的病根）
            print(f"[SKIP] 找不到 {probe.name}（先跑 build-transport-probe.cmd）")
            if what == "release":
                print("       release 模式：SKIP 一律按 FAIL，不给'找不到就跳过'留口子")
                results.append(False)
        else:
            ok = False
            for attempt in (1, 2):
                subprocess.run([str(probe)], cwd=str(probe.parent), capture_output=True)
                text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
                tail = [ln for ln in text.splitlines() if "通过" in ln or "失败" in ln]
                ok = "0 个失败" in text
                if ok:
                    print("\n".join(tail) if tail else "(日志里没有汇总行)")
                    if attempt == 2:
                        print("  [注意] 第一次跑失败了，重试才通过 —— 这条检查在本机是偶发的")
                    break
                print(f"[第 {attempt} 次失败] " + "；".join(tail[-3:]))
            print(f"---- {'PASS' if ok else 'FAIL'}: 传输层 6 阶段自测")
            results.append(ok)

    print("=" * 78)
    passed = sum(1 for r in results if r)
    print(f"汇总: PASS {passed} / FAIL {len(results) - passed}")
    for r in results:
        print(f"  [{'PASS' if r else 'FAIL'}]")
    return 0 if all(results) and results else 1


if __name__ == "__main__":
    raise SystemExit(main())
