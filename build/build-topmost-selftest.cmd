@echo off
REM Build topmost-selftest.exe -- window-style bisect for the overlay TOPMOST failure.
REM PURE ASCII on purpose (cmd.exe reads .cmd with the OEM code page).
setlocal
cd /d "%~dp0"

if not defined VCROOT set "VCROOT=D:\BuildTools"
call "%VCROOT%\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
if errorlevel 1 (
  echo Failed to initialise the MSVC environment from "%VCROOT%".
  exit /b 1
)

cl /nologo /std:c++20 /EHsc /O2 /W3 /MD /utf-8 ^
   /DUNICODE /D_UNICODE ^
   /Fe"topmost-selftest.exe" ^
   topmost-selftest.cpp ^
   /link user32.lib

if errorlevel 1 (
  echo BUILD FAILED
  exit /b 1
)
echo BUILD OK
