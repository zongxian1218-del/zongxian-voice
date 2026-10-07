@echo off
REM ===========================================================================
REM  Build display-probe.exe  (locate the exact cause of capture denial)
REM
REM  Pure ASCII on purpose: cmd.exe reads .cmd with the OEM code page, so
REM  non-ASCII bytes here turn into mojibake and can break the batch parser.
REM
REM  vcvars64.bat is used instead of hand-written /I paths because the
REM  Universal CRT headers do not live under the MSVC folder.
REM ===========================================================================
setlocal
cd /d "%~dp0"

call "D:\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
if errorlevel 1 (
  echo Failed to initialise the MSVC environment.
  exit /b 1
)

if not exist "build" mkdir "build"

echo Compiling display-probe.cpp ...

cl /nologo /std:c++20 /EHsc /O2 /W3 /MD /utf-8 ^
   /DUNICODE /D_UNICODE ^
   /Fo"build\\" /Fe"build\display-probe.exe" ^
   display-probe.cpp ^
   /link d3d11.lib dxgi.lib wtsapi32.lib user32.lib ole32.lib

if errorlevel 1 (
  echo.
  echo BUILD FAILED
  exit /b 1
)

echo.
echo BUILD OK
