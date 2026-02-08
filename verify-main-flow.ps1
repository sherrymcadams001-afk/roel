# verify-main-flow.ps1
# Brings up CDP + LotL + Orchestrator + UI and runs probes for the WhatsApp main flow.

$ErrorActionPreference = 'Stop'

function Test-HttpOk {
    param(
        [Parameter(Mandatory=$true)][string]$Url,
        [int]$TimeoutSec = 3
    )
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec $TimeoutSec
        return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300)
    } catch {
        return $false
    }
}

$ScriptDir = Split-Path $MyInvocation.MyCommand.Path
$InnerRoot = Join-Path $ScriptDir 'zero-main'
if (Test-Path $InnerRoot) {
    $BaseDir = $InnerRoot
} else {
    $BaseDir = $ScriptDir
}

$LotlDir = Join-Path $BaseDir 'lotl'
$OrchestratorDir = Join-Path $BaseDir 'imessage_orchestrator'

$OuterVenvPython = Join-Path $ScriptDir '.venv\Scripts\python.exe'
$InnerVenvPython = Join-Path $BaseDir '.venv\Scripts\python.exe'
$PythonExe = if (Test-Path $OuterVenvPython) { $OuterVenvPython } elseif (Test-Path $InnerVenvPython) { $InnerVenvPython } else { 'python' }

$StreamlitExe = Join-Path (Split-Path $PythonExe) 'streamlit.exe'

$LogDir = Join-Path $OrchestratorDir 'data\logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$LotlHealthUrl = 'http://127.0.0.1:3000/health'
$LotlReadyUrl = 'http://127.0.0.1:3000/ready'
$CdpUrl = 'http://127.0.0.1:9222/json/version'

Write-Host "[1/5] Ensuring Chrome CDP on :9222" -ForegroundColor Cyan
if (-not (Test-HttpOk -Url $CdpUrl -TimeoutSec 2)) {
    Push-Location $LotlDir
    try {
        Start-Process -FilePath 'npm.cmd' -ArgumentList @('run','launch-chrome:win') -WindowStyle Minimized
        Start-Sleep -Seconds 3
    } finally {
        Pop-Location
    }
}
if (-not (Test-HttpOk -Url $CdpUrl -TimeoutSec 2)) {
    throw "CDP not available on :9222. Ensure Chrome launched with --remote-debugging-port=9222"
}

Write-Host "[2/5] Ensuring LotL controller on :3000" -ForegroundColor Cyan
if (-not (Test-HttpOk -Url $LotlHealthUrl -TimeoutSec 3)) {
    Get-Process -Name node -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1

    $out = Join-Path $LogDir 'lotl-controller.stdout.log'
    $err = Join-Path $LogDir 'lotl-controller.stderr.log'

    Start-Process -FilePath 'node.exe' -ArgumentList @('scripts/start-controller.js','--host','127.0.0.1') -WorkingDirectory $LotlDir -WindowStyle Minimized -RedirectStandardOutput $out -RedirectStandardError $err | Out-Null
    Start-Sleep -Seconds 2
}

if (-not (Test-HttpOk -Url $LotlHealthUrl -TimeoutSec 3)) {
    throw "LotL controller is not responding on $LotlHealthUrl. See $LogDir\\lotl-controller.stderr.log"
}

# Deep readiness probe
if (-not (Test-HttpOk -Url $LotlReadyUrl -TimeoutSec 10)) {
    throw "LotL /ready failed. Make sure an AI tab is open and logged in. See $LogDir\\lotl-controller.stderr.log"
}

Write-Host "[3/5] Starting Orchestrator (WhatsApp only)" -ForegroundColor Cyan
$env:LLM_PROVIDER = 'copilot'
$env:ENABLE_IMESSAGE = 'false'
$env:ENABLE_WHATSAPP = 'true'
$env:LOTL_BASE_URL = 'http://127.0.0.1:3000'

$orchOut = Join-Path $LogDir 'orchestrator.stdout.log'
$orchErr = Join-Path $LogDir 'orchestrator.stderr.log'

$orchRunning = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'orchestrator\.py' }
if (-not $orchRunning) {
    Start-Process -FilePath $PythonExe -ArgumentList @('orchestrator.py') -WorkingDirectory $OrchestratorDir -WindowStyle Minimized -RedirectStandardOutput $orchOut -RedirectStandardError $orchErr | Out-Null
    Start-Sleep -Seconds 2
}

Write-Host "[4/5] Starting Streamlit UI on :8501" -ForegroundColor Cyan
$uiUp = Test-NetConnection -ComputerName 127.0.0.1 -Port 8501 -InformationLevel Quiet
if (-not $uiUp) {
    if (-not (Test-Path $StreamlitExe)) {
        throw "streamlit.exe not found next to $PythonExe"
    }
    $uiOut = Join-Path $LogDir 'ui.stdout.log'
    $uiErr = Join-Path $LogDir 'ui.stderr.log'
    Start-Process -FilePath $StreamlitExe -ArgumentList @('run','ui.py','--server.port','8501') -WorkingDirectory $OrchestratorDir -WindowStyle Minimized -RedirectStandardOutput $uiOut -RedirectStandardError $uiErr | Out-Null
    $deadline = (Get-Date).AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 400
        $uiUp = Test-NetConnection -ComputerName 127.0.0.1 -Port 8501 -InformationLevel Quiet
    } while (-not $uiUp -and (Get-Date) -lt $deadline)
}

Write-Host "[5/5] Probing WhatsApp poll endpoint" -ForegroundColor Cyan
try {
    $poll = Invoke-RestMethod -Uri 'http://127.0.0.1:3000/whatsapp/poll' -Method GET -TimeoutSec 15
    $poll | ConvertTo-Json -Depth 6
} catch {
    Write-Host "Poll failed: $($_.Exception.Message)" -ForegroundColor Yellow
}

Write-Host "\nNEXT:" -ForegroundColor Green
Write-Host "- Send a WhatsApp message from an UNKNOWN number to the logged-in WhatsApp Web account." -ForegroundColor Green
Write-Host "- Watch logs: $LogDir\\imessage_orchestrator.log" -ForegroundColor Green
Write-Host "- Approvals UI: http://127.0.0.1:8501" -ForegroundColor Green
