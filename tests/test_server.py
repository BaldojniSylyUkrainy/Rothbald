from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import app_info
import hardware_check
import model_manager
import native_update
import process_utils
import rothbald
import server
import transcribe_video
import update_manifest
from hardware_check import HardwarePreflight
from model_manager import (
    ModelManager,
    WINDOWS_VULKAN_WHISPER_REPO,
    resolve_model_snapshot,
    whisper_spec_for_device,
)
from release_notes import validate_release_notes
from scripts import generate_release_manifest
from scripts import smoke_packaged
from scripts import versioning
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from updater import UpdateManager


ROOT = Path(__file__).resolve().parents[1]


def locked_versions(path: Path) -> dict[str, str]:
    result = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, version = line.split("==", 1)
        result[name.lower().replace("_", "-")] = version.rstrip(" \\")
    return result


def updater_key_pair() -> tuple[str, str]:
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
    return (
        base64.b64encode(private_raw).decode("ascii"),
        base64.b64encode(public_raw).decode("ascii"),
    )


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.payload) - self.offset
        chunk = self.payload[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


class TranscriptionRuntimeTests(unittest.TestCase):
    def test_windows_runtime_smoke_initializes_faster_whisper_vad(self) -> None:
        faster_whisper = types.ModuleType("faster_whisper")
        faster_whisper.WhisperModel = object
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.dict(
                 sys.modules,
                 {
                     "ctranslate2": types.ModuleType("ctranslate2"),
                     "requests": types.ModuleType("requests"),
                     "faster_whisper": faster_whisper,
                 },
             ), \
             mock.patch.object(transcribe_video, "verify_faster_whisper_vad_runtime") as verify_vad:
            transcribe_video.verify_runtime_dependencies()
        verify_vad.assert_called_once_with()

    def test_windows_spec_collects_faster_whisper_vad_models(self) -> None:
        spec = (ROOT / "Rothbald.spec").read_text(encoding="utf-8")
        self.assertIn(
            'collect_data_files("faster_whisper", includes=["assets/*.onnx"])',
            spec,
        )


class ApplicationInfoTests(unittest.TestCase):
    def test_close_confirmation_lists_active_work(self) -> None:
        title, prompt, details = rothbald.close_confirmation_text({
            "active": True,
            "model_status": "downloading",
            "models": [{"name": "Whisper", "status": "downloading", "percent": 42}],
            "update_status": "downloading",
            "update_percent": 17,
            "processing": 1,
            "queued": 3,
            "indexing": 2,
            "media_checks": 0,
        })
        self.assertEqual(title, "Закрити Rothbald?")
        self.assertIn("ще тривають", prompt)
        self.assertIn("Whisper — 42%", details)
        self.assertIn("Завантаження оновлення — 17%", details)
        self.assertIn("Відео в черзі: 3", details)
        self.assertIn("Індексація для пошуку: 2", details)

    def test_close_confirmation_always_asks_when_idle(self) -> None:
        _title, prompt, details = rothbald.close_confirmation_text({"active": False})
        self.assertIn("Точно хочеш", prompt)
        self.assertIn("активних процесів немає", details)

    def test_embedded_build_metadata_is_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "VERSION").write_text("9.8.7.6\n", encoding="utf-8")
            (root / "build-info.json").write_text(
                '{"version":"1.2.3.4","commit":"abcdef123456","built_at":"2026-07-27T00:00:00Z","channel":"release"}',
                encoding="utf-8",
            )
            with mock.patch.object(app_info, "RUNTIME_ROOT", root), mock.patch.object(app_info, "SOURCE_ROOT", root):
                info = app_info.application_info()
            self.assertEqual(info["version"], "1.2.3.4")
            self.assertEqual(info["commit"], "abcdef123456")
            self.assertEqual(info["channel"], "release")

    def test_frozen_launcher_exposes_bundled_tools_on_path(self) -> None:
        with mock.patch.object(rothbald.sys, "frozen", True, create=True), \
             mock.patch.object(rothbald, "application_root", return_value=Path("/app/runtime")), \
             mock.patch.dict(rothbald.os.environ, {"PATH": "/usr/bin"}, clear=False):
            rothbald.configure_bundled_tools()
            self.assertEqual(
                rothbald.os.environ["PATH"].split(rothbald.os.pathsep)[0],
                str(Path("/app/runtime")),
            )

    def test_duplicate_instance_does_not_force_macos_window_to_front(self) -> None:
        self.assertFalse(rothbald.duplicate_instance_should_focus("darwin"))
        self.assertTrue(rothbald.duplicate_instance_should_focus("win32"))

    def test_packaged_runtime_smoke_imports_transcription_backend(self) -> None:
        completed = mock.Mock(returncode=0)
        with mock.patch.object(transcribe_video, "verify_runtime_dependencies") as verify, \
             mock.patch.object(server, "bundled_tool", side_effect=lambda name: name), \
             mock.patch.object(rothbald.subprocess, "run", return_value=completed):
            rothbald.runtime_smoke()
        verify.assert_called_once_with()


