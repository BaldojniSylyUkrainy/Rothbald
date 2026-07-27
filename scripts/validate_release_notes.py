#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from release_notes import validate_release_notes


def main() -> None:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    validate_release_notes(ROOT / "RELEASE_NOTES.md", version)
    print(f"Release notes match Rothbald {version}")


if __name__ == "__main__":
    main()
