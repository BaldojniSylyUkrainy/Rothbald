# READMEAI — project memory for Codex

Last updated: 2026-07-26

This file is the authoritative technical memory for the local video-search application. Read it completely before diagnosing or modifying the app. After every material change, update the affected sections and append a dated changelog entry.

Do not store secrets, tokens, or full user media paths here. User media and generated data belong in `data/` or on the selected external drive and must not be committed.

## Product goal

A private, local Apple Silicon and Windows application for searching long-form videos used in editing projects. The user selects a project folder on any available internal or external storage. The app recursively discovers media, transcribes it locally, and returns clickable file-relative timestamps.

The app must support two distinct search modes across every indexed video in the selected project:

- exact text search;
- multilingual semantic search for the same idea expressed with different words.

Selecting a video only controls the player. It must never narrow the search scope.

## Target environments

- Apple Silicon uses `mlx-whisper` and is the primary optimized target.
- Windows x64 uses `faster-whisper` Turbo on CPU/int8 by default. `ROTHBALD_CUDA=1` opts into CUDA/float16 when the machine has a compatible runtime.
- Source launches require `ffmpeg` and `ffprobe`; packaged builds include both binaries.
- Python virtual environment: `.venv/`.
- Local URL: `http://127.0.0.1:8765`.
- The local URL is an internal transport only. `rothbald.py` presents it inside a dedicated pywebview desktop window: WKWebView/Cocoa on macOS and WebView2/WinForms on Windows. Packaged users do not interact with an external browser.
- macOS source launch uses `start.command` / `setup.command`; Windows uses `start.bat` / `setup.ps1`.
- `start.command` runs the server under `/usr/bin/caffeinate -i`: idle system sleep is prevented for the lifetime of the app, while display sleep and screen locking remain allowed. Active transcription keeps the source disk in use.
- Packaged builds store generated data under Application Support on macOS and LocalAppData on Windows. Source launches retain the repository-local `data/` behavior.
- No VPS or automatic internet video downloading is in scope for this version.

## Main files

- `server.py` — HTTP server, SQLite storage, folder scanning, queue, transcription orchestration, semantic indexing, search, and media range serving.
- `transcribe_video.py` — isolated MLX Whisper transcription subprocess.
- `prepare_models.py` / `prepare_semantic.py` — download and verify the Whisper and semantic models during setup.
- `model_manager.py` — platform model manifest, remote revision check, resilient local verification, exact byte progress, and background downloads for the startup gate.
- `rothbald.py` / `Rothbald.spec` — native desktop-window launcher and PyInstaller definition.
- `assets/app-icon.png`, `.icns`, and `.ico` — rounded `Ro` application icon derived from the same Bradley Hand wordmark used by the UI.
- `static/index.html` — application markup.
- `static/app.js` — client state, queue progress, search tabs, video selection, and project actions.
- `static/style.css` — responsive dark interface.
- `requirements.txt` — direct Python dependencies; `requirements.lock` is the reproducible full environment used by setup.
- `VERSION` — the single four-component public application version.
- `app_info.py` and `scripts/prepare_build.py` — runtime build metadata and deterministic platform version resources.
- `.github/workflows/release.yml` — manual gated signed/notarized draft-release pipeline.
- `tests/test_server.py` — standard-library regression tests for migrations, queue claiming, matching, ranges, and chunk checkpoints.
- `README.md` — user-facing setup and usage.

## Runtime architecture

### Media discovery

The user chooses a folder through the platform-native macOS or Windows picker. `scan_project()` recursively finds supported media without copying it. Supported extensions currently include MP4, MOV, MKV, WebM, M4V, AVI, MP3, M4A, WAV, FLAC, and OGG.

Video identity is stable inside a project and based on `project_id + relative_path`, not the absolute SSD path. The historical global `UNIQUE(source_path)` is rebuilt away during migration, so the same physical media may safely appear in two different projects. Size and mtime detect modifications; a quick size + first/last-megabyte fingerprint protects `Locate` from attaching an unrelated folder. `ffprobe` has a 45-second timeout and always runs outside SQLite write transactions. Rescanning queues only new or changed media.

Scanning first marks the project's media unavailable and then marks every rediscovered relative path available. It must never delete a video row, transcript, or semantic index merely because a file or drive is absent. This is required for removable SSDs and `Locate`.

### Transcription

