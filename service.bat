@echo off
REM Formula 1 Live Timing service control (Windows).
REM Wraps service.ps1 so it runs regardless of PowerShell execution policy.
REM Usage: service.bat {start^|stop^|restart^|status}
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0service.ps1" %*
