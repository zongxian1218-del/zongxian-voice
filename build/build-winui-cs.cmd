@echo off
rem ===========================================================================
rem Build the Zongxian voice WinUI 3 desktop app (C#).
rem
rem IMPORTANT: keep this file ASCII-only.
rem   cmd.exe parses .bat using the OEM code page (936 on zh-CN Windows).
rem   UTF-8 multi-byte sequences can swallow the following newline, merging
rem   lines and breaking `if` blocks. An editor rewriting the file with a
rem   different encoding is enough to break it - so: ASCII only, always.
rem
rem Why plain `dotnet build` works here (unlike the C++ route):
rem   Microsoft.WindowsAppSDK for .NET ships its own XAML compiler
rem   (tools\net6.0\Microsoft.UI.Xaml.Markup.Compiler.dll) plus the
rem   buildTransitive targets that invoke it. No Visual Studio component
rem   is required. The C++/WinRT route needed a VS component that could
rem   not be installed without elevation - see ..\winui\BUILD-NOTES.md.
rem
rem Usage:
rem   build\build-winui-cs.cmd           Release build
rem   build\build-winui-cs.cmd debug     Debug build
rem   build\build-winui-cs.cmd run       build then launch
rem   build\build-winui-cs.cmd clean     remove bin/obj first
rem ===========================================================================
setlocal EnableDelayedExpansion

set "PROJ_DIR=%~dp0..\src\winui-cs"
set "DOTNET=C:\Program Files\dotnet\dotnet.exe"
set "DIST=%~dp0..\dist\winui"
set "CONFIG=Release"
set "DO_RUN=0"

if /I "%~1"=="debug" set "CONFIG=Debug"
if /I "%~1"=="run" set "DO_RUN=1"
if /I "%~1"=="clean" (
  echo [1/3] Cleaning bin/obj
  if exist "%PROJ_DIR%\bin" rmdir /S /Q "%PROJ_DIR%\bin"
  if exist "%PROJ_DIR%\obj" rmdir /S /Q "%PROJ_DIR%\obj"
)

if not exist "%DOTNET%" (
  echo [ERROR] dotnet.exe not found at "%DOTNET%"
  echo         Install the .NET SDK: winget install Microsoft.DotNet.SDK.8
  exit /b 1
)
echo [1/3] dotnet SDK:
"%DOTNET%" --version

echo [2/3] Building (%CONFIG%) ...
rem Keep intermediates out of the (non-ASCII) source path when possible.
rem MSBuild handles the path fine, but a separate obj dir keeps things tidy.
"%DOTNET%" build "%PROJ_DIR%\ZongxianVoice.csproj" ^
  -c %CONFIG% ^
  -p:Platform=x64 ^
  -v:minimal ^
  --nologo
if errorlevel 1 (
  echo.
  echo [ERROR] Build failed.
  echo   Common causes:
  echo     - Windows App SDK restore failed: needs access to nuget.org
  echo     - TargetPlatformVersion mismatch: see ZongxianVoice.csproj
  exit /b 1
)

rem --- collect artifacts so there is one stable place to look ---
rem IMPORTANT: copy .runtimeconfig.json and .deps.json too.
rem   A framework-dependent .NET app needs BOTH of them next to the exe.
rem   Copying only the exe produces this at launch, with no event-log entry:
rem     A fatal error was encountered. The library 'hostpolicy.dll' required
rem     to execute the application was not found ...
rem     Failed to run as a self-contained app.
rem   That message is misleading (the app is NOT self-contained) - the real
rem   cause is the missing runtimeconfig.json.
echo [3/4] Collecting artifacts ...
if not exist "%DIST%" mkdir "%DIST%"
rem Copy the whole output dir, not a filtered list: filtering is how the
rem runtimeconfig.json went missing in the first place.
xcopy /Y /E /Q /I "%PROJ_DIR%\bin\x64\%CONFIG%\net8.0-windows10.0.19041.0\win-x64\*.*" "%DIST%\" >nul 2>nul

rem --- 关键：把**刚构建的**引擎探针同步到应用输出目录与 dist ---
rem   Why (2026-10-06 实际踩到): 应用目录里的 tools\zxprobe.exe 如果还是旧副本，
rem   "共享电脑声音"会**静默失效** —— 旧探针不认识新子命令 stream，启动即退码 1，
rem   采集泵一块数据都拿不到，而界面与日志看上去全对。
set "APP_OUT=%PROJ_DIR%\bin\x64\%CONFIG%\net8.0-windows10.0.19041.0\win-x64"
set "PROBE_SRC=%~dp0..\dist\zxprobe.exe"
rem 只用单行 if：多行括号块曾经把整个脚本的括号配对搞乱（实测：cmd 开始把注释里的
rem 单词当参数去 mkdir，构建卡死十几分钟）。cmd 脚本里这一条值得当规矩。
if not exist "%APP_OUT%\tools" mkdir "%APP_OUT%\tools" >nul 2>nul
if not exist "%DIST%\tools" mkdir "%DIST%\tools" >nul 2>nul
if exist "%PROBE_SRC%" copy /Y "%PROBE_SRC%" "%APP_OUT%\tools\zxprobe.exe" >nul
if exist "%PROBE_SRC%" copy /Y "%PROBE_SRC%" "%DIST%\tools\zxprobe.exe" >nul
if not exist "%PROBE_SRC%" echo [WARN] run build\build-media.cmd first

rem --- strip run-time residue that xcopy just dragged in ---
rem   Why: xcopy copies whatever sits next to the exe in the build tree, including files the
rem   APP wrote while running (mic self-check recordings, audio-probe tmp, native video
rem   diagnostics, engine logs). Those shipped to users once already (v22 had 37 stray logs
rem   and 3 recordings inside it). run-checks.py now fails on them, so clean them here too:
rem   the packaging step must not depend on someone remembering to wipe the build tree.
rem   received/ was added 2026-10-06: the file-transfer landing dir shipped zx-filetest.bin
rem   (184320 B) inside v36 because neither the packer nor the guard knew that name.
echo       Cleaning run-time residue from dist ...
for %%D in (recordings audio-probe-tmp received sent incoming) do (
  if exist "%DIST%\%%D" rmdir /S /Q "%DIST%\%%D" >nul 2>nul
)
del /Q "%DIST%\nativevideo-*.txt" >nul 2>nul
del /Q "%DIST%\*.log" >nul 2>nul

rem --- 用户配置与测试配置绝不能进包（2026-10-06 事故） ---
rem   Why: 我为了截图往构建目录手写了一份 app-config.json（里面是假房间"周末开黑/会议室/老王家"
rem   和假地址 192.168.1.5），xcopy 把它带进 dist，v58 就这样发给了用户 ——
rem   用户打开看到的就是"写死的房间"。用户配置必须**首次启动时由应用自己生成**。
rem   room-test-*.json 是房间列表离线单测的临时产物，同理不能进包。
del /Q "%DIST%\app-config.json" >nul 2>nul
del /Q "%DIST%\room-test-*.json" >nul 2>nul
rem   加入了 preflight 诊断（加入失败时写的那种）与 logs\ 子目录：
rem   它原来写在 exe 同目录 ⇒ 被守卫当残留拦下（v60 实测），
rem   且真装到 Program Files 时那目录只读、写入会失败。现在统一写 logs\。
del /Q "%DIST%\join-preflight.txt" >nul 2>nul
if exist "%DIST%\logs" rmdir /S /Q "%DIST%\logs" >nul 2>nul

rem --- deploy the media engine web assets (WebView2 loads these) ---
rem   MediaEngine maps this folder to an https virtual host so the page
rem   counts as a secure context (required by getUserMedia).
rem   【无条件拷贝】原来套了 if exist "%PROJ_DIR%\media"，但在 PATH 受限的环境里
rem   这个判断曾走 else 分支：dist\winui\media 里留着 10/5 的旧页面，
rem   打包出去等于"没有共享电脑声音"这个功能（实测踩到）。宁可无脑拷，也不让版本不一致。
echo       Deploying media assets ...
rem 用**内联路径**（%~dp0..）而不是变量：实测这处变量在脚本后半段会取到空值，
rem 于是 xcopy 静默不动，dist 里留着 10/5 的旧页面（等于没有共享电脑声音这个功能）。
if not exist "%~dp0..\dist\winui\media" mkdir "%~dp0..\dist\winui\media" >nul 2>nul
xcopy /Y /E /Q /I "%~dp0..\src\winui-cs\media\*.*" "%~dp0..\dist\winui\media\" >nul 2>nul

rem --- generate resources.pri with makepri (Windows SDK) ---
rem   MSBuild's normal PRI path needs Visual Studio-only tasks
rem   (Microsoft.Build.Packaging.Pri.Tasks.dll / Microsoft.Build.AppxPackage.dll),
rem   which is why EnableCoreMrtTooling is false in the csproj. WinUI still needs a
rem   resources.pri at runtime (without it XAML load dies with 0xc000027b), so we
rem   invoke makepri.exe from the Windows SDK ourselves.
rem   The logic lives in build\gen-app-pri.ps1 so the build tree can use it too --
rem   running straight out of bin\ used to crash with no clue because only the
rem   packaged copy had a PRI. That script also searches several SDK versions instead
rem   of hard-coding one.
echo [4/4] Generating resources.pri ...
rem 用**全路径**调 Windows PowerShell：本机的 PATH 里可能没有 powershell，
rem 那样这一步会静默失败，resources.pri 不重新生成（旧 pri 结构对不上就 0xc000027b）。实测踩到。
set "PS1=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS1%" set "PS1=powershell"
"%PS1%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0gen-app-pri.ps1" -AppDir "%~dp0..\dist\winui"
if errorlevel 1 (
  echo       [WARN] resources.pri was not generated - the app will fail to start ^(0xc000027b^)
)

rem --- provenance: record which commit / when this dist was built ---
rem   Why: dist\winui once sat stale for ~11 hours (dll 03:50 vs source 15:09) and nothing
rem   in the folder said so. run-checks.py now REQUIRES this file next to the exe.
set "GITEXE="
if exist "D:\BuildTools\MinGit\cmd\git.exe" set "GITEXE=D:\BuildTools\MinGit\cmd\git.exe"
if not defined GITEXE for /f "delims=" %%i in ('where git 2^>nul') do if not defined GITEXE set "GITEXE=%%i"
set "COMMIT=unknown"
if defined GITEXE for /f "delims=" %%c in ('"%GITEXE%" rev-parse --short HEAD 2^>nul') do set "COMMIT=%%c"
set "DIRTY="
if defined GITEXE for /f "delims=" %%d in ('"%GITEXE%" status --porcelain 2^>nul') do set "DIRTY=+dirty"
(
  echo commit: %COMMIT%%DIRTY%
  echo built:  %DATE% %TIME%
  echo config: %CONFIG% / x64 / net8.0-windows10.0.19041.0 / win-x64
  echo rebuild: build\build-winui-cs.cmd
) > "%DIST%\BUILD-INFO.txt"
echo       BUILD-INFO.txt: commit=%COMMIT%%DIRTY%

echo.
echo === Build OK (%CONFIG%) ===
echo Artifacts: %DIST%
dir /b "%DIST%\ZongxianVoice.exe" 2>nul
dir /b "%DIST%\ZongxianVoice.runtimeconfig.json" 2>nul
dir /b "%DIST%\resources.pri" 2>nul

if "%DO_RUN%"=="1" (
  echo.
  echo Launching ...
  start "" "%DIST%\ZongxianVoice.exe"
)
endlocal
