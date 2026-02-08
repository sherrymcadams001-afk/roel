<#
Start-ProjectZero.ps1

Windows launcher for Project Zero + LotL stack.
- Launches Chrome with CDP
- Starts LotL controller (localhost:3000)
- Starts Streamlit UI (localhost:8501)
- Starts Python orchestrator

Designed to be runnable from a Desktop shortcut (via Start-ProjectZero.cmd).
#>

[CmdletBinding()]
param(
    [ValidateSet('lotl','copilot')]
    [string]$Provider = 'lotl',

    [ValidateSet('api','single','multi')]
    [string]$Mode = 'api',

    [string]$HostAddress = '127.0.0.1',

    [int]$ChromePort = 9222,

    [int]$ControllerPort = 3000,

    [int]$UiPort = 8501,

    [switch]$SkipChrome,

    [switch]$SkipVerify
)

$ErrorActionPreference = 'Stop'

function Resolve-RepoRoot {
    # Script is expected to live in repo root.
    $root = $PSScriptRoot
    if (-not $root) {
        throw 'Cannot determine script root.'
    }
    return (Resolve-Path $root).Path
}

function Find-ChromeExe {
    $candidates = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
    )
    foreach ($p in $candidates) {
        if ($p -and (Test-Path $p)) { return $p }
    }
    return $null
}

function Find-VenvPython([string]$repoRoot) {
    $parent = Split-Path -Parent $repoRoot
    $candidates = @(
        (Join-Path $repoRoot '.venv\Scripts\python.exe'),
        (Join-Path $parent '.venv\Scripts\python.exe'),
        (Join-Path $repoRoot 'imessage_orchestrator\.venv\Scripts\python.exe')
    )
    foreach ($p in $candidates) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

function Wait-HttpOk([string]$url, [int]$timeoutSec = 30) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $null = Invoke-RestMethod -Uri $url -Method Get -TimeoutSec 3
            return $true
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    return $false
}

function Format-EnvAssignments([hashtable]$envMap) {
    $parts = @()
    foreach ($k in $envMap.Keys) {
        $v = [string]$envMap[$k]
        $parts += "`$env:$k='$v';"
    }
    return ($parts -join '')
}

$repoRoot = Resolve-RepoRoot
$lotlDir = Join-Path $repoRoot 'lotl'
$orchestratorDir = Join-Path $repoRoot 'imessage_orchestrator'

if (-not (Test-Path $lotlDir)) { throw "LotL directory missing: $lotlDir" }
if (-not (Test-Path $orchestratorDir)) { throw "Orchestrator directory missing: $orchestratorDir" }

$chromeExe = Find-ChromeExe
$pythonExe = Find-VenvPython -repoRoot $repoRoot

if (-not $pythonExe) {
    throw "Python venv not found. Expected .venv at repo root or imessage_orchestrator/.venv."
}

# Ensure LotL deps exist.
if (-not (Test-Path (Join-Path $lotlDir 'node_modules'))) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        throw 'npm not found. Install Node.js (npm) first.'
    }
    Write-Output '[SETUP] Installing LotL dependencies (npm install)...'
    Push-Location $lotlDir
    try {
        npm install
    } finally {
        Pop-Location
    }
}

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw 'node not found. Install Node.js and ensure it is on PATH.'
}

# Common env configuration (Windows defaults: WhatsApp on, iMessage off)
$envBlock = @{
    'LLM_PROVIDER'      = $Provider
    'LOTL_BASE_URL'     = "http://$HostAddress`:$ControllerPort"
    'LOTL_TIMEOUT'      = '180'
    'PYTHONUNBUFFERED'  = '1'
    'ENABLE_IMESSAGE'   = 'false'
    'ENABLE_WHATSAPP'   = 'true'
}

$stateDir = Join-Path $orchestratorDir 'data'
$logDir = Join-Path $stateDir 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$controllerOut = Join-Path $lotlDir 'controller.debug.log'
$controllerErr = Join-Path $lotlDir 'controller.debug.err.log'
$uiOut = Join-Path $orchestratorDir 'streamlit.log'
$uiErr = Join-Path $orchestratorDir 'streamlit.err.log'
$orchOut = Join-Path $logDir 'orchestrator.console.log'
$orchErr = Join-Path $logDir 'orchestrator.console.err.log'

