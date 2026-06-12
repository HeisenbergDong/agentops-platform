param(
    [string]$TaskName = "AgentOpsWorker"
)

$ErrorActionPreference = "Stop"

& (Join-Path $PSScriptRoot "install_worker_autostart.ps1") -TaskName $TaskName -Uninstall
