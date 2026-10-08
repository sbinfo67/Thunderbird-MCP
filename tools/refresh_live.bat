@echo off
rem Reinstall the bridge add-on only when addon/ changed, stop a daemon that runs old code, then check.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m tbmcp refresh
if errorlevel 1 exit /b 1
rem refresh leaves a closed Thunderbird closed; there is nothing live to check then.
tasklist /FI "IMAGENAME eq thunderbird.exe" /NH | findstr /I /B "thunderbird.exe" >nul
if errorlevel 1 (
    echo Thunderbird is not running, live check skipped.
    exit /b 0
)
".venv\Scripts\python.exe" -m tbmcp doctor --wait 90
exit /b %errorlevel%
