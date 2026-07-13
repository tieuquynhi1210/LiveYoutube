# Đóng gói LiveYoutube thành thư mục chạy độc lập (kèm FFmpeg).
# Yêu cầu: đã copy ffmpeg.exe + ffprobe.exe vào resources\ffmpeg\
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    python -m venv .venv
    & ".venv\Scripts\python.exe" -m pip install --upgrade pip
}
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller

if (-not (Test-Path "resources\ffmpeg\ffmpeg.exe")) {
    Write-Warning "Chua co resources\ffmpeg\ffmpeg.exe — goi se KHONG kem FFmpeg."
    Write-Warning "May test se can tu cai FFmpeg, hoac copy ffmpeg.exe/ffprobe.exe vao resources\ffmpeg\."
}

& ".venv\Scripts\pyinstaller.exe" --noconfirm --clean --windowed `
    --name LiveYoutube `
    --add-data "resources;resources" `
    app.py

Write-Host ""
Write-Host "Xong! Goi nam tai: dist\LiveYoutube\" -ForegroundColor Green
Write-Host "Copy/nen ca thu muc dist\LiveYoutube sang may test, chay LiveYoutube.exe." -ForegroundColor Green
