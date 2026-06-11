$ErrorActionPreference = "Stop"

function Ensure-WorkerVenv {
    if (Test-Path ".\.venv\Scripts\python.exe") {
        return
    }
    python -m venv .venv
    if ($LASTEXITCODE -eq 0 -and (Test-Path ".\.venv\Scripts\python.exe")) {
        return
    }
    Write-Host "python -m venv is unavailable; falling back to virtualenv."
    python -m pip install virtualenv
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install virtualenv. Install a standard Python 3.11+ runtime and retry."
    }
    python -m virtualenv .venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path ".\.venv\Scripts\python.exe")) {
        throw "Could not create worker virtual environment."
    }
}

Ensure-WorkerVenv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\pip install -e ".[dev]"
Write-Host "Worker environment installed."
