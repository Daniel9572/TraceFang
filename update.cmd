@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\service.ps1" update %*
set "tracefangExit=%ERRORLEVEL%"
if not "%tracefangExit%"=="0" pause
exit /b %tracefangExit%
