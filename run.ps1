# Chạy nhanh LiveYoutube (tự tạo venv + cài dependency lần đầu)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    Write-Host "Tạo môi trường ảo..." -ForegroundColor Cyan
    python -m venv .venv
    & ".venv\Scripts\python.exe" -m pip install --upgrade pip
    & ".venv\Scripts\python.exe" -m pip install -r requirements.txt
}

& ".venv\Scripts\python.exe" -m src.main
