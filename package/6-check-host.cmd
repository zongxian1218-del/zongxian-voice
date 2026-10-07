@echo off
REM ===========================================================================
REM  Zongxian Voice - JOINER connection self-check
REM
REM  Run this ON THE FRIEND'S MACHINE when the app says
REM  "[engine error] websocket error".
REM
REM  It tells you WHERE the chain breaks: TCP, HTTP, or WebSocket.
REM
REM  Pure ASCII on purpose - see the note in 1-host.cmd.
REM ===========================================================================
setlocal
cd /d "%~dp0"

set HOSTIP=
set TMPIP=
set /p TMPIP=Host IP address (like 192.168.1.5): 
if "%TMPIP%"=="" (
  echo   No IP entered.
  pause
  exit /b 1
)
set HOSTIP=%TMPIP%

echo.
echo Checking %HOSTIP%:45890 ...
echo.

"%~dp0ZongxianVoice.exe" --port 45999 --check-host %HOSTIP%

echo.
echo ============================================================
echo   Also written to: check-host.txt
echo   Please send that file back if you need help.
echo ============================================================
echo.
pause
exit /b 0
