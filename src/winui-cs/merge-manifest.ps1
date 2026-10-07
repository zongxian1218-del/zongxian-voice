# merge-manifest.ps1 —— 把 app.manifest 与 WindowsAppSDK.manifest 合并
#
# 为什么需要这一步：
#   非打包（unpackaged）的 WinUI 3 应用靠**免注册 WinRT 激活**工作 ——
#   即把 WindowsAppSDK 提供的 activatableClass 列表作为 manifest 嵌入 exe。
#   正常情况下这件事由 Visual Studio 的 MSIX/Appx 打包任务在构建时自动完成。
#   但我们关掉了 EnableCoreMrtTooling（因为那套任务需要 VS 专有程序集，
#   见 BUILD-NOTES.md），连带把 manifest 的合并也关掉了。
#
#   结果：产物里没有嵌入激活清单 → WinRT 类型激活不了 →
#   Application.LoadComponent 抛 XamlParseException（HRESULT 0x802B000A）→
#   进程以 0xC000027B 静默退出。
#
# 这个脚本把两份 manifest 合进一个 <assembly> 根：
#   · app.manifest          → DPI 感知、执行级别（asInvoker）
#   · WindowsAppSDK.manifest → 全部 activatableClass / comClass / proxyStub
#
# 用法（由 build-winui-cs.cmd 调用）：
#   pwsh -File merge-manifest.ps1 -App <app.manifest> -Sdk <WindowsAppSDK.manifest> -Out <merged.manifest>

param(
    [Parameter(Mandatory = $true)][string]$App,
    [Parameter(Mandatory = $true)][string]$Sdk,
    [Parameter(Mandatory = $true)][string]$Out
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $App)) { throw "找不到 $App" }
if (-not (Test-Path $Sdk)) { throw "找不到 $Sdk" }

# 用 XML DOM 合并，比字符串拼接可靠：能保证命名空间与层级都合法。
# PreserveWhitespace=false 让输出更紧凑（110 KB 的 manifest 会明显变小）。
[xml]$appDoc = New-Object System.Xml.XmlDocument
$appDoc.PreserveWhitespace = $false
$appDoc.Load($App)

[xml]$sdkDoc = New-Object System.Xml.XmlDocument
$sdkDoc.PreserveWhitespace = $false
$sdkDoc.Load($Sdk)

$asmNs = 'urn:schemas-microsoft-com:asm.v1'
$newDoc = New-Object System.Xml.XmlDocument
$newDoc.PreserveWhitespace = $false

# 以 SDK 的根为基底：它已经声明了 asmv1 / asmv3 / winrtv1 三个命名空间，
# 而 WinRT 激活部分占了绝大多数内容，保留它最省事。
$root = $newDoc.ImportNode($sdkDoc.DocumentElement, $true)
$newDoc.AppendChild($root) | Out-Null

# SDK manifest 里没有 assemblyIdentity；补一个（部分加载器要求它存在）
if (-not $root.SelectSingleNode("//*[local-name()='assemblyIdentity']")) {
    $id = $newDoc.CreateElement('assemblyIdentity', $asmNs)
    $id.SetAttribute('version', '1.0.0.0')
    $id.SetAttribute('name', 'ZongxianVoice.app')
    $root.InsertBefore($id, $root.FirstChild) | Out-Null
}

# 把 app.manifest 里的实质内容逐个并入（跳过根元素与 assemblyIdentity，
# 那两项已经有或已补）。
foreach ($child in $appDoc.DocumentElement.ChildNodes) {
    if ($child.NodeType -ne 'Element') { continue }
    $local = $child.LocalName
    if ($local -eq 'assemblyIdentity') { continue }
    # trustInfo / compatibility / application 三类是本项目自己的声明，
    # 不与 SDK manifest 冲突，直接搬过来。
    $imported = $newDoc.ImportNode($child, $true)
    $newDoc.DocumentElement.AppendChild($imported) | Out-Null
}

$outDir = Split-Path -Parent $Out
if ($outDir -and -not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null
}
$newDoc.Save($Out)

# 校验：合并结果必须仍然包含 WinUI 的核心激活类，否则说明合错了。
$check = Get-Content $Out -Raw
$must = @(
    'Microsoft.UI.Xaml.Application',
    'Microsoft.UI.Xaml.Window',
    'requestedExecutionLevel',
    'dpiAwareness'
)
$missing = @()
foreach ($m in $must) {
    if ($check -notmatch [regex]::Escape($m)) { $missing += $m }
}
if ($missing.Count -gt 0) {
    throw "合并后的 manifest 缺少：$($missing -join ', ')"
}

Write-Host "  合并完成: $(Split-Path -Leaf $Out)  ($([math]::Round((Get-Item $Out).Length / 1KB, 1)) KB)"
