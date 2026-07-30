from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


MACOS_HELPER_NAME = "install-rothbald-update.sh"


def macos_app_bundle(executable: Path | None = None) -> Path:
    """Return the running Rothbald.app without accepting an unrelated target."""
    candidate = (executable or Path(sys.executable)).resolve()
    for parent in (candidate, *candidate.parents):
        if parent.name == "Rothbald.app" and parent.suffix == ".app":
            if (parent / "Contents" / "MacOS").is_dir():
                return parent
            break
    raise ValueError(
        "Автооновлення macOS доступне лише для Rothbald, запущеного з установленого Rothbald.app"
    )


def windows_installer_arguments() -> list[str]:
    """Keep an updater launch unattended while preserving the manual installer."""
    return [
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/CLOSEAPPLICATIONS",
        "/RESTARTAPPLICATIONS",
        "/NORESTART",
    ]


def macos_update_helper_text() -> str:
    """Create a detached, fail-closed app-bundle swapper for a verified DMG."""
    return r"""#!/bin/sh
set -eu

DMG_PATH=$1
TARGET_APP=$2
ROTHBALD_PID=$3
LOG_PATH=$4

exec >>"$LOG_PATH" 2>&1

if [ "$(basename "$TARGET_APP")" != "Rothbald.app" ]; then
  echo "Refusing unexpected application target: $TARGET_APP"
  exit 20
fi
if [ ! -f "$DMG_PATH" ] || [ ! -d "$TARGET_APP/Contents/MacOS" ]; then
  echo "Verified DMG or installed Rothbald.app is missing"
  exit 21
fi

while kill -0 "$ROTHBALD_PID" 2>/dev/null; do
  sleep 1
done

MOUNT_POINT=$(mktemp -d "${TMPDIR:-/tmp}/rothbald-update.XXXXXX")
STAGED_APP="${TARGET_APP}.rothbald-new-${ROTHBALD_PID}"
BACKUP_APP="${TARGET_APP}.rothbald-old-${ROTHBALD_PID}"
MOUNTED=0
INSTALLED=0

cleanup() {
  if [ "$MOUNTED" -eq 1 ]; then
    /usr/bin/hdiutil detach "$MOUNT_POINT" -quiet || true
  fi
  /bin/rm -rf "$MOUNT_POINT" "$STAGED_APP"
  if [ -d "$BACKUP_APP" ] && [ ! -d "$TARGET_APP" ]; then
    /bin/mv "$BACKUP_APP" "$TARGET_APP" || true
  fi
  if [ "$INSTALLED" -eq 0 ] && [ -d "$TARGET_APP" ]; then
    /usr/bin/open -n "$TARGET_APP" || true
  fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

/usr/bin/hdiutil attach "$DMG_PATH" -nobrowse -readonly -mountpoint "$MOUNT_POINT"
MOUNTED=1
SOURCE_APP="$MOUNT_POINT/Rothbald.app"
if [ ! -d "$SOURCE_APP/Contents/MacOS" ]; then
  echo "Rothbald.app is missing from the verified DMG"
  exit 22
fi

/bin/rm -rf "$STAGED_APP" "$BACKUP_APP"
/usr/bin/ditto "$SOURCE_APP" "$STAGED_APP"
/usr/bin/codesign --verify --deep --strict "$STAGED_APP"
/usr/sbin/spctl --assess --type execute "$STAGED_APP"

/bin/mv "$TARGET_APP" "$BACKUP_APP"
if ! /bin/mv "$STAGED_APP" "$TARGET_APP"; then
  /bin/mv "$BACKUP_APP" "$TARGET_APP"
  echo "Could not replace Rothbald.app"
  exit 23
fi
/bin/rm -rf "$BACKUP_APP"
/usr/bin/open -n "$TARGET_APP"
INSTALLED=1
echo "Rothbald update installed successfully"
"""


def write_macos_update_helper(updates_dir: Path) -> Path:
    updates_dir.mkdir(parents=True, exist_ok=True)
    helper = updates_dir / MACOS_HELPER_NAME
    helper.write_text(macos_update_helper_text(), encoding="utf-8")
    helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
    return helper


def macos_helper_arguments(
    installer: Path,
    target_app: Path,
    *,
    process_id: int | None = None,
) -> tuple[Path, list[str]]:
    if installer.suffix.lower() != ".dmg" or not installer.is_file():
        raise ValueError("Перевірений DMG оновлення не знайдено")
    target = macos_app_bundle(target_app / "Contents" / "MacOS" / "Rothbald")
    if not os.access(target.parent, os.W_OK):
        raise ValueError(
            "Rothbald.app неможливо замінити автоматично: немає доступу до папки застосунку"
        )
    helper = write_macos_update_helper(installer.parent)
    log_path = installer.parent / "install.log"
    arguments = [
        str(helper),
        str(installer),
        str(target),
        str(process_id or os.getpid()),
        str(log_path),
    ]
    return helper, arguments
