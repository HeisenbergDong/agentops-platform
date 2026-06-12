param(
    [string]$TaskName = "AgentOpsWorker",
    [string]$Config = "",
    [string]$LogDir = "",
    [double]$RestartDelaySeconds = 5,
    [int]$MaxRestartAttempts = 0,
    [double]$LogMaxMB = 10,
    [int]$LogBackups = 5,
    [switch]$RunElevated,
    [switch]$RunNow,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Exe = Join-Path $ProjectRoot "agentops-worker.exe"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if ($Uninstall) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task $TaskName."
    } else {
        Write-Host "Scheduled task $TaskName is not installed."
    }
    return
}

if (-not (Test-Path $Exe) -and -not (Test-Path $Python)) {
    throw "No worker runtime found. Use agentops-worker.exe from the package, or create apps\worker-windows\.venv in a source checkout."
}

$argumentList = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$(Join-Path $ProjectRoot 'scripts\start_worker.ps1')`"",
    "-Supervise",
    "-RestartDelaySeconds", "$RestartDelaySeconds",
    "-MaxRestartAttempts", "$MaxRestartAttempts",
    "-LogMaxMB", "$LogMaxMB",
    "-LogBackups", "$LogBackups"
)
if ($Config) {
    $argumentList += @("-Config", "`"$Config`"")
}
if ($LogDir) {
    $argumentList += @("-LogDir", "`"$LogDir`"")
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ($argumentList -join " ") `
    -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 365) `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable
$runLevel = if ($RunElevated) { "Highest" } else { "Limited" }
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel $runLevel

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "AgentOps Windows Worker interactive supervisor. Recommended for Trae CN GUI automation." `
    -Force | Out-Null

Write-Host "Installed scheduled task $TaskName for interactive logon autostart."
Write-Host "Task action: powershell.exe $($argumentList -join ' ')"

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started scheduled task $TaskName."
}
