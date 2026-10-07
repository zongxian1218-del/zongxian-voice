@echo off
REM ===========================================================================
REM  Build encode-probe.exe  (hardware H.264 encoder verification, synthetic input)
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

echo Compiling encode-probe.cpp ...

cl /nologo /std:c++20 /EHsc /O2 /W3 /MD /utf-8 ^
   /DUNICODE /D_UNICODE ^
   /Fo"build\\" /Fe"build\encode-probe.exe" ^
   encode-probe.cpp ^
   /link mfplat.lib mf.lib mfuuid.lib ole32.lib oleaut32.lib

if errorlevel 1 (
  echo.
  echo BUILD FAILED
  exit /b 1
)

echo.
echo BUILD OK
