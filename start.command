#!/bin/zsh
set -e
cd "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  echo "Спочатку запусти ./setup.command"
  read -k 1
  exit 1
fi
source .venv/bin/activate
# Keep the Mac and external media awake while the app is running. The display
# may still turn off or lock normally; closing the lid is controlled by macOS.
exec /usr/bin/caffeinate -i python rothbald.py
