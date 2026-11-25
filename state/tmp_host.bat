@echo off
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d C:\Users\blake\deepai-data-1\.deepai-code\scratch\2026-11-24-try-to-go-into-github-5017c798\umbra
if not exist dist\test mkdir dist\test
set L=C:\Users\blake\deepai-data-1\.deepai-code\scratch\2026-11-24-try-to-go-into-github-5017c798\luau-build
cl /nologo /O1 /Zi /DEBUG /TP /std:c++17 /DUMBRA_HOST /I C:\Users\blake\deepai-data-1\.deepai-code\scratch\2026-11-24-try-to-go-into-github-5017c798\luau-src\VM\include /I C:\Users\blake\deepai-data-1\.deepai-code\scratch\2026-11-24-try-to-go-into-github-5017c798\luau-src\Compiler\include /I C:\Users\blake\deepai-data-1\.deepai-code\scratch\2026-11-24-try-to-go-into-github-5017c798\luau-src\Common\include /I C:\Users\blake\deepai-data-1\.deepai-code\scratch\2026-11-24-try-to-go-into-github-5017c798\luau-src\Ast\include tests\host.c /Fe:dist\test\RobloxPlayerBeta.exe /Fo:dist/test/ /link /MAP:dist/test/host.map /PDB:dist/test/host.pdb %L%\Luau.Compiler.lib %L%\Luau.Ast.lib %L%\Luau.Bytecode.lib %L%\Luau.VM.lib %L%\Luau.Common.lib user32.lib winhttp.lib msvcrt.lib oldnames.lib