if (-not $SkipChrome) {
    if (-not $chromeExe) {
        throw 'Chrome not found. Install Google Chrome or pass -SkipChrome if already running with CDP.'
    }

    $userDataDir = Join-Path $repoRoot ".lotl\chrome-lotl-$ChromePort"
    New-Item -ItemType Directory -Force -Path $userDataDir | Out-Null

    Write-Output "[1/5] Launching Chrome CDP on :$ChromePort ..."
    $chromeArgs = @(
        "--remote-debugging-port=$ChromePort",
        "--user-data-dir=$userDataDir",
        'https://aistudio.google.com'
    )
    Start-Process -FilePath $chromeExe -ArgumentList $chromeArgs | Out-Null
}

Write-Output "[2/5] Starting LotL controller on :$ControllerPort (mode=$Mode) ..."
$controllerEnv = $envBlock.Clone()

$controllerEnvAssign = Format-EnvAssignments -envMap $controllerEnv

$controllerCmd = @(
    'powershell',
    '-NoProfile',
    '-NoExit',
    '-Command',
    (
        "`$ErrorActionPreference='Stop';" +
        $controllerEnvAssign +
        "Set-Location '$lotlDir';" +
        "node scripts/start-controller.js --host $HostAddress --port $ControllerPort --chrome-port $ChromePort --mode $Mode"
    )
)

$controllerProc = Start-Process -FilePath $controllerCmd[0] -ArgumentList $controllerCmd[1..($controllerCmd.Length-1)] -PassThru -RedirectStandardOutput $controllerOut -RedirectStandardError $controllerErr

if (-not (Wait-HttpOk -url "http://$HostAddress`:$ControllerPort/health" -timeoutSec 45)) {
    throw "LotL controller did not become healthy on http://$HostAddress`:$ControllerPort/health. See $controllerOut"
}
Write-Output "      OK: LotL /health"

Write-Output "[3/5] Starting Streamlit UI on :$UiPort ..."
$commonEnvAssign = Format-EnvAssignments -envMap $envBlock
$uiCmd = @(
    'powershell',
    '-NoProfile',
    '-NoExit',
    '-Command',
    (
        "`$ErrorActionPreference='Stop';" +
        $commonEnvAssign +
        "Set-Location '$repoRoot';" +
        "& '$pythonExe' -m streamlit run '$orchestratorDir\ui.py' --server.headless true --server.address 127.0.0.1 --server.port $UiPort"
    )
)

$uiProc = Start-Process -FilePath $uiCmd[0] -ArgumentList $uiCmd[1..($uiCmd.Length-1)] -PassThru -RedirectStandardOutput $uiOut -RedirectStandardError $uiErr

# Streamlit health endpoint.
$uiHealth = "http://127.0.0.1`:$UiPort/_stcore/health"
if (-not (Wait-HttpOk -url $uiHealth -timeoutSec 60)) {
    throw "Streamlit UI did not become healthy at $uiHealth. See $uiOut"
}
Write-Output "      OK: UI healthy: http://127.0.0.1`:$UiPort"

Write-Output "[4/5] Starting Orchestrator ..."
$orchCmd = @(
    'powershell',
    '-NoProfile',
    '-NoExit',
    '-Command',
    (
        "`$ErrorActionPreference='Stop';" +
        $commonEnvAssign +
        "Set-Location '$repoRoot';" +
        "& '$pythonExe' -u '$orchestratorDir\main.py'"
    )
)

$orchProc = Start-Process -FilePath $orchCmd[0] -ArgumentList $orchCmd[1..($orchCmd.Length-1)] -PassThru -RedirectStandardOutput $orchOut -RedirectStandardError $orchErr

Write-Output "      OK: Orchestrator started (logs: $orchOut)"

Write-Output '[5/5] Optional endpoint verification ...'
if (-not $SkipVerify) {
    $verify = Join-Path $lotlDir 'verify_endpoints.ps1'
    if (Test-Path $verify) {
        try {
            powershell -NoProfile -ExecutionPolicy Bypass -File $verify | Out-Host
        } catch {
            Write-Warning "Endpoint verifier failed: $($_.Exception.Message)"
        }
    } else {
        Write-Warning "Verifier missing: $verify"
    }
}

Write-Output ''
Write-Output 'All services launched.'
Write-Output "- LotL:   http://$HostAddress`:$ControllerPort/health"
Write-Output "- UI:     http://127.0.0.1`:$UiPort"
Write-Output "- Logs:   $controllerOut ; $controllerErr ; $uiOut ; $uiErr ; $orchOut ; $orchErr"
Write-Output ''
Write-Output 'Tip: close the three spawned PowerShell windows to stop services.'
