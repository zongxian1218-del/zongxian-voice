# Fetch the 5 libdatachannel dependencies at the exact commits recorded in the repo.
#
# Why: in this environment `git clone` of github.com:443 does not work (submodules fail),
#      but codeload.github.com tarballs are reachable with Invoke-WebRequest.
#
# NOTE: this file is deliberately PURE ASCII. Windows PowerShell reads .ps1 as the ANSI
#       code page (GBK here), so non-ASCII bytes can eat the closing quote and break parsing.
$ErrorActionPreference = 'Stop'
$root = "D:\BuildTools\libdatachannel"

$deps = @(
  @{ name = 'plog';     owner = 'SergiusTheBest';   repo = 'plog';     sha = '94899e0b926ac1b0f4750bfbd495167b4a6ae9ef' },
  @{ name = 'usrsctp';  owner = 'paullouisageneau'; repo = 'usrsctp';  sha = 'fec583d54493f879d2ae44a743423bf8a04371ab' },
  @{ name = 'libjuice'; owner = 'paullouisageneau'; repo = 'libjuice'; sha = 'b89c792e3612faf2f12cf35bcc56857313a06be3' },
  @{ name = 'json';     owner = 'nlohmann';         repo = 'json';     sha = '55f93686c01528224f448c19128836e7df245f72' },
  @{ name = 'libsrtp';  owner = 'cisco';            repo = 'libsrtp';  sha = 'd33b8ffb1491a0b4b58a206889f09800cf7310ab' }
)

$tmp = "D:\BuildTools\_deps-dl"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

foreach ($d in $deps) {
  $dest = Join-Path $root ("deps\" + $d.name)
  if ((Get-ChildItem $dest -Force -ErrorAction SilentlyContinue | Measure-Object).Count -gt 3) {
    "  skip {0} (already present)" -f $d.name
    continue
  }
  $url = "https://codeload.github.com/{0}/{1}/tar.gz/{2}" -f $d.owner, $d.repo, $d.sha
  $tgz = Join-Path $tmp ($d.name + ".tar.gz")
  try {
    Invoke-WebRequest -Uri $url -OutFile $tgz -TimeoutSec 120 -UseBasicParsing
  } catch {
    "  [!] {0} download failed: {1}" -f $d.name, $_.Exception.Message
    continue
  }
  $size = [Math]::Round((Get-Item $tgz).Length / 1KB, 0)
  Get-ChildItem $dest -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
  New-Item -ItemType Directory -Force -Path $dest | Out-Null
  & tar -xzf $tgz --strip-components=1 -C $dest
  if ($LASTEXITCODE -ne 0) { "  [!] {0} extract failed" -f $d.name; continue }
  $n = (Get-ChildItem $dest -Recurse -File | Measure-Object).Count
  "  {0,-9} {1,6} KB  ->  {2} files" -f $d.name, $size, $n
}

"`n=== deps now ==="
Get-ChildItem (Join-Path $root 'deps') -Directory | ForEach-Object {
  "  {0,-9} {1} files" -f $_.Name, (Get-ChildItem $_.FullName -Recurse -File | Measure-Object).Count
}
