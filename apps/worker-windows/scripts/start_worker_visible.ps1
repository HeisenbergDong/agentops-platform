param(
    [string]$Config = "",
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

$arguments = @()
if ($Config) {
    $arguments += @("--config", $Config)
}
$arguments += $WorkerArgs

if (Test-Path $Exe) {
    $command = $Exe
    $argumentList = $arguments
} else {
    $command = $Python
    $argumentList = @("-m", "worker.main") + $arguments
}

Write-Host "Starting AgentOps Worker in a visible window."
Write-Host "Close that window to stop this temporary worker."
Write-Host "This script does not install autostart or a Windows service."

$startParams = @{
    FilePath = $command
    WorkingDirectory = $ProjectRoot
    WindowStyle = "Normal"
}
if ($argumentList.Count -gt 0) {
    $startParams.ArgumentList = $argumentList
}

Start-Process @startParams | Out-Null
