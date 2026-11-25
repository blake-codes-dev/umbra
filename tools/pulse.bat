@echo off
rem Liveness beat for the Windows agent. Call it after each meaningful step; if it goes stale the
rem container side gets an alert instead of waiting on a reply that is never coming.
rem   tools\pulse.bat beat "pinning gettop for 2.748"
rem   tools\pulse.bat status
setlocal
if "%1"=="beat"   python "%~dp0pulse.py" beat --as windows --note "%~2" & goto :eof
if "%1"=="status" python "%~dp0pulse.py" status & goto :eof
echo usage: pulse.bat beat "what you are doing" ^| status
