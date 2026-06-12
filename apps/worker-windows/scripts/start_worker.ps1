param(
    [switch]$Supervise,
    [string]$Config = "",
    [string]$LogDir = "",
    [double]$RestartDelaySeconds = 5,
    [int]$MaxRestartAttempts = 0,
    [double]$LogMaxMB = 10,
    [int]$LogBackups = 5,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$WorkerArgs
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Exe = Join-Path $ProjectRoot "agentops-worker.exe"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Exe) -and -not (Test-Path $Python)) {
    throw "No worker runtime found. Use agentops-worker.exe from the package, or create apps\worker-windows\.venv in a source checkout."
}

Set-Location $ProjectRoot

if ($Supervise) {
    if (Test-Path $Exe) {
        $command = $Exe
        $supervisorArgs = @("supervise")
    } else {
        $command = $Python
        $supervisorArgs = @("-m", "worker.main", "supervise")
    }
    $supervisorArgs += @(
        "--restart-delay-seconds", "$RestartDelaySeconds",
        "--max-restart-attempts", "$MaxRestartAttempts",
        "--log-max-mb", "$LogMaxMB",
        "--log-backups", "$LogBackups"
    )
    if ($Config) {
        $supervisorArgs += @("--config", $Config)
    }
    if ($LogDir) {
        $supervisorArgs += @("--log-dir", $LogDir)
    }
    & $command @supervisorArgs
    exit $LASTEXITCODE
}

if (Test-Path $Exe) {
    & $Exe @WorkerArgs
} else {
    & $Python -m worker.main @WorkerArgs
}
exit $LASTEXITCODE
