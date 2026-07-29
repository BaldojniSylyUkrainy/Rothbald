#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import queue
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import uuid
from collections import Counter
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app_info import application_info
from hardware_check import HardwarePreflight
from model_manager import (
    EMBEDDING_PATTERNS,
    EMBEDDING_REPO,
    get_model_manager,
    whisper_spec_for_device,
)
from process_utils import quiet_process_options
from updater import UpdateManager


SOURCE_ROOT = Path(__file__).resolve().parent
ROOT = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT))


def default_data_dir() -> Path:
    if not getattr(sys, "frozen", False):
        return SOURCE_ROOT / "data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Rothbald"
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) / "Rothbald" if local else Path.home() / "AppData" / "Local" / "Rothbald"
    return Path.home() / ".local" / "share" / "rothbald"


DATA_DIR = Path(os.environ.get("ROTHBALD_DATA_DIR", os.environ.get("VIDEO_SEARCH_DATA_DIR", default_data_dir())))
TRANSCRIPT_DIR = DATA_DIR / "transcripts"
STATIC_DIR = ROOT / "static"
DB_PATH = DATA_DIR / "video_search.sqlite3"
BACKUP_DIR = DATA_DIR / "backups"
EMBEDDING_MODEL = EMBEDDING_REPO
EMBEDDING_MODEL_FILES = list(EMBEDDING_PATTERNS)
SEMANTIC_INDEX_VERSION = "natural-topic-v2"
SEMANTIC_INDEX_ID = f"{EMBEDDING_MODEL}@{SEMANTIC_INDEX_VERSION}"
EMBEDDING_DIMENSION = 1024
SEMANTIC_SCORE_FLOOR = 0.70
HOST = "127.0.0.1"
PORT = int(os.environ.get("VIDEO_SEARCH_PORT", "8765"))
TRANSCRIPTION_PART_SECONDS = 30 * 60
TRANSCRIPTION_OVERLAP_SECONDS = 2
FFPROBE_TIMEOUT_SECONDS = 45
DURATION_RETRY_SECONDS = 24 * 60 * 60
PROCESS_WATCHDOG_SECONDS = 20 * 60
BACKUP_KEEP = 5
ALLOWED = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".mp3", ".m4a", ".wav", ".flac", ".ogg"}
jobs: queue.Queue[tuple[str, str, int]] = queue.Queue()
duration_jobs: queue.Queue[str] = queue.Queue()
embedding_lock = threading.RLock()
process_lock = threading.RLock()
active_processes: dict[str, subprocess.Popen] = {}
interrupt_reasons: dict[str, str] = {}
_embedder = None
model_manager = get_model_manager(DATA_DIR)
hardware_preflight = HardwarePreflight(DATA_DIR)
_app_info = application_info()
update_manager = UpdateManager(
    DATA_DIR,
    _app_info["version"],
    enabled=(
        getattr(sys, "frozen", False)
        and _app_info["channel"] == "release"
    ) or os.environ.get("ROTHBALD_ENABLE_UPDATER") == "1",
)
folder_picker_callback = None
runtime_lock = threading.Lock()
runtime_started = False
runtime_stopping = threading.Event()
worker_thread: threading.Thread | None = None
duration_worker_thread: threading.Thread | None = None


class JobInterrupted(RuntimeError):
    pass


@contextmanager
def db():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def backup_database(reason: str = "auto", force: bool = False) -> Path | None:
    """Create a small rolling SQLite backup. Media files are never copied or touched."""
    if not DB_PATH.is_file() or DB_PATH.stat().st_size == 0:
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(BACKUP_DIR.glob("video_search-*.sqlite3"), key=lambda path: path.stat().st_mtime)
    if not force and existing and time.time() - existing[-1].stat().st_mtime < 24 * 60 * 60:
        return existing[-1]
    safe_reason = re.sub(r"[^a-z0-9_-]+", "-", reason.lower()).strip("-") or "auto"
    destination = BACKUP_DIR / f"video_search-{time.strftime('%Y%m%d-%H%M%S')}-{safe_reason}.sqlite3"
    source_connection = sqlite3.connect(DB_PATH, timeout=30)
    backup_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(backup_connection)
    finally:
        backup_connection.close()
        source_connection.close()
    existing = sorted(BACKUP_DIR.glob("video_search-*.sqlite3"), key=lambda path: path.stat().st_mtime)
    for stale in existing[:-BACKUP_KEEP]:
        stale.unlink(missing_ok=True)
    return destination


def _videos_table_has_global_source_unique(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='videos'"
    ).fetchone()
    if row and row[0] and re.search(r"source_path\s+TEXT\s+NOT\s+NULL\s+UNIQUE", row[0], re.I):
        return True
    for index in connection.execute("PRAGMA index_list(videos)").fetchall():
        if not index[2]:
            continue
        columns = [item[2] for item in connection.execute(f"PRAGMA index_info('{index[1]}')")]
        if columns == ["source_path"]:
            return True
    return False


