$ErrorActionPreference = "Stop"

$currentPid = $PID
$patterns = @(
    "agentops-worker.exe",
    "worker.main",
    "apps\worker-windows",
    "AgentOps\worker.json"
)

$targets = Get-CimInstance Win32_Process |
    Where-Object {
        $commandLine = $_.CommandLine
        $_.ProcessId -ne $currentPid -and
        $commandLine -and
        ($patterns | Where-Object { $pattern = $_; $pattern -and $commandLine -like "*$pattern*" })
    } |
    Sort-Object ProcessId -Unique

if (-not $targets) {
    Write-Host "No local AgentOps Worker process is running."
    return
}

foreach ($target in $targets) {
    Write-Host "Stopping AgentOps Worker process $($target.ProcessId) $($target.Name)"
    Stop-Process -Id $target.ProcessId -Force
}

Write-Host "Stopped $($targets.Count) AgentOps Worker process(es)."
