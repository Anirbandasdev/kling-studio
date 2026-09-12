@echo off
rem Ship an update:  publish.bat 3.1.0 --notes "what changed" "and this"
cd /d "%~dp0"
set PYTHONUTF8=1
python "%~dp0tools\publish.py" %*
