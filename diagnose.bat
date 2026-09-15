@echo off
rem Run this when torch fails to load (c10.dll WinError 1114).
rem It prints system facts + the full torch error. Send the output
rem to the developer. Press any key to close at the end.
cd /d "%~dp0"
echo ===== OS =====
wmic os get Caption,Version,BuildNumber /value 2>nul
echo ===== CPU =====
wmic cpu get Name /value 2>nul
echo ===== VC runtime DLLs in System32 =====
dir /b C:\Windows\System32\vcruntime140.dll C:\Windows\System32\vcruntime140_1.dll C:\Windows\System32\msvcp140.dll C:\Windows\System32\msvcp140_1.dll C:\Windows\System32\msvcp140_2.dll C:\Windows\System32\concrt140.dll 2>nul
echo ===== VC redist version (registry) =====
reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" /v Version 2>nul
echo ===== Python =====
.venv\Scripts\python.exe -c "import sys,platform; print(platform.architecture()); print(sys.version)" 2>&1
echo ===== torch import (full error) =====
.venv\Scripts\python.exe -X faulthandler -c "import torch; print('TORCH OK', torch.__version__)" 2>&1
echo ===== done =====
pause
