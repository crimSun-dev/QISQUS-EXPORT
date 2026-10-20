@echo off
rem Opens the Qiscus Export window (same as the desktop icon). The console menu is qexport.bat.
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  echo qexport is not installed on this PC yet. Running install.bat...
  call "%~dp0install.bat"
  exit /b
)
start "" "%~dp0.venv\Scripts\pythonw.exe" -m qexport gui
