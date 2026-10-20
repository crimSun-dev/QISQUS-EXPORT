@echo off
rem ============================================================================
rem  qexport installer — sets up the Qiscus exporter on this PC.
rem  Safe to run again: it repairs/updates the install and keeps your data.
rem ============================================================================
setlocal EnableExtensions
rem <nul: chcp otherwise swallows redirected stdin.
chcp 65001 >nul <nul
cd /d "%~dp0"
title qexport installer
set "PYTHONUTF8=1"
set "VENV=%~dp0.venv"
set "VPY=%VENV%\Scripts\python.exe"

echo.
echo  =====================================================
echo    qexport - Qiscus chat / broadcast exporter setup
echo  =====================================================
echo.

rem ---------------------------------------------------------------- 1. Python
echo [1/5] Looking for Python 3.11 or newer...
call :find_python
if defined PY goto :have_python

echo       Python 3.11+ not found.
where winget >nul 2>&1
if errorlevel 1 goto :no_winget
echo       Installing Python 3.12 with winget (this can take a few minutes)...
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
call :find_python
if defined PY goto :have_python

:no_winget
echo.
echo  Python could not be installed automatically.
echo  1. A download page will open. Install Python 3.12 (or newer).
echo  2. On the first installer screen, tick "Add python.exe to PATH".
echo  3. Then run install.bat again.
start "" "https://www.python.org/downloads/windows/"
goto :fail

:have_python
%PY% -c "import sys; print('      Found Python', sys.version.split()[0])"

rem ---------------------------------------------------------------- 2. venv
echo [2/5] Preparing a private Python environment (.venv)...
if exist "%VPY%" goto :venv_ok
%PY% -m venv "%VENV%"
if errorlevel 1 (
  echo  Could not create the environment.
  goto :fail
)
:venv_ok

rem ---------------------------------------------------------------- 3. packages
echo [3/5] Installing qexport and its libraries (needs internet)...
"%VPY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
"%VPY%" -m pip install -e . --quiet --disable-pip-version-check
if errorlevel 1 (
  echo  Package installation failed. Check the internet connection and run install.bat again.
  goto :fail
)
"%VPY%" -m qexport --help >nul 2>&1
if errorlevel 1 (
  echo  qexport did not start after installation.
  goto :fail
)
echo       Installed.

rem ---------------------------------------------------------------- 4. credentials
echo [4/5] Qiscus login...
if not exist ".env" goto :do_setup
"%VPY%" -m qexport auth-check
if errorlevel 1 goto :do_setup
choice /c YN /n /m "      Saved login works. Replace it with a different account? [Y/N] "
if errorlevel 2 goto :shortcut
:do_setup
"%VPY%" -m qexport setup
if errorlevel 1 (
  echo.
  echo  Login setup was not completed. You can finish it later from the menu, option 7.
)

rem ---------------------------------------------------------------- 5. shortcut
:shortcut
if defined QEXPORT_NO_SHORTCUT (
  echo [5/5] Desktop shortcut skipped.
  goto :done
)
echo [5/5] Creating a desktop shortcut "Qiscus Export"...
rem The folder path can contain apostrophes, so it reaches PowerShell through an env var.
set "QX_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=$env:QX_DIR; $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Qiscus Export.lnk')); $s.TargetPath=(Join-Path $d 'qexport.bat'); $s.WorkingDirectory=$d; $s.IconLocation=$env:SystemRoot + '\System32\imageres.dll,184'; $s.Save()" >nul 2>&1
if errorlevel 1 (
  echo       Shortcut skipped. Start the tool by double-clicking qexport.bat in this folder.
) else (
  echo       Done.
)

:done
echo.
echo  =====================================================
echo    Setup complete.
echo    Start it from the "Qiscus Export" desktop icon,
echo    or double-click qexport.bat in this folder.
echo  =====================================================
echo.
choice /c YN /n /m "Open the menu now? [Y/N] "
if errorlevel 2 goto :end
call "%~dp0qexport.bat"
goto :end

rem ============================================================================
:find_python
set "PY="
for %%C in ("py -3.13" "py -3.12" "py -3.11" "python" "python3") do call :try_python %%C
if defined PY goto :eof
for %%D in (Python313 Python312 Python311) do call :try_python_path "%LOCALAPPDATA%\Programs\Python\%%D\python.exe"
goto :eof

:try_python_path
rem A full path may contain spaces, so PY keeps its quotes.
if defined PY goto :eof
if not exist "%~1" goto :eof
"%~1" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set PY="%~1"
goto :eof

:try_python
if defined PY goto :eof
%~1 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
goto :eof

:fail
echo.
echo  Setup did not finish. Fix the problem above and run install.bat again.
pause
exit /b 1

:end
endlocal
exit /b 0
