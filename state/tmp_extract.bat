@echo off
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d C:\Users\blake\deepai-data-1\.deepai-code\scratch\2026-11-24-try-to-go-into-github-5017c798\umbra\src\native
lib /nologo /extract:chkstk.obj "%VCToolsInstallDir%lib\x64\libvcruntime.lib" /out:chkstk.obj
