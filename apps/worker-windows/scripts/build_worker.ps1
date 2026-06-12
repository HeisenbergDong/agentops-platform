param(
    [string]$Python = "python",
    [string]$PackageName = "agentops-worker-windows",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$DistDir = Join-Path $ProjectRoot "dist"
$BuildDir = Join-Path $ProjectRoot "build"
$PackageDir = Join-Path $DistDir $PackageName
$ZipPath = Join-Path $DistDir "$PackageName.zip"

Set-Location $ProjectRoot

function Ensure-WorkerVenv {
    if (Test-Path $VenvPython) {
        return
    }
    & $Python -m venv .venv
    if ($LASTEXITCODE -eq 0 -and (Test-Path $VenvPython)) {
        return
    }
    Write-Host "python -m venv is unavailable; falling back to virtualenv."
    & $Python -m pip install virtualenv
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install virtualenv. Install a standard Python 3.11+ runtime and retry."
    }
    & $Python -m virtualenv .venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPython)) {
        throw "Could not create worker virtual environment."
    }
}

if ($Clean) {
    if (Test-Path $BuildDir) {
        Remove-Item -LiteralPath $BuildDir -Recurse -Force
    }
    if (Test-Path $DistDir) {
        Remove-Item -LiteralPath $DistDir -Recurse -Force
    }
}

Ensure-WorkerVenv

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e ".[package]"
& $VenvPython -c "import comtypes.client; comtypes.client.GetModule('UIAutomationCore.dll')"

& $VenvPython -m PyInstaller `
    --noconfirm `
    --onefile `
    --clean `
    --name agentops-worker `
    --paths $ProjectRoot `
    --hidden-import comtypes `
    --hidden-import comtypes.client `
    --hidden-import comtypes.gen.UIAutomationClient `
    --hidden-import mss `
    --hidden-import PIL.Image `
    worker\main.py

New-Item -ItemType Directory -Force -Path $PackageDir | Out-Null
Copy-Item -LiteralPath (Join-Path $DistDir "agentops-worker.exe") -Destination $PackageDir -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot "README.md") -Destination $PackageDir -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot "scripts") -Destination (Join-Path $PackageDir "scripts") -Recurse -Force

if (Test-Path $ZipPath) {
    Remove-Item -LiteralPath $ZipPath -Force
}
Compress-Archive -Path (Join-Path $PackageDir "*") -DestinationPath $ZipPath -Force

Write-Host "Worker EXE: $(Join-Path $PackageDir 'agentops-worker.exe')"
Write-Host "Worker ZIP: $ZipPath"
Write-Host "Start:"
Write-Host "Double-click agentops-worker.exe, or run .\agentops-worker.exe"
Write-Host "First run will ask for server URL and worker registration code."
Write-Host "Autostart:"
Write-Host ".\scripts\install_worker_autostart.ps1 -RunNow"
