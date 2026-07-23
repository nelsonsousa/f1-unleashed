#requires -Version 5.1
<#
  Formula 1 Live Timing service control (Windows / PowerShell).
  Usage:  .\f1unleashed.ps1 {start|stop|restart|status|install}

  Windows counterpart of f1unleashed.sh. If PowerShell blocks the script, run it
  via the f1unleashed.bat wrapper or:  powershell -ExecutionPolicy Bypass -File f1unleashed.ps1 start
#>

$ErrorActionPreference = 'Stop'

$AppDir  = $PSScriptRoot
$PidFile = Join-Path $AppDir '.server.pid'
$LogFile = Join-Path $AppDir 'server.log'
$VenvPy  = Join-Path $AppDir 'venv\Scripts\python.exe'
$VenvDir = Join-Path $AppDir 'venv'
# Interpreter used to create the venv on first run (override with $env:PYTHON).
$Python  = if ($env:PYTHON) { $env:PYTHON } else { 'python' }

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

# Create the virtualenv + install dependencies on first run if it's missing, so a
# fresh checkout starts out-of-the-box. No-op once the venv exists.
function Initialize-Venv {
    if (Test-Path $VenvPy) { return $true }
    Write-Host "No virtualenv at $VenvDir - creating it (first run)..."
    if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
        Write-Host "Error: '$Python' not found on PATH. Install Python 3.13, or set `$env:PYTHON."
        return $false
    }
    & $Python -m venv $VenvDir
    if (-not (Test-Path $VenvPy)) { Write-Host "Error: failed to create the virtualenv."; return $false }
    Write-Host "Installing dependencies (this runs once)..."
    & $VenvPy -m pip install --quiet --upgrade pip
    & $VenvPy -m pip install -r (Join-Path $AppDir 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { Write-Host "Error: dependency install failed."; return $false }
    Write-Host "Virtualenv ready."
    return $true
}

function Start-Server {
    $running = Get-RunningPid
    if ($running) { Write-Host "Server is already running (PID: $running)"; return }
    if (Test-Path $PidFile) { Remove-Item $PidFile -Force }
    if (-not (Initialize-Venv)) { exit 1 }

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
    'install' { if (-not (Initialize-Venv)) { exit 1 } }
    default   { Write-Host "Usage: .\f1unleashed.ps1 {start|stop|restart|status|install}"; exit 1 }
}
