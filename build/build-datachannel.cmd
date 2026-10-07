@echo off
REM ===========================================================================
REM  Build libdatachannel (MPL-2.0) with the MbedTLS backend, MSVC x64 Release.
REM
REM  Feasibility spike for "replace our hand-rolled UDP transport".
REM  PURE ASCII: cmd.exe reads .cmd with the OEM code page, and Windows
REM  PowerShell reads .ps1 as ANSI -- non-ASCII breaks both (learned the hard way).
REM
REM  Layout: deps are placed under libdatachannel\deps\<name> at the exact
REM  commits recorded in the repo (git submodule clone is blocked here, so they
REM  were fetched from codeload.github.com instead).
REM ===========================================================================
setlocal
set ROOT=D:\BuildTools\libdatachannel
set MBED=D:\BuildTools\mbedtls
REM NOTE: do NOT put quotes in the variable itself -- "%CM%" would become ""C:\Program Files\..""
set CM=C:\Program Files\CMake\bin\cmake.exe

call "D:\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
if errorlevel 1 (
  echo Failed to init MSVC environment.
  exit /b 1
)

set PY=C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe

echo === [0/3] enable MbedTLS DTLS-SRTP ===
REM libdatachannel's dtlssrtptransport.cpp uses mbedtls_ssl_get_dtls_srtp_negotiation_result,
REM which only exists when MBEDTLS_SSL_DTLS_SRTP is enabled in the MbedTLS config header
REM (off by default). Without this the build fails with C3861 in dtlssrtptransport.cpp.
"%PY%" "%MBED%\scripts\config.py" set MBEDTLS_SSL_DTLS_SRTP
if errorlevel 1 ( echo CONFIG MBEDTLS FAILED & exit /b 1 )

echo === [1/3] configure MbedTLS ===
if not exist "%MBED%\build" mkdir "%MBED%\build"
pushd "%MBED%\build"
REM MbedTLS 2.28 declares cmake_minimum_required(2.8.12); CMake 4.x dropped <3.5
REM compatibility, so the policy floor has to be raised explicitly.
"%CM%" .. -G "NMake Makefiles" -DCMAKE_BUILD_TYPE=Release ^
  -DCMAKE_POLICY_VERSION_MINIMUM=3.5 ^
  -DENABLE_TESTING=OFF -DENABLE_PROGRAMS=OFF -DUSE_SHARED_MBEDTLS_LIBRARY=OFF ^
  -DUSE_STATIC_MBEDTLS_LIBRARY=ON
if errorlevel 1 ( echo CONFIGURE MBEDTLS FAILED & popd & exit /b 1 )

echo === [2/3] build MbedTLS ===
"%CM%" --build . --config Release
if errorlevel 1 ( echo BUILD MBEDTLS FAILED & popd & exit /b 1 )
popd

echo === [3/3] configure+build libdatachannel ===
if not exist "%ROOT%\build" mkdir "%ROOT%\build"
pushd "%ROOT%\build"
"%CM%" .. -G "NMake Makefiles" -DCMAKE_BUILD_TYPE=Release ^
  -DCMAKE_POLICY_VERSION_MINIMUM=3.5 ^
  -DCMAKE_C_FLAGS="/utf-8" -DCMAKE_CXX_FLAGS="/utf-8" ^
  -DUSE_MBEDTLS=1 -DUSE_GNUTLS=0 -DUSE_NICE=0 -DNO_TESTS=ON -DNO_EXAMPLES=OFF ^
  -DCMAKE_PREFIX_PATH="%MBED%\build" ^
  -DMbedTLS_INCLUDE_DIR="%MBED%\include" ^
  -DMbedTLS_LIBRARY="%MBED%\build\library\mbedtls.lib" ^
  -DMbedX509_LIBRARY="%MBED%\build\library\mbedx509.lib" ^
  -DMbedCrypto_LIBRARY="%MBED%\build\library\mbedcrypto.lib"
if errorlevel 1 ( echo CONFIGURE DATACHANNEL FAILED & popd & exit /b 1 )

"%CM%" --build . --config Release
if errorlevel 1 ( echo BUILD DATACHANNEL FAILED & popd & exit /b 1 )
popd

echo.
echo BUILD OK
