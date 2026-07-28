# Розробка Rothbald

Цей документ потрібен лише для роботи з вихідним кодом. Користувачі готових DMG та Windows installer не встановлюють Python, ffmpeg або інші компоненти.

## Локальний запуск із коду

Потрібні Python 3.11+ та `ffmpeg`/`ffprobe`. CI й release використовують Python 3.12.

### Apple Silicon

1. Запусти `setup.command`.
2. Запусти `start.command`.

### Windows

1. Встанови ffmpeg: `winget install Gyan.FFmpeg`.
2. Запусти `setup.ps1` у PowerShell.
3. Для Vulkan GPU встанови Visual Studio 2022 C++ tools і запусти `scripts/build_whisper_cpp_windows.ps1`. Скрипт сам підготує зафіксований Vulkan SDK у `build/`, якщо системного SDK немає.
4. Запусти `start.bat`.

## Перевірка

```bash
ast-grep scan .
ast-grep test
python -m py_compile server.py transcribe_video.py prepare_semantic.py prepare_models.py model_manager.py rothbald.py
node --check static/app.js
python -m unittest discover -s tests -v
```

Source setup встановлює повний platform lock: `requirements-macos.lock` або
`requirements-windows.lock`. PyInstaller додатково використовує відповідний
`requirements-build-*.lock`. `requirements.txt` і `requirements-build.txt` є
вхідними файлами для регенерації lock-файлів, а не CI install targets.

## Пакування

Перед PyInstaller встанови runtime/build lock для поточної платформи. На Windows
також виконай `scripts/build_whisper_cpp_windows.ps1`: скрипт перевіряє SHA-256
зафіксованих Vulkan SDK і whisper.cpp, збирає статичний Vulkan
`whisper-cli.exe` та точний probe індексів Vulkan GPU. Потім виконай
`python scripts/prepare_build.py` і `pyinstaller --noconfirm Rothbald.spec`.
Для macOS build environment має містити `MACOSX_DEPLOYMENT_TARGET=14.0`. Збірка
містить Python runtime, PySide6/QtWebEngine, `ffmpeg`, `ffprobe` та весь код бекенду.

`sgconfig.yml` підключає правила з `rules/`, а їхні fixtures і snapshots лежать
у `rule-tests/`. Структурний review завжди починай з `ast-grep scan .`, після
зміни правил запускай `ast-grep test`, і лише потім переходь до звичайних тестів.

GitHub Actions нативно збирає Apple Silicon `.app`/DMG на macOS runner і Windows x64 застосунок з інсталятором Inno Setup на Windows runner.
