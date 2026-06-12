param(
    [string]$ServiceName = "AgentOpsWorker",
    [string]$ScheduledTaskName = "AgentOpsWorker",
    [string]$LogDir = ""
)

$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName $ScheduledTaskName -ErrorAction SilentlyContinue
if ($task) {
    $taskInfo = Get-ScheduledTaskInfo -TaskName $ScheduledTaskName -ErrorAction SilentlyContinue
    Write-Host "Scheduled task: $ScheduledTaskName"
    Write-Host "  State: $($task.State)"
    if ($taskInfo) {
        Write-Host "  Last run: $($taskInfo.LastRunTime)"
        Write-Host "  Last result: $($taskInfo.LastTaskResult)"
        Write-Host "  Next run: $($taskInfo.NextRunTime)"
    }
} else {
    Write-Host "Scheduled task: not installed"
}

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service) {
    $serviceInfo = Get-CimInstance Win32_Service | Where-Object { $_.Name -eq $ServiceName } | Select-Object -First 1
    Write-Host "Windows service: $ServiceName"
    Write-Host "  Status: $($service.Status)"
    if ($serviceInfo) {
        Write-Host "  Start type: $($serviceInfo.StartMode)"
    }
} else {
    Write-Host "Windows service: not installed"
}

$patterns = @("agentops-worker.exe", "worker.main", "service-run", "supervise", "AgentOps\worker.json")
$processes = Get-CimInstance Win32_Process |
    Where-Object {
        $commandLine = $_.CommandLine
        $commandLine -and ($patterns | Where-Object { $pattern = $_; $commandLine -like "*$pattern*" })
    } |
    Sort-Object ProcessId -Unique

if ($processes) {
    Write-Host "Processes:"
    foreach ($process in $processes) {
        Write-Host "  $($process.ProcessId) $($process.Name) $($process.CommandLine)"
    }
} else {
    Write-Host "Processes: none"
}

if (-not $LogDir) {
    $base = $env:LOCALAPPDATA
    if (-not $base) {
        $base = $env:APPDATA
    }
    if ($base) {
        $LogDir = Join-Path $base "AgentOps\Worker\logs"
    }
}

if ($LogDir -and (Test-Path $LogDir)) {
    Write-Host "Logs:"
    Get-ChildItem -Path $LogDir -Filter "agentops-worker.log*" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 6 |
        ForEach-Object {
            Write-Host "  $($_.FullName) ($($_.Length) bytes, $($_.LastWriteTime))"
        }
} else {
    Write-Host "Logs: not found"
}
