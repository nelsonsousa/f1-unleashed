@echo off
REM Formula 1 Live Timing service control (Windows).
REM Wraps f1unleashed.ps1 so it runs regardless of PowerShell execution policy.
REM Usage: f1unleashed.bat {start^|stop^|restart^|status}
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0f1unleashed.ps1" %*