- Apple engine/model: `mlx-whisper` with `mlx-community/whisper-large-v3-turbo`.
- Windows engine/model: `faster-whisper` with `mobiuslabsgmbh/faster-whisper-large-v3-turbo`.
- There is no UI model selector. Platform selection is automatic so saved transcription signatures never ambiguously mix engines.
- Both transcription backends use the same fixed language hint for deterministic decoding.
- Whisper runs in a subprocess so its MLX/Metal memory is released after each file.
- Media is split into 30-minute core ranges with two-second overlap. `ffmpeg` extracts one temporary mono WAV at a time; completed parts are checkpointed in `transcription_parts`. Resume reuses all completed parts and repeats only the interrupted part. Midpoint ownership removes overlap duplicates when timestamps are reassembled.
- Only Whisper may use Metal/GPU resources.
- Progress is parsed from Whisper/tqdm stderr and saved in SQLite.
- `ffmpeg` and Whisper run in their own process groups. Pause/Abort first terminate the complete group and force-kill it after five seconds if necessary, preventing orphan decoders.
- If a managed subprocess produces no stderr activity for 20 minutes, the watchdog terminates it and marks the video as errored instead of hanging forever.
- UI marks a processing item as potentially stalled after 10 minutes without a progress update.
- On application restart, every project containing `processing` or `queued` transcription items is atomically moved to the paused state, its queue generation is advanced, and its unfinished videos become `paused`. Whisper must never auto-resume at launch or merely because the project was opened; the user must press `Продовжити`.
- Queue pause is persisted per project. Paused projects remain paused across application restarts.
- Pausing immediately terminates the current managed subprocess and changes every unfinished item in that project to `paused`. Saved part checkpoints and progress remain intact.
- Aborting immediately terminates the current managed subprocess, changes unfinished items to `cancelled`, and deletes their checkpoints. Completed transcripts and prior atomic transcript versions remain intact.
- Each project has a persistent `queue_generation`. Pause and abort invalidate the previous generation; a fresh full-folder re-transcription creates a new generation. Every in-memory transcription job carries its generation, and the worker rejects stale jobs before starting Whisper. This prevents repeated abort/restart cycles from reordering or reviving old work.
- Job claiming is one conditional SQLite `UPDATE` that checks video status, availability, project pause, and generation atomically. Never reintroduce separate check-then-set calls; they allowed a pause race to launch a `processing` job.

Important resource decision: semantic embeddings must run on CPU. Do not move them back to Torch MPS without solving unified-memory contention with MLX Whisper. Running both on Metal caused a long transcription to stall at 8%. On Windows, semantic embeddings also remain on CPU so transcription behavior is predictable across CPU and optional CUDA machines.

### Model bootstrap and updates

- The HTTP server starts before models are ready so the UI can show the model gate.
- `GET /api/bootstrap` exposes overall status plus separate speech/meaning byte totals, downloaded bytes, percentages, current file, and errors.
- `POST /api/bootstrap/start` starts or retries the background check.
- Every launch verifies local snapshots and checks the current Hugging Face revision when online. A changed revision downloads through the same progress UI and updates `model-manifest.json` only after both model snapshots verify.
- First launch requires the network. Later launches may proceed offline when both local snapshots are intact.
- Queue work blocks on the model manager, while project and static endpoints stay available.

### Atomic transcript replacement

New transcription output is parsed before existing segments are deleted. Existing transcript/search data therefore remains available until a replacement succeeds. A successful replacement increments `transcript_revision`; semantic chunks are stamped with the same revision and are searchable only when `semantic_revision == transcript_revision`. If semantic reindexing fails, vectors from the older transcript can never leak into results. On transcription failure, the prior searchable data remains.

### Semantic indexing

