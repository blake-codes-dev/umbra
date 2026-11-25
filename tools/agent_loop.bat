@echo off
rem Keeps the Windows agent working: run a turn, sleep, run the next one. Register once with
rem   schtasks /create /sc onlogon /tn LyphenAgent /tr "%~dp0agent_loop.bat"
rem and it survives crashes, ended turns and reboots.
rem   set AGENT_CMD to your CLI if it is not `pi`; extra flags go in AGENT_ARGS.
setlocal enabledelayedexpansion
set HERE=%~dp0
set ROOT=%HERE%..
set LOCK=%ROOT%\state\agent\lock-windows
set PROMPT=%ROOT%\state\agent\prompt-windows.txt
if "%AGENT_CMD%"=="" set AGENT_CMD=pi
if "%AGENT_ARGS%"=="" set AGENT_ARGS=--auto

:top
rem one turn at a time; a lock older than 40 minutes is treated as a crash
if exist "%LOCK%" (
  for /f "delims=" %%f in ('powershell -NoProfile -Command "(Get-Date)-(Get-Item '%LOCK%').LastWriteTime | %%TotalMinutes"') do set AGE=%%f
  for /f "delims=." %%a in ("!AGE!") do set AGE=%%a
  if !AGE! LSS 40 (
    timeout /t 60 /nobreak >nul
    goto :top
  )
)
echo %DATE% %TIME% loop turn > "%LOCK%"
python "%HERE%brief.py" --for windows > "%PROMPT%"
cd /d "%ROOT%"
call %AGENT_CMD% %AGENT_ARGS% < "%PROMPT%"
del "%LOCK%" 2>nul
timeout /t 45 /nobreak >nul
goto :top
