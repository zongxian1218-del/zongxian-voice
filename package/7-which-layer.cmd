@echo off
REM ===========================================================================
REM  Zongxian Voice - which layer is broken?  (run on the FRIEND'S machine)
REM
REM  Separates two very different causes of "cannot connect":
REM    a) the mesh/VPN link between the two machines is not working
REM    b) the link works but the HOST firewall drops the packets
REM
REM  How it distinguishes them: it tests ICMP ping FIRST.
REM    ping OK  + TCP fail  ->  network path exists, firewall blocks the port
REM    ping FAIL           ->  the mesh/VPN link itself is not up
REM
REM  Pure ASCII on purpose - see the note in 1-host.cmd.
REM ===========================================================================
setlocal
cd /d "%~dp0"

set HOSTIP=
set TMPIP=
set /p TMPIP=Host IP address (like 26.103.67.21): 
if "%TMPIP%"=="" (
  echo   No IP entered.
  pause
  exit /b 1
)
set HOSTIP=%TMPIP%

echo.
echo ============================================================
echo   Target: %HOSTIP%
echo ============================================================
echo.

echo [1/2] ICMP ping  - is the machine reachable at all?
ping -n 3 -w 2000 %HOSTIP%
echo.

echo [2/2] TCP port 45890  - is the app reachable?
powershell -NoProfile -Command ^
  "$r = Test-NetConnection -ComputerName '%HOSTIP%' -Port 45890 -WarningAction SilentlyContinue; if ($r.TcpTestSucceeded) { Write-Host '  RESULT: TCP 45890 OK' } else { Write-Host '  RESULT: TCP 45890 FAILED' }"

echo.
echo ============================================================
echo   HOW TO READ THIS
echo ============================================================
echo.
echo   ping OK   + TCP OK    ->  everything works, try the app again
echo.
echo   ping OK   + TCP FAIL  ->  the machines can reach each other,
echo        so the HOST FIREWALL is dropping the port.
echo        FIX: on the HOST machine, right-click 4-allow-firewall.cmd
echo             and choose "Run as administrator".
echo.
echo   ping FAIL             ->  the mesh / VPN link is not up.
echo        FIX: open your mesh tool (Radmin VPN etc.) on BOTH machines
echo             and confirm the other machine is listed and ONLINE and
echo             that both are in the SAME network. Then ping again.
echo.
echo   Note: some setups block ICMP while TCP still works. If ping fails
echo   but the app connects, ignore the ping result.

echo.
echo ============================================================
pause
exit /b 0
