# Fetch MbedTLS source for libdatachannel's DTLS backend.
#
# Version note: libdatachannel HEAD uses mbedtls_pk_copy_from_psa (MbedTLS >= 3.6), and
# MbedTLS >= 3.6 hard-requires its `framework` submodule just to CONFIGURE -- so both are
# fetched here. 3.5.2 configures without the submodule but lacks that symbol (C3861);
# 2.28 LTS is rejected outright (version parses as 0.0.0, libdatachannel wants >= 3).
#
# PURE ASCII on purpose: Windows PowerShell reads .ps1 as the ANSI code page (GBK here),
# so non-ASCII bytes can eat the closing quote and break parsing.
$ErrorActionPreference = 'Stop'

$ver  = 'v3.6.2'
$dest = "D:\BuildTools\mbedtls"
$tmp  = "D:\BuildTools\_deps-dl"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

function Get-TarGz([string]$url, [string]$tgz, [string]$target) {
  Invoke-WebRequest -Uri $url -OutFile $tgz -TimeoutSec 180 -UseBasicParsing
  Get-ChildItem $target -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
  New-Item -ItemType Directory -Force -Path $target | Out-Null
  & tar -xzf $tgz --strip-components=1 -C $target
  if ($LASTEXITCODE -ne 0) { throw "extract failed: $tgz" }
}

if ((Get-ChildItem $dest -Force -ErrorAction SilentlyContinue | Measure-Object).Count -le 3) {
  Get-TarGz "https://codeload.github.com/Mbed-TLS/mbedtls/tar.gz/refs/tags/$ver" `
            (Join-Path $tmp "mbedtls-$ver.tar.gz") $dest
  "mbedtls {0} -> {1} files" -f $ver, (Get-ChildItem $dest -Recurse -File | Measure-Object).Count
} else {
  "mbedtls already present"
}

# --- framework submodule, at the exact commit recorded by the tag ---
$fw = Join-Path $dest 'framework'
if ((Get-ChildItem $fw -Force -ErrorAction SilentlyContinue | Measure-Object).Count -le 3) {
  $api = "https://api.github.com/repos/Mbed-TLS/mbedtls/contents/framework?ref=$ver"
  $sha = (Invoke-RestMethod -Uri $api -TimeoutSec 60).sha
  "framework commit = {0}" -f $sha
  Get-TarGz "https://codeload.github.com/Mbed-TLS/mbedtls-framework/tar.gz/$sha" `
            (Join-Path $tmp 'mbedtls-framework.tar.gz') $fw
  "framework -> {0} files" -f (Get-ChildItem $fw -Recurse -File | Measure-Object).Count
} else {
  "framework already present"
}

"framework/CMakeLists.txt present: {0}" -f (Test-Path (Join-Path $fw 'CMakeLists.txt'))
