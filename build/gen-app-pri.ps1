# gen-app-pri.ps1 -- generate resources.pri for the WinUI 3 unpackaged app.
#
# WHY THIS EXISTS
#   ZongxianVoice.csproj sets <EnableCoreMrtTooling>false</EnableCoreMrtTooling> because the
#   MRT tooling needs Microsoft.Build.Packaging.Pri.Tasks.dll / Microsoft.Build.AppxPackage.dll,
#   which ship only with a full Visual Studio install (this machine has Build Tools only).
#   But the comment there -- "unpackaged mode does not need it" -- is WRONG: without
#   resources.pri, WinUI 3 fails to load XAML and the app dies at startup with
#   0xc000027b (STATUS_STOWED_EXCEPTION), with no window at all. Measured, not guessed:
#     bin build tree (no pri)      -> crash, 0 windows
#     same exe + resources.pri     -> window appears
#     remove ONLY resources.pri    -> crash again
#
#   So we generate the pri ourselves with makepri.exe from the Windows SDK.
#   Sizing sanity check: the generated file is ~868 KB, and the old known-good copy that
#   shipped in dist/ was 868,296 B -- i.e. the same thing, produced the same way.
#
# USAGE
#   powershell -File build\gen-app-pri.ps1 -AppDir <folder containing ZongxianVoice.exe>
#
# PURE ASCII on purpose: Windows PowerShell reads .ps1 as the ANSI code page (GBK here),
# so non-ASCII bytes can eat the closing quote and break parsing.

param(
    [Parameter(Mandatory = $true)][string]$AppDir
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path (Join-Path $AppDir 'ZongxianVoice.exe'))) {
    throw "ZongxianVoice.exe not found in $AppDir"
}

# --- locate makepri.exe (Windows SDK) -------------------------------------------------
$makepri = $null
$candidates = @()
foreach ($ver in @('10.0.26100.0', '10.0.22621.0', '10.0.22000.0', '10.0.20348.0', '10.0.19041.0')) {
    $candidates += "C:\Program Files (x86)\Windows Kits\10\bin\$ver\x64\makepri.exe"
}
foreach ($c in $candidates) { if (Test-Path $c) { $makepri = $c; break } }
if (-not $makepri) {
    $hit = Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\bin" -Recurse -Filter makepri.exe -ErrorAction SilentlyContinue |
           Where-Object { $_.FullName -match '\\x64\\' } | Select-Object -First 1
    if ($hit) { $makepri = $hit.FullName }
}
if (-not $makepri) {
    throw "makepri.exe not found. Install the Windows SDK 'Windows SDK for Desktop Apps' component, or generate resources.pri on a machine that has it and copy it in."
}
Write-Host "makepri: $makepri"

# --- stage only what belongs in the resource index ------------------------------------
# Mirror what build-winui-cs.cmd has been doing successfully: copy the app folder's
# TOP-LEVEL files into a clean staging dir, then index that.
#   * Top-level matters: the bulk of the resulting PRI is the framework's own resource
#     indexes (Microsoft.UI.pri ~101 KB, Microsoft.UI.Xaml.Controls.pri ~1.25 MB), which
#     sit next to the exe. An earlier version of this script filtered by extension
#     (.exe/.dll/.json/.winmd) and therefore dropped the .pri files -- the result was a
#     14 KB PRI instead of ~868 KB, i.e. it would not have been enough.
#   * Clean dir matters: makepri chokes if a resources.pri already exists in the folder
#     it is indexing, so we never index the app folder in place.
#   * Sub-directories are deliberately not staged: the compiled XAML lives in src\*.xbf,
#     but the packaging path that is known to work does not index them either (the XBFs
#     are embedded in the assembly), so we stay identical to it rather than inventing
#     a different recipe.
$work = Join-Path ([System.IO.Path]::GetTempPath()) ("zxpri-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $work | Out-Null

Get-ChildItem $AppDir -File | Copy-Item -Destination $work -Force

Push-Location $work
try {
    & $makepri createconfig /cf priconfig.xml /dq en-US /o | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "makepri createconfig failed ($LASTEXITCODE)" }
    & $makepri new /pr . /cf priconfig.xml /of resources.pri /o | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "makepri new failed ($LASTEXITCODE)" }
} finally {
    Pop-Location
}

$produced = Join-Path $work 'resources.pri'
if (-not (Test-Path $produced)) { throw "makepri did not produce resources.pri" }
$size = (Get-Item $produced).Length
# Sanity floor: a PRI that only indexes a handful of files means the framework resource
# indexes were not staged, and WinUI will still fail to start. Fail loudly instead.
if ($size -lt 200000) {
    throw ("resources.pri is only {0:N0} bytes -- the framework .pri files were probably not staged; refusing to install it" -f $size)
}
Copy-Item $produced (Join-Path $AppDir 'resources.pri') -Force
Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue

Write-Host ("resources.pri -> {0} ({1:N0} bytes)" -f (Join-Path $AppDir 'resources.pri'), $size)
