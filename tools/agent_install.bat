@echo off
rem One-time setup so the Windows side resurrects itself even when the DeepAI Code app is closed.
rem Prefer scheduling from the app (it is cheaper), but this survives app restarts and reboots.
rem   tools\agent_install.bat
setlocal
schtasks /create /f /sc minute /mo 5 /tn "LyphenAgent" /tr "\"%~dp0agent_loop.bat\"" >nul
if errorlevel 1 (
  echo failed - run this from an elevated prompt once
) else (
  echo registered: LyphenAgent, every 5 minutes, starts %~dp0agent_loop.bat
  echo stop it with: schtasks /delete /tn LyphenAgent /f
)
