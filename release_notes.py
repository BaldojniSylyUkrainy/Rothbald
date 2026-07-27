from __future__ import annotations

import re
from pathlib import Path


PLACEHOLDER_PATTERN = re.compile(
    r"\b(?:TODO|TBD|TBA|WIP|PLACEHOLDER|CHANGEME|FIXME)\b|"
    r"заглушк|описати|дописати|тут\s+буде|текст\s+релізу",
    re.IGNORECASE,
)


def validate_release_notes(path: Path, version: str) -> str:
    try:
        notes = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"Missing release notes: {path}") from exc
    if not notes:
        raise ValueError("RELEASE_NOTES.md must not be empty")
    expected_title = f"# Rothbald {version}"
    first_line = notes.splitlines()[0].strip()
    if first_line != expected_title:
        raise ValueError(f"RELEASE_NOTES.md must start with: {expected_title}")
    if PLACEHOLDER_PATTERN.search(notes):
        raise ValueError("RELEASE_NOTES.md contains a placeholder")
    if len(notes) < 100:
        raise ValueError("RELEASE_NOTES.md is too short to describe this release")
    if not re.search(r"(?m)^##\s+\S", notes):
        raise ValueError("RELEASE_NOTES.md must contain at least one section heading")
    if not re.search(r"(?m)^[-*]\s+\S", notes):
        raise ValueError("RELEASE_NOTES.md must contain at least one bullet item")
    return notes + "\n"