- Sole model: `intfloat/multilingual-e5-large-instruct` (0.6B parameters, 1024-dimensional embeddings). The local Hugging Face cache is about 1.1 GB on the target machine.
- Runs locally on CPU through Hugging Face Transformers and PyTorch.
- Queries use the model's required `Instruct: ...\nQuery: ...` format with a transcript-retrieval instruction. Documents are embedded without a prefix, as required by the Instruct model.
- The model is multilingual; queries should use the same writing system as the source transcript because the same query also feeds the text-match tab.
- Whisper segments containing multiple sentences are split at sentence punctuation. Internal timestamps are estimated proportionally because Whisper only supplies the enclosing segment time range.
- Chunks end at natural pauses, completed sentences near the target size, or a likely lexical topic change measured across rolling neighboring segments. Soft context begins around 22 seconds/240 characters; hard bounds are about 80 seconds/850 characters. Abnormally long single Whisper segments are subdivided before chunking. Topic boundaries do not overlap, avoiding cross-topic contamination.
- Repetitive/hallucinated chunks are filtered using token-frequency and unique-word ratios.
- Normalized float32 embeddings are stored as SQLite BLOBs.
- Semantic indexing is persisted as `pending/indexing/ready/error` with a separate `semantic_progress`. The UI polls through final indexing and shows its own blue project-wide progress bar instead of making the green transcription bar appear stuck.
- Semantic similarity uses cosine similarity via a dot product of normalized vectors.
- Current semantic relevance floor is `0.70`, calibrated conservatively for the higher similarity distribution of Large Instruct. This is a relevance threshold, not a result-count limit.
- Stored chunk model IDs include the chunking version (`@natural-topic-v2`). On startup, every video with saved transcript segments but without the current model/version is marked for reindexing, regardless of its transcription queue status or media availability. Model or chunk-format migrations therefore cannot silently reuse incompatible vectors or lose offline transcripts during a paused re-transcription.

### Search behavior

Search always covers all indexed videos in the selected project.

The search bar contains only the query and the general `Знайти` button. Project switching belongs exclusively to the recent-project home screen; do not restore the redundant project dropdown inside the workspace.

- Text matching normalizes case and punctuation; maps common keyboard and orthographic variants; handles basic language-specific inflections; and permits one Damerau-Levenshtein edit (including adjacent transposition) in query words of at least five letters.
- The `Точні` tab returns every transcript segment matching all normalized query tokens. Matching candidates come from the indexed `segment_terms` vocabulary instead of scanning every transcript token for every request. Arbitrary interior substring matching is forbidden: `мир` must not match `Владимир` and `рост` must not match `просто`. It does not return a separate row for every repeated occurrence inside one segment.
- Semantic search returns every useful chunk at or above the relevance threshold, sorted by score.
- There are no count limits on exact or semantic results.
- The browser launches exact and semantic endpoints independently. Exact hits render as soon as they arrive; a slow semantic query does not block them. A newer query aborts both older requests and stale project responses are ignored.
- UI tabs: `За змістом`, `Точні`, and `Усі`, each with a full result count.
- All results stay in client state, but only 100 DOM rows are rendered at a time and additional rows are appended as the user scrolls. This is presentation virtualization, not a result limit.
- Semantic and exact results are intentionally not deduplicated across tabs.
- Result clicks seek the local player to approximately 1.5 seconds before the timestamp.

### Video selection

- Clicking a sidebar video selects it for playback.
- Clicking the selected sidebar video again clears it.
- The `×` button over the player also clears the selection.
- Clicking a search result selects/seeks the referenced video and leaves that exact or semantic result visibly highlighted. The highlight survives asynchronous result updates, incremental rendering, and tab switches. Clicking the same result again removes only its highlight without stopping playback; choosing another result transfers the highlight, while choosing a sidebar video or clearing the player removes it.
- A persistent caption strip directly below the player shows the selected video's full name and, when different, its project-relative path. It updates for both sidebar selection and search-result navigation, so the active source remains identifiable even when its sidebar row is far outside the scroll position.

### Project actions

- App launch shows a home screen with recent projects and no project opened by default. Unfinished transcription queues remain paused until the user explicitly presses `Продовжити` after opening a project. Pending semantic indexing may recover in the background, but Whisper transcription does not.
- A project is an internal SQLite record; the user does not manage a separate project file.
- Opening a recent project performs a quick existence check at the saved folder path without a recursive rescan.
- `Locate` changes the project's root folder while preserving stable video IDs, transcripts, semantic chunks, and queue history. Videos are reconnected using their relative paths.
- Missing media is visibly marked, cannot be played or requeued, but its completed transcript remains searchable.
- `До проєктів` returns to the empty home screen without deleting data or stopping background work.
- `Оновити папку` rescans and queues only new or modified media.
- `Перерозпізнати все` is available only when the current queue is idle.
- It requires browser confirmation and queues every currently available video in the selected project.
- It does not immediately delete old transcripts; each is atomically replaced after successful reprocessing.
- `Пауза` stops the active subprocess and pauses all queued videos for the current project; `Продовжити` requeues only those paused videos.
- `Abort` clears the unfinished queue and returns the project to an idle state. The user can then run `Перерозпізнати все` to rebuild the whole available folder from the beginning.
- `Видалити проєкт` deletes only its local database rows, checkpoints, vectors, and transcript JSON files after explicit browser confirmation. It never touches source media.

