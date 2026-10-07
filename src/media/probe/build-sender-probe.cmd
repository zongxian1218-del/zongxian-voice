@echo off
REM ===========================================================================
REM  Build sender-probe.exe  (capture -> GPU NV12 -> hardware H.264 encode)
REM
REM  Pure ASCII on purpose: cmd.exe reads .cmd with the OEM code page.
REM ===========================================================================
setlocal
cd /d "%~dp0"

REM Toolchain locations. Override by setting these env vars before calling if your
REM layout differs.
if not defined VCROOT set "VCROOT=D:\BuildTools"

call "%VCROOT%\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
if errorlevel 1 (
  echo Failed to initialise the MSVC environment from "%VCROOT%".
  echo Set VCROOT to your Visual Studio / Build Tools root and retry.
  exit /b 1
)

if not exist "build" mkdir "build"

echo Compiling sender-probe.cpp ...

REM user32.lib: remote-control input injection (SendInput) and the authorization UI
REM gdi32.lib : brushes/colors for the "being controlled" red banner
cl /nologo /std:c++20 /EHsc /O2 /W3 /MD /utf-8 ^
   /DUNICODE /D_UNICODE ^
   /Fo"build\\" /Fe"build\sender-probe.exe" ^
   sender-probe.cpp ^
   /link d3d11.lib dxgi.lib mfplat.lib mf.lib mfuuid.lib ws2_32.lib ole32.lib oleaut32.lib user32.lib gdi32.lib

if errorlevel 1 (
  echo.
  echo BUILD FAILED
  exit /b 1
)

echo.
echo BUILD OK
