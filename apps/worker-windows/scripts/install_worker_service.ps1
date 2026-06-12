param(
    [string]$ServiceName = "AgentOpsWorker",
    [string]$DisplayName = "AgentOps Windows Worker",
    [string]$Description = "AgentOps worker supervisor for Windows. Use scheduled-task autostart for Trae CN GUI automation.",
    [string]$Config = "",
    [string]$LogDir = "",
    [double]$RestartDelaySeconds = 5,
    [int]$MaxRestartAttempts = 0,
    [double]$LogMaxMB = 10,
    [int]$LogBackups = 5,
    [switch]$Start,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Exe = Join-Path $ProjectRoot "agentops-worker.exe"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script from an elevated PowerShell session."
    }
}

Assert-Admin

if ($Uninstall) {
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($service) {
        if ($service.Status -ne "Stopped") {
            Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
            $service.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(30))
        }
        sc.exe delete $ServiceName | Out-Null
        Write-Host "Removed Windows service $ServiceName."
    } else {
        Write-Host "Windows service $ServiceName is not installed."
    }
    return
}

if (-not (Test-Path $Exe) -and -not (Test-Path $Python)) {
    throw "No worker runtime found. Use agentops-worker.exe from the package, or create apps\worker-windows\.venv in a source checkout."
}

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service) {
    throw "Windows service $ServiceName already exists. Re-run with -Uninstall first if you want to replace it."
}

if (Test-Path $Exe) {
    $command = $Exe
    $serviceArgs = @("service-run")
} else {
    $command = Join-Path $env:SystemRoot "System32\cmd.exe"
    $serviceArgs = @("/c", "cd", "/d", $ProjectRoot, "&&", $Python, "-m", "worker.main", "service-run")
}

$serviceArgs += @(
    "--service-name", $ServiceName,
    "--restart-delay-seconds", "$RestartDelaySeconds",
    "--max-restart-attempts", "$MaxRestartAttempts",
    "--log-max-mb", "$LogMaxMB",
    "--log-backups", "$LogBackups"
)
if ($Config) {
    $serviceArgs += @("--config", $Config)
}
if ($LogDir) {
    $serviceArgs += @("--log-dir", $LogDir)
}

$quotedArgs = ($serviceArgs | ForEach-Object {
    if ($_ -match "\s") { "`"$_`"" } else { $_ }
}) -join " "
if (Test-Path $Exe) {
    $binaryPath = "`"$command`" $quotedArgs"
} else {
    $binaryPath = "`"$command`" $quotedArgs"
}

New-Service `
    -Name $ServiceName `
    -BinaryPathName $binaryPath `
    -DisplayName $DisplayName `
    -Description $Description `
    -StartupType Automatic `
    -DependsOn "Tcpip" | Out-Null

sc.exe failure $ServiceName reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null
sc.exe failureflag $ServiceName 1 | Out-Null

try {
    if (-not [System.Diagnostics.EventLog]::SourceExists($ServiceName)) {
        New-EventLog -LogName Application -Source $ServiceName
    }
} catch {
    Write-Warning "Could not register event log source ${ServiceName}: $($_.Exception.Message)"
}

Write-Host "Installed Windows service $ServiceName."
Write-Host "Binary path: $binaryPath"
Write-Warning "Windows services run in Session 0 and usually cannot control Trae CN GUI. Prefer install_worker_autostart.ps1 for normal GUI automation."

if ($Start) {
    Start-Service -Name $ServiceName
    Write-Host "Started Windows service $ServiceName."
}