## Queue and ETA

The UI shows:

- per-video progress, percentage, and approximate ETA;
- total processed media duration versus project duration;
- completed video count;
- project-wide ETA estimated from the active video's observed processing speed.
- a separate semantic-index percentage and completed-video count.

ETA is approximate and stabilizes only after the active file has measurable progress.

## SQLite data

Database: `data/video_search.sqlite3`. Generated transcript JSON files: `data/transcripts/`. The entire `data/` directory is ignored by Git.

Core tables:

- `projects` — stable project ID, display name, saved root folder, creation/scan times, `last_opened_at`, pause state, and persistent queue generation.
- `videos` — stable ID, project-relative path, current absolute source path, availability, file fingerprint, queue state, progress, duration, errors, and semantic state.
- `segments` — timestamped Whisper transcript segments plus normalized text.
- `segment_terms` — deduplicated normalized term index per segment for scalable text lookup.
- `semantic_chunks` — larger timestamped passages, embedding BLOBs, model ID, and transcript revision.
- `transcription_parts` — resumable 30-minute part payloads keyed by video, source/model signature, and part index.

Schema additions are migrated in `init_storage()` using `PRAGMA table_info(videos)` and conditional `ALTER TABLE` statements. Preserve backward compatibility with the user's existing database.

Rolling SQLite backups live in `data/backups/`. Startup creates at most one per day; destructive local actions create one immediately; the newest five are retained. Backups and all generated data are Git-ignored.

`db()` is a custom context manager that commits successful transactions, rolls back failures, and always closes the SQLite connection. Do not change it back to returning a raw `sqlite3.Connection`: SQLite's own `with connection:` transaction context does not close the connection and previously leaked file descriptors until the worker crashed with `unable to open database file`.

The queue worker catches unexpected job-level failures so a secondary database/logging error cannot terminate the only worker thread and silently freeze the remaining queue.

Queue items are three-tuples `(action, video_id, queue_generation)`. Never enqueue a two-tuple or bypass the generation check for transcription jobs. Semantic-only jobs carry the generation for a consistent queue shape but are governed by their semantic/status checks.

## HTTP endpoints

- `GET /api/projects`
- `GET /api/bootstrap`
- `POST /api/bootstrap/start`
- `POST /api/projects/choose`
- `POST /api/projects/{id}/open`
- `POST /api/projects/{id}/locate`
- `POST /api/projects/{id}/scan`
- `POST /api/projects/{id}/retranscribe`
- `POST /api/projects/{id}/pause`
- `POST /api/projects/{id}/resume`
- `POST /api/projects/{id}/abort`
- `GET /api/videos?project={id}`
- `POST /api/videos/{id}/transcribe`
- `GET /api/search?q={query}&project={id}`
- `GET /api/search/exact?q={query}&project={id}`
- `GET /api/search/semantic?q={query}&project={id}`
- `DELETE /api/projects/{id}`
- `GET /media/{video_id}` with HTTP byte-range support
- `/` and `/static/*`

Static files send `Cache-Control: no-store` so UI fixes appear after reload.
State-changing requests reject foreign browser origins; missing Origin remains allowed for local command-line diagnostics.

## Operational notes

