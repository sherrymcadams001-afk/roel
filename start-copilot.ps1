# Start-Copilot.ps1
# Starts Chrome (CDP), LotL Controller, and the orchestrator in Copilot+WhatsApp mode.

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path $MyInvocation.MyCommand.Path

# Adjust path if we are in the root folder but the code is in zero-main
$InnerRoot = Join-Path $ScriptDir "zero-main"

if (Test-Path $InnerRoot) {
    Write-Host "Code detected in nested 'zero-main' folder. adjusting paths..."
    $BaseDir = $InnerRoot
} else {
    $BaseDir = $ScriptDir
}

$LotlDir = Join-Path $BaseDir "lotl"
$OrchestratorDir = Join-Path $BaseDir "imessage_orchestrator"

# venv lives at the OUTER repo root by default (C:\...\zero-main\.venv)
$OuterVenvPython = Join-Path $ScriptDir ".venv\Scripts\python.exe"
$InnerVenvPython = Join-Path $BaseDir ".venv\Scripts\python.exe"
$PythonExe = $null
if (Test-Path $OuterVenvPython) { $PythonExe = $OuterVenvPython }
elseif (Test-Path $InnerVenvPython) { $PythonExe = $InnerVenvPython }
else { $PythonExe = "python" }

$OrchestratorPy = Join-Path $OrchestratorDir "orchestrator.py"

# 1. Environment (Windows: WhatsApp-only, Copilot provider)
$env:LLM_PROVIDER = "copilot"
$env:ENABLE_IMESSAGE = "false"
$env:ENABLE_WHATSAPP = "true"
$env:LOTL_BASE_URL = "http://127.0.0.1:3000"

# 2. Ensure Chrome CDP is available (port 9222)
Write-Host "Ensuring Chrome remote debugging (CDP) is available on :9222..." -ForegroundColor Cyan
Push-Location $LotlDir
try {
    $null = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:9222/json/version"
    Write-Host "   CDP detected on :9222" -ForegroundColor Green
} catch {
    Write-Host "   CDP not detected. Launching Chrome via LotL scripts..." -ForegroundColor Yellow
    Start-Process -FilePath "npm" -ArgumentList "run", "launch-chrome:win" -WindowStyle Minimized
    Start-Sleep -Seconds 3
}
Pop-Location

# 3. Start LotL Controller (Node.js)
Write-Host "Starting LotL Controller (Node.js)..." -ForegroundColor Cyan
if (Test-Path "$LotlDir\node_modules") {
    $LotlHealthUrl = "http://127.0.0.1:3000/health"
    $LotlLogsDir = Join-Path $OrchestratorDir "data\logs"
    New-Item -ItemType Directory -Force -Path $LotlLogsDir | Out-Null
    $LotlStdoutLog = Join-Path $LotlLogsDir "lotl-controller.stdout.log"
    $LotlStderrLog = Join-Path $LotlLogsDir "lotl-controller.stderr.log"

    $IsLotlHealthy = $false
    try {
        $null = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri $LotlHealthUrl
        $IsLotlHealthy = $true
    } catch {
        $IsLotlHealthy = $false
    }

    if (-not $IsLotlHealthy) {
        Write-Host "   Starting controller background process..." -ForegroundColor Cyan
        $NpmExe = "npm.cmd"
        $proc = Start-Process -FilePath $NpmExe -ArgumentList @("run", "start:local") -WorkingDirectory $LotlDir -WindowStyle Minimized -RedirectStandardOutput $LotlStdoutLog -RedirectStandardError $LotlStderrLog -PassThru
        Write-Host "   Controller PID: $($proc.Id)" -ForegroundColor DarkGray

        $Deadline = (Get-Date).AddSeconds(20)
        while ((Get-Date) -lt $Deadline) {
            try {
                $null = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri $LotlHealthUrl
                $IsLotlHealthy = $true
                break
            } catch {
                Start-Sleep -Milliseconds 500
            }
        }

        if (-not $IsLotlHealthy) {
            Write-Host "   LotL did not become ready on $LotlHealthUrl" -ForegroundColor Red
            if (Test-Path $LotlStderrLog) {
                Write-Host "   stderr log: $LotlStderrLog" -ForegroundColor DarkGray
            }
            if (Test-Path $LotlStdoutLog) {
                Write-Host "   stdout log: $LotlStdoutLog" -ForegroundColor DarkGray
            }
            throw "LotL Controller failed to start or bind to port 3000. See logs in $LotlLogsDir"
        }

        Write-Host "   Controller ready." -ForegroundColor Green
    } else {
        Write-Host "   Controller already responding on $LotlHealthUrl" -ForegroundColor Yellow
    }
} else {
    Write-Error "node_modules not found in $LotlDir"
}

# 4. Start Orchestrator (backend loop)
Write-Host "Starting Orchestrator (Copilot + WhatsApp)..." -ForegroundColor Magenta
Write-Host "   Using Python: $PythonExe"
Write-Host "   Press Ctrl+C to stop."

if (-not (Test-Path $OrchestratorPy)) {
    throw "Orchestrator not found: $OrchestratorPy"
}

Set-Location $OrchestratorDir
& $PythonExe $OrchestratorPy