def _rebuild_videos_table(connection: sqlite3.Connection) -> None:
    """Remove the historical global path UNIQUE without losing projects or transcripts."""
    if not _videos_table_has_global_source_unique(connection):
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE videos_new (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                original_name TEXT NOT NULL,
                source_path TEXT NOT NULL,
                relative_path TEXT,
                available INTEGER NOT NULL DEFAULT 1,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                content_fingerprint TEXT,
                status TEXT NOT NULL DEFAULT 'ready',
                error TEXT,
                progress REAL NOT NULL DEFAULT 0,
                media_duration REAL NOT NULL DEFAULT 0,
                duration_checked_at REAL NOT NULL DEFAULT 0,
                started_at REAL,
                semantic_status TEXT NOT NULL DEFAULT 'pending',
                semantic_error TEXT,
                semantic_progress REAL NOT NULL DEFAULT 0,
                transcript_revision INTEGER NOT NULL DEFAULT 0,
                semantic_revision INTEGER NOT NULL DEFAULT -1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            INSERT INTO videos_new(
                id,project_id,original_name,source_path,relative_path,available,size,mtime,
                content_fingerprint,status,error,progress,media_duration,started_at,
                duration_checked_at,
                semantic_status,semantic_error,semantic_progress,transcript_revision,
                semantic_revision,created_at,updated_at
            )
            SELECT id,project_id,original_name,source_path,relative_path,available,size,mtime,
                   content_fingerprint,status,error,progress,media_duration,started_at,
                   0,
                   semantic_status,semantic_error,semantic_progress,transcript_revision,
                   semantic_revision,created_at,updated_at
            FROM videos;
            DROP TABLE videos;
            ALTER TABLE videos_new RENAME TO videos;
            COMMIT;
            """
        )
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


def init_storage() -> None:
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    backup_database("startup")
    with db() as connection:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL, scanned_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS videos (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                original_name TEXT NOT NULL, source_path TEXT NOT NULL,
                size INTEGER NOT NULL, mtime REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'ready', error TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                start REAL NOT NULL, end REAL NOT NULL,
                text TEXT NOT NULL, normalized TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS semantic_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                start REAL NOT NULL, end REAL NOT NULL,
                text TEXT NOT NULL, embedding BLOB NOT NULL,
                model TEXT NOT NULL,
                transcript_revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS transcription_parts (
                video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                signature TEXT NOT NULL,
                part_index INTEGER NOT NULL,
                start REAL NOT NULL,
                end REAL NOT NULL,
                segments_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY(video_id, part_index)
            );
            CREATE TABLE IF NOT EXISTS segment_terms (
                segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
                term TEXT NOT NULL,
                PRIMARY KEY(segment_id, term)
            );
            CREATE INDEX IF NOT EXISTS idx_segments_video ON segments(video_id, start);
            CREATE INDEX IF NOT EXISTS idx_semantic_video ON semantic_chunks(video_id, start);
            CREATE INDEX IF NOT EXISTS idx_segment_terms_term ON segment_terms(term, segment_id);
            CREATE INDEX IF NOT EXISTS idx_transcription_parts_video ON transcription_parts(video_id, signature);
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(videos)")}
        migrations = {
            "progress": "ALTER TABLE videos ADD COLUMN progress REAL NOT NULL DEFAULT 0",
            "media_duration": "ALTER TABLE videos ADD COLUMN media_duration REAL NOT NULL DEFAULT 0",
            "duration_checked_at": "ALTER TABLE videos ADD COLUMN duration_checked_at REAL NOT NULL DEFAULT 0",
            "started_at": "ALTER TABLE videos ADD COLUMN started_at REAL",
            "semantic_status": "ALTER TABLE videos ADD COLUMN semantic_status TEXT NOT NULL DEFAULT 'pending'",
            "semantic_error": "ALTER TABLE videos ADD COLUMN semantic_error TEXT",
            "relative_path": "ALTER TABLE videos ADD COLUMN relative_path TEXT",
            "available": "ALTER TABLE videos ADD COLUMN available INTEGER NOT NULL DEFAULT 1",
            "content_fingerprint": "ALTER TABLE videos ADD COLUMN content_fingerprint TEXT",
            "semantic_progress": "ALTER TABLE videos ADD COLUMN semantic_progress REAL NOT NULL DEFAULT 0",
            "transcript_revision": "ALTER TABLE videos ADD COLUMN transcript_revision INTEGER NOT NULL DEFAULT 0",
            "semantic_revision": "ALTER TABLE videos ADD COLUMN semantic_revision INTEGER NOT NULL DEFAULT -1",
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)
        project_columns = {row["name"] for row in connection.execute("PRAGMA table_info(projects)")}
        if "last_opened_at" not in project_columns:
            connection.execute("ALTER TABLE projects ADD COLUMN last_opened_at REAL NOT NULL DEFAULT 0")
        if "queue_paused" not in project_columns:
            connection.execute("ALTER TABLE projects ADD COLUMN queue_paused INTEGER NOT NULL DEFAULT 0")
        if "queue_generation" not in project_columns:
            connection.execute("ALTER TABLE projects ADD COLUMN queue_generation INTEGER NOT NULL DEFAULT 0")
        chunk_columns = {row["name"] for row in connection.execute("PRAGMA table_info(semantic_chunks)")}
        if "transcript_revision" not in chunk_columns:
            connection.execute(
                "ALTER TABLE semantic_chunks ADD COLUMN transcript_revision INTEGER NOT NULL DEFAULT 0"
            )
        _rebuild_videos_table(connection)
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_project_relative ON videos(project_id,relative_path)"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_videos_project_status ON videos(project_id,status)")
        connection.execute(
            "UPDATE projects SET last_opened_at=MAX(created_at, scanned_at) WHERE last_opened_at=0"
        )
        projects = connection.execute("SELECT id,path FROM projects").fetchall()
        for project in projects:
            root = Path(project["path"])
            videos = connection.execute(
                "SELECT id,source_path,original_name,relative_path FROM videos WHERE project_id=?",
                (project["id"],),
            ).fetchall()
            for video in videos:
                relative = video["relative_path"]
                if not relative:
                    try:
                        relative = Path(video["source_path"]).relative_to(root).as_posix()
                    except ValueError:
                        relative = video["original_name"]
                connection.execute(
                    "UPDATE videos SET relative_path=?, available=? WHERE id=?",
                    (relative, int(Path(video["source_path"]).is_file()), video["id"]),
                )
        connection.execute(
            """UPDATE projects SET queue_paused=1,queue_generation=queue_generation+1
               WHERE id IN (SELECT DISTINCT project_id FROM videos
                            WHERE status IN ('processing','queued'))"""
        )
        connection.execute(
            """UPDATE videos SET status='paused', started_at=NULL,
               error='Черга очікує ручного продовження після перезапуску'
               WHERE status IN ('processing','queued','paused') AND project_id IN
               (SELECT id FROM projects WHERE queue_paused=1)"""
        )
        connection.execute(
            "UPDATE videos SET semantic_status='pending',semantic_progress=0 WHERE semantic_status='indexing'"
        )
        connection.execute(
            """UPDATE videos SET semantic_revision=transcript_revision,semantic_progress=1
               WHERE semantic_status='ready' AND semantic_revision<0
                 AND EXISTS (
                    SELECT 1 FROM semantic_chunks c WHERE c.video_id=videos.id
                      AND c.model=? AND c.transcript_revision=videos.transcript_revision
                 )""",
            (SEMANTIC_INDEX_ID,),
        )
        connection.execute(
            """UPDATE videos SET semantic_status='pending',semantic_error=NULL,semantic_progress=0
               WHERE EXISTS (SELECT 1 FROM segments s WHERE s.video_id=videos.id)
               AND (semantic_revision!=transcript_revision OR NOT EXISTS (
                    SELECT 1 FROM semantic_chunks c
                    WHERE c.video_id=videos.id AND c.model=?
                      AND c.transcript_revision=videos.transcript_revision
               ))""",
            (SEMANTIC_INDEX_ID,),
        )
        connection.execute("UPDATE videos SET progress=1 WHERE status='done'")
        missing_terms = connection.execute(
            """SELECT s.id,s.normalized FROM segments s
               WHERE NOT EXISTS (SELECT 1 FROM segment_terms t WHERE t.segment_id=s.id)"""
        ).fetchall()
        connection.executemany(
            "INSERT OR IGNORE INTO segment_terms(segment_id,term) VALUES (?,?)",
            [(row["id"], term) for row in missing_terms for term in set(row["normalized"].split())],
        )


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower().translate(
        str.maketrans({"ё": "е", "є": "э", "і": "и", "ї": "и", "ґ": "г"})
    )
    text = re.sub(r"[^0-9a-zа-я]+", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def edit_distance_at_most(left: str, right: str, limit: int = 1) -> bool:
    """Bounded Damerau-Levenshtein for a single likely typing error."""
    if left == right:
        return True
    if abs(len(left) - len(right)) > limit:
        return False
    previous_previous: list[int] | None = None
    previous = list(range(len(right) + 1))
    for row_index, left_char in enumerate(left, 1):
        current = [row_index]
        for column_index, right_char in enumerate(right, 1):
            value = min(
                current[-1] + 1,
                previous[column_index] + 1,
                previous[column_index - 1] + int(left_char != right_char),
            )
            if (
                previous_previous is not None
                and row_index > 1 and column_index > 1
                and left_char == right[column_index - 2]
                and left[row_index - 2] == right_char
            ):
                value = min(value, previous_previous[column_index - 2] + 1)
            current.append(value)
        if min(current) > limit:
            return False
        previous_previous, previous = previous, current
    return previous[-1] <= limit


def text_token_matches(query: str, candidate: str) -> bool:
    endings = (
        "иями", "ями", "ами", "овать", "ировать", "ого", "ему", "ому", "ыми", "ими",
        "ах", "ях", "ов", "ев", "ей", "ам", "ям", "ом", "ем", "ой", "ая", "ое", "ые",
        "ый", "ий", "ую", "юю", "ы", "и", "а", "я", "у", "ю", "е",
    )

    def forms(word: str) -> set[str]:
        variants = {word}
        for ending in endings:
            if word.endswith(ending) and len(word) - len(ending) >= 4:
                variants.add(word[:-len(ending)])
                break
        return variants

    for query_form in forms(query):
        for candidate_form in forms(candidate):
            if query_form == candidate_form:
                return True
            if len(query_form) >= 5 and edit_distance_at_most(query_form, candidate_form):
                return True
    return False


def text_matches_query(normalized_text: str, query_words: list[str]) -> bool:
    candidates = normalized_text.split()
    return all(any(text_token_matches(query, candidate) for candidate in candidates) for query in query_words)


def respond(handler: BaseHTTPRequestHandler, payload: object, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def get_video(video_id: str):
    with db() as connection:
        return connection.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()


def project_is_paused(project_id: str) -> bool:
    with db() as connection:
        row = connection.execute("SELECT queue_paused FROM projects WHERE id=?", (project_id,)).fetchone()
    return bool(row and row["queue_paused"])


def project_queue_generation(project_id: str) -> int:
    with db() as connection:
        row = connection.execute("SELECT queue_generation FROM projects WHERE id=?", (project_id,)).fetchone()
    return int(row["queue_generation"]) if row else -1


def claim_transcription_job(video_id: str, generation: int) -> sqlite3.Row | None:
    """Atomically claim a queued job only while its project generation is runnable."""
    now = time.time()
    with db() as connection:
        cursor = connection.execute(
            """UPDATE videos SET status='processing',started_at=?,error=NULL,updated_at=?
               WHERE id=? AND status='queued' AND available=1
                 AND EXISTS (
                    SELECT 1 FROM projects p WHERE p.id=videos.project_id
                      AND p.queue_paused=0 AND p.queue_generation=?
                 )""",
            (now, now, video_id, generation),
        )
        if cursor.rowcount != 1:
            return None
        return connection.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()


def job_still_current(video_id: str, generation: int) -> bool:
    with db() as connection:
        return bool(
            connection.execute(
                """SELECT 1 FROM videos v JOIN projects p ON p.id=v.project_id
                   WHERE v.id=? AND v.status='processing' AND v.available=1
                     AND p.queue_paused=0 AND p.queue_generation=?""",
                (video_id, generation),
            ).fetchone()
        )


def terminate_process_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                **quiet_process_options(),
            )
        except OSError:
            process.terminate()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            process.terminate()

    def force_kill() -> None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                        **quiet_process_options(),
                    )
                except OSError:
                    process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    process.kill()

    threading.Thread(target=force_kill, daemon=True).start()


def interrupt_project_processes(video_ids: set[str], reason: str) -> None:
    with process_lock:
        for video_id in video_ids:
            interrupt_reasons[video_id] = reason
            process = active_processes.get(video_id)
            if process and process.poll() is None:
                terminate_process_group(process)


def control_project_queue(project_id: str, action: str) -> dict:
    if action not in {"pause", "resume", "abort"}:
        raise ValueError("Невідома дія з чергою")
    rows = []
    with db() as connection:
        project = connection.execute("SELECT id,queue_generation FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise ValueError("Проєкт не знайдено")
        processing_ids = {
            row["id"] for row in connection.execute(
                "SELECT id FROM videos WHERE project_id=? AND status='processing'", (project_id,)
            ).fetchall()
        }
        if action == "pause":
            generation = int(project["queue_generation"]) + 1
            connection.execute(
                "UPDATE projects SET queue_paused=1,queue_generation=? WHERE id=?",
                (generation, project_id),
            )
            connection.execute(
                """UPDATE videos SET status='paused',started_at=NULL,
                   error='Призупинено користувачем',updated_at=?
                   WHERE project_id=? AND status IN ('queued','processing')""",
                (time.time(), project_id),
            )
            queued = 0
        elif action == "resume":
            generation = int(project["queue_generation"])
            connection.execute("UPDATE projects SET queue_paused=0 WHERE id=?", (project_id,))
            rows = connection.execute(
                "SELECT id FROM videos WHERE project_id=? AND status='paused' AND available=1 ORDER BY created_at,original_name",
                (project_id,),
            ).fetchall()
            connection.execute(
                """UPDATE videos SET status='queued',started_at=NULL,error=NULL,updated_at=?
                   WHERE project_id=? AND status='paused' AND available=1""",
                (time.time(), project_id),
            )
            connection.execute(
                "UPDATE videos SET status='cancelled',error='Файл недоступний' WHERE project_id=? AND status='paused' AND available=0",
                (project_id,),
            )
            queued = len(rows)
        else:
            generation = int(project["queue_generation"]) + 1
            connection.execute(
                "UPDATE projects SET queue_paused=0,queue_generation=? WHERE id=?",
                (generation, project_id),
            )
            connection.execute(
                """UPDATE videos SET status='cancelled',progress=0,started_at=NULL,
                   error='Зупинено користувачем',updated_at=?
                   WHERE project_id=? AND status IN ('queued','processing','paused')""",
                (time.time(), project_id),
            )
            connection.execute(
                "DELETE FROM transcription_parts WHERE video_id IN (SELECT id FROM videos WHERE project_id=?)",
                (project_id,),
            )
            queued = 0
    if action in {"pause", "abort"}:
        interrupt_project_processes(processing_ids, "paused" if action == "pause" else "cancelled")
    else:
        for row in rows:
            jobs.put(("transcribe", row["id"], generation))
    return {"status": action, "queued": queued, "generation": generation}


def media_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            [bundled_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=False, timeout=FFPROBE_TIMEOUT_SECONDS,
            **quiet_process_options(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return 0.0
    try:
        return max(0.0, float(result.stdout.strip()))
    except ValueError:
        return 0.0


def probe_media_duration(video_id: str) -> None:
    """Probe one file outside SQLite locks and commit only if the row is unchanged."""
    with db() as connection:
        row = connection.execute(
            "SELECT source_path,size,mtime,available,media_duration FROM videos WHERE id=?",
            (video_id,),
        ).fetchone()
    if not row or not row["available"] or row["media_duration"] > 0:
        return
    source = Path(row["source_path"])
    duration = media_duration(source) if source.is_file() else 0.0
    checked_at = time.time()
    with db() as connection:
        connection.execute(
            """UPDATE videos SET media_duration=?,duration_checked_at=?
               WHERE id=? AND source_path=? AND size=? AND mtime=? AND media_duration<=0""",
            (duration, checked_at, video_id, row["source_path"], row["size"], row["mtime"]),
        )


def enqueue_due_duration_probes() -> int:
    """Schedule retryable ffprobe work without delaying the application window."""
    cutoff = time.time() - DURATION_RETRY_SECONDS
    with db() as connection:
        rows = connection.execute(
            """SELECT v.id FROM videos v
               WHERE v.available=1 AND v.media_duration<=0 AND v.duration_checked_at<?
               ORDER BY v.created_at,v.original_name""",
            (cutoff,),
        ).fetchall()
    for row in rows:
        duration_jobs.put(row["id"])
    return len(rows)


def file_fingerprint(path: Path) -> str:
    """Fast identity check: size plus hashes from both ends of a potentially huge video."""
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(str(stat.st_size).encode())
    with path.open("rb") as handle:
        digest.update(handle.read(1024 * 1024))
        if stat.st_size > 1024 * 1024:
            handle.seek(max(0, stat.st_size - 1024 * 1024))
            digest.update(handle.read(1024 * 1024))
    return f"{stat.st_size}:{digest.hexdigest()}"


def choose_folder_dialog(prompt: str) -> Path | None:
    if folder_picker_callback is not None:
        return folder_picker_callback(prompt)
    if sys.platform == "darwin":
        safe_prompt = prompt.replace("\\", "\\\\").replace('"', '\\"')
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", f'POSIX path of (choose folder with prompt "{safe_prompt}")'],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            return None
        return Path(result.stdout.strip().rstrip("/")).resolve()
    if sys.platform == "win32":
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            selection = filedialog.askdirectory(title=prompt, mustexist=True)
        finally:
            root.destroy()
        return Path(selection).resolve() if selection else None
    raise RuntimeError("Системний вибір папки підтримується лише на macOS та Windows")


def set_folder_picker(callback) -> None:
    global folder_picker_callback
    folder_picker_callback = callback


def bundled_tool(name: str) -> str:
    suffix = ".exe" if sys.platform == "win32" else ""
    bundled = ROOT / f"{name}{suffix}"
    return str(bundled) if bundled.is_file() else name


def verify_project_files(project_id: str) -> dict:
    with db() as connection:
        project = connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise ValueError("Проєкт не знайдено")
        folder = Path(project["path"])
        if not folder.is_dir():
            connection.execute("UPDATE videos SET available=0 WHERE project_id=?", (project_id,))
            return {"folder_available": False, "found": 0, "missing": connection.execute(
                "SELECT COUNT(*) FROM videos WHERE project_id=?", (project_id,)
            ).fetchone()[0]}
        rows = connection.execute(
            "SELECT id,relative_path,original_name,size,content_fingerprint FROM videos WHERE project_id=?", (project_id,)
        ).fetchall()
        found = 0
        for row in rows:
            relative = row["relative_path"] or row["original_name"]
            source = (folder / relative).resolve()
            available = source.is_file() and source.stat().st_size == row["size"]
            found += int(available)
            connection.execute(
                "UPDATE videos SET source_path=?, available=? WHERE id=?",
                (str(source), int(available), row["id"]),
            )
        return {"folder_available": True, "found": found, "missing": len(rows) - found}


def relocate_project(project_id: str, folder: Path) -> dict:
    folder = folder.resolve()
    if not folder.is_dir():
        raise ValueError("Обрана папка недоступна")
    with db() as connection:
        project = connection.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise ValueError("Проєкт не знайдено")
        collision = connection.execute(
            "SELECT id FROM projects WHERE path=? AND id!=?", (str(folder), project_id)
        ).fetchone()
        if collision:
            raise ValueError("Ця папка вже належить іншому проєкту")
        samples = connection.execute(
            """SELECT relative_path,original_name,size,content_fingerprint FROM videos
               WHERE project_id=? ORDER BY created_at LIMIT 3""",
            (project_id,),
        ).fetchall()
        matched = 0
        for sample in samples:
            candidate = folder / (sample["relative_path"] or sample["original_name"])
            if not candidate.is_file() or candidate.stat().st_size != sample["size"]:
                continue
            if sample["content_fingerprint"] and file_fingerprint(candidate) != sample["content_fingerprint"]:
                continue
            matched += 1
        if samples and matched == 0:
            raise ValueError("У цій папці не знайдено файлів, що належать цьому проєкту")
        connection.execute(
            "UPDATE projects SET name=?,path=?,last_opened_at=? WHERE id=?",
            (folder.name, str(folder), time.time(), project_id),
        )
    return verify_project_files(project_id)


def progress_from_line(line: str) -> float | None:
    match = re.search(r"(?:^|\s)(\d{1,3})%\|", line)
    if not match:
        match = re.search(r"\bprogress\s*=\s*(\d{1,3})%", line, re.IGNORECASE)
    if not match:
        return None
    return min(1.0, max(0.0, int(match.group(1)) / 100))


class LocalEmbedder:
    def __init__(self) -> None:
        import torch
        from transformers import AutoModel, AutoTokenizer

        self.torch = torch
        # Keep Metal exclusively for Whisper. Running Torch MPS and MLX together
        # on an M1 with unified memory can stall long transcriptions.
        self.device = "cpu"
        self.tokenizer = AutoTokenizer.from_pretrained(EMBEDDING_MODEL, local_files_only=True)
        self.model = AutoModel.from_pretrained(EMBEDDING_MODEL, local_files_only=True).to(self.device)
        self.model.eval()

    def encode(self, texts: list[str], kind: str) -> "object":
        import numpy as np

        if kind == "query":
            task = (
                "Given a Russian-language search query, retrieve transcript passages "
                "that express the same claim, idea, or topic"
            )
            prepared = [f"Instruct: {task}\nQuery: {text}" for text in texts]
        else:
            # E5 Large Instruct expects retrieval documents without a prefix.
            prepared = texts
        batches = []
        # A small CPU batch keeps the 0.6B model within the M1's unified-memory budget.
        for offset in range(0, len(prepared), 4):
            batch = prepared[offset:offset + 4]
            tokens = self.tokenizer(batch, max_length=512, padding=True, truncation=True, return_tensors="pt")
            tokens = {key: value.to(self.device) for key, value in tokens.items()}
            with self.torch.inference_mode():
                output = self.model(**tokens).last_hidden_state
                mask = tokens["attention_mask"].unsqueeze(-1).expand(output.size()).float()
                pooled = (output * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
                pooled = self.torch.nn.functional.normalize(pooled, p=2, dim=1)
            batches.append(pooled.cpu().numpy().astype(np.float32))
        return np.concatenate(batches, axis=0) if batches else np.empty((0, EMBEDDING_DIMENSION), dtype=np.float32)


def embedder() -> LocalEmbedder:
    global _embedder
    with embedding_lock:
        if _embedder is None:
            _embedder = LocalEmbedder()
        return _embedder


TOPIC_STOPWORDS = {
    "этот", "эта", "это", "эти", "того", "такой", "такая", "такие", "который", "которая",
    "которые", "потому", "поэтому", "только", "очень", "можно", "нужно", "будет", "были",
    "есть", "если", "когда", "тогда", "чтобы", "сейчас", "здесь", "тоже", "уже", "вообще",
    "просто", "себя", "свои", "свой", "наши", "нашей", "ваши", "через", "между", "после",
    "перед", "против", "также", "либо", "сказал", "говорит", "говорить", "сегодня",
}


def _chunk_payload(parts: list[dict]) -> dict:
    return {
        "start": float(parts[0]["start"]),
        "end": float(parts[-1]["end"]),
        "text": " ".join(str(part["text"]).strip() for part in parts),
    }


def _ends_sentence(text: str) -> bool:
    return bool(re.search(r"[.!?…][\"'»”’)]*$", text.strip()))


def _topic_terms(parts: list[dict]) -> Counter:
    terms: Counter = Counter()
    for part in parts:
        for word in normalize(str(part["text"])).split():
            if len(word) >= 4 and word not in TOPIC_STOPWORDS:
                # A short prefix reduces noise from common Russian inflections.
                terms[word[:6]] += 1
    return terms


def _topic_similarity(left: list[dict], right: list[dict]) -> float:
    left_terms, right_terms = _topic_terms(left), _topic_terms(right)
    if len(left_terms) < 3 or len(right_terms) < 3:
        return 1.0
    numerator = sum(value * right_terms.get(term, 0) for term, value in left_terms.items())
    denominator = math.sqrt(sum(value * value for value in left_terms.values())) * math.sqrt(
        sum(value * value for value in right_terms.values())
    )
    return numerator / denominator if denominator else 1.0


def _split_timed_item(item: dict) -> list[dict]:
    """Split multi-sentence Whisper segments and estimate internal timestamps proportionally."""
    text = str(item.get("text", "")).strip()
    if not text:
        return []
    start = float(item.get("start", 0))
    end = max(start, float(item.get("end", start)))
    parts = [part.strip() for part in re.findall(r".+?(?:[.!?…]+(?=\s|$)|$)", text) if part.strip()]
    if len(parts) == 1 and (len(text) > 420 or end - start > 80):
        words = text.split()
        desired_parts = max(2, math.ceil(len(text) / 320), math.ceil((end - start) / 70))
        words_per_part = max(1, math.ceil(len(words) / desired_parts))
        parts = [" ".join(words[offset:offset + words_per_part]) for offset in range(0, len(words), words_per_part)]
    if len(parts) <= 1:
        return [{"start": start, "end": end, "text": text}]
    weights = [max(1, len(part)) for part in parts]
    total_weight = sum(weights)
    duration = end - start
    units, elapsed_weight = [], 0
    for part, weight in zip(parts, weights):
        part_start = start + duration * elapsed_weight / total_weight
        elapsed_weight += weight
        part_end = start + duration * elapsed_weight / total_weight
        units.append({"start": part_start, "end": part_end, "text": part})
    return units


def make_semantic_chunks(items: list[dict]) -> list[dict]:
    """Group Whisper segments at natural speech boundaries without cutting a topic arbitrarily."""
    cleaned = [unit for item in items for unit in _split_timed_item(item)]
    chunks: list[dict] = []
    current: list[dict] = []
    characters = 0
    for index, item in enumerate(cleaned):
        if current:
            span = float(current[-1]["end"]) - float(current[0]["start"])
            gap = max(0.0, float(item["start"]) - float(current[-1]["end"]))
            enough_context = span >= 22 or characters >= 240
            sentence_end = _ends_sentence(str(current[-1]["text"]))
            long_pause = gap >= 2.0 and len(normalize(_chunk_payload(current)["text"]).split()) >= 8
            natural_pause = enough_context and gap >= 1.0
            target_sentence = sentence_end and (span >= 45 or characters >= 430)
            topic_change = (
                enough_context and sentence_end
                and _topic_similarity(current[-4:], cleaned[index:index + 4]) < 0.08
            )
            projected_span = float(item["end"]) - float(current[0]["start"])
            hard_limit = projected_span > 80 or characters + len(str(item["text"])) > 850
            if hard_limit or long_pause or natural_pause or target_sentence or topic_change:
                chunks.append(_chunk_payload(current))
                current = []
                characters = 0
        current.append(item)
        characters += len(str(item["text"]))
    if current:
        chunks.append(_chunk_payload(current))
    return chunks


def useful_semantic_text(text: str) -> bool:
    words = normalize(text).split()
    if len(words) < 8:
        return False
    counts: dict[str, int] = {}
    for word in words:
        counts[word] = counts.get(word, 0) + 1
    most_common = max(counts.values(), default=0) / len(words)
    unique_ratio = len(counts) / len(words)
    return most_common < 0.24 and unique_ratio > 0.20


def index_semantics(video_id: str, items: list[dict] | None = None) -> None:
    try:
        with db() as connection:
            video = connection.execute(
                "SELECT transcript_revision FROM videos WHERE id=?", (video_id,)
            ).fetchone()
            if not video:
                return
            revision = int(video["transcript_revision"])
            connection.execute(
                """UPDATE videos SET semantic_status='indexing',semantic_error=NULL,
                   semantic_progress=0 WHERE id=?""",
                (video_id,),
            )
            if items is None:
                rows = connection.execute(
                    "SELECT start,end,text FROM segments WHERE video_id=? ORDER BY start",
                    (video_id,),
                ).fetchall()
                items = [dict(row) for row in rows]
        chunks = [chunk for chunk in make_semantic_chunks(items) if useful_semantic_text(chunk["text"])]
        vector_parts = []
        for offset in range(0, len(chunks), 12):
            batch = chunks[offset:offset + 12]
            with embedding_lock:
                vector_parts.append(embedder().encode([chunk["text"] for chunk in batch], "passage"))
            with db() as connection:
                connection.execute(
                    """UPDATE videos SET semantic_progress=? WHERE id=?
                       AND transcript_revision=? AND semantic_status='indexing'""",
                    (min(0.99, (offset + len(batch)) / max(1, len(chunks))), video_id, revision),
                )
        if vector_parts:
            import numpy as np
            vectors = np.concatenate(vector_parts, axis=0)
        else:
            vectors = []
        with db() as connection:
            current = connection.execute(
                "SELECT transcript_revision,semantic_status FROM videos WHERE id=?", (video_id,)
            ).fetchone()
            if not current or int(current["transcript_revision"]) != revision:
                return
            connection.execute("DELETE FROM semantic_chunks WHERE video_id=?", (video_id,))
            connection.executemany(
                """INSERT INTO semantic_chunks(
                       video_id,start,end,text,embedding,model,transcript_revision
                   ) VALUES (?,?,?,?,?,?,?)""",
                [
                    (video_id, chunk["start"], chunk["end"], chunk["text"], vectors[i].tobytes(), SEMANTIC_INDEX_ID, revision)
                    for i, chunk in enumerate(chunks)
                ],
            )
            connection.execute(
                """UPDATE videos SET semantic_status='ready',semantic_error=NULL,
                   semantic_progress=1,semantic_revision=? WHERE id=?""",
                (revision, video_id),
            )
    except Exception as exc:
        with db() as connection:
            connection.execute(
                """UPDATE videos SET semantic_status='error',semantic_error=?,
                   semantic_progress=0 WHERE id=?""",
                (str(exc)[-2000:], video_id),
            )


def semantic_search(raw: str, project_id: str) -> list[dict]:
    import numpy as np

    with db() as connection:
        rows = connection.execute(
            """SELECT c.video_id,c.start,c.end,c.text,c.embedding,v.original_name,v.available
               FROM semantic_chunks c JOIN videos v ON v.id=c.video_id
               WHERE c.model=? AND c.transcript_revision=v.transcript_revision
                 AND v.semantic_revision=v.transcript_revision
                 AND v.semantic_status='ready' AND (?='' OR v.project_id=?)""",
            (SEMANTIC_INDEX_ID, project_id, project_id),
        ).fetchall()
    rows = [row for row in rows if useful_semantic_text(row["text"])]
    if not rows:
        return []
    with embedding_lock:
        query = embedder().encode([raw], "query")[0]
    compatible = [
        row for row in rows
        if len(row["embedding"]) == EMBEDDING_DIMENSION * np.dtype(np.float32).itemsize
    ]
    if not compatible:
        return []
    rows = compatible
    matrix = np.stack([np.frombuffer(row["embedding"], dtype=np.float32) for row in rows])
    scores = matrix @ query
    best = np.argsort(scores)[::-1]
    return [
        {
            "video_id": rows[i]["video_id"], "video_name": rows[i]["original_name"],
            "start": rows[i]["start"], "end": rows[i]["end"], "text": rows[i]["text"],
            "score": float(scores[i]), "match_type": "semantic", "available": bool(rows[i]["available"]),
        }
        for i in best if scores[i] >= SEMANTIC_SCORE_FLOOR
    ]


def _batched(values: list, size: int = 700):
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def exact_search(raw: str, project_id: str) -> list[dict]:
    """Find every lexical hit via the term index, then verify the complete segment."""
    words = normalize(raw).split()
    if not words:
        return []
    with db() as connection:
        vocabulary = [
            row[0] for row in connection.execute(
                """SELECT DISTINCT t.term FROM segment_terms t
                   JOIN segments s ON s.id=t.segment_id
                   JOIN videos v ON v.id=s.video_id
                   WHERE (?='' OR v.project_id=?)""",
                (project_id, project_id),
            ).fetchall()
        ]
        candidate_ids: set[int] | None = None
        for word in words:
            matching_terms = [term for term in vocabulary if text_token_matches(word, term)]
            if not matching_terms:
                return []
            word_ids: set[int] = set()
            for batch in _batched(matching_terms):
                placeholders = ",".join("?" for _ in batch)
                word_ids.update(
                    row[0] for row in connection.execute(
                        f"SELECT DISTINCT segment_id FROM segment_terms WHERE term IN ({placeholders})",
                        batch,
                    ).fetchall()
                )
            candidate_ids = word_ids if candidate_ids is None else candidate_ids & word_ids
            if not candidate_ids:
                return []
        rows = []
        for batch in _batched(sorted(candidate_ids or [])):
            placeholders = ",".join("?" for _ in batch)
            params: list[object] = list(batch)
            project_clause = ""
            if project_id:
                project_clause = " AND v.project_id=?"
                params.append(project_id)
            rows.extend(
                connection.execute(
                    f"""SELECT s.video_id,s.start,s.end,s.text,s.normalized,
                               v.original_name,v.available
                        FROM segments s JOIN videos v ON v.id=s.video_id
                        WHERE s.id IN ({placeholders}){project_clause}""",
                    params,
                ).fetchall()
            )
    rows = [row for row in rows if text_matches_query(row["normalized"], words)]
    rows.sort(key=lambda row: (row["original_name"].casefold(), row["start"]))
    return [
        {
            "video_id": row["video_id"], "video_name": row["original_name"],
            "start": row["start"], "end": row["end"], "text": row["text"],
            "score": 1.0, "match_type": "exact", "available": bool(row["available"]),
        }
        for row in rows
    ]


def scan_project(project_id: str) -> int:
    with db() as connection:
        project = connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        existing = connection.execute(
            "SELECT * FROM videos WHERE project_id=?", (project_id,)
        ).fetchall()
    if not project:
        raise ValueError("Проєкт не знайдено")
    folder = Path(project["path"])
    if not folder.is_dir():
        raise ValueError("Папка недоступна. Перевір, чи доступний диск або розташування")

    by_relative = {row["relative_path"]: row for row in existing if row["relative_path"]}
    by_source = {row["source_path"]: row for row in existing}
    staged: list[dict] = []
    now = time.time()
    cutoff = now - DURATION_RETRY_SECONDS
    # Complete every filesystem read before changing the database. If an external
    # drive disappears mid-scan, the last known-good availability state survives.
    for path in folder.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in ALLOWED:
            continue
        source = str(path.resolve())
        relative = path.relative_to(folder).as_posix()
        stat = path.stat()
        old = by_relative.get(relative) or by_source.get(source)
        video_id = old["id"] if old else hashlib.sha256(f"{project_id}\0{relative}".encode()).hexdigest()[:32]
        metadata_changed = bool(
            old and (old["size"] != stat.st_size or abs(old["mtime"] - stat.st_mtime) > .001)
        )
        fingerprint = (
            file_fingerprint(path)
            if not old or metadata_changed or not old["content_fingerprint"]
            else old["content_fingerprint"]
        )
        changed = bool(
            old
            and (
                old["size"] != stat.st_size
                or (old["content_fingerprint"] and old["content_fingerprint"] != fingerprint)
                or (
                    not old["content_fingerprint"]
                    and abs(old["mtime"] - stat.st_mtime) > .001
                )
            )
        )
        status = "ready" if not old or changed else old["status"]
        duration = float(old["media_duration"]) if old and not changed else 0.0
        duration_checked_at = float(old["duration_checked_at"]) if old and not changed else 0.0
        staged.append({
            "id": video_id,
            "name": path.name,
            "source": source,
            "relative": relative,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "fingerprint": fingerprint,
            "status": status,
            "progress": 1.0 if status == "done" else (0.0 if changed or not old else old["progress"]),
            "duration": duration,
            "duration_checked_at": duration_checked_at,
        })

    to_queue: list[str] = []
    duration_queue: list[str] = []
    with db() as connection:
        connection.execute("UPDATE videos SET available=0 WHERE project_id=?", (project_id,))
        for item in staged:
            connection.execute(
                """
                INSERT INTO videos(
                    id,project_id,original_name,source_path,relative_path,available,size,mtime,
                    content_fingerprint,status,progress,media_duration,duration_checked_at,created_at,updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id, original_name=excluded.original_name,
                    source_path=excluded.source_path, relative_path=excluded.relative_path, available=1,
                    size=excluded.size, mtime=excluded.mtime,content_fingerprint=excluded.content_fingerprint,
                    status=excluded.status, progress=excluded.progress,
                    media_duration=excluded.media_duration,duration_checked_at=excluded.duration_checked_at,
                    updated_at=excluded.updated_at
                """,
                (
                    item["id"], project_id, item["name"], item["source"], item["relative"],
                    item["size"], item["mtime"], item["fingerprint"], item["status"],
                    item["progress"], item["duration"], item["duration_checked_at"], now, now,
                ),
            )
            if item["status"] == "ready":
                next_status = "paused" if project["queue_paused"] else "queued"
                connection.execute(
                    "UPDATE videos SET status=?, progress=0, started_at=NULL, error=NULL WHERE id=?",
                    (next_status, item["id"]),
                )
                if next_status == "queued":
                    to_queue.append(item["id"])
            if item["duration"] <= 0 and item["duration_checked_at"] < cutoff:
                duration_queue.append(item["id"])
        connection.execute("UPDATE projects SET scanned_at=? WHERE id=?", (now, project_id))
    generation = int(project["queue_generation"])
    for video_id in duration_queue:
        duration_jobs.put(video_id)
    for video_id in to_queue:
        jobs.put(("transcribe", video_id, generation))
    return len(staged)


def transcription_signature(row: sqlite3.Row) -> str:
    value = (
        f"{row['size']}:{row['mtime']:.6f}:"
        f"{transcription_model()}:{TRANSCRIPTION_PART_SECONDS}"
    )
    return hashlib.sha256(value.encode()).hexdigest()


def transcription_model() -> str:
    return whisper_spec_for_device().repo_id


def transcription_command(input_path: Path, output_path: Path) -> list[str]:
    model = transcription_model()
    if getattr(sys, "frozen", False):
        return [sys.executable, "--transcribe", str(input_path), str(output_path), model]
    return [
        sys.executable,
        str(SOURCE_ROOT / "transcribe_video.py"),
        str(input_path),
        str(output_path),
        model,
    ]


def transcription_ranges(duration: float) -> list[tuple[float, float, float, float]]:
    if duration <= 0:
        return [(0.0, 0.0, 0.0, 0.0)]
    result = []
    count = max(1, math.ceil(duration / TRANSCRIPTION_PART_SECONDS))
    for index in range(count):
        core_start = index * TRANSCRIPTION_PART_SECONDS
        core_end = min(duration, (index + 1) * TRANSCRIPTION_PART_SECONDS)
        actual_start = max(0.0, core_start - (TRANSCRIPTION_OVERLAP_SECONDS if index else 0))
        actual_end = min(duration, core_end + (TRANSCRIPTION_OVERLAP_SECONDS if index < count - 1 else 0))
        result.append((core_start, core_end, actual_start, actual_end))
    return result


def run_managed_process(
    video_id: str,
    generation: int,
    command: list[str],
    on_line=None,
    watchdog: float = PROCESS_WATCHDOG_SECONDS,
) -> None:
    process_options = {}
    if sys.platform == "win32":
        process_options.update(quiet_process_options(new_process_group=True))
    else:
        process_options["start_new_session"] = True
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=0,
        **process_options,
    )
    with process_lock:
        active_processes[video_id] = process
    if not job_still_current(video_id, generation):
        terminate_process_group(process)
    assert process.stderr is not None
    line_queue: queue.Queue[str | None] = queue.Queue()
    stderr_tail = ""
    last_activity = time.time()

    def consume(line: str) -> None:
        nonlocal stderr_tail
        if line:
            stderr_tail = (stderr_tail + "\n" + line)[-6000:]
            if on_line:
                on_line(line)

    def read_stderr() -> None:
        buffer = ""
        try:
            while True:
                chunk = process.stderr.read(1)
                if not chunk:
                    break
                character = chunk.decode("utf-8", errors="replace")
                if character in "\r\n":
                    if buffer:
                        line_queue.put(buffer)
                        buffer = ""
                else:
                    buffer += character
            if buffer:
                line_queue.put(buffer)
        finally:
            line_queue.put(None)

    threading.Thread(
        target=read_stderr, daemon=True, name=f"stderr-{video_id[:8]}"
    ).start()
    stream_ended = False
    try:
        while process.poll() is None or not stream_ended:
            try:
                line = line_queue.get(timeout=1)
                if line is None:
                    stream_ended = True
                else:
                    last_activity = time.time()
                    consume(line)
            except queue.Empty:
                pass
            if not job_still_current(video_id, generation):
                terminate_process_group(process)
            if time.time() - last_activity > watchdog:
                terminate_process_group(process)
                raise RuntimeError("Процес не відповідав понад 20 хвилин")
    finally:
        with process_lock:
            if active_processes.get(video_id) is process:
                active_processes.pop(video_id, None)
    return_code = process.wait()
    with process_lock:
        interrupted = interrupt_reasons.get(video_id)
    if interrupted or not job_still_current(video_id, generation):
        raise JobInterrupted(interrupted or "stale")
    if return_code:
        raise RuntimeError((stderr_tail.strip() or "Зовнішній процес завершився з помилкою")[-3000:])


def _save_part(video_id: str, signature: str, index: int, start: float, end: float, items: list[dict]) -> None:
    with db() as connection:
        connection.execute(
            """INSERT INTO transcription_parts(video_id,signature,part_index,start,end,segments_json,created_at)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(video_id,part_index) DO UPDATE SET
               signature=excluded.signature,start=excluded.start,end=excluded.end,
               segments_json=excluded.segments_json,created_at=excluded.created_at""",
            (video_id, signature, index, start, end, json.dumps(items, ensure_ascii=False), time.time()),
        )


def replace_video_transcript(video_id: str, items: list[dict]) -> int:
    cleaned = [
        {
            "start": max(0.0, float(item.get("start", 0))),
            "end": max(0.0, float(item.get("end", item.get("start", 0)))),
            "text": str(item.get("text", "")).strip(),
        }
        for item in items if str(item.get("text", "")).strip()
    ]
    with db() as connection:
        current = connection.execute(
            "SELECT status,transcript_revision FROM videos WHERE id=?", (video_id,)
        ).fetchone()
        if not current or current["status"] != "processing":
            raise JobInterrupted("stale")
        revision = int(current["transcript_revision"]) + 1
        connection.execute("DELETE FROM segments WHERE video_id=?", (video_id,))
        for item in cleaned:
            normalized = normalize(item["text"])
            cursor = connection.execute(
                "INSERT INTO segments(video_id,start,end,text,normalized) VALUES (?,?,?,?,?)",
                (video_id, item["start"], item["end"], item["text"], normalized),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO segment_terms(segment_id,term) VALUES (?,?)",
                [(cursor.lastrowid, term) for term in set(normalized.split())],
            )
        connection.execute(
            """UPDATE videos SET status='done',progress=1,error=NULL,updated_at=?,
               semantic_status='pending',semantic_error=NULL,semantic_progress=0,
               transcript_revision=? WHERE id=?""",
            (time.time(), revision, video_id),
        )
        connection.execute("DELETE FROM transcription_parts WHERE video_id=?", (video_id,))
    output = TRANSCRIPT_DIR / f"{video_id}.json"
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"segments": cleaned}, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, output)
    return revision


def transcribe(video_id: str, generation: int) -> None:
    row = claim_transcription_job(video_id, generation)
    if not row:
        return
    source = Path(row["source_path"])
    signature = transcription_signature(row)
    try:
        if not source.is_file():
            raise RuntimeError("Відео недоступне — перевір диск або розташування файлу")
        duration = float(row["media_duration"])
        if duration <= 0:
            duration = media_duration(source)
            with db() as connection:
                connection.execute(
                    """UPDATE videos SET media_duration=?,duration_checked_at=?
                       WHERE id=? AND status='processing'""",
                    (duration, time.time(), video_id),
                )
        ranges = transcription_ranges(duration)
        with db() as connection:
            connection.execute(
                "DELETE FROM transcription_parts WHERE video_id=? AND signature!=?",
                (video_id, signature),
            )
            saved_rows = connection.execute(
                """SELECT part_index,segments_json FROM transcription_parts
                   WHERE video_id=? AND signature=?""",
                (video_id, signature),
            ).fetchall()
        saved = {int(item["part_index"]): json.loads(item["segments_json"]) for item in saved_rows}
        with db() as connection:
            connection.execute(
                "UPDATE videos SET progress=? WHERE id=? AND status='processing'",
                (len(saved) / max(1, len(ranges)), video_id),
            )
        with tempfile.TemporaryDirectory(prefix=f"video-search-{video_id[:8]}-") as temporary_dir:
            temporary_root = Path(temporary_dir)
            for index, (core_start, core_end, actual_start, actual_end) in enumerate(ranges):
                if index in saved:
                    continue
                if not job_still_current(video_id, generation):
                    raise JobInterrupted("stale")
                audio = temporary_root / f"part-{index:04d}.wav"
                result = temporary_root / f"part-{index:04d}.json"
                if actual_end > actual_start:
                    run_managed_process(
                        video_id,
                        generation,
                        [
                            bundled_tool("ffmpeg"), "-v", "error", "-nostdin", "-ss", f"{actual_start:.3f}",
                            "-i", str(source), "-t", f"{actual_end - actual_start:.3f}", "-vn",
                            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio),
                        ],
                    )
                    transcription_source = audio
                else:
                    transcription_source = source
                last_saved = 0.0

                def consume_progress(line: str, part_index: int = index) -> None:
                    nonlocal last_saved
                    parsed = progress_from_line(line)
                    now = time.time()
                    if parsed is not None and (parsed >= 1 or now - last_saved >= 0.8):
                        overall = (part_index + parsed) / max(1, len(ranges))
                        with db() as connection:
                            connection.execute(
                                """UPDATE videos SET progress=?,updated_at=? WHERE id=?
                                   AND status='processing'""",
                                (overall, now, video_id),
                            )
                        last_saved = now

                run_managed_process(
                    video_id,
                    generation,
                    transcription_command(transcription_source, result),
                    consume_progress,
                )
                payload = json.loads(result.read_text(encoding="utf-8"))
                part_items = []
                for item in payload.get("segments", []):
                    absolute_start = float(item.get("start", 0)) + actual_start
                    absolute_end = float(item.get("end", absolute_start)) + actual_start
                    midpoint = (absolute_start + absolute_end) / 2
                    if midpoint < core_start or (index < len(ranges) - 1 and midpoint >= core_end):
                        continue
                    part_items.append({"start": absolute_start, "end": absolute_end, "text": item.get("text", "")})
                _save_part(video_id, signature, index, core_start, core_end, part_items)
                saved[index] = part_items
                with db() as connection:
                    connection.execute(
                        "UPDATE videos SET progress=?,updated_at=? WHERE id=? AND status='processing'",
                        ((index + 1) / len(ranges), time.time(), video_id),
                    )
        combined = [item for index in range(len(ranges)) for item in saved.get(index, [])]
        replace_video_transcript(video_id, combined)
        index_semantics(video_id, combined)
    except JobInterrupted:
        pass
    except Exception as exc:
        with process_lock:
            interrupted = interrupt_reasons.get(video_id)
        if not interrupted:
            with db() as connection:
                connection.execute(
                    """UPDATE videos SET status='error',error=?,started_at=NULL,updated_at=?
                       WHERE id=? AND status='processing'""",
                    (str(exc), time.time(), video_id),
                )
    finally:
        with process_lock:
            active_processes.pop(video_id, None)
            interrupt_reasons.pop(video_id, None)


def worker() -> None:
    while not runtime_stopping.is_set():
        try:
            action, video_id, generation = jobs.get(timeout=0.25)
        except queue.Empty:
            continue
        try:
            if runtime_stopping.is_set():
                continue
            model_manager.wait()
            if runtime_stopping.is_set():
                continue
            if action == "semantic":
                row = get_video(video_id)
                if row and row["semantic_status"] in {"pending", "error"}:
                    with db() as connection:
                        has_segments = connection.execute(
                            "SELECT EXISTS(SELECT 1 FROM segments WHERE video_id=?)", (video_id,)
                        ).fetchone()[0]
                    if has_segments:
                        index_semantics(video_id)
            else:
                transcribe(video_id, generation)
        except Exception as exc:
            print(f"Worker recovered after {action} error for {video_id}: {exc}", file=sys.stderr)
            try:
                with db() as connection:
                    connection.execute(
                        """UPDATE videos SET status='error',error=?,updated_at=?
                           WHERE id=? AND status='processing'""",
                        (f"Внутрішня помилка worker: {str(exc)[-1800:]}", time.time(), video_id),
                    )
            except Exception as recovery_exc:
                print(f"Could not record worker error: {recovery_exc}", file=sys.stderr)
        finally:
            jobs.task_done()


def duration_worker() -> None:
    while not runtime_stopping.is_set():
        try:
            video_id = duration_jobs.get(timeout=0.25)
        except queue.Empty:
            continue
        try:
            if not runtime_stopping.is_set():
                probe_media_duration(video_id)
        except Exception as exc:
            print(f"Duration probe failed for {video_id}: {exc}", file=sys.stderr)
        finally:
            duration_jobs.task_done()


def start_runtime() -> None:
    """Start model work only after the machine check has been accepted."""
    global runtime_started, worker_thread, duration_worker_thread
    if not hardware_preflight.apply_saved_device():
        raise ValueError("Спочатку підтвердь перевірку сумісності комп’ютера")
    model_manager.start()
    with runtime_lock:
        if runtime_started:
            return
        runtime_stopping.clear()
        runtime_started = True
        worker_thread = threading.Thread(target=worker, daemon=True, name="rothbald-worker")
        worker_thread.start()
        duration_worker_thread = threading.Thread(
            target=duration_worker, daemon=True, name="rothbald-duration-worker"
        )
        duration_worker_thread.start()
        enqueue_due_duration_probes()
        resume_pending_semantic_indexing()


def shutdown_runtime(timeout: float = 7.0) -> None:
    """Pause unfinished work and terminate every managed child before app exit."""
    global runtime_started, worker_thread, duration_worker_thread
    runtime_stopping.set()
    try:
        if DB_PATH.is_file():
            with db() as connection:
                project_ids = [
                    row[0] for row in connection.execute(
                        """SELECT DISTINCT project_id FROM videos
                           WHERE status IN ('queued','processing')"""
                    ).fetchall()
                ]
                for project_id in project_ids:
                    connection.execute(
                        """UPDATE projects SET queue_paused=1,
                           queue_generation=queue_generation+1 WHERE id=?""",
                        (project_id,),
                    )
                connection.execute(
                    """UPDATE videos SET status='paused',started_at=NULL,
                       error='Черга очікує ручного продовження після закриття застосунку',updated_at=?
                       WHERE status IN ('queued','processing')""",
                    (time.time(),),
                )
                connection.execute(
                    "UPDATE videos SET semantic_status='pending',semantic_progress=0 WHERE semantic_status='indexing'"
                )
    except sqlite3.Error as exc:
        print(f"Could not persist shutdown state: {exc}", file=sys.stderr)

    with process_lock:
        running = list(active_processes.items())
        for video_id, _process in running:
            interrupt_reasons[video_id] = "shutdown"
    for _video_id, process in running:
        if process.poll() is None:
            terminate_process_group(process)

    deadline = time.monotonic() + max(0.0, timeout)
    for _video_id, process in running:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()

    while True:
        try:
            jobs.get_nowait()
        except queue.Empty:
            break
        else:
            jobs.task_done()
    while True:
        try:
            duration_jobs.get_nowait()
        except queue.Empty:
            break
        else:
            duration_jobs.task_done()
    for thread in (worker_thread, duration_worker_thread):
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
    with runtime_lock:
        runtime_started = False
        worker_thread = None
        duration_worker_thread = None


def resume_pending_semantic_indexing() -> None:
    with db() as connection:
        semantic_rows = connection.execute(
            """SELECT v.id,p.queue_generation FROM videos v JOIN projects p ON p.id=v.project_id
               WHERE v.semantic_status IN ('pending','error')
               AND EXISTS (SELECT 1 FROM segments s WHERE s.video_id=v.id)
               ORDER BY v.created_at,v.original_name"""
        ).fetchall()
    for row in semantic_rows:
        jobs.put(("semantic", row["id"], int(row["queue_generation"])))


def parse_range_header(header: str | None, size: int) -> tuple[int, int] | None:
    if size <= 0:
        return None
    if not header:
        return (0, size - 1)
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or size <= 0:
        return None
    left, right = match.groups()
    if not left and not right:
        return None
    if not left:
        suffix = int(right)
        if suffix <= 0:
            return None
        return (max(0, size - suffix), size - 1)
    start = int(left)
    end = min(int(right), size - 1) if right else size - 1
    if start >= size or start > end:
        return None
    return (start, end)


def backend_change_blocker() -> str | None:
    """Protect active model/transcription work from a runtime backend switch."""
    if not runtime_started:
        return None
    if model_manager.snapshot()["status"] in {"checking", "downloading"}:
        return "Дочекайся завершення перевірки або завантаження моделей."
    with db() as connection:
        busy = connection.execute(
            """SELECT EXISTS(
                   SELECT 1 FROM videos
                   WHERE status IN ('queued','processing')
                      OR semantic_status IN ('pending','indexing')
               )"""
        ).fetchone()[0]
    if busy:
        return "Дочекайся завершення розпізнавання та індексації або зупини активну чергу."
    return None


def hardware_api_report() -> dict:
    report = hardware_preflight.inspect()
    blocker = backend_change_blocker()
    report["backend_change_allowed"] = blocker is None
    report["backend_change_blocker"] = blocker
    return report


class Handler(BaseHTTPRequestHandler):
    server_version = "VideoSearch/0.3"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/bootstrap":
            return respond(self, model_manager.snapshot())
        if parsed.path == "/api/hardware":
            return respond(self, hardware_api_report())
        if parsed.path == "/api/app":
            return respond(self, application_info())
        if parsed.path == "/api/update":
            return respond(self, update_manager.snapshot())
        if parsed.path == "/api/projects":
            return self.projects()
        if parsed.path == "/api/videos":
            return self.videos(query.get("project", [""])[0])
        if parsed.path == "/api/search":
            return self.search(query.get("q", [""])[0], query.get("project", [""])[0])
        if parsed.path == "/api/search/exact":
            return self.search_exact(query.get("q", [""])[0], query.get("project", [""])[0])
        if parsed.path == "/api/search/semantic":
            return self.search_semantic(query.get("q", [""])[0], query.get("project", [""])[0])
        if parsed.path.startswith("/media/"):
            return self.media(parsed.path.rsplit("/", 1)[-1])
        if parsed.path in {"/", "/index.html"}:
            return self.static("index.html")
        if parsed.path == "/favicon.svg":
            return self.static("favicon.svg")
        if parsed.path.startswith("/static/"):
            return self.static(parsed.path.removeprefix("/static/"))
        self.send_error(404)

    def do_POST(self) -> None:
        if not self.trusted_origin():
            return respond(self, {"error": "Запит відхилено: недовірене джерело"}, 403)
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/hardware/confirm":
            try:
                length = min(16_384, max(0, int(self.headers.get("Content-Length", "0") or 0)))
                payload = json.loads(self.rfile.read(length) or b"{}")
                device = str(payload.get("device", "auto"))
                current = hardware_preflight.inspect()
                blocker = backend_change_blocker()
                if current["accepted"] and device != current["selected_device"] and blocker:
                    raise ValueError(blocker)
                hardware_preflight.confirm(device)
                start_runtime()
                return respond(self, hardware_api_report())
            except (ValueError, json.JSONDecodeError) as exc:
                return respond(self, {"error": str(exc)}, 400)
        if path == "/api/bootstrap/start":
            try:
                start_runtime()
                model_manager.start(force=model_manager.snapshot()["status"] == "error")
                return respond(self, model_manager.snapshot(), 202)
            except ValueError as exc:
                return respond(self, {"error": str(exc)}, 409)
        if path == "/api/update/check":
            return respond(self, update_manager.start_check(), 202)
        if path == "/api/update/download":
            try:
                return respond(self, update_manager.start_download(), 202)
            except ValueError as exc:
                return respond(self, {"error": str(exc)}, 409)
        if path == "/api/update/install":
            try:
                update_manager.install()
                return respond(self, {"launched": True}, 202)
            except ValueError as exc:
                return respond(self, {"error": str(exc)}, 409)
        if path == "/api/projects/choose":
            return self.choose_folder()
        match = re.fullmatch(r"/api/projects/([0-9a-f]+)/open", path)
        if match:
            return self.open_project(match.group(1))
        match = re.fullmatch(r"/api/projects/([0-9a-f]+)/locate", path)
        if match:
            return self.locate_project(match.group(1))
        match = re.fullmatch(r"/api/projects/([0-9a-f]+)/scan", path)
        if match:
            return self.rescan(match.group(1))
        match = re.fullmatch(r"/api/projects/([0-9a-f]+)/retranscribe", path)
        if match:
            return self.retranscribe_project(match.group(1))
        match = re.fullmatch(r"/api/projects/([0-9a-f]+)/(pause|resume|abort)", path)
        if match:
            return self.control_queue(match.group(1), match.group(2))
        match = re.fullmatch(r"/api/videos/([0-9a-f]+)/transcribe", path)
        if match:
            return self.enqueue(match.group(1))
        self.send_error(404)

    def do_DELETE(self) -> None:
        if not self.trusted_origin():
            return respond(self, {"error": "Запит відхилено: недовірене джерело"}, 403)
        path = urllib.parse.urlparse(self.path).path
        match = re.fullmatch(r"/api/projects/([0-9a-f]+)", path)
        if match:
            return self.delete_project(match.group(1))
        self.send_error(404)

    def trusted_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urllib.parse.urlparse(origin)
        try:
            port = parsed.port
        except ValueError:
            return False
        return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"} and port == PORT

    def choose_folder(self) -> None:
        folder = choose_folder_dialog("Обери папку нового проєкту")
        if folder is None:
            return respond(self, {"cancelled": True})
        try:
            if not folder.is_dir():
                raise ValueError("Папка недоступна")
            project_id = uuid.uuid4().hex
            now = time.time()
            with db() as connection:
                existing = connection.execute("SELECT id FROM projects WHERE path=?", (str(folder),)).fetchone()
                if existing:
                    project_id = existing["id"]
                connection.execute(
                    """INSERT INTO projects(id,name,path,created_at,scanned_at,last_opened_at)
                       VALUES (?,?,?,?,0,?) ON CONFLICT(path) DO UPDATE SET
                       name=excluded.name,last_opened_at=excluded.last_opened_at""",
                    (project_id, folder.name, str(folder), now, now),
                )
            count = scan_project(project_id)
            respond(self, {"id": project_id, "name": folder.name, "path": str(folder), "videos": count, "existing": bool(existing)}, 201)
        except Exception as exc:
            respond(self, {"error": str(exc)}, 500)

    def open_project(self, project_id: str) -> None:
        try:
            info = verify_project_files(project_id)
            with db() as connection:
                connection.execute("UPDATE projects SET last_opened_at=? WHERE id=?", (time.time(), project_id))
            respond(self, info)
        except ValueError as exc:
            respond(self, {"error": str(exc)}, 404)
        except sqlite3.IntegrityError:
            respond(self, {"error": "Не вдалося оновити розташування файлів"}, 409)

    def locate_project(self, project_id: str) -> None:
        folder = choose_folder_dialog("Знайди нове розташування папки цього проєкту")
        if folder is None:
            return respond(self, {"cancelled": True})
        try:
            respond(self, relocate_project(project_id, folder))
        except ValueError as exc:
            respond(self, {"error": str(exc)}, 400)
        except sqlite3.IntegrityError:
            respond(self, {"error": "У цій папці є файли, вже прив’язані до іншого проєкту"}, 409)

    def rescan(self, project_id: str) -> None:
        try:
            respond(self, {"videos": scan_project(project_id)})
        except Exception as exc:
            respond(self, {"error": str(exc)}, 400)

    def retranscribe_project(self, project_id: str) -> None:
        backup_database("before-retranscribe", force=True)
        with db() as connection:
            project = connection.execute("SELECT id,queue_paused,queue_generation FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                return respond(self, {"error": "Проєкт не знайдено"}, 404)
            if project["queue_paused"]:
                return respond(self, {"error": "Черга на паузі. Спочатку продовж її або натисни Abort"}, 409)
            busy = connection.execute(
                "SELECT COUNT(*) FROM videos WHERE project_id=? AND status IN ('queued','processing','paused')",
                (project_id,),
            ).fetchone()[0]
            if busy:
                return respond(
                    self,
                    {"error": "Спочатку дочекайся завершення поточної черги"},
                    409,
                )
            rows = connection.execute(
                "SELECT id FROM videos WHERE project_id=? AND available=1 ORDER BY original_name",
                (project_id,),
            ).fetchall()
            generation = int(project["queue_generation"]) + 1
            connection.execute(
                "UPDATE projects SET queue_generation=? WHERE id=?", (generation, project_id)
            )
            connection.execute(
                "DELETE FROM transcription_parts WHERE video_id IN (SELECT id FROM videos WHERE project_id=?)",
                (project_id,),
            )
            connection.execute(
                """UPDATE videos SET status='queued', progress=0, started_at=NULL,
                   error=NULL, updated_at=? WHERE project_id=? AND available=1""",
                (time.time(), project_id),
            )
        for row in rows:
            jobs.put(("transcribe", row["id"], generation))
        respond(self, {"queued": len(rows)}, 202)

    def delete_project(self, project_id: str) -> None:
        with db() as connection:
            project = connection.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                return respond(self, {"error": "Проєкт не знайдено"}, 404)
            busy_ids = {
                row[0] for row in connection.execute(
                    "SELECT id FROM videos WHERE project_id=? AND status='processing'", (project_id,)
                ).fetchall()
            }
            all_ids = [
                row[0] for row in connection.execute("SELECT id FROM videos WHERE project_id=?", (project_id,))
            ]
        interrupt_project_processes(busy_ids, "cancelled")
        backup_database("before-project-delete", force=True)
        with db() as connection:
            connection.execute("DELETE FROM projects WHERE id=?", (project_id,))
        for video_id in all_ids:
            (TRANSCRIPT_DIR / f"{video_id}.json").unlink(missing_ok=True)
        respond(self, {"deleted": True})

    def control_queue(self, project_id: str, action: str) -> None:
        try:
            respond(self, control_project_queue(project_id, action), 202)
        except ValueError as exc:
            respond(self, {"error": str(exc)}, 404)

    def projects(self) -> None:
        with db() as connection:
            rows = connection.execute(
                """SELECT p.*, COUNT(v.id) video_count,
                   COALESCE(SUM(CASE WHEN v.status='done' THEN 1 ELSE 0 END),0) done_count,
                   COALESCE(SUM(CASE WHEN v.status IN ('queued','processing') THEN 1 ELSE 0 END),0) busy_count,
                   COALESCE(SUM(CASE WHEN v.status='paused' THEN 1 ELSE 0 END),0) paused_count,
                   COALESCE(SUM(CASE WHEN v.available=0 THEN 1 ELSE 0 END),0) missing_count,
                   COALESCE(SUM(CASE WHEN v.semantic_status IN ('pending','indexing')
                                     AND EXISTS (SELECT 1 FROM segments s WHERE s.video_id=v.id)
                                THEN 1 ELSE 0 END),0) semantic_busy_count
                   FROM projects p LEFT JOIN videos v ON v.project_id=p.id
                   GROUP BY p.id ORDER BY p.last_opened_at DESC,p.created_at DESC"""
            ).fetchall()
        payload = []
        for row in rows:
            item = dict(row)
            item["folder_available"] = Path(row["path"]).is_dir()
            payload.append(item)
        respond(self, payload)

    def videos(self, project_id: str) -> None:
        with db() as connection:
            rows = connection.execute(
                """SELECT v.*, COUNT(s.id) segment_count, COALESCE(MAX(s.end),0) duration
                   FROM videos v LEFT JOIN segments s ON s.video_id=v.id
                   WHERE (?='' OR v.project_id=?) GROUP BY v.id ORDER BY v.original_name""",
                (project_id, project_id),
            ).fetchall()
        respond(self, [{
            "id": r["id"], "name": r["original_name"], "size": r["size"],
            "status": r["status"], "error": r["error"], "segments": r["segment_count"],
            "duration": r["media_duration"] or r["duration"], "progress": r["progress"],
            "started_at": r["started_at"], "updated_at": r["updated_at"],
            "semantic_status": r["semantic_status"], "semantic_error": r["semantic_error"],
            "semantic_progress": r["semantic_progress"],
            "transcript_revision": r["transcript_revision"], "semantic_revision": r["semantic_revision"],
            "available": bool(r["available"]), "relative_path": r["relative_path"],
        } for r in rows])

    def enqueue(self, video_id: str) -> None:
        row = get_video(video_id)
        if not row:
            return respond(self, {"error": "Відео не знайдено"}, 404)
        if not row["available"] or not Path(row["source_path"]).is_file():
            return respond(self, {"error": "Файл недоступний. Скористайся Locate для проєкту"}, 409)
        if project_is_paused(row["project_id"]):
            return respond(self, {"error": "Черга проєкту на паузі. Спочатку натисни «Продовжити»"}, 409)
        if row["status"] not in {"queued", "processing"}:
            with db() as connection:
                has_parts = connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM transcription_parts WHERE video_id=?)", (video_id,)
                ).fetchone()[0]
                connection.execute(
                    "UPDATE videos SET status='queued',progress=?,started_at=NULL,error=NULL WHERE id=?",
                    (row["progress"] if has_parts else 0, video_id),
                )
            jobs.put(("transcribe", video_id, project_queue_generation(row["project_id"])))
        respond(self, {"status": "queued"}, 202)

    def search(self, raw: str, project_id: str) -> None:
        if not normalize(raw):
            return respond(self, [])
        lexical = exact_search(raw, project_id)
        try:
            semantic = semantic_search(raw, project_id)
        except Exception as exc:
            print(f"Semantic search unavailable: {exc}", file=sys.stderr)
            semantic = []
        # Return every hit. The UI separates result types into tabs.
        respond(self, semantic + lexical)

    def search_exact(self, raw: str, project_id: str) -> None:
        try:
            respond(self, exact_search(raw, project_id))
        except Exception as exc:
            respond(self, {"error": f"Точний пошук не виконався: {exc}"}, 500)

    def search_semantic(self, raw: str, project_id: str) -> None:
        try:
            respond(self, semantic_search(raw, project_id) if normalize(raw) else [])
        except Exception as exc:
            respond(self, {"error": f"Пошук за змістом не виконався: {exc}"}, 500)

    def static(self, relative: str) -> None:
        target = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            return self.send_error(HTTPStatus.NOT_FOUND)
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def media(self, video_id: str) -> None:
        row = get_video(video_id)
        target = Path(row["source_path"]) if row else None
        if not target or not target.is_file():
            return self.send_error(404)
        size = target.stat().st_size
        header = self.headers.get("Range")
        parsed_range = parse_range_header(header, size)
        if parsed_range is None:
            self.send_response(416); self.send_header("Content-Range", f"bytes */{size}"); self.end_headers(); return
        start, end = parsed_range
        self.send_response(206 if header else 200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with target.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk: break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)


def create_http_server() -> ThreadingHTTPServer:
    init_storage()
    http_server = ThreadingHTTPServer((HOST, PORT), Handler)
    http_server.daemon_threads = True
    try:
        if hardware_preflight.apply_saved_device():
            start_runtime()
    except Exception:
        http_server.server_close()
        raise
    return http_server


def main() -> None:
    server = create_http_server()
    print(f"Відкрий http://{HOST}:{PORT}")
    print(f"Модель: {transcription_model()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nЗупинено")
    finally:
        shutdown_runtime()
        server.server_close()


if __name__ == "__main__":
    main()