- Server-side changes require stopping with `Control-C` and rerunning `start.command`.
- Static changes usually require only a hard browser reload, but restarting is the safest handoff instruction.
- If macOS blocks a command file, the user may need to open it through Finder's context menu or Privacy & Security.
- Keep the Terminal window open during long transcription runs. `start.command` automatically prevents idle system sleep through `caffeinate`; no manual Energy settings change is normally needed.
- Screen locking and display sleep are safe. Closing the MacBook lid normally forces sleep and cannot be reliably overridden by this app.
- External SSD disconnection causes media access errors but must not delete completed transcript data.
- `setup.command` performs Apple-Silicon, Python ≥3.11, ffmpeg/ffprobe, and 8-GB-free-space checks and installs `requirements.lock`. Model verification/download now belongs to the in-app model gate.
- `setup.ps1` performs the corresponding Windows dependency checks and installs the platform-marked requirements.
- `.github/workflows/build.yml` tests and packages CI artifacts on native `macos-15` ARM64 and `windows-latest` runners.
- `.github/workflows/release.yml` is manual-only and uses the protected `release` environment. It signs/notarizes/staples the Apple Silicon DMG, verifies the Windows bundle, generates checksums plus `latest.json`, and creates a draft release that must be published manually.
- Build version metadata is generated from `VERSION` before PyInstaller. The packaged UI reads `/api/app`; never hardcode a displayed version in HTML or JavaScript.

## Verification checklist

Run after changes:

```bash
.venv/bin/python -m py_compile server.py transcribe_video.py prepare_semantic.py prepare_models.py model_manager.py rothbald.py
node --check static/app.js
.venv/bin/python -m unittest discover -s tests -v
```

Also verify proportionally to the change:

- SQLite migration on a temporary database or backup;
- `progress_from_line()` against tqdm-style output;
- semantic model retrieval with `prepare_semantic.py`;
- semantic BLOB storage/search on a copied database;
- exact and semantic result counts without hidden limits;
- HTTP API after restarting the local server.

Do not modify or delete user media during testing. Prefer temporary files and copied databases.

## Known limitations

- Timestamps are file-relative, not embedded source timecode or Premiere sequence timecode.
- Embedded WebView playback depends on its codec/container support; transcription can support formats the player cannot preview.
- Speaker diarization is not implemented.
- Whisper may hallucinate repetitive text in poor audio; semantic indexing filters obvious repetition but cannot repair the transcript.
- Semantic relevance is threshold-based and may require future tuning or a user-adjustable sensitivity control.
- Exact results are segment-level rather than per-token-occurrence.
- Checkpoint resume is at 30-minute granularity; the one interrupted part restarts, not the entire media file.
- There is no queue reordering control.

## Packaging status

- Native CI bundles are prepared through PyInstaller and GitHub Actions; the manual release path additionally signs and notarizes Apple Silicon.
- The bundle opens as a standalone desktop window through pywebview, not as a browser tab. PyInstaller embeds the platform-specific Dock/executable icon.
- Model updates are checked in-app against published Hugging Face revisions and downloaded with exact byte progress.
- Draft GitHub Release hosting, embedded build identity, macOS Developer ID signing/notarization, and cross-platform SHA-256 manifests are implemented. Automatic binary updates and Windows Authenticode still require a separate product decision/certificate.

## Changelog

### 2026-07-27

- Added a quiet application footer with `baldojnisyly@gmail.com` support contact and the version embedded by the current build. The UI fetches `/api/app`; it contains no hardcoded version string.
- Added the single four-part `VERSION`, generated build metadata, macOS `Info.plist` versioning, Windows executable version resources, and regression coverage for embedded metadata precedence.
- Added a manual-only protected release workflow modeled on `yt-dlp BD`: Windows x64 verification, Apple Silicon Developer ID signing, hardened runtime, notarization, stapling, Gatekeeper validation, checksums, `latest.json`, and draft GitHub Release assembly.
- Added owner handoff and Apple credential documentation. Tauri updater keys are intentionally not reused because Rothbald is a PyInstaller application and cannot verify Tauri updater signatures.

### 2026-07-26

- Replaced the browser-launching packaged shell with a standalone pywebview window sized for 13-inch screens (1280×800, 960×640 minimum), using WKWebView on macOS and WebView2 on Windows.
- Reworked the UI into a predominantly monochrome Helvetica system using black, graphite, neutral gray, and white. Low-saturation pastel violet marks important numbers, indices, focus, selection, live progress, result markers, and functional card separators; pastel green is reserved for completion, while pastel red is reserved for destructive actions, errors, and unavailable media. Increased section, card, metadata, search, and player-caption spacing for dense text-heavy projects. Search remains prominent through structure and scale, and the completed-processing panel stays auto-collapsing and manually expandable.
- Replaced the generated single-letter icon with a deterministic black rounded `Ro` icon using a large white Bradley Hand mark; calibrated its transparent canvas for standard Dock sizing and added reproducible PNG, ICNS, ICO, favicon, and PyInstaller wiring.
- Replaced the home headline with a concise product explanation and capability list; removed SSD-specific language from all user-facing UI because project media may live on any accessible storage.
- Reduced repeated status styling in the video list: completed cards show one neutral `Готово` state without a full progress bar or a second semantic-ready line; hover and captions remain grayscale, while selection and separators use the restrained violet signal.
- Completed video cards use only a small green dot beside neutral `Готово`, preventing repeated color-heavy labels. When every media file is offline, processing now reports `Файли проєкту зараз недоступні`, highlights the state softly in red, confirms how many transcripts remain saved, and points to reconnecting the disk or using Locate while text search continues to work.
- Added an inline `Locate` action directly beside the all-media-offline explanation so the user can repair the project path without returning to the recent-project screen.

