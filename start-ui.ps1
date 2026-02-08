# start-ui.ps1
# Starts Streamlit UI for Project Zero on Windows.

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path $MyInvocation.MyCommand.Path

$InnerRoot = Join-Path $ScriptDir "zero-main"
if (Test-Path $InnerRoot) { $BaseDir = $InnerRoot } else { $BaseDir = $ScriptDir }

$OrchestratorDir = Join-Path $BaseDir "imessage_orchestrator"

$OuterVenvPython = Join-Path $ScriptDir ".venv\Scripts\python.exe"
$InnerVenvPython = Join-Path $BaseDir ".venv\Scripts\python.exe"
$PythonExe = $null
if (Test-Path $OuterVenvPython) { $PythonExe = $OuterVenvPython }
elseif (Test-Path $InnerVenvPython) { $PythonExe = $InnerVenvPython }
else { $PythonExe = "python" }

# Mirror runtime env used by the orchestrator
$env:ENABLE_IMESSAGE = "false"
$env:ENABLE_WHATSAPP = "true"
$env:LOTL_BASE_URL = "http://127.0.0.1:3000"
$env:LLM_PROVIDER = "copilot"

$UiPy = Join-Path $OrchestratorDir "ui.py"
if (-not (Test-Path $UiPy)) { throw "UI not found: $UiPy" }

Set-Location $ScriptDir
& $PythonExe -m streamlit run $UiPy --server.address 127.0.0.1 --server.port 8501
