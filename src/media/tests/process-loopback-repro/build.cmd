@echo off
call "D:\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
cl /nologo /EHsc /std:c++17 /Fe:loopback-min.exe main.cpp /link mmdevapi.lib ole32.lib runtimeobject.lib > build.log 2>&1
echo build-exit=%errorlevel%