class HardwarePreflightTests(unittest.TestCase):
    def test_windows_child_process_flags_hide_console_windows(self) -> None:
        with mock.patch.object(process_utils.sys, "platform", "win32"):
            quiet = process_utils.quiet_process_options()
            grouped = process_utils.quiet_process_options(new_process_group=True)
        self.assertTrue(quiet["creationflags"] & 0x08000000)
        self.assertTrue(grouped["creationflags"] & 0x08000000)
        self.assertTrue(grouped["creationflags"] & 0x00000200)

    def test_frozen_runtime_tool_is_resolved_from_pyinstaller_internal_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            internal = Path(temporary) / "_internal"
            internal.mkdir()
            probe = internal / "rothbald-vulkan-probe.exe"
            probe.write_bytes(b"probe")
            with mock.patch.object(hardware_check.sys, "platform", "win32"), \
                 mock.patch.object(hardware_check.sys, "frozen", True, create=True), \
                 mock.patch.object(hardware_check.sys, "_MEIPASS", str(internal), create=True), \
                 mock.patch.object(hardware_check.sys, "executable", str(Path(temporary) / "Rothbald.exe")):
                self.assertEqual(
                    hardware_check.runtime_tool_path("rothbald-vulkan-probe"),
                    probe,
                )

    def test_limited_machine_requires_confirmation_and_persists_device(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(hardware_check.sys, "platform", "win32"), \
             mock.patch.object(hardware_check.platform, "machine", return_value="AMD64"), \
             mock.patch.object(hardware_check, "_physical_memory", return_value=8 * hardware_check.GIB), \
             mock.patch.object(hardware_check, "_cuda_device_count", return_value=0), \
             mock.patch.object(hardware_check, "_nvidia_gpus", return_value=[]), \
             mock.patch.object(hardware_check.shutil, "disk_usage", return_value=mock.Mock(free=30 * hardware_check.GIB)), \
             mock.patch.object(hardware_check.os, "cpu_count", return_value=8):
            checker = HardwarePreflight(Path(temporary))
            report = checker.inspect()
            self.assertEqual(report["performance"], "limited")
            self.assertTrue(report["requires_confirmation"])
            self.assertTrue(any("CPU" in warning for warning in report["warnings"]))
            self.assertEqual(report["requirements"]["system"], "Windows 10 22H2+ · x64")
            self.assertEqual(report["requirements"]["ram_minimum_bytes"], 8 * hardware_check.GIB)
            self.assertEqual(report["requirements"]["ram_recommended_bytes"], 16 * hardware_check.GIB)
            self.assertEqual(report["requirements"]["disk_minimum_bytes"], 6 * hardware_check.GIB)
            self.assertEqual(report["requirements"]["disk_recommended_bytes"], 8 * hardware_check.GIB)
            confirmed = checker.confirm("cpu")
            self.assertTrue(confirmed["accepted"])
            self.assertEqual(confirmed["selected_device"], "cpu")

    def test_insufficient_memory_blocks_model_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(hardware_check.sys, "platform", "darwin"), \
             mock.patch.object(hardware_check.platform, "machine", return_value="arm64"), \
             mock.patch.object(hardware_check, "_physical_memory", return_value=4 * hardware_check.GIB), \
             mock.patch.object(hardware_check.shutil, "disk_usage", return_value=mock.Mock(free=30 * hardware_check.GIB)), \
             mock.patch.object(hardware_check.os, "cpu_count", return_value=8):
            checker = HardwarePreflight(Path(temporary))
            report = checker.inspect()
            self.assertEqual(report["performance"], "blocked")
            with self.assertRaises(ValueError):
                checker.confirm("auto")

    def test_unknown_memory_and_storage_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(hardware_check.sys, "platform", "darwin"), \
             mock.patch.object(hardware_check.platform, "machine", return_value="arm64"), \
             mock.patch.object(hardware_check, "_physical_memory", return_value=0), \
             mock.patch.object(hardware_check.shutil, "disk_usage", side_effect=OSError("unavailable")), \
             mock.patch.object(hardware_check.os, "cpu_count", return_value=8):
            report = HardwarePreflight(Path(temporary)).inspect()
        self.assertEqual(report["performance"], "blocked")
        self.assertTrue(any("оперативної пам’яті" in item for item in report["blockers"]))
        self.assertTrue(any("вільне місце" in item for item in report["blockers"]))

    def test_macos_ventura_is_blocked_by_packaged_torch_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(hardware_check.sys, "platform", "darwin"), \
             mock.patch.object(hardware_check.platform, "machine", return_value="arm64"), \
             mock.patch.object(hardware_check.platform, "mac_ver", return_value=("13.6.9", ("", "", ""), "")), \
             mock.patch.object(hardware_check, "_physical_memory", return_value=16 * hardware_check.GIB), \
             mock.patch.object(hardware_check.shutil, "disk_usage", return_value=mock.Mock(free=30 * hardware_check.GIB)), \
             mock.patch.object(hardware_check.os, "cpu_count", return_value=8):
            report = HardwarePreflight(Path(temporary)).inspect()
        self.assertEqual(report["performance"], "blocked")
        self.assertTrue(any("macOS 14.0" in blocker for blocker in report["blockers"]))

    def test_windows_gpu_is_selectable_when_cuda_runtime_sees_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(hardware_check.sys, "platform", "win32"), \
             mock.patch.object(hardware_check.platform, "machine", return_value="AMD64"), \
             mock.patch.object(hardware_check, "_physical_memory", return_value=16 * hardware_check.GIB), \
             mock.patch.object(hardware_check, "_cuda_device_count", return_value=1), \
             mock.patch.object(hardware_check, "_nvidia_gpus", return_value=[{"index": 0, "name": "NVIDIA Test", "memory": 8 * hardware_check.GIB}]), \
             mock.patch.object(hardware_check.shutil, "disk_usage", return_value=mock.Mock(free=30 * hardware_check.GIB)), \
             mock.patch.object(hardware_check.os, "cpu_count", return_value=8):
            checker = HardwarePreflight(Path(temporary))
            report = checker.inspect()
            self.assertTrue(any(device["key"] == "cuda:0" and device["available"] for device in report["devices"]))
            self.assertTrue(checker.confirm("cuda:0")["accepted"])

    def test_windows_amd_and_intel_use_vulkan_while_nvidia_stays_on_cuda(self) -> None:
        vulkan = [
            {"index": 0, "name": "NVIDIA Test", "vendor": "nvidia", "memory": 8 * hardware_check.GIB},
            {"index": 1, "name": "AMD Radeon Test", "vendor": "amd", "memory": 16 * hardware_check.GIB},
            {"index": 2, "name": "Intel Arc Test", "vendor": "intel", "memory": 8 * hardware_check.GIB},
        ]
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(hardware_check.sys, "platform", "win32"), \
             mock.patch.object(hardware_check.platform, "machine", return_value="AMD64"), \
             mock.patch.object(hardware_check, "_physical_memory", return_value=16 * hardware_check.GIB), \
             mock.patch.object(hardware_check, "_cuda_device_count", return_value=0), \
             mock.patch.object(hardware_check, "_nvidia_gpus", return_value=[]), \
             mock.patch.object(hardware_check, "_vulkan_gpus", return_value=vulkan), \
             mock.patch.object(hardware_check.shutil, "disk_usage", return_value=mock.Mock(free=30 * hardware_check.GIB)), \
             mock.patch.object(hardware_check.os, "cpu_count", return_value=8), \
             mock.patch.dict(os.environ, {}, clear=False):
            checker = HardwarePreflight(Path(temporary))
            report = checker.inspect()
            keys = {device["key"] for device in report["devices"]}
            self.assertNotIn("vulkan:0", keys)
            self.assertIn("vulkan:1", keys)
            self.assertIn("vulkan:2", keys)
            self.assertEqual(report["resolved_device"], "vulkan:1")
            confirmed = checker.confirm("auto")
            self.assertEqual(confirmed["selected_device"], "auto")
            self.assertEqual(os.environ["ROTHBALD_DEVICE"], "vulkan:1")

    def test_existing_cpu_or_auto_choice_requires_confirmation_when_vulkan_appears(self) -> None:
        amd_vulkan = [{
            "index": 0,
            "name": "AMD Radeon Test",
            "vendor": "amd",
            "memory": 16 * hardware_check.GIB,
            "type": "discrete",
        }]
        for saved_device in ("auto", "cpu"):
            with self.subTest(saved_device=saved_device), \
                 tempfile.TemporaryDirectory() as temporary, \
                 mock.patch.object(hardware_check.sys, "platform", "win32"), \
                 mock.patch.object(hardware_check.platform, "machine", return_value="AMD64"), \
                 mock.patch.object(hardware_check, "_physical_memory", return_value=16 * hardware_check.GIB), \
                 mock.patch.object(hardware_check, "_cuda_device_count", return_value=0), \
                 mock.patch.object(hardware_check, "_nvidia_gpus", return_value=[]), \
                 mock.patch.object(hardware_check, "_vulkan_gpus", return_value=[]) as vulkan_probe, \
                 mock.patch.object(
                     hardware_check.shutil,
                     "disk_usage",
                     return_value=mock.Mock(free=30 * hardware_check.GIB),
                 ), \
                 mock.patch.object(hardware_check.os, "cpu_count", return_value=8):
                checker = HardwarePreflight(Path(temporary))
                self.assertTrue(checker.confirm(saved_device)["accepted"])

                vulkan_probe.return_value = amd_vulkan
                upgraded = checker.inspect()

                self.assertTrue(upgraded["requires_confirmation"])
                self.assertEqual(upgraded["selected_device"], saved_device)
                self.assertEqual(
                    upgraded["resolved_device"],
                    "vulkan:0" if saved_device == "auto" else "cpu",
                )

    def test_preflight_revision_reopens_matching_legacy_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(hardware_check.sys, "platform", "win32"), \
             mock.patch.object(hardware_check.platform, "machine", return_value="AMD64"), \
             mock.patch.object(hardware_check, "_physical_memory", return_value=16 * hardware_check.GIB), \
             mock.patch.object(hardware_check, "_cuda_device_count", return_value=0), \
             mock.patch.object(hardware_check, "_nvidia_gpus", return_value=[]), \
             mock.patch.object(hardware_check, "_vulkan_gpus", return_value=[]), \
             mock.patch.object(hardware_check.shutil, "disk_usage", return_value=mock.Mock(free=30 * hardware_check.GIB)), \
             mock.patch.object(hardware_check.os, "cpu_count", return_value=8):
            checker = HardwarePreflight(Path(temporary))
            report = checker.inspect()
            checker._save({
                "fingerprint": report["fingerprint"],
                "device": "auto",
                "accepted_at": time.time(),
            })
            self.assertTrue(checker.inspect()["requires_confirmation"])
            confirmed = checker.confirm("auto")
            self.assertTrue(confirmed["accepted"])
            self.assertEqual(
                json.loads(checker.settings_path.read_text(encoding="utf-8"))["preflight_revision"],
                hardware_check.PREFLIGHT_REVISION,
            )

    def test_backend_label_describes_effective_runtime(self) -> None:
        devices = [
            {"key": "cuda:0", "label": "NVIDIA RTX Test · 8 ГБ"},
            {"key": "vulkan:1", "label": "AMD Radeon Test · 16 ГБ (Vulkan)"},
        ]
        self.assertEqual(hardware_check.runtime_backend_label("cpu", devices), "CPU")
        self.assertEqual(
            hardware_check.runtime_backend_label("cuda:0", devices),
            "CUDA · NVIDIA RTX Test · 8 ГБ",
        )
        self.assertEqual(
            hardware_check.runtime_backend_label("vulkan:1", devices),
            "Vulkan · AMD Radeon Test · 16 ГБ",
        )

    def test_windows_auto_prefers_cuda_over_vulkan(self) -> None:
        self.assertEqual(
            hardware_check.resolve_windows_device(
                "auto",
                1,
                [{"index": 4, "vendor": "amd"}],
            ),
            "cuda:0",
        )

    def test_windows_auto_prefers_discrete_amd_over_intel_igpu(self) -> None:
        self.assertEqual(
            hardware_check.resolve_windows_device(
                "auto",
                0,
                [
                    {"index": 0, "vendor": "intel", "type": "integrated"},
                    {"index": 1, "vendor": "amd", "type": "discrete"},
                ],
            ),
            "vulkan:1",
        )

    def test_transcription_device_resolution_has_safe_cpu_fallback(self) -> None:
        self.assertEqual(transcribe_video.resolve_faster_whisper_device("auto", 0), ("cpu", 0, "int8"))
        self.assertEqual(transcribe_video.resolve_faster_whisper_device("auto", 2), ("cuda", 0, "float16"))
        self.assertEqual(transcribe_video.resolve_faster_whisper_device("cuda:1", 2), ("cuda", 1, "float16"))
        with self.assertRaises(RuntimeError):
            transcribe_video.resolve_faster_whisper_device("cuda:3", 1)

    def test_project_language_modes_map_to_whisper_hints(self) -> None:
        self.assertEqual(transcribe_video.whisper_language("standard"), "ru")
        self.assertIsNone(transcribe_video.whisper_language("auto"))
        with self.assertRaises(RuntimeError):
            transcribe_video.whisper_language("unknown")

    def test_whisper_cpp_json_is_normalized_to_existing_segment_contract(self) -> None:
        parsed = transcribe_video.parse_whisper_cpp_result(
            {
                "result": {"language": "ru"},
                "transcription": [
                    {"offsets": {"from": 1250, "to": 2750}, "text": " Тестовий фрагмент "},
                    {"offsets": {"from": 3000, "to": 3500}, "text": "   "},
                ],
            }
        )
        self.assertEqual(parsed["language"], "ru")
        self.assertEqual(
            parsed["segments"],
            [{"start": 1.25, "end": 2.75, "text": "Тестовий фрагмент"}],
        )

    def test_vulkan_transcription_retries_on_cpu_after_gpu_failure(self) -> None:
        calls = []

        def fake_run(command, environment):
            calls.append((list(command), dict(environment)))
            if len(calls) == 1:
                return 1, "Vulkan initialization failed"
            prefix = Path(command[command.index("--output-file") + 1])
            prefix.with_suffix(prefix.suffix + ".json").write_text(
                json.dumps(
                    {
                        "result": {"language": "ru"},
                        "transcription": [
                            {"offsets": {"from": 0, "to": 1000}, "text": "Готово"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            return 0, ""

        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(transcribe_video, "runtime_tool_path", return_value=Path("whisper-cli.exe")), \
             mock.patch.object(transcribe_video, "_local_whisper_cpp_model", return_value=Path("turbo.bin")), \
             mock.patch.object(transcribe_video, "_run_whisper_cpp", side_effect=fake_run):
            root = Path(temporary)
            result = transcribe_video.whisper_cpp_transcribe(
                root / "input.wav",
                root / "output.json",
                "vulkan:3",
                "auto",
            )
        self.assertEqual(result["text"], "Готово")
        self.assertEqual(calls[0][1]["GGML_VK_VISIBLE_DEVICES"], "3")
        self.assertEqual(calls[0][0][calls[0][0].index("--language") + 1], "auto")
        self.assertIn("--no-gpu", calls[1][0])
        self.assertNotIn("GGML_VK_VISIBLE_DEVICES", calls[1][1])


class TemporaryStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.originals = (server.DATA_DIR, server.TRANSCRIPT_DIR, server.BACKUP_DIR, server.DB_PATH)
        server.DATA_DIR = self.root / "data"
        server.TRANSCRIPT_DIR = server.DATA_DIR / "transcripts"
        server.BACKUP_DIR = server.DATA_DIR / "backups"
        server.DB_PATH = server.DATA_DIR / "test.sqlite3"
        server.DATA_DIR.mkdir(parents=True)
        with server.duration_pending_lock:
            server.duration_pending.clear()
        while not server.duration_jobs.empty():
            server.duration_jobs.get_nowait()
            server.duration_jobs.task_done()

    def tearDown(self) -> None:
        server.DATA_DIR, server.TRANSCRIPT_DIR, server.BACKUP_DIR, server.DB_PATH = self.originals
        self.temporary.cleanup()

    def add_project_and_video(self, paused: int = 0) -> tuple[str, str]:
        server.init_storage()
        project_id, video_id = "a" * 32, "b" * 32
        now = time.time()
        with server.db() as connection:
            connection.execute(
                """INSERT INTO projects(
                       id,name,path,created_at,scanned_at,last_opened_at,queue_paused,queue_generation
                   ) VALUES (?,?,?,?,?,?,?,0)""",
                (project_id, "test", str(self.root), now, now, now, paused),
            )
            connection.execute(
                """INSERT INTO videos(
                       id,project_id,original_name,source_path,relative_path,available,size,mtime,
                       status,created_at,updated_at
                   ) VALUES (?,?,?,?,?,1,?,?, 'queued',?,?)""",
                (video_id, project_id, "one.mp4", str(self.root / "one.mp4"), "one.mp4", 10, now, now, now),
            )
        return project_id, video_id


class MigrationTests(TemporaryStorageTest):
    def test_old_global_source_unique_is_removed_without_losing_segments(self) -> None:
        now = time.time()
        connection = sqlite3.connect(server.DB_PATH)
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,name TEXT NOT NULL,path TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL,scanned_at REAL NOT NULL
            );
            CREATE TABLE videos (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                original_name TEXT NOT NULL,source_path TEXT NOT NULL UNIQUE,
                size INTEGER NOT NULL,mtime REAL NOT NULL,status TEXT NOT NULL DEFAULT 'ready',
                error TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL
            );
            CREATE TABLE segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                start REAL NOT NULL,end REAL NOT NULL,text TEXT NOT NULL,normalized TEXT NOT NULL
            );
            """
        )
        connection.execute("INSERT INTO projects VALUES (?,?,?,?,?)", ("a" * 32, "A", "/missing-a", now, now))
        connection.execute(
            "INSERT INTO videos VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("1" * 32, "a" * 32, "one.mp4", "/same/file.mp4", 1, now, "done", None, now, now),
        )
        connection.execute(
            "INSERT INTO segments(video_id,start,end,text,normalized) VALUES (?,?,?,?,?)",
            ("1" * 32, 0, 1, "экономика растет", "экономика растет"),
        )
        connection.commit()
        connection.close()

        server.init_storage()
        with server.db() as migrated:
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM segments").fetchone()[0], 1)
            self.assertEqual(
                migrated.execute("SELECT language_mode FROM projects").fetchone()[0],
                "standard",
            )
            migrated.execute(
                "INSERT INTO projects(id,name,path,created_at,scanned_at,last_opened_at) VALUES (?,?,?,?,?,?)",
                ("b" * 32, "B", "/missing-b", now, now, now),
            )
            migrated.execute(
                """INSERT INTO videos(
                       id,project_id,original_name,source_path,relative_path,size,mtime,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?)""",
                ("2" * 32, "b" * 32, "one.mp4", "/same/file.mp4", "one.mp4", 1, now, now, now),
            )
            self.assertEqual(migrated.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(migrated.execute("PRAGMA foreign_key_check").fetchall(), [])


class QueueTests(TemporaryStorageTest):
    def tearDown(self) -> None:
        server.runtime_stopping.clear()
        with server.process_lock:
            server.active_processes.clear()
            server.interrupt_reasons.clear()
        while True:
            try:
                server.jobs.get_nowait()
            except server.queue.Empty:
                break
            else:
                server.jobs.task_done()
        while True:
            try:
                server.duration_jobs.get_nowait()
            except server.queue.Empty:
                break
            else:
                server.duration_jobs.task_done()
        super().tearDown()

    def test_backend_change_is_blocked_while_processing_queue_is_active(self) -> None:
        _project_id, video_id = self.add_project_and_video(paused=0)
        with mock.patch.object(server, "runtime_started", True), \
             mock.patch.object(server.model_manager, "snapshot", return_value={"status": "ready"}):
            self.assertIn("розпізнавання", server.backend_change_blocker())
            with server.db() as connection:
                connection.execute(
                    """UPDATE videos
                       SET status='done',semantic_status='ready'
                       WHERE id=?""",
                    (video_id,),
                )
            self.assertIsNone(server.backend_change_blocker())

    def test_rescan_preserves_searchable_transcript_until_replacement_succeeds(self) -> None:
        project_id, video_id = self.add_project_and_video(paused=0)
        source = self.root / "one.mp4"
        source.write_bytes(b"0123456789")
        first_fingerprint = server.file_fingerprint(source)
        transcript = server.TRANSCRIPT_DIR / f"{video_id}.json"
        server.TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        transcript.write_text('{"segments":[{"text":"старий текст"}]}', encoding="utf-8")
        with server.db() as connection:
            connection.execute(
                """UPDATE videos SET status='done',progress=1,content_fingerprint=?,
                   semantic_status='ready',semantic_progress=1,
                   transcript_revision=1,semantic_revision=1 WHERE id=?""",
                (first_fingerprint, video_id),
            )
            cursor = connection.execute(
                """INSERT INTO segments(video_id,start,end,text,normalized)
                   VALUES (?,?,?,?,?)""",
                (video_id, 0, 1, "старий текст", server.normalize("старий текст")),
            )
            connection.execute(
                "INSERT INTO segment_terms(segment_id,term) VALUES (?,?)",
                (cursor.lastrowid, server.normalize("старий")),
            )
            connection.execute(
                """INSERT INTO semantic_chunks(
                       video_id,start,end,text,embedding,model,transcript_revision
                   ) VALUES (?,?,?,?,?,?,?)""",
                (
                    video_id,
                    0,
                    1,
                    "старий текст для збереження індексу",
                    b"\0" * (server.EMBEDDING_DIMENSION * 4),
                    server.SEMANTIC_INDEX_ID,
                    1,
                ),
            )

        source.write_bytes(b"abcdefghij")
        with mock.patch.object(server, "media_duration", return_value=10):
            self.assertEqual(server.scan_project(project_id), 1)

        with server.db() as connection:
            video = connection.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
            self.assertEqual(video["status"], "queued")
            self.assertEqual(video["semantic_status"], "ready")
            self.assertEqual(video["transcript_revision"], 1)
            self.assertEqual(video["semantic_revision"], 1)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM segments WHERE video_id=?", (video_id,)
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM semantic_chunks WHERE video_id=?", (video_id,)
                ).fetchone()[0],
                1,
            )
        self.assertTrue(transcript.is_file())

    def test_rescan_ignores_mtime_only_change_when_fingerprint_matches(self) -> None:
        project_id, video_id = self.add_project_and_video(paused=0)
        source = self.root / "one.mp4"
        source.write_bytes(b"0123456789")
        fingerprint = server.file_fingerprint(source)
        with server.db() as connection:
            connection.execute(
                """UPDATE videos SET status='done',progress=1,content_fingerprint=?,
                   size=?,mtime=? WHERE id=?""",
                (fingerprint, source.stat().st_size, source.stat().st_mtime - 10, video_id),
            )

        with mock.patch.object(server, "media_duration", return_value=10):
            self.assertEqual(server.scan_project(project_id), 1)

        with server.db() as connection:
            video = connection.execute("SELECT status,progress FROM videos WHERE id=?", (video_id,)).fetchone()
        self.assertEqual(video["status"], "done")
        self.assertEqual(video["progress"], 1)

    def test_failed_scan_preserves_last_known_availability(self) -> None:
        project_id, video_id = self.add_project_and_video(paused=0)
        source = self.root / "one.mp4"
        source.write_bytes(b"0123456789")
        before = 123.0
        with server.db() as connection:
            connection.execute(
                "UPDATE projects SET scanned_at=? WHERE id=?", (before, project_id)
            )
            connection.execute(
                """UPDATE videos SET available=1,size=?,mtime=?,content_fingerprint=? WHERE id=?""",
                (source.stat().st_size, source.stat().st_mtime, server.file_fingerprint(source), video_id),
            )

        def interrupted_walk():
            yield source
            raise OSError("drive disconnected")

        with mock.patch.object(Path, "rglob", return_value=interrupted_walk()):
            with self.assertRaisesRegex(OSError, "drive disconnected"):
                server.scan_project(project_id)

        with server.db() as connection:
            video = connection.execute("SELECT available FROM videos WHERE id=?", (video_id,)).fetchone()
            project = connection.execute("SELECT scanned_at FROM projects WHERE id=?", (project_id,)).fetchone()
        self.assertEqual(video["available"], 1)
        self.assertEqual(project["scanned_at"], before)

    def test_scan_defers_duration_probe_to_background_queue(self) -> None:
        project_id, video_id = self.add_project_and_video(paused=0)
        source = self.root / "one.mp4"
        source.write_bytes(b"0123456789")
        with mock.patch.object(server, "media_duration", side_effect=AssertionError("must be background")):
            self.assertEqual(server.scan_project(project_id), 1)
        self.assertEqual(server.duration_jobs.get_nowait(), video_id)
        server.duration_jobs.task_done()

    def test_duration_probe_backoff_skips_recent_failure(self) -> None:
        _project_id, video_id = self.add_project_and_video(paused=0)
        with server.db() as connection:
            connection.execute(
                "UPDATE videos SET available=1,media_duration=0,duration_checked_at=? WHERE id=?",
                (time.time(), video_id),
            )
        self.assertEqual(server.enqueue_due_duration_probes(), 0)
        with server.db() as connection:
            connection.execute(
                "UPDATE videos SET duration_checked_at=? WHERE id=?",
                (time.time() - server.DURATION_RETRY_SECONDS - 1, video_id),
            )
        self.assertEqual(server.enqueue_due_duration_probes(), 1)
        self.assertEqual(server.duration_jobs.get_nowait(), video_id)
        server.duration_jobs.task_done()

    def test_shutdown_pauses_queue_and_terminates_managed_process(self) -> None:
        project_id, video_id = self.add_project_and_video(paused=0)
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        with server.process_lock:
            server.active_processes[video_id] = process
        with mock.patch.object(server, "terminate_process_group") as terminate:
            server.shutdown_runtime(timeout=0.1)
        terminate.assert_called_once_with(process)
        with server.db() as connection:
            project = connection.execute(
                "SELECT queue_paused,queue_generation FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            video = connection.execute("SELECT status FROM videos WHERE id=?", (video_id,)).fetchone()
        self.assertEqual((project["queue_paused"], project["queue_generation"]), (1, 1))
        self.assertEqual(video["status"], "paused")

    def test_paused_project_cannot_be_claimed(self) -> None:
        _, video_id = self.add_project_and_video(paused=1)
        self.assertIsNone(server.claim_transcription_job(video_id, 0))
        with server.db() as connection:
            self.assertEqual(connection.execute("SELECT status FROM videos WHERE id=?", (video_id,)).fetchone()[0], "queued")

    def test_runnable_job_is_claimed_atomically(self) -> None:
        _, video_id = self.add_project_and_video(paused=0)
        row = server.claim_transcription_job(video_id, 0)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "processing")
        self.assertIsNone(server.claim_transcription_job(video_id, 0))

    def test_completed_checkpoints_are_reused(self) -> None:
        _, video_id = self.add_project_and_video(paused=0)
        source = self.root / "one.mp4"
        source.write_bytes(b"0123456789")
        source_stat = source.stat()
        with server.db() as connection:
            connection.execute(
                """UPDATE videos SET media_duration=3700,size=?,mtime=?,content_fingerprint=?
                   WHERE id=?""",
                (source_stat.st_size, source_stat.st_mtime, server.file_fingerprint(source), video_id),
            )
            row = connection.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
            signature = server.transcription_signature(row)
            connection.execute(
                """INSERT INTO transcription_parts(
                       video_id,signature,part_index,start,end,segments_json,created_at
                   ) VALUES (?,?,?,?,?,?,?)""",
                (video_id, signature, 0, 0, 1800, '[{"start":10,"end":11,"text":"первая часть"}]', time.time()),
            )
        whisper_calls = []

        def fake_process(_video_id, _generation, command, on_line=None, watchdog=0):
            if command[0] == "ffmpeg":
                Path(command[-1]).write_bytes(b"audio")
            else:
                whisper_calls.append(command)
                Path(command[3]).write_text(
                    '{"segments":[{"start":5,"end":6,"text":"следующая часть"}]}',
                    encoding="utf-8",
                )
                if on_line:
                    on_line("100%|")

        with mock.patch.object(server, "run_managed_process", side_effect=fake_process), \
             mock.patch.object(server, "resolve_model_snapshot", return_value=Path("/models/exact")), \
             mock.patch.object(server, "index_semantics"):
            server.transcribe(video_id, 0)

        self.assertEqual(len(whisper_calls), 2)
        with server.db() as connection:
            video = connection.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
            starts = [row[0] for row in connection.execute(
                "SELECT start FROM segments WHERE video_id=? ORDER BY start", (video_id,)
            )]
            self.assertEqual(video["status"], "done")
            self.assertEqual(video["transcript_revision"], 1)
            self.assertEqual(starts, [10, 1803, 3603])
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM transcription_parts WHERE video_id=?", (video_id,)
            ).fetchone()[0], 0)

    def test_changed_source_is_rejected_before_transcript_commit(self) -> None:
        _, video_id = self.add_project_and_video(paused=0)
        source = self.root / "one.mp4"
        source.write_bytes(b"0123456789")
        stat = source.stat()
        with server.db() as connection:
            connection.execute(
                "UPDATE videos SET size=?,mtime=?,content_fingerprint=? WHERE id=?",
                (stat.st_size, stat.st_mtime, server.file_fingerprint(source), video_id),
            )
            row = connection.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        source.write_bytes(b"abcdefghij")
        with self.assertRaisesRegex(RuntimeError, "замінене"):
            server.assert_source_unchanged(source, row)

    def test_failed_json_export_does_not_strand_semantic_indexing(self) -> None:
        _, video_id = self.add_project_and_video(paused=0)
        with server.db() as connection:
            connection.execute("UPDATE videos SET status='processing' WHERE id=?", (video_id,))
        with mock.patch.object(Path, "write_text", side_effect=OSError("disk full")):
            revision = server.replace_video_transcript(
                video_id, [{"start": 0, "end": 1, "text": "готовий текст"}]
            )
        self.assertEqual(revision, 1)
        with server.db() as connection:
            row = connection.execute(
                "SELECT status,semantic_status FROM videos WHERE id=?", (video_id,)
            ).fetchone()
        self.assertEqual((row["status"], row["semantic_status"]), ("done", "pending"))

    def test_duration_probe_queue_deduplicates_video(self) -> None:
        self.assertTrue(server.enqueue_duration_probe("video"))
        self.assertFalse(server.enqueue_duration_probe("video"))
        self.assertEqual(server.duration_jobs.get_nowait(), "video")
        server.duration_jobs.task_done()
        with server.duration_pending_lock:
            server.duration_pending.discard("video")

    def test_delete_does_not_interrupt_project_when_backup_fails(self) -> None:
        project_id, _video_id = self.add_project_and_video(paused=0)
        handler = object()
        with mock.patch.object(server, "backup_database", side_effect=OSError("disk full")), \
             mock.patch.object(server, "interrupt_project_processes") as interrupt, \
             mock.patch.object(server, "respond") as response:
            server.Handler.delete_project(handler, project_id)
        interrupt.assert_not_called()
        self.assertEqual(response.call_args.args[2], 507)
        with server.db() as connection:
            self.assertTrue(connection.execute(
                "SELECT EXISTS(SELECT 1 FROM projects WHERE id=?)", (project_id,)
            ).fetchone()[0])


class SearchAndUtilityTests(unittest.TestCase):
    def test_local_api_rejects_dns_rebinding_hosts(self) -> None:
        self.assertTrue(server.trusted_local_host("127.0.0.1:8765", 8765))
        self.assertTrue(server.trusted_local_host("localhost:8765", 8765))
        self.assertFalse(server.trusted_local_host("attacker.example:8765", 8765))
        self.assertFalse(server.trusted_local_host("127.0.0.1:9999", 8765))
        self.assertFalse(server.trusted_local_host("user@127.0.0.1:8765", 8765))

    def test_semantic_query_instruction_is_language_neutral(self) -> None:
        self.assertIn("any language", server.SEMANTIC_QUERY_INSTRUCTION)
        self.assertNotIn("Russian-language", server.SEMANTIC_QUERY_INSTRUCTION)

    def test_progress_parser_accepts_mlx_and_whisper_cpp_formats(self) -> None:
        self.assertEqual(server.progress_from_line(" 42%|████"), 0.42)
        self.assertEqual(
            server.progress_from_line("whisper_print_progress_callback: progress =  65%"),
            0.65,
        )

    def test_http_server_owns_its_socket_before_reporting_ready(self) -> None:
        with mock.patch.object(server, "init_storage"), \
             mock.patch.object(server.hardware_preflight, "apply_saved_device", return_value=False), \
             mock.patch.object(server, "HOST", "127.0.0.1"), \
             mock.patch.object(server, "PORT", 0):
            http_server = server.create_http_server()
        try:
            self.assertGreater(http_server.server_address[1], 0)
            self.assertTrue(http_server.daemon_threads)
        finally:
            http_server.server_close()

    def test_exact_matching_does_not_match_inside_unrelated_words(self) -> None:
        self.assertFalse(server.text_token_matches("мир", "владимир"))
        self.assertFalse(server.text_token_matches("рост", "просто"))
        self.assertTrue(server.text_token_matches("бизнес", "бизнеса"))
        self.assertTrue(server.text_token_matches("бизнсе", "бизнес"))
        self.assertTrue(server.text_token_matches("экономика", "экономики"))

    def test_normalization_is_case_insensitive_and_maps_keyboard_letters(self) -> None:
        self.assertEqual(server.normalize("ЄКОНОМІКА, ЁЛКА"), "экономика елка")

    def test_long_media_ranges_overlap_but_keep_distinct_cores(self) -> None:
        ranges = server.transcription_ranges(3700)
        self.assertEqual(len(ranges), 3)
        self.assertEqual(ranges[0], (0, 1800, 0, 1802))
        self.assertEqual(ranges[1], (1800, 3600, 1798, 3602))
        self.assertEqual(ranges[2], (3600, 3700, 3598, 3700))

    def test_http_ranges_include_suffix_and_reject_invalid_values(self) -> None:
        self.assertEqual(server.parse_range_header(None, 100), (0, 99))
        self.assertEqual(server.parse_range_header("bytes=10-19", 100), (10, 19))
        self.assertEqual(server.parse_range_header("bytes=-20", 100), (80, 99))
        self.assertEqual(server.parse_range_header("bytes=90-", 100), (90, 99))
        self.assertIsNone(server.parse_range_header("bytes=100-120", 100))
        self.assertIsNone(server.parse_range_header("bytes=1-2,4-5", 100))

    def test_frozen_transcription_uses_the_packaged_executable(self) -> None:
        with mock.patch.object(server.sys, "frozen", True, create=True), \
             mock.patch.object(server, "resolve_model_snapshot", return_value=Path("/models/exact")):
            command = server.transcription_command(
                Path("/tmp/input.wav"), Path("/tmp/output.json"), "auto"
            )
        self.assertEqual(command[:2], [server.sys.executable, "--transcribe"])
        self.assertEqual(Path(command[-2]), Path("/models/exact"))
        self.assertEqual(command[-1], "auto")

    def test_language_mode_changes_transcription_checkpoint_signature(self) -> None:
        row = {"size": 10, "mtime": 123.5}
        self.assertNotEqual(
            server.transcription_signature(row, "standard"),
            server.transcription_signature(row, "auto"),
        )


class ModelBootstrapTests(unittest.TestCase):
    def test_vulkan_keeps_turbo_but_selects_the_ggml_artifact(self) -> None:
        spec = whisper_spec_for_device("vulkan:2", "win32")
        self.assertEqual(spec.repo_id, WINDOWS_VULKAN_WHISPER_REPO)
        self.assertEqual(spec.patterns, ("ggml-large-v3-turbo.bin",))

    def test_progress_snapshot_is_weighted_and_detached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = ModelManager(Path(directory))
            manager._set_model(
                "speech",
                status="downloading",
                downloaded=50,
                total=100,
                percent=50,
                eta_seconds=10,
                bytes_per_second=5,
            )
            manager._set_model("meaning", status="downloading", downloaded=0, total=300, percent=0)
            snapshot = manager.snapshot()
            self.assertEqual(snapshot["percent"], 12)
            self.assertEqual(snapshot["eta_seconds"], 10)
            self.assertEqual(snapshot["bytes_per_second"], 5)
            snapshot["models"][0]["percent"] = 99
            self.assertEqual(manager.snapshot()["models"][0]["percent"], 50)

    def test_downloaded_snapshot_is_verified_at_its_exact_revision(self) -> None:
        spec = model_manager.ModelSpec(
            "speech",
            "Розпізнавання мовлення",
            "example/speech",
            ("config.json", "model.bin"),
        )
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(model_manager, "model_specs", return_value=(spec,)):
            manager = ModelManager(Path(directory))
            checked_revisions = []
            manager._locally_ready = mock.Mock(
                side_effect=lambda _spec, revision=None: (
                    checked_revisions.append(revision) or revision == "remote-sha"
                )
            )
            manager._remote_files = mock.Mock(
                return_value=("remote-sha", [("config.json", 10), ("model.bin", 90)])
            )
            manager._download_file = mock.Mock()

            manager._run()

            self.assertEqual(manager.snapshot()["status"], "ready")
            self.assertEqual(checked_revisions, [None, "remote-sha"])
            manager._download_file.assert_not_called()
            manifest = json.loads(manager.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["models"]["speech"]["revision"], "remote-sha")

    def test_runtime_resolves_exact_manifest_revision_without_main_ref(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model-manifest.json").write_text(json.dumps({
                "models": {"speech": {"repo": "owner/model", "revision": "exact-sha"}}
            }), encoding="utf-8")
            snapshot = root / "cache" / "snapshots" / "exact-sha"
            spec = model_manager.ModelSpec("speech", "Speech", "owner/model", ("config.json",))
            with mock.patch.object(model_manager, "model_specs", return_value=(spec,)), \
                 mock.patch(
                     "huggingface_hub.snapshot_download", return_value=str(snapshot)
                 ) as download:
                self.assertEqual(
                    resolve_model_snapshot(root, "speech", "owner/model"),
                    snapshot,
                )
            download.assert_called_once_with(
                repo_id="owner/model",
                revision="exact-sha",
                allow_patterns=["config.json"],
                local_files_only=True,
            )

    def test_local_verification_requires_every_model_pattern(self) -> None:
        spec = model_manager.ModelSpec(
            "speech",
            "Розпізнавання мовлення",
            "example/speech",
            ("config.json", "model.bin"),
        )
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            (snapshot / "config.json").write_text("{}", encoding="utf-8")
            with mock.patch(
                "huggingface_hub.snapshot_download",
                return_value=str(snapshot),
            ) as download:
                self.assertFalse(ModelManager._locally_ready(spec, "exact-sha"))
                (snapshot / "model.bin").write_bytes(b"model")
                self.assertTrue(ModelManager._locally_ready(spec, "exact-sha"))

            download.assert_called_with(
                repo_id="example/speech",
                revision="exact-sha",
                allow_patterns=["config.json", "model.bin"],
                local_files_only=True,
            )


class UpdaterTests(unittest.TestCase):
    def signed_manifest(self, private_key: str, version: str, payload: bytes) -> dict:
        filename = f"Rothbald-{version}-Mac-Apple-Silicon.dmg"
        return update_manifest.sign_manifest(
            {
                "schema": 1,
                "version": version,
                "notes": f"# Rothbald {version}\n\n## Нове\n\n- Безпечне оновлення.\n",
                "pub_date": "2026-07-27T00:00:00Z",
                "platforms": {
                    "darwin-aarch64": {
                        "url": (
                            "https://github.com/BaldojniSylyUkrainy/Rothbald/"
                            f"releases/download/v{version}/{filename}"
                        ),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size": len(payload),
                    }
                },
            },
            private_key,
        )

    def test_signed_update_is_downloaded_verified_and_opened(self) -> None:
        private_key, public_key = updater_key_pair()
        installer_payload = b"signed installer payload"
        manifest = self.signed_manifest(private_key, "0.2.0.0", installer_payload)
        manifest_payload = json.dumps(manifest).encode("utf-8")

        def opener(request, timeout):
            self.assertGreater(timeout, 0)
            return FakeResponse(
                manifest_payload if request.full_url.endswith("latest.json") else installer_payload
            )

        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(update_manifest, "PUBLIC_KEY_BASE64", public_key):
            manager = UpdateManager(
                Path(temporary),
                "0.1.2.0",
                enabled=True,
                platform_name="darwin",
                urlopen=opener,
            )
            manager._check()
            self.assertEqual(manager.snapshot()["status"], "available")
            manager._download()
            self.assertEqual(manager.snapshot()["status"], "downloaded")
            opened = []
            manager.set_installer_callback(lambda path: opened.append(path) or True)
            manager.install()
            self.assertEqual(len(opened), 1)
            self.assertEqual(opened[0].read_bytes(), installer_payload)

    def test_previous_installer_is_removed_on_next_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            updates = root / "updates"
            updates.mkdir()
            stale = updates / "old-installer.dmg"
            stale.write_bytes(b"old")
            UpdateManager(root, "0.4.2.0", enabled=False)
            self.assertFalse(stale.exists())

    def test_macos_update_helper_swaps_only_rothbald_and_reopens_it(self) -> None:
        helper = native_update.macos_update_helper_text()
        shell = shutil.which("sh")
        if shell:
            completed = subprocess.run(
                [shell, "-n"],
                input=helper,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('if [ "$(basename "$TARGET_APP")" != "Rothbald.app" ]', helper)
        self.assertIn('/usr/bin/codesign --verify --deep --strict "$STAGED_APP"', helper)
        self.assertIn('/usr/sbin/spctl --assess --type execute "$STAGED_APP"', helper)
        self.assertIn('/usr/bin/open -n "$TARGET_APP"', helper)
        self.assertIn('while kill -0 "$ROTHBALD_PID"', helper)
        self.assertIn('if [ "$INSTALLED" -eq 0 ] && [ -d "$TARGET_APP" ]', helper)

    def test_macos_update_helper_arguments_are_bound_to_installed_app(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "Rothbald.app"
            executable = app / "Contents" / "MacOS" / "Rothbald"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"binary")
            dmg = root / "Rothbald-0.6.0.0-Mac-Apple-Silicon.dmg"
            dmg.write_bytes(b"verified")

            self.assertEqual(native_update.macos_app_bundle(executable), app.resolve())
            helper, arguments = native_update.macos_helper_arguments(
                dmg,
                app,
                process_id=4321,
            )

            self.assertTrue(helper.is_file())
            self.assertTrue(os.access(helper, os.X_OK))
            self.assertEqual(arguments[1:4], [str(dmg), str(app.resolve()), "4321"])
            with self.assertRaisesRegex(ValueError, "Rothbald.app"):
                native_update.macos_app_bundle(root / "Other.app" / "Contents" / "MacOS" / "Other")

    def test_windows_updater_runs_silently_and_installer_reopens_rothbald(self) -> None:
        arguments = native_update.windows_installer_arguments()
        self.assertIn("/VERYSILENT", arguments)
        self.assertIn("/CLOSEAPPLICATIONS", arguments)
        self.assertIn("/RESTARTAPPLICATIONS", arguments)
        installer = (ROOT / "installer/Rothbald.iss").read_text(encoding="utf-8")
        self.assertIn("Flags: nowait skipifnotsilent", installer)

    def test_download_failure_is_visible_and_retryable_without_rechecking_manifest(self) -> None:
        private_key, public_key = updater_key_pair()
        installer_payload = b"retryable signed installer"
        manifest = self.signed_manifest(private_key, "0.2.0.0", installer_payload)
        manifest_payload = json.dumps(manifest).encode("utf-8")
        downloads = 0

        def opener(request, timeout):
            nonlocal downloads
            if request.full_url.endswith("latest.json"):
                return FakeResponse(manifest_payload)
            downloads += 1
            if downloads == 1:
                raise OSError("temporary network failure")
            return FakeResponse(installer_payload)

        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(update_manifest, "PUBLIC_KEY_BASE64", public_key):
            manager = UpdateManager(
                Path(temporary),
                "0.1.2.0",
                enabled=True,
                platform_name="darwin",
                urlopen=opener,
            )
            manager._check()
            manager._download()
            failed = manager.snapshot()
            self.assertEqual(failed["status"], "error")
            self.assertEqual(failed["retry_action"], "download")
            self.assertIn("temporary network failure", failed["error"])

            manager._download()
            self.assertEqual(manager.snapshot()["status"], "downloaded")
            self.assertEqual(downloads, 2)

    def test_downloaded_update_is_not_downgraded_by_another_check(self) -> None:
        manager = UpdateManager(
            Path("unused"),
            "0.1.2.0",
            enabled=True,
            platform_name="darwin",
        )
        manager._set(status="downloaded", version="0.2.0.0", percent=100)
        with mock.patch("updater.threading.Thread") as thread:
            snapshot = manager.start_check()
        self.assertEqual(snapshot["status"], "downloaded")
        thread.assert_not_called()

    def test_tampered_manifest_is_rejected(self) -> None:
        private_key, public_key = updater_key_pair()
        manifest = self.signed_manifest(private_key, "0.2.0.0", b"installer")
        manifest["notes"] = "Змінений після підписання текст"
        with mock.patch.object(update_manifest, "PUBLIC_KEY_BASE64", public_key):
            with self.assertRaisesRegex(ValueError, "signature is invalid"):
                update_manifest.verify_manifest(manifest)

    def test_release_notes_reject_wrong_version_and_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "RELEASE_NOTES.md"
            path.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must not be empty"):
                validate_release_notes(path, "1.2.3.4")
            path.write_text(
                "# Rothbald 9.9.9.9\n\n## Зміни\n\n- Достатньо довгий справжній опис релізу для перевірки контракту.\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "must start"):
                validate_release_notes(path, "1.2.3.4")
            path.write_text(
                "# Rothbald 1.2.3.4\n\n## Зміни\n\n- TODO: достатньо довгий текст, який однаково має бути відхилено як заглушку.\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "placeholder"):
                validate_release_notes(path, "1.2.3.4")


class ReleaseContractTests(unittest.TestCase):
    def test_packaged_smoke_accepts_the_real_product_title(self) -> None:
        markup = (ROOT / "static/index.html").read_text(encoding="utf-8")
        self.assertTrue(smoke_packaged.is_rothbald_document(markup))
        self.assertFalse(smoke_packaged.is_rothbald_document("<title>Not Rothbald</title>"))

    def test_version_bump_updates_release_inputs_and_notes_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            version_file = root / "VERSION"
            notes = root / "RELEASE_NOTES.md"
            workflow = root / "release.yml"
            version_file.write_text("0.3.1.0\n", encoding="utf-8")
            notes.write_text("# Rothbald 0.3.1.0\n", encoding="utf-8")
            workflow.write_text('tag:\n  default: "v0.3.1.0"\n', encoding="utf-8")
            with mock.patch.object(versioning, "VERSION_FILE", version_file), \
                 mock.patch.object(versioning, "RELEASE_NOTES", notes), \
                 mock.patch.object(versioning, "RELEASE_WORKFLOW", workflow):
                self.assertEqual(versioning.bump("hotfix"), "0.3.1.1")
                self.assertEqual(versioning.check_contract(), "0.3.1.1")
                self.assertEqual(versioning.bump("fix"), "0.3.2.0")
                self.assertEqual(versioning.check_contract(), "0.3.2.0")
                self.assertEqual(versioning.bump("feature"), "0.4.0.0")
                self.assertEqual(versioning.check_contract(), "0.4.0.0")
            self.assertIn('default: "v0.4.0.0"', workflow.read_text(encoding="utf-8"))
            self.assertTrue(notes.read_text(encoding="utf-8").startswith("# Rothbald 0.4.0.0\n"))

    def test_clean_system_exit_is_not_written_as_a_crash(self) -> None:
        launcher = (ROOT / "rothbald.py").read_text(encoding="utf-8")
        self.assertIn("except Exception:", launcher)
        self.assertNotIn("except BaseException:", launcher)

    def test_hardware_gate_explains_required_resources_below_detected_values(self) -> None:
        script = (ROOT / "static/app.js").read_text(encoding="utf-8")
        stylesheet = (ROOT / "static/style.css").read_text(encoding="utf-8")
        self.assertIn("requirements.ram_minimum_bytes", script)
        self.assertIn("requirements.disk_recommended_bytes", script)
        self.assertIn("Мінімум ${hardwareSize", script)
        self.assertIn(".hardware-requirement", stylesheet)

    def test_backend_pickers_use_themed_menus_and_model_poll_skips_hardware_probe(self) -> None:
        markup = (ROOT / "static/index.html").read_text(encoding="utf-8")
        script = (ROOT / "static/app.js").read_text(encoding="utf-8")
        stylesheet = (ROOT / "static/style.css").read_text(encoding="utf-8")
        self.assertNotIn('<select id="hardwareDevice"', markup)
        self.assertNotIn('<select id="appBackendSelect"', markup)
        self.assertIn('class="choice-menu choice-menu-up hidden"', markup)
        self.assertIn("bootstrapModels(false, false)", script)
        self.assertIn(".choice-option[aria-selected=\"true\"]", stylesheet)

    def test_updater_state_module_loads_before_the_application_and_describes_errors(self) -> None:
        markup = (ROOT / "static/index.html").read_text(encoding="utf-8")
        self.assertLess(
            markup.index('<script src="/static/update_flow.js"></script>'),
            markup.index('<script src="/static/app.js"></script>'),
        )
        self.assertIn('aria-describedby="updateSummary updateError"', markup)

    def test_native_player_bridge_and_search_navigation_are_wired(self) -> None:
        markup = (ROOT / "static/index.html").read_text(encoding="utf-8")
        script = (ROOT / "static/app.js").read_text(encoding="utf-8")
        launcher = (ROOT / "rothbald.py").read_text(encoding="utf-8")
        spec = (ROOT / "Rothbald.spec").read_text(encoding="utf-8")
        self.assertLess(
            markup.index('qrc:///qtwebchannel/qwebchannel.js'),
            markup.index('<script src="/static/app.js"></script>'),
        )
        self.assertIn("state.nativePlayer.select", script)
        self.assertIn("state.nativePlayer.setPlayerGeometry", script)
        self.assertIn("player.play().catch", script)
        self.assertIn("smoothScrollTo(playerCard)", script)
        self.assertIn("smoothScrollTo($('.results-title'))", script)
        self.assertIn("class NativePlayerBridge", launcher)
        self.assertIn('"PySide6.QtMultimediaWidgets"', spec)
        self.assertIn('"PySide6.QtWebChannel"', spec)

    def test_search_tabs_are_prominent_and_scrollbars_are_visually_hidden(self) -> None:
        stylesheet = (ROOT / "static/style.css").read_text(encoding="utf-8")
        self.assertIn("*::-webkit-scrollbar { width: 0; height: 0; }", stylesheet)
        self.assertIn("scrollbar-width: none", stylesheet)
        self.assertRegex(stylesheet, r"\.result-tabs button\s*\{[^}]*min-height:\s*44px;")
        self.assertIn(".result-tabs button.active", stylesheet)

    def test_update_download_cannot_be_collapsed_and_one_click_continues_to_install(self) -> None:
        script = (ROOT / "static/app.js").read_text(encoding="utf-8")
        self.assertIn("if (!updateFlow.canDismiss(status?.status)) return;", script)
        self.assertIn("state.updateInstallRequested = true;", script)
        self.assertIn("await installDownloadedUpdate();", script)
        self.assertIn("retry_action: 'install'", script)
        self.assertIn("$('#closeUpdateModal').classList.toggle('hidden', downloading)", script)
        self.assertNotIn("Відкрити DMG", script)

    def test_project_creation_and_search_copy_are_unambiguous(self) -> None:
        markup = (ROOT / "static/index.html").read_text(encoding="utf-8")
        script = (ROOT / "static/app.js").read_text(encoding="utf-8")
        self.assertIn('id="newProjectModal"', markup)
        self.assertIn('id="startNewProject"', markup)
        self.assertIn("openNewProjectGuide", script)
        self.assertIn("Перевірити зміни в папці", markup)
        self.assertNotIn('id="searchInput" type="search" placeholder=', markup)
        self.assertNotIn("Перевірити оновлення", markup)
        self.assertNotIn("if (info.channel === 'release') $('#checkUpdates').classList.remove('hidden')", script)

    def test_backend_permission_refreshes_when_background_work_becomes_idle(self) -> None:
        script = (ROOT / "static/app.js").read_text(encoding="utf-8")
        self.assertIn("backendBusy: null", script)
        self.assertIn("wasBusy === true && !state.backendBusy", script)
        self.assertIn("renderBackendStatus(await api('/api/hardware'))", script)

    def test_project_language_picker_hides_internal_default_language_code(self) -> None:
        markup = (ROOT / "static/index.html").read_text(encoding="utf-8")
        script = (ROOT / "static/app.js").read_text(encoding="utf-8")
        self.assertIn("Мова розпізнавання", markup)
        self.assertIn("Стандартна", markup)
        self.assertIn("Автовизначення", script)
        self.assertIn("data-picker-kind=\"language\"", markup)

    def test_release_workflow_keeps_windows_unsigned_without_weakening_updater_manifest(self) -> None:
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertNotIn("WINDOWS_CERTIFICATE", workflow)
        self.assertNotIn("signtool", workflow.lower())
        self.assertIn('ROTHBALD_UPDATER_PRIVATE_KEY: ${{ secrets.ROTHBALD_UPDATER_PRIVATE_KEY }}', workflow)
        self.assertIn("python scripts/generate_release_manifest.py", workflow)

    def test_model_gate_does_not_force_viewport_scrollbars(self) -> None:
        stylesheet = (ROOT / "static/style.css").read_text(encoding="utf-8")
        self.assertIn("overflow-x: hidden; overflow-y: auto;", stylesheet)
        self.assertRegex(
            stylesheet,
            r"\.model-gate::after\s*\{\s*position:\s*fixed;",
        )

    @unittest.skipUnless(sys.platform == "darwin", "ICNS validation requires macOS iconutil")
    def test_macos_icon_contains_complete_standard_iconset(self) -> None:
        expected_sizes = {
            "icon_16x16.png": (16, 16),
            "icon_16x16@2x.png": (32, 32),
            "icon_32x32.png": (32, 32),
            "icon_32x32@2x.png": (64, 64),
            "icon_128x128.png": (128, 128),
            "icon_128x128@2x.png": (256, 256),
            "icon_256x256.png": (256, 256),
            "icon_256x256@2x.png": (512, 512),
            "icon_512x512.png": (512, 512),
            "icon_512x512@2x.png": (1024, 1024),
        }
        with tempfile.TemporaryDirectory() as temporary:
            iconset = Path(temporary) / "app-icon.iconset"
            subprocess.run(
                [
                    "/usr/bin/iconutil",
                    "--convert",
                    "iconset",
                    str(ROOT / "assets/app-icon.icns"),
                    "--output",
                    str(iconset),
                ],
                check=True,
                capture_output=True,
            )
            self.assertEqual({path.name for path in iconset.iterdir()}, set(expected_sizes))
            for filename, expected_size in expected_sizes.items():
                payload = (iconset / filename).read_bytes()
                self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(struct.unpack(">II", payload[16:24]), expected_size)

    def test_release_manifest_uses_user_facing_installer_names(self) -> None:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        private_key, public_key = updater_key_pair()
        with tempfile.TemporaryDirectory() as temporary:
            assets = Path(temporary)
            macos_name = f"Rothbald-{version}-Mac-Apple-Silicon.dmg"
            windows_name = f"Rothbald-{version}-Windows-Setup.exe"
            (assets / macos_name).write_bytes(b"macos")
            (assets / windows_name).write_bytes(b"windows")
            with mock.patch.object(generate_release_manifest, "ASSETS", assets), \
                 mock.patch.dict(
                     os.environ,
                     {
                         "REQUESTED_TAG": f"v{version}",
                         "ROTHBALD_UPDATER_PRIVATE_KEY": private_key,
                     },
                 ), \
                 mock.patch.object(update_manifest, "PUBLIC_KEY_BASE64", public_key):
                generate_release_manifest.main()

            manifest = json.loads((assets / "latest.json").read_text(encoding="utf-8"))
            with mock.patch.object(update_manifest, "PUBLIC_KEY_BASE64", public_key):
                update_manifest.verify_manifest(manifest)
            self.assertEqual(manifest["platforms"]["darwin-aarch64"]["url"],
                             f"https://github.com/BaldojniSylyUkrainy/Rothbald/releases/download/v{version}/{macos_name}")
            self.assertEqual(manifest["platforms"]["windows-x86_64"]["url"],
                             f"https://github.com/BaldojniSylyUkrainy/Rothbald/releases/download/v{version}/{windows_name}")
            checksums = (assets / "SHA256SUMS.txt").read_text(encoding="utf-8")
            self.assertIn(f"  {macos_name}\n", checksums)
            self.assertIn(f"  {windows_name}\n", checksums)

    def test_release_manifest_rejects_a_private_key_that_does_not_match_the_app(self) -> None:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        private_key, _ = updater_key_pair()
        with tempfile.TemporaryDirectory() as temporary:
            assets = Path(temporary)
            (assets / f"Rothbald-{version}-Mac-Apple-Silicon.dmg").write_bytes(b"macos")
            (assets / f"Rothbald-{version}-Windows-Setup.exe").write_bytes(b"windows")
            with mock.patch.object(generate_release_manifest, "ASSETS", assets), \
                 mock.patch.dict(
                     os.environ,
                     {
                         "REQUESTED_TAG": f"v{version}",
                         "ROTHBALD_UPDATER_PRIVATE_KEY": private_key,
                     },
                 ):
                with self.assertRaisesRegex(SystemExit, "signature is invalid"):
                    generate_release_manifest.main()

    def test_platform_locks_include_direct_and_platform_dependencies(self) -> None:
        direct = {}
        for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
            line = raw.split(";", 1)[0].strip()
            if line and "==" in line:
                name, version = line.split("==", 1)
                direct[name.lower().replace("_", "-")] = version
        macos = locked_versions(ROOT / "requirements-macos.lock")
        windows = locked_versions(ROOT / "requirements-windows.lock")
        for name, version in direct.items():
            if name == "mlx-whisper":
                self.assertEqual(macos.get(name), version)
                self.assertNotIn(name, windows)
            elif name == "faster-whisper":
                self.assertEqual(windows.get(name), version)
                self.assertNotIn(name, macos)
            else:
                self.assertEqual(macos.get(name), version)
                self.assertEqual(windows.get(name), version)
        self.assertIn("macholib", locked_versions(ROOT / "requirements-build-macos.lock"))
        self.assertIn("pefile", locked_versions(ROOT / "requirements-build-windows.lock"))

    def test_release_version_and_macos_minimum_are_synchronized(self) -> None:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(version, r"^\d+\.\d+\.\d+\.\d+$")
        self.assertEqual(versioning.format_version(versioning.next_version((0, 3, 1, 0), "hotfix")), "0.3.1.1")
        self.assertEqual(versioning.format_version(versioning.next_version((0, 3, 1, 0), "fix")), "0.3.2.0")
        self.assertEqual(versioning.format_version(versioning.next_version((0, 3, 1, 0), "feature")), "0.4.0.0")
        self.assertEqual(versioning.check_contract(), version)
        self.assertIn('\"LSMinimumSystemVersion\": \"14.0\"', (ROOT / "Rothbald.spec").read_text())
        self.assertIn("macOS 14.0+", (ROOT / "README.md").read_text(encoding="utf-8"))
        build_workflow = (ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8")
        self.assertNotIn('tags: ["v*"]', build_workflow)
        self.assertIn("if: github.event_name != 'pull_request'", build_workflow)
        self.assertIn("retention-days: 1", build_workflow)
        self.assertIn("Rothbald-*-Windows-Setup.exe", build_workflow)
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn(f'default: "v{version}"', workflow)
        self.assertIn("gh release create \"$TAG\" --verify-tag --draft", workflow)
        self.assertIn('[[ "$REQUESTED_TAG" =~ ^v[0-9]+\\.', workflow)
        self.assertIn("Rothbald-${VERSION}-Mac-Apple-Silicon.dmg", workflow)
        self.assertGreaterEqual(workflow.count("scripts/smoke_packaged.py"), 2)
        self.assertIn("scripts/smoke_packaged.py", build_workflow)
        installer = (ROOT / "installer/Rothbald.iss").read_text(encoding="utf-8")
        self.assertIn("OutputBaseFilename=Rothbald-{#MyAppVersion}-Windows-Setup", installer)


if __name__ == "__main__":
    unittest.main()
