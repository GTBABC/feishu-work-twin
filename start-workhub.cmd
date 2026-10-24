@echo off
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-workhub.ps1" %*
set "workhub_result=%errorlevel%"
if not "%workhub_result%"=="0" (
    echo.
    echo Workhub startup failed. The error is shown above.
    echo Startup log: "%~dp0data\logs\workhub\startup.log"
    pause
)
exit /b %workhub_result%
