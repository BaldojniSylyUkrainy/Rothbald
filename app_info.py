from __future__ import annotations

import json
import os
import sys
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT))


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_version() -> str:
    for path in (RUNTIME_ROOT / "VERSION", SOURCE_ROOT / "VERSION"):
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return "development"


def application_info() -> dict[str, str]:
    """Return metadata embedded by the build, with a source-tree fallback for development."""
    metadata = _read_json(RUNTIME_ROOT / "build-info.json")
    version = str(metadata.get("version") or os.environ.get("ROTHBALD_BUILD_VERSION") or _read_version())
    commit = str(metadata.get("commit") or "")
    return {
        "name": "Rothbald",
        "version": version,
        "commit": commit,
        "built_at": str(metadata.get("built_at") or ""),
        "channel": str(metadata.get("channel") or ("release" if getattr(sys, "frozen", False) else "development")),
    }
