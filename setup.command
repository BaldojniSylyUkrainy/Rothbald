#!/bin/zsh
set -e
cd "${0:A:h}"

fail() {
  echo
  echo "Не можу продовжити: $1"
  echo "Натисни будь-яку клавішу, щоб закрити вікно."
  read -k 1
  exit 1
}

[[ "$(uname -m)" == "arm64" ]] || fail "ця збірка розрахована на Mac з Apple Silicon (M1 або новіший)."
command -v python3 >/dev/null || fail "не знайдено Python 3. Встанови його через Homebrew: brew install python"
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' || fail "потрібен Python 3.11 або новіший."
command -v ffmpeg >/dev/null || fail "не знайдено ffmpeg. Встанови через Homebrew: brew install ffmpeg"
command -v ffprobe >/dev/null || fail "не знайдено ffprobe. Він встановлюється разом із ffmpeg."

free_kb=$(df -Pk "$PWD" | awk 'NR==2 {print $4}')
(( free_kb >= 8 * 1024 * 1024 )) || fail "потрібно щонайменше 8 ГБ вільного місця для моделей і робочого середовища."

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-macos.lock
echo
echo "Готово. Запусти start.command — Rothbald сам перевірить і завантажить моделі у своєму вікні."
read -k 1
