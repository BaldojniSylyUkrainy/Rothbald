# -*- mode: python ; coding: utf-8 -*-
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


version = Path("VERSION").read_text(encoding="utf-8").strip()
build_info = Path("build/build-info.json")
windows_version = Path("build/windows-version.txt")
if not build_info.is_file() or not windows_version.is_file():
    raise SystemExit("Run python scripts/prepare_build.py before PyInstaller")

datas = [
    ("static", "static"),
    ("assets/app-icon.png", "assets"),
    ("VERSION", "."),
    (str(build_info), "."),
]
binaries = []
hiddenimports = []

packages = ["huggingface_hub", "transformers", "tokenizers", "torch", "numpy", "scipy", "webview"]
packages += ["faster_whisper", "ctranslate2"] if sys.platform == "win32" else ["mlx", "mlx_whisper"]
for package in packages:
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

for executable in ("ffmpeg", "ffprobe"):
    path = shutil.which(executable)
    if path:
        binaries.append((path, "."))

a = Analysis(
    ["rothbald.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=(
        ["PyQt5", "PyQt6", "PySide2", "PySide6", "gi"]
        if sys.platform in {"darwin", "win32"} else []
    ),
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Rothbald",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon="assets/app-icon.ico" if sys.platform == "win32" else "assets/app-icon.icns",
    version=str(windows_version) if sys.platform == "win32" else None,
    target_arch="arm64" if sys.platform == "darwin" else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Rothbald",
)
app = BUNDLE(
    coll,
    name="Rothbald.app",
    icon="assets/app-icon.icns",
    bundle_identifier="ua.rothbald.app",
    info_plist={
        "CFBundleDisplayName": "Rothbald",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "NSHighResolutionCapable": True,
    },
) if sys.platform == "darwin" else None
