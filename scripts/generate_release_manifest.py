#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSETS = Path(os.environ.get("RELEASE_ASSETS_DIR", ROOT / "release-assets"))
REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "BaldojniSylyUkrainy/Rothbald")


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
        "darwin-aarch64": f"Rothbald_{version}_aarch64.dmg",
        "windows-x86_64": f"Rothbald_{version}_windows-x86_64.zip",
    }
    missing = [name for name in expected.values() if not (ASSETS / name).is_file()]
    if missing:
        raise SystemExit(f"Missing release assets: {', '.join(missing)}")
    platforms = {}
    checksum_lines = []
    for platform, name in expected.items():
        digest = sha256(ASSETS / name)
        checksum_lines.append(f"{digest}  {name}")
        platforms[platform] = {
            "url": f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}",
            "sha256": digest,
        }
    manifest = {
        "version": version,
        "notes": os.environ.get("RELEASE_NOTES", f"Оновлення Rothbald {version}"),
        "pub_date": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "platforms": platforms,
    }
    (ASSETS / "latest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (ASSETS / "SHA256SUMS.txt").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
