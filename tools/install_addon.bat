@echo off
rem Build the bridge XPI, install it permanently and restart Thunderbird.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m tbmcp install-addon --yes
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m tbmcp doctor --wait 90
exit /b %errorlevel%
