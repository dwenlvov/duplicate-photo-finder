@echo off
rem Starts the program: the page opens in the browser. Closing this window switches the program off.
cd /d "%~dp0"
set PYTHONUTF8=1
"%~dp0python\python.exe" "%~dp0launcher.py"
if errorlevel 1 pause
