$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
  throw "Потрібен Python 3.11 або новіший."
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
  throw "Не знайдено ffmpeg. Встанови: winget install Gyan.FFmpeg"
}
python -c "import sys; raise SystemExit(sys.version_info < (3, 11))"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
  python -m venv .venv
}
& .venv\Scripts\python.exe -m pip install --upgrade pip
& .venv\Scripts\python.exe -m pip install -r requirements-windows.lock
Write-Host "Готово. Запусти start.bat — моделі завантажаться у вікні Rothbald."
