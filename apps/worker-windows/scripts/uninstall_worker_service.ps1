param(
    [string]$ServiceName = "AgentOpsWorker"
)

$ErrorActionPreference = "Stop"

& (Join-Path $PSScriptRoot "install_worker_service.ps1") -ServiceName $ServiceName -Uninstall
