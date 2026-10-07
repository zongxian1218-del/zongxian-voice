"""唯一发版入口 —— 任何手工打包 / 手工改清单都视为违规。

为什么必须有它（2026-10-06 审计结论）：
  守卫与打包原本是两个互不调用的脚本，守卫 FAIL 不影响出包；run-checks 里 SKIP 不计失败；
  BUILD-INFO 由一次性脚本手写。结果是"自检不过不发包"这条规矩**在机制上没有任何强制力** ——
  实际发生的是：dist 里的页面比源码旧、包里混进测试残留，而包照样发出去了。

本脚本把顺序钉死（任何一步不过就停，不产出 zip）：
  1 构建（引擎 + 应用）—— 失败即停
  2 同步媒体资源（Python 做，不依赖 cmd 里那段不可靠的拷贝）
  3 写 BUILD-INFO（含 sources，由构建写，不允许手写）
  4 **跑守卫**（SKIP 一律按 FAIL）—— 在打包之前
  5 打包（复用 make-voice-package.py，它是唯一打包实现）
  6 **回读校验**：zip 内每个条目与 dist\\winui 对应文件**逐字节相同**（此前缺的就是这一环）
  7 登记 RELEASES（zip 名 / sha256 / 条目数 / 时间），旧包挪进隔离区
  8 打印汇总

用法： python build\\release.py --version 37
"""
import argparse
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import time
import zipfile

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable
DIST = ROOT / 'dist'
VOICE_DIST = DIST / 'winui'          # 打包源
QUARANTINE = ROOT / 'build' / ('dist-quarantine-' + time.strftime('%Y-%m-%d'))

# 发版时**必须**存在于 dist\winui 的关键文件（缺一个就停）
REQUIRED = ['ZongxianVoice.exe', 'ZongxianVoice.dll', 'resources.pri',
            'media/call.html', 'media/share-audio-worklet.js',
            'tools/zxprobe.exe', 'BUILD-INFO.txt']


def say(msg):
    print(msg, flush=True)


def run(argv, cwd=None, shell=False):
    """跑一条命令并把输出透传；返回退出码。"""
    p = subprocess.run(argv, cwd=str(cwd or ROOT), shell=shell)
    return p.returncode


