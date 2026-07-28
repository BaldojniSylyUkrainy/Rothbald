from __future__ import annotations

import hashlib
import json
import os
import ssl
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Callable

import certifi

from update_manifest import REPOSITORY, verify_manifest, version_tuple


MANIFEST_URL = f"https://github.com/{REPOSITORY}/releases/latest/download/latest.json"
PLATFORM_KEYS = {
    "darwin": "darwin-aarch64",
    "win32": "windows-x86_64",
}
MAX_MANIFEST_BYTES = 1024 * 1024


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _urlopen(request: urllib.request.Request, timeout: int):
    context = ssl.create_default_context(cafile=certifi.where())
    return urllib.request.urlopen(request, timeout=timeout, context=context)


class UpdateManager:
    def __init__(
        self,
        data_dir: Path,
        current_version: str,
        *,
        enabled: bool,
        platform_name: str | None = None,
        urlopen: Callable = _urlopen,
    ):
        self.data_dir = data_dir
        self.current_version = current_version
        self.enabled = enabled
        self.platform_name = platform_name or sys.platform
        self.urlopen = urlopen
        self._lock = threading.RLock()
        self._candidate: dict | None = None
        self._installer_callback: Callable[[Path], bool] | None = None
        self._state = {
            "enabled": enabled,
            "platform": PLATFORM_KEYS.get(self.platform_name, ""),
            "status": "idle" if enabled else "disabled",
            "current_version": current_version,
            "version": "",
            "notes": "",
            "downloaded": 0,
            "total": 0,
            "percent": 0,
            "error": None,
            "retry_action": None,
        }

    def set_installer_callback(self, callback: Callable[[Path], bool] | None) -> None:
        self._installer_callback = callback

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, **changes) -> None:
        with self._lock:
            self._state.update(changes)

    def start_check(self) -> dict:
        with self._lock:
            if not self.enabled:
                return dict(self._state)
            if self._state["status"] in {"checking", "downloading", "downloaded"}:
                return dict(self._state)
            self._state.update(status="checking", error=None, retry_action=None)
        threading.Thread(target=self._check, daemon=True, name="rothbald-update-check").start()
        return self.snapshot()

    def _check(self) -> None:
        try:
            request = urllib.request.Request(
                MANIFEST_URL,
                headers={"Accept": "application/json", "User-Agent": f"Rothbald/{self.current_version}"},
            )
            with self.urlopen(request, timeout=15) as response:
                payload = response.read(MAX_MANIFEST_BYTES + 1)
            if len(payload) > MAX_MANIFEST_BYTES:
                raise ValueError("Updater manifest is unexpectedly large")
            manifest = verify_manifest(json.loads(payload.decode("utf-8")))
            platform_key = PLATFORM_KEYS.get(self.platform_name)
            if not platform_key or platform_key not in manifest["platforms"]:
                raise ValueError("This platform is missing from the updater manifest")
            if version_tuple(manifest["version"]) <= version_tuple(self.current_version):
                self._candidate = None
                self._set(
                    status="up_to_date",
                    version=manifest["version"],
                    notes="",
                    downloaded=0,
                    total=0,
                    percent=100,
                    error=None,
                    retry_action=None,
                )
                return
            self._candidate = {
                "version": manifest["version"],
                "notes": manifest["notes"],
                **manifest["platforms"][platform_key],
            }
            self._set(
                status="available",
                version=self._candidate["version"],
                notes=self._candidate["notes"],
                downloaded=0,
                total=self._candidate["size"],
                percent=0,
                error=None,
                retry_action=None,
            )
        except Exception as exc:
            self._candidate = None
            self._set(
                status="error",
                error=f"Не вдалося перевірити оновлення: {exc}",
                retry_action="check",
            )

    def start_download(self) -> dict:
        with self._lock:
            if not self.enabled:
                raise ValueError("Автооновлення доступне лише у встановленій release-збірці")
            if self._state["status"] == "downloaded":
                return dict(self._state)
            if self._state["status"] == "downloading":
                return dict(self._state)
            if not self._candidate:
                raise ValueError("Немає перевіреного оновлення для завантаження")
            self._state.update(
                status="downloading",
                downloaded=0,
                percent=0,
                error=None,
                retry_action=None,
            )
        threading.Thread(target=self._download, daemon=True, name="rothbald-update-download").start()
        return self.snapshot()

    def _download(self) -> None:
        candidate = dict(self._candidate or {})
        try:
            updates_dir = self.data_dir / "updates"
            updates_dir.mkdir(parents=True, exist_ok=True)
            filename = candidate["url"].rsplit("/", 1)[-1]
            destination = updates_dir / filename
            temporary = destination.with_suffix(destination.suffix + ".part")
            for stale in updates_dir.iterdir():
                if stale.is_file() and stale not in {destination, temporary}:
                    stale.unlink(missing_ok=True)
            digest = hashlib.sha256()
            downloaded = 0
            request = urllib.request.Request(
                candidate["url"],
                headers={"Accept": "application/octet-stream", "User-Agent": f"Rothbald/{self.current_version}"},
            )
            try:
                with self.urlopen(request, timeout=60) as response, temporary.open("wb") as target:
                    while chunk := response.read(1024 * 1024):
                        target.write(chunk)
                        digest.update(chunk)
                        downloaded += len(chunk)
                        if downloaded > candidate["size"]:
                            raise ValueError("Downloaded installer is larger than the signed manifest")
                        self._set(
                            downloaded=downloaded,
                            percent=round(downloaded / candidate["size"] * 100),
                        )
                if downloaded != candidate["size"]:
                    raise ValueError("Downloaded installer size does not match the signed manifest")
                if digest.hexdigest() != candidate["sha256"]:
                    raise ValueError("Downloaded installer SHA-256 does not match the signed manifest")
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            self._set(
                status="downloaded",
                downloaded=downloaded,
                percent=100,
                error=None,
                retry_action=None,
            )
        except Exception as exc:
            self._set(
                status="error",
                error=f"Не вдалося завантажити оновлення: {exc}",
                retry_action="download",
            )

    def install(self) -> None:
        with self._lock:
            if self._state["status"] != "downloaded" or not self._candidate:
                raise ValueError("Перевірений installer ще не завантажено")
            filename = self._candidate["url"].rsplit("/", 1)[-1]
            installer = self.data_dir / "updates" / filename
            callback = self._installer_callback
        if not installer.is_file():
            raise ValueError("Завантажений installer більше не доступний")
        digest = file_sha256(installer)
        if installer.stat().st_size != self._candidate["size"] or digest != self._candidate["sha256"]:
            installer.unlink(missing_ok=True)
            raise ValueError("Installer не пройшов повторну перевірку цілісності")
        if callback is None or not callback(installer):
            raise ValueError("Не вдалося відкрити installer оновлення")
