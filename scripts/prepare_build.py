#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


def project_version() -> str:
    declared = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    requested = os.environ.get("ROTHBALD_BUILD_VERSION", declared).strip()
    if not VERSION_PATTERN.fullmatch(declared):
        raise SystemExit("VERSION must contain four numeric components")
    if requested != declared:
        raise SystemExit(f"ROTHBALD_BUILD_VERSION must equal VERSION ({declared})")
    return declared


def git_commit() -> str:
    if value := os.environ.get("GITHUB_SHA"):
        return value
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def windows_version_file(version: str) -> str:
    components = ", ".join(version.split("."))
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({components}), prodvers=({components}), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'Baldojni Syly Ukrainy'),
    StringStruct('FileDescription', 'Rothbald'),
    StringStruct('FileVersion', '{version}'),
    StringStruct('InternalName', 'Rothbald'),
    StringStruct('OriginalFilename', 'Rothbald.exe'),
    StringStruct('ProductName', 'Rothbald'),
    StringStruct('ProductVersion', '{version}')
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)\n"""


def main() -> None:
    version = project_version()
    build_dir = ROOT / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "name": "Rothbald",
        "version": version,
        "commit": git_commit(),
        "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "channel": os.environ.get("ROTHBALD_BUILD_CHANNEL", "development"),
    }
    (build_dir / "build-info.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (build_dir / "windows-version.txt").write_text(windows_version_file(version), encoding="utf-8")
    print(f"Prepared Rothbald build {version} ({metadata['commit'][:12] or 'no commit'})")


if __name__ == "__main__":
    main()
