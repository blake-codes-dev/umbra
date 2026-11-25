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
start "" "roblox://placeId=4483381587"
echo one client opening - pick a light place (Baseplate, Natural Disaster Survival), not a 200-player one
echo when you are in-game: tell me, then run the app and Attach
