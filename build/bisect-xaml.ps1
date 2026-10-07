# bisect-xaml.ps1 —— 系统化二分 MainWindow.xaml，找出让 XamlCompiler 崩溃的位置
#
# 为什么需要脚本：手工反复"改文件→构建→恢复"太慢且容易出错（本会话已经因为
# 手工重排把 XAML 改坏过两次）。这个脚本把"截断 + 补闭合 + 构建"固定下来，
# 每次只回答一个问题：前 N 行是否可用。
#
# 用法: pwsh -File build\bisect-xaml.ps1 -N 200

param(
    [Parameter(Mandatory = $true)][int]$Take,
    [string]$File = 'src\winui-cs\src\MainWindow.xaml'
)

$ErrorActionPreference = 'Stop'

$full = [System.IO.File]::ReadAllLines($File, [System.Text.Encoding]::UTF8)
if ($Take -gt $full.Count) { $Take = $full.Count }

# 备份
$bak = "$env:TEMP\MW.bisect.bak"
if (-not (Test-Path $bak)) {
    Copy-Item $File $bak -Force
    Write-Host "已备份到 $bak"
}

# 取前 N 行，剥掉可能被截断在中间的注释，然后补上闭合标签
$head = $full[0..($Take - 1)]

# 若尾部处于未闭合注释中，补一个 -->
$joined = $head -join "`n"
$lastOpen = $joined.LastIndexOf('<!--')
$lastClose = $joined.LastIndexOf('-->')
if ($lastOpen -gt $lastClose) { $head += ' -->' }

# 补齐闭合：数一下截断处的开闭标签差（粗略，够用）
$tail = @(
    '        </Grid>',
    '    </Grid>',
    '</Window>'
)
$out = @()
$out += $head
$out += $tail
[System.IO.File]::WriteAllLines($File, $out, (New-Object System.Text.UTF8Encoding($false)))

# 语法校验
try {
    $settings = New-Object System.Xml.XmlReaderSettings
    $reader = [System.Xml.XmlReader]::Create((Resolve-Path $File).Path, $settings)
    while ($reader.Read()) { }
    $reader.Close()
    Write-Host "前 $Take 行: XML 可解析"
}
catch {
    Write-Host "前 $Take 行: XML 不可解析（$($_.Exception.Message.Split([char]10)[0])）—— 跳过构建"
    Copy-Item $bak $File -Force
    return
}

# 构建
Get-Process -Name 'ZongxianVoice' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Remove-Item 'src\winui-cs\obj' -Recurse -Force -ErrorAction SilentlyContinue
$build = cmd /c "build\build-winui-cs.cmd 2>&1" | Out-String

# 恢复完整文件
Copy-Item $bak $File -Force

$hasXamlCrash = $build -match 'XamlCompiler\.exe.*已退出'
$hasCsharpErr = $build -match 'error CS'

if ($hasXamlCrash) {
    Write-Host "前 $Take 行: ✗ XamlCompiler 崩溃 —— 问题在这 $Take 行之内"
    return
}
elseif ($hasCsharpErr) {
    Write-Host "前 $Take 行: ✓ XAML 编译通过（只有 C# 找不到控件名的预期错误）"
    return
}
elseif ($build -match 'Build OK') {
    Write-Host "前 $Take 行: ✓ 完整构建通过"
    return
}
else {
    Write-Host "前 $Take 行: ? 结果不明"
    return
}