def git_head():
    for cand in (r'D:\BuildTools\MinGit\cmd\git.exe', 'git'):
        try:
            r = subprocess.run([cand, 'rev-parse', '--short', 'HEAD'], cwd=str(ROOT),
                               capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                return r.stdout.strip()
        except Exception:
            continue
    return 'unknown'


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def step_build():
    say('\n=== [1/7] 构建 ===')
    for script in ('build-media.cmd', 'build-winui-cs.cmd'):
        say('  -> ' + script)
        rc = run(['cmd', '/c', str(ROOT / 'build' / script)])
        if rc != 0:
            say('  ✗ %s 退出码 %d —— 停止发版' % (script, rc))
            return False
    return True


def step_sync_media():
    say('\n=== [2/7] 同步媒体资源（页面/worklet 必须与源码同一份） ===')
    src = ROOT / 'src' / 'winui-cs' / 'media'
    dst = VOICE_DIST / 'media'
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in src.glob('*'):
        if f.is_file():
            shutil.copy2(f, dst / f.name)
            n += 1
    say('  已同步 %d 个文件到 dist\\winui\\media' % n)
    # 探针也必须同版本（应用实际调用的是 tools\ 里那份）
    probe = DIST / 'zxprobe.exe'
    if probe.is_file():
        (VOICE_DIST / 'tools').mkdir(parents=True, exist_ok=True)
        shutil.copy2(probe, VOICE_DIST / 'tools' / 'zxprobe.exe')
        say('  探针已同步：dist\\winui\\tools\\zxprobe.exe')
    return True


def step_build_info():
    say('\n=== [3/7] 写 BUILD-INFO（由发版入口写，禁止手写） ===')
    head = git_head()
    (VOICE_DIST / 'BUILD-INFO.txt').write_text(
        'commit: %s\n' % head +
        'built:  %s\n' % time.strftime('%Y/%m/%d %H:%M:%S') +
        'config: Release / x64 / net8.0-windows10.0.19041.0 / win-x64\n' +
        'rebuild: build\\release.py（唯一发版入口）\n' +
        'sources: src/winui-cs, src/media\n',          # 两边都算依赖，改任一边都算陈旧
        encoding='utf-8')
    say('  commit=%s' % head)
    missing = [f for f in REQUIRED if not (VOICE_DIST / f).exists()]
    if missing:
        say('  ✗ dist\\winui 缺关键文件：%s —— 停止发版' % ', '.join(missing))
        return False
    return True


def step_guards():
    say('\n=== [4/7] 跑守卫（SKIP 一律按 FAIL） ===')
    rc = run([PY, str(ROOT / 'build' / 'run-checks.py'), 'release'])
    if rc != 0:
        say('  ✗ 守卫未通过（退出码 %d）—— 不产出 zip' % rc)
        return False
    return True


def step_package(version):
    say('\n=== [5/7] 打包 v%s ===' % version)
    env = dict(os.environ)
    env['ZX_RELEASE_ENTRY'] = '1'        # make-voice-package.py 只认这个入口
    p = subprocess.run([PY, str(ROOT / 'build' / 'make-voice-package.py'),
                        '--version', str(version)], cwd=str(ROOT), env=env)
    return p.returncode == 0


def step_verify_zip(version):
    say('\n=== [6/7] 回读校验：zip 载荷与 dist\\winui 逐字节一致 ===')
    zp = DIST / ('同频-测试版-v%s.zip' % version)
    if not zp.is_file():
        say('  ✗ 找不到刚生成的 zip')
        return False
    bad = []
    with zipfile.ZipFile(zp) as z:
        names = [n for n in z.namelist() if not n.endswith('/')]
        for n in names:
            f = VOICE_DIST / n
            if not f.is_file():
                bad.append('zip 里有、dist 里没有：' + n)
                continue
            if hashlib.sha256(z.read(n)).hexdigest() != sha256_of(f):
                bad.append('内容不一致：' + n)
    for f in REQUIRED:
        if f not in names:
            bad.append('zip 缺关键文件：' + f)
    if bad:
        say('  ✗ 校验失败 %d 项：' % len(bad))
        for b in bad[:10]:
            say('    - ' + b)
        return False
    say('  ✓ %d 个条目全部与 dist 一致，关键文件齐' % len(names))
    return len(names)


def step_register(version, entries, zp):
    say('\n=== [7/7] 登记 RELEASES + 隔离旧包 ===')
    # 旧包挪走（策略：挪，不删）
    QUARANTINE.mkdir(parents=True, exist_ok=True)
    for old in DIST.glob('同频-测试版-v*.zip'):
        if old.name != zp.name:
            shutil.move(str(old), str(QUARANTINE / old.name))
            say('  旧包已隔离：' + old.name)
    rel = DIST / 'RELEASES.md'
    text = rel.read_text(encoding='utf-8') if rel.is_file() else '# dist 发布物清单\n'
    entry = ('| `%s` | 语音测试版 | %.1f MB | %s | sha256 `%s` | %d 条目 |\n'
             % (zp.name, zp.stat().st_size / 1048576, time.strftime('%Y-%m-%d %H:%M'),
                sha256_of(zp)[:16], entries))
    lines = text.splitlines()
    # 【2026-10-06 修】以前把带 `<!-- RELEASE-ENTRY -->` 标记的登记行 append 到**文件末尾**，
    # 结果是**孤立表格行**（表外一行 `| … |`），run-checks 的 RELEASES 结构守卫立刻 FAIL
    # （v37 发版后实测抓到：第 118 行孤立）。现在直接写进"发布物"那张表里。
    lines, old_marker = _upsert_table_row(lines, '| 名称 |', zp.name, entry.rstrip('\n')), None
    lines = [ln for ln in lines if ln.strip() != '<!-- RELEASE-ENTRY -->']
    rel.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    say('  已登记：%s（写进「发布物」表；sha256 与条目数都在里头）' % zp.name)
    return True


def _table_bounds(lines, header_match):
    """找一张 markdown 表格的 [表头行, 数据行结束) 半开区间；找不到返回 None。

    【为什么需要】2026-10-06 实测：规范化步骤把新产物行 append 到**文件末尾**，
    而末尾是正文段落 —— 于是登记行成了孤立表格行（markdown 渲染错乱），
    守卫却因为"文件名出现过"而 PASS。名字对、位置错，只能靠结构修。
    """
    for i, ln in enumerate(lines):
        if header_match in ln and ln.strip().startswith("|"):
            j = i + 1
            if j < len(lines) and set(lines[j].strip()) <= set("|-: \t") and "|" in lines[j]:
                j += 1
                while j < len(lines) and lines[j].strip().startswith("|"):
                    j += 1
                return i, j
    return None


def _upsert_table_row(lines, header_match, name, row_text):
    """把 `name` 的登记行放进指定表格：已存在同名数据行就替换，否则插入表尾。"""
    b = _table_bounds(lines, header_match)
    if not b:
        return lines + ["", row_text]
    start, end = b
    for k in range(start + 2, end):
        if ("`%s`" % name) in lines[k]:
            lines[k] = row_text
            return lines
    lines.insert(end, row_text)
    return lines


def step_normalize_dist():
    """把 dist 规范化到"守卫能通过"的状态。这一步也归发版入口，
    因为"靠人记得清理 dist"正是过去失败的模式（审计 §7.2）。

    做两件事：
      1) 引擎构建副产物（zongxian_media.dll）不该躺在 dist 根：应用调用的是
         tools\\zxprobe.exe（自包含），这个 dll 没有消费者 → 挪进隔离区
      2) 清单里指向"已经不在 dist 的包"的旧行 → 删掉（清单必须与 dist 事实一致）
    """
    say('\n=== [0/7] 规范化 dist（挪副产物 + 清单对齐事实） ===')
    QUARANTINE.mkdir(parents=True, exist_ok=True)
    for junk in ('zongxian_media.dll',):
        p = DIST / junk
        if p.is_file():
            shutil.move(str(p), str(QUARANTINE / junk))
            say('  副产物已隔离：%s（应用用 tools\\zxprobe.exe，不需要它）' % junk)

    rel = DIST / 'RELEASES.md'
    if not rel.is_file():
        rel.write_text('# dist 发布物清单\n\n', encoding='utf-8')
    text = rel.read_text(encoding='utf-8')
    lines = text.splitlines()
    out, dropped = [], []
    import re as _re
    for ln in lines:
        m = _re.search(r'`([^`]+\.(?:zip|exe|dll))`', ln)
        if m:
            name = m.group(1)
            if not (DIST / name).is_file():
                dropped.append(name)
                continue
        out.append(ln)
    # dist 根下真实存在的产物，必须在清单里有名字（守卫按"名字是否出现"检查）
    # 【2026-10-06 修】原来是把行 append 到文件末尾 —— 末尾是正文，登记行变成**孤立表格行**
    # （守卫当时因为"名字出现过"而 PASS，属于"名字对、位置错"）。现在写进真正的表格里。
    for f in sorted(DIST.iterdir()):
        if f.is_file() and f.suffix.lower() in ('.exe', '.zip', '.dll'):
            if ('`%s`' % f.name) not in '\n'.join(out):
                row = ('| `%s` | 引擎/工具产物 | %.2f MB | %s | 由 build\\release.py 规范化 |'
                       % (f.name, f.stat().st_size / 1048576, time.strftime('%Y-%m-%d %H:%M')))
                out = _upsert_table_row(out, '| 名称 |', f.name, row)
                dropped.append('(登记) ' + f.name)
    rel.write_text('\n'.join(out) + '\n', encoding='utf-8')
    if dropped:
        say('  清单已对齐：%s' % ', '.join(dropped[:6]))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--version', required=True)
    a = ap.parse_args()

    say('发版入口 build\\release.py —— 唯一允许的打包路径')
    if not step_build():
        return 1
    # 规范化必须在**构建之后**：build-media.cmd 每次都会把 zongxian_media.dll 丢进 dist 根
    if not step_normalize_dist():
        return 1
    if not step_sync_media():
        return 1
    if not step_build_info():
        return 1
    if not step_guards():
        return 1
    if not step_package(a.version):
        say('  ✗ 打包失败')
        return 1
    entries = step_verify_zip(a.version)
    if not entries:
        return 1
    if not step_register(a.version, entries, DIST / ('同频-测试版-v%s.zip' % a.version)):
        return 1
    say('\n=== 发版完成：dist\\同频-测试版-v%s.zip（%d 条目，已与 dist 逐字节校验）===' %
        (a.version, entries))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
