@echo off
REM ===========================================================================
REM  Build the capture probe (screen capture + H.264 encoder availability)
REM
REM  Uses the official vcvars64.bat so that include/library paths (including
REM  the Universal CRT, which is NOT under the MSVC folder) are set correctly.
REM  Hand-rolling /I paths missed ucrt\ctype.h the first time.
REM
REM  /utf-8 is required because the source contains Chinese comments; without
REM  it the compiler warns C4819 on a CP936 system.
REM
REM  Pure ASCII on purpose: cmd.exe reads .cmd with the OEM code page.
REM ===========================================================================
setlocal
cd /d "%~dp0"

call "D:\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
if errorlevel 1 (
  echo Failed to initialise the MSVC environment.
  exit /b 1
)

if not exist "build" mkdir "build"

echo Compiling capture_probe.cpp ...

cl /nologo /std:c++20 /EHsc /O2 /W3 /MD /utf-8 ^
   /DUNICODE /D_UNICODE ^
   /Fo"build\\" /Fe"build\capture_probe.exe" ^
   capture_probe.cpp ^
   /link windowsapp.lib d3d11.lib dxgi.lib mfplat.lib mf.lib mfuuid.lib ^
         ole32.lib oleaut32.lib runtimeobject.lib shlwapi.lib

if errorlevel 1 (
  echo.
  echo BUILD FAILED
  exit /b 1
)

echo.
echo BUILD OK
