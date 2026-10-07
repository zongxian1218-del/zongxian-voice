@echo off
rem ===========================================================================
rem Build the Zongxian voice media engine.
rem
rem IMPORTANT: keep this file ASCII-only.
rem   cmd.exe parses .bat files using the OEM code page (936 on zh-CN Windows).
rem   UTF-8 Chinese comments get decoded as GBK, and some multi-byte sequences
rem   swallow the following newline. Lines then merge and `if` blocks break
rem   with errors like "do was unexpected at this time".
rem   Chinese documentation lives in src/media/README.md instead.
rem
rem Usage:
rem   build\build-media.cmd          incremental build
rem   build\build-media.cmd clean    wipe the intermediate dir first
rem ===========================================================================
setlocal EnableDelayedExpansion

set "SRC=%~dp0..\src\media"
rem Intermediate dir, namespaced by repository folder name.
rem   Was a shared "%TEMP%\zx-build": two checkouts on one machine then fight over the
rem   same CMake cache ("source ... does not match ... used to generate cache").
for %%I in ("%~dp0..") do set "REPONAME=%%~nxI"
set "BUILD=%TEMP%\zx-build-!REPONAME!"
set "DIST=%~dp0..\dist"
rem Visual Studio root. Override with:  set VSROOT=C:\path\to\BuildTools
rem If this default does not exist, vcvars64.bat is located automatically via vswhere.
if not defined VSROOT set "VSROOT=D:\BuildTools"

if /I "%~1"=="clean" (
  echo [1/5] Cleaning %BUILD%
  if exist "%BUILD%" rmdir /S /Q "%BUILD%"
)

rem --- locate vcvars64.bat ---
set "VCVARS="
if exist "%VSROOT%\VC\Auxiliary\Build\vcvars64.bat" set "VCVARS=%VSROOT%\VC\Auxiliary\Build\vcvars64.bat"
if not defined VCVARS (
  for /f "usebackq delims=" %%i in (`"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2^>nul`) do (
    if exist "%%i\VC\Auxiliary\Build\vcvars64.bat" set "VCVARS=%%i\VC\Auxiliary\Build\vcvars64.bat"
  )
)
if not defined VCVARS (
  echo [ERROR] vcvars64.bat not found.
  echo         Install the "Desktop development with C++" workload of VS Build Tools.
  exit /b 1
)
echo [2/5] Compiler env: %VCVARS%
call "%VCVARS%" >nul
if errorlevel 1 (
  echo [ERROR] vcvars64.bat failed
  exit /b 1
)

rem --- locate cmake ---
set "CMAKE="
if exist "%VSROOT%\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe" (
  set "CMAKE=%VSROOT%\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
)
if not defined CMAKE (
  for /f "delims=" %%i in ('where cmake 2^>nul') do if not defined CMAKE set "CMAKE=%%i"
)
if not defined CMAKE (
  echo [ERROR] cmake.exe not found
  exit /b 1
)
echo [3/5] cmake: %CMAKE%

rem --- configure ---
rem Using the "Visual Studio 17 2022" generator rather than Ninja:
rem the Ninja + MSVC + non-ASCII source path combination fails intermittently
rem during dependency scanning, and the VS generator locates the SDK by itself.
echo [4/5] Configuring ...
"%CMAKE%" -S "%SRC%" -B "%BUILD%" -G "Visual Studio 17 2022" -A x64
if errorlevel 1 (
  echo [ERROR] CMake configure failed
  exit /b 1
)

rem --- build ---
echo [5/5] Building ...
"%CMAKE%" --build "%BUILD%" --config Release --parallel
if errorlevel 1 (
  echo [ERROR] Build failed
  exit /b 1
)

rem --- collect artifacts so the Python side has one stable place to look ---
if not exist "%DIST%" mkdir "%DIST%"
for /r "%BUILD%" %%f in (zongxian_media.dll zxprobe.exe) do (
  if exist "%%f" copy /Y "%%f" "%DIST%\" >nul
)

echo.
echo === Build OK ===
echo Artifacts: %DIST%
dir /b "%DIST%\zongxian_media.dll" 2>nul
dir /b "%DIST%\zxprobe.exe" 2>nul
echo.
echo Next:
echo   "%DIST%\zxprobe.exe" devices
echo   "%DIST%\zxprobe.exe" loopback-probe
echo   "%DIST%\zxprobe.exe" record --seconds 10
endlocal
