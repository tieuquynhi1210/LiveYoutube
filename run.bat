@echo off
REM Chay nhanh LiveYoutube (tu tao venv + cai dependency lan dau)
cd /d "%~dp0"

if not exist ".venv" (
    echo Tao moi truong ao...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

".venv\Scripts\python.exe" -m src.main
pause
