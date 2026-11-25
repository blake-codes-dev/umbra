@echo off
rem Builds both payloads. core.dll is the manual-map one (no CRT, position independent,
rem entry called by the injector); stub.dll is the tiny diagnostic used by tests.
setlocal
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
pushd "%~dp0"
cl /nologo /LD /O1 /Os /GS- /GR- /EHsc- /D_USRDLL /D_WINDLL stub.c /link /ENTRY:DllMain /NODEFAULTLIB:libcmt kernel32.lib /Fe:stub.dll /MAP:stub.map

rem /GS- keeps us free of the CRT's stack cookie; every risky deref is in SEH instead
cl /nologo /c /O2 /Os /GS- /EHsc crtinfo.cpp /Fo:crtinfo.obj
cl /nologo /LD /O2 /Os /GS- /GR- /EHs-c- /D_USRDLL core.c ^
   /link /ENTRY:DllMain /NODEFAULTLIB:libcmt /NODEFAULTLIB:vcruntime ^
   kernel32.lib user32.lib winhttp.lib libvcruntime.lib chkstk.obj crtinfo.obj /SECTION:.lystub,ERW /Fe:core.dll /MAP:core.map
popd
