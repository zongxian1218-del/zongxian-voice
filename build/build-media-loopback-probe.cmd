@echo off
REM ===========================================================================
REM Build zxprobe WITH the real process-loopback probe enabled, into a SEPARATE
REM build dir, so the normal build (which has it off) is untouched.
REM
REM Why a separate script: the probe crashes the calling process with
REM 0xC0000374 (heap corruption) on this machine -- measured 2026-10-05.
REM Never enable it in the main app; run it in a throwaway child process.
REM
REM Paths are passed via environment variables on purpose: this file must stay
REM pure ASCII, and the project lives under a non-ASCII path. Writing the path
REM into the .cmd as ASCII turned it into "D:/??/ai001/..." and cmake failed.
REM
REM Usage (from PowerShell):
REM   $env:SRCDIR="<repo>\src\media"; $env:BUILDDIR="$env:TEMP\zx-build-probe"
REM   cmd /c build\build-media-loopback-probe.cmd
REM   & "$env:BUILDDIR\bin\Release\zxprobe.exe" loopback-probe
REM ===========================================================================
setlocal

if not defined SRCDIR set "SRCDIR=%~dp0..\src\media"
if not defined BUILDDIR set "BUILDDIR=%TEMP%\zx-build-probe"

call "D:\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] vcvars64.bat failed
  exit /b 1
)

set "CMAKE=C:\Program Files\CMake\bin\cmake.exe"
if not exist "%CMAKE%" (
  echo [ERROR] cmake not found at "%CMAKE%"
  exit /b 1
)

echo [1/2] Configuring with -DZX_ENABLE_PROCESS_LOOPBACK_PROBE ...
"%CMAKE%" -S "%SRCDIR%" -B "%BUILDDIR%" -G "Visual Studio 17 2022" -A x64 ^
  -DCMAKE_CXX_FLAGS=/DZX_ENABLE_PROCESS_LOOPBACK_PROBE
if errorlevel 1 (
  echo [ERROR] configure failed
  exit /b 1
)

echo [2/2] Building zxprobe ...
"%CMAKE%" --build "%BUILDDIR%" --config Release --target zxprobe --parallel
if errorlevel 1 (
  echo [ERROR] build failed
  exit /b 1
)

echo.
echo Built: %BUILDDIR%\bin\Release\zxprobe.exe
echo Run it in a THROWAWAY process -- it is expected to crash on machines
echo where process loopback is broken (0xC0000374).
endlocal
