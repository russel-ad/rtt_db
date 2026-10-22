@echo off
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

tasklist /FI "IMAGENAME eq rtt_t2.exe" 2>NUL | find /I "rtt_t2.exe" >NUL
if not errorlevel 1 (
    echo Please close %ROOT%dist\rtt_t2.exe before packaging.
    exit /b 1
)

echo Starting to package the application...
python -m PyInstaller --noconfirm --clean -F -w ^
  -i "%ROOT%tool.ico" ^
  --exclude-module scipy ^
  --exclude-module PySide6 --exclude-module PyQt6 ^
  --collect-submodules bleak.backends.winrt ^
  --collect-all winrt --copy-metadata bleak ^
  --specpath "%ROOT%build" ^
  --distpath "%ROOT%dist" ^
  --workpath "%ROOT%build" ^
  "%ROOT%rtt_t2.py"

if errorlevel 1 (
    echo Packaging failed.
    exit /b 1
)

echo Packaging complete: %ROOT%dist\rtt_t2.exe
pause
