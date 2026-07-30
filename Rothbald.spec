# -*- mode: python ; coding: utf-8 -*-
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs


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
    ("THIRD_PARTY_NOTICES.md", "licenses"),
]
binaries = []
hiddenimports = []

if sys.platform == "darwin":
    # collect_all() probes every MLX submodule in an isolated process. Some
    # utility modules initialize Metal while merely being imported, which can
    # abort a headless CI build. Static analysis finds the runtime imports; copy
    # only the assets and native libraries here without executing MLX.
    datas += collect_data_files("mlx") + collect_data_files("mlx_whisper")
    binaries += collect_dynamic_libs("mlx")
elif sys.platform == "win32":
    # faster-whisper loads Silero VAD ONNX models at transcription time. They
    # are package data, so PyInstaller's module analysis does not collect them.
    datas += collect_data_files("faster_whisper", includes=["assets/*.onnx"])
hiddenimports += [
    "mlx._reprlib_fix",
    "mlx.extension",
    "mlx.utils",
    "mlx.nn",
    "mlx.nn.init",
    "mlx.nn.losses",
    "mlx.nn.utils",
    "transformers.models.xlm_roberta.modeling_xlm_roberta",
    "transformers.models.xlm_roberta.tokenization_xlm_roberta",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
]

for executable in ("ffmpeg", "ffprobe"):
    if sys.platform == "win32":
        path = Path("build/windows-tools") / f"{executable}.exe"
    else:
        resolved = shutil.which(executable)
        path = Path(resolved).resolve() if resolved else Path()
    if not path.is_file():
        raise SystemExit(f"Real {executable} binary is missing; prepare runtime tools before PyInstaller")
    binaries.append((str(path), "."))

if sys.platform == "darwin":
    ffmpeg_prefix = Path(shutil.which("ffmpeg")).resolve().parent.parent
    ffmpeg_licenses = [
        path for pattern in ("LICENSE*", "COPYING*")
        for path in ffmpeg_prefix.glob(pattern) if path.is_file()
    ]
    if not ffmpeg_licenses:
        raise SystemExit("FFmpeg license files are missing from the Homebrew package")
    datas += [(str(path), "licenses/ffmpeg") for path in ffmpeg_licenses]

if sys.platform == "win32":
    for executable in ("whisper-cli.exe", "rothbald-vulkan-probe.exe"):
        path = Path("build/windows-tools") / executable
        if not path.is_file():
            raise SystemExit(
                f"{path} is missing; run scripts/build_whisper_cpp_windows.ps1 before PyInstaller"
            )
        binaries.append((str(path), "."))
    whisper_license = Path("build/windows-tools/whisper.cpp-LICENSE.txt")
    if not whisper_license.is_file():
        raise SystemExit(f"{whisper_license} is missing from the prepared Vulkan backend")
    datas.append((str(whisper_license), "licenses"))
    ffmpeg_license = Path("build/windows-tools/FFmpeg-LICENSE.txt")
    if not ffmpeg_license.is_file():
        raise SystemExit(f"{ffmpeg_license} is missing from the prepared FFmpeg runtime")
    datas.append((str(ffmpeg_license), "licenses"))

a = Analysis(
    ["rothbald.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=(
        ["PyQt5", "PyQt6", "PySide2", "gi", "webview"]
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
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
    },
) if sys.platform == "darwin" else None
