@echo off
setlocal
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
pushd "%~dp0"
cl /nologo /LD /O1 /Os /GS- /GR- /EHsc- /D_USRDLL /D_WINDLL stub.c /link /ENTRY:DllMain /NODEFAULTLIB:libcmt kernel32.lib /Fe:stub.dll /Fm:stub.map
popd
