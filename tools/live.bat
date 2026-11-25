@echo off
rem Fresh-client helper. Hyperion arms per process and our scan walks the whole image, so a pile of
rem leftover clients is the difference between "sometimes works" and "works".
rem   tools\live.bat            kill every client and stop
rem   tools\live.bat start      kill every client, then open exactly one via the launcher link
setlocal enabledelayedexpansion
set N=0
for /f %%c in ('tasklist /fi "imagename eq RobloxPlayerBeta.exe" 2^>nul ^| find /c /i "RobloxPlayerBeta"') do set N=%%c
echo clients running before: %N%
if /i "%~1"=="start" goto start
taskkill /f /im RobloxPlayerBeta.exe >nul 2>&1
echo killed. open one client with: tools\live.bat start
goto :eof
:start
taskkill /f /im RobloxPlayerBeta.exe >nul 2>&1
timeout /t 2 /nobreak >nul
rem home screen, not a deeplink: baseplate is restricted for this account and a deeplink to it
rem bounces, so let the owner pick the place that actually loads
start "" "roblox://experiences/start"
echo one client opening at the home screen - click YOUR unrestricted place, not a baseplate
echo when you are in-game for ~10s: run the app and Attach
echo to bisect a crash: echo nohook> "%LOCALAPPDATA%\Lyphen\flags.txt"  (and delete it after)
