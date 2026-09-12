@echo off
rem Builds "dist\Kling Studio.exe": one file, no console, no Python needed to run it.
rem Only this build step needs PyInstaller:  python -m pip install pyinstaller
cd /d "%~dp0"

python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo PyInstaller is not installed. Install it with:
  echo   python -m pip install pyinstaller
  exit /b 1
)

if not exist "ui\app.ico" python tools\make_icon.py

python -m PyInstaller --noconfirm --clean --onefile --windowed --name "Kling Studio" --icon "ui\app.ico" --add-data "ui;ui" "Kling Studio.pyw"
if errorlevel 1 exit /b 1

echo.
echo Built: %~dp0dist\Kling Studio.exe
