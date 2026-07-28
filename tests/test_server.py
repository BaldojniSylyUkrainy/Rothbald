from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app_info
import hardware_check
import model_manager
import rothbald
import server
import transcribe_video
import update_manifest
from hardware_check import HardwarePreflight
from model_manager import ModelManager
from release_notes import validate_release_notes
from scripts import generate_release_manifest
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
        result[name.lower().replace("_", "-")] = version
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


class ApplicationInfoTests(unittest.TestCase):
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


class HardwarePreflightTests(unittest.TestCase):
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

    def test_transcription_device_resolution_has_safe_cpu_fallback(self) -> None:
        self.assertEqual(transcribe_video.resolve_faster_whisper_device("auto", 0), ("cpu", 0, "int8"))
        self.assertEqual(transcribe_video.resolve_faster_whisper_device("auto", 2), ("cuda", 0, "float16"))
        self.assertEqual(transcribe_video.resolve_faster_whisper_device("cuda:1", 2), ("cuda", 1, "float16"))
        with self.assertRaises(RuntimeError):
            transcribe_video.resolve_faster_whisper_device("cuda:3", 1)


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
        with server.db() as connection:
            connection.execute("UPDATE videos SET media_duration=3700 WHERE id=?", (video_id,))
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


class SearchAndUtilityTests(unittest.TestCase):
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
        with mock.patch.object(server.sys, "frozen", True, create=True):
            command = server.transcription_command(Path("/tmp/input.wav"), Path("/tmp/output.json"))
        self.assertEqual(command[:2], [server.sys.executable, "--transcribe"])
        self.assertEqual(command[-1], server.MODEL)


class ModelBootstrapTests(unittest.TestCase):
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
             mock.patch.object(model_manager, "MODEL_SPECS", (spec,)):
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
        self.assertIn('\"LSMinimumSystemVersion\": \"14.0\"', (ROOT / "Rothbald.spec").read_text())
        self.assertIn("macOS 14.0+", (ROOT / "README.md").read_text(encoding="utf-8"))
        build_workflow = (ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8")
        self.assertNotIn('tags: ["v*"]', build_workflow)
        self.assertIn("if: github.event_name != 'pull_request'", build_workflow)
        self.assertIn("retention-days: 1", build_workflow)
        self.assertIn("Rothbald-*-Windows-Setup.exe", build_workflow)
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("gh release create \"$TAG\" --verify-tag --draft", workflow)
        self.assertIn('[[ "$REQUESTED_TAG" =~ ^v[0-9]+\\.', workflow)
        self.assertIn("Rothbald-${VERSION}-Mac-Apple-Silicon.dmg", workflow)
        installer = (ROOT / "installer/Rothbald.iss").read_text(encoding="utf-8")
        self.assertIn("OutputBaseFilename=Rothbald-{#MyAppVersion}-Windows-Setup", installer)


if __name__ == "__main__":
    unittest.main()
