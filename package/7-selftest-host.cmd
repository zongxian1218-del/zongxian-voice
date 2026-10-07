@echo off
REM ===========================================================================
REM  Host machine - automated dual-machine self test
REM
REM  Run this on the machine that acts as HOST (the one running the signaling
REM  server). It then prints the address the other machine must use.
REM
REM  Everything is automated except one thing: choosing a window in the system
REM  "select what to share" popup. That popup is rendered INSIDE the WebView2
REM  compositor and has no top-level window, so no external tool can click it
REM  (verified by enumerating windows). A human must click that one.
REM
REM  Pure ASCII on purpose - see the note in 1-host.cmd.
REM ===========================================================================
setlocal
cd /d "%~dp0"

echo.
echo ============================================================
echo   DUAL-MACHINE SELF TEST - HOST
echo ============================================================
echo.
echo   This machine will run the full automated sequence:
echo     chat - call - share - stop - share again - share during call
echo.
echo   IMPORTANT: allow the firewall prompt if it appears.
echo.

set PORT=45890
set NAME=Host
set TMPNAME=
set /p TMPNAME=Your name, Enter for Host : 
if not "%TMPNAME%"=="" set NAME=%TMPNAME%

echo.
echo Starting host on port %PORT% ...
echo.
echo ------------------------------------------------------------
echo   On the OTHER machine run 8-selftest-join.cmd and enter
echo   THIS machine's LAN IP.
echo.
echo   Your LAN IP is printed in the app log right after startup.
echo   Look in the Media Engine panel for lines starting with
echo   the text that means "peer should connect". It shows every
echo   usable address, for example:
echo        ws://192.168.1.104:%PORT%/signal
echo ------------------------------------------------------------
echo.

start "" "%~dp0ZongxianVoice.exe" --port %PORT% --name "%NAME%" --room t1 --selftest-host --log-file "%~dp0zx-host.txt"

echo.
echo Test running in the background. It takes about 80 seconds.
echo When it finishes, send back this file:
echo     %~dp0zx-host.txt
echo.
pause
exit /b 0
