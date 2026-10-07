@echo off
REM ===========================================================================
REM  Second machine - automated dual-machine self test (joiner side)
REM
REM  Run 7-selftest-host.cmd on the host FIRST, then this on the other machine.
REM
REM  Pure ASCII on purpose - see the note in 1-host.cmd.
REM ===========================================================================
setlocal
cd /d "%~dp0"

echo.
echo ============================================================
echo   DUAL-MACHINE SELF TEST - JOINER
echo ============================================================
echo.
echo   Run 7-selftest-host.cmd on the host machine FIRST.
echo.

set HOSTIP=
set TMPIP=
:askip
set TMPIP=
set /p TMPIP=Host IP address, for example 192.168.1.104 : 
if "%TMPIP%"=="" (
  echo   No IP entered. Ask the host for their address and try again.
  goto askip
)
set HOSTIP=%TMPIP%

echo.
echo   Checking the host is reachable on port 45890 ...
powershell -NoProfile -Command ^
  "$r = Test-NetConnection -ComputerName '%HOSTIP%' -Port 45890 -WarningAction SilentlyContinue; if ($r.TcpTestSucceeded) { Write-Host '  OK - host reachable' } else { Write-Host '  FAILED - cannot reach the host.'; Write-Host '  Check: host started? firewall allowed? same LAN?' }"

echo.
echo Starting joiner (port 45891) ...
echo The test takes about 80 seconds. Nothing to click.
echo.

start "" "%~dp0ZongxianVoice.exe" --port 45891 --name Join --room t1 --signal ws://%HOSTIP%:45890/signal --selftest-join --log-file "%~dp0zx-join.txt"

echo.
echo When it finishes, send back this file:
echo     %~dp0zx-join.txt
echo.
pause
exit /b 0
