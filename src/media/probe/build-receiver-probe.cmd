@echo off
REM ===========================================================================
REM  Build receiver-probe.exe
REM    UDP reassemble -> H.264 decode (libavcodec) -> BMP proof
REM
REM  Why libavcodec instead of the Media Foundation decoder MFT:
REM  the MS software decoder has a fixed 27-frame pipeline (~900 ms) and
REM  rejects every setting that would shorten it. libavcodec lets us force
REM  thread_count=1 + AV_CODEC_FLAG_LOW_DELAY, which keeps latency at ~1 frame.
REM
REM  Pure ASCII on purpose: cmd.exe reads .cmd with the OEM code page.
REM ===========================================================================
setlocal
cd /d "%~dp0"

REM Toolchain locations. Override by setting these env vars before calling this
REM script if your layout differs -- hardcoding them is how "works on my machine"
REM starts (this project has already been bitten by absolute paths twice).
if not defined VCROOT set "VCROOT=D:\BuildTools"
if not defined FFROOT set "FFROOT=D:\BuildTools\ffmpeg\ffmpeg-9.0.2-full_build-shared"

call "%VCROOT%\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
if errorlevel 1 (
  echo Failed to initialise the MSVC environment from "%VCROOT%".
  echo Set VCROOT to your Visual Studio / Build Tools root and retry.
  exit /b 1
)

if not exist "%FFROOT%\include\libavcodec\avcodec.h" (
  echo FFmpeg headers not found under "%FFROOT%".
  echo Set FFROOT to your FFmpeg shared build root and retry.
  exit /b 1
)

if not exist "build" mkdir "build"

echo Compiling receiver-probe.cpp ...

cl /nologo /std:c++20 /EHsc /O2 /W3 /MD /utf-8 ^
   /DUNICODE /D_UNICODE ^
   /I"%FFROOT%\include" ^
   /Fo"build\\" /Fe"build\receiver-probe.exe" ^
   receiver-probe.cpp ^
   /link /LIBPATH:"%FFROOT%\lib" ^
         ws2_32.lib d3d11.lib dxgi.lib user32.lib gdi32.lib ^
         mfplat.lib mf.lib mfuuid.lib ole32.lib oleaut32.lib ^
         avcodec.lib avutil.lib swscale.lib

if errorlevel 1 (
  echo.
  echo BUILD FAILED
  exit /b 1
)

REM The shared FFmpeg DLLs must sit next to the exe.
REM avcodec-63.dll depends on swresample-7.dll as well -- missing it gives
REM 0xC0000135 (STATUS_DLL_NOT_FOUND) with no other explanation.
echo Copying FFmpeg runtime DLLs ...
copy /y "%FFROOT%\bin\avcodec-63.dll"    "build\" >nul
copy /y "%FFROOT%\bin\avutil-61.dll"     "build\" >nul
copy /y "%FFROOT%\bin\swscale-10.dll"    "build\" >nul
copy /y "%FFROOT%\bin\swresample-7.dll"  "build\" >nul

echo.
echo BUILD OK
