from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


REPOSITORY = "BaldojniSylyUkrainy/Rothbald"
PUBLIC_KEY_BASE64 = "ECfOePRsST7Uf8KctfmEzESxtlfCJXlSITviAA+Od8A="
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PLATFORM_ASSET_PATTERNS = {
    "darwin-aarch64": "Rothbald-{version}-Mac-Apple-Silicon.dmg",
    "windows-x86_64": "Rothbald-{version}-Windows-Setup.exe",
}


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in manifest.items() if key != "signature"}
    return json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sign_manifest(manifest: dict[str, Any], private_key_base64: str) -> dict[str, Any]:
    try:
        private_raw = base64.b64decode(private_key_base64.strip(), validate=True)
        private_key = Ed25519PrivateKey.from_private_bytes(private_raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("ROTHBALD_UPDATER_PRIVATE_KEY is not a valid Ed25519 key") from exc
    signed = dict(manifest)
    signed["signature"] = base64.b64encode(
        private_key.sign(canonical_manifest_bytes(signed))
    ).decode("ascii")
    return signed


def verify_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("schema") != 1:
        raise ValueError("Unsupported updater manifest schema")
    version = str(manifest.get("version", ""))
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("Updater manifest has an invalid version")
    notes = manifest.get("notes")
    if not isinstance(notes, str) or not notes.strip():
        raise ValueError("Updater manifest has empty release notes")
    platforms = manifest.get("platforms")
    if not isinstance(platforms, dict) or not platforms:
        raise ValueError("Updater manifest has no platforms")
    expected_prefix = f"https://github.com/{REPOSITORY}/releases/download/v{version}/"
    for platform_name, asset in platforms.items():
        if not isinstance(platform_name, str) or not isinstance(asset, dict):
            raise ValueError("Updater manifest contains an invalid platform entry")
        if platform_name not in PLATFORM_ASSET_PATTERNS:
            raise ValueError("Updater manifest contains an unsupported platform")
        url = asset.get("url")
        digest = asset.get("sha256")
        size = asset.get("size")
        if not isinstance(url, str) or not url.startswith(expected_prefix):
            raise ValueError("Updater asset URL is outside the trusted release path")
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Updater asset URL must use GitHub HTTPS")
        expected_name = PLATFORM_ASSET_PATTERNS[platform_name].format(version=version)
        if parsed.path.rsplit("/", 1)[-1] != expected_name:
            raise ValueError("Updater asset filename does not match its platform")
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
            raise ValueError("Updater asset has an invalid SHA-256")
        if not isinstance(size, int) or size <= 0:
            raise ValueError("Updater asset has an invalid size")
    try:
        signature = base64.b64decode(str(manifest.get("signature", "")), validate=True)
        public_raw = base64.b64decode(PUBLIC_KEY_BASE64, validate=True)
        Ed25519PublicKey.from_public_bytes(public_raw).verify(
            signature,
            canonical_manifest_bytes(manifest),
        )
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise ValueError("Updater manifest signature is invalid") from exc
    return manifest


def version_tuple(version: str) -> tuple[int, int, int, int]:
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"Invalid four-part version: {version}")
    return tuple(int(part) for part in version.split("."))  # type: ignore[return-value]
