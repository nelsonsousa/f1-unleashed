#requires -Version 5.1
<#
  Formula 1 Live Timing service control (Windows / PowerShell).
  Usage:  .\service.ps1 {start|stop|restart|status}

  Windows counterpart of service.sh. If PowerShell blocks the script, run it
  via the service.bat wrapper or:  powershell -ExecutionPolicy Bypass -File service.ps1 start
#>

$ErrorActionPreference = 'Stop'

$AppDir  = $PSScriptRoot
$PidFile = Join-Path $AppDir '.server.pid'
$LogFile = Join-Path $AppDir 'server.log'
$VenvPy  = Join-Path $AppDir 'venv\Scripts\python.exe'

$BindHost = if ($env:HOST) { $env:HOST } else { '0.0.0.0' }
# Exported so the app process (live session monitor) can address its own API.
if (-not $env:PORT) { $env:PORT = '1950' }
$Port = $env:PORT

function Get-RunningPid {
    if (-not (Test-Path $PidFile)) { return $null }
    $procId = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $procId) { return $null }
    if (Get-Process -Id $procId -ErrorAction SilentlyContinue) { return [int]$procId }
    return $null
}

function Start-Server {
    $running = Get-RunningPid
    if ($running) { Write-Host "Server is already running (PID: $running)"; return }
    if (Test-Path $PidFile) { Remove-Item $PidFile -Force }
    if (-not (Test-Path $VenvPy)) {
        Write-Host "venv python not found at $VenvPy"
        Write-Host "Create it with:  python -m venv venv ;  venv\Scripts\pip install -r requirements.txt"
        exit 1
    }

    Write-Host "Starting server..."
    $proc = Start-Process -FilePath $VenvPy `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', $BindHost, '--port', $Port) `
        -WorkingDirectory $AppDir `
        -RedirectStandardOutput $LogFile -RedirectStandardError "$LogFile.err" `
        -WindowStyle Hidden -PassThru
    $proc.Id | Out-File -FilePath $PidFile -Encoding ascii
    Start-Sleep -Seconds 1

    if (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue) {
        Write-Host "Server started (PID: $($proc.Id))"
        Write-Host "Listening on http://${BindHost}:$Port"
        Write-Host "Log file: $LogFile"
    } else {
        Write-Host "Failed to start server. Check $LogFile for details."
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        exit 1
    }
}

function Stop-Server {
    $procId = Get-RunningPid
    if (-not $procId) {
        Write-Host "Server is not running"
        if (Test-Path $PidFile) { Remove-Item $PidFile -Force }
        return
    }
    Write-Host "Stopping server (PID: $procId)..."
    Stop-Process -Id $procId -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 10; $i++) {
        if (-not (Get-Process -Id $procId -ErrorAction SilentlyContinue)) {
            Write-Host "Server stopped"
            Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
            return
        }
        Start-Sleep -Seconds 1
    }
    Write-Host "Force killing server..."
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host "Server stopped"
}

function Get-Status {
    $procId = Get-RunningPid
    if (-not $procId) {
        Write-Host "Server is not running"
        if (Test-Path $PidFile) { Remove-Item $PidFile -Force }
        return
    }
    Write-Host "Server is running (PID: $procId)"
    Write-Host "Listening on http://${BindHost}:$Port"
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:$Port/health" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { Write-Host "Health check: OK" } else { Write-Host "Health check: Not responding" }
    } catch {
        Write-Host "Health check: Not responding"
    }
}

switch ($args[0]) {
    'start'   { Start-Server }
    'stop'    { Stop-Server }
    'restart' { Stop-Server; Start-Sleep -Seconds 1; Start-Server }
    'status'  { Get-Status }
    default   { Write-Host "Usage: .\service.ps1 {start|stop|restart|status}"; exit 1 }
}
