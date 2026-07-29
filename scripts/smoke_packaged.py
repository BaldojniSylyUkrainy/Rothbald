#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def is_rothbald_document(markup: str) -> bool:
    return "<title>Rothbald" in markup and 'id="modelGate"' in markup


def main() -> None:
    parser = argparse.ArgumentParser(description="Start a packaged Rothbald bundle and verify its local UI/API.")
    parser.add_argument("executable", type=Path)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    executable = args.executable.resolve()
    if not executable.is_file():
        raise SystemExit(f"Packaged executable does not exist: {executable}")

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    port = free_port()
    with tempfile.TemporaryDirectory(prefix="rothbald-smoke-") as data_dir:
        env = os.environ.copy()
        env.update({
            "ROTHBALD_DATA_DIR": data_dir,
            "ROTHBALD_ENABLE_UPDATER": "0",
            "VIDEO_SEARCH_PORT": str(port),
        })
        process = subprocess.Popen(
            [str(executable)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + args.timeout
        last_error = "application did not answer"
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    raise SystemExit(
                        f"Packaged application exited with {process.returncode}\n{stdout}\n{stderr}"
                    )
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/app", timeout=2) as response:
                        info = json.load(response)
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                        markup = response.read().decode("utf-8")
                    if info.get("name") != "Rothbald" or info.get("version") != version:
                        raise RuntimeError(f"unexpected application metadata: {info}")
                    if not is_rothbald_document(markup):
                        raise RuntimeError("packaged UI did not return the Rothbald document")
                    print(f"Packaged Rothbald {version} started and served its native UI.")
                    return
                except Exception as error:
                    last_error = str(error)
                    time.sleep(0.5)
            raise SystemExit(f"Packaged application smoke test timed out: {last_error}")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
