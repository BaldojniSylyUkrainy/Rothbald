#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
RELEASE_NOTES = ROOT / "RELEASE_NOTES.md"
RELEASE_WORKFLOW = ROOT / ".github/workflows/release.yml"
VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$")
DEFAULT_PATTERN = re.compile(r'(?m)^(\s*default:\s*)"v\d+\.\d+\.\d+\.\d+"\s*$')


def read_version() -> tuple[int, int, int, int]:
    value = VERSION_FILE.read_text(encoding="utf-8").strip()
    match = VERSION_PATTERN.fullmatch(value)
    if not match:
        raise SystemExit("VERSION must use MAJOR.MINOR.PATCH.HOTFIX")
    return tuple(int(part) for part in match.groups())


def format_version(parts: tuple[int, int, int, int]) -> str:
    return ".".join(str(part) for part in parts)


def next_version(current: tuple[int, int, int, int], kind: str) -> tuple[int, int, int, int]:
    major, minor, patch, hotfix = current
    if kind == "hotfix":
        return major, minor, patch, hotfix + 1
    if kind == "fix":
        return major, minor, patch + 1, 0
    if kind == "feature":
        return major, minor + 1, 0, 0
    raise ValueError(f"Unknown release kind: {kind}")


def check_contract() -> str:
    version = format_version(read_version())
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    defaults = DEFAULT_PATTERN.findall(workflow)
    if len(defaults) != 1 or f'default: "v{version}"' not in workflow:
        raise SystemExit(f"release workflow default must be v{version}")
    first_line = RELEASE_NOTES.read_text(encoding="utf-8").splitlines()[0]
    if first_line != f"# Rothbald {version}":
        raise SystemExit(f"RELEASE_NOTES.md must start with # Rothbald {version}")
    return version


def bump(kind: str) -> str:
    version = format_version(next_version(read_version(), kind))
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    workflow, replacements = DEFAULT_PATTERN.subn(rf'\1"v{version}"', workflow)
    if replacements != 1:
        raise SystemExit("release workflow must contain exactly one tag default")
    VERSION_FILE.write_text(version + "\n", encoding="utf-8")
    RELEASE_WORKFLOW.write_text(workflow, encoding="utf-8")
    RELEASE_NOTES.write_text(
        f"# Rothbald {version}\n\n## Зміни\n\n"
        "- TODO: заміни цей рядок реальним описом перед commit або release.\n",
        encoding="utf-8",
    )
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description="Keep the Rothbald release version contract synchronized.")
    parser.add_argument("action", choices=("hotfix", "fix", "feature", "check"))
    args = parser.parse_args()
    if args.action == "check":
        version = check_contract()
        print(f"Version contract is synchronized: v{version}")
        return
    version = bump(args.action)
    print(f"Prepared {args.action} release v{version}; replace the release-notes placeholder before commit.")


if __name__ == "__main__":
    main()
