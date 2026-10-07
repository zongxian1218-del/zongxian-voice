@echo off
REM ===========================================================================
REM  Zongxian Voice - open the firewall port  (RUN AS ADMINISTRATOR)
REM
REM  Right-click this file -> "Run as administrator"
REM
REM  ---------------------------------------------------------------------------
REM  IMPORTANT - which firewall profile matters:
REM  Windows keeps SEPARATE rules for Domain / Private / Public networks.
REM  A VPN adapter (Radmin, ZeroTier, Tailscale, ...) is very often classified
REM  as PUBLIC, and on a default Windows install the PUBLIC profile is ON with
REM  inbound blocked. Result: the host machine silently drops the connection
REM  and the other side only reports "TCP connect timeout" - no hint at all.
REM
REM  So this script opens the port for ALL THREE profiles explicitly rather than
REM  relying on a single "any". It also PRINTS the resulting rules so you can
REM  confirm they were really created.
REM
REM  Why port-based and not program-based:
REM  A rule tied to the .exe full path breaks as soon as the app is extracted
REM  to a different folder (Desktop vs Documents vs D:\...). A port rule keeps
REM  working regardless of where the app lives.
REM
REM  Pure ASCII on purpose - see the note in 1-host.cmd.
REM ===========================================================================
setlocal

net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo   *** This must be run AS ADMINISTRATOR. ***
  echo.
  echo   Right-click this file and choose "Run as administrator".
  echo.
  pause
  exit /b 1
)

echo.
echo Current firewall profile states:
netsh advfirewall show allprofiles state | findstr /C:"Profile Settings" /C:"State"

echo.
echo Removing any old Zongxian rules...
netsh advfirewall firewall delete rule name="Zongxian Voice TCP 45890" >nul 2>&1
netsh advfirewall firewall delete rule name="Zongxian Voice UDP Media" >nul 2>&1
netsh advfirewall firewall delete rule name="Zongxian Voice (TCP)" >nul 2>&1
netsh advfirewall firewall delete rule name="Zongxian Voice (UDP)" >nul 2>&1

echo.
echo Adding TCP 45890 rule for Domain, Private and Public...
netsh advfirewall firewall add rule name="Zongxian Voice TCP 45890" dir=in action=allow protocol=TCP localport=45890 profile=domain,private,public

echo Adding UDP 45890-45900 rule for WebRTC media...
netsh advfirewall firewall add rule name="Zongxian Voice UDP Media" dir=in action=allow protocol=UDP localport=45890-45900 profile=domain,private,public

echo.
echo ============================================================
echo   VERIFY - the rules just created:
echo ============================================================
netsh advfirewall firewall show rule name="Zongxian Voice TCP 45890"
echo.
netsh advfirewall firewall show rule name="Zongxian Voice UDP Media"

echo.
echo ============================================================
echo   If you see "Enabled: Yes" and "Profiles: Domain,Private,Public"
echo   for both rules above, the firewall is no longer the problem.
echo.
echo   Next:
echo     - Host  : run 5-check-network.cmd
echo     - Friend: run 6-check-host.cmd
echo ============================================================
echo.
pause
exit /b 0
