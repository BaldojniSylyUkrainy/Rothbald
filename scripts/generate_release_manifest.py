#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from release_notes import validate_release_notes
from update_manifest import sign_manifest, verify_manifest


ASSETS = Path(os.environ.get("RELEASE_ASSETS_DIR", ROOT / "release-assets"))
REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "BaldojniSylyUkrainy/Rothbald")
NOTES_PATH = Path(os.environ.get("RELEASE_NOTES_PATH", ROOT / "RELEASE_NOTES.md"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    tag = os.environ.get("REQUESTED_TAG", f"v{version}")
    if tag != f"v{version}":
        raise SystemExit(f"REQUESTED_TAG must equal v{version}")
    expected = {
        "darwin-aarch64": f"Rothbald-{version}-Mac-Apple-Silicon.dmg",
        "windows-x86_64": f"Rothbald-{version}-Windows-Setup.exe",
    }
    missing = [name for name in expected.values() if not (ASSETS / name).is_file()]
    if missing:
        raise SystemExit(f"Missing release assets: {', '.join(missing)}")
    try:
        notes = validate_release_notes(NOTES_PATH, version)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    private_key = os.environ.get("ROTHBALD_UPDATER_PRIVATE_KEY", "").strip()
    if not private_key:
        raise SystemExit("ROTHBALD_UPDATER_PRIVATE_KEY is required")
    platforms = {}
    checksum_lines = []
    for platform, name in expected.items():
        asset = ASSETS / name
        digest = sha256(asset)
        checksum_lines.append(f"{digest}  {name}")
        platforms[platform] = {
            "url": f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}",
            "sha256": digest,
            "size": asset.stat().st_size,
        }
    try:
        manifest = sign_manifest({
            "schema": 1,
            "version": version,
            "notes": notes,
            "pub_date": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "platforms": platforms,
        }, private_key)
        verify_manifest(manifest)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    (ASSETS / "latest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (ASSETS / "SHA256SUMS.txt").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
