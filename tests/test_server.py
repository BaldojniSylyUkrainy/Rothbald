from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app_info
import server
from model_manager import ModelManager


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
            manager._set_model("speech", status="downloading", downloaded=50, total=100, percent=50)
            manager._set_model("meaning", status="downloading", downloaded=0, total=300, percent=0)
            snapshot = manager.snapshot()
            self.assertEqual(snapshot["percent"], 12)
            snapshot["models"][0]["percent"] = 99
            self.assertEqual(manager.snapshot()["models"][0]["percent"], 50)


if __name__ == "__main__":
    unittest.main()
