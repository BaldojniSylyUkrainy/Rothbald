# Rothbald

**rothpithnavach baldojnyi** — локальний розпізнавач і пошук реплік у монтажних матеріалах.

Rothbald рекурсивно знаходить відео в папці проєкту, локально перетворює мовлення на текст, будує точний і смисловий індекси та відкриває потрібну репліку на таймкоді. Відео й транскрипти не відправляються у хмару.

## Підтримувані платформи

- Apple Silicon: MLX Whisper Turbo;
- Windows x64: Faster‑Whisper Turbo на CPU; CUDA можна ввімкнути через `ROTHBALD_CUDA=1`.

Потрібні Python 3.11+ та `ffmpeg`/`ffprobe`. Готові GitHub-збірки включають `ffmpeg`.

## Перший запуск із коду

### Apple Silicon

1. Запусти `setup.command`.
2. Запусти `start.command`.
3. Rothbald перевірить моделі у власному стартовому екрані. Якщо їх немає або є новіша ревізія, покаже точний прогрес завантаження.

### Windows

1. Встанови ffmpeg: `winget install Gyan.FFmpeg`.
2. Запусти `setup.ps1` у PowerShell.
3. Запусти `start.bat`.

Після успішного завантаження моделей транскрипція й пошук працюють офлайн. Якщо мережі немає, але локальні файли цілі, Rothbald запускається з ними.

Rothbald відкривається як окремий desktop-застосунок у нативному вікні WKWebView на macOS та WebView2 на Windows. Зовнішній браузер, адресний рядок або окрема вкладка для готової збірки не потрібні.

## Керування проєктами

- «Новий проєкт» додає папку без копіювання відео.
- «Оновити папку» додає нові або змінені файли.
- Pause зберігає готові 30-хвилинні частини; Resume продовжує з останньої завершеної частини.
- Abort скидає незавершену чергу, але не видаляє готові транскрипти.
- Locate прив’язує перенесену або перейменовану папку без повторного розпізнавання.
- Видалення проєкту прибирає лише локальний індекс Rothbald і ніколи не чіпає вихідні медіафайли.

## Перевірка

```bash
python -m py_compile server.py transcribe_video.py prepare_semantic.py prepare_models.py model_manager.py rothbald.py
node --check static/app.js
python -m unittest discover -s tests -v
```

## GitHub Actions і збірки

Workflow `.github/workflows/build.yml` запускає перевірки та нативно будує:

- `Rothbald-Apple-Silicon.zip` на ARM64 runner macOS;
- `Rothbald-Windows-x64.zip` на Windows runner.

Обидві збірки отримують власну сучасну іконку `Ro`, створену з того самого рукописного логотипа Rothbald, що використовується в інтерфейсі.

Запуск відбувається на push у `main`, pull request, тег `v*` або вручну через Actions. Збірки поки не підписані: перед публічним релізом додай Apple Developer ID/notarization і Windows code-signing certificate.

Технічна пам’ять про архітектуру та інваріанти зберігається в [READMEAI.md](READMEAI.md).
