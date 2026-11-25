@echo off
rem Windows side of the agent mailbox. Same file the container writes, seen instantly: C:\ is
rem mounted at /config over 9p, so there is no sync step.
rem
rem   tools\mail.bat read
rem   tools\mail.bat send "core.dll rebuilt, run the harness"
rem   tools\mail.bat status
setlocal
set HERE=%~dp0
if "%1"=="read"   python "%HERE%mail.py" read --for windows & goto :eof
if "%1"=="send"   python "%HERE%mail.py" send --to container --from windows "%~2" & goto :eof
if "%1"=="status" python "%HERE%mail.py" status & goto :eof
if "%1"=="watch"  python "%HERE%mail.py" watch --for windows & goto :eof
echo usage: mail.bat read ^| send "text" ^| status ^| watch
