param(
    [string]$ServiceName = "AgentOpsWorker",
    [string]$ScheduledTaskName = "AgentOpsWorker",
    [switch]$SkipService,
    [switch]$SkipScheduledTask,
    [switch]$ProcessOnly
)

$ErrorActionPreference = "Stop"

if (-not $ProcessOnly -and -not $SkipScheduledTask) {
    $task = Get-ScheduledTask -TaskName $ScheduledTaskName -ErrorAction SilentlyContinue
    if ($task) {
        Write-Host "Stopping scheduled task $ScheduledTaskName."
        Stop-ScheduledTask -TaskName $ScheduledTaskName -ErrorAction SilentlyContinue
    }
}

if (-not $ProcessOnly -and -not $SkipService) {
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($service -and $service.Status -ne "Stopped") {
        Write-Host "Stopping Windows service $ServiceName."
        Stop-Service -Name $ServiceName -ErrorAction SilentlyContinue
    }
}

$currentPid = $PID
$patterns = @(
    "agentops-worker.exe",
    "worker.main",
    "service-run",
    "supervise",
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