- Renamed the product and all primary UI identity to Rothbald (`rothpithnavach baldojnyi`) without using a `Z`; added a handwritten system-font wordmark and matching favicon.
- Rebuilt the interface as a dark editorial system inspired by the Spiilka/Kunsht case study: strict grid, oversized typography, thin rules, orange signal color, acid progress accents, flat project/result rows, and responsive layout.
- Added an in-app model gate with total and per-model progress, exact byte counts, current file, first-launch downloads, online revision checks, offline fallback for intact snapshots, and retryable errors.
- Added Windows support through Faster-Whisper Turbo, a native folder picker, portable child-process streaming/termination, Windows launch/setup scripts, and packaged data locations.
- Added a frozen launcher path for transcription subprocesses, bundled ffmpeg discovery, a PyInstaller onedir spec, native ARM64 macOS/Windows GitHub Actions builds, and repository ignore rules.
- Expanded regression coverage to ten tests, including packaged transcription routing and detached weighted model-progress snapshots; verified Python/JavaScript syntax, tests, startup UI, final home UI, and clean browser logs.

### 2026-07-24

- Added persistent exact/semantic result selection with a strong active-card state, `aria-pressed`, survival across tab/rerender updates, transfer to another result, and repeat-click deselection that leaves playback running.
- Added a responsive caption strip below the player with the selected video's full name and project-relative path; it clears with player selection and updates from both sidebar and search results.
- Completed the full pre-packaging review patch. Rebuilt the legacy video schema to remove global source-path uniqueness while retaining project-relative uniqueness and old transcripts; added revision-gated semantic indexes, fast `segment_terms`, rolling SQLite backups, strict `Locate` fingerprints, ffprobe timeouts outside write transactions, safe Range parsing, local-origin checks, and project deletion that never touches media.
- Replaced whole-file transcription with 30-minute resumable checkpoints plus two-second overlaps, temporary ffmpeg audio extraction, atomic queue claims, generation checks, and full process-group termination. Pause now preserves completed parts and Abort intentionally clears them.
- Split exact and semantic HTTP searches so exact results display without waiting for the embedding model; fixed false positives such as `мир` inside `Владимир` and `рост` inside `просто`; added client request cancellation and 100-row incremental rendering without truncating results.
- Added a separate semantic-index progress bar, fixed polling through final indexing, robust UI/network error handling, accessible labels/live regions/progress values, keyboard focus states, project deletion controls, and a favicon. Verified the finished UI in the local in-app browser with clean console logs.
- Added deterministic dependency locking, setup preflight checks, setup-time model downloads, runtime offline Hugging Face mode, and eight regression tests covering old-database migration, foreign-key integrity, atomic queue claims, real checkpoint reuse/reassembly, typo/inflection matching, false-positive prevention, checkpoint ranges, normalization, and HTTP byte ranges.
- Removed the stray `.start.command.swp` and generated Python caches; added ignore rules for future swap files.
- Downloaded the previously missing Turbo weights, constrained setup downloads to runtime-required files (excluding the unused 2+ GB ONNX export), removed 632+ MB of interrupted duplicate blobs, and verified both models offline. A real one-second MLX/Metal Turbo transcription smoke test completed successfully.

