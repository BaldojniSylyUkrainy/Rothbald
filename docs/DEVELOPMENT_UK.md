# Розробка Rothbald

Цей документ потрібен лише для роботи з вихідним кодом. Користувачі готових DMG та Windows installer не встановлюють Python, ffmpeg або інші компоненти.

## Локальний запуск із коду

Потрібні Python 3.11+ та `ffmpeg`/`ffprobe`.

### Apple Silicon

1. Запусти `setup.command`.
2. Запусти `start.command`.

### Windows

1. Встанови ffmpeg: `winget install Gyan.FFmpeg`.
2. Запусти `setup.ps1` у PowerShell.
3. Запусти `start.bat`.

## Перевірка

```bash
python -m py_compile server.py transcribe_video.py prepare_semantic.py prepare_models.py model_manager.py rothbald.py
node --check static/app.js
python -m unittest discover -s tests -v
```

## Пакування

Перед PyInstaller виконай `python scripts/prepare_build.py`, а потім `pyinstaller --noconfirm Rothbald.spec`. Збірка містить Python runtime, PySide6/QtWebEngine, `ffmpeg`, `ffprobe` та весь код бекенду.

GitHub Actions нативно збирає Apple Silicon `.app`/DMG на macOS runner і Windows x64 застосунок з інсталятором Inno Setup на Windows runner.
