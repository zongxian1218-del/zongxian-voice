@echo off
REM ===========================================================================
REM  Zongxian Voice - collect diagnostics
REM
REM  Pure ASCII on purpose - see the note in 1-host.cmd for why.
REM ===========================================================================
setlocal
cd /d "%~dp0"

set OUT=diagnostics.txt
echo Collecting... > "%OUT%"
echo ============================================== >> "%OUT%"
echo Zongxian Voice diagnostics   %DATE% %TIME% >> "%OUT%"
echo ============================================== >> "%OUT%"
echo. >> "%OUT%"

echo [System] >> "%OUT%"
ver >> "%OUT%" 2>&1
echo. >> "%OUT%"

echo [WebView2 runtime] >> "%OUT%"
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv >> "%OUT%" 2>&1
echo. >> "%OUT%"

echo [Process] >> "%OUT%"
tasklist /FI "IMAGENAME eq ZongxianVoice.exe" >> "%OUT%" 2>&1
echo. >> "%OUT%"

echo [Ports] >> "%OUT%"
netstat -ano | findstr ":45890 :45891" >> "%OUT%" 2>&1
echo. >> "%OUT%"

echo [IP addresses] >> "%OUT%"
ipconfig | findstr /C:"IPv4" >> "%OUT%" 2>&1
echo. >> "%OUT%"

echo [Engine log] >> "%OUT%"
if exist zx-log.txt (
  type zx-log.txt >> "%OUT%"
) else (
  echo   No zx-log.txt found. Launch with 1-host.cmd or 2-join.cmd, >> "%OUT%"
  echo   or copy the last lines from the Media Engine panel in the app. >> "%OUT%"
)
echo. >> "%OUT%"


echo [Join preflight] >> "%OUT%"
if exist join-preflight.txt (
  type join-preflight.txt >> "%OUT%"
) else (
  echo   none >> "%OUT%"
)
echo. >> "%OUT%"

echo [Host check] >> "%OUT%"
if exist check-host.txt (
  type check-host.txt >> "%OUT%"
) else (
  echo   none >> "%OUT%"
)
echo. >> "%OUT%"

echo [Network report] >> "%OUT%"
if exist net-report.txt (
  type net-report.txt >> "%OUT%"
) else (
  echo   none >> "%OUT%"
)
echo. >> "%OUT%"
echo [Startup errors] >> "%OUT%"
if exist startup-error.log (
  type startup-error.log >> "%OUT%"
) else (
  echo   none >> "%OUT%"
)

echo.
echo Done: %OUT%
echo Please send this file back.
pause
exit /b 0

