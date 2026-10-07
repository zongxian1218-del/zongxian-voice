@echo off
rem ===========================================================================
rem Build the Zongxian voice WinUI 3 desktop app.
rem
rem IMPORTANT: keep this file ASCII-only.
rem   cmd.exe parses .bat using the OEM code page (936 on zh-CN Windows).
rem   UTF-8 multi-byte sequences can swallow the following newline, merging
rem   lines and breaking `if` blocks with errors like
rem   "do was unexpected at this time". Non-ASCII also breaks when an editor
rem   rewrites the file with a different encoding - so: ASCII only, always.
rem
rem Why MSBuild instead of CMake:
rem   C++/WinRT XAML compilation (XamlCompiler producing .g.hpp/.xbf, then
rem   cppwinrt producing projections) is driven by MSBuild targets. CMake has
rem   no supported path for this chain.
rem
rem Why packages.config instead of PackageReference:
rem   PackageReference resolves by target framework, which C++ projects do not
rem   have. See packages.config for details.
rem
rem Usage:
rem   build\build-winui.cmd            Release build
rem   build\build-winui.cmd debug      Debug build
rem   build\build-winui.cmd clean      wipe intermediates first
rem ===========================================================================
setlocal EnableDelayedExpansion

set "SRC=%~dp0..\src\winui"
set "PROJ=%SRC%\zongxian_voice.vcxproj"
set "DIST=%~dp0..\dist\winui"
set "VSROOT=D:\BuildTools"
set "CONFIG=Release"

if /I "%~1"=="debug" set "CONFIG=Debug"
if /I "%~1"=="clean" (
  echo [1/5] Cleaning intermediates
  if exist "%SRC%\obj" rmdir /S /Q "%SRC%\obj"
  if exist "%SRC%\bin" rmdir /S /Q "%SRC%\bin"
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
  echo [ERROR] vcvars64.bat not found. Install VS Build Tools C++ workload.
  exit /b 1
)
echo [2/5] Compiler env: %VCVARS%
call "%VCVARS%" >nul
if errorlevel 1 (
  echo [ERROR] vcvars64.bat failed
  exit /b 1
)

rem --- locate msbuild.exe ---
set "MSBUILD="
if exist "%VSROOT%\MSBuild\Current\Bin\MSBuild.exe" set "MSBUILD=%VSROOT%\MSBuild\Current\Bin\MSBuild.exe"
if not defined MSBUILD (
  for /f "delims=" %%i in ('where msbuild 2^>nul') do if not defined MSBUILD set "MSBUILD=%%i"
)
if not defined MSBUILD (
  echo [ERROR] MSBuild.exe not found.
  echo         Expected at %VSROOT%\MSBuild\Current\Bin\MSBuild.exe
  exit /b 1
)
echo [3/5] MSBuild: %MSBUILD%
echo       Config : %CONFIG%

rem --- restore packages (packages.config path) ---
rem Do NOT use the /restore switch: that uses the PackageReference path,
rem which fails for C++ projects during framework resolution.
echo [4/5] Restoring packages ...
"%MSBUILD%" "%PROJ%" /t:Restore /p:RestorePackagesConfig=true /v:minimal /nologo
if errorlevel 1 (
  echo [ERROR] Package restore failed. Needs network access to nuget.org.
  exit /b 1
)

rem --- build ---
echo [5/5] Building ...
"%MSBUILD%" "%PROJ%" /p:Configuration=%CONFIG% /p:Platform=x64 /m /v:minimal /nologo
if errorlevel 1 (
  echo.
  echo [ERROR] Build failed. Common causes:
  echo   - WindowsTargetPlatformVersion mismatch: see vcxproj Globals
  echo   - Windows App SDK imports not found: check NuGetPackageRoot in vcxproj
  exit /b 1
)

rem --- collect artifacts ---
if not exist "%DIST%" mkdir "%DIST%"
for /r "%SRC%\bin" %%f in (*.exe *.dll *.pri *.xbf) do (
  if exist "%%f" copy /Y "%%f" "%DIST%\" >nul 2>nul
)

echo.
echo === Build OK (%CONFIG%) ===
echo Artifacts: %DIST%
dir /b "%DIST%" 2>nul
echo.
echo Run it:
echo   "%DIST%\zongxian_voice.exe"
endlocal
