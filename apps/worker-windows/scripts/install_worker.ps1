$ErrorActionPreference = "Stop"
python -m venv .venv
.\.venv\Scripts\pip install -e ".[dev]"
Write-Host "Worker environment installed."
