@echo off
REM ===========================================================================
REM  Build transport-probe.exe  (self-built UDP transport self-test)
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

echo Compiling transport-probe.cpp ...

cl /nologo /std:c++20 /EHsc /O2 /W3 /MD /utf-8 ^
   /DUNICODE /D_UNICODE ^
   /Fo"build\\" /Fe"build\transport-probe.exe" ^
   transport-probe.cpp ^
   /link ws2_32.lib

if errorlevel 1 (
  echo.
  echo BUILD FAILED
  exit /b 1
)

echo.
echo BUILD OK
