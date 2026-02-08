# stop-stack.ps1
# Best-effort stop of local Project Zero processes.

$ErrorActionPreference = 'SilentlyContinue'

function Stop-ListenerOnPort([int]$Port) {
    try {
        $line = (netstat -ano | findstr ":$Port" | findstr "LISTENING" | Select-Object -First 1)
        if (-not $line) { return }
        $pid = (($line -split "\s+")[-1] -as [int])
        if ($pid -and $pid -gt 0) {
            Stop-Process -Id $pid -Force
        }
    } catch {
        # ignore
    }
}

Stop-ListenerOnPort 3000
Stop-ListenerOnPort 8501

try {
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -match 'orchestrator\.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
} catch {}

try {
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -match 'streamlit' -and $_.CommandLine -match 'ui\.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
} catch {}

# If the controller was launched via node without binding to 3000 (rare), also stop by script name.
try {
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -match 'lotl-controller-v3\.js' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
} catch {}

Write-Host 'Stopped stack processes (best-effort).'
