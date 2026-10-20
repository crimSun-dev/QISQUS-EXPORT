@echo off
rem ============================================================================
rem  qexport launcher.
rem    Double-click        -> menu
rem    qexport.bat <args>  -> runs that command directly, e.g. "qexport.bat status"
rem ============================================================================
setlocal EnableExtensions
rem <nul: chcp otherwise swallows redirected stdin.
chcp 65001 >nul <nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "VPY=%~dp0.venv\Scripts\python.exe"

if not exist "%VPY%" (
  echo qexport is not installed on this PC yet. Running install.bat...
  call "%~dp0install.bat"
  exit /b
)

if "%~1"=="" goto :menu_start
rem Outside parentheses so %errorlevel% is read after the command runs.
"%VPY%" -m qexport %*
exit /b %errorlevel%

:menu_start
title Qiscus Export
:menu
cls
echo.
echo  =====================================================
echo    Qiscus Export
echo  =====================================================
echo.
echo   1. Download everything  (first run, or continue an unfinished one)
echo   2. Update               (only conversations/campaigns with new activity)
echo   3. Show progress
echo   4. Rebuild the Excel file from downloaded data  (no internet needed)
echo   5. Retry items that failed
echo   6. Check the Qiscus login
echo   7. Set up / change the Qiscus login
echo   8. Open the exports folder
echo   9. Open the README
echo   A. Open the Qiscus Export window (the easier way to do all of the above)
echo   0. Exit
echo.
choice /c 1234567890A /n /m "Choose 0-9 or A: "
set "OPT=%errorlevel%"
if "%OPT%"=="10" goto :end
if "%OPT%"=="11" (
  start "" "%~dp0.venv\Scripts\pythonw.exe" -m qexport gui
  goto :menu
)
if "%OPT%"=="1" call :run export
if "%OPT%"=="2" call :run export --since-last-run
if "%OPT%"=="3" call :run status
if "%OPT%"=="4" call :run write-xlsx
if "%OPT%"=="5" call :run resume --retry-skipped
if "%OPT%"=="6" call :run auth-check
if "%OPT%"=="7" call :run setup
if "%OPT%"=="8" (
  if not exist "%~dp0exports" mkdir "%~dp0exports"
  start "" explorer "%~dp0exports"
)
if "%OPT%"=="9" start "" notepad "%~dp0README.md"
goto :menu

:run
echo.
"%VPY%" -m qexport %*
set "RC=%errorlevel%"
echo.
if "%RC%"=="0"   echo  Finished successfully.
if "%RC%"=="2"   echo  The Qiscus login expired or is wrong. Use option 7, then option 1 to continue.
if "%RC%"=="3"   echo  Finished, but some items could not be exported. See the "Export Info" sheet.
if "%RC%"=="4"   echo  Qiscus is rate-limiting. Progress is saved; wait a few minutes, then option 1.
if "%RC%"=="5"   echo  Qiscus refused a request or changed its API. Progress is saved; send the message above to support.
if "%RC%"=="6"   echo  Could not reach Qiscus. Check the internet connection, then option 1 to continue.
if "%RC%"=="130" echo  Stopped. Progress is saved; option 1 continues where it left off.
echo.
pause
goto :eof

:end
endlocal
exit /b 0
