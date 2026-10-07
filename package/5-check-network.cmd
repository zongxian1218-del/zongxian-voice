@echo off
REM ===========================================================================
REM  Zongxian Voice - HOST network self-check
REM
REM  Run this ON THE HOST after starting 1-host.cmd.
REM  It answers: "is my service actually reachable from the network?"
REM
REM  The most common failure is the service listening ONLY on 127.0.0.1
REM  (loopback), which works locally but is unreachable for the other side.
REM
REM  Pure ASCII on purpose - see the note in 1-host.cmd.
REM ===========================================================================
setlocal
cd /d "%~dp0"

set REPORT=%~dp0net-report.txt

echo Running network self-check...
"%~dp0ZongxianVoice.exe" --port 45999 --net-report "%REPORT%"

echo.
if exist "%REPORT%" (
  type "%REPORT%"
) else (
  echo   Report file was not created. Is ZongxianVoice.exe present?
)

echo.
echo ============================================================
echo   What to look for:
echo.
echo   1) "0.0.0.0:45890 ... LISTENING"  - GOOD, reachable on all interfaces
echo      "127.0.0.1:45890 ... LISTENING" - BAD, loopback only
echo.
echo   2) A firewall rule mentioning ZongxianVoice.
echo      If it says none found, run 4-allow-firewall.cmd as administrator.
echo.
echo   3) The IP list at the bottom - give your friend the address from
echo      your VPN / mesh network tool if you use one, otherwise the
echo      192.168.x.x one.
echo ============================================================
echo.
pause
exit /b 0
