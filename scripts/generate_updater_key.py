#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the one-time Rothbald Ed25519 updater signing key."
    )
    parser.add_argument(
        "--private-out",
        type=Path,
        default=Path(".secrets/rothbald-updater-private.key"),
    )
    args = parser.parse_args()
    destination = args.private_out.expanduser().resolve()
    if destination.exists():
        raise SystemExit(f"Refusing to replace existing updater key: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    destination.write_text(base64.b64encode(private_raw).decode("ascii") + "\n", encoding="ascii")
    os.chmod(destination, 0o600)
    print(f"Private key written to ignored file: {destination}")
    print(f"Public key (embed in the app): {base64.b64encode(public_raw).decode('ascii')}")


if __name__ == "__main__":
    main()
