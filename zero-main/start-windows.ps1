$ErrorActionPreference = "Stop"

$ScriptDir = $PSScriptRoot
$LotlDir = Join-Path $ScriptDir "lotl"
$OrchestratorDir = Join-Path $ScriptDir "imessage_orchestrator"
$PythonExe = "C:/Users/Ghost/Downloads/zero-main/.venv/Scripts/python.exe"

# 1. Install Node Dependencies
if (-not (Test-Path "$LotlDir\node_modules")) {
    Write-Host "Installing dependencies..."
    Push-Location $LotlDir
    npm install
    Pop-Location
}

# 2. Check Chrome
$ChromePort = 9222
$TcpTest = Test-NetConnection -ComputerName localhost -Port $ChromePort -InformationLevel Quiet
if (-not $TcpTest) {
    Write-Host "⚠️  Chrome is not running on port $ChromePort."
    Write-Host "Please start Chrome with: chrome.exe --remote-debugging-port=$ChromePort"
    $ChromePath = "C:\Program Files\Google\Chrome\Application\chrome.exe"
    if (Test-Path $ChromePath) {
        Write-Host "Attempting to launch Chrome..."
        Start-Process $ChromePath -ArgumentList "--remote-debugging-port=$ChromePort", "--user-data-dir=$ScriptDir\.lotl\chrome-data", "https://web.whatsapp.com", "https://aistudio.google.com"
        Start-Sleep -Seconds 5
    } else {
        Write-Host "Could not find Chrome. Please launch it manually."
        # Don't exit, maybe they launch it now
    }
}

# 3. Start LotL Controller
Write-Host "Starting LotL Controller..."
$Env:PORT = 3000
$Env:CHROME_PORT = 9222
$Env:HOST = "127.0.0.1"

# Kill existing node process if running (optional, risky if user runs other node apps)
# Stop-Process -Name node -ErrorAction SilentlyContinue

$ControllerProcess = Start-Process -FilePath "node" -ArgumentList "lotl-controller-v3.js" -WorkingDirectory $LotlDir -PassThru -NoNewWindow
Write-Host "Controller PID: $($ControllerProcess.Id)"
Start-Sleep -Seconds 2

# 4. Start Orchestrator
Write-Host "Starting Orchestrator..."
$Env:LLM_PROVIDER = "lotl"
$Env:LOTL_BASE_URL = "http://127.0.0.1:3000"
$Env:LOTL_TIMEOUT = "180.0"
$Env:PYTHONUNBUFFERED = "1"
$Env:ENABLE_WHATSAPP = "true"
$Env:ENABLE_IMESSAGE = "false"

Push-Location $OrchestratorDir
& $PythonExe main.py
Pop-Location
