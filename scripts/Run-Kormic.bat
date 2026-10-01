@echo off
setlocal
set "KORMIC_LAUNCHER=%~f0"
set "KORMIC_LAUNCH_MODE=%~1"
powershell.exe -NoLogo -NoProfile -Command "$text = [IO.File]::ReadAllText($env:KORMIC_LAUNCHER); & ([scriptblock]::Create(($text -split '(?m)^# POWERSHELL_START\r?\n', 2)[1]))"
set "KORMIC_EXIT=%ERRORLEVEL%"
if not "%KORMIC_EXIT%"=="0" pause
exit /b %KORMIC_EXIT%
# POWERSHELL_START
$ErrorActionPreference = 'Stop'
try {
    $root = Split-Path -Parent $env:KORMIC_LAUNCHER
    $backend = Join-Path $root 'Kormic_backend_sahil_ammar'
    $frontend = Join-Path $root 'kormic_frontend_ammar_sahil'
    $runtime = Join-Path $root '.runtime'
    $python = Join-Path $runtime 'python311\python.exe'
    if (!(Test-Path -LiteralPath $python)) { $python = Join-Path $backend 'venv\Scripts\python.exe' }
    if (!(Test-Path -LiteralPath $python)) { throw 'Backend Python is missing. Restore the project Python environment first.' }
    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    $node = if ($nodeCommand) { $nodeCommand.Source } else { Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' }
    if (!(Test-Path -LiteralPath $node)) { throw 'Node.js is missing. Install Node.js 22 or newer.' }
    foreach ($file in @((Join-Path $backend 'manage.py'), (Join-Path $frontend 'scripts\serve.mjs'))) {
        if (!(Test-Path -LiteralPath $file)) { throw "Required project file missing: $file" }
    }
    $env:PATH = (Split-Path -Parent $node) + ';' + $env:PATH
    $env:PORT = '5173'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:INVITE_DELIVERY_MODE = 'database'
    $processes = @(Get-CimInstance Win32_Process)
    function Get-PortOwner([int]$port) {
        $listener = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object LocalPort -eq $port | Select-Object -First 1
        if ($listener) { return ($processes | Where-Object ProcessId -eq $listener.OwningProcess | Select-Object -First 1) }
    }
    $backendOwner = Get-PortOwner 8000
    $frontendOwner = Get-PortOwner 5173
    if ($backendOwner -and !($backendOwner.ExecutablePath -eq $python -and $backendOwner.CommandLine -match 'manage\.py\s+runserver\s+127\.0\.0\.1:8000')) {
        throw 'Port 8000 belongs to another process. Close that service before starting Kormic.'
    }
    if ($frontendOwner -and !($frontendOwner.ExecutablePath -eq $node -and $frontendOwner.CommandLine -match 'scripts[\\/]serve\.mjs')) {
        throw 'Port 5173 belongs to another process. Close that service before starting Kormic.'
    }
    $worker = $processes | Where-Object { $_.ExecutablePath -eq $python -and $_.CommandLine -match 'manage\.py\s+github_worker(?:\s|$)' } | Select-Object -First 1
    $researchWorker = $processes | Where-Object { $_.ExecutablePath -eq $python -and $_.CommandLine -match 'manage\.py\s+university_worker(?:\s|$)' } | Select-Object -First 1
    $mailWorker = $processes | Where-Object { $_.ExecutablePath -eq $python -and $_.CommandLine -match 'manage\.py\s+invitation_worker(?:\s|$)' } | Select-Object -First 1
    if ($env:KORMIC_LAUNCH_MODE -eq '--check') {
        Write-Host "Python: $python"
        Write-Host "Node: $node"
        Write-Host "Backend running: $([bool]$backendOwner); frontend running: $([bool]$frontendOwner); worker running: $([bool]$worker)"
        Write-Host "University research worker running: $([bool]$researchWorker); invitation worker running: $([bool]$mailWorker)"
        Write-Host 'Launcher checks passed. Ollama/Qwen is never started by this file.'
        exit 0
    }
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
    if (!(Test-Path -LiteralPath (Join-Path $frontend 'dist\student\index.html')) -or $env:KORMIC_LAUNCH_MODE -eq '--rebuild') {
        Write-Host 'Building the frontend...'
        Push-Location $frontend
        try { & $node scripts/build.mjs; if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' } } finally { Pop-Location }
    }
    if (!$backendOwner) {
        Write-Host 'Applying database migrations...'
        Push-Location $backend
        try { & $python manage.py migrate --noinput; if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' } } finally { Pop-Location }
    }
    function Start-ServiceProcess($name, $executable, $arguments, $directory) {
        $process = Start-Process -FilePath $executable -ArgumentList $arguments -WorkingDirectory $directory -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtime "$name.out.log") -RedirectStandardError (Join-Path $runtime "$name.err.log") -PassThru
        Write-Host "Started $name (PID $($process.Id))."
        return $process
    }
    $started = @()
    if (!$backendOwner) { $started += Start-ServiceProcess 'backend' $python @('-u','manage.py','runserver','127.0.0.1:8000','--noreload') $backend }
    if (!$worker) { $started += Start-ServiceProcess 'github-worker' $python @('-u','manage.py','github_worker') $backend }
    if (!$researchWorker) { $started += Start-ServiceProcess 'university-worker' $python @('-u','manage.py','university_worker') $backend }
    if (!$mailWorker) { $started += Start-ServiceProcess 'invitation-worker' $python @('-u','manage.py','invitation_worker') $backend }
    if (!$frontendOwner) { $started += Start-ServiceProcess 'frontend' $node @('scripts/serve.mjs') $frontend }
    function Wait-Http([string]$url, [int]$expected) {
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            $status = 0
            try { $status = (Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2).StatusCode }
            catch { if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode } }
            if ($status -eq $expected) { return }
            Start-Sleep -Seconds 1
        }
        throw "Service did not become ready: $url. Check logs in $runtime."
    }
    Wait-Http 'http://127.0.0.1:8000/api/profile/github/overview/' 401
    Wait-Http 'http://127.0.0.1:5173/student/' 200
    foreach ($process in $started) { $process.Refresh(); if ($process.HasExited) { throw "Process $($process.Id) exited. Check logs in $runtime." } }
    Write-Host "Kormic is ready: http://127.0.0.1:5173/student/"
    Write-Host "Logs: $runtime"
    Write-Host 'Ollama/Qwen was not started. GitHub extraction uses Claude when Qwen is unavailable.'
    Write-Host 'Services keep running after this window closes. Re-running this file reuses them.'
    if ($env:KORMIC_LAUNCH_MODE -ne '--no-browser') { Start-Process 'http://127.0.0.1:5173/student/' }
    exit 0
} catch {
    Write-Host "Kormic could not start: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
