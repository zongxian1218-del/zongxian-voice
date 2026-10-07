@echo off
REM ===========================================================================
REM  Build dc-smoketest.exe -- libdatachannel DataChannel smoke test.
REM
REM  Links against the libdatachannel we built locally (shared lib + import lib);
REM  datachannel.dll is copied next to the exe so it runs without PATH changes.
REM
REM  PURE ASCII on purpose (cmd.exe reads .cmd with the OEM code page).
REM ===========================================================================
setlocal
cd /d "%~dp0"

if not defined VCROOT set "VCROOT=D:\BuildTools"
if not defined DCSRC set "DCSRC=D:\BuildTools\libdatachannel"

call "%VCROOT%\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
if errorlevel 1 (
  echo Failed to initialise the MSVC environment from "%VCROOT%".
  exit /b 1
)

if not exist "build" mkdir "build"

echo Compiling dc-smoketest.cpp ...
cl /nologo /std:c++17 /EHsc /O2 /W3 /MD /utf-8 ^
   /DUNICODE /D_UNICODE ^
   /I "%DCSRC%\include" ^
   /Fo"build\\" /Fe"build\dc-smoketest.exe" ^
   dc-smoketest.cpp ^
   /link "%DCSRC%\build\datachannel.lib" ws2_32.lib

if errorlevel 1 (
  echo.
  echo BUILD FAILED
  exit /b 1
)

copy /y "%DCSRC%\build\datachannel.dll" "build\datachannel.dll" >nul
echo.
echo BUILD OK
