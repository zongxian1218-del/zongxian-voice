@echo off
REM ===========================================================================
REM  Zongxian Voice - JOIN launcher
REM
REM  Checks reachability BEFORE starting the app, so a dead mesh link or a
REM  blocked port is reported in plain language instead of showing up as an
REM  obscure "websocket error" inside the app.
REM
REM  ---------------------------------------------------------------------------
REM  BATCH FILE RULES OBSERVED HERE - each one was learned the hard way:
REM    1) Pure ASCII only. cmd.exe reads this file with the OEM code page, so
REM       a UTF-8 file with Chinese comments gets mis-decoded and the mangled
REM       text is executed as commands.
REM    2) NEVER put a closing parenthesis inside an echo that sits inside an
REM       "if (...)" block. cmd.exe treats it as the end of the block and the
REM       script dies with "then was unexpected at this time".
REM    3) Avoid ? ! & ^ % inside echoed text for the same reason.
REM    4) A bare "pause" with stdin redirected still blocks, so the automated
REM       tests strip it out rather than relying on it.
REM ===========================================================================
setlocal

cd /d "%~dp0"

set LOCALPORT=45891
set HOSTIP=
set TMPIP=
:askip
set TMPIP=
set /p TMPIP=Host IP address, for example 26.103.67.21 : 
if "%TMPIP%"=="" (
  echo   No IP entered. Ask the host for their IP and try again.
  goto askip
)
set HOSTIP=%TMPIP%

set NAME=Friend
set TMPNAME=
set /p TMPNAME=Your name, Enter for Friend : 
if not "%TMPNAME%"=="" set NAME=%TMPNAME%

set SIGNAL=ws://%HOSTIP%:45890/signal

echo.
echo ============================================================
echo   Checking host reachability
echo   Target: %HOSTIP%
echo ============================================================
echo.

echo [1/2] ping test
ping -n 2 -w 1500 %HOSTIP% >nul 2>&1
if errorlevel 1 goto pingfail
echo   OK - host answers ping
goto tcptest

:pingfail
echo   FAILED - host does not answer ping
echo.
echo   The mesh link between the two machines is not working.
echo   The app cannot fix this - the machines must be able to reach
echo   each other first.
echo.
echo   Check these:
echo     - Is the host machine running, and does Radmin show it ONLINE
echo     - Are you both in the same Radmin network
echo     - Open Radmin on this PC and confirm the host IP %HOSTIP%
echo       appears in the list - Radmin lists many machines, so make
echo       sure this IP really is the host and not another device
echo     - Restart Radmin VPN on BOTH machines, quitting it fully
echo       rather than just disconnecting, then run this again
echo.
echo   If your setup blocks ping but TCP works, ignore this warning.
echo.

:tcptest
echo.
echo [2/2] TCP port 45890 test
powershell -NoProfile -Command ^
  "$r = Test-NetConnection -ComputerName '%HOSTIP%' -Port 45890 -WarningAction SilentlyContinue; if ($r.TcpTestSucceeded) { Write-Host '  OK - port is open' } else { Write-Host '  FAILED - port 45890 is not reachable'; Write-Host ''; Write-Host '  If ping worked, the machine is reachable and the HOST'; Write-Host '  FIREWALL is blocking the port.'; Write-Host '  FIX: on the HOST machine, right-click 4-allow-firewall.cmd'; Write-Host '       and choose Run as administrator.' }"

echo.
echo ============================================================
echo   Starting as JOINER
echo   Local port  : %LOCALPORT%
echo   Host signal : %SIGNAL%
echo   Your name   : %NAME%
echo ============================================================
echo.
pause

start "" "%~dp0ZongxianVoice.exe" --port %LOCALPORT% --name "%NAME%" --room test --signal %SIGNAL% --log-file "%~dp0zx-log.txt"
exit /b 0