- Migrated semantic retrieval to the locally cached and verified `intfloat/multilingual-e5-large-instruct`, including its required query instruction, raw document format, 1024-dimensional vector validation, a `0.70` relevance floor, and versioned semantic indexes. Measured warm query encoding at about 0.9 seconds on the target M1; initial model load was about 7.5 seconds.
- Replaced fixed minute chunks with natural transcript blocks: sentence splitting with proportional internal timestamps, pause-aware and sentence-aware boundaries, rolling lexical topic-change detection, and hard size/time bounds. On the former 11.5-hour test corpus this produced 882 filtered blocks averaging about 24 seconds, with no block over 58 seconds.
- Added typo-tolerant text matching without result limits: keyboard-letter and orthographic normalization, basic inflections, and one edit/transposition for words of five or more letters. The same query feeds both the text and semantic tabs.
- Removed the sole first/test project `Виступи` from local storage at the user's request, including its 11 video records, 13,199 segments, 637 old semantic chunks, and 11 JSON transcripts. No source media was touched. SQLite integrity remains OK; the temporary verification backup was also removed after all migration tests passed.
- Removed the obsolete 470 MB `multilingual-e5-small` cache after Large Instruct passed a real local retrieval check.
- Fixed semantic migration/recovery to use the presence of saved transcript segments rather than requiring video status `done`; paused, cancelled, or offline videos with existing text can now rebuild an incompatible/missing semantic index without restarting Whisper.
- Audited application storage after switching models: all 11 transcript JSON files match the 11 database video records and SQLite integrity is OK. Removed the obsolete 459 MB `whisper-small-mlx` Hugging Face cache while retaining the required semantic model, environment, database, and transcripts.
- Replaced the sole transcription model with `mlx-community/whisper-large-v3-turbo`; removed the `WHISPER_MODEL` override and model-choice instructions so every new or repeated transcription consistently uses Turbo.
- Recorded automatic update checking/downloading as a requirement for the future signed native macOS application; it is deliberately not implemented in the current development launcher.
- Changed restart behavior so existing transcription queues never auto-run. Interrupted/queued work is persisted as paused with an advanced generation and starts only after the user opens the project and presses `Продовжити`.
- Added persistent queue generations so stale in-memory transcription jobs from repeated Pause/Abort/restart cycles are rejected before Whisper starts. A new full-folder run also advances the generation.
- Backed up the live database and removed the orphaned development fixture `sample.wav`/`testproject` plus its cascaded segment and semantic chunk. Post-cleanup checks show zero duplicate rows, zero orphans, and `integrity_check=ok`.
- Fixed a recurring SQLite file-descriptor leak: every database context now explicitly commits/rolls back and closes. Added worker-level exception recovery so one failed job cannot kill the entire queue thread. Verified 3,000 consecutive database contexts with no descriptor growth.
- Wrapped the server process in macOS `caffeinate -i`, allowing the screen to lock or turn off while preventing the Mac from entering idle system sleep during transcription.
- Removed the redundant project selector from the search bar and renamed the general search action from `Знайти за змістом` to `Знайти`; result-type tabs still separate semantic and exact matches.
- Added persistent per-project queue pause/resume and immediate abort controls, including safe Whisper subprocess termination and stale-job guards.
- Added `paused` and `cancelled` video states. Resume restarts only the interrupted/paused files; abort preserves completed transcripts and enables a fresh full-folder re-transcription.
- Added a Photoshop/Premiere-style recent-project home screen; startup no longer auto-opens the last project.
- Added stable project-relative video identity, quick media availability checks, missing-media states, and `Locate` for moved or renamed folders.
- Changed rescanning and startup checks to preserve transcripts and semantic indexes when media or an external SSD is missing.
- Added `READMEAI.md` as persistent technical memory and linked it prominently from `README.md`.
- Added full-folder re-transcription with confirmation, idle-queue protection, and atomic transcript replacement.
- Added repeat-click and player `×` controls to clear video selection.
- Diagnosed a real transcription stall at 8%; reserved Metal for Whisper, moved semantic embeddings to CPU, and added stalled-state UI plus a 15-minute watchdog.
- Removed exact and semantic result-count limits and split results into `За змістом`, `Точні`, and `Усі` tabs.

### 2026-07-23

- Built the initial local M1 application with native project-folder selection, recursive media discovery, MLX Whisper transcription, SQLite storage, and timestamped playback.
- Added per-video and whole-queue progress/ETA.
- Added project-wide search independent of player selection.
- Added multilingual semantic search with `multilingual-e5-small`, hybrid exact/semantic results, and semantic-noise filtering.
- Fixed long filenames overflowing the sidebar into the player.
- Added cache prevention for static assets.
