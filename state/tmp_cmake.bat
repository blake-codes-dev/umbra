@echo off
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
set PATH=C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin;C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja;%PATH%
cd /d C:\Users\blake\deepai-data-1\.deepai-code\scratch\2026-11-24-try-to-go-into-github-5017c798
cmake -G Ninja -S luau-src -B luau-build -DCMAKE_BUILD_TYPE=Release -DLUAU_BUILD_CLI=OFF -DLUAU_BUILD_TESTS=OFF -DLUAU_BUILD_WEB=OFF
