@echo off
REM ===========================================================================
REM  Zongxian Voice - HOST launcher
REM
REM  IMPORTANT: this file is intentionally pure ASCII.
REM  cmd.exe reads .cmd files using the OEM code page (936 on Chinese Windows).
REM  A UTF-8 file with Chinese comments gets mis-decoded, and the mangled text
REM  is then executed as commands. Keeping this ASCII avoids that entirely.
REM  All Chinese instructions live in the separate readme text file.
REM ===========================================================================
setlocal
cd /d "%~dp0"

set PORT=45890
REM Read into a TEMP variable, then only overwrite if the user typed something.
REM Why not just "set NAME=Me" followed by "set /p NAME=..."?
REM Because set /p CLEARS the variable when the user presses Enter with no
REM input, so the default would be lost. Verified by testing both cases.
set NAME=Me
set TMPNAME=
set /p TMPNAME=Your name (Enter = Me): 
if not "%TMPNAME%"=="" set NAME=%TMPNAME%

echo.
echo ============================================================
echo   Starting as HOST
echo   Signal port : %PORT%
echo   Your name   : %NAME%
echo ============================================================
echo.
echo   Tell your friend these two things:
echo     1) your LAN IP - run:  ipconfig     (looks like 192.168.x.x)
echo     2) the room name - it is:  test
echo.
echo   Windows may ask about firewall access on first run.
echo   You MUST click Allow, otherwise your friend cannot connect.
echo.
pause

start "" "%~dp0ZongxianVoice.exe" --port %PORT% --name "%NAME%" --room test --log-file "%~dp0zx-log.txt"
exit /b 0
